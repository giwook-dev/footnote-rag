"""Retrieval scoping, the review gate, Claude request shape and refusal handling."""

import uuid
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from sqlalchemy import select

from footnote_rag.answering.llm import FALLBACK_BETA, ClaudeClient, FakeLLM, LLMError
from footnote_rag.answering.prompt import build_messages
from footnote_rag.answering.service import review_reasons
from footnote_rag.db.models import AuditLog, LlmCall
from footnote_rag.retrieval.embedder import FakeEmbedder, iter_batches
from footnote_rag.retrieval.search import RetrievedChunk

QUESTION = "회사는 리스부채를 어떻게 측정하나요?"


async def test_published_answer_has_footnotes(client, make_org, ingest, fake_llm):
    org = await make_org()
    doc_id = await ingest(org, "20260310900001")
    resp = await client.post(
        "/questions", json={"question": QUESTION}, headers=org.headers("member")
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "published", body
    assert "[1]" in body["text"] and "[2]" in body["text"]
    f1 = body["footnotes"][0]
    assert f1["n"] == 1 and f1["document_id"] == doc_id
    assert f1["corp_name"] == "가람전자(합성)"
    assert f1["section_path"] and f1["cited_text"] and f1["chunk_id"]
    assert f1["dart_url"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310900001"
    assert body["usage"]["model"] == "fake-llm"
    assert body["usage"]["cost_usd"] is None  # fake model is not in the price table
    assert isinstance(body["latency_ms"], int)

    # The request carried citation-enabled search_result blocks, then the question.
    blocks = fake_llm.calls[-1]["messages"][0]["content"]
    assert all(b["type"] == "search_result" for b in blocks[:-1])
    assert all(b["citations"] == {"enabled": True} for b in blocks[:-1])
    assert blocks[0]["source"].startswith(f"doc:{doc_id}#chunk:")
    assert " · " in blocks[0]["title"]
    assert blocks[-1] == {"type": "text", "text": QUESTION}

    got = await client.get(f"/answers/{body['answer_id']}", headers=org.headers("member"))
    assert got.status_code == 200 and got.json()["footnotes"] == body["footnotes"]


async def test_no_retrieval_results_skips_claude_and_goes_to_review(client, make_org, fake_llm):
    org = await make_org()  # no documents at all
    resp = await client.post(
        "/questions", json={"question": QUESTION}, headers=org.headers("reviewer")
    )
    body = resp.json()
    assert body["status"] == "pending_review"
    assert "no_retrieval_results" in body["review_reason"]
    assert body["text"] == "제공된 문서에서 근거를 찾지 못했습니다."
    assert fake_llm.calls == []  # Claude was not called


async def test_low_score_goes_to_review(client, make_org, ingest, settings, monkeypatch):
    org = await make_org()
    await ingest(org)
    monkeypatch.setattr(settings, "min_retrieval_score", 0.999)
    body = (
        await client.post("/questions", json={"question": QUESTION}, headers=org.headers("admin"))
    ).json()
    assert body["status"] == "pending_review"
    assert "low_retrieval_score" in body["review_reason"]


async def test_refusal_is_pending_review(client, app, make_org, ingest, sessionmaker):
    org = await make_org()
    await ingest(org)
    app.state.llm = FakeLLM(stop_reason="refusal")
    body = (
        await client.post("/questions", json={"question": QUESTION}, headers=org.headers("admin"))
    ).json()
    assert body["status"] == "pending_review"
    assert body["review_reason"].split(",")[0] == "refusal"
    assert body["footnotes"] == []
    async with sessionmaker() as s:
        call = await s.scalar(
            select(LlmCall).where(LlmCall.answer_id == uuid.UUID(body["answer_id"]))
        )
    assert call.stop_reason == "refusal"


async def test_no_citations_goes_to_review(client, app, make_org, ingest):
    class Uncited(FakeLLM):
        async def answer(self, *, system, messages):
            r = await super().answer(system=system, messages=messages)
            for block in r.content:
                block.pop("citations", None)
            return r

    org = await make_org()
    await ingest(org)
    app.state.llm = Uncited()
    body = (
        await client.post("/questions", json={"question": QUESTION}, headers=org.headers("admin"))
    ).json()
    assert body["status"] == "pending_review" and body["review_reason"] == "no_citations"


async def test_review_mode_always(client, make_org, ingest, settings, monkeypatch):
    org = await make_org()
    await ingest(org)
    monkeypatch.setattr(settings, "review_mode", "always")
    body = (
        await client.post("/questions", json={"question": QUESTION}, headers=org.headers("admin"))
    ).json()
    assert body["status"] == "pending_review" and body["review_reason"] == "review_mode_always"


async def test_member_cannot_see_pending_answer(client, make_org):
    org = await make_org()
    body = (
        await client.post("/questions", json={"question": QUESTION}, headers=org.headers("member"))
    ).json()
    assert body["status"] == "pending_review"
    assert body["footnotes"] == [] and "검토 대기" in body["text"]
    resp = await client.get(f"/answers/{body['answer_id']}", headers=org.headers("member"))
    assert resp.status_code == 404
    resp = await client.get(f"/answers/{body['answer_id']}", headers=org.headers("reviewer"))
    assert resp.status_code == 200 and resp.json()["status"] == "pending_review"


async def test_search_is_scoped_to_org_ready_docs_and_document_ids(client, make_org, ingest):
    org = await make_org()
    other = await make_org()
    doc1 = await ingest(org, "20260310900001")
    doc2 = await ingest(org, "20260310900002")
    await ingest(other, "20260310900003")
    queued = await client.post(
        "/documents/dart", json={"rcept_no": "20260310900099"}, headers=org.headers()
    )
    assert queued.status_code == 202  # stays queued: never searchable

    hits = (
        await client.post(
            "/search", json={"query": "리스부채", "k": 50}, headers=org.headers("member")
        )
    ).json()
    assert {h["document_id"] for h in hits} == {doc1, doc2}

    hits = (
        await client.post(
            "/search",
            json={"query": "리스부채", "k": 50, "document_ids": [doc2]},
            headers=org.headers("member"),
        )
    ).json()
    assert hits and {h["document_id"] for h in hits} == {doc2}

    body = (
        await client.post(
            "/questions",
            json={"question": QUESTION, "document_ids": [doc1]},
            headers=org.headers("member"),
        )
    ).json()
    assert {f["document_id"] for f in body["footnotes"]} == {doc1}


async def test_usage_and_audit_rows_for_question(client, make_org, ingest, sessionmaker):
    org = await make_org()
    await ingest(org)
    body = (
        await client.post("/questions", json={"question": QUESTION}, headers=org.headers("member"))
    ).json()
    async with sessionmaker() as s:
        actions = {
            a.action: a
            for a in await s.scalars(select(AuditLog).where(AuditLog.org_id == org.org_id))
        }
    assert actions["answer.created"].metadata_["status"] == "published"
    assert actions["answer.created"].target_id == body["answer_id"]
    assert actions["question.asked"].metadata_["retrieved"] > 0
    assert "text" not in actions["question.asked"].metadata_


def test_review_reason_rules(settings):
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
    low = RetrievedChunk(**{**chunk.__dict__, "score": -0.5})
    ok = SimpleNamespace(stop_reason="end_turn")
    refusal = SimpleNamespace(stop_reason="refusal")
    assert (
        review_reasons(retrieved=[chunk], llm_response=ok, footnote_count=2, settings=settings)
        == []
    )
    assert review_reasons(
        retrieved=[chunk], llm_response=ok, footnote_count=0, settings=settings
    ) == ["no_citations"]
    assert review_reasons(
        retrieved=[low], llm_response=ok, footnote_count=1, settings=settings
    ) == ["low_retrieval_score"]
    assert review_reasons(retrieved=[], llm_response=None, footnote_count=0, settings=settings) == [
        "no_retrieval_results",
        "no_citations",
    ]
    assert review_reasons(
        retrieved=[chunk], llm_response=refusal, footnote_count=0, settings=settings
    ) == ["refusal"]


# ---- ClaudeClient against a stub SDK object (no network) ----


class _StubMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.kwargs = response, error, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def _stub_client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def _response(stop_reason="end_turn", model="claude-sonnet-5-5"):
    from anthropic.types.beta import BetaMessage

    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {
                    "type": "text",
                    "text": "리스부채는 현재가치로 측정합니다.",
                    "citations": [
                        {
                            "type": "search_result_location",
                            "cited_text": "현재가치",
                            "source": "doc:a#chunk:b",
                            "title": "t",
                            "search_result_index": 0,
                            "start_block_index": 0,
                            "end_block_index": 1,
                        }
                    ],
                },
            ],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 10,
            },
        }
    )


