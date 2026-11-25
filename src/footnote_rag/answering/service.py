"""질문 답변 서비스: 검색 → Claude(search_result + citations) → 각주·숫자 검증 → 검토 게이트(+ 로컬 상향) → 저장.

역할: 질문 하나를 끝까지 처리하고 questions·answers·llm_calls·감사 로그 두 건을 한 트랜잭션으로 커밋한다.
``fallback_llm``이 있으면 모델을 바꾸면 나아질 수 있는 사유에 걸린 답만 fallback으로 한 번 더 묻는다(로컬 상향).
주요 요소: ``ask_question``, ``retrieve``/``retrieve_ranked``(연결 주석 우선 재정렬, 여러 문서 균형 검색 포함),
``interleave_by_rank``, ``review_reasons``(신뢰도 규칙), ``to_source_refs``, ``ESCALATION_REASONS``,
``escalation_triggers``.
호출 관계: ``answering.router``와 ``evals.run``이 호출하고, ``retrieval.search``, ``answering.llm``,
``answering.footnotes``, ``answering.number_check``, ``usage.pricing``, ``audit.service``를 호출한다.
학습 문서: docs/study/02-request-walkthrough.md, docs/study/06-claude-citations.md,
docs/adr/0010-local-cascade-and-number-grounding.md, docs/adr/0011-balanced-multi-document-retrieval.md

Question answering: retrieve -> Claude with search_result citations -> footnotes -> review gate.

Review gate (``REVIEW_MODE=on_low_confidence``): an answer goes to ``pending_review`` when
  * the model refused (``refusal``),
  * retrieval returned nothing (``no_retrieval_results``; Claude is NOT called),
  * the best similarity is below ``MIN_RETRIEVAL_SCORE`` (``low_retrieval_score``; measured over
    the candidates before the consolidated-first reordering, docs/adr/0009-prefer-consolidated-notes.md;
    with balanced multi-document retrieval, over every document's candidates), or
  * the answer carries no footnotes (``no_citations``), or
  * the client flagged it (``LLMResponse.review_flags``, e.g. ``truncated`` for a local model), or
  * a cited sentence has a number its cited chunks do not contain (``ungrounded_number``,
    ``NUMBER_GROUNDING_CHECK=review``; ``answering.number_check``).
Otherwise it is ``published``. ``REVIEW_MODE=always`` sends every answer to review.

Cascade (``fallback_llm``): when the primary answer has a reason in ``ESCALATION_REASONS`` or the
primary raised ``LLMError(502)`` (unusable output), the same messages go once to ``fallback_llm``
and its answer is checked again and kept. Retrieval reasons, ``review_mode_always``, 503 and 504
never escalate. ``no_citations`` of an answer the model explicitly abstained on
(``LLMResponse.abstained``, local ``insufficient: true``) does not escalate either; it still goes
to review. Every successful call gets its own ``llm_calls`` row on the same answer.

Balanced multi-document retrieval (``BALANCE_MULTI_DOCUMENT_RETRIEVAL``, 2+ ``document_ids``): each
document is searched (and consolidated-first reordered) on its own, then ``interleave_by_rank``
fills ``top_k`` rank by rank across the documents, and the result is sorted by score.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.answering.footnotes import SourceRef, build_footnotes
from footnote_rag.answering.llm import NO_EVIDENCE_TEXT, LLMClient, LLMError, LLMResponse
from footnote_rag.answering.number_check import MAX_REPORTED, UngroundedNumber, ungrounded_numbers
from footnote_rag.answering.prompt import SYSTEM_PROMPT, build_messages, source_id
from footnote_rag.audit.service import Actions, record
from footnote_rag.config import Settings, get_settings
from footnote_rag.db.models import Answer, Document, LlmCall, Question, User
from footnote_rag.observability import get_logger
from footnote_rag.retrieval.embedder import Embedder, EmbeddingError
from footnote_rag.retrieval.search import RetrievedChunk, prefer_statement_scope, search_chunks
from footnote_rag.usage.pricing import compute_cost

log = get_logger(__name__)

REFUSAL_TEXT = "모델이 이 요청에 대한 답변을 거절했습니다. 담당자 검토가 필요합니다."

# Review reasons another model might fix. Retrieval reasons (no_retrieval_results,
# low_retrieval_score) and review_mode_always do not depend on the model.
ESCALATION_REASONS = frozenset({"refusal", "truncated", "no_citations", "ungrounded_number"})
# LLMError codes that escalate: 502 = the output was unusable (e.g. invalid JSON). 503 (server
# unreachable, model missing) and 504 (timeout) would not get better with another model.
ESCALATION_STATUS_CODES = frozenset({502})


def escalation_triggers(response: LLMResponse, reasons: Sequence[str]) -> list[str]:
    """Reasons of ``reasons`` that escalate ``response`` to the fallback model.

    An explicit abstention (``response.abstained``: the model said there is no evidence) is the
    right answer to an unanswerable question; another model would only add latency. So its
    ``no_citations`` alone does not escalate. Other reasons (``truncated``, ``ungrounded_number``,
    ``refusal``) still do, and so does ``no_citations`` when the model gave source numbers that
    were all dropped (``abstained`` stays False).
    """
    triggers = [r for r in reasons if r in ESCALATION_REASONS]
    if response.abstained:
        triggers = [r for r in triggers if r != "no_citations"]
    return triggers


class DocumentScopeError(Exception):
    """A requested document id is not visible to the caller's organization."""


