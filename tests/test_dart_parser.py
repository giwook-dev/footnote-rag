import io
import zipfile
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from footnote_rag.dart import fake_data
from footnote_rag.dart.parser import (
    DartParseError,
    Section,
    is_note_heading,
    is_parse_suspicious,
    normalize_title,
    parse_document_xml,
    parse_document_zip,
    pick_main_member,
    sanitize_dart_xml,
)

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_audit_report.xml"


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _by_path(parsed):
    return {s.path: s.text for s in parsed.sections}


def test_nested_section_paths_and_metadata():
    parsed = parse_document_xml(FIXTURE.read_bytes())
    sections = _by_path(parsed)
    assert parsed.company_name == "테스트상사(합성)"
    assert parsed.corp_code == "99000123"
    assert parsed.document_name == "감사보고서"
    assert parsed.title == "테스트상사(합성) 감사보고서"
    assert list(sections)[:2] == ["감사보고서", "독립된 감사인의 감사보고서"]
    # LIBRARY is transparent; letter-spaced titles are joined.
    assert "(첨부)재무제표 > 재무상태표" in sections
    # Notes: numbered paragraphs become sub-sections; "(1)" and sentences are not headings.
    assert "(첨부)재무제표 > 주석" in sections
    assert "(첨부)재무제표 > 주석 > 1. 일반사항" in sections
    lease = sections["(첨부)재무제표 > 주석 > 12. 리스"]
    assert "(1) 리스부채는 리스료의 현재가치로 측정합니다." in lease
    assert "단기리스에는 인식 면제를 적용하였습니다." in lease
    # SECTION-3 nested under notes.
    assert "(첨부)재무제표 > 주석 > 리스 만기분석" in sections


def test_table_rows_become_pipe_separated_lines():
    sections = _by_path(parse_document_xml(FIXTURE.read_bytes()))
    table = sections["(첨부)재무제표 > 재무상태표"].splitlines()
    assert table[0] == "과 목 | 제 10 기 | 제 9 기"
    assert "리스부채 | 250 | 200" in table
    assert "(단위: 백만원)" in table
    assert all(line.strip() for line in table)  # empty rows dropped
    maturity = sections["(첨부)재무제표 > 주석 > 리스 만기분석"]
    assert "리스료 | 120 | 130" in maturity


def test_euc_kr_declared_encoding_is_honoured():
    xml = fake_data.build_document_xml("20260310900001", encoding="euc-kr")
    assert b'encoding="euc-kr"' in xml
    parsed = parse_document_xml(xml)
    assert parsed.company_name == "가람전자(합성)"
    assert any(s.path.endswith("12. 리스") for s in parsed.sections)


def test_undeclared_cp949_bytes_fall_back():
    xml = "<DOCUMENT><BODY><SECTION-1><TITLE>주석</TITLE><P>본문 텍스트</P></SECTION-1></BODY></DOCUMENT>"
    parsed = parse_document_xml(xml.encode("cp949"))
    assert parsed.sections[0].path == "주석"
    assert parsed.sections[0].text == "본문 텍스트"


def test_empty_document_is_a_parse_error():
    xml = b'<?xml version="1.0" encoding="utf-8"?><DOCUMENT><BODY><SECTION-1><TITLE>X</TITLE><P>  </P></SECTION-1></BODY></DOCUMENT>'
    with pytest.raises(DartParseError):
        parse_document_xml(xml)
    with pytest.raises(DartParseError):
        parse_document_zip(_zip({"20260101000001.xml": xml}), "20260101000001")


def test_invalid_zip_and_zip_without_xml_are_parse_errors():
    with pytest.raises(DartParseError):
        parse_document_zip(b"not a zip")
    with pytest.raises(DartParseError):
        parse_document_zip(_zip({"readme.txt": b"hi"}))


def test_main_member_prefers_rcept_no_then_largest():
    members = {"20260101000001_00760.xml": b"x" * 500, "20260101000001.xml": b"x" * 10}
    assert pick_main_member(members, "20260101000001") == "20260101000001.xml"
    assert pick_main_member(members, None) == "20260101000001_00760.xml"


def test_fake_zip_excludes_attachment():
    parsed = parse_document_zip(fake_data.build_document_zip("20260310900002"), "20260310900002")
    assert parsed.main_member == "20260310900002.xml"
    assert all("첨부(합성)" not in s.text for s in parsed.sections)


ANNUAL_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_annual_report.xml"
NOTES = "III. 재무에 관한 사항 > 3. 연결재무제표 주석"


