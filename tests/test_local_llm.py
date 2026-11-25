"""Local LLM (Ollama) provider: request shape, JSON validation, footnotes, review gate, errors,
usage rows, /llm/status, provider selection and eval report naming.

Ollama is simulated with ``httpx.MockTransport``; nothing leaves the process.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from footnote_rag.answering.footnotes import select_cited_text, split_sentences
from footnote_rag.answering.llm import NO_EVIDENCE_TEXT, ClaudeClient, FakeLLM, LLMError, LLMUsage
from footnote_rag.answering.local_llm import (
    OllamaClient,
    parse_local_answer,
    salvage_local_answer,
)
from footnote_rag.answering.prompt import (
    LOCAL_ANSWER_SCHEMA,
    LOCAL_RETRY_NOTE,
    LOCAL_SYSTEM_PROMPT,
    build_messages,
)
from footnote_rag.config import DEFAULT_LLM_PRICES, Settings
from footnote_rag.db.models import AuditLog, Chunk, LlmCall
from footnote_rag.evals.run import EvalSummary, render_report, report_filename
from footnote_rag.providers import MissingKeyError, make_llm
from footnote_rag.retrieval.search import RetrievedChunk
from footnote_rag.usage.pricing import compute_cost

QUESTION = "회사는 리스부채를 어떻게 측정하나요?"
MODEL = "qwen3:14b"


# ---- helpers ----


def chat_response(payload, *, done_reason="stop", prompt_tokens=1200, output_tokens=80) -> dict:
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {
        "model": MODEL,
        "created_at": "2026-11-24T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": prompt_tokens,
        "eval_count": output_tokens,
    }


class FakeOllama:
    """MockTransport handler: replays ``responses`` in order (the last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


def ollama(*responses) -> tuple[OllamaClient, FakeOllama]:
    handler = FakeOllama(*responses)
    client = OllamaClient(
        base_url="http://ollama.test:11434", model=MODEL, transport=httpx.MockTransport(handler)
    )
    return client, handler


def answer(*sentences: tuple[str, list[int]], insufficient: bool = False) -> dict:
    return {
        "insufficient": insufficient,
        "sentences": [{"text": t, "sources": s} for t, s in sentences],
    }


def _chunk(title: str, path: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title=title,
        document_meta={},
        source="dart",
        source_ref=None,
        section_path=path,
        chunk_index=0,
        text=text,
        score=0.8,
    )


async def _ask(client, org, role="admin"):
    return await client.post("/questions", json={"question": QUESTION}, headers=org.headers(role))


async def _hits(client, org) -> list[dict]:
    resp = await client.post("/search", json={"query": QUESTION}, headers=org.headers("member"))
    assert resp.status_code == 200
    return resp.json()


async def _llm_call(sessionmaker, answer_id: str) -> LlmCall:
    async with sessionmaker() as s:
        return await s.scalar(select(LlmCall).where(LlmCall.answer_id == uuid.UUID(answer_id)))


# ---- request shape (no DB use) ----


async def test_request_body_has_stream_false_schema_num_ctx_and_numbered_sources():
    llm, server = ollama(chat_response(answer(("리스부채는 현재가치로 측정합니다.", [1]))))
    chunks = [
        _chunk("가람전자 사업보고서", "주석 > 12. 리스", "리스부채는 현재가치로 측정합니다."),
        _chunk("나래화학 사업보고서", "주석 > 20. 우발부채", "소송이 진행 중입니다."),
    ]
    result = await llm.answer(system="ignored", messages=build_messages("질문?", chunks))

    assert len(server.requests) == 1
    assert server.requests[0].url == "http://ollama.test:11434/api/chat"
    body = server.bodies[0]
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert body["format"] == LOCAL_ANSWER_SCHEMA
    assert body["format"]["additionalProperties"] is False
    assert body["options"] == {"num_ctx": 16384, "num_predict": 2048, "temperature": 0.0}
    assert body["think"] is False
    system, user = body["messages"]
    assert system == {"role": "system", "content": LOCAL_SYSTEM_PROMPT}
    assert (
        "[1] 가람전자 사업보고서 · 주석 > 12. 리스\n리스부채는 현재가치로 측정합니다."
        in (user["content"])
    )
    assert "[2] 나래화학 사업보고서 · 주석 > 20. 우발부채\n소송이 진행 중입니다." in user["content"]
    assert user["content"].rstrip().endswith("질문: 질문?")

    assert result.model == "ollama:qwen3:14b"
    assert result.stop_reason == "end_turn"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1200, 80)
    assert result.citation_method == "model_reference"
    citation = result.content[0]["citations"][0]
    assert citation["search_result_index"] == 0
    assert citation["source"] == f"doc:{chunks[0].document_id}#chunk:{chunks[0].chunk_id}"


