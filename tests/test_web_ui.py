"""웹 화면 서빙(/ui, 보안 헤더, / 리다이렉트), GET /dart/reports, cli dart-find 출력 형식."""

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from footnote_rag.dart.client import DartError, FakeDartClient
from footnote_rag.db.models import AuditLog
from footnote_rag.providers import MissingKeyError

EXPECTED_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
PARAMS = {"stock_codes": "990001,990003", "date_from": "20260101", "date_to": "20260531"}


# ---- /ui static serving ----


async def test_ui_index_is_served_with_security_headers(client):
    resp = await client.get("/ui/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-security-policy"] == EXPECTED_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert '<script src="/ui/app.js" defer></script>' in resp.text
    assert 'id="login-form"' in resp.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/ui/app.js", "javascript"), ("/ui/style.css", "text/css")],
)
async def test_ui_assets_are_served(client, path, content_type):
    resp = await client.get(path)
    assert resp.status_code == 200
    assert content_type in resp.headers["content-type"]
    assert resp.headers["content-security-policy"] == EXPECTED_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_ui_index_has_note_diff_tab(client):
    resp = await client.get("/ui/")
    assert resp.status_code == 200
    assert 'id="tab-diff"' in resp.text and 'id="panel-diff"' in resp.text
    assert "변동 분석" in resp.text


