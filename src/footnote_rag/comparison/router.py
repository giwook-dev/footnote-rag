"""전기 대비 주석 변동 분석 엔드포인트(``/comparisons``, 인증된 모든 역할, 조직 격리).

역할: 비교 생성(``POST /comparisons``), 최근 목록, 결과 조회(주석별 최신 요약 포함), 주석 하나의 요약 생성
(``POST /comparisons/{id}/notes/{note_index}/summary``, 로컬/Fake LLM만)을 제공한다. 서비스의
``ComparisonError``와 ``LLMError``를 HTTP 상태(404/422/501/502/503/504)로 바꾼다.
주요 요소: ``post_comparison``, ``list_comparisons_endpoint``, ``get_comparison_endpoint``, ``post_summary``,
요청·응답 모델(``ComparisonIn``, ``ComparisonOut``, ``ComparisonResult``, ``NoteDiff``, ``SummaryOut`` 등).
호출 관계: ``comparison.service``를 호출한다. 웹 화면(``/ui``)의 "변동 분석" 탭이 쓴다.
학습 문서: docs/study/11-note-change-analysis.md
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.answering.llm import LLMError
from footnote_rag.answering.router import get_llm
from footnote_rag.auth.deps import ANY_MEMBER, require_role
from footnote_rag.comparison.service import (
    ComparisonError,
    create_comparison,
    get_comparison,
    latest_summaries,
    list_comparisons,
    summarize_note,
)
from footnote_rag.config import get_settings
from footnote_rag.dart.client import DartClient
from footnote_rag.db.models import NoteComparison, NoteComparisonSummary, User
from footnote_rag.db.session import get_session
from footnote_rag.providers import MissingKeyError, make_dart_client

router = APIRouter(tags=["comparisons"])

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such document, comparison or note in your organization"},
    422: {"description": "The pair cannot be compared (see detail, Korean message)"},
}


# ---- request / response models ----


class ComparisonIn(BaseModel):
    prior_document_id: uuid.UUID = Field(description="전년 사업보고서 문서 id")
    current_document_id: uuid.UUID = Field(description="당년 사업보고서 문서 id")
    scope: Literal["consolidated", "separate"] = Field(
        default="consolidated", description="consolidated = 연결재무제표 주석, separate = 별도"
    )


class DocumentInfo(BaseModel):
    document_id: str
    rcept_no: str
    corp_name: str | None = None
    corp_code: str | None = None
    report_nm: str | None = None
    rcept_dt: str | None = None
    title: str | None = None
    dart_url: str | None = None


class LineEntry(BaseModel):
    op: Literal["insert", "delete"]
    ref: str = Field(description="P{n} = 전년 주석의 n번째 줄, C{n} = 당년 주석의 n번째 줄")
    text: str
    numbers_only: bool = Field(description="숫자를 #로 바꾸면 짝 줄과 같다(숫자만 바뀜)")
    pair: str | None = Field(default=None, description="numbers_only 짝 줄 번호")


class CellChange(BaseModel):
    position: int = Field(description="행 안의 숫자 위치(1부터)")
    prior: str
    current: str
    delta: str
    change_pct: str | None = Field(default=None, description="증감률(%), 전년 0이면 null")
    flags: list[str]


class NumberChange(BaseModel):
    kind: Literal["yoy", "prior_mismatch", "row_added", "row_removed"]
    table: str
    label: str
    occurrence: int
    unit: str | None = Field(
        default=None,
        description="표의 단위(예: 백만원, 원문 '(단위 : …)' 표기). 표기가 없거나 이전 결과면 null",
    )
    prior_values: list[str] = Field(
        description="yoy: 전년 보고서 당기 값, prior_mismatch: 전년 보고서 당기 값"
    )
    current_values: list[str] = Field(
        description="yoy: 당년 보고서 당기 값, prior_mismatch: 당년 보고서 전기 값"
    )
    prior_ref: str | None = None
    current_ref: str | None = None
    cells: list[CellChange]
    flags: list[str]


class LineStats(BaseModel):
    equal: int
    text_added: int
    text_removed: int
    numbers_only: int


class NoteCounts(BaseModel):
    prior_mismatch: int
    large_change: int
    yoy: int
    row_added: int
    row_removed: int


class NoteDiff(BaseModel):
    index: int
    key: str
    title: str
    prior_title: str | None = None
    current_title: str | None = None
    prior_section_path: str | None = None
    current_section_path: str | None = None
    status: Literal["added", "removed", "changed", "unchanged"]
    matched_by: Literal["key", "similar_title"] | None = None
    watchlist: bool
    score: int
    prior_line_count: int
    current_line_count: int
    line_stats: LineStats
    lines: list[LineEntry]
    truncated: bool
    number_changes: list[NumberChange]
    counts: NoteCounts
    ambiguous_rows: int


class ComparisonCounts(BaseModel):
    notes: int
    prior_notes: int
    current_notes: int
    status_counts: dict[str, int]
    matched_by: dict[str, int]
    prior_mismatch: int
    large_change: int
    ambiguous_rows: int
    watchlist_changed: int


class ComparisonResult(BaseModel):
    version: int
    prior: DocumentInfo
    current: DocumentInfo
    scope: str
    generated_at: str
    elapsed_ms: int
    warnings: list[str]
    settings: dict[str, Any]
    summary: ComparisonCounts
    notes: list[NoteDiff]


class SummaryBullet(BaseModel):
    text: str
    refs: list[str]
    dropped_refs: list[str] = []
    ungrounded_numbers: list[str] = []
    grounded: bool


class SummaryOut(BaseModel):
    id: uuid.UUID
    comparison_id: uuid.UUID
    note_index: int
    note_key: str
    bullets: list[SummaryBullet]
    status: Literal["ok", "needs_review"]
    ungrounded_numbers: list[str]
    model: str
    created_at: datetime
    latency_ms: int | None = Field(default=None, description="요약을 만든 응답에만 있다")
    cost_usd: str | None = None


class ComparisonOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    prior_document_id: uuid.UUID
    current_document_id: uuid.UUID
    scope: str
    result: ComparisonResult
    summaries: dict[str, SummaryOut] = Field(
        default_factory=dict, description="note_index(문자열) → 그 주석의 최신 요약"
    )


class ComparisonListItem(BaseModel):
    id: uuid.UUID
    created_at: datetime
    corp_name: str | None
    prior_rcept_no: str
    current_rcept_no: str
    prior_report_nm: str | None = None
    current_report_nm: str | None = None
    scope: str
    summary: ComparisonCounts


# ---- dependencies and mapping ----


def get_dart_client(request: Request) -> DartClient:
    """``app.state.dart_client`` when set (tests), else one per request from the settings."""
    client = getattr(request.app.state, "dart_client", None)
    if client is not None:
        return client
    try:
        return make_dart_client(get_settings())
    except MissingKeyError:
        raise HTTPException(
            status_code=503, detail="OpenDART API 키가 설정되지 않았습니다."
        ) from None


def _http(exc: ComparisonError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _summary_out(
    row: NoteComparisonSummary, *, latency_ms: int | None = None, cost: Any = None
) -> SummaryOut:
    return SummaryOut(
        id=row.id,
        comparison_id=row.comparison_id,
        note_index=row.note_index,
        note_key=row.note_key,
        bullets=[SummaryBullet(**b) for b in row.bullets or []],
        status=row.status,  # type: ignore[arg-type]
        ungrounded_numbers=list(row.ungrounded_numbers or []),
        model=row.model,
        created_at=row.created_at,
        latency_ms=latency_ms,
        cost_usd=f"{cost:.4f}" if cost is not None else None,
    )


def _comparison_out(
    comparison: NoteComparison, summaries: dict[int, NoteComparisonSummary] | None = None
) -> ComparisonOut:
    return ComparisonOut(
        id=comparison.id,
        created_at=comparison.created_at,
        prior_document_id=comparison.prior_document_id,
        current_document_id=comparison.current_document_id,
        scope=comparison.scope,
        result=ComparisonResult.model_validate(comparison.result),
        summaries={str(i): _summary_out(s) for i, s in (summaries or {}).items()},
    )


# ---- endpoints ----


@router.post(
    "/comparisons",
    response_model=ComparisonOut,
    status_code=status.HTTP_201_CREATED,
    responses={**ERROR_RESPONSES, 503: {"description": "OpenDART unavailable"}},
)
async def post_comparison(
    body: ComparisonIn,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
    dart_client: DartClient = Depends(get_dart_client),
) -> ComparisonOut:
    """Compare the notes of a prior and a current annual report of the same company (code only,
    no LLM). Both documents must be ready DART imports of your organization."""
    try:
        comparison = await create_comparison(
            session,
            user=user,
            prior_document_id=body.prior_document_id,
            current_document_id=body.current_document_id,
            scope=body.scope,
            dart_client=dart_client,
            settings=get_settings(),
        )
    except ComparisonError as exc:
        raise _http(exc) from None
    return _comparison_out(comparison)


@router.get("/comparisons", response_model=list[ComparisonListItem])
async def list_comparisons_endpoint(
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> list[ComparisonListItem]:
    """Recent comparisons of your organization (newest first, at most 20)."""
    out = []
    for c in await list_comparisons(session, user):
        result = c.result or {}
        prior, current = result.get("prior") or {}, result.get("current") or {}
        out.append(
            ComparisonListItem(
                id=c.id,
                created_at=c.created_at,
                corp_name=current.get("corp_name") or prior.get("corp_name"),
                prior_rcept_no=str(prior.get("rcept_no") or ""),
                current_rcept_no=str(current.get("rcept_no") or ""),
                prior_report_nm=prior.get("report_nm"),
                current_report_nm=current.get("report_nm"),
                scope=c.scope,
                summary=ComparisonCounts.model_validate(result.get("summary") or {}),
            )
        )
    return out


@router.get("/comparisons/{comparison_id}", response_model=ComparisonOut, responses=ERROR_RESPONSES)
async def get_comparison_endpoint(
    comparison_id: uuid.UUID,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> ComparisonOut:
    """The full result plus the latest summary of each summarized note."""
    try:
        comparison = await get_comparison(session, user, comparison_id)
    except ComparisonError as exc:
        raise _http(exc) from None
    return _comparison_out(comparison, await latest_summaries(session, comparison))


@router.post(
    "/comparisons/{comparison_id}/notes/{note_index}/summary",
    response_model=SummaryOut,
    responses={
        **ERROR_RESPONSES,
        501: {"description": "LLM_PROVIDER=real: summaries run on the local LLM only"},
        502: {"description": "The local model returned invalid JSON twice"},
        503: {"description": "Ollama unreachable or the model is missing"},
        504: {"description": "Ollama timed out"},
    },
)
async def post_summary(
    comparison_id: uuid.UUID,
    note_index: int,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
    llm: Any = Depends(get_llm),
) -> SummaryOut:
    """Summarize one note's changes with the local model (``LOCAL_LLM_MODEL``, no cascade).
    Numbers not found in the data given to the model mark the summary ``needs_review``."""
    try:
        result = await summarize_note(
            session,
            user=user,
            comparison_id=comparison_id,
            note_index=note_index,
            llm=llm,
            settings=get_settings(),
        )
    except ComparisonError as exc:
        raise _http(exc) from None
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from None
    return _summary_out(result.summary, latency_ms=result.latency_ms, cost=result.cost_usd)
