"""질문 답변 서비스: 검색 → Claude(search_result + citations) → 각주 → 검토 게이트 → 저장.

역할: 질문 하나를 끝까지 처리하고 questions·answers·llm_calls·감사 로그 두 건을 한 트랜잭션으로 커밋한다.
주요 요소: ``ask_question``, ``retrieve``, ``review_reasons``(신뢰도 규칙), ``to_source_refs``.
호출 관계: ``answering.router``와 ``evals.run``이 호출하고, ``retrieval.search``, ``answering.llm``,
``answering.footnotes``, ``usage.pricing``, ``audit.service``를 호출한다.
학습 문서: docs/study/02-request-walkthrough.md, docs/study/06-claude-citations.md

Question answering: retrieve -> Claude with search_result citations -> footnotes -> review gate.

Review gate (``REVIEW_MODE=on_low_confidence``): an answer goes to ``pending_review`` when
  * the model refused (``refusal``),
  * retrieval returned nothing (``no_retrieval_results``; Claude is NOT called),
  * the best similarity is below ``MIN_RETRIEVAL_SCORE`` (``low_retrieval_score``), or
  * the answer carries no footnotes (``no_citations``).
Otherwise it is ``published``. ``REVIEW_MODE=always`` sends every answer to review.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.answering.footnotes import SourceRef, build_footnotes
from footnote_rag.answering.llm import NO_EVIDENCE_TEXT, LLMClient, LLMError, LLMResponse
from footnote_rag.answering.prompt import SYSTEM_PROMPT, build_messages, source_id
from footnote_rag.audit.service import Actions, record
from footnote_rag.config import Settings
from footnote_rag.db.models import Answer, Document, LlmCall, Question, User
from footnote_rag.retrieval.embedder import Embedder, EmbeddingError
from footnote_rag.retrieval.search import RetrievedChunk, search_chunks
from footnote_rag.usage.pricing import compute_cost

REFUSAL_TEXT = "모델이 이 요청에 대한 답변을 거절했습니다. 담당자 검토가 필요합니다."


class DocumentScopeError(Exception):
    """A requested document id is not visible to the caller's organization."""


class RetrievalUnavailableError(Exception):
    pass


@dataclass
class AskResult:
    answer: Answer
    footnotes: list[dict[str, Any]]
    usage: dict[str, Any]
    latency_ms: int
    retrieved: int


def to_source_refs(chunks: list[RetrievedChunk]) -> list[SourceRef]:
    return [
        SourceRef(
            source=source_id(c.document_id, c.chunk_id),
            chunk_id=str(c.chunk_id),
            document_id=str(c.document_id),
            document_title=c.document_title,
            corp_name=c.document_meta.get("corp_name"),
            section_path=c.section_path,
            dart_url=c.document_meta.get("dart_url"),
        )
        for c in chunks
    ]


def review_reasons(
    *,
    retrieved: list[RetrievedChunk],
    llm_response: LLMResponse | None,
    footnote_count: int,
    settings: Settings,
) -> list[str]:
    reasons: list[str] = []
    if llm_response is not None and llm_response.stop_reason == "refusal":
        reasons.append("refusal")
    if not retrieved:
        reasons.append("no_retrieval_results")
    elif max(c.score for c in retrieved) < settings.min_retrieval_score:
        reasons.append("low_retrieval_score")
    if footnote_count == 0 and "refusal" not in reasons:
        reasons.append("no_citations")
    if not reasons and settings.review_mode == "always":
        reasons.append("review_mode_always")
    return reasons


async def _check_document_scope(
    session: AsyncSession, org_id: uuid.UUID, document_ids: list[uuid.UUID]
) -> None:
    found = set(
        await session.scalars(
            select(Document.id).where(Document.org_id == org_id, Document.id.in_(document_ids))
        )
    )
    if found != set(document_ids):
        raise DocumentScopeError("document not found")


async def retrieve(
    session: AsyncSession,
    *,
    user: User,
    query: str,
    top_k: int,
    document_ids: list[uuid.UUID] | None,
    embedder: Embedder,
) -> list[RetrievedChunk]:
    if document_ids:
        await _check_document_scope(session, user.org_id, document_ids)
    try:
        q = await embedder.embed_query(query)
    except EmbeddingError as exc:
        raise RetrievalUnavailableError(str(exc)) from None
    return await search_chunks(
        session,
        org_id=user.org_id,
        query_vector=q.vectors[0],
        top_k=top_k,
        document_ids=document_ids or None,
    )