async def test_claude_request_shape():
    stub = _StubMessages(_response())
    llm = ClaudeClient(api_key="unused", client=_stub_client(stub))
    chunk = RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title="문서",
        document_meta={},
        source="dart",
        source_ref=None,
        section_path="주석 > 12. 리스",
        chunk_index=0,
        text="본문",
        score=0.8,
    )
    result = await llm.answer(system="sys", messages=build_messages("질문?", [chunk]))
    kw = stub.kwargs
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["max_tokens"] == 16000
    assert kw["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    for forbidden in ("temperature", "top_p", "top_k", "tool_choice", "stream"):
        assert forbidden not in kw
    assert "budget_tokens" not in str(kw["thinking"]) and kw["thinking"]["type"] != "disabled"
    assert "format" not in kw["output_config"]
    assert kw["messages"][-1]["role"] == "user"  # no assistant prefill
    sr = kw["messages"][0]["content"][0]
    assert sr["type"] == "search_result" and sr["citations"] == {"enabled": True}
    assert sr["title"] == "문서 · 주석 > 12. 리스"
    assert result.model == "claude-sonnet-5-5"
    assert (
        result.usage.cache_read_input_tokens == 50
        and result.usage.cache_creation_input_tokens == 10
    )
    assert [b["type"] for b in result.content] == ["thinking", "text"]


async def test_claude_refusal_is_checked_before_content():
    llm = ClaudeClient(api_key="unused", client=_stub_client(_StubMessages(_response("refusal"))))
    result = await llm.answer(system="s", messages=[{"role": "user", "content": "q"}])
    assert result.stop_reason == "refusal" and result.content == []


def _status_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (_status_error(anthropic.RateLimitError, 429), 503),
        (_status_error(anthropic.InternalServerError, 500), 503),
        (_status_error(anthropic.BadRequestError, 400), 502),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), 503),
    ],
)
async def test_claude_errors_map_to_502_503(error, status):
    llm = ClaudeClient(api_key="unused", client=_stub_client(_StubMessages(error=error)))
    with pytest.raises(LLMError) as exc:
        await llm.answer(system="s", messages=[])
    assert exc.value.status_code == status
    assert "boom" not in exc.value.public_message


