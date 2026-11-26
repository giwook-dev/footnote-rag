"""에이전트 실행 루프: 모델은 도구를 고르고, 코드가 실행·제한·기록·검증한다.

역할: 질문 하나로 ``agent_runs`` 행을 만들고, 로컬 모델(``chat_tools``)에 도구 목록을 주며 단계마다 도구 호출을
실행해 결과를 ``tool`` 메시지로 돌려준다. 모든 모델 호출·도구 호출을 ``agent_steps``(즉시 커밋)와
``llm_calls``(비용 0)에 남기고 ``emit``으로 실시간 이벤트를 보낸다. 끝나면 최종 답의 근거 번호와 숫자를 코드로
검증해(``verify_answer``) ``completed``/``needs_review``/``failed``를 정하고 감사 로그 두 건을 남긴다.
주요 요소: ``run_agent``, ``verify_answer``, ``Verification``, ``AgentOutcome``, ``supports_agent``,
``SYSTEM_PROMPT``, ``FINAL_ANSWER_NOTE``, ``REASON_LABELS``.
호출 관계: ``agent.router``(SSE)와 ``evals.agent``가 호출한다. ``agent.tools.run_tool``, ``LLMClient.chat_tools``
(``OllamaClient``·``FakeLLM``), ``audit.service.record``를 부른다.
학습 문서: docs/study/12-agent.md, docs/adr/0013-tool-using-agent.md

Loop (docs/adr/0013-tool-using-agent.md):

1. messages = [system prompt, question]; every turn sends the six tool specs.
2. a reply with ``tool_calls``: the first ``AGENT_MAX_CALLS_PER_STEP`` are run in order and their
   results appended as ``tool`` messages (the rest are dropped and counted).
3. a reply without ``tool_calls`` is the final answer.
4. after ``AGENT_MAX_STEPS`` tool turns, or when the same tool is called with the same arguments
   three times in a row (that third call is not run), the model is called once more *without*
   tools and told to answer from what it has (review reason ``step_limit`` / ``repeated_call``).
5. the whole run is bounded by ``AGENT_TIMEOUT_SECONDS`` (``failed``, error ``timeout``); a
   cancelled run (client gone) is ``failed`` with error ``client_disconnected``.

Verification of the final answer (``verify_answer``): every ``[id]`` mark must name a source of this
run (``N1:L12`` / ``C1:C5`` also need the line to be one that was shown); others are removed and
counted. Every number of the answer (``extract_numbers``; numbers of the question excluded) must
occur in some successful tool result. Reasons: ``no_citations`` (no valid mark), ``ungrounded_number``,
``tool_errors`` (more than half of the tool calls failed), plus ``step_limit``/``repeated_call``.
Any reason -> ``needs_review``, none -> ``completed``.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from footnote_rag.agent.tools import (
    AgentContext,
    ToolResult,
    build_context,
    run_tool,
    tool_specs,
)
from footnote_rag.answering.llm import LLMError, ToolCall, ToolChatResult
from footnote_rag.answering.number_check import MAX_REPORTED, contains_number, extract_numbers
from footnote_rag.audit.service import Actions, record
from footnote_rag.config import Settings
from footnote_rag.dart.client import DartClient
from footnote_rag.db.models import AgentRun, AgentStep, LlmCall, User
from footnote_rag.observability import get_logger
from footnote_rag.retrieval.embedder import Embedder

log = get_logger(__name__)

SYSTEM_PROMPT = """너는 DART 공시(사업보고서·감사보고서)를 읽고 질문에 답하는 분석 보조자다. 다음 규칙을 반드시 지켜라.
1. 답하기 전에 도구로 근거를 모은다. 문서는 list_documents가 주는 별칭(D1, D2 …)으로만 지정한다.
2. 비율·증감·합계·단위 환산 같은 계산은 직접 하지 말고 반드시 calculate 도구로 한다. calculate의 values에는 앞선 도구 결과에 나온 숫자를 그대로 넣는다.
3. 답의 모든 문장 끝에 근거 번호를 단다. 예: [S3], [F2], [K1], [N1:L12], [C1:C5]. 도구 결과에 없는 번호는 쓰지 않는다.
4. 숫자는 도구 결과에 있는 값만 그대로 쓴다. 어림하거나 단위를 바꾸지 않는다.
5. 세법·회계기준 판단이나 의견은 내지 않는다. 공시에 적힌 사실만 말한다.
6. 근거를 찾지 못하면 추측하지 말고 "제공된 문서에서 근거를 찾지 못했습니다."라고 답한다.
7. 답은 한국어로 짧게(6문장 이내) 쓴다."""
FINAL_ANSWER_NOTE = (
    "도구 호출을 더 할 수 없습니다. 도구를 부르지 말고 지금까지 모은 근거로만 답하세요. "
    "근거 번호를 달고, 도구 결과에 있는 숫자만 쓰는 규칙은 그대로입니다."
)
EMPTY_ANSWER_TEXT = "모델이 답을 내지 않았습니다."
REAL_LLM_UNSUPPORTED = "에이전트는 현재 로컬 LLM에서만 지원합니다"
REPEAT_LIMIT = 3
SUMMARY_CHARS = 300
ARGS_SUMMARY_CHARS = 160

# Review / failure reasons and their Korean labels (web, Eval report).
REASON_LABELS: dict[str, str] = {
    "no_citations": "근거 표시 없음",
    "ungrounded_number": "도구 결과에 없는 숫자",
    "tool_errors": "도구 오류가 절반 넘음",
    "step_limit": "단계 상한 도달",
    "repeated_call": "같은 호출 반복",
    "timeout": "시간 초과",
    "client_disconnected": "중지됨(연결 끊김)",
}

# "[S3]", "[N1:L12]", "[F1, F2]"; ids are a capital letter and digits, optional ":X#" line.
_MARK_ID = r"[A-Z]\d+(?::[A-Z]\d+)?"
CITATION_RE = re.compile(rf"\[\s*({_MARK_ID}(?:\s*,\s*{_MARK_ID})*)\s*\]")

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentTimeout(Exception):
    pass


def supports_agent(llm: Any, settings: Settings) -> bool:
    """Local and fake models only: never Claude (cost), whatever client is configured."""
    return settings.llm_mode != "real" and callable(getattr(llm, "chat_tools", None))


def model_id_of(llm: Any) -> str:
    return str(getattr(llm, "model_id", None) or getattr(llm, "model", "unknown"))


# ---------------------------------------------------------------- verification


@dataclass
class Verification:
    text: str  # the answer with invalid marks removed
    cited: list[str]  # valid marks in order ("F1", "N1:L3")
    dropped: list[str]  # removed marks
    ungrounded_numbers: list[str]
    reasons: list[str]


def _valid_mark(mark: str, sources: dict[str, dict[str, Any]]) -> bool:
    base, _sep, line = mark.partition(":")
    source = sources.get(base)
    if source is None:
        return False
    if not line:
        return True
    return line in (source.get("refs") or [])


def verify_answer(
    answer: str,
    *,
    question: str,
    sources: dict[str, dict[str, Any]],
    haystack: str,
    tool_calls: int = 0,
    tool_errors: int = 0,
    loop_reasons: list[str] | None = None,
) -> Verification:
    """Check a final answer against the run's sources and tool-result numbers (pure)."""
    cited: list[str] = []
    dropped: list[str] = []

    def replace(match: re.Match[str]) -> str:
        marks = [m.strip() for m in match.group(1).split(",")]
        keep = []
        for mark in marks:
            if _valid_mark(mark, sources):
                keep.append(mark)
                if mark not in cited:
                    cited.append(mark)
            else:
                dropped.append(mark)
        return "".join(f"[{m}]" for m in keep)

    text = CITATION_RE.sub(replace, answer)
    text = re.sub(r"[ \t]+([.,])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()
    asked = {value for _raw, value in extract_numbers(question)}
    ungrounded = [
        raw
        for raw, value in extract_numbers(CITATION_RE.sub(" ", text))
        if value not in asked and not contains_number(haystack, value)
    ]
    reasons = list(dict.fromkeys(loop_reasons or []))
    if not cited:
        reasons.append("no_citations")
    if ungrounded:
        reasons.append("ungrounded_number")
    if tool_calls and tool_errors * 2 > tool_calls:
        reasons.append("tool_errors")
    return Verification(text, cited, dropped, list(dict.fromkeys(ungrounded)), reasons)


# ---------------------------------------------------------------- run


@dataclass
class AgentOutcome:
    run_id: uuid.UUID
    status: str
    answer: str | None
    citations: list[dict[str, Any]]
    review_reasons: list[str]
    step_count: int
    tool_call_count: int
    tool_error_count: int
    tool_counts: dict[str, int]
    latency_ms: int
    model: str
    error: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    dropped_citations: list[str] = field(default_factory=list)
    ungrounded_numbers: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)  # step event payloads