async def test_settings_flow_into_request_options():
    s = Settings(
        _env_file=None,
        llm_provider="local",
        local_llm_model="llama3.1:8b",
        local_llm_num_ctx=8192,
        local_llm_max_tokens=512,
        local_llm_temperature=0.2,
        local_llm_think=True,
    )
    llm = make_llm(s)
    assert isinstance(llm, OllamaClient)
    body = llm.request_body([{"role": "user", "content": "x"}])
    assert body["model"] == "llama3.1:8b" and body["think"] is True
    assert body["options"] == {"num_ctx": 8192, "num_predict": 512, "temperature": 0.2}


# ---- provider selection (AC-1, AC-2) ----


def test_local_provider_needs_no_key_and_other_modes_are_unchanged():
    local = Settings(_env_file=None, llm_provider="local")
    assert local.llm_mode == "local" and local.anthropic_api_key is None
    llm = make_llm(local)
    assert isinstance(llm, OllamaClient)
    assert llm.base_url == "http://host.docker.internal:11434" and llm.model == "qwen3:14b"
    assert (llm.num_ctx, llm.max_tokens, llm.timeout) == (16384, 2048, 180.0)
    assert (llm.think, llm.temperature) == (False, 0.0)

    assert Settings(_env_file=None, llm_provider="auto").llm_mode == "fake"
    keyed = Settings(_env_file=None, llm_provider="auto", anthropic_api_key=SecretStr("sk-x"))
    assert keyed.llm_mode == "real"  # auto never picks local
    assert isinstance(make_llm(Settings(_env_file=None, llm_provider="fake")), FakeLLM)
    assert isinstance(make_llm(keyed), ClaudeClient)
    with pytest.raises(MissingKeyError):
        make_llm(Settings(_env_file=None, llm_provider="real"))


# ---- JSON parsing ----


def test_parse_is_strict_and_salvage_keeps_complete_sentences():
    ok = parse_local_answer(
        '{"insufficient": false, "sentences": [{"text": "A.", "sources": [1]}]}'
    )
    assert ok is not None and ok.sentences[0].sources == [1]
    for bad in (
        "not json",
        "[]",
        '{"insufficient": "no", "sentences": []}',
        '{"insufficient": false}',
        '{"insufficient": false, "sentences": [{"text": "A.", "sources": ["1"]}]}',
        '{"insufficient": false, "sentences": [{"text": "A.", "sources": [true]}]}',
        '{"insufficient": false, "sentences": [], "extra": 1}',
    ):
        assert parse_local_answer(bad) is None, bad

    cut = '{"insufficient": false, "sentences": [{"text": "A.", "sources": [1]}, {"text": "B'
    salvaged = salvage_local_answer(cut)
    assert [s.text for s in salvaged.sentences] == ["A."]
    assert salvage_local_answer('{"insufficient": tr').sentences == []


# ---- end to end through POST /questions ----