class RetrievalUnavailableError(Exception):
    pass


@dataclass
class Retrieval:
    """Final chunks plus the best similarity among the candidates before reordering."""

    chunks: list[RetrievedChunk]
    top_score: float | None


@dataclass
class AskResult:
    answer: Answer
    footnotes: list[dict[str, Any]]
    usage: dict[str, Any]
    latency_ms: int
    retrieved: int
    # Cascade: the fallback model was called, and the model ids called, in order.
    escalated: bool = False
    models: list[str] = field(default_factory=list)
    escalation_reasons: list[str] = field(default_factory=list)
    # Numbers of the final answer missing from their cited chunks (number_check).
    ungrounded_numbers: list[UngroundedNumber] = field(default_factory=list)


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
    top_score: float | None = None,
    ungrounded_numbers: Sequence[object] = (),
) -> list[str]:
    """Review reasons; ``top_score`` (default: best score in ``retrieved``) is compared with
    ``MIN_RETRIEVAL_SCORE``. ``ask_question`` passes the pre-reordering candidate maximum.
    ``ungrounded_numbers`` (``number_check.ungrounded_numbers``) adds ``ungrounded_number`` when
    ``NUMBER_GROUNDING_CHECK=review``."""
    reasons: list[str] = []
    if llm_response is not None and llm_response.stop_reason == "refusal":
        reasons.append("refusal")
    if llm_response is not None:
        # Client-specific flags, e.g. "truncated" when a local model hit num_predict.
        flags = getattr(llm_response, "review_flags", None) or []
        reasons.extend(f for f in flags if f not in reasons)
    if not retrieved:
        reasons.append("no_retrieval_results")
    elif (
        top_score if top_score is not None else max(c.score for c in retrieved)
    ) < settings.min_retrieval_score:
        reasons.append("low_retrieval_score")
    if footnote_count == 0 and "refusal" not in reasons:
        reasons.append("no_citations")
    if ungrounded_numbers and settings.number_grounding_check == "review":
        reasons.append("ungrounded_number")
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


def interleave_by_rank(
    ranked_lists: Sequence[Sequence[RetrievedChunk]], top_k: int
) -> list[RetrievedChunk]:
    """Round robin by rank: every list's 1st, then every list's 2nd, ... (list order kept) until
    ``top_k`` chunks. A list that runs out leaves its slots to the others' next ranks. The
    result is sorted by score, highest first (ties keep the round-robin order)."""
    picked: list[RetrievedChunk] = []
    rank = 0
    while len(picked) < top_k and any(rank < len(lst) for lst in ranked_lists):
        for lst in ranked_lists:
            if len(picked) >= top_k:
                break
            if rank < len(lst):
                picked.append(lst[rank])
        rank += 1
    return sorted(picked, key=lambda c: c.score, reverse=True)