async def test_fake_dart_find_lists_the_prior_annual_report(
    capsys, settings, monkeypatch, tmp_path
):
    from footnote_rag import cli

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    argv = ["dart-find", "--stock-codes", "990001", "--from", "20250101", "--to", "20260531"]
    assert await cli.cmd_dart_find(cli.build_parser().parse_args(argv)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "가람전자(합성)\t990001\t사업보고서 (2024.12)\t20250310900101\t20250310" in lines
    assert "가람전자(합성)\t990001\t사업보고서 (2025.12)\t20260310900101\t20260310" in lines


async def test_root_redirects_to_ui(client):
    resp = await client.get("/")
    assert resp.status_code == 307
    assert resp.headers["location"] == "/ui/"


async def test_ui_without_trailing_slash_redirects(client):
    resp = await client.get("/ui")
    assert resp.status_code in (307, 308)
    assert resp.headers["location"].endswith("/ui/")


async def test_swagger_docs_keep_working_without_ui_csp(client):
    resp = await client.get("/docs")
    assert resp.status_code == 200
    assert "content-security-policy" not in resp.headers
    assert (await client.get("/openapi.json")).status_code == 200


async def test_api_responses_do_not_get_ui_csp(client):
    resp = await client.get("/healthz")
    assert "content-security-policy" not in resp.headers


# ---- GET /dart/reports ----


async def test_dart_reports_requires_token(client):
    resp = await client.get("/dart/reports", params=PARAMS)
    assert resp.status_code == 401


async def test_dart_reports_fake_mode_returns_synthetic_reports(client, make_org, sessionmaker):
    org = await make_org()
    resp = await client.get(
        "/dart/reports",
        params={**PARAMS, "stock_codes": "990001, 990003,123456", "detail": "F001"},
        headers=org.headers("member"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "fake"
    assert body["not_found"] == ["123456"]
    assert {r["rcept_no"] for r in body["reports"]} == {"20260310900001", "20260310900003"}
    first = next(r for r in body["reports"] if r["rcept_no"] == "20260310900001")
    assert first == {
        "corp_name": "가람전자(합성)",
        "stock_code": "990001",
        "report_nm": "감사보고서 (2025.12)",
        "rcept_no": "20260310900001",
        "rcept_dt": "20260310",
    }

    async with sessionmaker() as s:
        entry = await s.scalar(
            select(AuditLog).where(AuditLog.org_id == org.org_id, AuditLog.action == "dart.search")
        )
    assert entry is not None and entry.actor_user_id is not None
    assert entry.metadata_["stock_codes"] == ["990001", "990003", "123456"]
    assert entry.metadata_["date_from"] == "20260101"
    assert entry.metadata_["date_to"] == "20260531"
    assert entry.metadata_["result_count"] == 2


async def test_dart_reports_default_detail_is_a001_annual_report(client, make_org, sessionmaker):
    org = await make_org()
    resp = await client.get("/dart/reports", params=PARAMS, headers=org.headers("member"))
    assert resp.status_code == 200, resp.text
    reports = resp.json()["reports"]
    assert len(reports) >= 1
    assert {r["report_nm"] for r in reports} == {"사업보고서 (2025.12)"}
    assert {r["stock_code"] for r in reports} == {"990001", "990003"}
    async with sessionmaker() as s:
        entry = await s.scalar(
            select(AuditLog).where(AuditLog.org_id == org.org_id, AuditLog.action == "dart.search")
        )
    assert entry.metadata_["detail"] == "A001"


async def test_dart_reports_detail_a001_explicit(client, make_org):
    org = await make_org()
    resp = await client.get(
        "/dart/reports", params={**PARAMS, "detail": "A001"}, headers=org.headers("member")
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["reports"]) >= 1


async def test_dart_reports_detail_f002_and_date_range(client, make_org):
    org = await make_org()
    resp = await client.get(
        "/dart/reports",
        params={**PARAMS, "date_from": "20260401", "detail": "F002"},
        headers=org.headers("reviewer"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reports"] == []  # synthetic filings are dated 20260310


@pytest.mark.parametrize(
    "override",
    [
        {"stock_codes": "12345"},
        {"stock_codes": "abcdef"},
        {"stock_codes": ""},
        {"stock_codes": " , "},
        {"stock_codes": ",".join(f"{i:06d}" for i in range(11))},
        {"date_from": "2026-01-01"},
        {"date_from": "20261301"},
        {"date_to": "2026053"},
        {"date_from": "20260601", "date_to": "20260101"},
        {"detail": "F003"},
        {"detail": "A002"},
        {"detail": "a001"},
        {"detail": ""},
    ],
)
async def test_dart_reports_invalid_input_is_422(client, make_org, override):
    org = await make_org()
    resp = await client.get(
        "/dart/reports", params={**PARAMS, **override}, headers=org.headers("member")
    )
    assert resp.status_code == 422, resp.text


async def test_dart_reports_missing_params_is_422(client, make_org):
    org = await make_org()
    resp = await client.get(
        "/dart/reports", params={"stock_codes": "990001"}, headers=org.headers("member")
    )
    assert resp.status_code == 422


class _FailingDart(FakeDartClient):
    """corpCode lookups work (synthetic data); the list.json call fails."""

    def __init__(self, error: DartError) -> None:
        super().__init__()
        self._error = error

    async def list_reports(self, **kwargs):
        raise self._error


@pytest.mark.parametrize(
    ("code", "retryable", "expected"), [("020", True, 503), ("010", False, 502)]
)
async def test_dart_errors_map_to_503_or_502_without_secrets(
    client, make_org, monkeypatch, settings, sessionmaker, code, retryable, expected
):
    from footnote_rag.dart import service

    secret = "SECRET-OPENDART-KEY-0123456789"
    monkeypatch.setattr(settings, "opendart_api_key", SecretStr(secret))
    leaky = f"GET https://opendart.fss.or.kr/api/list.json?crtfc_key={secret} failed"
    monkeypatch.setattr(
        service,
        "make_dart_client",
        lambda s=None: _FailingDart(DartError(code, leaky, retryable=retryable)),
    )
    org = await make_org()
    resp = await client.get("/dart/reports", params=PARAMS, headers=org.headers("member"))
    assert resp.status_code == expected
    detail = resp.json()["detail"]
    assert code in detail
    for forbidden in (secret, "crtfc_key", "opendart.fss.or.kr", "list.json"):
        assert forbidden not in resp.text
    async with sessionmaker() as s:
        entry = await s.scalar(
            select(AuditLog).where(AuditLog.org_id == org.org_id, AuditLog.action == "dart.search")
        )
    assert entry is None  # failed searches are not recorded as searches


async def test_missing_dart_key_is_503(client, make_org, monkeypatch):
    from footnote_rag.dart import service

    def _raise(s=None):
        raise MissingKeyError("OPENDART_API_KEY is required when the provider is 'real'")

    monkeypatch.setattr(service, "make_dart_client", _raise)
    org = await make_org()
    resp = await client.get("/dart/reports", params=PARAMS, headers=org.headers("member"))
    assert resp.status_code == 503
    assert "OPENDART_API_KEY" not in resp.text


async def test_find_reports_service_returns_reports_not_found_and_mode(
    settings, monkeypatch, tmp_path
):
    from footnote_rag.dart.service import find_reports

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    reports, not_found, mode = await find_reports(
        settings, ["990002", "999999"], "20260101", "20260531", "F001"
    )
    assert mode == "fake"
    assert not_found == ["999999"]
    assert [r["rcept_no"] for r in reports] == ["20260310900002"]
    assert reports[0]["stock_code"] == "990002"
    assert reports[0]["corp_name"] == "나래포털(합성)"


# ---- CLI output format ----


async def test_cli_dart_find_default_detail_is_a001_with_same_format(
    capsys, settings, monkeypatch, tmp_path
):
    from footnote_rag import cli

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    argv = ["dart-find", "--stock-codes", "990001,000000,990003"]
    args = cli.build_parser().parse_args([*argv, "--from", "20260101", "--to", "20260531"])
    assert args.detail == "A001"
    assert await cli.cmd_dart_find(args) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "corp_name	stock_code	report_nm	rcept_no	rcept_dt",
        "가람전자(합성)	990001	사업보고서 (2025.12)	20260310900101	20260310",
        "다솜모터스(합성)	990003	사업보고서 (2025.12)	20260310900103	20260310",
    ]
    comments = [line for line in captured.err.splitlines() if line.startswith("# ")]
    assert comments == [
        "# OpenDART mode: fake",
        "# stock_code 000000 not found in corpCode list",
        "# 2 report(s) (pblntf_detail_ty=A001)",
    ]


def test_cli_dart_find_rejects_unknown_detail():
    from footnote_rag import cli

    argv = ["dart-find", "--stock-codes", "990001", "--from", "20260101", "--to", "20260531"]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([*argv, "--detail", "F003"])


async def test_cli_dart_find_output_format_is_unchanged(capsys, settings, monkeypatch, tmp_path):
    from footnote_rag import cli

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    argv = ["dart-find", "--stock-codes", "990001,000000,990003", "--detail", "F001"]
    args = cli.build_parser().parse_args([*argv, "--from", "20260101", "--to", "20260531"])
    assert await cli.cmd_dart_find(args) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "corp_name\tstock_code\treport_nm\trcept_no\trcept_dt",
        "가람전자(합성)\t990001\t감사보고서 (2025.12)\t20260310900001\t20260310",
        "다솜모터스(합성)\t990003\t감사보고서 (2025.12)\t20260310900003\t20260310",
    ]
    comments = [line for line in captured.err.splitlines() if line.startswith("# ")]
    assert comments == [
        "# OpenDART mode: fake",
        "# stock_code 000000 not found in corpCode list",
        "# 2 report(s) (pblntf_detail_ty=F001)",
    ]