async def test_local_answer_gets_footnotes_with_model_reference(
    client, app, make_org, ingest, sessionmaker
):
    org = await make_org()
    doc_id = await ingest(org)
    hits = await _hits(client, org)
    assert len(hits) >= 2
    llm, server = ollama(
        chat_response(
            answer(
                ("리스부채는 현재가치로 측정합니다[1].", [1]),
                ("할인율은 증분차입이자율입니다.", [2]),
                ("단기리스는 인식 면제를 적용합니다.", [1, 1]),
            )
        )
    )
    app.state.llm = llm
    resp = await _ask(client, org)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "published", body
    # "[1]" written by the model inside the sentence is stripped; markers come from sources.
    assert body["text"] == (
        "리스부채는 현재가치로 측정합니다.[1] 할인율은 증분차입이자율입니다.[2] "
        "단기리스는 인식 면제를 적용합니다.[1]"
    )
    assert [f["n"] for f in body["footnotes"]] == [1, 2]  # same chunk reuses its number
    for f, hit in zip(body["footnotes"], hits[:2], strict=True):
        assert f["chunk_id"] == hit["chunk_id"]
        assert f["document_id"] == hit["document_id"] == doc_id
        assert f["section_path"] == hit["section_path"]
        assert f["document_title"] == hit["document_title"]
        assert f["cited_text"] and f["cited_text"] in hit["text"]
        assert len(f["cited_text"]) <= 300
        assert f["citation_method"] == "model_reference"
    # k in the prompt equals the number of retrieved chunks
    prompt = server.bodies[0]["messages"][1]["content"]
    assert f"[{len(hits)}] " in prompt and f"[{len(hits) + 1}] " not in prompt

    assert body["usage"]["model"] == "ollama:qwen3:14b"
    assert body["usage"]["cost_usd"] == "0.0000"
    assert (body["usage"]["input_tokens"], body["usage"]["output_tokens"]) == (1200, 80)
    call = await _llm_call(sessionmaker, body["answer_id"])
    assert call.model == "ollama:qwen3:14b"
    assert (call.input_tokens, call.output_tokens) == (1200, 80)
    assert call.cost_usd == Decimal("0")
    assert call.latency_ms >= 0 and call.stop_reason == "end_turn"
    assert call.dropped_source_refs == 0

    got = await client.get(f"/answers/{body['answer_id']}", headers=org.headers("member"))
    assert got.json()["footnotes"] == body["footnotes"]


async def test_cited_text_comes_from_the_cited_chunk(client, app, make_org, ingest, sessionmaker):
    org = await make_org()
    await ingest(org)
    hits = await _hits(client, org)
    target = split_sentences(hits[0]["text"])[0]
    app.state.llm, _ = ollama(chat_response(answer((target, [1]))))
    body = (await _ask(client, org)).json()
    async with sessionmaker() as s:
        chunk = await s.get(Chunk, uuid.UUID(body["footnotes"][0]["chunk_id"]))
    assert body["footnotes"][0]["cited_text"] == select_cited_text(chunk.text, target)


async def test_out_of_range_numbers_are_dropped_and_counted(
    client, app, make_org, ingest, sessionmaker
):
    org = await make_org()
    await ingest(org)
    app.state.llm, _ = ollama(
        chat_response(answer(("근거 있는 문장.", [1, 99, 0]), ("범위 밖만 단 문장.", [-1])))
    )
    body = (await _ask(client, org)).json()
    assert body["status"] == "published"
    assert body["text"] == "근거 있는 문장.[1] 범위 밖만 단 문장."
    assert len(body["footnotes"]) == 1
    call = await _llm_call(sessionmaker, body["answer_id"])
    assert call.dropped_source_refs == 3


async def test_all_numbers_out_of_range_is_no_citations_review(
    client, app, make_org, ingest, sessionmaker
):
    org = await make_org()
    await ingest(org)
    app.state.llm, _ = ollama(chat_response(answer(("문장.", [50]), ("또 문장.", [51]))))
    body = (await _ask(client, org)).json()
    assert body["status"] == "pending_review"
    assert body["review_reason"] == "no_citations"
    assert body["text"] == NO_EVIDENCE_TEXT and body["footnotes"] == []
    assert (await _llm_call(sessionmaker, body["answer_id"])).dropped_source_refs == 2


