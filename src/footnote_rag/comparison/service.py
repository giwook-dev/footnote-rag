"""전기 대비 주석 변동 분석 서비스: 비교 생성(코드), 주석 요약(로컬 모델, 요청할 때만).

역할: ``create_comparison``은 같은 조직·같은 회사(corp_code)의 ready 상태 DART 문서 두 건을 검사하고, 원문 ZIP
(캐시 우선)을 파싱해 ``comparison.notes.compare_documents``로 비교한 결과를 저장하고 감사 로그
``comparison.created``를 남긴다. ``summarize_note``는 저장된 결과의 주석 하나를 로컬 모델(``chat_json``)에
보내 요약받고, 요약의 근거 줄 번호·숫자를 검증해 ``ok``/``needs_review``로 저장한다(``llm_calls``와
감사 로그 ``comparison.summarized`` 포함). Claude(``LLM_PROVIDER=real``)는 비용 때문에 지원하지 않는다(501).
주요 요소: ``create_comparison``, ``summarize_note``, ``list_comparisons``, ``get_comparison``,
``latest_summaries``, ``ComparisonError``(HTTP 상태 포함), ``REAL_LLM_UNSUPPORTED``.
호출 관계: ``comparison.router``와 ``evals.note_diff``가 호출한다. ``jobs.handlers.load_dart_zip``·``dart_cache_dir``,
``dart.parser.parse_document_zip``, ``comparison.prompt``, ``usage.pricing.compute_cost``, ``audit.service.record``를 부른다.
학습 문서: docs/study/11-note-change-analysis.md, docs/adr/0012-note-change-analysis.md
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.answering.llm import JsonResult, LLMError
from footnote_rag.audit.service import Actions, record
from footnote_rag.comparison.notes import SCOPES, DiffOptions, compare_documents
from footnote_rag.comparison.prompt import (
    SUMMARY_SCHEMA,
    SUMMARY_SYSTEM_PROMPT,
    build_summary_prompt,
    check_bullets,
    is_summary_shape,
)
from footnote_rag.config import Settings
from footnote_rag.dart.client import DartClient, DartError
from footnote_rag.dart.parser import DartParseError, ParsedDocument, parse_document_zip
from footnote_rag.db.models import (
    Document,
    LlmCall,
    NoteComparison,
    NoteComparisonSummary,
    User,
)
from footnote_rag.jobs.handlers import dart_cache_dir, dart_url, load_dart_zip
from footnote_rag.observability import get_logger
from footnote_rag.usage.pricing import compute_cost

log = get_logger(__name__)

RESULT_VERSION = 1
ANNUAL_REPORT_WORD = "사업보고서"
REAL_LLM_UNSUPPORTED = "변동 요약은 현재 로컬 LLM에서만 지원합니다"
DOCUMENT_NOT_FOUND = "Document not found"
COMPARISON_NOT_FOUND = "Comparison not found"


class ComparisonError(Exception):
    """A request the comparison service rejects, with the HTTP status for the router."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def diff_options(settings: Settings) -> DiffOptions:
    return DiffOptions(
        change_threshold=Decimal(str(settings.note_diff_change_threshold)),
        max_lines=settings.note_diff_max_lines,
        watchlist=settings.note_diff_watchlist_words,
    )


def _rcept_no(doc: Document) -> str:
    return str((doc.meta or {}).get("rcept_no") or doc.source_ref or "")


def _document_info(doc: Document, parsed: ParsedDocument | None = None) -> dict[str, Any]:
    meta = doc.meta or {}
    rcept_no = _rcept_no(doc)
    return {
        "document_id": str(doc.id),
        "rcept_no": rcept_no,
        "corp_name": meta.get("corp_name") or (parsed.company_name if parsed else None),
        "corp_code": meta.get("corp_code"),
        "report_nm": meta.get("report_nm") or (parsed.document_name if parsed else None),
        "rcept_dt": meta.get("rcept_dt"),
        "title": doc.title,
        "dart_url": meta.get("dart_url") or (dart_url(rcept_no) if rcept_no else None),
    }


async def _load_documents(
    session: AsyncSession, user: User, prior_id: uuid.UUID, current_id: uuid.UUID
) -> tuple[Document, Document]:
    docs = {
        d.id: d
        for d in await session.scalars(
            select(Document).where(
                Document.org_id == user.org_id, Document.id.in_([prior_id, current_id])
            )
        )
    }
    prior, current = docs.get(prior_id), docs.get(current_id)
    if prior is None or current is None:
        raise ComparisonError(404, DOCUMENT_NOT_FOUND)
    return prior, current


