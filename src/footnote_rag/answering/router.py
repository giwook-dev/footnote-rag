"""질문·답변·검색 엔드포인트(``POST /questions``, ``GET /answers/{id}``, ``POST /search``, ``GET /llm/status``).

역할: 서비스 예외를 HTTP 상태(404/503/502/504)로 바꾸고, member에게는 검토 대기 답변 본문을 숨긴다.
``GET /llm/status``(admin)는 현재 LLM 제공자·모델·연결 상태를 돌려준다(``/readyz``와 별개).
주요 요소: ``post_question``, ``get_answer``, ``search``, ``llm_status``, ``_visible_to``,
``get_embedder``·``get_llm``(app.state).
호출 관계: ``answering.service``의 ``ask_question``·``retrieve``를 호출한다. ``mcp_server``도 이 API를 쓴다.
학습 문서: docs/study/02-request-walkthrough.md

/questions, /answers, /search and /llm/status endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.answering.llm import LLMClient, LLMError
from footnote_rag.answering.service import (
    DocumentScopeError,
    RetrievalUnavailableError,
    ask_question,
    retrieve,
)
from footnote_rag.auth.deps import ADMINS, ANY_MEMBER, REVIEWERS, require_role
from footnote_rag.config import get_settings
from footnote_rag.db.models import Answer, User
from footnote_rag.db.session import get_session
from footnote_rag.retrieval.embedder import Embedder

router = APIRouter(tags=["questions"])

MEMBER_VISIBLE = ("published", "approved")
PENDING_NOTICE = "답변이 검토 대기 중입니다. 검토자가 승인하면 공개됩니다."


def get_embedder(request: Request) -> Embedder:
    return request.app.state.embedder


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


class QuestionIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    document_ids: list[uuid.UUID] | None = None


class AnswerOut(BaseModel):
    answer_id: uuid.UUID
    question_id: uuid.UUID | None = None
    status: str
    review_reason: str | None = None
    text: str | None
    footnotes: list[dict[str, Any]]
    usage: dict[str, Any] | None = None
    latency_ms: int | None = None
    created_at: datetime | None = None


class SearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    k: int = Field(default=8, ge=1, le=50)
    document_ids: list[uuid.UUID] | None = None


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    corp_name: str | None
    section_path: str
    score: float
    text: str
    dart_url: str | None


class LLMStatusOut(BaseModel):
    provider: str
    model: str
    base_url: str | None = None
    # None = not checked (Claude: no probe call to avoid spending tokens)
    connected: bool | None
    model_available: bool | None
    detail: str | None = None


def _visible_to(user: User, status: str) -> bool:
    return user.role in REVIEWERS or status in MEMBER_VISIBLE


@router.post("/questions", response_model=AnswerOut)
async def post_question(
    body: QuestionIn,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
    embedder: Embedder = Depends(get_embedder),
    llm: LLMClient = Depends(get_llm),
) -> AnswerOut:
    try:
        result = await ask_question(
            session,
            user=user,
            question=body.question,
            document_ids=body.document_ids,
            embedder=embedder,
            llm=llm,
            settings=get_settings(),
        )
    except DocumentScopeError:
        raise HTTPException(status_code=404, detail="Document not found") from None
    except RetrievalUnavailableError:
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from None
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from None
    a = result.answer
    visible = _visible_to(user, a.status)
    return AnswerOut(
        answer_id=a.id,
        question_id=a.question_id,
        status=a.status,
        review_reason=a.review_reason,
        text=a.text if visible else PENDING_NOTICE,
        footnotes=result.footnotes if visible else [],
        usage=result.usage,
        latency_ms=result.latency_ms,
        created_at=a.created_at,
    )


@router.get("/answers/{answer_id}", response_model=AnswerOut)
async def get_answer(
    answer_id: uuid.UUID,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> AnswerOut:
    a = await session.scalar(
        select(Answer).where(Answer.id == answer_id, Answer.org_id == user.org_id)
    )
    if a is None or not _visible_to(user, a.status):
        raise HTTPException(status_code=404, detail="Answer not found")
    return AnswerOut(
        answer_id=a.id,
        question_id=a.question_id,
        status=a.status,
        review_reason=a.review_reason,
        text=a.text,
        footnotes=a.footnotes,
        created_at=a.created_at,
    )


@router.get("/llm/status", response_model=LLMStatusOut, tags=["ops"])
async def llm_status(
    _user: User = Depends(require_role(*ADMINS)),
    llm: LLMClient = Depends(get_llm),
) -> LLMStatusOut:
    """Current LLM provider, model and reachability (local: ``GET {base}/api/tags``, 3 s)."""
    status = getattr(llm, "status", None)
    if callable(status):
        return LLMStatusOut(**await status())
    return LLMStatusOut(
        provider=str(getattr(llm, "provider", "unknown")),
        model=str(getattr(llm, "model", "unknown")),
        connected=None,
        model_available=None,
    )


@router.post("/search", response_model=list[SearchHit])
async def search(
    body: SearchIn,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
    embedder: Embedder = Depends(get_embedder),
) -> list[SearchHit]:
    try:
        chunks = await retrieve(
            session,
            user=user,
            query=body.query,
            top_k=body.k,
            document_ids=body.document_ids,
            embedder=embedder,
        )
    except DocumentScopeError:
        raise HTTPException(status_code=404, detail="Document not found") from None
    except RetrievalUnavailableError:
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from None
    return [
        SearchHit(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_title=c.document_title,
            corp_name=c.document_meta.get("corp_name"),
            section_path=c.section_path,
            score=round(c.score, 4),
            text=c.text,
            dart_url=c.document_meta.get("dart_url"),
        )
        for c in chunks
    ]
