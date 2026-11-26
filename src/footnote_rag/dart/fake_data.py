"""Fake 모드와 테스트용 합성 DART 데이터. 실제 공시가 아니다.

역할: ``(합성)`` 표시가 붙은 가상 회사 3곳의 공시 목록(F001 감사보고서, A001 사업보고서),
원문 XML/ZIP(첨부 XML 포함), corpCode ZIP을 만든다. 합성 사업보고서의 주석은 실제 사업보고서처럼
주석 번호별 소제목을 ``TITLE`` 요소로 둔다. 가람전자(합성)에는 전기 대비 주석 변동 분석 시연용 전년 사업보고서
(``20250310900101``, 2024 사업연도)가 있고, 2025 보고서(``20260310900101``)와 비교하면 주석 추가·삭제,
우발부채 문장 변경, 차입금 금액 변동(큰 변동 포함), 전기 수치 불일치가 나온다. 에이전트 ``get_financials`` 도구용으로
가람전자(합성) 2024·2025 사업보고서의 합성 재무제표(``FAKE_FINANCIALS``, 원 단위)도 있다.
주요 요소: ``FAKE_CORPS``, ``FAKE_REPORTS``, ``FAKE_ANNUAL_REPORTS``, ``FAKE_PRIOR_ANNUAL_REPORTS``,
``FAKE_ANNUAL_PAIR``, ``reports_for``, ``build_document_xml``, ``build_document_zip``, ``build_corp_code_zip``,
``FAKE_FINANCIALS``, ``financial_statement_rows``.
호출 관계: ``dart.client.FakeDartClient``가 호출한다. 파서는 이 데이터와 테스트 픽스처로만 검증됐다.
학습 문서: docs/study/07-dart-integration.md, docs/study/09-testing-and-ops.md

Synthetic DART data for fake mode and tests. NOT real filings.

Company names carry the "(합성)" marker and corp codes start with 99 so nobody mistakes
this for real disclosure data.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

FAKE_CORPS = [
    {"corp_code": "99000001", "corp_name": "가람전자(합성)", "stock_code": "990001"},
    {"corp_code": "99000002", "corp_name": "나래포털(합성)", "stock_code": "990002"},
    {"corp_code": "99000003", "corp_name": "다솜모터스(합성)", "stock_code": "990003"},
]

FAKE_REPORTS = [
    {
        "corp_name": c["corp_name"],
        "corp_code": c["corp_code"],
        "stock_code": c["stock_code"],
        "report_nm": "감사보고서 (2025.12)",
        "rcept_no": f"2026031090000{i + 1}",
        "flr_nm": "합성회계법인",
        "rcept_dt": "20260310",
    }
    for i, c in enumerate(FAKE_CORPS)
]

# A001 사업보고서: listed companies file their audited statements and notes here.
FAKE_ANNUAL_REPORTS = [
    {
        "corp_name": c["corp_name"],
        "corp_code": c["corp_code"],
        "stock_code": c["stock_code"],
        "report_nm": "사업보고서 (2025.12)",
        "rcept_no": f"2026031090010{i + 1}",
        "flr_nm": c["corp_name"],
        "rcept_dt": "20260310",
    }
    for i, c in enumerate(FAKE_CORPS)
]

# The prior year's annual report of 가람전자(합성), for the note change analysis demo
# (docs/adr/0012): compared with 20260310900101 it has one note removed, one added, a changed
# 우발부채 sentence, borrowing amounts that changed (one by more than 30%) and one prior-period
# figure the 2025 report restates.
PRIOR_ANNUAL_RCEPT_NO = "20250310900101"
FAKE_PRIOR_ANNUAL_REPORTS = [
    {
        "corp_name": FAKE_CORPS[0]["corp_name"],
        "corp_code": FAKE_CORPS[0]["corp_code"],
        "stock_code": FAKE_CORPS[0]["stock_code"],
        "report_nm": "사업보고서 (2024.12)",
        "rcept_no": PRIOR_ANNUAL_RCEPT_NO,
        "flr_nm": FAKE_CORPS[0]["corp_name"],
        "rcept_dt": "20250310",
    }
]
FAKE_ANNUAL_REPORTS += FAKE_PRIOR_ANNUAL_REPORTS
# (prior, current) annual report pair of the same synthetic company.
FAKE_ANNUAL_PAIR = (PRIOR_ANNUAL_RCEPT_NO, FAKE_ANNUAL_REPORTS[0]["rcept_no"])


def reports_for(detail: str) -> list[dict[str, str]]:
    """Synthetic filing list per ``pblntf_detail_ty`` (F002 shares the F001 list, as before)."""
    return FAKE_ANNUAL_REPORTS if detail == "A001" else FAKE_REPORTS


_NOTES: dict[str, list[tuple[str, list[str]]]] = {
    "default": [
        ("1. 일반사항", ["회사는 전자부품의 제조와 판매를 주된 사업으로 영위하고 있습니다."]),
        (
            "2. 중요한 회계정책",
            ["재무제표는 한국채택국제회계기준에 따라 작성되었습니다."],
        ),
        (
            "12. 리스",
            [
                "회사는 리스부채를 리스개시일 현재 지급되지 않은 리스료의 현재가치로 측정합니다.",
                "회사는 단기리스와 소액자산 리스에 대하여 인식 면제를 적용하였습니다.",
            ],
        ),
        (
            "20. 우발부채와 약정사항",
            ["보고기간말 현재 회사가 피소된 소송사건은 3건이며 소송가액은 1,200백만원입니다."],
        ),
    ],
}


def _company(rcept_no: str) -> dict[str, str]:
    for r in (*FAKE_REPORTS, *FAKE_ANNUAL_REPORTS):
        if r["rcept_no"] == rcept_no:
            return r
    return {"corp_name": "합성회사(합성)", "corp_code": "99999999", "report_nm": "감사보고서"}


def _is_annual(rcept_no: str) -> bool:
    return any(r["rcept_no"] == rcept_no for r in FAKE_ANNUAL_REPORTS)


# A note body block after the boxed paragraphs: a paragraph (str) or a table (rows of cells).
Block = str | list[list[str]]
# (heading, paragraphs boxed in one table cell as in the original synthetic reports, blocks)
AnnualNote = tuple[str, list[str], list[Block]]

_BORROWINGS_TITLE = "차입금 내역"
_CONTINGENCY_SENTENCE_2025 = "당기 중 회사는 합성은행과 운영자금 한도약정을 새로 체결하였습니다."
# 가람전자(합성) 2025 (20260310900101): extra notes and blocks on top of the default notes.
_GARAM_2025_EXTRA: dict[str, list[Block]] = {
    "20. 우발부채와 약정사항": [_CONTINGENCY_SENTENCE_2025],
}
_GARAM_2025_NEW_NOTES: list[AnnualNote] = [
    (
        "15. 차입금",
        [],
        [
            _BORROWINGS_TITLE,
            [
                ["당기", "(단위 : 백만원)"],
                ["구분", "금액"],
                ["단기차입금", "5,000"],
                ["장기차입금", "12,000"],
                ["전기", "(단위 : 백만원)"],
                ["구분", "금액"],
                ["단기차입금", "3,000"],
                ["장기차입금", "11,500"],  # the 2024 report said 11,000: restated
            ],
        ],
    ),
    (
        "21. 사업결합",
        [],
        ["당기 중 회사는 합성소재(합성)의 지분 100%를 취득하여 종속기업으로 편입하였습니다."],
    ),
]
# 가람전자(합성) 2024 (20250310900101): the prior year's notes.
_GARAM_2024_NOTES: list[AnnualNote] = [
    _NOTES["default"][0] + ([],),
    _NOTES["default"][1] + ([],),
    _NOTES["default"][2] + ([],),
    (
        "15. 차입금",
        [],
        [
            _BORROWINGS_TITLE,
            [
                ["당기", "(단위 : 백만원)"],
                ["구분", "금액"],
                ["단기차입금", "3,000"],
                ["장기차입금", "11,000"],
                ["전기", "(단위 : 백만원)"],
                ["구분", "금액"],
                ["단기차입금", "2,800"],
                ["장기차입금", "10,000"],
            ],
        ],
    ),
    (
        "18. 매각예정비유동자산",
        [],
        ["회사는 보고기간말 현재 매각예정으로 분류한 합성공장 토지를 보유하고 있습니다."],
    ),
    (
        "20. 우발부채와 약정사항",
        ["보고기간말 현재 회사가 피소된 소송사건은 1건이며 소송가액은 300백만원입니다."],
        ["회사는 주요 거래처의 차입금에 대하여 지급보증을 제공하고 있습니다."],
    ),
]


def _note_number(heading: str) -> int:
    return int(heading.split(".", 1)[0])


def _annual_notes(rcept_no: str, company: dict[str, str]) -> list[AnnualNote]:
    if rcept_no == PRIOR_ANNUAL_RCEPT_NO:
        return _GARAM_2024_NOTES
    notes: list[AnnualNote] = [(h, p, []) for h, p in _NOTES["default"]]
    if rcept_no == FAKE_ANNUAL_PAIR[1]:
        notes = [(h, p, [*b, *_GARAM_2025_EXTRA.get(h, [])]) for h, p, b in notes]
        notes = sorted([*notes, *_GARAM_2025_NEW_NOTES], key=lambda n: _note_number(n[0]))
    return notes


def _block_xml(block: Block) -> str:
    if isinstance(block, str):
        return f"<P>{escape(block)}</P>"
    rows = "".join(
        "<TR>" + "".join(f"<TD>{escape(cell)}</TD>" for cell in row) + "</TR>" for row in block
    )
    return f'<TABLE ACLASS="NORMAL" BORDER="1"><TBODY>{rows}</TBODY></TABLE>'


def _annual_report_xml(rcept_no: str, company: dict[str, str], encoding: str) -> str:
    """Annual report (A001) shaped like the real ones: each note is a ``TABLE-GROUP`` with its own
    ``TITLE`` (note sub-heading) and ``TABLE`` body under the notes ``SECTION-2``."""
    name = escape(company["corp_name"])
    prior_year = rcept_no == PRIOR_ANNUAL_RCEPT_NO
    term, year, lease = (
        ("56", "2024", ("1,100", "1,000")) if prior_year else ("57", "2025", ("1,234", "1,100"))
    )
    notes_xml = []
    for heading, paragraphs, blocks in _annual_notes(rcept_no, company):
        boxed = ""
        if paragraphs:
            cells = "".join(f"<P>{escape(p)}</P>" for p in paragraphs)
            boxed = (
                '<TABLE ACLASS="NORMAL" BORDER="0"><TBODY><TR>'
                f"<TD>{cells}</TD></TR></TBODY></TABLE>"
            )
        notes_xml.append(
            f'<TABLE-GROUP ADELETETABLE="N"><TITLE ATOC="Y">{escape(heading)} (연결)</TITLE>'
            f"{boxed}{''.join(_block_xml(b) for b in blocks)}</TABLE-GROUP>"
        )
    return f"""<?xml version="1.0" encoding="{encoding}"?>