def check_pair(prior: Document, current: Document) -> None:
    """422 checks of a prior/current pair (same organization is checked before: 404)."""
    for doc in (prior, current):
        if doc.source != "dart":
            raise ComparisonError(
                422, "OpenDART에서 가져온 문서만 비교할 수 있습니다(업로드 문서 제외)."
            )
        if doc.status != "ready":
            raise ComparisonError(
                422, f"처리가 끝난(ready) 문서만 비교할 수 있습니다(현재 상태: {doc.status})."
            )
    if prior.id == current.id:
        raise ComparisonError(422, "전년 보고서와 당년 보고서가 같은 문서입니다.")
    prior_corp = (prior.meta or {}).get("corp_code")
    current_corp = (current.meta or {}).get("corp_code")
    if not prior_corp or prior_corp != current_corp:
        raise ComparisonError(422, "같은 회사(corp_code)의 보고서만 비교할 수 있습니다.")
    prior_dt = str((prior.meta or {}).get("rcept_dt") or "")
    current_dt = str((current.meta or {}).get("rcept_dt") or "")
    if not prior_dt or not current_dt or prior_dt >= current_dt:
        raise ComparisonError(
            422, "전년 보고서의 접수일이 당년 보고서의 접수일보다 이전이어야 합니다."
        )


async def _parse(dart_client: DartClient, settings: Settings, doc: Document) -> ParsedDocument:
    rcept_no = _rcept_no(doc)
    try:
        content = await load_dart_zip(
            dart_client, dart_cache_dir(settings.dart_dir, dart_client), rcept_no
        )
    except DartError as exc:
        log.warning("comparison.dart_failed", rcept_no=rcept_no, code=exc.code)
        raise ComparisonError(
            503, f"OpenDART 원문을 가져오지 못했습니다({rcept_no}). 잠시 후 다시 시도하세요."
        ) from None
    try:
        return await asyncio.to_thread(parse_document_zip, content, rcept_no)
    except DartParseError as exc:
        raise ComparisonError(422, f"원문을 파싱하지 못했습니다({rcept_no}): {exc}") from None


async def create_comparison(
    session: AsyncSession,
    *,
    user: User,
    prior_document_id: uuid.UUID,
    current_document_id: uuid.UUID,
    scope: str,
    dart_client: DartClient,
    settings: Settings,
) -> NoteComparison:
    """Compare the notes of two annual reports of one company and store the result."""
    if scope not in SCOPES:
        raise ComparisonError(422, f"scope는 {', '.join(SCOPES)} 중 하나여야 합니다.")
    prior, current = await _load_documents(session, user, prior_document_id, current_document_id)
    check_pair(prior, current)
    started = time.perf_counter()
    prior_parsed = await _parse(dart_client, settings, prior)
    current_parsed = await _parse(dart_client, settings, current)
    result = await asyncio.to_thread(
        compare_documents,
        prior_parsed.sections,
        current_parsed.sections,
        scope=scope,  # type: ignore[arg-type]
        options=diff_options(settings),
    )
    prior_info = _document_info(prior, prior_parsed)
    current_info = _document_info(current, current_parsed)
    warnings = [
        f"{side} 보고서명에 '{ANNUAL_REPORT_WORD}'가 없습니다: {info['report_nm'] or '-'}"
        for side, info in (("전년", prior_info), ("당년", current_info))
        if ANNUAL_REPORT_WORD not in str(info["report_nm"] or "")
    ]
    if result["summary"]["notes"] == 0:
        warnings.append("두 보고서 모두에서 해당 범위의 재무제표 주석을 찾지 못했습니다.")
    result = {
        "version": RESULT_VERSION,
        "prior": prior_info,
        "current": current_info,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "warnings": warnings,
        **result,
    }
    comparison = NoteComparison(
        id=uuid.uuid4(),
        org_id=user.org_id,
        created_by=user.id,
        prior_document_id=prior.id,
        current_document_id=current.id,
        scope=scope,
        result=result,
    )
    session.add(comparison)
    summary = result["summary"]
    record(
        session,
        action=Actions.COMPARISON_CREATED,
        org_id=user.org_id,
        actor_user_id=user.id,
        target_type="note_comparison",
        target_id=comparison.id,
        metadata={
            "prior_rcept_no": prior_info["rcept_no"],
            "current_rcept_no": current_info["rcept_no"],
            "scope": scope,
            "notes": summary["notes"],
            "status_counts": summary["status_counts"],
            "prior_mismatch": summary["prior_mismatch"],
            "large_change": summary["large_change"],
            "ambiguous_rows": summary["ambiguous_rows"],
        },
    )
    await session.commit()
    await session.refresh(comparison)
    log.info(
        "comparison.created",
        comparison_id=str(comparison.id),
        notes=summary["notes"],
        elapsed_ms=result["elapsed_ms"],
    )
    return comparison