async def test_insufficient_is_no_evidence_and_pending_review(client, app, make_org, ingest):
    org = await make_org()
    await ingest(org)
    app.state.llm, _ = ollama(chat_response(answer(insufficient=True)))
    body = (await _ask(client, org)).json()
    assert body["status"] == "pending_review" and "no_citations" in body["review_reason"]
    assert body["text"] == NO_EVIDENCE_TEXT and body["footnotes"] == []


async def test_length_done_reason_is_truncated_review(client, app, make_org, ingest, sessionmaker):
    org = await make_org()
    await ingest(org)
    app.state.llm, server = ollama(
        chat_response(answer(("리스부채는 현재가치로 측정합니다.", [1])), done_reason="length")
    )
    body = (await _ask(client, org)).json()
    assert body["status"] == "pending_review"
    assert body["review_reason"] == "truncated"
    assert body["footnotes"] and body["footnotes"][0]["citation_method"] == "model_reference"
    call = await _llm_call(sessionmaker, body["answer_id"])
    assert call.stop_reason == "max_tokens"
    assert len(server.requests) == 1


async def test_length_with_cut_json_is_salvaged_without_retry(client, app, make_org, ingest):
    org = await make_org()
    await ingest(org)
    cut = '{"insufficient": false, "sentences": [{"text": "완성된 문장.", "sources": [1]}, {"te'
    app.state.llm, server = ollama(chat_response(cut, done_reason="length"))
    resp = await _ask(client, org)
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "완성된 문장.[1]"
    assert body["status"] == "pending_review" and body["review_reason"] == "truncated"
    assert len(server.requests) == 1  # no retry: it would hit the same limit


async def test_invalid_json_is_retried_once_then_succeeds(client, app, make_org, ingest):
    org = await make_org()
    await ingest(org)
    app.state.llm, server = ollama(
        chat_response("{not json", prompt_tokens=100, output_tokens=5),
        chat_response(answer(("두 번째 시도 문장.", [1])), prompt_tokens=110, output_tokens=20),
    )
    body = (await _ask(client, org)).json()
    assert body["status"] == "published" and body["text"] == "두 번째 시도 문장.[1]"
    assert len(server.requests) == 2
    retry_messages = server.bodies[1]["messages"]
    assert retry_messages[-1] == {"role": "user", "content": LOCAL_RETRY_NOTE}
    assert retry_messages[:2] == server.bodies[0]["messages"]
    # usage covers both attempts
    assert (body["usage"]["input_tokens"], body["usage"]["output_tokens"]) == (210, 25)


async def test_invalid_json_twice_is_502_and_audited(client, app, make_org, ingest, sessionmaker):
    org = await make_org()
    await ingest(org)
    app.state.llm, server = ollama(
        chat_response('{"insufficient": false, "sentences": [{"text": 1, "sources": []}]}'),
        chat_response("still not json"),
    )
    resp = await _ask(client, org, "member")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "로컬 LLM 응답이 JSON 형식에 맞지 않습니다"
    assert len(server.requests) == 2
    async with sessionmaker() as s:
        failed = await s.scalar(
            select(AuditLog).where(
                AuditLog.org_id == org.org_id, AuditLog.action == "question.failed"
            )
        )
    assert failed.metadata_["status_code"] == 502