async def retrieve_ranked(
    session: AsyncSession,
    *,
    user: User,
    query: str,
    top_k: int,
    document_ids: list[uuid.UUID] | None,
    embedder: Embedder,
    settings: Settings | None = None,
) -> Retrieval:
    """Vector search scoped to the caller's org, then the consolidated-first reordering.

    ``PREFER_CONSOLIDATED=true``: fetch ``max(top_k, RETRIEVAL_CANDIDATES)`` candidates and pick
    ``top_k`` with ``prefer_statement_scope``. ``false``: exactly the plain top-k search.

    ``BALANCE_MULTI_DOCUMENT_RETRIEVAL=true`` and 2+ distinct ``document_ids``: the above runs per
    document (``search_chunks`` with that one document), then ``interleave_by_rank`` picks
    ``top_k`` evenly across the documents (top_k=8, 3 documents -> 3/3/2), sorted by score, so
    one document with higher similarities cannot crowd the others out of a comparison question.
    ``top_score`` stays the best candidate similarity over every document.
    """
    settings = settings or get_settings()
    if document_ids:
        await _check_document_scope(session, user.org_id, document_ids)
    try:
        q = await embedder.embed_query(query)
    except EmbeddingError as exc:
        raise RetrievalUnavailableError(str(exc)) from None
    prefer = settings.prefer_consolidated
    fetch = max(top_k, settings.retrieval_candidates) if prefer else top_k
    distinct = list(dict.fromkeys(document_ids or []))
    if settings.balance_multi_document_retrieval and len(distinct) > 1:
        per_document: list[list[RetrievedChunk]] = []
        scores: list[float] = []
        for document_id in distinct:
            found = await search_chunks(
                session,
                org_id=user.org_id,
                query_vector=q.vectors[0],
                top_k=fetch,
                document_ids=[document_id],
            )
            scores.extend(c.score for c in found)
            per_document.append(
                prefer_statement_scope(found, query=query, top_k=top_k) if prefer else found
            )
        return Retrieval(
            chunks=interleave_by_rank(per_document, top_k), top_score=max(scores, default=None)
        )
    candidates = await search_chunks(
        session,
        org_id=user.org_id,
        query_vector=q.vectors[0],
        top_k=fetch,
        document_ids=document_ids or None,
    )
    top_score = max((c.score for c in candidates), default=None)
    if prefer:
        chunks = prefer_statement_scope(candidates, query=query, top_k=top_k)
    else:
        chunks = candidates
    return Retrieval(chunks=chunks, top_score=top_score)


async def retrieve(
    session: AsyncSession,
    *,
    user: User,
    query: str,
    top_k: int,
    document_ids: list[uuid.UUID] | None,
    embedder: Embedder,
    settings: Settings | None = None,
) -> list[RetrievedChunk]:
    """Final (reordered) chunks of ``retrieve_ranked``; ``POST /search`` and the Eval use this."""
    result = await retrieve_ranked(
        session,
        user=user,
        query=query,
        top_k=top_k,
        document_ids=document_ids,
        embedder=embedder,
        settings=settings,
    )
    return result.chunks


@dataclass
class _Attempt:
    """One LLM response (or none: nothing retrieved) turned into text, footnotes and reasons."""

    response: LLMResponse | None
    text: str
    footnotes: list[dict[str, Any]]
    ungrounded: list[UngroundedNumber]
    reasons: list[str]


def _evaluate(
    question: str, retrieval: Retrieval, response: LLMResponse | None, settings: Settings
) -> _Attempt:
    retrieved = retrieval.chunks
    ungrounded: list[UngroundedNumber] = []
    if response is None:
        text, footnotes = NO_EVIDENCE_TEXT, []
    elif response.stop_reason == "refusal":
        text, footnotes = REFUSAL_TEXT, []
    else:
        built = build_footnotes(response.content, to_source_refs(retrieved))
        method = response.citation_method
        text = built.text or NO_EVIDENCE_TEXT
        footnotes = [{**f, "citation_method": method} for f in built.footnotes]
        if settings.number_grounding_check != "off":
            ungrounded = ungrounded_numbers(
                response.content,
                [c.text for c in retrieved],
                question,
                sources=[source_id(c.document_id, c.chunk_id) for c in retrieved],
            )
            if ungrounded:
                log.warning(
                    "answer.ungrounded_numbers",
                    model=response.model,
                    count=len(ungrounded),
                    numbers=[u.text for u in ungrounded[:MAX_REPORTED]],
                )
    reasons = review_reasons(
        retrieved=retrieved,
        llm_response=response,
        footnote_count=len(footnotes),
        settings=settings,
        top_score=retrieval.top_score,
        ungrounded_numbers=ungrounded,
    )
    return _Attempt(response, text, footnotes, ungrounded, reasons)


def _client_model(llm: LLMClient) -> str:
    return str(getattr(llm, "model_id", None) or getattr(llm, "model", "unknown"))


async def _record_failure(session: AsyncSession, user: User, exc: LLMError) -> None:
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