def _args_summary(name: str, args: Any) -> str:
    if isinstance(args, dict):
        inner = ", ".join(
            f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items() if v is not None
        )
    else:
        inner = json.dumps(args, ensure_ascii=False)
    text = f"{name}({inner})"
    return text if len(text) <= ARGS_SUMMARY_CHARS else text[: ARGS_SUMMARY_CHARS - 1] + "…"


def _summary(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= SUMMARY_CHARS else flat[: SUMMARY_CHARS - 1] + "…"


def _jsonable(args: Any) -> Any:
    try:
        json.dumps(args)
        return args
    except (TypeError, ValueError):
        return str(args)


def _signature(call: ToolCall) -> str:
    try:
        return call.name + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return call.name + str(call.arguments)


class _Run:
    """State of one run; ``run_agent`` is the entry point."""

    def __init__(
        self,
        *,
        question: str,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        settings: Settings,
        llm: Any,
        embedder: Embedder,
        dart_client: DartClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        emit: Emit | None,
    ) -> None:
        self.question = question
        self.org_id = org_id
        self.user_id = user_id
        self.settings = settings
        self.llm = llm
        self.embedder = embedder
        self.dart_client = dart_client
        self.sm = sessionmaker
        self._emit = emit
        self.run_id = uuid.uuid4()
        self.model = model_id_of(llm)
        self.started = time.monotonic()
        self.deadline = self.started + settings.agent_timeout_seconds
        self.idx = 0
        self.model_calls = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.tool_counts: dict[str, int] = {}
        self.steps: list[dict[str, Any]] = []
        self.ctx: AgentContext | None = None
        self.last_model_step: uuid.UUID | None = None

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            await self._emit(event, data)
        except Exception:  # noqa: BLE001 - a broken listener never breaks the run
            log.warning("agent.emit_failed", event=event)

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise AgentTimeout()
        return left

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    # ---- persistence

    async def start(self) -> None:
        async with self.sm() as session:
            session.add(
                AgentRun(
                    id=self.run_id,
                    org_id=self.org_id,
                    user_id=self.user_id,
                    question=self.question,
                    status="running",
                    model=self.model,
                )
            )
            record(
                session,
                action=Actions.AGENT_RUN_STARTED,
                org_id=self.org_id,
                actor_user_id=self.user_id,
                target_type="agent_run",
                target_id=self.run_id,
                metadata={"question_chars": len(self.question), "model": self.model},
            )
            await session.commit()
        await self.emit(
            "run",
            {
                "run_id": str(self.run_id),
                "model": self.model,
                "max_steps": self.settings.agent_max_steps,
                "question": self.question,
            },
        )

    async def save_step(
        self,
        *,
        kind: str,
        tool_name: str | None,
        args: dict[str, Any],
        ok: bool,
        result_text: str,
        sources: list[dict[str, Any]],
        latency_ms: int,
        event: dict[str, Any],
        reply: ToolChatResult | None = None,
    ) -> None:
        self.idx += 1
        step_id = uuid.uuid4()
        if kind == "model":
            self.last_model_step = step_id
        async with self.sm() as session:
            session.add(
                AgentStep(
                    id=step_id,
                    run_id=self.run_id,
                    idx=self.idx,
                    kind=kind,
                    tool_name=tool_name,
                    args=args,
                    ok=ok,
                    result_text=result_text,
                    sources=sources,
                    latency_ms=latency_ms,
                )
            )
            if reply is not None:
                session.add(
                    LlmCall(
                        id=uuid.uuid4(),
                        org_id=self.org_id,
                        answer_id=None,
                        agent_run_id=self.run_id,
                        model=reply.model_id,
                        input_tokens=reply.usage.input_tokens,
                        output_tokens=reply.usage.output_tokens,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=0,
                        # Agent runs only on local (ollama:) or fake models: no per-token cost.
                        cost_usd=Decimal("0"),
                        latency_ms=reply.latency_ms,
                        stop_reason=reply.done_reason,
                        dropped_source_refs=0,
                    )
                )
            await session.commit()
        payload = {"idx": self.idx, "kind": kind, "tool_name": tool_name, **event}
        payload["latency_ms"] = latency_ms
        self.steps.append(payload)
        await self.emit("step", payload)

    async def finish(
        self,
        *,
        status: str,
        answer: str | None,
        reasons: list[str],
        error: str | None,
        verification: Verification | None,
    ) -> AgentOutcome:
        sources = dict(self.ctx.sources) if self.ctx else {}
        cited_bases = list(
            dict.fromkeys(m.partition(":")[0] for m in (verification.cited if verification else []))
        )
        citations = [sources[b] for b in cited_bases if b in sources]
        latency = self.elapsed_ms()
        async with self.sm() as session:
            run = await session.get(AgentRun, self.run_id)
            if run is not None:
                run.status = status
                run.answer = answer
                run.citations = citations
                run.review_reasons = reasons
                run.step_count = self.model_calls
                run.tool_call_count = self.tool_calls
                run.latency_ms = latency
                run.error = error
                run.finished_at = datetime.now(UTC)
            if verification is not None and self.last_model_step is not None:
                step = await session.get(AgentStep, self.last_model_step)
                if step is not None:
                    # Kept on the final model step: GET /agent/runs/{id} reads it back.
                    step.args = {
                        **(step.args or {}),
                        "verification": {
                            "cited": verification.cited,
                            "dropped_citations": verification.dropped,
                            "ungrounded_numbers": verification.ungrounded_numbers,
                            "reasons": verification.reasons,
                        },
                    }
            record(
                session,
                action=Actions.AGENT_RUN_FINISHED,
                org_id=self.org_id,
                actor_user_id=self.user_id,
                target_type="agent_run",
                target_id=self.run_id,
                metadata={
                    "status": status,
                    "steps": self.model_calls,
                    "tool_calls": self.tool_calls,
                    "tool_errors": self.tool_errors,
                    "tool_counts": self.tool_counts,
                    "review_reasons": reasons,
                    "error": error,
                    "model": self.model,
                    "latency_ms": latency,
                    "dropped_citations": len(verification.dropped) if verification else 0,
                    "ungrounded_numbers": (
                        verification.ungrounded_numbers[:MAX_REPORTED] if verification else []
                    ),
                },
            )
            await session.commit()
        outcome = AgentOutcome(
            run_id=self.run_id,
            status=status,
            answer=answer,
            citations=citations,
            review_reasons=reasons,
            step_count=self.model_calls,
            tool_call_count=self.tool_calls,
            tool_error_count=self.tool_errors,
            tool_counts=dict(self.tool_counts),
            latency_ms=latency,
            model=self.model,
            error=error,
            sources=list(sources.values()),
            dropped_citations=list(verification.dropped) if verification else [],
            ungrounded_numbers=list(verification.ungrounded_numbers) if verification else [],
            steps=list(self.steps),
        )
        log.info(
            "agent.run_finished",
            run_id=str(self.run_id),
            status=status,
            steps=self.model_calls,
            tool_calls=self.tool_calls,
            reasons=reasons,
            error=error,
            latency_ms=latency,
        )
        if status == "failed":
            await self.emit(
                "error",
                {"run_id": str(self.run_id), "error": error, "message": _failure_message(error)},
            )
        else:
            await self.emit("answer", answer_payload(outcome))
        return outcome

    # ---- loop

    async def call_model(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ToolChatResult:
        try:
            return await asyncio.wait_for(
                self.llm.chat_tools(messages, tools), timeout=self.remaining()
            )
        except TimeoutError:
            raise AgentTimeout() from None

    async def call_tool(self, call: ToolCall) -> tuple[ToolResult, int]:
        assert self.ctx is not None
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                run_tool(self.ctx, call.name, call.arguments), timeout=self.remaining()
            )
        except TimeoutError:
            raise AgentTimeout() from None
        return result, int((time.perf_counter() - started) * 1000)

    async def loop(self) -> tuple[str, list[str]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self.question},
        ]
        specs = tool_specs()
        per_step = max(1, self.settings.agent_max_calls_per_step)
        force_reason: str | None = None
        last_signature: str | None = None
        repeats = 0
        while True:
            forced = force_reason is not None
            if forced:
                messages.append({"role": "user", "content": FINAL_ANSWER_NOTE})
            reply = await self.call_model(messages, [] if forced else specs)
            self.model_calls += 1
            calls = [] if forced else list(reply.tool_calls)
            executed = calls[:per_step]
            final = not executed
            await self.save_step(
                kind="model",
                tool_name=None,
                args={
                    "tool_calls": [
                        {"name": c.name, "arguments": _jsonable(c.arguments)} for c in executed
                    ],
                    "dropped_calls": len(calls) - len(executed),
                    "ignored_calls": len(reply.tool_calls) if forced else 0,
                    "final": final,
                    "forced_final": forced,
                    "done_reason": reply.done_reason,
                },
                ok=True,
                result_text=reply.content or "",
                sources=[],
                latency_ms=reply.latency_ms,
                event={
                    "label": "최종 답" if final else "모델 응답",
                    "args_summary": "",
                    "ok": True,
                    "summary": _summary(reply.content or "")
                    if final
                    else "도구 호출: " + ", ".join(c.name for c in executed),
                    "source_ids": [],
                    "final": final,
                    "forced_final": forced,
                    "dropped_calls": len(calls) - len(executed),
                },
                reply=reply,
            )
            if final:
                return reply.content or "", [force_reason] if force_reason else []
            run_calls: list[ToolCall] = []
            tool_messages: list[dict[str, Any]] = []
            for call in executed:
                signature = _signature(call)
                repeats = repeats + 1 if signature == last_signature else 1
                last_signature = signature
                if repeats >= REPEAT_LIMIT:
                    force_reason = "repeated_call"
                    break
                result, latency = await self.call_tool(call)
                self.tool_calls += 1
                self.tool_counts[call.name] = self.tool_counts.get(call.name, 0) + 1
                if not result.ok:
                    self.tool_errors += 1
                run_calls.append(call)
                tool_messages.append(
                    {"role": "tool", "content": result.model_text, "tool_name": call.name}
                )
                await self.save_step(
                    kind="tool",
                    tool_name=call.name,
                    args=_jsonable(call.arguments)
                    if isinstance(call.arguments, dict)
                    else {"raw": _jsonable(call.arguments)},
                    ok=result.ok,
                    result_text=result.model_text,
                    sources=result.sources,
                    latency_ms=latency,
                    event={
                        "args_summary": _args_summary(call.name, call.arguments),
                        "ok": result.ok,
                        "summary": _summary(result.error if not result.ok else result.text),
                        "source_ids": [s["id"] for s in result.sources],
                        "error": result.error,
                        "result_text": result.model_text,
                        "sources": result.sources,
                    },
                )
            if run_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.content or "",
                        # Ollama wants an object here; a malformed argument string is not echoed.
                        "tool_calls": [
                            {
                                "function": {
                                    "name": c.name,
                                    "arguments": c.arguments
                                    if isinstance(c.arguments, dict)
                                    else {},
                                }
                            }
                            for c in run_calls
                        ],
                    }
                )
                messages.extend(tool_messages)
            if force_reason is None and self.model_calls >= self.settings.agent_max_steps:
                force_reason = "step_limit"

    async def execute(self) -> AgentOutcome:
        await self.start()
        try:
            async with self.sm() as tool_session:
                user = await tool_session.get(User, self.user_id)
                if user is None or user.org_id != self.org_id:
                    raise RuntimeError("agent user not found")
                self.ctx = await build_context(
                    tool_session,
                    user=user,
                    settings=self.settings,
                    embedder=self.embedder,
                    dart_client=self.dart_client,
                )
                raw_answer, loop_reasons = await self.loop()
        except asyncio.CancelledError:
            await asyncio.shield(self._fail("client_disconnected"))
            raise
        except AgentTimeout:
            return await self._fail("timeout")
        except LLMError as exc:
            log.warning("agent.llm_failed", status_code=exc.status_code)
            return await self._fail(f"llm_error_{exc.status_code}", exc.public_message)
        except Exception as exc:  # noqa: BLE001 - the run row must not stay "running"
            log.exception("agent.run_crashed", error=type(exc).__name__)
            return await self._fail("internal_error")
        ctx = self.ctx
        verification = verify_answer(
            raw_answer or EMPTY_ANSWER_TEXT,
            question=self.question,
            sources=ctx.sources,
            haystack=ctx.haystack(),
            tool_calls=self.tool_calls,
            tool_errors=self.tool_errors,
            loop_reasons=loop_reasons,
        )
        status = "needs_review" if verification.reasons else "completed"
        return await self.finish(
            status=status,
            answer=verification.text,
            reasons=verification.reasons,
            error=None,
            verification=verification,
        )

    async def _fail(self, reason: str, message: str | None = None) -> AgentOutcome:
        error = reason if message is None else f"{reason}: {message}"
        reasons = [reason] if reason in REASON_LABELS else []
        return await self.finish(
            status="failed", answer=None, reasons=reasons, error=error, verification=None
        )


