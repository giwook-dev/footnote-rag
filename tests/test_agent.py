"""도구 쓰는 에이전트: 도구, 실행 루프, OllamaClient.chat_tools, SSE API, 마이그레이션.

실제 Postgres, 외부 네트워크 없음. 모델은 스크립트형 ``FakeLLM``(또는 ``httpx.MockTransport``의 Ollama),
OpenDART는 ``FakeDartClient``(또는 MockTransport의 ``HttpDartClient``)다.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select, text

from footnote_rag.agent import tools as agent_tools
from footnote_rag.agent.runner import run_agent, verify_answer
from footnote_rag.agent.tools import (
    AgentContext,
    CalcError,
    build_context,
    cover_period,
    evaluate_expression,
    report_code,
    run_tool,
)
from footnote_rag.answering.llm import ClaudeClient, FakeLLM, LLMError
from footnote_rag.answering.local_llm import OllamaClient
from footnote_rag.dart import fake_data
from footnote_rag.dart.client import DartError, FakeDartClient, HttpDartClient
from footnote_rag.db.models import AgentRun, AgentStep, AuditLog, LlmCall, User

PRIOR, CURRENT = fake_data.FAKE_ANNUAL_PAIR  # 가람전자(합성) 2024 / 2025 사업보고서
OTHER_CORP_ANNUAL = "20260310900102"  # 나래포털(합성) 2025
MODEL = "qwen3:14b"


def _call(name: str, **arguments) -> dict:
    return {"name": name, "arguments": arguments}


DEBT_RATIO_SCRIPT = [
    [_call("list_documents")],
    [_call("get_financials", document="D1", accounts=["부채총계", "자본총계"], statement="BS")],
    [
        _call(
            "calculate",
            expression="round(a / b * 100, 1)",
            values={"a": "38,765,000,000", "b": "60000000000"},
        )
    ],
    "가람전자(합성)의 2025년 연결 부채비율은 64.6%입니다 [K1]. 부채총계는 38,765,000,000원 [F1], "
    "자본총계는 60,000,000,000원입니다 [F2].",
]


async def _garam_pair(org, ingest) -> None:
    await ingest(org, PRIOR)
    await ingest(org, CURRENT)


async def _run(org, settings, script, **overrides):
    llm = FakeLLM(agent_script=script, **overrides.pop("llm_kwargs", {}))
    events: list[tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    outcome = await run_agent(
        overrides.pop("question", "가람전자 2025 연결 부채비율은?"),
        org_id=org.org_id,
        user_id=org.admin_id,
        settings=settings.model_copy(update=overrides) if overrides else settings,
        llm=llm,
        embedder=overrides.get("embedder") or _embedder(settings),
        dart_client=FakeDartClient(),
        emit=emit,
    )
    return outcome, events, llm


def _embedder(settings):
    from footnote_rag.retrieval.embedder import FakeEmbedder

    return FakeEmbedder(dim=settings.embedding_dim)


async def _context(sessionmaker, org, settings, dart=None) -> tuple[AgentContext, object]:
    session = sessionmaker()
    user = await session.get(User, org.admin_id)
    ctx = await build_context(
        session,
        user=user,
        settings=settings,
        embedder=_embedder(settings),
        dart_client=dart or FakeDartClient(),
    )
    return ctx, session


def _bare_context(settings, numbers=()) -> AgentContext:
    ctx = AgentContext(
        session=None, user=None, settings=settings, embedder=None, dart_client=FakeDartClient()
    )
    ctx.numbers = list(numbers)
    return ctx


# ---------------------------------------------------------------- registry


def test_six_tools_have_korean_descriptions_and_json_schemas():
    names = list(agent_tools.TOOLS)
    assert names == [
        "list_documents",
        "search_filings",
        "read_note",
        "compare_notes",
        "get_financials",
        "calculate",
    ]
    for tool in agent_tools.TOOLS.values():
        spec = tool.spec()
        assert spec["type"] == "function" and spec["function"]["name"] == tool.name
        assert any("가" <= ch <= "힣" for ch in tool.description) and tool.label
        params = spec["function"]["parameters"]
        assert params["type"] == "object" and isinstance(params["properties"], dict)
        assert set(params["required"]) <= set(params["properties"])


# ---------------------------------------------------------------- calculate


def test_expression_arithmetic_parentheses_and_round():
    names = {"a": Decimal("38765000000"), "b": Decimal("60000000000")}
    assert evaluate_expression("round(a / b * 100, 1)", names) == Decimal("64.6")
    assert evaluate_expression("(a - b) / 1000000", names) == Decimal("-21235")
    assert evaluate_expression("round(a / 1000000000)", names) == 39
    assert str(evaluate_expression("round(5 / 2)", {})) == "3"  # half up, not banker's
    assert evaluate_expression("-a + b", names) == Decimal("21235000000")


@pytest.mark.parametrize(
    "expression",
    [
        "a.real",
        "abs(a)",
        "a ** 2",
        "'1' + '2'",
        "a[0]",
        "a // b",
        "a % b",
        "__import__('os')",
        "round(a, b)",
        "round(a, ndigits=1)",
        "[a, b]",
        "a if a else b",
        "lambda: a",
    ],
)
def test_expression_rejects_other_syntax(expression):
    with pytest.raises(CalcError):
        evaluate_expression(expression, {"a": Decimal(1), "b": Decimal(2)})


def test_expression_division_by_zero_and_literals():
    with pytest.raises(CalcError, match="0으로 나눌 수 없습니다"):
        evaluate_expression("a / (b - b)", {"a": Decimal(1), "b": Decimal(2)})
    for ok in ("a * 100", "a / 1000000", "a * 1000000000000", "a + 1", "round(a, 2)"):
        evaluate_expression(ok, {"a": Decimal(3)})
    for bad in ("a * 1234", "a * 0.5", "a + 12", "a * 1.0"):
        with pytest.raises(CalcError, match="수식에"):
            evaluate_expression(bad, {"a": Decimal(3)})


async def test_calculate_requires_values_from_earlier_results(settings):
    ctx = _bare_context(settings, ["38765000000", "60000000000"])
    bad = await run_tool(
        ctx, "calculate", {"expression": "a / b", "values": {"a": "38765", "b": "60000000000"}}
    )
    assert not bad.ok and "출처 없는 값: 38765" in bad.error and bad.text.startswith("오류: ")
    literal = await run_tool(
        ctx, "calculate", {"expression": "a / 1234", "values": {"a": "38765000000"}}
    )
    assert not literal.ok and "1234" in literal.error
    good = await run_tool(
        ctx,
        "calculate",
        {"expression": "round(a / b * 100, 1)", "values": {"a": "38,765,000,000", "b": 6e10}},
    )
    assert good.ok, good.error
    assert good.sources[0]["id"] == "K1" and good.sources[0]["result"] == "64.6"
    assert "64.6" in good.numbers and "64.6" in ctx.numbers  # later calculations can use K1


async def test_calculate_accepts_dot_thousands_and_negatives(settings):
    ctx = _bare_context(settings, ["39.012", "1234567"])
    ctx.source_numbers = {"F1": ["39.012"], "F2": ["1234567"]}
    result = await run_tool(
        ctx,
        "calculate",
        {"expression": "a + b", "values": {"a": "39,012", "b": "(1.234.567)"}},
    )
    assert result.ok, result.error
    source = result.sources[0]
    assert source["result"] == "-1195555"
    assert [(i["name"], i["sources"]) for i in source["inputs"]] == [("a", ["F1"]), ("b", ["F2"])]
    decimal = await run_tool(ctx, "calculate", {"expression": "a * 2", "values": {"a": "39.012"}})
    assert decimal.ok and decimal.sources[0]["result"] == "78.024"


async def test_unknown_tool_and_bad_arguments_are_error_results(settings):
    ctx = _bare_context(settings)
    unknown = await run_tool(ctx, "delete_everything", {})
    assert not unknown.ok and "알 수 없는 도구" in unknown.error
    missing = await run_tool(ctx, "calculate", {"expression": "1 + 1"})
    assert not missing.ok and "필수 인자" in missing.error
    extra = await run_tool(ctx, "list_documents", {"verbose": True})
    assert not extra.ok and "알 수 없는 인자" in extra.error
    wrong = await run_tool(ctx, "calculate", {"expression": "1", "values": ["a"]})
    assert not wrong.ok and "객체" in wrong.error
    not_json = await run_tool(ctx, "calculate", "{not json")
    assert not not_json.ok and "JSON" in not_json.error


# ---------------------------------------------------------------- documents, notes, financials


async def test_aliases_and_list_documents_are_org_scoped(make_org, ingest, sessionmaker, settings):
    org, other = await make_org(), await make_org()
    await _garam_pair(org, ingest)
    await ingest(other, OTHER_CORP_ANNUAL)
    ctx, session = await _context(sessionmaker, org, settings)
    other_ctx, other_session = await _context(sessionmaker, other, settings)
    try:
        assert list(ctx.aliases) == ["D1", "D2"]
        assert [d.meta["rcept_no"] for d in ctx.aliases.values()] == [CURRENT, PRIOR]
        listed = await run_tool(ctx, "list_documents", {})
        assert listed.ok and "D1 · 가람전자(합성) · 사업보고서 (2025.12)" in listed.text
        assert "나래포털" not in listed.text
        assert str(next(iter(ctx.aliases.values())).id) not in listed.text  # no UUIDs
        other_listed = await run_tool(other_ctx, "list_documents", {})
        assert "가람전자" not in other_listed.text and list(other_ctx.aliases) == ["D1"]
        # D2 exists only in the first organization; a UUID is never accepted either
        missing = await run_tool(other_ctx, "read_note", {"document": "D2", "note": "리스"})
        assert not missing.ok and "알 수 없는 문서 별칭" in missing.error
        doc_id = str(ctx.aliases["D1"].id)
        by_uuid = await run_tool(other_ctx, "read_note", {"document": doc_id, "note": "리스"})
        assert not by_uuid.ok and "알 수 없는 문서 별칭" in by_uuid.error
    finally:
        await session.close()
        await other_session.close()


async def test_read_note_by_name_number_similarity_and_candidates(
    make_org, ingest, sessionmaker, settings
):
    org = await make_org()
    await _garam_pair(org, ingest)
    ctx, session = await _context(sessionmaker, org, settings)
    try:
        exact = await run_tool(ctx, "read_note", {"document": "D1", "note": "우발부채와 약정사항"})
        assert exact.ok, exact.error
        source = exact.sources[0]
        assert source["id"] == "N1" and source["note_title"].startswith("20. 우발부채와 약정사항")
        assert "L1 " in exact.text and source["refs"][0] == "L1"
        assert "합성은행과 운영자금 한도약정을 새로 체결" in exact.text
        number = await run_tool(ctx, "read_note", {"document": "D1", "note": "15"})
        assert number.ok and number.sources[0]["note_title"].startswith("15. 차입금")
        similar = await run_tool(ctx, "read_note", {"document": "D1", "note": "우발부채"})
        assert similar.ok and similar.sources[0]["note_title"].startswith("20.")
        assert similar.sources[0]["id"] == "N3"  # ids keep counting within a run
        none = await run_tool(ctx, "read_note", {"document": "D1", "note": "법인세비용"})
        assert not none.ok and "후보:" in none.error and "1. 일반사항" in none.error
        assert len(ctx.parse_cache) == 1  # the filing was parsed once for the run
        paged = await run_tool(ctx, "read_note", {"document": "D1", "note": "차입금", "offset": 2})
        assert paged.ok and paged.sources[0]["line_start"] == 3
    finally:
        await session.close()


async def test_compare_notes_reports_changes_and_rejects_other_companies(
    make_org, ingest, sessionmaker, settings
):
    org = await make_org()
    await _garam_pair(org, ingest)
    await ingest(org, OTHER_CORP_ANNUAL)
    ctx, session = await _context(sessionmaker, org, settings)
    try:
        # D1 가람 2025, D2 가람 2024, D3 나래포털 2025
        mismatch = await run_tool(
            ctx, "compare_notes", {"prior": "D2", "current": "D3", "note": "리스"}
        )
        assert not mismatch.ok and "corp_code" in mismatch.error
        diff = await run_tool(
            ctx, "compare_notes", {"prior": "D2", "current": "D1", "note": "우발부채"}
        )
        assert diff.ok, diff.error
        source = diff.sources[0]
        assert source["id"] == "C1" and source["status"] == "changed"
        added = [ref for ref, line in source["lines"].items() if "한도약정" in line]
        assert added and added[0].startswith("C") and f"+ {added[0]} " in diff.text
        borrowings = await run_tool(
            ctx, "compare_notes", {"prior": "D2", "current": "D1", "note": "차입금"}
        )
        assert (
            borrowings.ok and "단기차입금" in borrowings.text and "large_change" in borrowings.text
        )
        assert "전기 수치 불일치" in borrowings.text and "(단위 백만원)" in borrowings.text
    finally:
        await session.close()


def test_report_code_mapping():
    assert report_code("사업보고서 (2025.12)") == "11011"
    assert report_code("[기재정정]사업보고서 (2024.12)") == "11011"
    assert report_code("반기보고서 (2025.06)") == "11012"
    assert report_code("분기보고서 (2026.03)") == "11013"
    assert report_code("분기보고서 (2025.09)") == "11014"
    assert report_code("감사보고서 (2025.12)") is None
    # imported documents keep the plain DOCUMENT-NAME; the period comes from the cover
    assert report_code("분기보고서", ("2026", "03")) == "11013"
    assert report_code("분기보고서") is None
    cover = "삼성전자 (제 57 기) 사업연도 2025년 01월 01일 부터 2025년 12월 31일 까지"
    assert cover_period(cover) == ("2025", "12")
    assert cover_period("제 58 기 1분기 2026년 1월 1일부터 2026년 3월 31일까지") == ("2026", "03")
    assert cover_period("기간 없음") is None


async def test_get_financials_partial_match_and_account_list(
    make_org, ingest, sessionmaker, settings
):
    org = await make_org()
    await _garam_pair(org, ingest)
    dart = FakeDartClient()
    ctx, session = await _context(sessionmaker, org, settings, dart)
    try:
        found = await run_tool(
            ctx, "get_financials", {"document": "D1", "accounts": ["부채", "자본 총계"]}
        )
        assert found.ok, found.error
        assert [s["account"] for s in found.sources] == ["부채총계", "자본총계"]
        f1 = found.sources[0]
        assert f1["id"] == "F1" and f1["current"]["amount"] == "38765000000"
        assert f1["prior"]["amount"] == "35000000000" and f1["unit"] == "원"
        assert "38,765,000,000" in found.text and "38765000000" in found.numbers
        assert dart.financial_calls[-1] == ("99000001", "2025", "11011", "CFS")
        prior_year = await run_tool(
            ctx, "get_financials", {"document": "D2", "accounts": ["매출액"], "statement": "IS"}
        )
        assert prior_year.ok and dart.financial_calls[-1][1] == "2024"
        none = await run_tool(
            ctx, "get_financials", {"document": "D1", "accounts": ["연구개발비"], "statement": "bs"}
        )
        assert not none.ok and "일치하는 계정이 없습니다" in none.error
        assert "자산총계, 부채총계, 자본총계" in none.error and "매출액" not in none.error
    finally:
        await session.close()


def _fnltt_transport(responses: list[dict], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=responses.pop(0))

    return httpx.MockTransport(handler)


async def test_http_financial_statements_cache_and_no_data(tmp_path):
    rows = fake_data.financial_statement_rows("99000001", "2025", "11011", "CFS")
    seen: list[httpx.Request] = []
    transport = _fnltt_transport(
        [{"status": "000", "message": "정상", "list": rows}, {"status": "013", "message": "없음"}],
        seen,
    )
    client = HttpDartClient("KEY", transport=transport, fnltt_cache_dir=tmp_path / "fnltt")
    first = await client.financial_statements("99000001", "2025", "11011", "CFS")
    assert first == rows and len(seen) == 1
    assert seen[0].url.path.endswith("/fnlttSinglAcntAll.json")
    params = dict(seen[0].url.params)
    assert params["corp_code"] == "99000001" and params["bsns_year"] == "2025"
    assert params["reprt_code"] == "11011" and params["fs_div"] == "CFS"
    assert (tmp_path / "fnltt" / "99000001_2025_11011_CFS.json").is_file()
    again = await client.financial_statements("99000001", "2025", "11011", "CFS")
    assert again == rows and len(seen) == 1  # served from data/dart/fnltt
    empty = await client.financial_statements("99000001", "2023", "11011", "CFS")
    assert empty == [] and len(seen) == 2
    assert not (tmp_path / "fnltt" / "99000001_2023_11011_CFS.json").exists()


async def test_http_financial_statements_other_status_is_dart_error(tmp_path):
    seen: list[httpx.Request] = []
    client = HttpDartClient(
        "KEY", transport=_fnltt_transport([{"status": "020", "message": "limit"}], seen)
    )
    with pytest.raises(DartError) as exc:
        await client.financial_statements("1", "2025", "11011", "OFS")
    assert exc.value.code == "020" and exc.value.retryable is True


async def test_fake_dart_financials_only_for_the_synthetic_company():
    dart = FakeDartClient()
    rows = await dart.financial_statements("99000001", "2025", "11011", "CFS")
    assert {r["account_nm"] for r in rows} == {
        "자산총계",
        "부채총계",
        "자본총계",
        "매출액",
        "영업이익",
        "당기순이익",
    }
    assert all(r["currency"] == "KRW" for r in rows)
    assert await dart.financial_statements("99000002", "2025", "11011", "CFS") == []
    assert await dart.financial_statements("99000001", "2025", "11012", "CFS") == []


# ---------------------------------------------------------------- verification (pure)


def test_verify_answer_drops_unknown_ids_and_flags_numbers():
    sources = {
        "F1": {"id": "F1"},
        "K1": {"id": "K1"},
        "N1": {"id": "N1", "refs": ["L1", "L2"]},
    }
    checked = verify_answer(
        "부채비율은 64.6%입니다 [K1, F9]. 근거 줄 [N1:L2][N1:L7]. 2025년 [D1] 기준입니다.",
        question="2025년 부채비율은?",
        sources=sources,
        haystack="64.6 38765000000",
    )
    assert checked.cited == ["K1", "N1:L2"]
    assert checked.dropped == ["F9", "N1:L7", "D1"]
    assert "[F9]" not in checked.text and "[K1]" in checked.text and "[D1]" not in checked.text
    assert checked.ungrounded_numbers == [] and checked.reasons == []
    direct = verify_answer("약 64.61%입니다 [K1]", question="q", sources=sources, haystack="64.6")
    assert direct.ungrounded_numbers == ["64.61"] and direct.reasons == ["ungrounded_number"]
    none = verify_answer("찾지 못했습니다.", question="q", sources=sources, haystack="")
    assert none.reasons == ["no_citations"]
    errors = verify_answer(
        "답 [K1]", question="q", sources=sources, haystack="", tool_calls=3, tool_errors=2
    )
    assert errors.reasons == ["tool_errors"]


# ---------------------------------------------------------------- loop


async def test_completed_run_stores_steps_and_llm_calls(make_org, ingest, sessionmaker, settings):
    org = await make_org()
    await _garam_pair(org, ingest)
    outcome, events, _llm = await _run(org, settings, DEBT_RATIO_SCRIPT)
    assert outcome.status == "completed", (outcome.review_reasons, outcome.answer)
    assert outcome.step_count == 4 and outcome.tool_call_count == 3
    assert [c["id"] for c in outcome.citations] == ["K1", "F1", "F2"]
    assert [e for e, _d in events] == ["run"] + ["step"] * 7 + ["answer"]
    async with sessionmaker() as s:
        run = await s.get(AgentRun, outcome.run_id)
        steps = list(
            await s.scalars(
                select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.idx)
            )
        )
        calls = await s.scalar(
            select(func.count()).select_from(LlmCall).where(LlmCall.agent_run_id == run.id)
        )
    assert run.status == "completed" and run.finished_at is not None and run.answer
    assert [(st.kind, st.tool_name) for st in steps] == [
        ("model", None),
        ("tool", "list_documents"),
        ("model", None),
        ("tool", "get_financials"),
        ("model", None),
        ("tool", "calculate"),
        ("model", None),
    ]
    assert steps[3].sources[0]["id"] == "F1" and "38,765,000,000" in steps[3].result_text
    assert steps[-1].args["final"] is True and steps[-1].args["verification"]["cited"]
    assert calls == 4


async def test_ungrounded_number_needs_review(make_org, ingest, settings):
    org = await make_org()
    await _garam_pair(org, ingest)
    script = [
        *DEBT_RATIO_SCRIPT[:2],
        "부채비율은 약 64.61%입니다 [F1][F2].",  # the model divided by itself
    ]
    outcome, _events, _llm = await _run(org, settings, script)
    assert outcome.status == "needs_review"
    assert outcome.review_reasons == ["ungrounded_number"]
    assert outcome.ungrounded_numbers == ["64.61"]


async def test_unknown_source_ids_are_removed_and_counted(make_org, ingest, sessionmaker, settings):
    org = await make_org()
    await _garam_pair(org, ingest)
    script = [*DEBT_RATIO_SCRIPT[:3], "부채비율은 64.6%입니다 [K1][K7][S2]."]
    outcome, events, _llm = await _run(org, settings, script)
    assert outcome.status == "completed"
    assert outcome.answer == "부채비율은 64.6%입니다 [K1]."
    assert outcome.dropped_citations == ["K7", "S2"]
    answer = dict(events)["answer"]
    assert answer["dropped_citations"] == ["K7", "S2"]
    async with sessionmaker() as s:
        finished = await s.scalar(
            select(AuditLog).where(
                AuditLog.target_id == str(outcome.run_id), AuditLog.action == "agent.run.finished"
            )
        )
    assert finished.metadata_["dropped_citations"] == 2


async def test_step_limit_makes_a_last_call_without_tools(make_org, ingest, settings):
    org = await make_org()
    await _garam_pair(org, ingest)
    script = [
        [_call("list_documents")],
        [_call("search_filings", query="리스부채")],
        [_call("search_filings", query="우발부채")],
        "리스부채는 현재가치로 측정합니다 [S1].",
    ]
    outcome, _events, llm = await _run(org, settings, script, agent_max_steps=2)
    assert outcome.step_count == 3 and outcome.tool_call_count == 2
    assert "step_limit" in outcome.review_reasons and outcome.status == "needs_review"
    assert llm.tool_turns[-1]["tools"] == [] and llm.tool_turns[0]["tools"]


async def test_same_call_three_times_stops_with_repeated_call(make_org, ingest, settings):
    org = await make_org()
    await _garam_pair(org, ingest)
    note = _call("read_note", document="D1", note="차입금")
    script = [[note], [note], [note], [note], "단기차입금은 5,000입니다 [N1:L4]."]
    outcome, _events, llm = await _run(org, settings, script)
    assert outcome.review_reasons[0] == "repeated_call" and outcome.status == "needs_review"
    assert outcome.tool_call_count == 2  # the third identical call is not run
    assert llm.tool_turns[-1]["tools"] == []


async def test_max_calls_per_step_and_bad_calls_come_back_as_errors(
    make_org, ingest, sessionmaker, settings
):
    org = await make_org()
    await _garam_pair(org, ingest)
    script = [
        [
            _call("no_such_tool"),
            _call("read_note", document="D1"),
            _call("list_documents"),
            _call("list_documents", extra=1),
        ],
        "답을 찾지 못했습니다.",
    ]
    outcome, events, _llm = await _run(org, settings, script)
    tool_steps = [d for e, d in events if e == "step" and d["kind"] == "tool"]
    assert [s["ok"] for s in tool_steps] == [False, False, True]  # 4th call dropped (max 3)
    assert "알 수 없는 도구" in tool_steps[0]["error"] and "필수 인자" in tool_steps[1]["error"]
    first_model = next(d for e, d in events if e == "step" and d["kind"] == "model")
    assert first_model["dropped_calls"] == 1
    assert outcome.review_reasons == ["no_citations", "tool_errors"]


async def test_timeout_fails_the_run(make_org, ingest, sessionmaker, settings):
    org = await make_org()
    await _garam_pair(org, ingest)
    outcome, events, _llm = await _run(
        org,
        settings,
        DEBT_RATIO_SCRIPT,
        agent_timeout_seconds=0.05,
        llm_kwargs={"agent_delay_seconds": 0.5},
    )
    assert outcome.status == "failed" and outcome.error == "timeout"
    assert events[-1][0] == "error"
    async with sessionmaker() as s:
        run = await s.get(AgentRun, outcome.run_id)
    assert run.status == "failed" and run.error == "timeout" and run.review_reasons == ["timeout"]


async def test_cancelled_run_is_failed_client_disconnected(
    make_org, ingest, sessionmaker, settings
):
    org = await make_org()
    await _garam_pair(org, ingest)
    started = asyncio.Event()

    async def emit(event, data):
        if event == "run":
            started.set()

    task = asyncio.create_task(
        run_agent(
            "질문입니다",
            org_id=org.org_id,
            user_id=org.admin_id,
            settings=settings,
            llm=FakeLLM(agent_script=DEBT_RATIO_SCRIPT, agent_delay_seconds=5),
            embedder=_embedder(settings),
            dart_client=FakeDartClient(),
            emit=emit,
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessionmaker() as s:
        run = await s.scalar(
            select(AgentRun)
            .where(AgentRun.org_id == org.org_id)
            .order_by(AgentRun.created_at.desc())
        )
    assert run.status == "failed" and run.error == "client_disconnected"


async def test_llm_error_fails_the_run(make_org, ingest, settings):
    org = await make_org()

    class Broken(FakeLLM):
        async def chat_tools(self, messages, tools):
            raise LLMError(503, "로컬 LLM 서버(Ollama)에 연결할 수 없습니다")

    outcome = await run_agent(
        "질문입니다",
        org_id=org.org_id,
        user_id=org.admin_id,
        settings=settings,
        llm=Broken(),
        embedder=_embedder(settings),
        dart_client=FakeDartClient(),
    )
    assert outcome.status == "failed" and outcome.error.startswith("llm_error_503")


# ---------------------------------------------------------------- OllamaClient.chat_tools


def _ollama(*replies):
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
        num_ctx=8192,
        max_tokens=512,
        transport=httpx.MockTransport(handler),
    )
    return client, requests


async def test_chat_tools_sends_tools_without_format_and_parses_calls():
    reply = {
        "model": MODEL,
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "list_documents", "arguments": {}}},
                {
                    "function": {
                        "name": "calculate",
                        "arguments": '{"expression": "a", "values": {}}',
                    }
                },
            ],
        },
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 120,
        "eval_count": 30,
    }
    client, requests = _ollama(reply)
    specs = agent_tools.tool_specs()
    messages = [{"role": "user", "content": "질문"}]
    result = await client.chat_tools(messages, specs)
    body = json.loads(requests[0].content)
    assert body["tools"] == specs and "format" not in body
    assert body["messages"] == messages and body["stream"] is False
    assert body["options"] == {"num_ctx": 8192, "num_predict": 512, "temperature": 0.0}
    assert [c.name for c in result.tool_calls] == ["list_documents", "calculate"]
    assert result.tool_calls[1].arguments == {"expression": "a", "values": {}}
    assert result.model_id == "ollama:qwen3:14b" and result.usage.input_tokens == 120
    final = await client.chat_tools(messages, [])
    assert "tools" not in json.loads(requests[1].content) and final.done_reason == "stop"


@pytest.mark.parametrize(
    ("error", "status"),
    [(httpx.ConnectError("refused"), 503), (httpx.ReadTimeout("slow"), 504)],
)
async def test_chat_tools_maps_transport_errors(error, status):
    client, _ = _ollama(error)
    with pytest.raises(LLMError) as exc:
        await client.chat_tools([{"role": "user", "content": "q"}], [])
    assert exc.value.status_code == status


# ---------------------------------------------------------------- API (SSE)


def _events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


async def test_sse_run_then_list_and_detail(client, app, make_org, ingest, sessionmaker):
    org, other = await make_org(), await make_org()
    await _garam_pair(org, ingest)
    app.state.llm = FakeLLM(agent_script=DEBT_RATIO_SCRIPT)
    resp = await client.post(
        "/agent/runs", json={"question": "부채비율은?"}, headers=org.headers("member")
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _events(resp.text)
    kinds = [e for e, _d in events]
    assert kinds[0] == "run" and kinds[-2:] == ["answer", "done"]
    assert set(kinds[1:-2]) == {"step"} and len(kinds) == 10
    run_id = events[0][1]["run_id"]
    step = next(d for e, d in events if e == "step" and d["tool_name"] == "get_financials")
    assert step["ok"] and step["source_ids"] == ["F1", "F2"] and len(step["summary"]) <= 300
    assert step["args_summary"].startswith('get_financials(document="D1"')
    answer = events[-2][1]
    assert answer["status"] == "completed" and answer["review_reasons"] == []
    assert {s["id"] for s in answer["sources"]} >= {"F1", "F2", "K1"}
    assert events[-1][1] == {"run_id": run_id, "status": "completed"}

    listed = await client.get("/agent/runs", headers=org.headers("reviewer"))
    assert listed.status_code == 200 and listed.json()[0]["id"] == run_id
    assert listed.json()[0]["step_count"] == 4
    detail = await client.get(f"/agent/runs/{run_id}", headers=org.headers("member"))
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "completed" and len(body["steps"]) == 7
    assert [c["id"] for c in body["citations"]] == ["K1", "F1", "F2"]
    assert body["steps"][1]["result_text"].startswith("조직의 DART 문서 2개")

    assert (await client.get(f"/agent/runs/{run_id}", headers=other.headers())).status_code == 404
    assert (await client.get("/agent/runs", headers=other.headers())).json() == []
    missing = await client.get(f"/agent/runs/{uuid.uuid4()}", headers=org.headers())
    assert missing.status_code == 404

    async with sessionmaker() as s:
        actions = list(
            await s.scalars(
                select(AuditLog.action)
                .where(AuditLog.target_id == run_id)
                .order_by(AuditLog.created_at)
            )
        )
        finished = await s.scalar(
            select(AuditLog).where(
                AuditLog.target_id == run_id, AuditLog.action == "agent.run.finished"
            )
        )
        calls = list(
            await s.scalars(select(LlmCall).where(LlmCall.agent_run_id == uuid.UUID(run_id)))
        )
    assert actions == ["agent.run.started", "agent.run.finished"]
    meta = finished.metadata_
    assert meta["status"] == "completed" and meta["steps"] == 4 and meta["review_reasons"] == []
    assert meta["tool_counts"] == {"list_documents": 1, "get_financials": 1, "calculate": 1}
    assert len(calls) == 4 and all(c.cost_usd == 0 and c.answer_id is None for c in calls)


async def test_default_fake_llm_demo_policy_answers_from_search(client, make_org, ingest):
    org = await make_org()
    await _garam_pair(org, ingest)
    resp = await client.post(
        "/agent/runs", json={"question": "리스부채는 어떻게 측정하나요?"}, headers=org.headers()
    )
    events = _events(resp.text)
    tools = [d["tool_name"] for e, d in events if e == "step" and d["kind"] == "tool"]
    assert tools == ["list_documents", "search_filings"]
    answer = dict(events)["answer"]
    assert answer["status"] == "completed", answer
    assert answer["citations"][0]["id"].startswith("S")


async def test_empty_question_and_real_provider_are_json_errors(
    client, app, make_org, settings, monkeypatch
):
    class _NoCall:
        class beta:  # noqa: N801 - mimics the SDK attribute path
            class messages:  # noqa: N801
                @staticmethod
                async def create(**kwargs):
                    raise AssertionError("Claude must not be called")

    org = await make_org()
    empty = await client.post("/agent/runs", json={"question": "   "}, headers=org.headers())
    assert empty.status_code == 422 and empty.headers["content-type"].startswith("application/json")
    monkeypatch.setattr(settings, "llm_provider", "real")
    app.state.llm = ClaudeClient(api_key="unused", client=_NoCall())
    resp = await client.post("/agent/runs", json={"question": "부채비율은?"}, headers=org.headers())
    assert resp.status_code == 501
    assert resp.json()["detail"] == "에이전트는 현재 로컬 LLM에서만 지원합니다"
    config = await client.get("/agent/config", headers=org.headers("member"))
    assert config.status_code == 200 and config.json()["supported"] is False
    assert (await client.post("/agent/runs", json={"question": "부채비율은?"})).status_code == 401


async def test_agent_config_lists_model_limits_and_tools(client, make_org):
    org = await make_org()
    resp = await client.get("/agent/config", headers=org.headers("member"))
    body = resp.json()
    assert body["supported"] is True and body["model"] == "fake-llm"
    assert body["max_steps"] == 6 and body["max_calls_per_step"] == 3
    assert [t["name"] for t in body["tools"]][-1] == "calculate"
    assert body["reason_labels"]["step_limit"] == "단계 상한 도달"


def test_openapi_lists_agent_paths(app):
    from fastapi.testclient import TestClient  # noqa: F401 - schema only

    spec = app.openapi()
    assert {"/agent/runs", "/agent/runs/{run_id}", "/agent/config"} <= set(spec["paths"])


# ---------------------------------------------------------------- migration


async def test_migration_0005_downgrade_and_upgrade(sessionmaker):
    from tests.test_embedding_rate_limit_cache import _alembic

    await asyncio.to_thread(_alembic, "downgrade", "0004_note_comparisons")
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
    assert "agent_runs" not in tables and "agent_steps" not in tables
    assert "agent_run_id" not in columns and "note_comparison_id" in columns
    await asyncio.to_thread(_alembic, "upgrade", "head")
    assert "0005_agent_runs (head)" in await asyncio.to_thread(_alembic, "current")
    async with sessionmaker() as s:
        tables = set(
            await s.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        )
        nullable = await s.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'llm_calls' AND column_name = 'agent_run_id'"
            )
        )
        cascade = await s.scalar(
            text(
                "SELECT confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'agent_steps'::regclass AND contype = 'f'"
            )
        )
    assert {"agent_runs", "agent_steps"} <= tables
    assert nullable == "YES" and cascade == "c"
