"""OpenDART client: status classification, error bodies, URL masking.

Uses httpx.MockTransport - no network access.
"""

import json

import httpx
import pytest
from structlog.testing import capture_logs

from footnote_rag.dart import fake_data
from footnote_rag.dart.client import (
    DartError,
    FakeDartClient,
    HttpDartClient,
    classify_status,
    parse_error_body,
)
from footnote_rag.dart.corp_codes import CorpCodeCache, parse_corp_code_zip
from footnote_rag.observability import mask_url

SECRET = "TESTKEY-not-a-real-key-1234567890"


@pytest.mark.parametrize("code", ["020", "800", "900"])
def test_retryable_status_codes(code):
    assert classify_status(code).retryable is True


@pytest.mark.parametrize("code", ["010", "011", "012", "013", "014", "901"])
def test_non_retryable_status_codes(code):
    err = classify_status(code)
    assert err.retryable is False
    assert err.code == code


def test_error_body_json_and_xml():
    err = parse_error_body(json.dumps({"status": "020", "message": "limit"}).encode())
    assert (err.code, err.retryable) == ("020", True)
    xml = "<result><status>014</status><message>파일이 존재하지 않습니다</message></result>"
    err = parse_error_body(xml.encode("utf-8"))
    assert (err.code, err.retryable) == ("014", False)
    assert parse_error_body(b"garbage").retryable is True


def test_mask_url():
    url = f"https://opendart.fss.or.kr/api/document.xml?crtfc_key={SECRET}&rcept_no=1"
    masked = mask_url(url)
    assert SECRET not in masked
    assert "crtfc_key=***" in masked and "rcept_no=1" in masked


def _client(handler) -> HttpDartClient:
    return HttpDartClient(SECRET, transport=httpx.MockTransport(handler))


async def test_logged_request_url_masks_crtfc_key():
    zip_bytes = fake_data.build_document_zip("20260310900001")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["crtfc_key"] == SECRET  # sent to the API...
        return httpx.Response(200, content=zip_bytes)

    with capture_logs() as logs:
        content = await _client(handler).download_document("20260310900001")
    assert content == zip_bytes
    urls = [e["url"] for e in logs if "url" in e]
    assert urls, "request URL should be logged"
    for entry in logs:
        assert SECRET not in json.dumps(entry, ensure_ascii=False)  # ...but never logged
    assert all("crtfc_key=***" in u for u in urls)


async def test_zip_endpoint_returning_error_document():
    def handler(request):
        return httpx.Response(200, json={"status": "013", "message": "조회된 데이터가 없습니다"})

    with pytest.raises(DartError) as exc:
        await _client(handler).download_document("20260310900001")
    assert exc.value.code == "013" and exc.value.retryable is False


async def test_http_and_network_failures_are_classified():
    with pytest.raises(DartError) as exc:
        await _client(lambda r: httpx.Response(503)).download_document("1")
    assert exc.value.retryable is True

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(DartError) as exc:
        await _client(boom).download_document("1")
    assert exc.value.retryable is True and exc.value.code == "network"
    assert SECRET not in str(exc.value)


async def test_list_reports_paginates_and_handles_no_data():
    pages = {
        "1": {"status": "000", "total_page": 2, "list": [_item("20260310000001")]},
        "2": {"status": "000", "total_page": 2, "list": [_item("20260310000002")]},
    }

    def handler(request):
        params = request.url.params
        assert params["pblntf_ty"] == "F" and params["pblntf_detail_ty"] == "F001"
        assert params["last_reprt_at"] == "Y"
        return httpx.Response(200, json=pages[params["page_no"]])

    reports = await _client(handler).list_reports(
        corp_code="00000001", bgn_de="20260101", end_de="20260531", detail="F001"
    )
    assert [r.rcept_no for r in reports] == ["20260310000001", "20260310000002"]

    empty = await _client(lambda r: httpx.Response(200, json={"status": "013"})).list_reports(
        corp_code="x", bgn_de="1", end_de="2"
    )
    assert empty == []

    with pytest.raises(DartError) as exc:
        await _client(lambda r: httpx.Response(200, json={"status": "020"})).list_reports(
            corp_code="x", bgn_de="1", end_de="2"
        )
    assert exc.value.retryable is True


@pytest.mark.parametrize(
    ("detail", "pblntf_ty"), [(None, "A"), ("A001", "A"), ("F001", "F"), ("F002", "F")]
)
async def test_list_reports_pblntf_ty_follows_detail(detail, pblntf_ty):
    seen: list[dict[str, str]] = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"status": "000", "total_page": 1, "list": []})

    kwargs = {} if detail is None else {"detail": detail}
    await _client(handler).list_reports(
        corp_code="00126380", bgn_de="20260101", end_de="20260531", **kwargs
    )
    assert len(seen) == 1
    assert seen[0]["pblntf_ty"] == pblntf_ty
    assert seen[0]["pblntf_detail_ty"] == (detail or "A001")  # A001 is the default


async def test_list_reports_rejects_unknown_detail_without_a_request():
    def handler(request):
        raise AssertionError("no request expected")

    with pytest.raises(ValueError):
        await _client(handler).list_reports(
            corp_code="x", bgn_de="20260101", end_de="20260531", detail="F003"
        )


async def test_fake_client_returns_annual_reports_by_default():
    fake = FakeDartClient()
    corp = fake_data.FAKE_CORPS[0]["corp_code"]
    annual = await fake.list_reports(corp_code=corp, bgn_de="20260101", end_de="20260531")
    audit = await fake.list_reports(
        corp_code=corp, bgn_de="20260101", end_de="20260531", detail="F001"
    )
    assert [r.report_nm for r in annual] == ["사업보고서 (2025.12)"]
    assert [r.report_nm for r in audit] == ["감사보고서 (2025.12)"]
    assert annual[0].rcept_no != audit[0].rcept_no


def _item(rcept_no: str) -> dict:
    return {
        "corp_name": "합성",
        "corp_code": "00000001",
        "stock_code": "000001",
        "report_nm": "감사보고서 (2025.12)",
        "rcept_no": rcept_no,
        "flr_nm": "합성회계법인",
        "rcept_dt": rcept_no[:8],
    }


async def test_corp_code_cache(tmp_path):
    corps = parse_corp_code_zip(fake_data.build_corp_code_zip())
    assert {c.stock_code for c in corps} == {"990001", "990002", "990003"}  # unlisted dropped
    cache = CorpCodeCache(FakeDartClient(), tmp_path / "corp_codes.json", ttl_days=7)
    info = await cache.by_stock_code("990002")
    assert info.corp_code == "99000002"
    assert (tmp_path / "corp_codes.json").exists()
    with pytest.raises(KeyError):
        await cache.by_stock_code("123456")
