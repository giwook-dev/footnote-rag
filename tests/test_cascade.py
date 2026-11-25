"""Local cascade (fallback_llm), number grounding in the review gate, think levels, warm-up,
fallback settings and /llm/status. Ollama is simulated with httpx.MockTransport."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from footnote_rag.answering.llm import NO_EVIDENCE_TEXT, LLMError, LLMResponse, LLMUsage
from footnote_rag.answering.local_llm import OllamaClient
from footnote_rag.answering.service import ask_question, escalation_triggers, review_reasons
from footnote_rag.config import Settings
from footnote_rag.db.models import AuditLog, LlmCall, User
from footnote_rag.providers import make_fallback_llm, make_llm
from footnote_rag.retrieval.search import RetrievedChunk

QUESTION = "회사는 리스부채를 어떻게 측정하나요?"


# ---- scripted LLM doubles ----


def _results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for m in messages for b in m["content"] if b.get("type") == "search_result"]


def cite_first(messages) -> list[dict[str, Any]]:
    """One sentence copied from the first search result (numbers grounded), citing it."""
    first = _results(messages)[0]
    sentence = first["content"][0]["text"].splitlines()[0][:60]
    return [
        {
            "type": "text",
            "text": sentence,
            "citations": [
                {
                    "type": "search_result_location",
                    "source": first["source"],
                    "cited_text": sentence,
                    "search_result_index": 0,
                }
            ],
        }
    ]


def no_evidence(messages) -> list[dict[str, Any]]:
    return [{"type": "text", "text": NO_EVIDENCE_TEXT}]


def converted_amount(messages) -> list[dict[str, Any]]:
    """A cited sentence whose amount is not in the chunk (a model-made conversion)."""
    blocks = cite_first(messages)
    blocks[0]["text"] = "리스부채는 약 9.87조 원입니다."
    return blocks


class ScriptedLLM:
    provider = "local"

    def __init__(
        self, model: str, script, *, flags: list[str] | None = None, abstained: bool = False
    ) -> None:
        self.model = model
        self.model_id = f"ollama:{model}"
        self.script = script
        self.flags = flags or []
        self.abstained = abstained
        self.calls = 0

    async def answer(self, *, system: str, messages: list[dict[str, Any]]) -> LLMResponse:
        self.calls += 1
        if isinstance(self.script, LLMError):
            raise self.script
        return LLMResponse(
            model=self.model_id,
            stop_reason="end_turn",
            content=self.script(messages),
            usage=LLMUsage(input_tokens=100, output_tokens=10),
            latency_ms=3,
            citation_method="model_reference",
            review_flags=list(self.flags),
            abstained=self.abstained,
        )


async def _ask(sessionmaker, org, embedder, settings, llm, fallback=None):
    async with sessionmaker() as s:
        user = await s.get(User, org.admin_id)
        return await ask_question(
            s,
            user=user,
            question=QUESTION,
            document_ids=None,
            embedder=embedder,
            llm=llm,
            settings=settings,
            fallback_llm=fallback,
        )


async def _calls(sessionmaker, answer_id) -> list[LlmCall]:
    async with sessionmaker() as s:
        rows = await s.scalars(select(LlmCall).where(LlmCall.answer_id == answer_id))
        return list(rows)


async def _asked(sessionmaker, question_id) -> dict:
    async with sessionmaker() as s:
        row = await s.scalar(
            select(AuditLog).where(
                AuditLog.action == "question.asked", AuditLog.target_id == str(question_id)
            )
        )
        return row.metadata_


async def _failed(sessionmaker, org_id) -> AuditLog | None:
    async with sessionmaker() as s:
        return await s.scalar(
            select(AuditLog).where(AuditLog.org_id == org_id, AuditLog.action == "question.failed")
        )


@pytest.fixture
async def org(make_org, ingest):
    o = await make_org()
    await ingest(o)
    return o


# ---- review_reasons ----


def test_review_reasons_adds_ungrounded_number_only_in_review_mode(settings):
    chunk = RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title="t",
        document_meta={},
        source="dart",
        source_ref=None,
        section_path="p",
        chunk_index=0,
        text="x",
        score=0.9,
    )
    ok = SimpleNamespace(stop_reason="end_turn")
    kwargs = {"retrieved": [chunk], "llm_response": ok, "footnote_count": 1}
    assert settings.number_grounding_check == "review"
    assert review_reasons(**kwargs, settings=settings) == []
    assert review_reasons(**kwargs, settings=settings, ungrounded_numbers=["1.2"]) == [
        "ungrounded_number"
    ]
    off = settings.model_copy(update={"number_grounding_check": "off"})
    assert review_reasons(**kwargs, settings=off, ungrounded_numbers=["1.2"]) == []


# ---- number grounding through ask_question ----


async def test_ungrounded_number_goes_to_review_and_is_audited(
    org, sessionmaker, fake_embedder, settings
):
    result = await _ask(
        sessionmaker, org, fake_embedder, settings, ScriptedLLM("m", converted_amount)
    )
    assert result.answer.status == "pending_review"
    assert result.answer.review_reason == "ungrounded_number"
    assert [u.text for u in result.ungrounded_numbers] == ["9.87"]
    meta = await _asked(sessionmaker, result.answer.question_id)
    assert meta["ungrounded_numbers"] == ["9.87"]
    assert meta["escalated"] is False and meta["escalation_reasons"] == []


async def test_number_check_off_publishes(org, sessionmaker, fake_embedder, settings):
    off = settings.model_copy(update={"number_grounding_check": "off"})
    result = await _ask(sessionmaker, org, fake_embedder, off, ScriptedLLM("m", converted_amount))
    assert result.answer.status == "published" and result.answer.review_reason is None
    assert result.ungrounded_numbers == []


async def test_grounded_numbers_publish(org, sessionmaker, fake_embedder, settings):
    result = await _ask(sessionmaker, org, fake_embedder, settings, ScriptedLLM("m", cite_first))
    assert result.answer.status == "published", result.answer.review_reason
    assert result.ungrounded_numbers == []


# ---- cascade ----


async def test_no_citations_escalates_and_fallback_publishes(
    org, sessionmaker, fake_embedder, settings
):
    primary, fallback = ScriptedLLM("small", no_evidence), ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert (primary.calls, fallback.calls) == (1, 1)
    assert result.answer.status == "published" and result.footnotes
    assert result.escalated is True
    assert result.models == ["ollama:small", "ollama:big"]
    assert result.escalation_reasons == ["no_citations"]
    calls = await _calls(sessionmaker, result.answer.id)
    assert sorted(c.model for c in calls) == ["ollama:big", "ollama:small"]
    assert result.usage["model"] == "ollama:big"
    assert result.usage["input_tokens"] == 200  # both calls
    meta = await _asked(sessionmaker, result.answer.question_id)
    assert meta["escalated"] is True
    assert meta["escalation_reasons"] == ["no_citations"]
    assert meta["models"] == ["ollama:small", "ollama:big"]


@pytest.mark.parametrize(
    ("script", "flags", "reason"),
    [(converted_amount, [], "ungrounded_number"), (cite_first, ["truncated"], "truncated")],
    ids=["ungrounded-number", "truncated"],
)
async def test_other_model_reasons_escalate(
    org, sessionmaker, fake_embedder, settings, script, flags, reason
):
    primary, fallback = ScriptedLLM("small", script, flags=flags), ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert fallback.calls == 1 and result.escalated
    assert reason in result.escalation_reasons
    assert result.answer.status == "published"


async def test_explicit_abstention_alone_does_not_escalate(
    org, sessionmaker, fake_embedder, settings
):
    """unans-01~04 on 2026-11-25: the small model said "insufficient" (the right answer) and the
    cascade still called the big model. ``no_citations`` of an explicit abstention stays put."""
    primary = ScriptedLLM("small", no_evidence, abstained=True)
    fallback = ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert (primary.calls, fallback.calls) == (1, 0)
    assert result.escalated is False and result.escalation_reasons == []
    assert result.models == ["ollama:small"]
    # the review gate is unchanged: the abstention still goes to review
    assert result.answer.status == "pending_review"
    assert result.answer.review_reason == "no_citations"
    calls = await _calls(sessionmaker, result.answer.id)
    assert [c.model for c in calls] == ["ollama:small"]
    meta = await _asked(sessionmaker, result.answer.question_id)
    assert meta["escalated"] is False and meta["escalation_reasons"] == []


@pytest.mark.parametrize(
    ("script", "flags", "reason"),
    [(no_evidence, ["truncated"], "truncated"), (converted_amount, [], "ungrounded_number")],
    ids=["truncated", "ungrounded-number"],
)
async def test_abstention_with_another_reason_still_escalates(
    org, sessionmaker, fake_embedder, settings, script, flags, reason
):
    primary = ScriptedLLM("small", script, flags=flags, abstained=True)
    fallback = ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert fallback.calls == 1 and result.escalated is True
    assert reason in result.escalation_reasons
    assert result.answer.status == "published"
    assert len(await _calls(sessionmaker, result.answer.id)) == 2


async def test_dropped_source_numbers_without_abstention_still_escalate(
    org, sessionmaker, fake_embedder, settings
):
    """The model answered but every source number was out of range (``abstained=False``)."""

    def out_of_range(messages):
        return [{"type": "text", "text": NO_EVIDENCE_TEXT}]

    primary = ScriptedLLM("small", out_of_range, abstained=False)
    fallback = ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert fallback.calls == 1 and result.escalation_reasons == ["no_citations"]
    assert result.answer.status == "published"


def test_escalation_triggers():
    def response(abstained: bool) -> LLMResponse:
        return LLMResponse("m", "end_turn", abstained=abstained)

    assert escalation_triggers(response(False), ["no_citations"]) == ["no_citations"]
    assert escalation_triggers(response(True), ["no_citations"]) == []
    assert escalation_triggers(response(True), ["truncated", "no_citations"]) == ["truncated"]
    assert escalation_triggers(response(True), ["low_retrieval_score", "no_citations"]) == []
    assert escalation_triggers(response(False), ["low_retrieval_score"]) == []
    # Claude / Fake responses keep the default
    assert LLMResponse("m", "end_turn").abstained is False


async def test_refusal_escalates(org, sessionmaker, fake_embedder, settings):
    class Refusing(ScriptedLLM):
        async def answer(self, *, system, messages):
            self.calls += 1
            return LLMResponse(self.model_id, "refusal", [], LLMUsage(), 1)

    primary, fallback = Refusing("small", None), ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert result.escalation_reasons == ["refusal"] and fallback.calls == 1
    assert result.answer.status == "published"


async def test_low_retrieval_score_alone_does_not_escalate(
    org, sessionmaker, fake_embedder, settings
):
    strict = settings.model_copy(update={"min_retrieval_score": 0.999})
    primary, fallback = ScriptedLLM("small", cite_first), ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, strict, primary, fallback)
    assert result.answer.review_reason == "low_retrieval_score"
    assert fallback.calls == 0 and result.escalated is False
    assert result.models == ["ollama:small"]
    assert len(await _calls(sessionmaker, result.answer.id)) == 1


async def test_no_retrieval_results_does_not_call_any_model(
    make_org, sessionmaker, fake_embedder, settings
):
    empty = await make_org()  # nothing imported
    primary, fallback = ScriptedLLM("small", cite_first), ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, empty, fake_embedder, settings, primary, fallback)
    assert "no_retrieval_results" in result.answer.review_reason
    assert (primary.calls, fallback.calls) == (0, 0) and not result.escalated


async def test_review_mode_always_does_not_escalate(org, sessionmaker, fake_embedder, settings):
    always = settings.model_copy(update={"review_mode": "always"})
    primary, fallback = ScriptedLLM("small", cite_first), ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, always, primary, fallback)
    assert result.answer.review_reason == "review_mode_always" and fallback.calls == 0


async def test_fallback_that_also_fails_stays_pending_review(
    org, sessionmaker, fake_embedder, settings
):
    primary, fallback = ScriptedLLM("small", no_evidence), ScriptedLLM("big", no_evidence)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert (primary.calls, fallback.calls) == (1, 1)  # escalated once, not again
    assert result.answer.status == "pending_review"
    assert result.answer.review_reason == "no_citations"
    assert result.escalated is True
    assert len(await _calls(sessionmaker, result.answer.id)) == 2


async def test_primary_502_escalates(org, sessionmaker, fake_embedder, settings):
    primary = ScriptedLLM("small", LLMError(502, "로컬 LLM 응답이 JSON 형식에 맞지 않습니다"))
    fallback = ScriptedLLM("big", cite_first)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert result.answer.status == "published"
    assert result.escalated and result.escalation_reasons == ["llm_error_502"]
    assert result.models == ["ollama:small", "ollama:big"]
    calls = await _calls(sessionmaker, result.answer.id)
    assert [c.model for c in calls] == ["ollama:big"]  # the failed call has no usage row
    assert await _failed(sessionmaker, org.org_id) is None


@pytest.mark.parametrize("code", [503, 504])
async def test_primary_503_504_do_not_escalate(org, sessionmaker, fake_embedder, settings, code):
    primary = ScriptedLLM("small", LLMError(code, "down"))
    fallback = ScriptedLLM("big", cite_first)
    with pytest.raises(LLMError) as exc:
        await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert exc.value.status_code == code and fallback.calls == 0
    failed = await _failed(sessionmaker, org.org_id)
    assert failed.metadata_ == {"status_code": code, "reason": "down"}


async def test_primary_502_then_fallback_error_fails_with_the_fallback_error(
    org, sessionmaker, fake_embedder, settings
):
    primary = ScriptedLLM("small", LLMError(502, "bad json"))
    fallback = ScriptedLLM("big", LLMError(504, "timeout"))
    with pytest.raises(LLMError) as exc:
        await _ask(sessionmaker, org, fake_embedder, settings, primary, fallback)
    assert exc.value.status_code == 504
    failed = await _failed(sessionmaker, org.org_id)
    assert failed.metadata_ == {"status_code": 504, "reason": "timeout"}


async def test_without_fallback_nothing_changes(org, sessionmaker, fake_embedder, settings):
    primary = ScriptedLLM("small", no_evidence)
    result = await _ask(sessionmaker, org, fake_embedder, settings, primary, None)
    assert (
        result.answer.status == "pending_review" and result.answer.review_reason == "no_citations"
    )
    assert result.escalated is False and result.models == ["ollama:small"]
    assert len(await _calls(sessionmaker, result.answer.id)) == 1
    with pytest.raises(LLMError):
        await _ask(sessionmaker, org, fake_embedder, settings, ScriptedLLM("s", LLMError(502, "x")))


async def test_api_uses_app_state_fallback(client, app, org):
    app.state.llm = ScriptedLLM("small", no_evidence)
    app.state.fallback_llm = ScriptedLLM("big", cite_first)
    resp = await client.post("/questions", json={"question": QUESTION}, headers=org.headers())
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "published"
    assert set(body) == {
        "answer_id",
        "question_id",
        "status",
        "review_reason",
        "text",
        "footnotes",
        "usage",
        "latency_ms",
        "created_at",
    }
    assert app.state.fallback_llm.calls == 1


# ---- Ollama: think levels and warm-up ----


def _ollama(handler, **kwargs) -> OllamaClient:
    return OllamaClient(
        base_url="http://ollama.test:11434", transport=httpx.MockTransport(handler), **kwargs
    )


def test_think_level_is_sent_as_is():
    llm = _ollama(lambda r: httpx.Response(200), model="gpt-oss:20b", think="low")
    assert llm.request_body([{"role": "user", "content": "x"}])["think"] == "low"
    s = Settings(_env_file=None, llm_provider="local", local_llm_think="high")
    assert make_llm(s).request_body([])["think"] == "high"


async def test_think_level_reaches_the_http_body():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"insufficient": true, "sentences": []}'},
                "done_reason": "stop",
            },
        )

    llm = _ollama(handler, model="gpt-oss:20b", think="medium")
    await llm.answer(system="", messages=[{"role": "user", "content": []}])
    assert seen[0]["think"] == "medium"


async def test_warm_up_posts_empty_generate():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"done": True})

    assert await _ollama(handler, model="qwen3:14b").warm_up() is True
    assert seen[0].method == "POST"
    assert seen[0].url == "http://ollama.test:11434/api/generate"
    assert json.loads(seen[0].content) == {"model": "qwen3:14b", "prompt": "", "stream": False}


@pytest.mark.parametrize(
    "outcome",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(404), RuntimeError()],
    ids=["refused", "timeout", "missing", "unexpected"],
)
async def test_warm_up_failure_never_raises(outcome):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert await _ollama(handler).warm_up() is False


def _chat_reply(content: str, done_reason: str = "stop"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"content": content}, "done_reason": done_reason}
        )

    return handler


ONE_SOURCE = [
    {
        "role": "user",
        "content": [
            {
                "type": "search_result",
                "source": "doc:1#chunk:1",
                "title": "t",
                "content": [{"type": "text", "text": "리스부채 1,234"}],
            },
            {"type": "text", "text": "리스부채는?"},
        ],
    }
]


@pytest.mark.parametrize(
    ("content", "done_reason", "abstained"),
    [
        ('{"insufficient": true, "sentences": []}', "stop", True),
        (
            '{"insufficient": false, "sentences": [{"text": "1,234", "sources": [1]}]}',
            "stop",
            False,
        ),
        # out-of-range source numbers only: not an abstention (the cascade still escalates)
        (
            '{"insufficient": false, "sentences": [{"text": "1,234", "sources": [9]}]}',
            "stop",
            False,
        ),
        # cut off by num_predict: insufficient is read from the salvaged text
        ('{"insufficient": true, "sentences": [', "length", True),
    ],
    ids=["insufficient", "answered", "dropped-refs", "truncated-insufficient"],
)
async def test_ollama_insufficient_sets_abstained(content, done_reason, abstained):
    llm = _ollama(_chat_reply(content, done_reason))
    response = await llm.answer(system="", messages=ONE_SOURCE)
    assert response.abstained is abstained


async def test_ollama_loaded_models_reads_api_ps():
    seen: list[httpx.Request] = []
    ps = {
        "models": [
            {"name": "qwen3:14b", "model": "qwen3:14b", "size": 1000, "size_vram": 780},
            {"model": "qwen3.5:9b", "size": 500, "size_vram": 500},
            {"size": 1},  # no name: skipped
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ps)

    assert await _ollama(handler).loaded_models() == [
        {"name": "qwen3:14b", "size": 1000, "size_vram": 780},
        {"name": "qwen3.5:9b", "size": 500, "size_vram": 500},
    ]
    assert seen[0].method == "GET" and seen[0].url == "http://ollama.test:11434/api/ps"


async def test_ollama_unload_posts_keep_alive_zero():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"done": True, "done_reason": "unload"})

    assert await _ollama(handler, model="qwen3.5:9b").unload("qwen3:14b") is True
    assert seen[0].method == "POST"
    assert seen[0].url == "http://ollama.test:11434/api/generate"
    assert json.loads(seen[0].content) == {"model": "qwen3:14b", "keep_alive": 0}


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        RuntimeError(),
    ],
    ids=["refused", "timeout", "server-error", "bad-json", "unexpected"],
)
async def test_ollama_loaded_models_and_unload_never_raise(outcome):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    llm = _ollama(handler)
    assert await llm.loaded_models() == []
    expected_unload = isinstance(outcome, httpx.Response) and outcome.status_code < 400
    assert await llm.unload("qwen3:14b") is expected_unload


# ---- settings and factories ----


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [("false", False), ("true", True), ("low", "low"), ("medium", "medium"), ("high", "high")],
)
def test_local_llm_think_env_parsing(monkeypatch, raw, parsed):
    monkeypatch.setenv("LOCAL_LLM_THINK", raw)
    assert Settings(_env_file=None).local_llm_think == parsed


def test_invalid_think_and_new_defaults(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_THINK", "max")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.delenv("LOCAL_LLM_THINK")
    s = Settings(_env_file=None)
    assert s.local_llm_fallback_model == "" and s.local_llm_fallback_think is None
    assert s.number_grounding_check == "review"
    monkeypatch.setenv("NUMBER_GROUNDING_CHECK", "off")
    monkeypatch.setenv("LOCAL_LLM_FALLBACK_THINK", "false")
    s = Settings(_env_file=None)
    assert s.number_grounding_check == "off" and s.local_llm_fallback_think is False


def test_make_fallback_llm():
    assert make_fallback_llm(Settings(_env_file=None, llm_provider="local")) is None
    assert (
        make_fallback_llm(
            Settings(_env_file=None, llm_provider="fake", local_llm_fallback_model="qwen3:14b")
        )
        is None
    )
    s = Settings(
        _env_file=None,
        llm_provider="local",
        local_llm_think="low",
        local_llm_fallback_model="qwen3:14b",
        local_llm_num_ctx=8192,
    )
    fallback = make_fallback_llm(s)
    assert isinstance(fallback, OllamaClient)
    assert (fallback.model, fallback.think, fallback.num_ctx) == ("qwen3:14b", "low", 8192)
    explicit = s.model_copy(update={"local_llm_fallback_think": False})
    assert make_fallback_llm(explicit).think is False
    assert make_llm(s).model == "qwen3:14b"  # primary keeps LOCAL_LLM_MODEL


# ---- GET /llm/status ----


async def test_llm_status_reports_the_fallback_model(client, app, make_org):
    org = await make_org()
    body = (await client.get("/llm/status", headers=org.headers())).json()
    assert "fallback" not in body  # cascade off: unchanged response
    tags = {"models": [{"name": "qwen3:14b"}]}
    app.state.fallback_llm = _ollama(lambda r: httpx.Response(200, json=tags), model="qwen3:14b")
    body = (await client.get("/llm/status", headers=org.headers())).json()
    assert body["provider"] == "fake"
    assert body["fallback"] == {
        "model": "qwen3:14b",
        "connected": True,
        "model_available": True,
        "detail": None,
    }
    app.state.fallback_llm = _ollama(lambda r: httpx.Response(200, json=tags), model="gpt-oss:20b")
    fb = (await client.get("/llm/status", headers=org.headers())).json()["fallback"]
    assert fb["connected"] is True and fb["model_available"] is False
    assert fb["detail"] == "모델 gpt-oss:20b이 없습니다: ollama pull gpt-oss:20b"