async def get_comparison(
    session: AsyncSession, user: User, comparison_id: uuid.UUID
) -> NoteComparison:
    comparison = await session.scalar(
        select(NoteComparison).where(
            NoteComparison.id == comparison_id, NoteComparison.org_id == user.org_id
        )
    )
    if comparison is None:
        raise ComparisonError(404, COMPARISON_NOT_FOUND)
    return comparison


async def list_comparisons(
    session: AsyncSession, user: User, *, limit: int = 20
) -> list[NoteComparison]:
    return list(
        await session.scalars(
            select(NoteComparison)
            .where(NoteComparison.org_id == user.org_id)
            .order_by(NoteComparison.created_at.desc())
            .limit(limit)
        )
    )


async def latest_summaries(
    session: AsyncSession, comparison: NoteComparison
) -> dict[int, NoteComparisonSummary]:
    """Latest summary per note index (a regenerated summary replaces the older one)."""
    rows = await session.scalars(
        select(NoteComparisonSummary)
        .where(
            NoteComparisonSummary.comparison_id == comparison.id,
            NoteComparisonSummary.org_id == comparison.org_id,
        )
        .order_by(NoteComparisonSummary.created_at)
    )
    return {row.note_index: row for row in rows}


@dataclass
class SummaryResult:
    summary: NoteComparisonSummary
    latency_ms: int
    cost_usd: Decimal | None


def _supports_summary(llm: Any, settings: Settings) -> bool:
    return settings.llm_mode != "real" and callable(getattr(llm, "chat_json", None))


def _report_name(info: dict[str, Any]) -> str:
    parts = [info.get("corp_name"), info.get("report_nm"), info.get("rcept_no")]
    return " · ".join(str(p) for p in parts if p) or "-"


async def summarize_note(
    session: AsyncSession,
    *,
    user: User,
    comparison_id: uuid.UUID,
    note_index: int,
    llm: Any,
    settings: Settings,
) -> SummaryResult:
    """Summarize one note's changes with the local (or fake) model and check the summary."""
    if not _supports_summary(llm, settings):
        raise ComparisonError(501, REAL_LLM_UNSUPPORTED)
    comparison = await get_comparison(session, user, comparison_id)
    notes = (comparison.result or {}).get("notes") or []
    if not 0 <= note_index < len(notes):
        raise ComparisonError(404, "Note not found")
    note = notes[note_index]
    if note.get("status") == "unchanged":
        raise ComparisonError(422, "변동이 없는 주석은 요약하지 않습니다.")
    prompt = build_summary_prompt(
        note,
        prior_report=_report_name(comparison.result.get("prior") or {}),
        current_report=_report_name(comparison.result.get("current") or {}),
    )
    org_id, user_id = user.org_id, user.id
    try:
        reply: JsonResult = await llm.chat_json(
            system=SUMMARY_SYSTEM_PROMPT,
            user=prompt.user,
            schema=SUMMARY_SCHEMA,
            validate=is_summary_shape,
        )
    except LLMError as exc:
        log.warning("comparison.summary_failed", status_code=exc.status_code)
        raise
    data = reply.data if is_summary_shape(reply.data) else {"bullets": []}
    checked = check_bullets(data, prompt)
    summary = NoteComparisonSummary(
        id=uuid.uuid4(),
        comparison_id=comparison.id,
        org_id=org_id,
        note_index=note_index,
        note_key=str(note.get("key")),
        bullets=checked.bullets,
        status=checked.status,
        ungrounded_numbers=checked.ungrounded_numbers,
        model=reply.model_id,
        created_by=user_id,
    )
    session.add(summary)
    cost = compute_cost(reply.model_id, reply.usage, settings.llm_prices)
    session.add(
        LlmCall(
            id=uuid.uuid4(),
            org_id=org_id,
            answer_id=None,
            note_comparison_id=comparison.id,
            model=reply.model_id,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            cache_read_input_tokens=reply.usage.cache_read_input_tokens,
            cache_creation_input_tokens=reply.usage.cache_creation_input_tokens,
            cost_usd=cost,
            latency_ms=reply.latency_ms,
            stop_reason=reply.done_reason,
            dropped_source_refs=sum(len(b["dropped_refs"]) for b in checked.bullets),
        )
    )
    record(
        session,
        action=Actions.COMPARISON_SUMMARIZED,
        org_id=org_id,
        actor_user_id=user_id,
        target_type="note_comparison",
        target_id=comparison.id,
        metadata={
            "note_index": note_index,
            "note_key": note.get("key"),
            "status": checked.status,
            "model": reply.model_id,
            "latency_ms": reply.latency_ms,
            "bullets": len(checked.bullets),
            "ungrounded_numbers": checked.ungrounded_numbers[:10],
        },
    )
    await session.commit()
    await session.refresh(summary)
    return SummaryResult(summary=summary, latency_ms=reply.latency_ms, cost_usd=cost)
