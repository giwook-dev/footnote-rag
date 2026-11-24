"""종목코드로 감사보고서를 찾는 서비스 함수(CLI ``dart-find``와 ``GET /dart/reports``가 함께 쓴다).

역할: 종목코드 → corpCode 캐시로 고유번호 조회 → ``list.json``으로 기간·종류에 맞는 감사보고서 목록 조회.
corpCode 목록에 없는 종목코드는 ``not_found``로 모은다.
주요 요소: ``find_reports``.
호출 관계: ``cli.cmd_dart_find``와 ``dart.router.search_reports``가 호출하고,
``providers.make_dart_client``·``dart.corp_codes.CorpCodeCache``·``DartClient.list_reports``를 부른다.
학습 문서: docs/study/07-dart-integration.md

Search audit reports by stock code (shared by the CLI and the HTTP API).
"""

from __future__ import annotations

from typing import Literal

from footnote_rag.config import Settings
from footnote_rag.dart.client import DartClient
from footnote_rag.dart.corp_codes import CorpCodeCache
from footnote_rag.jobs.handlers import dart_cache_dir
from footnote_rag.providers import make_dart_client


async def find_reports(
    settings: Settings,
    stock_codes: list[str],
    date_from: str,
    date_to: str,
    detail: str = "F001",
    *,
    client: DartClient | None = None,
) -> tuple[list[dict[str, str]], list[str], Literal["real", "fake"]]:
    """Return ``(reports, not_found, mode)``.

    ``reports`` are ``DartReport.as_dict()`` rows whose ``stock_code`` is the queried code.
    Raises ``MissingKeyError`` (real mode without a key) and ``DartError`` (OpenDART failure).
    """
    dart = client if client is not None else make_dart_client(settings)
    cache_path = dart_cache_dir(settings.dart_dir, dart) / "corp_codes.json"
    cache = CorpCodeCache(dart, cache_path, settings.dart_corp_code_ttl_days)
    reports: list[dict[str, str]] = []
    not_found: list[str] = []
    for stock_code in stock_codes:
        try:
            corp = await cache.by_stock_code(stock_code)
        except KeyError:
            not_found.append(stock_code)
            continue
        found = await dart.list_reports(
            corp_code=corp.corp_code, bgn_de=date_from, end_de=date_to, detail=detail
        )
        for r in found:
            reports.append({**r.as_dict(), "stock_code": stock_code})
    return reports, not_found, settings.dart_mode