async def ask_question(
    session: AsyncSession,
    *,
    user: User,
    question: str,
    document_ids: list[uuid.UUID] | None,
    embedder: Embedder,
    llm: LLMClient,
    settings: Settings,
) -> AskResult:
    started = time.perf_counter()
    retrieved = await retrieve(
        session,
        user=user,
        query=question,
        top_k=settings.top_k,
        document_ids=document_ids,
        embedder=embedder,
    )

    llm_response: LLMResponse | None = None
    if not retrieved:
        text, footnotes = NO_EVIDENCE_TEXT, []
    else:
        try:
            llm_response = await llm.answer(
                system=SYSTEM_PROMPT, messages=build_messages(question, retrieved)
            )
        except LLMError as exc:
            org_id, user_id = user.org_id, user.id  # read before rollback expires `user`
            await session.rollback()
            record(
                session,
                action=Actions.QUESTION_FAILED,
                org_id=org_id,
                actor_user_id=user_id,
                target_type="question",
                metadata={"status_code": exc.status_code, "reason": exc.public_message},
            )
            await session.commit()
            raise
        if llm_response.stop_reason == "refusal":
            text, footnotes = REFUSAL_TEXT, []
        else:
            built = build_footnotes(llm_response.content, to_source_refs(retrieved))
            text, footnotes = built.text or NO_EVIDENCE_TEXT, built.footnotes

    reasons = review_reasons(
        retrieved=retrieved,
        llm_response=llm_response,
        footnote_count=len(footnotes),
        settings=settings,
    )
    status = "pending_review" if reasons else "published"

    q = Question(id=uuid.uuid4(), org_id=user.org_id, asked_by=user.id, text=question)
    session.add(q)
    await session.flush()
    answer = Answer(
        id=uuid.uuid4(),
        question_id=q.id,
        org_id=user.org_id,
        text=text,
        footnotes=footnotes,
        status=status,
        review_reason=",".join(reasons) or None,
    )
    session.add(answer)
    await session.flush()

    cost: Decimal | None = None
    usage_out: dict[str, Any] = {
        "model": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": "0.0000",
    }
    if llm_response is not None:
        cost = compute_cost(llm_response.model, llm_response.usage, settings.llm_prices)
        session.add(
            LlmCall(
                id=uuid.uuid4(),
                org_id=user.org_id,
                answer_id=answer.id,
                model=llm_response.model,
                input_tokens=llm_response.usage.input_tokens,
                output_tokens=llm_response.usage.output_tokens,
                cache_read_input_tokens=llm_response.usage.cache_read_input_tokens,
                cache_creation_input_tokens=llm_response.usage.cache_creation_input_tokens,
                cost_usd=cost,
                latency_ms=llm_response.latency_ms,
                stop_reason=llm_response.stop_reason,
            )
        )
        usage_out = {
            "model": llm_response.model,
            "input_tokens": llm_response.usage.input_tokens,
            "output_tokens": llm_response.usage.output_tokens,
            "cache_read_input_tokens": llm_response.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": llm_response.usage.cache_creation_input_tokens,
            "cost_usd": f"{cost:.4f}" if cost is not None else None,
        }

    record(
        session,
        action=Actions.QUESTION_ASKED,
        org_id=user.org_id,
        actor_user_id=user.id,
        target_type="question",
        target_id=q.id,
        metadata={
            "question_chars": len(question),
            "document_ids": [str(d) for d in document_ids or []],
            "retrieved": len(retrieved),
            "top_score": round(max((c.score for c in retrieved), default=0.0), 4),
        },
    )
    record(
        session,
        action=Actions.ANSWER_CREATED,
        org_id=user.org_id,
        actor_user_id=user.id,
        target_type="answer",
        target_id=answer.id,
        metadata={
            "question_id": str(q.id),
            "status": status,
            "review_reason": answer.review_reason,
            "footnote_count": len(footnotes),
            "llm_called": llm_response is not None,
            "model": usage_out["model"],
            "stop_reason": llm_response.stop_reason if llm_response else None,
            "cost_usd": usage_out["cost_usd"],
        },
    )
    await session.commit()
    await session.refresh(answer)
    return AskResult(
        answer=answer,
        footnotes=footnotes,
        usage=usage_out,
        latency_ms=int((time.perf_counter() - started) * 1000),
        retrieved=len(retrieved),
    )
