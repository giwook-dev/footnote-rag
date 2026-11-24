"""OpenDART API 클라이언트(Protocol 뒤의 HTTP 실제 구현과 합성 데이터 Fake).

역할: ``list.json``(공시 목록, 페이지 반복), ``document.xml``(원문 ZIP), ``corpCode.xml``(고유번호 ZIP)을 호출하고,
OpenDART ``status`` 코드와 HTTP 오류를 재시도 가능/불가 ``DartError``로 분류한다. 로그 URL의 ``crtfc_key``는 가린다.
주요 요소: ``DartClient``, ``HttpDartClient``, ``FakeDartClient``, ``classify_status``, ``parse_error_body``.
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

import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from lxml import etree

from footnote_rag.observability import get_logger, mask_url

log = get_logger(__name__)

RETRYABLE_CODES = frozenset({"020", "800", "900"})
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
        self, *, corp_code: str, bgn_de: str, end_de: str, detail: str = "F001"
    ) -> list[DartReport]: ...

    async def download_document(self, rcept_no: str) -> bytes: ...

    async def download_corp_codes(self) -> bytes: ...


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
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

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
        self, *, corp_code: str, bgn_de: str, end_de: str, detail: str = "F001"
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
                    "pblntf_ty": "F",
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


class FakeDartClient:
    """Synthetic, network-free DART client used in tests and in fake mode."""

    def __init__(self, documents: dict[str, bytes] | None = None) -> None:
        from footnote_rag.dart import fake_data

        self._fake_data = fake_data
        self._documents = documents
        self.download_calls: list[str] = []

    async def list_reports(
        self, *, corp_code: str, bgn_de: str, end_de: str, detail: str = "F001"
    ) -> list[DartReport]:
        return [
            DartReport(**r)
            for r in self._fake_data.FAKE_REPORTS
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


def read_zip_members(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist() if not info.is_dir()}