@pytest.mark.parametrize(
    ("response", "status", "detail"),
    [
        (
            httpx.ConnectError("Connection refused"),
            503,
            "로컬 LLM 서버(Ollama)에 연결할 수 없습니다",
        ),
        (
            httpx.ConnectError("[Errno -2] Name or service not known"),
            503,
            "로컬 LLM 서버(Ollama)에 연결할 수 없습니다",
        ),
        (httpx.ReadTimeout("timed out"), 504, "로컬 LLM 서버(Ollama) 응답 시간이 초과되었습니다"),
        (
            httpx.Response(404, json={"error": "model 'qwen3:14b' not found"}),
            503,
            "모델 qwen3:14b이 없습니다: ollama pull qwen3:14b",
        ),
    ],
    ids=["refused", "dns", "timeout", "model-missing"],
)
async def test_ollama_errors_map_to_http_status_and_are_audited(
    client, app, make_org, ingest, sessionmaker, response, status, detail
):
    org = await make_org()
    await ingest(org)
    app.state.llm, _ = ollama(response)
    resp = await _ask(client, org, "member")
    assert resp.status_code == status
    assert resp.json()["detail"] == detail
    async with sessionmaker() as s:
        failed = await s.scalar(
            select(AuditLog).where(
                AuditLog.org_id == org.org_id, AuditLog.action == "question.failed"
            )
        )
        calls = (await s.scalars(select(LlmCall).where(LlmCall.org_id == org.org_id))).all()
    assert failed.metadata_ == {"status_code": status, "reason": detail}
    assert calls == []


async def test_server_error_is_503_and_bad_request_is_502():
    for code, status in ((500, 503), (400, 502)):
        llm, _ = ollama(httpx.Response(code, json={"error": "boom"}))
        with pytest.raises(LLMError) as exc:
            await llm.answer(system="", messages=build_messages("q", [_chunk("t", "p", "x")]))
        assert exc.value.status_code == status
        assert "boom" not in exc.value.public_message


# ---- Claude path keeps its shape, plus citation_method ----


async def test_claude_path_footnotes_get_api_citation(client, make_org, ingest):
    org = await make_org()
    await ingest(org)
    body = (await _ask(client, org, "member")).json()
    assert body["status"] == "published" and body["footnotes"]
    for f in body["footnotes"]:
        assert f["citation_method"] == "api_citation"
        assert set(f) == {
            "n",
            "document_id",
            "document_title",
            "corp_name",
            "section_path",
            "chunk_id",
            "cited_text",
            "dart_url",
            "citation_method",
        }
    assert body["usage"]["model"] == "fake-llm"


# ---- cited_text selection ----


def test_select_cited_text_picks_the_most_overlapping_sentence():
    chunk = (
        "회사는 단기리스에 인식 면제를 적용합니다. "
        "리스부채는 리스개시일 현재 지급되지 않은 리스료의 현재가치로 측정합니다.\n"
        "할인율은 증분차입이자율을 사용합니다."
    )
    assert (
        select_cited_text(chunk, "리스부채는 지급되지 않은 리스료의 현재가치로 측정됩니다")
        == "리스부채는 리스개시일 현재 지급되지 않은 리스료의 현재가치로 측정합니다."
    )
    assert (
        select_cited_text(chunk, "증분차입이자율로 할인") == "할인율은 증분차입이자율을 사용합니다."
    )
    # no overlap at all: the first sentence (ties go to the earlier one)
    assert select_cited_text(chunk, "xyz") == "회사는 단기리스에 인식 면제를 적용합니다."


def test_select_cited_text_is_cut_to_300_chars():
    long_sentence = "리스" * 400 + "."
    cited = select_cited_text(f"짧은 문장. {long_sentence}", "리스리스")
    assert cited == long_sentence[:300] and len(cited) == 300
    assert select_cited_text("", "x") == ""


# ---- pricing ----


def test_ollama_models_cost_zero_and_unknown_models_stay_unpriced():
    usage = LLMUsage(input_tokens=10_000, output_tokens=5_000)
    assert compute_cost("ollama:qwen3:14b", usage, DEFAULT_LLM_PRICES) == Decimal("0")
    assert compute_cost("qwen3:14b", usage, DEFAULT_LLM_PRICES) is None
    assert compute_cost("claude-sonnet-5-5", usage, DEFAULT_LLM_PRICES) == Decimal("0.07")


# ---- GET /llm/status ----