def test_annual_report_note_titles_become_sub_sections():
    parsed = parse_document_xml(ANNUAL_FIXTURE.read_bytes())
    sections = _by_path(parsed)
    assert parsed.title == "시험전자(합성) 사업보고서"
    general = sections[f"{NOTES} > 1. 일반적 사항 (연결)"]
    lease = sections[f"{NOTES} > 12. 리스"]  # whitespace in the TITLE is normalized
    # Paragraphs and tables up to the next TITLE belong to the sub-section.
    assert "가. 연결회사의 개요" in general
    assert "연결회사는 합성 부품을 제조하고 판매합니다." in general
    assert "합성자회사 | 100%" in general
    assert "(1) 리스부채는 리스료의 현재가치로 측정합니다." in lease
    assert "단기리스에는 인식 면제를 적용하였습니다." in lease
    # "가."/"나." level headings are not split.
    assert not any(p.endswith(("가. 연결회사의 개요", "나. 단기리스")) for p in sections)


def test_annual_report_note_title_text_is_not_duplicated_and_preamble_stays():
    sections = _by_path(parse_document_xml(ANNUAL_FIXTURE.read_bytes()))
    assert sections[NOTES] == "제 10 기 2025년 12월 31일 현재"
    texts = "\n".join(sections.values())
    assert "일반적 사항 (연결)" not in texts
    assert "12. 리스" not in texts and "12.  리스" not in texts
    assert "3. 연결재무제표 주석" not in texts


def test_title_headings_take_priority_over_numbered_paragraphs():
    sections = _by_path(parse_document_xml(ANNUAL_FIXTURE.read_bytes()))
    # Inside a notes section with TITLE sub-headings, "1. ..." paragraphs are body text.
    assert f"{NOTES} > 1. 연결대상 종속기업 현황" not in sections
    assert "1. 연결대상 종속기업 현황" in sections[f"{NOTES} > 1. 일반적 사항 (연결)"]
    # Non-notes sections are unchanged.
    assert (
        sections["I. 회사의 개요 > 1. 회사의 개요"]
        == "회사는 합성 데이터 시험용으로 설립되었습니다."
    )


def test_table_group_note_titles_become_sub_sections():
    """Real filings wrap each note in TABLE-GROUP > (TITLE, TABLE...) under the notes section."""
    sections = _by_path(parse_document_xml(ANNUAL_FIXTURE.read_bytes()))
    separate = "III. 재무에 관한 사항 > 5. 재무제표 주석"
    assert (
        sections[f"{separate} > 1. 일반적 사항"]
        == "가. 회사의 개요 회사는 별도재무제표를 작성합니다."
    )
    lease = sections[f"{separate} > 12. 리스"].splitlines()
    assert lease == [
        "리스부채는 증분차입이자율로 할인합니다.",
        "3. 기타",
        "구분 | 금액",
        "리스부채 | 50",
    ]
    assert separate not in sections  # no text before the first note
    assert not any(p.startswith(f"{separate} > 3. 기타") for p in sections)


def test_fake_annual_report_has_note_sub_sections():
    rcept_no = fake_data.FAKE_ANNUAL_REPORTS[0]["rcept_no"]
    parsed = parse_document_zip(fake_data.build_document_zip(rcept_no), rcept_no)
    paths = [s.path for s in parsed.sections]
    assert f"{NOTES} > 12. 리스 (연결)" in paths
    assert f"{NOTES} > 1. 일반사항 (연결)" in paths


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12. 리스", True),
        ("3. 중요한 회계정책", True),
        ("(1) 리스부채", False),
        ("1) 단기리스", False),
        ("1.5 배", False),
        ("1. 회사는 단기리스에 인식 면제를 적용하였습니다.", False),
    ],
)
def test_note_heading_detection(text, expected):
    assert is_note_heading(text) is expected


MALFORMED_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_malformed_report.xml"
MALFORMED_PATHS = [
    "I. 회사의 개요 > 1. 회사의 개요",
    "I. 회사의 개요 > 2. 회사의 연혁",
    "I. 회사의 개요 > 5. 정관에 관한 사항",
    "II. 사업의 내용 > 1. 사업의 개요",
    "II. 사업의 내용 > 5. 위험관리 및 파생거래",
    "II. 사업의 내용 > 7. 기타 참고사항",
    "V. 회계감사인의 감사의견 등 > 1. 외부감사에 관한 사항",
    "VI. 이사회 등 회사의 기관에 관한 사항 > 1. 이사회에 관한 사항",
]
ALL_FIXES = {"bare_amp": 2, "bare_lt": 3, "bad_attr_quote": 1, "pseudo_tag": 2}
NO_FIXES = {"bare_amp": 0, "bare_lt": 0, "bad_attr_quote": 0, "pseudo_tag": 0}


