"""에이전트 엔드포인트(``/agent``, 인증된 모든 역할, 조직 격리).

역할: ``POST /agent/runs``는 실행을 백그라운드 태스크로 시작하고 진행을 SSE(``text/event-stream``)로 흘려보낸다
(``run`` → ``step``… → ``answer`` 또는 ``error`` → ``done``). 클라이언트가 끊기면 태스크를 취소해 실행을
``failed``(``client_disconnected``)로 남긴다. 빈 질문(422)과 ``LLM_PROVIDER=real``(501)은 스트림을 열기 전에 일반 JSON
오류로 답한다. ``GET /agent/runs``(최근 목록), ``GET /agent/runs/{id}``(단계·근거 포함, 다른 조직 404),
``GET /agent/config``(모델·상한, 화면 표시용)도 둔다.
주요 요소: ``post_run``, ``list_runs``, ``get_run``, ``agent_config``, ``sse_event``, ``AgentRunIn``, ``AgentRunOut``.
호출 관계: ``agent.runner.run_agent``를 호출한다. 웹 화면(``/ui``)의 "에이전트" 탭이 쓴다.
학습 문서: docs/study/12-agent.md, docs/adr/0013-tool-using-agent.md
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.agent.runner import (
    REAL_LLM_UNSUPPORTED,
    REASON_LABELS,
    model_id_of,
    run_agent,
    supports_agent,
)
from footnote_rag.agent.tools import TOOLS
from footnote_rag.answering.router import get_embedder, get_llm
from footnote_rag.auth.deps import ANY_MEMBER, require_role
from footnote_rag.comparison.router import get_dart_client
from footnote_rag.config import get_settings
from footnote_rag.dart.client import DartClient
from footnote_rag.db.models import AgentRun, AgentStep, User
from footnote_rag.db.session import get_session
from footnote_rag.observability import get_logger
from footnote_rag.retrieval.embedder import Embedder

log = get_logger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

HEARTBEAT_SECONDS = 1.0
KEEPALIVE_EVERY = 15  # heartbeats between SSE comments
RUN_NOT_FOUND = "Agent run not found"
# Strong references to running agent tasks (a task is dropped once it finishes).
_RUNNING: set[asyncio.Task[Any]] = set()


class AgentRunIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000, description="질문(한국어)")


class AgentRunSummary(BaseModel):
    id: uuid.UUID
    question: str
    status: str
    model: str
    step_count: int
    tool_call_count: int
    review_reasons: list[str]
    latency_ms: int | None = None
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class AgentStepOut(BaseModel):
    idx: int
    kind: str
    tool_name: str | None
    args: dict[str, Any]
    ok: bool
    result_text: str
    sources: list[dict[str, Any]]
    latency_ms: int
    created_at: datetime


class AgentRunOut(AgentRunSummary):
    answer: str | None = None
    citations: list[dict[str, Any]]
    sources: list[dict[str, Any]] = Field(description="이 실행의 모든 도구 결과 근거(단계 순서)")
    dropped_citations: list[str] = []
    ungrounded_numbers: list[str] = []
    steps: list[AgentStepOut]


class AgentConfigOut(BaseModel):
    supported: bool
    provider: str
    model: str
    max_steps: int
    max_calls_per_step: int
    timeout_seconds: float
    tools: list[dict[str, str]]
    reason_labels: dict[str, str]


def sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _summary(run: AgentRun) -> AgentRunSummary:
    return AgentRunSummary(
        id=run.id,
        question=run.question,
        status=run.status,
        model=run.model,
        step_count=run.step_count,
        tool_call_count=run.tool_call_count,
        review_reasons=list(run.review_reasons or []),
        latency_ms=run.latency_ms,
        error=run.error,
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


@router.get("/config", response_model=AgentConfigOut)
async def agent_config(
    _user: User = Depends(require_role(*ANY_MEMBER)),
    llm: Any = Depends(get_llm),
) -> AgentConfigOut:
    """Model and limits shown on the agent tab (no model call)."""
    settings = get_settings()
    return AgentConfigOut(
        supported=supports_agent(llm, settings),
        provider=settings.llm_mode,
        model=model_id_of(llm),
        max_steps=settings.agent_max_steps,
        max_calls_per_step=settings.agent_max_calls_per_step,
        timeout_seconds=settings.agent_timeout_seconds,
        tools=[
            {"name": t.name, "label": t.label, "description": t.description} for t in TOOLS.values()
        ],
        reason_labels=REASON_LABELS,
    )


@router.post(
    "/runs",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE: run, step…, answer, done"},
        422: {"description": "Empty question"},
        501: {"description": "LLM_PROVIDER=real: the agent runs on the local LLM only"},
    },
)
async def post_run(
    body: AgentRunIn,
    request: Request,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
    llm: Any = Depends(get_llm),
    embedder: Embedder = Depends(get_embedder),
    dart_client: DartClient = Depends(get_dart_client),
) -> StreamingResponse:
    """Run the tool-using agent on one question and stream its progress as server-sent events."""
    question = body.question.strip()
    if len(question) < 2:
        raise HTTPException(status_code=422, detail="질문을 입력하세요.")
    settings = get_settings()
    if not supports_agent(llm, settings):
        raise HTTPException(status_code=501, detail=REAL_LLM_UNSUPPORTED)
    org_id, user_id = user.org_id, user.id
    await session.close()  # the run opens its own sessions; free this connection now

    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def emit(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    task = asyncio.create_task(
        run_agent(
            question,
            org_id=org_id,
            user_id=user_id,
            settings=settings,
            llm=llm,
            embedder=embedder,
            dart_client=dart_client,
            emit=emit,
        )
    )
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    task.add_done_callback(lambda _t: queue.put_nowait(None))

    async def stream() -> AsyncIterator[str]:
        idle = 0
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        log.info("agent.client_disconnected")
                        return
                    idle += 1
                    if idle % KEEPALIVE_EVERY == 0:
                        yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                yield sse_event(*item)
            outcome = None if task.cancelled() or task.exception() else task.result()
            if outcome is None:
                yield sse_event("error", {"message": "에이전트 실행 중 오류가 났습니다."})
            yield sse_event(
                "done",
                {
                    "run_id": str(outcome.run_id) if outcome else None,
                    "status": outcome.status if outcome else "failed",
                },
            )
        finally:
            if not task.done():
                task.cancel()  # client gone: the run records client_disconnected itself

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs", response_model=list[AgentRunSummary])
async def list_runs(
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> list[AgentRunSummary]:
    """Recent agent runs of your organization (newest first, at most 20)."""
    runs = await session.scalars(
        select(AgentRun)
        .where(AgentRun.org_id == user.org_id)
        .order_by(AgentRun.created_at.desc())
        .limit(20)
    )
    return [_summary(r) for r in runs]


@router.get(
    "/runs/{run_id}", response_model=AgentRunOut, responses={404: {"description": "Not found"}}
)
async def get_run(
    run_id: uuid.UUID,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> AgentRunOut:
    """One run with every step (what the model was given) and every source."""
    run = await session.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.org_id == user.org_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    steps = list(
        await session.scalars(
            select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.idx)
        )
    )
    final = next((s for s in reversed(steps) if s.kind == "model"), None)
    verification = (final.args or {}).get("verification") or {} if final else {}
    return AgentRunOut(
        **_summary(run).model_dump(),
        answer=run.answer,
        citations=list(run.citations or []),
        sources=[src for s in steps for src in s.sources or []],
        dropped_citations=list(verification.get("dropped_citations") or []),
        ungrounded_numbers=list(verification.get("ungrounded_numbers") or []),
        steps=[
            AgentStepOut(
                idx=s.idx,
                kind=s.kind,
                tool_name=s.tool_name,
                args=s.args or {},
                ok=s.ok,
                result_text=s.result_text,
                sources=list(s.sources or []),
                latency_ms=s.latency_ms,
                created_at=s.created_at,
            )
            for s in steps
        ],
    )