async def test_llm_error_returns_502_and_is_audited(client, app, make_org, ingest, sessionmaker):
    class Broken(FakeLLM):
        async def answer(self, *, system, messages):
            raise LLMError(502, "LLM request failed")

    org = await make_org()
    await ingest(org)
    app.state.llm = Broken()
    resp = await client.post(
        "/questions", json={"question": QUESTION}, headers=org.headers("member")
    )
    assert resp.status_code == 502
    async with sessionmaker() as s:
        failed = await s.scalar(
            select(AuditLog).where(
                AuditLog.org_id == org.org_id, AuditLog.action == "question.failed"
            )
        )
    assert failed is not None


# ---- embeddings ----


async def test_voyage_embedder_uses_input_types_and_batches(monkeypatch):
    from footnote_rag.retrieval import embedder as emb

    calls = []

    class StubAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def embed(self, texts, model=None, input_type=None, output_dimension=None, **kw):
            calls.append((len(texts), model, input_type, output_dimension))
            return SimpleNamespace(embeddings=[[0.0] * 4 for _ in texts], total_tokens=len(texts))

    import voyageai

    monkeypatch.setattr(voyageai, "AsyncClient", StubAsyncClient)
    e = emb.VoyageEmbedder(api_key="unused", dim=4, max_texts=1000, max_tokens=50)
    assert e.model == "voyage-4"
    r = await e.embed_documents(["가" * 20] * 5)
    assert len(r.vectors) == 5 and r.total_tokens == 5
    assert [c[0] for c in calls] == [2, 2, 1]  # token budget splits batches
    assert {c[2] for c in calls} == {"document"}
    assert {c[1] for c in calls} == {"voyage-4"}
    await e.embed_query("질문")
    assert calls[-1][2] == "query"


def test_batches_respect_text_count_limit():
    batches = list(iter_batches(["a"] * 2500, max_texts=1000, max_tokens=10**9))
    assert [len(b) for b in batches] == [1000, 1000, 500]


async def test_fake_embedder_is_deterministic_and_normalised():
    e = FakeEmbedder(dim=64)
    a = (await e.embed_query("리스부채 측정")).vectors[0]
    b = (await e.embed_query("리스부채 측정")).vectors[0]
    assert a == b
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9