def test_sections_after_bare_characters_are_split():
    parsed = parse_document_xml(MALFORMED_FIXTURE.read_bytes())
    assert [s.path for s in parsed.sections] == MALFORMED_PATHS
    assert parsed.truncated is False
    sections = _by_path(parsed)
    assert sections["II. 사업의 내용 > 5. 위험관리 및 파생거래"].splitlines() == [
        "회사는 환율 변동 위험을 관리합니다.",
        "종속기업 | 스노우 주식회사",  # the cell with the doubled attribute quote survives
        "지분율 | 100%",
    ]
    assert sections["II. 사업의 내용 > 7. 기타 참고사항"].splitlines() == [
        "<Manufacturing Excellence>",
        "제조 경쟁력을 강화합니다.",
        "<Advanced Tech & SDV>",
        "기타 참고사항 본문입니다.",
    ]
    assert sections["V. 회계감사인의 감사의견 등 > 1. 외부감사에 관한 사항"] == (
        "감사인은 합성회계법인입니다."
    )


def test_attribute_quote_and_pseudo_tag_rules_are_needed(monkeypatch):
    """With only the "&" and "<" rules the later structure and text are damaged."""
    import re

    from footnote_rag.dart import parser

    never = re.compile(rb"(?!x)x")
    monkeypatch.setattr(parser, "_BAD_ATTR_QUOTE_RE", never)
    monkeypatch.setattr(parser, "_PSEUDO_TAG_RE", never)
    parsed = parser.parse_document_xml(MALFORMED_FIXTURE.read_bytes())
    texts = "\n".join(s.text for s in parsed.sections)
    assert "II. 사업의 내용 > 7. 기타 참고사항" not in [s.path for s in parsed.sections]
    assert "스노우 주식회사" not in texts and "<Manufacturing Excellence>" not in texts


def test_truncated_document_is_flagged_and_logged():
    xml = (
        b"<DOCUMENT><BODY><SECTION-1><TITLE>A</TITLE><P>a</P></SECTION-1></BODY></DOCUMENT>"
        b"<SECTION-1><TITLE>B</TITLE><P>dropped by recover mode</P></SECTION-1>"
    )
    with capture_logs() as logs:
        parsed = parse_document_xml(xml, main_member="t.xml")
    assert parsed.truncated is True  # a warning, not a failure
    assert [s.path for s in parsed.sections] == ["A"]
    warnings = [e for e in logs if e["event"] == "dart.parse_truncated"]
    assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"
    assert warnings[0]["main_member"] == "t.xml" and warnings[0]["line"] == 1
    with capture_logs() as logs:
        assert parse_document_xml(MALFORMED_FIXTURE.read_bytes()).truncated is False
    assert not [e for e in logs if e["event"] == "dart.parse_truncated"]


def test_without_sanitizing_the_fixture_loses_its_structure(monkeypatch):
    """Guards the fixture itself: it must reproduce the real-filing failure."""
    from footnote_rag.dart import parser

    monkeypatch.setattr(parser, "sanitize_dart_xml", lambda raw: (raw, {}))
    paths = [s.path for s in parser.parse_document_xml(MALFORMED_FIXTURE.read_bytes()).sections]
    assert paths != MALFORMED_PATHS
    assert "V. 회계감사인의 감사의견 등 > 1. 외부감사에 관한 사항" not in paths


def test_bare_characters_are_preserved_verbatim():
    sections = _by_path(parse_document_xml(MALFORMED_FIXTURE.read_bytes()))
    texts = "\n".join(sections.values())
    assert "Place&JAPAN Co., Ltd" in sections["I. 회사의 개요 > 1. 회사의 개요"]
    assert "S&P(미국)" in sections["II. 사업의 내용 > 1. 사업의 개요"]
    assert "<개정 2015.03.20>" in sections["I. 회사의 개요 > 5. 정관에 관한 사항"]
    assert "<신 설>" in sections["I. 회사의 개요 > 5. 정관에 관한 사항"]  # whitespace normalized
    assert "< TV 시장점유율 추이 >" in sections["II. 사업의 내용 > 1. 사업의 개요"]
    assert "&amp;" not in texts and "&lt;" not in texts


def test_valid_entities_are_not_double_escaped():
    sections = _by_path(parse_document_xml(MALFORMED_FIXTURE.read_bytes()))
    assert sections["I. 회사의 개요 > 2. 회사의 연혁"] == (
        "정상 엔티티: R&D 센터, 비율 1 < 2, 회사's 브랜드"
    )
    fixed, counts = sanitize_dart_xml(b"<P>R&amp;D &lt; &#39; &#x27; &nbsp;</P>")
    assert fixed == b"<P>R&amp;D &lt; &#39; &#x27; &nbsp;</P>"
    assert counts == NO_FIXES


