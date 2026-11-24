import io
import zipfile
from pathlib import Path

import pytest

from footnote_rag.dart import fake_data
from footnote_rag.dart.parser import (
    DartParseError,
    is_note_heading,
    normalize_title,
    parse_document_xml,
    parse_document_zip,
    pick_main_member,
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


def test_normalize_title():
    assert normalize_title("재 무 상 태 표") == "재무상태표"
    assert normalize_title("(첨부)재 무 제 표") == "(첨부)재무제표"
    assert normalize_title("독립된  감사인의 감사보고서") == "독립된 감사인의 감사보고서"