<DOCUMENT>
<DOCUMENT-NAME ACODE="11011">사업보고서</DOCUMENT-NAME>
<COMPANY-NAME AREGCIK="{company["corp_code"]}">{name}</COMPANY-NAME>
<BODY>
<COVER><COVER-TITLE>사 업 보 고 서</COVER-TITLE><P>{name} (제 {term} 기) 사업연도 {year}년 01월 01일부터 {year}년 12월 31일까지</P></COVER>
<SECTION-1 ACLASS="MANDATORY">
<TITLE ATOC="Y">I. 회사의 개요</TITLE>
<SECTION-2 ACLASS="MANDATORY">
<TITLE ATOC="Y">1. 회사의 개요</TITLE>
<P>{name}는 전자부품의 제조와 판매를 주된 사업으로 영위하고 있습니다.</P>
</SECTION-2>
</SECTION-1>
<SECTION-1 ACLASS="MANDATORY">
<TITLE ATOC="Y">III. 재무에 관한 사항</TITLE>
<SECTION-2 ACLASS="MANDATORY">
<TITLE ATOC="Y">2. 연결재무제표</TITLE>
<TABLE><TBODY>
<TR><TH>과 목</TH><TH>제 57 기</TH><TH>제 56 기</TH></TR>
<TR><TD>리스부채</TD><TE>{lease[0]}</TE><TE>{lease[1]}</TE></TR>
<TR><TU>(단위: 백만원)</TU></TR>
</TBODY></TABLE>
</SECTION-2>
<SECTION-2 ACLASS="MANDATORY">
<TITLE ATOC="Y">3. 연결재무제표 주석</TITLE>
<P>제 {term} 기 {year}년 12월 31일 현재</P>
{"".join(notes_xml)}
</SECTION-2>
</SECTION-1>
</BODY>
</DOCUMENT>
"""


def build_document_xml(rcept_no: str, *, encoding: str = "utf-8") -> bytes:
    company = _company(rcept_no)
    if _is_annual(rcept_no):
        return _annual_report_xml(rcept_no, company, encoding).encode(encoding)
    name = escape(company["corp_name"])
    notes_xml = []
    for heading, paragraphs in _NOTES["default"]:
        notes_xml.append(f"<P>{escape(heading)}</P>")
        notes_xml.extend(f"<P>{escape(p)}</P>" for p in paragraphs)
    xml = f"""<?xml version="1.0" encoding="{encoding}"?>
