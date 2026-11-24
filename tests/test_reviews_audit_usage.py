"""Review state machine + concurrency, append-only audit log, usage aggregation."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from footnote_rag.answering.llm import LLMUsage
from footnote_rag.config import DEFAULT_LLM_PRICES
from footnote_rag.db.models import Answer, AuditLog, LlmCall
from footnote_rag.usage.pricing import compute_cost


async def _pending_answer(client, org) -> str:
    resp = await client.post(
        "/questions", json={"question": "근거 없는 질문"}, headers=org.headers("member")
    )
    body = resp.json()
    assert body["status"] == "pending_review"
    return body["answer_id"]


async def test_review_queue_and_approve(client, make_org, sessionmaker):
    org = await make_org()
    answer_id = await _pending_answer(client, org)
    queue = await client.get("/reviews?status=pending_review", headers=org.headers("reviewer"))
    assert queue.status_code == 200
    assert answer_id in [i["answer_id"] for i in queue.json()]
    resp = await client.post(
        f"/reviews/{answer_id}",
        json={"decision": "approve", "comment": "확인함"},
        headers=org.headers("reviewer"),
    )
    assert resp.status_code == 200 and resp.json()["status"] == "approved"
    # Approved answers become visible to members.
    got = await client.get(f"/answers/{answer_id}", headers=org.headers("member"))
    assert got.status_code == 200 and got.json()["status"] == "approved"
    async with sessionmaker() as s:
        a = await s.get(Answer, uuid.UUID(answer_id))
        audit = await s.scalar(
            select(AuditLog).where(
                AuditLog.action == "review.decided", AuditLog.target_id == answer_id
            )
        )
    assert a.reviewed_by is not None and a.review_comment == "확인함" and a.reviewed_at is not None
    assert audit.metadata_["decision"] == "approve" and audit.metadata_["new_status"] == "approved"


async def test_only_pending_answers_can_be_decided(client, make_org):
    org = await make_org()
    answer_id = await _pending_answer(client, org)
    first = await client.post(
        f"/reviews/{answer_id}", json={"decision": "reject"}, headers=org.headers("admin")
    )
    assert first.status_code == 200 and first.json()["status"] == "rejected"
    again = await client.post(
        f"/reviews/{answer_id}", json={"decision": "approve"}, headers=org.headers("admin")
    )
    assert again.status_code == 409
    missing = await client.post(
        f"/reviews/{uuid.uuid4()}", json={"decision": "approve"}, headers=org.headers("admin")
    )
    assert missing.status_code == 404
    member = await client.post(
        f"/reviews/{answer_id}", json={"decision": "approve"}, headers=org.headers("member")
    )
    assert member.status_code == 403


async def test_concurrent_reviews_one_wins_other_409(client, make_org, sessionmaker):
    org = await make_org()
    answer_id = await _pending_answer(client, org)
    results = await asyncio.gather(
        client.post(
            f"/reviews/{answer_id}", json={"decision": "approve"}, headers=org.headers("reviewer")
        ),
        client.post(
            f"/reviews/{answer_id}", json={"decision": "reject"}, headers=org.headers("admin")
        ),
        client.post(
            f"/reviews/{answer_id}", json={"decision": "approve"}, headers=org.headers("admin")
        ),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409]
    async with sessionmaker() as s:
        decisions = list(
            await s.scalars(
                select(AuditLog).where(
                    AuditLog.action == "review.decided", AuditLog.target_id == answer_id
                )
            )
        )
    assert len(decisions) == 1  # losers wrote no audit entry


async def test_audit_log_rejects_update_and_delete(sessionmaker, make_org):
    org = await make_org()
    async with sessionmaker() as s:
        row_id = await s.scalar(select(AuditLog.id).where(AuditLog.org_id == org.org_id).limit(1))
    for stmt in (
        "UPDATE audit_logs SET action = 'tampered' WHERE id = :id",
        "DELETE FROM audit_logs WHERE id = :id",
    ):
        async with sessionmaker() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                await s.execute(text(stmt), {"id": row_id})
            await s.rollback()
    async with sessionmaker() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("TRUNCATE audit_logs"))
        await s.rollback()
    async with sessionmaker() as s:
        assert (await s.get(AuditLog, row_id)).action != "tampered"


async def test_audit_log_endpoint_filters_and_paginates(client, make_org, ingest):
    org = await make_org()
    await ingest(org)
    await client.post(
        "/questions", json={"question": "리스부채 측정"}, headers=org.headers("member")
    )
    resp = await client.get("/audit-logs?limit=2&offset=0", headers=org.headers("admin"))
    page = resp.json()
    assert resp.status_code == 200 and len(page["items"]) == 2 and page["total"] > 2
    actions = {
        i["action"]
        for i in (await client.get("/audit-logs?limit=500", headers=org.headers("admin"))).json()[
            "items"
        ]
    }
    assert {
        "auth.login_succeeded",
        "user.created",
        "document.dart_import_requested",
        "document.ingest_succeeded",
        "question.asked",
        "answer.created",
    } <= actions
    filtered = (
        await client.get("/audit-logs?action=answer.created", headers=org.headers("admin"))
    ).json()
    assert filtered["total"] == 1 and filtered["items"][0]["action"] == "answer.created"
    assert filtered["items"][0]["request_id"]  # request id from the middleware
    future = (
        await client.get("/audit-logs?from=2999-01-01T00:00:00Z", headers=org.headers("admin"))
    ).json()
    assert future["total"] == 0
    other = await make_org()
    other_actions = (await client.get("/audit-logs?limit=500", headers=other.headers())).json()
    assert all(i["action"] != "answer.created" for i in other_actions["items"])


def test_cost_from_price_table():
    usage = LLMUsage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    # sonnet 5.5: 2 + 1.0 + 0.10 + 2.50
    assert compute_cost("claude-sonnet-5-5", usage, DEFAULT_LLM_PRICES) == Decimal("5.600000")
    assert compute_cost("unknown-model", usage, DEFAULT_LLM_PRICES) is None


async def test_usage_endpoint_aggregates_per_org(client, make_org, sessionmaker):
    org = await make_org()
    other = await make_org()
    async with sessionmaker() as s:
        for latency, model, cost in [
            (100, "claude-sonnet-5-5", Decimal("0.010000")),
            (200, "claude-sonnet-5-5", Decimal("0.020000")),
            (300, "claude-sonnet-5", Decimal("0.030000")),
            (1000, "mystery-model", None),
        ]:
            s.add(
                LlmCall(
                    id=uuid.uuid4(),
                    org_id=org.org_id,
                    model=model,
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_input_tokens=1,
                    cache_creation_input_tokens=2,
                    cost_usd=cost,
                    latency_ms=latency,
                    stop_reason="end_turn",
                )
            )
        s.add(
            LlmCall(
                id=uuid.uuid4(),
                org_id=other.org_id,
                model="claude-sonnet-5-5",
                input_tokens=999,
                output_tokens=999,
                cost_usd=Decimal("9"),
                latency_ms=5,
                stop_reason="end_turn",
            )
        )
        await s.commit()
    body = (await client.get("/usage", headers=org.headers("admin"))).json()
    assert body["calls"] == 4
    assert body["input_tokens"] == 40 and body["output_tokens"] == 20
    assert body["cache_read_input_tokens"] == 4 and body["cache_creation_input_tokens"] == 8
    assert Decimal(body["cost_usd"]) == Decimal("0.06")
    assert body["unpriced_calls"] == 1
    assert body["latency_ms_p50"] == pytest.approx(250.0)
    assert body["latency_ms_p95"] == pytest.approx(895.0)
    assert {m["model"] for m in body["by_model"]} == {
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "mystery-model",
    }
    empty = (
        await client.get("/usage?from=2999-01-01T00:00:00Z", headers=org.headers("admin"))
    ).json()
    assert empty["calls"] == 0 and empty["latency_ms_p50"] is None


async def test_llm_call_records_actual_response_model(client, app, make_org, ingest, sessionmaker):
    from footnote_rag.answering.llm import FakeLLM

    org = await make_org()
    await ingest(org)
    app.state.llm = FakeLLM(model="claude-sonnet-5")  # e.g. served by the fallback model
    body = (
        await client.post(
            "/questions", json={"question": "리스부채"}, headers=org.headers("member")
        )
    ).json()
    async with sessionmaker() as s:
        call = await s.scalar(
            select(LlmCall).where(LlmCall.answer_id == uuid.UUID(body["answer_id"]))
        )
    assert call.model == "claude-sonnet-5"
    assert call.cost_usd is not None and call.latency_ms >= 0
    assert body["usage"]["cost_usd"] is not None
