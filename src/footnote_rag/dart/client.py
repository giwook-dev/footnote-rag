"""OpenDART API 클라이언트(Protocol 뒤의 HTTP 실제 구현과 합성 데이터 Fake).

역할: ``list.json``(공시 목록, 페이지 반복. ``pblntf_ty``는 종류 코드의 첫 글자: A001→A, F001/F002→F), ``document.xml``(원문 ZIP), ``corpCode.xml``(고유번호 ZIP),
``fnlttSinglAcntAll.json``(단일회사 전체 재무제표, 에이전트 ``get_financials`` 도구용, ``data/dart/fnltt/`` 캐시)을 호출하고,
OpenDART ``status`` 코드와 HTTP 오류를 재시도 가능/불가 ``DartError``로 분류한다. 로그 URL의 ``crtfc_key``는 가린다.
주요 요소: ``DartClient``, ``HttpDartClient``, ``FakeDartClient``, ``classify_status``, ``parse_error_body``,
``REPORT_DETAILS``(A001 사업보고서 기본, F001, F002), ``pblntf_ty_for``.
호출 관계: ``providers.make_dart_client``가 만들고 ``jobs.handlers``, ``dart.corp_codes``, ``cli``가 쓴다.
학습 문서: docs/study/07-dart-integration.md

OpenDART API client.

``DartClient`` is a Protocol so the worker and CLI can run against ``FakeDartClient``
(synthetic data, no network) or ``HttpDartClient`` (real OpenDART).

Status codes (OpenDART developer guide):
  000 OK
  010 unregistered key, 011 unusable key, 012 IP not allowed, 901 key expired  -> not retryable
  013 no data, 014 file does not exist                                         -> not retryable
  020 request limit exceeded, 800 maintenance, 900 undefined error             -> retryable
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from lxml import etree

from footnote_rag.observability import get_logger, mask_url

log = get_logger(__name__)

RETRYABLE_CODES = frozenset({"020", "800", "900"})

# pblntf_detail_ty codes this service searches. Listed companies file their audited financial
# statements and notes inside the annual report (A001); F001/F002 are mostly non-listed filers.
REPORT_DETAILS: dict[str, str] = {
    "A001": "사업보고서",
    "F001": "감사보고서",
    "F002": "연결감사보고서",
}
DEFAULT_DETAIL = "A001"


def pblntf_ty_for(detail: str) -> str:
    """``pblntf_ty`` is the first letter of the detail code: A001 -> A, F001/F002 -> F."""
    if detail not in REPORT_DETAILS:
        raise ValueError(f"unsupported pblntf_detail_ty: {detail}")
    return detail[0]


NON_RETRYABLE_CODES = frozenset({"010", "011", "012", "013", "014", "901"})


class DartError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"OpenDART error {code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


def classify_status(code: str, message: str = "") -> DartError:
    """Map an OpenDART ``status`` value to a DartError with the right retry policy."""
    if code in RETRYABLE_CODES:
        return DartError(code, message or "temporary OpenDART failure", retryable=True)
    if code in NON_RETRYABLE_CODES:
        return DartError(code, message or "OpenDART request rejected", retryable=False)
    # Unknown codes: be conservative and do not hammer the API.
    return DartError(code, message or "unknown OpenDART status", retryable=False)


@dataclass(frozen=True)
class DartReport:
    corp_name: str
    corp_code: str
    stock_code: str
    report_nm: str
    rcept_no: str
    flr_nm: str
    rcept_dt: str

    def as_dict(self) -> dict[str, str]:
        return {
            "corp_name": self.corp_name,
            "corp_code": self.corp_code,
            "stock_code": self.stock_code,
            "report_nm": self.report_nm,
            "rcept_no": self.rcept_no,
            "flr_nm": self.flr_nm,
            "rcept_dt": self.rcept_dt,
        }


class DartClient(Protocol):
    async def list_reports(
        self, *, corp_code: str, bgn_de: str, end_de: str, detail: str = DEFAULT_DETAIL
    ) -> list[DartReport]: ...

    async def download_document(self, rcept_no: str) -> bytes: ...

    async def download_corp_codes(self) -> bytes: ...

    async def financial_statements(
        self, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str
    ) -> list[dict[str, Any]]: ...


# fnlttSinglAcntAll.json (단일회사 전체 재무제표) report codes and statement kinds.
REPORT_CODES: dict[str, str] = {
    "11011": "사업보고서",
    "11012": "반기보고서",
    "11013": "1분기보고서",
    "11014": "3분기보고서",
}
FS_DIVS: dict[str, str] = {"CFS": "연결재무제표", "OFS": "재무제표(별도)"}
STATEMENT_DIVS: dict[str, str] = {
    "BS": "재무상태표",
    "IS": "손익계산서",
    "CIS": "포괄손익계산서",
    "CF": "현금흐름표",
    "SCE": "자본변동표",
}


def _read_json_list(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _write_json_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.part")
    tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _is_zip(content: bytes) -> bool:
    return content[:2] == b"PK"


def parse_error_body(content: bytes) -> DartError:
    """OpenDART sometimes answers a ZIP endpoint with a JSON or XML error document."""
    text = content.strip()
    if text.startswith(b"{"):
        try:
            data = json.loads(text.decode("utf-8", errors="replace"))
            return classify_status(str(data.get("status", "")), str(data.get("message", "")))
        except json.JSONDecodeError:
            pass
    if text.startswith(b"<"):
        try:
            root = etree.fromstring(text, parser=etree.XMLParser(recover=True))
            if root is not None:
                code = (root.findtext(".//status") or "").strip()
                msg = (root.findtext(".//message") or "").strip()
                if code:
                    return classify_status(code, msg)
        except etree.XMLSyntaxError:
            pass
    return DartError("invalid_response", "expected a ZIP payload", retryable=True)


class HttpDartClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://opendart.fss.or.kr/api",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        fnltt_cache_dir: Path | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        # data/dart/fnltt: financial statement responses (status 000) are cached here.
        self._fnltt_cache_dir = fnltt_cache_dir

    async def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        query = {"crtfc_key": self._key, **params}
        url = f"{self._base}/{path}"
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            request = client.build_request("GET", url, params=query)
            safe_url = mask_url(str(request.url))
            log.info("dart.request", url=safe_url)
            try:
                response = await client.send(request)
            except httpx.TransportError as exc:
                log.warning("dart.network_error", url=safe_url, error=type(exc).__name__)
                raise DartError("network", type(exc).__name__, retryable=True) from None
        log.info("dart.response", url=safe_url, status_code=response.status_code)
        if response.status_code >= 500 or response.status_code == 429:
            raise DartError(str(response.status_code), "OpenDART HTTP error", retryable=True)
        if response.status_code >= 400:
            raise DartError(str(response.status_code), "OpenDART HTTP error", retryable=False)
        return response

    async def list_reports(
        self, *, corp_code: str, bgn_de: str, end_de: str, detail: str = DEFAULT_DETAIL
    ) -> list[DartReport]:
        reports: list[DartReport] = []
        page_no = 1
        while True:
            resp = await self._get(
                "list.json",
                {
                    "corp_code": corp_code,
                    "bgn_de": bgn_de,
                    "end_de": end_de,
                    "pblntf_ty": pblntf_ty_for(detail),
                    "pblntf_detail_ty": detail,
                    "last_reprt_at": "Y",
                    "page_no": page_no,
                    "page_count": 100,
                },
            )
            data = resp.json()
            code = str(data.get("status", ""))
            if code == "013":  # no matching filings
                return reports
            if code != "000":
                raise classify_status(code, str(data.get("message", "")))
            for item in data.get("list", []):
                reports.append(
                    DartReport(
                        corp_name=str(item.get("corp_name", "")),
                        corp_code=str(item.get("corp_code", "")),
                        stock_code=str(item.get("stock_code", "")).strip(),
                        report_nm=str(item.get("report_nm", "")).strip(),
                        rcept_no=str(item.get("rcept_no", "")),
                        flr_nm=str(item.get("flr_nm", "")),
                        rcept_dt=str(item.get("rcept_dt", "")),
                    )
                )
            total_page = int(data.get("total_page") or 1)
            if page_no >= total_page:
                return reports
            page_no += 1

    async def _download_zip(self, path: str, params: dict[str, Any]) -> bytes:
        resp = await self._get(path, params)
        content = resp.content
        if not _is_zip(content):
            raise parse_error_body(content)
        return content

    async def download_document(self, rcept_no: str) -> bytes:
        return await self._download_zip("document.xml", {"rcept_no": rcept_no})

    async def download_corp_codes(self) -> bytes:
        return await self._download_zip("corpCode.xml", {})

    def fnltt_cache_path(
        self, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str
    ) -> Path | None:
        if self._fnltt_cache_dir is None:
            return None
        return self._fnltt_cache_dir / f"{corp_code}_{bsns_year}_{reprt_code}_{fs_div}.json"

    async def financial_statements(
        self, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str
    ) -> list[dict[str, Any]]:
        """``fnlttSinglAcntAll.json`` rows (단일회사 전체 재무제표). Status ``013`` (no data) is an
        empty list; other statuses raise ``DartError``. Status 000 responses are cached in
        ``fnltt_cache_dir`` and reused."""
        cache = self.fnltt_cache_path(corp_code, bsns_year, reprt_code, fs_div)
        if cache is not None:
            cached = await asyncio.to_thread(_read_json_list, cache)
            if cached is not None:
                return cached
        resp = await self._get(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
                "fs_div": fs_div,
            },
        )
        try:
            data = resp.json()
        except ValueError:
            raise DartError("invalid_response", "expected JSON", retryable=True) from None
        code = str(data.get("status", ""))
        if code == "013":  # no financial statements for this report
            return []
        if code != "000":
            raise classify_status(code, str(data.get("message", "")))
        rows = [r for r in data.get("list") or [] if isinstance(r, dict)]
        if cache is not None:
            await asyncio.to_thread(_write_json_atomic, cache, rows)
        return rows


class FakeDartClient:
    """Synthetic, network-free DART client used in tests and in fake mode."""

    def __init__(self, documents: dict[str, bytes] | None = None) -> None:
        from footnote_rag.dart import fake_data

        self._fake_data = fake_data
        self._documents = documents
        self.download_calls: list[str] = []
        self.financial_calls: list[tuple[str, str, str, str]] = []

    async def list_reports(
        self, *, corp_code: str, bgn_de: str, end_de: str, detail: str = DEFAULT_DETAIL
    ) -> list[DartReport]:
        pblntf_ty_for(detail)  # same validation as the HTTP client
        return [
            DartReport(**r)
            for r in self._fake_data.reports_for(detail)
            if r["corp_code"] == corp_code and bgn_de <= r["rcept_dt"] <= end_de
        ]

    async def download_document(self, rcept_no: str) -> bytes:
        self.download_calls.append(rcept_no)
        if self._documents is not None:
            if rcept_no not in self._documents:
                raise classify_status("014", "file does not exist")
            return self._documents[rcept_no]
        return self._fake_data.build_document_zip(rcept_no)

    async def download_corp_codes(self) -> bytes:
        return self._fake_data.build_corp_code_zip()

    async def financial_statements(
        self, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str
    ) -> list[dict[str, Any]]:
        """Synthetic statements of 가람전자(합성) (``fake_data.financial_statement_rows``)."""
        self.financial_calls.append((corp_code, bsns_year, reprt_code, fs_div))
        return self._fake_data.financial_statement_rows(corp_code, bsns_year, reprt_code, fs_div)


def read_zip_members(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist() if not info.is_dir()}
