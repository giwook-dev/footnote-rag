"""Eval runner/validator on a synthetic dataset, CLI commands, MCP tools."""

import json
from pathlib import Path

import httpx
import pytest

from footnote_rag.dart import fake_data
from footnote_rag.evals.dataset import load_dataset
from footnote_rag.evals.run import render_report, run_eval, write_report
from footnote_rag.evals.validate import validate

LEASE = "(첨부)재무제표 > 주석 > 12. 리스"


def _write_dataset(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return path


SYNTHETIC_ROWS = [
    {
        "id": "syn-1",
        "rcept_no": "20260310900001",
        "corp_name": "가람전자(합성)",
        "question": "리스부채는 어떻게 측정하나요?",
        "expected_section_prefixes": [LEASE],
        "answerable": True,
        "evidence_quote": "회사는 리스부채를 리스개시일 현재 지급되지 않은 리스료의 현재가치로 측정합니다.",
    },
    {
        "id": "syn-2",
        "rcept_no": ["20260310900001", "20260310900002"],
        "corp_name": ["가람전자(합성)", "나래포털(합성)"],
        "question": "두 회사의 소송 현황을 비교해 주세요.",
        "expected_section_prefixes": ["(첨부)재무제표 > 주석 > 20. 우발부채와 약정사항"],
        "answerable": True,
        "evidence_quote": [
            "보고기간말 현재 회사가 피소된 소송사건은 3건이며 소송가액은 1,200백만원입니다."
        ],
    },
    {
        "id": "syn-3",
        "rcept_no": "20260310900002",
        "corp_name": "나래포털(합성)",
        "question": "회사의 2030년 매출 목표는 얼마인가요?",
        "expected_section_prefixes": [],
        "answerable": False,
        "evidence_quote": None,
    },
    {"_example": True, "id": "ex-1", "rcept_no": "<rcept_no>", "question": "예시"},
]


@pytest.fixture
def zip_dir(tmp_path):
    d = tmp_path / "dart"
    d.mkdir()
    for r in ("20260310900001", "20260310900002"):
        (d / f"{r}.zip").write_bytes(fake_data.build_document_zip(r))
    return d


def test_validate_passes_for_quotes_present_in_parsed_text(tmp_path, zip_dir):
    items = load_dataset(_write_dataset(tmp_path / "ds.jsonl", SYNTHETIC_ROWS))
    report = validate(items, zip_dir, check_coverage=False)
    assert report.ok, report.errors
    assert report.checked == 3 and report.skipped_examples == 1


def test_validate_fails_for_invented_quote_wrong_section_and_missing_zip(tmp_path, zip_dir):
    bad = [
        {**SYNTHETIC_ROWS[0], "id": "invented", "evidence_quote": "원문에 없는 문장입니다."},
        {
            **SYNTHETIC_ROWS[0],
            "id": "wrong-section",
            "expected_section_prefixes": ["독립된 감사인의 감사보고서"],
        },
        {**SYNTHETIC_ROWS[0], "id": "no-zip", "rcept_no": "20990101000000"},
    ]
    report = validate(
        load_dataset(_write_dataset(tmp_path / "bad.jsonl", bad)), zip_dir, check_coverage=False
    )
    joined = "\n".join(report.errors)
    assert not report.ok
    assert "invented" in joined and "wrong-section" in joined and "no-zip" in joined


def test_validate_coverage_rules(tmp_path, zip_dir):
    report = validate(load_dataset(_write_dataset(tmp_path / "ds.jsonl", SYNTHETIC_ROWS)), zip_dir)
    assert any(">= 15 rows" in e for e in report.errors)
    assert any("삼성전자" in e for e in report.errors)


def test_example_only_dataset_validates_trivially(tmp_path, zip_dir):
    from footnote_rag.evals.validate import main

    path = _write_dataset(tmp_path / "ex.jsonl", [SYNTHETIC_ROWS[-1]])
    assert main(["--dataset", str(path), "--zip-dir", str(zip_dir)]) == 0


REPO_ROOT = Path(__file__).parent.parent


def test_repository_dataset_is_valid(tmp_path):
    """evals/dataset.jsonl must pass the validator when the filings are cached in data/dart.

    CI has no filings (they are never committed): then the only allowed errors are "missing"
    ZIPs - coverage rules and row checks that do not need the text must still pass."""
    items = load_dataset(REPO_ROOT / "evals" / "dataset.jsonl")
    report = validate(items, REPO_ROOT / "data" / "dart")
    assert report.ok or all("missing" in e for e in report.errors), report.errors
    without_filings = validate(items, tmp_path)  # what CI sees, even when data/dart exists
    assert without_filings.errors and all("missing" in e for e in without_filings.errors)


def test_repository_dataset_composition():
    import re

    items = load_dataset(REPO_ROOT / "evals" / "dataset.jsonl")
    assert not any(i.example for i in items)
    assert len(items) >= 18
    assert len({i.id for i in items}) == len(items)
    assert all(re.fullmatch(r"(samsung|naver|hyundai|compare|unans)-\d{2}", i.id) for i in items)
    single = [i for i in items if i.answerable and not i.is_comparison]
    for company in ("삼성전자", "NAVER", "현대자동차"):
        assert sum(1 for i in single if i.corp_names == [company]) >= 4, company
    comparisons = [i for i in items if i.answerable and i.is_comparison]
    assert len(comparisons) >= 2
    assert all(i.id.startswith("compare-") for i in comparisons)
    assert all(len(i.rcept_nos) == len(i.corp_names) >= 2 for i in comparisons)
    unanswerable = [i for i in items if not i.answerable]
    assert len(unanswerable) >= 3
    assert all(i.id.startswith("unans-") and not i.evidence_quotes for i in unanswerable)
    for item in items:
        if item.answerable:
            assert item.expected_section_prefixes, item.id
            assert all(20 <= len(q) <= 150 for q in item.evidence_quotes), item.id


async def test_eval_runner_fake_mode_writes_markdown_report(
    tmp_path, settings, fake_embedder, fake_llm, fake_dart
):
    items = load_dataset(_write_dataset(tmp_path / "ds.jsonl", SYNTHETIC_ROWS))
    summary = await run_eval(
        items,
        settings=settings,
        embedder=fake_embedder,
        llm=fake_llm,
        dart_client=fake_dart,
        mode="fake",
        dataset_name="synthetic",
    )
    assert summary.skipped_examples == 1 and len(summary.results) == 3
    assert not summary.import_failures
    assert summary.recall_at_k is not None and 0.0 <= summary.recall_at_k <= 1.0
    assert summary.citation_accuracy is not None
    assert summary.abstention_rate is not None
    report_path = write_report(summary, tmp_path / "reports")
    md = report_path.read_text(encoding="utf-8")
    assert report_path.suffix == ".md"
    for needle in (
        "recall@",
        "citation accuracy",
        "unanswerable correctly abstained",
        "avg latency",
        "syn-2",
    ):
        assert needle in md
    assert "Fake mode" in render_report(summary)


# ---- CLI ----


async def test_cli_dart_find_prints_reports(capsys, settings, monkeypatch, tmp_path):
    from footnote_rag import cli

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    args = cli.build_parser().parse_args(
        [
            "dart-find",
            "--stock-codes",
            "990001,990003",
            "--from",
            "20260101",
            "--to",
            "20260531",
            "--detail",
            "F001",
        ]
    )
    assert await cli.cmd_dart_find(args) == 0
    out = capsys.readouterr().out
    assert "20260310900001" in out and "20260310900003" in out
    assert "가람전자(합성)" in out and "감사보고서 (2025.12)" in out


async def test_cli_dart_fetch_prints_sanitized_counts(capsys, settings, monkeypatch, tmp_path):
    from footnote_rag import cli

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    args = cli.build_parser().parse_args(["dart-fetch", "--rcept-no", "20260310900001"])
    assert await cli.cmd_dart_fetch(args) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("== 20260310900001: ")
    assert first.endswith("sections) (sanitized &:0 <:0 attr:0 tag:0)")


async def test_cli_dart_import_uses_the_service(capsys, make_org, sessionmaker, monkeypatch):
    from sqlalchemy import select

    from footnote_rag import cli
    from footnote_rag.db import session as db_session
    from footnote_rag.db.models import AuditLog, Document
    from footnote_rag.documents import service

    org = await make_org()
    calls = []
    original = service.request_dart_import

    async def spy(session, *, user, rcept_no):
        calls.append(rcept_no)
        return await original(session, user=user, rcept_no=rcept_no)

    monkeypatch.setattr(service, "request_dart_import", spy)

    async def no_dispose():  # keep the shared test engine alive
        return None

    monkeypatch.setattr(db_session, "dispose_engine", no_dispose)
    args = cli.build_parser().parse_args(
        [
            "dart-import",
            "--org-admin-email",
            org.emails["admin"],
            "--rcept-no",
            "20260310900001,bad",
        ]
    )
    assert await cli.cmd_dart_import(args) == 1  # "bad" is rejected
    assert calls == ["20260310900001", "bad"]
    assert "queued" in capsys.readouterr().out
    async with sessionmaker() as s:
        doc = await s.scalar(select(Document).where(Document.org_id == org.org_id))
        audit = await s.scalar(
            select(AuditLog).where(
                AuditLog.org_id == org.org_id, AuditLog.action == "document.dart_import_requested"
            )
        )
    assert doc.source_ref == "20260310900001" and audit is not None


# ---- MCP ----


async def test_mcp_tools_call_http_api_with_token(monkeypatch):
    from footnote_rag import mcp_server

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.url.path, request.headers["authorization"], json.loads(request.content))
        )
        if request.url.path == "/search":
            return httpx.Response(200, json=[{"chunk_id": "c"}])
        return httpx.Response(200, json={"status": "published", "text": "답[1]"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("FOOTNOTE_API_TOKEN", "tok123")
    monkeypatch.setenv("FOOTNOTE_API_URL", "http://api.test")
    assert await mcp_server._post("/search", {"query": "q", "k": 3}, transport) == [
        {"chunk_id": "c"}
    ]
    assert (await mcp_server._post("/questions", {"question": "q"}, transport))[
        "status"
    ] == "published"
    assert seen[0] == ("/search", "Bearer tok123", {"query": "q", "k": 3})

    denied = httpx.MockTransport(lambda r: httpx.Response(403, json={"detail": "Forbidden"}))
    with pytest.raises(mcp_server.ApiError, match="403"):
        await mcp_server._post("/search", {"query": "q"}, denied)

    tools = {t.name for t in await mcp_server.mcp.list_tools()}
    assert tools == {"search_documents", "ask"}


async def test_mcp_requires_token(monkeypatch):
    from footnote_rag import mcp_server

    monkeypatch.delenv("FOOTNOTE_API_TOKEN", raising=False)
    with pytest.raises(mcp_server.ApiError, match="FOOTNOTE_API_TOKEN"):
        await mcp_server._post("/search", {"query": "q"})