def test_sanitize_counts_and_markup_left_alone():
    parsed = parse_document_xml(MALFORMED_FIXTURE.read_bytes())
    assert parsed.sanitized == ALL_FIXES
    raw = b'<?xml version="1.0"?><!-- c --><A x="1"><B/><C>a&b <x>< y</C><![CDATA[z]]></A>'
    fixed, counts = sanitize_dart_xml(raw)
    assert fixed == (
        b'<?xml version="1.0"?><!-- c --><A x="1"><B/><C>a&amp;b <x>&lt; y</C><![CDATA[z]]></A>'
    )
    assert counts == {**NO_FIXES, "bare_amp": 1, "bare_lt": 1}
    # Well-formed documents are unchanged; EUC-KR bytes never contain ASCII "&" / "<".
    euc = "<P>삼성&LG <개정></P>".encode("euc-kr")
    fixed, counts = sanitize_dart_xml(euc)
    assert fixed.decode("euc-kr") == "<P>삼성&amp;LG &lt;개정></P>"
    assert counts == {**NO_FIXES, "bare_amp": 1, "bare_lt": 1}


def test_attribute_quote_and_pseudo_tag_rules():
    raw = (
        b'<TE ENG=""Snow Corporation" VALIGN="MIDDLE">x</TE>'
        b'<P USERMARK="B"><Manufacturing Excellence></P><P><New Product: A &amp; B></P>'
    )
    fixed, counts = sanitize_dart_xml(raw)
    assert fixed == (
        b'<TE ENG="&quot;Snow Corporation" VALIGN="MIDDLE">x</TE>'
        b'<P USERMARK="B">&lt;Manufacturing Excellence></P><P>&lt;New Product: A &amp; B></P>'
    )
    assert counts == {**NO_FIXES, "bad_attr_quote": 1, "pseudo_tag": 2}
    # Real markup is untouched: attributes with values, empty values, self-closing and
    # whitespace before ">" or "/>".
    valid = (
        b'<TABLE ACLASS="NORMAL" BORDER="1"><TR><TD ALIGN="">a</TD><TE\n  WIDTH="1">b</TE>'
        b'<IMG SRC="x.jpg"/><PGBRK /></TR></TABLE ><P >c</P>'
    )
    assert sanitize_dart_xml(valid) == (valid, NO_FIXES)


def test_sanitized_counts_are_logged():
    with capture_logs() as logs:
        parse_document_xml(MALFORMED_FIXTURE.read_bytes(), main_member="x.xml")
    events = [e for e in logs if e["event"] == "dart.sanitized"]
    assert events and {k: events[0][k] for k in ALL_FIXES} == ALL_FIXES
    assert events[0]["main_member"] == "x.xml"


def _doc(sections: list[tuple[str, str]]) -> bytes:
    body = "".join(f"<SECTION-1><TITLE>{t}</TITLE><P>{p}</P></SECTION-1>" for t, p in sections)
    return f"<DOCUMENT><BODY>{body}</BODY></DOCUMENT>".encode()


def test_parse_suspicious_warning_when_one_section_dominates():
    xml = _doc([("A", "가" * 900), ("B", "나" * 50), ("C", "다" * 50)])
    with capture_logs() as logs:
        parsed = parse_document_xml(xml, main_member="m.xml")
    warnings = [e for e in logs if e["event"] == "dart.parse_suspicious"]
    assert len(parsed.sections) == 3  # a warning, not a failure
    assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"
    assert warnings[0]["largest_path"] == "A" and warnings[0]["sections"] == 3
    assert warnings[0]["largest_chars"] == 900 and warnings[0]["total_chars"] == 1000


def test_no_parse_suspicious_warning_for_balanced_or_many_sections():
    balanced = _doc([("A", "가" * 400), ("B", "나" * 300), ("C", "다" * 300)])
    many = _doc([("A", "가" * 900)] + [(f"S{i}", "나") for i in range(19)])
    for xml in (balanced, many):
        with capture_logs() as logs:
            parse_document_xml(xml)
        assert not [e for e in logs if e["event"] == "dart.parse_suspicious"]
    assert not is_parse_suspicious([])
    assert is_parse_suspicious([Section("A", "x" * 6), Section("B", "y" * 4)])
    assert not is_parse_suspicious([Section("A", "x" * 5), Section("B", "y" * 5)])


def test_normalize_title():
    assert normalize_title("재 무 상 태 표") == "재무상태표"
    assert normalize_title("(첨부)재 무 제 표") == "(첨부)재무제표"
    assert normalize_title("독립된  감사인의 감사보고서") == "독립된 감사인의 감사보고서"