async def ask_question(
    session: AsyncSession,
    *,
    user: User,
    question: str,
    document_ids: list[uuid.UUID] | None,
    embedder: Embedder,
    llm: LLMClient,
    settings: Settings,
    fallback_llm: LLMClient | None = None,
) -> AskResult:
    started = time.perf_counter()
    retrieval = await retrieve_ranked(
        session,
        user=user,
        query=question,
        top_k=settings.top_k,
        document_ids=document_ids,
        embedder=embedder,
        settings=settings,
    )
    retrieved = retrieval.chunks

    responses: list[LLMResponse] = []
    models: list[str] = []
    escalation_reasons: list[str] = []
    escalation_error: int | None = None
    if not retrieved:
        final = _evaluate(question, retrieval, None, settings)
    else:
        messages = build_messages(question, retrieved)
        primary: LLMResponse | None = None
        try:
            primary = await llm.answer(system=SYSTEM_PROMPT, messages=messages)
        except LLMError as exc:
            if fallback_llm is None or exc.status_code not in ESCALATION_STATUS_CODES:
                await _record_failure(session, user, exc)
                raise
            models.append(_client_model(llm))
            escalation_reasons = [f"llm_error_{exc.status_code}"]
        if primary is not None:
            responses.append(primary)
            models.append(primary.model)
            final = _evaluate(question, retrieval, primary, settings)
            if fallback_llm is not None and escalation_triggers(primary, final.reasons):
                escalation_reasons = list(final.reasons)
        if fallback_llm is not None and escalation_reasons:
            log.info(
                "answer.escalating",
                primary=models[0],
                fallback=_client_model(fallback_llm),
                reasons=escalation_reasons,
            )
            try:
                second = await fallback_llm.answer(system=SYSTEM_PROMPT, messages=messages)
            except LLMError as exc:
                if primary is None:
                    await _record_failure(session, user, exc)
                    raise
                # The primary answer is still usable (it goes to review): keep it.
                models.append(_client_model(fallback_llm))
                escalation_error = exc.status_code
                log.warning(
                    "answer.escalation_failed",
                    fallback=_client_model(fallback_llm),
                    status_code=exc.status_code,
                )
            else:
                responses.append(second)
                models.append(second.model)
                final = _evaluate(question, retrieval, second, settings)
    escalated = bool(escalation_reasons)
    status = "pending_review" if final.reasons else "published"
    reported_numbers = [u.text for u in final.ungrounded[:MAX_REPORTED]]

    q = Question(id=uuid.uuid4(), org_id=user.org_id, asked_by=user.id, text=question)
    session.add(q)
    await session.flush()
    answer = Answer(
        id=uuid.uuid4(),
        question_id=q.id,
        org_id=user.org_id,
        text=final.text,
        footnotes=final.footnotes,
        status=status,
        review_reason=",".join(final.reasons) or None,
    )
    session.add(answer)
    await session.flush()

    usage_out: dict[str, Any] = {
        "model": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": "0.0000",
    }
    costs: list[Decimal | None] = []
    for response in responses:
        cost = compute_cost(response.model, response.usage, settings.llm_prices)
        costs.append(cost)
        session.add(
            LlmCall(
                id=uuid.uuid4(),
                org_id=user.org_id,
                answer_id=answer.id,
                model=response.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_input_tokens=response.usage.cache_read_input_tokens,
                cache_creation_input_tokens=response.usage.cache_creation_input_tokens,
                cost_usd=cost,
                latency_ms=response.latency_ms,
                stop_reason=response.stop_reason,
                dropped_source_refs=response.dropped_source_refs,
            )
        )
    if final.response is not None:
        # Model of the answer that was kept; tokens and cost cover every call (cascade: both).
        total_cost = (
            sum((c for c in costs if c is not None), Decimal(0))
            if all(c is not None for c in costs)
            else None
        )
        usage_out = {
            "model": final.response.model,
            "input_tokens": sum(r.usage.input_tokens for r in responses),
            "output_tokens": sum(r.usage.output_tokens for r in responses),
            "cache_read_input_tokens": sum(r.usage.cache_read_input_tokens for r in responses),
            "cache_creation_input_tokens": sum(
                r.usage.cache_creation_input_tokens for r in responses
            ),
            "cost_usd": f"{total_cost:.4f}" if total_cost is not None else None,
        }

    asked_metadata: dict[str, Any] = {
        "question_chars": len(question),
        "document_ids": [str(d) for d in document_ids or []],
        "retrieved": len(retrieved),
        "top_score": round(retrieval.top_score or 0.0, 4),
        "escalated": escalated,
        "escalation_reasons": escalation_reasons,
        "ungrounded_numbers": reported_numbers,
    }
    if escalated:
        asked_metadata["models"] = models
    if escalation_error is not None:
        asked_metadata["escalation_error"] = escalation_error
    record(
        session,
        action=Actions.QUESTION_ASKED,
        org_id=user.org_id,
        actor_user_id=user.id,
        target_type="question",
        target_id=q.id,
        metadata=asked_metadata,
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
            "footnote_count": len(final.footnotes),
            "llm_called": bool(responses),
            "model": usage_out["model"],
            "stop_reason": final.response.stop_reason if final.response else None,
            "cost_usd": usage_out["cost_usd"],
        },
    )
    await session.commit()
    await session.refresh(answer)
    return AskResult(
        answer=answer,
        footnotes=final.footnotes,
        usage=usage_out,
        latency_ms=int((time.perf_counter() - started) * 1000),
        retrieved=len(retrieved),
        escalated=escalated,
        models=models,
        escalation_reasons=escalation_reasons,
        ungrounded_numbers=final.ungrounded,
    )