async def test_llm_status_is_admin_only(client, make_org):
    org = await make_org()
    for role in ("member", "reviewer"):
        resp = await client.get("/llm/status", headers=org.headers(role))
        assert resp.status_code == 403
    assert (await client.get("/llm/status")).status_code == 401


async def test_llm_status_fake(client, make_org):
    org = await make_org()
    body = (await client.get("/llm/status", headers=org.headers())).json()
    assert body["provider"] == "fake" and body["model"] == "fake-llm"
    assert body["connected"] is True


async def test_llm_status_local_unreachable(client, app, make_org):
    org = await make_org()
    app.state.llm, server = ollama(httpx.ConnectError("Connection refused"))
    body = (await client.get("/llm/status", headers=org.headers())).json()
    assert body["provider"] == "local" and body["model"] == MODEL
    assert body["connected"] is False and body["model_available"] is None
    assert body["detail"] == "로컬 LLM 서버(Ollama)에 연결할 수 없습니다"
    assert server.requests[0].url == "http://ollama.test:11434/api/tags"


async def test_llm_status_local_model_present_or_missing(client, app, make_org):
    org = await make_org()
    tags = {"models": [{"name": "qwen3:14b", "model": "qwen3:14b"}, {"name": "llama3.1:8b"}]}
    app.state.llm, _ = ollama(tags)
    body = (await client.get("/llm/status", headers=org.headers())).json()
    assert body == {
        "provider": "local",
        "model": MODEL,
        "base_url": "http://ollama.test:11434",
        "connected": True,
        "model_available": True,
        "detail": None,
    }

    app.state.llm, _ = ollama({"models": [{"name": "llama3.1:8b"}]})
    body = (await client.get("/llm/status", headers=org.headers())).json()
    assert body["connected"] is True and body["model_available"] is False
    assert body["detail"] == "모델 qwen3:14b이 없습니다: ollama pull qwen3:14b"


async def test_llm_status_uses_three_second_timeout():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json={"models": []})

    llm = OllamaClient(base_url="http://o:11434", transport=httpx.MockTransport(handler))
    await llm.status()
    assert seen["timeout"]["read"] == 3.0 and seen["timeout"]["connect"] == 3.0


# ---- eval report ----


def test_eval_report_header_and_filename_carry_provider_and_model():
    local = EvalSummary(
        mode="live",
        dataset="d",
        k=8,
        provider="local",
        model="ollama:qwen3:14b",
        citation_method="model_reference",
    )
    md = render_report(local)
    assert "- provider: local" in md
    assert "- model: `ollama:qwen3:14b`" in md
    assert "- citation_method: model_reference" in md
    assert report_filename(local, "20261124T000000Z") == "20261124T000000Z-live-local-qwen3-14b.md"

    claude = EvalSummary(mode="live", dataset="d", k=8, provider="real", model="claude-sonnet-5-5")
    assert "- citation_method: api_citation" in render_report(claude)
    assert report_filename(claude, "S") == "S-live-real-claude-sonnet-5-5.md"
    fake = EvalSummary(mode="fake", dataset="d", k=8, provider="fake", model="fake-llm")
    assert report_filename(fake, "S") == "S-fake-fake-llm.md"


# ---- migration ----


async def test_upgrade_from_0002_adds_dropped_source_refs_with_default_zero(
    client, make_org, ingest, sessionmaker
):
    import asyncio

    from sqlalchemy import text

    from tests.test_embedding_rate_limit_cache import _alembic

    org = await make_org()
    await ingest(org)
    assert (await _ask(client, org)).status_code == 200  # an existing llm_calls row
    await asyncio.to_thread(_alembic, "downgrade", "0002_embedding_cache")
    await asyncio.to_thread(_alembic, "upgrade", "head")
    assert "(head)" in await asyncio.to_thread(_alembic, "current")  # 0003 and later applied
    async with sessionmaker() as s:
        values = (
            await s.scalars(
                text("SELECT dropped_source_refs FROM llm_calls WHERE org_id = :org"),
                {"org": org.org_id},
            )
        ).all()
    assert values == [0]