def _failure_message(error: str | None) -> str:
    reason = (error or "").partition(":")[0]
    if reason == "timeout":
        return "실행 시간 상한을 넘어 멈췄습니다."
    if reason == "client_disconnected":
        return "요청이 중지되어 실행을 멈췄습니다."
    if reason.startswith("llm_error_"):
        return (error or "").partition(":")[2].strip() or "로컬 LLM 호출이 실패했습니다."
    return "에이전트 실행 중 오류가 났습니다."


def answer_payload(outcome: AgentOutcome) -> dict[str, Any]:
    """The ``answer`` SSE event (also the core of ``GET /agent/runs/{id}``)."""
    return {
        "run_id": str(outcome.run_id),
        "status": outcome.status,
        "answer": outcome.answer,
        "citations": outcome.citations,
        "sources": outcome.sources,
        "review_reasons": outcome.review_reasons,
        "dropped_citations": outcome.dropped_citations,
        "ungrounded_numbers": outcome.ungrounded_numbers,
        "step_count": outcome.step_count,
        "tool_call_count": outcome.tool_call_count,
        "latency_ms": outcome.latency_ms,
        "model": outcome.model,
    }


async def run_agent(
    question: str,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    settings: Settings,
    llm: Any,
    embedder: Embedder,
    dart_client: DartClient,
    emit: Emit | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> AgentOutcome:
    """Run the agent for one question and return the stored outcome. Events (``run``, ``step``…,
    ``answer`` or ``error``) go to ``emit`` as they happen. The caller checks ``supports_agent``."""
    from footnote_rag.db.session import get_sessionmaker

    run = _Run(
        question=question,
        org_id=org_id,
        user_id=user_id,
        settings=settings,
        llm=llm,
        embedder=embedder,
        dart_client=dart_client,
        sessionmaker=sessionmaker or get_sessionmaker(),
        emit=emit,
    )
    return await run.execute()