<DOCUMENT>
<DOCUMENT-NAME ACODE="00760">감사보고서</DOCUMENT-NAME>
<COMPANY-NAME AREGCIK="{company["corp_code"]}">{name}</COMPANY-NAME>
<BODY>
<COVER><COVER-TITLE>감 사 보 고 서</COVER-TITLE><P>{name} 제 57 기 2025년 1월 1일부터 2025년 12월 31일까지</P></COVER>
<SECTION-1 ACLASS="MANDATORY">
<TITLE ATOC="Y">독립된 감사인의 감사보고서</TITLE>
<P>감사의견: 우리는 {name}의 재무제표가 중요성의 관점에서 공정하게 표시하고 있다고 의견을 표명합니다.</P>
</SECTION-1>
<SECTION-1 ACLASS="MANDATORY">
<TITLE ATOC="Y">(첨부)재 무 제 표</TITLE>
<SECTION-2>
<TITLE ATOC="Y">재 무 상 태 표</TITLE>
<TABLE><TBODY>
<TR><TH>과 목</TH><TH>제 57 기</TH><TH>제 56 기</TH></TR>
<TR><TD>리스부채</TD><TE>1,234</TE><TE>1,100</TE></TR>
<TR><TD>자산총계</TD><TE>98,765</TE><TE>90,000</TE></TR>
<TR><TU>(단위: 백만원)</TU></TR>
</TBODY></TABLE>
</SECTION-2>
<SECTION-2>
<TITLE ATOC="Y">주석</TITLE>
{"".join(notes_xml)}
</SECTION-2>
</SECTION-1>
</BODY>
</DOCUMENT>
"""
    return xml.encode(encoding)


def build_document_zip(rcept_no: str, *, encoding: str = "utf-8") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{rcept_no}.xml", build_document_xml(rcept_no, encoding=encoding))
        zf.writestr(
            f"{rcept_no}_00760.xml",
            '<?xml version="1.0" encoding="utf-8"?><DOCUMENT><BODY><P>첨부(합성)</P></BODY></DOCUMENT>'.encode(),
        )
    return buf.getvalue()


# Synthetic OpenDART fnlttSinglAcntAll rows (원, consolidated and separate alike) of
# 가람전자(합성), per fiscal year. NOT real figures. 2025 -> 2024 report pairs: 부채비율 64.6% / 63.6%.
FAKE_FINANCIALS: dict[str, dict[str, int]] = {
    "2025": {
        "자산총계": 98_765_000_000,
        "부채총계": 38_765_000_000,
        "자본총계": 60_000_000_000,
        "매출액": 120_000_000_000,
        "영업이익": 9_600_000_000,
        "당기순이익": 7_200_000_000,
    },
    "2024": {
        "자산총계": 90_000_000_000,
        "부채총계": 35_000_000_000,
        "자본총계": 55_000_000_000,
        "매출액": 110_000_000_000,
        "영업이익": 8_800_000_000,
        "당기순이익": 6_600_000_000,
    },
    "2023": {
        "자산총계": 84_000_000_000,
        "부채총계": 33_000_000_000,
        "자본총계": 51_000_000_000,
        "매출액": 100_000_000_000,
        "영업이익": 8_000_000_000,
        "당기순이익": 6_000_000_000,
    },
}
_FAKE_ACCOUNTS: tuple[tuple[str, str, str, str], ...] = (
    # (account_nm, sj_div, sj_nm, account_id)
    ("자산총계", "BS", "재무상태표", "ifrs-full_Assets"),
    ("부채총계", "BS", "재무상태표", "ifrs-full_Liabilities"),
    ("자본총계", "BS", "재무상태표", "ifrs-full_Equity"),
    ("매출액", "IS", "손익계산서", "ifrs-full_Revenue"),
    ("영업이익", "IS", "손익계산서", "dart_OperatingIncomeLoss"),
    ("당기순이익", "IS", "손익계산서", "ifrs-full_ProfitLoss"),
)
_FAKE_TERMS = {"2025": "57", "2024": "56", "2023": "55"}


def financial_statement_rows(
    corp_code: str, bsns_year: str, reprt_code: str, fs_div: str
) -> list[dict[str, str]]:
    """``fnlttSinglAcntAll``-shaped rows for 가람전자(합성) annual reports (``11011``) of 2024 and
    2025; anything else is ``[]`` (what the real API answers with status 013)."""
    prior_year = str(int(bsns_year) - 1) if bsns_year.isdigit() else ""
    if (
        corp_code != FAKE_CORPS[0]["corp_code"]
        or reprt_code != "11011"
        or bsns_year not in ("2024", "2025")
        or fs_div not in ("CFS", "OFS")
    ):
        return []
    rcept_no = PRIOR_ANNUAL_RCEPT_NO if bsns_year == "2024" else FAKE_ANNUAL_PAIR[1]
    current, prior = FAKE_FINANCIALS[bsns_year], FAKE_FINANCIALS[prior_year]
    return [
        {
            "rcept_no": rcept_no,
            "reprt_code": reprt_code,
            "bsns_year": bsns_year,
            "corp_code": corp_code,
            "sj_div": sj_div,
            "sj_nm": sj_nm,
            "account_id": account_id,
            "account_nm": name,
            "account_detail": "-",
            "thstrm_nm": f"제 {_FAKE_TERMS[bsns_year]} 기",
            "thstrm_amount": str(current[name]),
            "frmtrm_nm": f"제 {_FAKE_TERMS[prior_year]} 기",
            "frmtrm_amount": str(prior[name]),
            "ord": str(order),
            "currency": "KRW",
        }
        for order, (name, sj_div, sj_nm, account_id) in enumerate(_FAKE_ACCOUNTS, start=1)
    ]


def build_corp_code_zip() -> bytes:
    items = "".join(
        f"<list><corp_code>{c['corp_code']}</corp_code><corp_name>{escape(c['corp_name'])}</corp_name>"
        f"<stock_code>{c['stock_code']}</stock_code><modify_date>20260101</modify_date></list>"
        for c in FAKE_CORPS
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><result>{items}<list><corp_code>99000009</corp_code><corp_name>비상장(합성)</corp_name><stock_code> </stock_code><modify_date>20260101</modify_date></list></result>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml.encode("utf-8"))
    return buf.getvalue()
