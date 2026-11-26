"""전기 대비 주석 변동 분석 API·서비스·요약(로컬 LLM) 테스트. 실제 Postgres, 외부 네트워크 없음.

Ollama는 ``httpx.MockTransport``로 흉내 낸다.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from sqlalchemy import select, text

from footnote_rag.answering.llm import ClaudeClient, JsonResult, LLMUsage
from footnote_rag.answering.local_llm import JSON_RETRY_NOTE, OllamaClient
from footnote_rag.comparison.prompt import (
    SUMMARY_SCHEMA,
    SummaryPrompt,
    build_summary_prompt,
    check_bullets,
)
from footnote_rag.dart import fake_data
from footnote_rag.db.models import AuditLog, LlmCall, NoteComparisonSummary

PRIOR, CURRENT = fake_data.FAKE_ANNUAL_PAIR
OTHER_CORP_ANNUAL = "20260310900102"  # 나래포털(합성) 2025
MODEL = "qwen3:14b"


async def _pair(org, ingest) -> tuple[str, str]:
    return await ingest(org, PRIOR), await ingest(org, CURRENT)


async def _compare(client, org, prior_id, current_id, role="member", **extra):
    return await client.post(
        "/comparisons",
        json={"prior_document_id": prior_id, "current_document_id": current_id, **extra},
        headers=org.headers(role),
    )


async def _audit(sessionmaker, org, action) -> list[AuditLog]:
    async with sessionmaker() as s:
        return list(
            await s.scalars(
                select(AuditLog).where(AuditLog.org_id == org.org_id, AuditLog.action == action)
            )
        )


def _note_index(body: dict, key: str) -> int:
    return next(n["index"] for n in body["result"]["notes"] if n["key"] == key)


# ---------------------------------------------------------------- create / read


async def test_create_and_read_comparison_of_the_synthetic_pair(
    client, make_org, ingest, sessionmaker
):
    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    resp = await _compare(client, org, prior_id, current_id)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    result = body["result"]
    assert result["prior"]["rcept_no"] == PRIOR and result["current"]["rcept_no"] == CURRENT
    assert result["prior"]["dart_url"].startswith("https://dart.fss.or.kr/")
    assert result["current"]["corp_name"] == "가람전자(합성)"
    assert result["scope"] == "consolidated" and result["warnings"] == []
    summary = result["summary"]
    assert summary["status_counts"] == {"changed": 2, "added": 1, "removed": 1, "unchanged": 3}
    assert summary["prior_mismatch"] == 1 and summary["large_change"] == 1
    assert result["notes"][0]["key"] == "우발부채와약정사항"  # watchlist first
    assert [n["index"] for n in result["notes"]] == list(range(len(result["notes"])))

    got = await client.get(f"/comparisons/{body['id']}", headers=org.headers("member"))
    assert got.status_code == 200
    assert got.json()["result"] == result and got.json()["summaries"] == {}

    listed = await client.get("/comparisons", headers=org.headers("reviewer"))
    assert listed.status_code == 200
    item = listed.json()[0]
    assert item["id"] == body["id"]
    assert (item["prior_rcept_no"], item["current_rcept_no"]) == (PRIOR, CURRENT)
    assert item["corp_name"] == "가람전자(합성)" and item["summary"] == summary

    (entry,) = await _audit(sessionmaker, org, "comparison.created")
    assert entry.target_id == body["id"]
    assert entry.metadata_["prior_rcept_no"] == PRIOR
    assert entry.metadata_["current_rcept_no"] == CURRENT
    assert entry.metadata_["scope"] == "consolidated"
    assert entry.metadata_["prior_mismatch"] == 1 and entry.metadata_["large_change"] == 1


async def test_separate_scope_without_separate_notes_warns(client, make_org, ingest):
    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    resp = await _compare(client, org, prior_id, current_id, scope="separate")
    assert resp.status_code == 201, resp.text
    result = resp.json()["result"]
    assert result["summary"]["notes"] == 0 and result["warnings"]
    bad = await _compare(client, org, prior_id, current_id, scope="both")
    assert bad.status_code == 422


async def test_openapi_lists_comparison_schemas(client):
    spec = (await client.get("/openapi.json")).json()
    assert "/comparisons" in spec["paths"]
    assert "/comparisons/{comparison_id}/notes/{note_index}/summary" in spec["paths"]
    for name in ("ComparisonIn", "ComparisonOut", "NoteDiff", "NumberChange", "SummaryOut"):
        assert name in spec["components"]["schemas"]


# ---------------------------------------------------------------- isolation and validation


async def test_other_organization_gets_404(client, make_org, ingest, fake_llm):
    org, other = await make_org(), await make_org()
    prior_id, current_id = await _pair(org, ingest)
    resp = await _compare(client, other, prior_id, current_id)
    assert resp.status_code == 404
    created = (await _compare(client, org, prior_id, current_id)).json()
    for path in (f"/comparisons/{created['id']}",):
        assert (await client.get(path, headers=other.headers("admin"))).status_code == 404
    summary = await client.post(
        f"/comparisons/{created['id']}/notes/0/summary", headers=other.headers("admin")
    )
    assert summary.status_code == 404
    assert (await client.get("/comparisons", headers=other.headers("admin"))).json() == []
    missing = await client.get(f"/comparisons/{uuid.uuid4()}", headers=org.headers("member"))
    assert missing.status_code == 404
    assert (await client.get("/comparisons")).status_code == 401


async def test_invalid_pairs_are_422_with_korean_messages(client, make_org, ingest, worker):
    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    other_corp = await ingest(org, OTHER_CORP_ANNUAL)
    upload = await client.post(
        "/documents/upload",
        files={"file": ("notes.txt", "주석 텍스트입니다.".encode(), "text/plain")},
        headers=org.headers("member"),
    )
    assert upload.status_code == 202, upload.text
    while await worker.run_once():
        pass
    queued = await client.post(
        "/documents/dart", json={"rcept_no": "20260310900103"}, headers=org.headers("member")
    )  # the worker is not run: the document stays queued
    cases = {
        "corp": (prior_id, other_corp, "같은 회사"),
        "order": (current_id, prior_id, "이전이어야"),
        "same": (current_id, current_id, "같은 문서"),
        "not_ready": (prior_id, queued.json()["document_id"], "ready"),
        "upload": (upload.json()["document_id"], current_id, "OpenDART"),
    }
    for name, (a, b, phrase) in cases.items():
        resp = await _compare(client, org, a, b)
        assert resp.status_code == 422, (name, resp.text)
        assert phrase in resp.json()["detail"], (name, resp.json())


# ---------------------------------------------------------------- summaries


async def test_fake_llm_summary_is_grounded_and_recorded(
    client, make_org, ingest, sessionmaker, fake_llm
):
    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    created = (await _compare(client, org, prior_id, current_id)).json()
    index = _note_index(created, "우발부채와약정사항")
    resp = await client.post(
        f"/comparisons/{created['id']}/notes/{index}/summary", headers=org.headers("member")
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "ok" and out["ungrounded_numbers"] == []
    assert out["bullets"] and all(b["refs"] and b["grounded"] for b in out["bullets"])
    assert out["model"] == "fake-llm" and out["latency_ms"] is not None
    call = fake_llm.calls[-1]
    assert call["schema"] == SUMMARY_SCHEMA
    assert "[C2] (추가) " + fake_data._CONTINGENCY_SENTENCE_2025 in call["user"]

    async with sessionmaker() as s:
        calls = list(
            await s.scalars(
                select(LlmCall).where(LlmCall.note_comparison_id == uuid.UUID(created["id"]))
            )
        )
    assert len(calls) == 1 and calls[0].answer_id is None and calls[0].model == "fake-llm"
    (entry,) = await _audit(sessionmaker, org, "comparison.summarized")
    assert entry.metadata_["note_key"] == "우발부채와약정사항"
    assert entry.metadata_["status"] == "ok" and entry.metadata_["model"] == "fake-llm"
    assert "latency_ms" in entry.metadata_

    # Regenerating adds a row; GET shows the latest per note.
    again = await client.post(
        f"/comparisons/{created['id']}/notes/{index}/summary", headers=org.headers("member")
    )
    assert again.status_code == 200
    got = (await client.get(f"/comparisons/{created['id']}", headers=org.headers("member"))).json()
    assert got["summaries"][str(index)]["id"] == again.json()["id"]
    async with sessionmaker() as s:
        rows = await s.scalars(
            select(NoteComparisonSummary).where(
                NoteComparisonSummary.comparison_id == uuid.UUID(created["id"])
            )
        )
        assert len(list(rows)) == 2


async def test_unchanged_note_and_bad_index(client, make_org, ingest):
    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    created = (await _compare(client, org, prior_id, current_id)).json()
    unchanged = _note_index(created, "리스")
    base = f"/comparisons/{created['id']}/notes"
    assert (
        await client.post(f"{base}/{unchanged}/summary", headers=org.headers())
    ).status_code == 422
    assert (await client.post(f"{base}/99/summary", headers=org.headers())).status_code == 404


class _StubLLM:
    """``chat_json`` returning a fixed reply (no network)."""

    provider = "local"

    def __init__(self, data: dict) -> None:
        self.data = data

    async def chat_json(self, *, system, user, schema, validate=None) -> JsonResult:
        return JsonResult(
            data=self.data,
            usage=LLMUsage(10, 5),
            latency_ms=7,
            done_reason="stop",
            model_id="ollama:stub",
        )


async def test_ungrounded_numbers_and_invalid_refs_need_review(
    client, app, make_org, ingest, sessionmaker
):
    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    created = (await _compare(client, org, prior_id, current_id)).json()
    index = _note_index(created, "차입금")
    app.state.llm = _StubLLM(
        {
            "bullets": [
                {"text": "단기차입금이 3,000에서 5,000으로 늘었습니다.", "refs": ["P4", "[C4]"]},
                {"text": "장기차입금은 약 1.2조 원입니다.", "refs": ["C5"]},
                {"text": "전기 수치가 재작성되었습니다.", "refs": ["C999"]},
            ]
        }
    )
    resp = await client.post(
        f"/comparisons/{created['id']}/notes/{index}/summary", headers=org.headers("member")
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "needs_review"
    assert out["ungrounded_numbers"] == ["1.2"]
    first, second, third = out["bullets"]
    assert first["grounded"] and first["refs"] == ["P4", "C4"]
    assert not second["grounded"] and second["ungrounded_numbers"] == ["1.2"]
    assert not third["grounded"] and third["refs"] == [] and third["dropped_refs"] == ["C999"]
    async with sessionmaker() as s:
        call = await s.scalar(
            select(LlmCall).where(LlmCall.note_comparison_id == uuid.UUID(created["id"]))
        )
    assert call.model == "ollama:stub" and str(call.cost_usd) == "0.000000"


def test_dot_thousands_separator_in_the_data_grounds_a_comma_number():
    prompt = SummaryPrompt(
        user="", evidence="[C3] (추가) 약정 금액 | 39.012\n| 표 | 행 |", refs=frozenset({"C3"})
    )
    checked = check_bullets(
        {"bullets": [{"text": "약정 금액은 39,012입니다.", "refs": ["C3"]}]}, prompt
    )
    assert checked.status == "ok" and checked.ungrounded_numbers == []
    reverse = SummaryPrompt(user="", evidence="[C1] 금액 | 39,012", refs=frozenset({"C1"}))
    assert check_bullets({"bullets": [{"text": "39.012", "refs": ["C1"]}]}, reverse).status == "ok"
    assert check_bullets({"bullets": []}, prompt).status == "needs_review"


def test_summary_prompt_prefers_sentence_changes_and_caps_number_rows():
    note = {
        "key": "차입금",
        "title": "15. 차입금 (연결)",
        "status": "changed",
        "lines": [
            {"op": "delete", "ref": "P1", "text": "숫자 | 1", "numbers_only": True, "pair": "C1"},
            {"op": "insert", "ref": "C1", "text": "숫자 | 2", "numbers_only": True, "pair": "P1"},
            {
                "op": "insert",
                "ref": "C2",
                "text": "새 문장입니다.",
                "numbers_only": False,
                "pair": None,
            },
        ],
        "number_changes": [
            {
                "kind": "yoy",
                "table": "표",
                "label": f"행{i}",
                "occurrence": 0,
                "prior_values": ["1"],
                "current_values": ["2"],
                "prior_ref": "P1",
                "current_ref": "C1",
                "cells": [
                    {
                        "position": 1,
                        "prior": "1",
                        "current": "2",
                        "delta": "1",
                        "change_pct": "100",
                        "flags": ["large_change"],
                    }
                ],
                "flags": ["large_change"],
            }
            for i in range(40)
        ],
    }
    prompt = build_summary_prompt(note, prior_report="A 2024", current_report="A 2025")
    assert prompt.user.index("[C2]") < prompt.user.index("[P1] (삭제)")
    assert prompt.number_rows_included == 30
    assert prompt.refs == {"P1", "C1", "C2"}
    assert "3~5개 항목" in prompt.user and "3~5개" not in prompt.evidence


async def test_real_llm_provider_is_501_without_calling_claude(
    client, app, make_org, ingest, settings, monkeypatch
):
    class _NoCall:
        class beta:  # noqa: N801 - mimics the SDK attribute path
            class messages:  # noqa: N801
                @staticmethod
                async def create(**kwargs):
                    raise AssertionError("Claude must not be called")

    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    created = (await _compare(client, org, prior_id, current_id)).json()
    monkeypatch.setattr(settings, "llm_provider", "real")
    app.state.llm = ClaudeClient(api_key="unused", client=_NoCall())
    resp = await client.post(
        f"/comparisons/{created['id']}/notes/0/summary", headers=org.headers("member")
    )
    assert resp.status_code == 501
    assert resp.json()["detail"] == "변동 요약은 현재 로컬 LLM에서만 지원합니다"


# ---------------------------------------------------------------- OllamaClient.chat_json


def _chat(content, *, done_reason="stop") -> dict:
    return {
        "model": MODEL,
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": 100,
        "eval_count": 20,
    }


def _ollama(*replies, think=False):
    requests: list[httpx.Request] = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    client = OllamaClient(
        base_url="http://ollama.test:11434",
        model=MODEL,
        think=think,
        num_ctx=8192,
        max_tokens=512,
        transport=httpx.MockTransport(handler),
    )
    return client, requests


async def test_chat_json_sends_schema_and_options_and_retries_once():
    good = json.dumps({"bullets": [{"text": "a", "refs": ["C1"]}]})
    client, requests = _ollama(_chat("not json"), _chat(good), think="low")
    result = await client.chat_json(system="sys", user="usr", schema=SUMMARY_SCHEMA)
    assert result.data == json.loads(good) and result.model_id == "ollama:qwen3:14b"
    assert result.usage.input_tokens == 200 and result.usage.output_tokens == 40
    first, second = (json.loads(r.content) for r in requests)
    assert first["format"] == SUMMARY_SCHEMA and first["think"] == "low"
    assert first["options"] == {"num_ctx": 8192, "num_predict": 512, "temperature": 0.0}
    assert first["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert second["messages"][-1] == {"role": "user", "content": JSON_RETRY_NOTE}


async def test_chat_json_validator_failure_twice_is_502():
    from footnote_rag.answering.llm import LLMError

    client, requests = _ollama(_chat(json.dumps({"wrong": 1})))
    with pytest.raises(LLMError) as exc:
        await client.chat_json(
            system="s", user="u", schema=SUMMARY_SCHEMA, validate=lambda d: "bullets" in d
        )
    assert exc.value.status_code == 502 and len(requests) == 2


async def test_summary_with_unreachable_ollama_is_503(client, app, make_org, ingest):
    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    created = (await _compare(client, org, prior_id, current_id)).json()
    ollama, _ = _ollama(httpx.ConnectError("refused"))
    app.state.llm = ollama
    resp = await client.post(
        f"/comparisons/{created['id']}/notes/0/summary", headers=org.headers("member")
    )
    assert resp.status_code == 503
    assert "Ollama" in resp.json()["detail"]


async def test_summary_with_local_ollama_records_zero_cost(
    client, app, make_org, ingest, sessionmaker
):
    org = await make_org()
    prior_id, current_id = await _pair(org, ingest)
    created = (await _compare(client, org, prior_id, current_id)).json()
    index = _note_index(created, "차입금")
    reply = {"bullets": [{"text": "단기차입금 3,000 → 5,000 (66.7%).", "refs": ["C4"]}]}
    ollama, requests = _ollama(_chat(json.dumps(reply, ensure_ascii=False)))
    app.state.llm = ollama
    resp = await client.post(
        f"/comparisons/{created['id']}/notes/{index}/summary", headers=org.headers("member")
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok" and resp.json()["cost_usd"] == "0.0000"
    assert len(requests) == 1
    async with sessionmaker() as s:
        call = await s.scalar(
            select(LlmCall).where(LlmCall.note_comparison_id == uuid.UUID(created["id"]))
        )
    assert call.model == "ollama:qwen3:14b" and call.input_tokens == 100


# ---------------------------------------------------------------- migration


async def test_migration_0004_downgrade_and_upgrade(sessionmaker):
    from tests.test_embedding_rate_limit_cache import _alembic

    await asyncio.to_thread(_alembic, "downgrade", "0003_llm_calls_dropped_refs")
    async with sessionmaker() as s:
        tables = set(
            await s.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        )
        columns = set(
            await s.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'llm_calls'"
                )
            )
        )
    assert "note_comparisons" not in tables and "note_comparison_summaries" not in tables
    assert "note_comparison_id" not in columns
    await asyncio.to_thread(_alembic, "upgrade", "head")
    assert "(head)" in await asyncio.to_thread(_alembic, "current")  # 0004 and later applied
    async with sessionmaker() as s:
        tables = set(
            await s.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        )
        nullable = await s.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'llm_calls' AND column_name = 'note_comparison_id'"
            )
        )
    assert {"note_comparisons", "note_comparison_summaries"} <= tables
    assert nullable == "YES"
