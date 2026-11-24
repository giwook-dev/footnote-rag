"""Synthetic DART data for fake mode and tests. NOT real filings.

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
    for r in FAKE_REPORTS:
        if r["rcept_no"] == rcept_no:
            return r
    return {"corp_name": "합성회사(합성)", "corp_code": "99999999", "report_nm": "감사보고서"}


def build_document_xml(rcept_no: str, *, encoding: str = "utf-8") -> bytes:
    company = _company(rcept_no)
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
