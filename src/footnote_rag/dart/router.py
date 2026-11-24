"""DART 보고서 검색 엔드포인트 ``GET /dart/reports``(member 이상).

역할: 종목코드(쉼표 구분, 최대 10개)·기간(YYYYMMDD)·종류(A001 기본/F001/F002)를 검증하고 ``find_reports``를 호출한다.
OpenDART 오류는 재시도 가능하면 503, 아니면 502로 바꾸며, 응답에는 오류 코드와 고정된 짧은 메시지만 넣는다
(API 키·요청 URL·OpenDART 원문 메시지는 넣지 않는다). 성공하면 ``dart.search`` 감사 로그를 남긴다.
주요 요소: ``search_reports``, ``parse_stock_codes``, ``DartSearchOut``.
호출 관계: ``dart.service.find_reports``와 ``audit.service.record``를 호출한다. 웹 화면(``/ui``)의 문서 탭이 쓴다.
학습 문서: docs/study/07-dart-integration.md

GET /dart/reports.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.audit.service import Actions, record
from footnote_rag.auth.deps import ANY_MEMBER, require_role
from footnote_rag.config import get_settings
from footnote_rag.dart.client import DartError
from footnote_rag.dart.service import find_reports
from footnote_rag.db.models import User
from footnote_rag.db.session import get_session
from footnote_rag.observability import get_logger
from footnote_rag.providers import MissingKeyError

log = get_logger(__name__)
router = APIRouter(tags=["dart"])

MAX_STOCK_CODES = 10
_STOCK_CODE_RE = re.compile(r"^\d{6}$")
_DATE_RE = re.compile(r"^\d{8}$")
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_]{1,20}$")


class DartReportOut(BaseModel):
    corp_name: str
    stock_code: str
    report_nm: str
    rcept_no: str
    rcept_dt: str


class DartSearchOut(BaseModel):
    mode: Literal["real", "fake"]
    reports: list[DartReportOut]
    not_found: list[str]


def parse_stock_codes(raw: str) -> list[str]:
    codes: list[str] = []
    for part in raw.split(","):
        code = part.strip()
        if code and code not in codes:
            codes.append(code)
    if not codes:
        raise HTTPException(status_code=422, detail="stock_codes is empty")
    if len(codes) > MAX_STOCK_CODES:
        raise HTTPException(
            status_code=422, detail=f"stock_codes allows at most {MAX_STOCK_CODES} codes"
        )
    bad = [c for c in codes if not _STOCK_CODE_RE.match(c)]
    if bad:
        raise HTTPException(status_code=422, detail="each stock code must be 6 digits")
    return codes


def _parse_date(name: str, value: str) -> str:
    if not _DATE_RE.match(value):
        raise HTTPException(status_code=422, detail=f"{name} must be YYYYMMDD")
    try:
        datetime.strptime(value, "%Y%m%d")  # calendar check only (e.g. rejects 20260230)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} is not a valid date") from None
    return value


def _dart_error_detail(exc: DartError) -> str:
    """Only the OpenDART status code and a fixed short message: never the key, URL or raw text."""
    code = exc.code if _SAFE_CODE_RE.match(exc.code or "") else "unknown"
    if exc.retryable:
        return f"OpenDART 오류 {code}: 일시적인 오류입니다. 잠시 후 다시 시도하세요."
    return f"OpenDART 오류 {code}: 요청이 거부되었습니다(키·권한·요청 값 확인 필요)."


@router.get("/dart/reports", response_model=DartSearchOut)
async def search_reports(
    stock_codes: str = Query(
        ..., max_length=200, description="comma separated 6-digit stock codes (max 10)"
    ),
    date_from: str = Query(..., description="YYYYMMDD"),
    date_to: str = Query(..., description="YYYYMMDD"),
    detail: Literal["A001", "F001", "F002"] = Query(
        default="A001",
        description=(
            "A001 annual report (listed companies: audited statements and notes are here), "
            "F001 audit report, F002 consolidated audit report"
        ),
    ),
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> DartSearchOut:
    codes = parse_stock_codes(stock_codes)
    bgn = _parse_date("date_from", date_from)
    end = _parse_date("date_to", date_to)
    if bgn > end:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    try:
        reports, not_found, mode = await find_reports(get_settings(), codes, bgn, end, detail)
    except MissingKeyError:
        raise HTTPException(
            status_code=503, detail="OpenDART API 키가 설정되지 않았습니다."
        ) from None
    except DartError as exc:
        log.warning("dart.search_failed", code=exc.code, retryable=exc.retryable)
        raise HTTPException(
            status_code=503 if exc.retryable else 502, detail=_dart_error_detail(exc)
        ) from None
    record(
        session,
        action=Actions.DART_SEARCH,
        org_id=user.org_id,
        actor_user_id=user.id,
        target_type="dart",
        metadata={
            "stock_codes": codes,
            "date_from": bgn,
            "date_to": end,
            "detail": detail,
            "result_count": len(reports),
            "not_found": not_found,
        },
    )
    await session.commit()
    return DartSearchOut(
        mode=mode,
        reports=[DartReportOut(**{k: r[k] for k in DartReportOut.model_fields}) for r in reports],
        not_found=not_found,
    )
