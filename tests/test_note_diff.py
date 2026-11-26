"""전기 대비 주석 변동 탐지 엔진(comparison/notes.py) 단위 테스트. 네트워크·DB 불필요."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from footnote_rag.comparison.notes import (
    WATCHLIST_WEIGHT,
    DiffOptions,
    Note,
    compare_documents,
    compare_rows,
    diff_lines,
    extract_notes,
    is_large_change,
    match_notes,
    note_score,
    parse_number,
    parse_table_rows,
)
from footnote_rag.dart import fake_data
from footnote_rag.dart.parser import Section, parse_document_xml

FIXTURES = Path(__file__).parent / "fixtures"
CONS = "III. 재무에 관한 사항 > 3. 연결재무제표 주석"
SEP = "III. 재무에 관한 사항 > 5. 재무제표 주석"


@pytest.fixture(scope="session")
def _database() -> None:
    """Override conftest's autouse DB setup: pure functions only."""


@pytest.fixture
def _clean_jobs() -> None:
    """Override conftest's autouse job cleanup: no database is needed here."""


def note(key: str, lines: list[str], title: str | None = None) -> Note:
    return Note(key=key, title=title or key, section_path=f"{CONS} > {title or key}", lines=lines)


# ---------------------------------------------------------------- extraction and matching


def test_extract_notes_groups_sections_by_key_and_scope():
    sections = [
        Section("I. 회사의 개요 > 1. 회사의 개요", "본문"),
        Section(CONS, "제 57 기 2025년 12월 31일 현재"),
        Section(f"{CONS} > 12. 차입금 (연결)", "  첫 줄 \n\n둘째   줄"),
        Section(f"{CONS} > 16. 우발부채와 약정사항 (연결)", "소송"),
        Section(f"{CONS} > 12. 차입금 (연결) > 가. 단기", "셋째 줄"),
        Section(f"{SEP} > 12. 차입금", "별도 줄"),
    ]
    notes = extract_notes(sections)
    assert [n.key for n in notes] == ["차입금", "우발부채와약정사항"]
    assert notes[0].lines == ["첫 줄", "둘째 줄", "셋째 줄"]
    assert notes[0].title == "12. 차입금 (연결)"
    assert notes[0].section_path == f"{CONS} > 12. 차입금 (연결)"
    separate = extract_notes(sections, "separate")
    assert [(n.key, n.lines) for n in separate] == [("차입금", ["별도 줄"])]
    with pytest.raises(ValueError):
        extract_notes(sections, "both")  # type: ignore[arg-type]


def test_match_notes_by_key_then_similar_title_then_added_removed():
    prior = [note("차입금", []), note("특수관계자와의거래", []), note("매각예정자산", [])]
    current = [note("차입금", []), note("특수관계자와의거래내역", []), note("사업결합", [])]
    pairs = match_notes(prior, current)
    summary = [
        (p.prior.key if p.prior else None, p.current.key if p.current else None, p.matched_by)
        for p in pairs
    ]
    assert summary == [
        ("차입금", "차입금", "key"),
        ("특수관계자와의거래", "특수관계자와의거래내역", "similar_title"),
        (None, "사업결합", None),
        ("매각예정자산", None, None),
    ]
    assert pairs[2].status == "added" and pairs[3].status == "removed"


def test_similar_title_matching_is_one_to_one_highest_ratio_first():
    prior = [note("특수관계자와의거래", []), note("특수관계자와의거래내역", [])]
    current = [note("특수관계자와의거래내역들", [])]
    pairs = match_notes(prior, current)
    assert pairs[0].prior.key == "특수관계자와의거래내역"  # ratio 0.95 beats 0.86
    assert pairs[0].matched_by == "similar_title"
    assert pairs[1].prior.key == "특수관계자와의거래" and pairs[1].current is None
    # below 0.85: not matched at all
    unrelated = match_notes([note("리스", [])], [note("차입금", [])])
    assert [p.status for p in unrelated] == ["added", "removed"]


# ---------------------------------------------------------------- line diff


def test_numbers_only_lines_are_told_apart_from_sentence_changes():
    prior = ["공통 줄", "단기차입금 | 3,000 | (200)", "회사는 지급보증을 제공하고 있습니다.", "끝"]
    current = ["공통 줄", "단기차입금 | 5,000 | -", "회사는 신규 한도약정을 체결하였습니다.", "끝"]
    diff = diff_lines(prior, current)
    by_ref = {e["ref"]: e for e in diff.entries}
    assert by_ref["P2"]["numbers_only"] and by_ref["C2"]["numbers_only"]
    assert by_ref["P2"]["pair"] == "C2" and by_ref["C2"]["pair"] == "P2"
    assert not by_ref["P3"]["numbers_only"] and not by_ref["C3"]["numbers_only"]
    assert diff.stats() == {"equal": 2, "text_added": 1, "text_removed": 1, "numbers_only": 1}
    assert diff.changed and not diff.truncated


def test_inserted_and_deleted_lines_carry_note_line_numbers():
    diff = diff_lines(["a", "b"], ["a", "x", "b", "y"])
    assert [(e["op"], e["ref"], e["text"]) for e in diff.entries] == [
        ("insert", "C2", "x"),
        ("insert", "C4", "y"),
    ]
    unchanged = diff_lines(["a"], ["a"])
    assert unchanged.entries == [] and not unchanged.changed


def test_max_lines_truncates_the_stored_lines_not_the_stats():
    diff = diff_lines([], [f"새 문장 {chr(0xAC00 + i)}" for i in range(10)], max_lines=3)
    assert len(diff.entries) == 3 and diff.truncated
    assert diff.text_added == 10
    result = compare_documents(
        [],
        [Section(f"{CONS} > 1. 신규 (연결)", "\n".join(f"줄{chr(0xAC00 + i)}" for i in range(9)))],
        options=DiffOptions(max_lines=4),
    )
    assert result["notes"][0]["truncated"] is True and len(result["notes"][0]["lines"]) == 4


# ---------------------------------------------------------------- numbers and tables


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("1,234", Decimal(1234)),
        ("(1,234)", Decimal(-1234)),
        ("△1,234", Decimal(-1234)),
        ("-1,234", Decimal(-1234)),
        ("-", Decimal(0)),
        ("0.035", Decimal("0.035")),
        ("12.5%", Decimal("12.5")),
        ("12345", Decimal(12345)),
        ("2025-12-23", None),
        ("2025.12.31", None),
        ("2025", None),
        ("(1,234", None),
        ("1,234 백만원", None),
        ("우리은행 외 28개 은행", None),
    ],
)
def test_parse_number(cell, expected):
    parsed = parse_number(cell)
    assert (parsed.value if parsed else None) == expected


SAMSUNG_LIKE = [
    "특수관계자거래에 대한 공시",
    "당기 | (단위 : 백만원)",
    "특수관계자 | 관계기업 | 공동기업",
    "매출 등, 특수관계자거래 | 110,512 | 67,330 | 96,962",
    "매입 등, 특수관계자거래 | 1,000 | (200) | -",
    "전기 | (단위 : 백만원)",
    "특수관계자 | 관계기업 | 공동기업",
    "매출 등, 특수관계자거래 | 90,000 | 67,330 | 96,962",
    "매입 등, 특수관계자거래 | 900 | △100 | 5",
    "약정에 대한 공시",
    "약정 금액 | 21,647,106",
    "계약체결일 | 2025-12-23",
]


def test_period_block_table_rows():
    rows = parse_table_rows(SAMSUNG_LIKE)
    got = [(r.table, r.label, r.occurrence, r.period, r.values) for r in rows]
    title = "특수관계자거래에 대한 공시"
    assert got == [
        (
            title,
            "매출 등, 특수관계자거래",
            0,
            "current",
            (Decimal(110512), Decimal(67330), Decimal(96962)),
        ),
        (
            title,
            "매입 등, 특수관계자거래",
            0,
            "current",
            (Decimal(1000), Decimal(-200), Decimal(0)),
        ),
        (
            title,
            "매출 등, 특수관계자거래",
            0,
            "prior",
            (Decimal(90000), Decimal(67330), Decimal(96962)),
        ),
        (title, "매입 등, 특수관계자거래", 0, "prior", (Decimal(900), Decimal(-100), Decimal(5))),
        ("약정에 대한 공시", "약정 금액", 0, None, (Decimal(21647106),)),
    ]  # the date row is not a number row
    assert rows[0].line == 3


def test_table_unit_from_period_marker_and_unit_line_and_reset_by_new_title():
    lines = [
        "특수관계자거래에 대한 공시",
        "당기 | (단위 : 백만원)",
        "매출 등 | 110,512",
        "전기 | (단위 : 백만원)",
        "매출 등 | 90,000",
        "차입금 내역",
        "(단위 : 천원)",
        "단기차입금 | 3,000",
        "단위 없는 표",
        "건수 | 12",
    ]
    rows = parse_table_rows(lines)
    assert [(r.table, r.period, r.unit) for r in rows] == [
        ("특수관계자거래에 대한 공시", "current", "백만원"),
        ("특수관계자거래에 대한 공시", "prior", "백만원"),
        ("차입금 내역", None, "천원"),
        ("단위 없는 표", None, None),  # the new title reset the unit
    ]
    prior = ["차입금 내역", "(단위 : 천원)", "단기차입금 | 2,000", "단위 없는 표", "건수 | 10"]
    changes = compare_rows(prior, lines[5:]).changes
    assert [(c["table"], c["unit"]) for c in changes] == [
        ("차입금 내역", "천원"),
        ("단위 없는 표", None),
    ]


def test_samsung_like_rows_carry_the_unit_of_their_period_block():
    rows = parse_table_rows(SAMSUNG_LIKE)
    assert [r.unit for r in rows] == ["백만원", "백만원", "백만원", "백만원", None]


def test_periods_written_as_rows_are_rows_not_block_markers():
    prior = ["손익 요약", "구분 | 매출 | 원가", "당기 | 1,000 | 800", "전기 | 900 | 700"]
    current = ["손익 요약", "구분 | 매출 | 원가", "당기 | 1,500 | 820", "전기 | 1,000 | 790"]
    rows = parse_table_rows(current)
    assert [(r.label, r.period, r.values) for r in rows] == [
        ("", "current", (Decimal(1500), Decimal(820))),
        ("", "prior", (Decimal(1000), Decimal(790))),
    ]
    changes = compare_rows(prior, current).changes
    assert [(c["kind"], c["prior_values"], c["current_values"]) for c in changes] == [
        ("prior_mismatch", ["1000", "800"], ["1000", "790"]),
        ("yoy", ["1000", "800"], ["1500", "820"]),
    ]
    assert changes[1]["cells"][0]["flags"] == ["large_change"]


def test_column_form_table_with_current_period_first():
    lines = [
        "리스부채의 만기",
        "구분 | 당기말 | 전기말",
        "1년 이내 | 1,000 | 800",
        "1년 초과 | 2,000 | 2,500",
        "(단위 : 백만원)",
        "역순 표",
        "구분 | 전기말 | 당기말",
        "1년 이내 | 7 | 9",
    ]
    rows = parse_table_rows(lines)
    assert [(r.label, r.period, r.values) for r in rows] == [
        ("1년 이내", "current", (Decimal(1000),)),
        ("1년 이내", "prior", (Decimal(800),)),
        ("1년 초과", "current", (Decimal(2000),)),
        ("1년 초과", "prior", (Decimal(2500),)),
    ]  # the "전기말 first" table is left out


# ---------------------------------------------------------------- number changes


def test_yoy_prior_mismatch_and_large_change():
    prior = [
        "차입금 내역",
        "당기 | (단위 : 백만원)",
        "단기차입금 | 3,000",
        "장기차입금 | 11,000",
        "사채 | 100",
        "전기 | (단위 : 백만원)",
        "단기차입금 | 2,800",
    ]
    current = [
        "차입금 내역",
        "당기 | (단위 : 백만원)",
        "단기차입금 | 3,900",
        "장기차입금 | 12,000",
        "리스부채 | 50",
        "전기 | (단위 : 백만원)",
        "단기차입금 | 3,000",
        "장기차입금 | 11,500",
    ]
    rows = compare_rows(prior, current)
    kinds = [(c["kind"], c["label"]) for c in rows.changes]
    assert kinds == [
        ("prior_mismatch", "장기차입금"),
        ("yoy", "단기차입금"),
        ("yoy", "장기차입금"),
        ("row_added", "리스부채"),
        ("row_removed", "사채"),
    ]
    mismatch = rows.changes[0]
    assert mismatch["prior_values"] == ["11000"] and mismatch["current_values"] == ["11500"]
    assert mismatch["flags"] == ["prior_mismatch"]
    assert (mismatch["prior_ref"], mismatch["current_ref"]) == ("P4", "C8")
    short = rows.changes[1]["cells"][0]
    assert short == {
        "position": 1,
        "prior": "3000",
        "current": "3900",
        "delta": "900",
        "change_pct": "30",
        "flags": ["large_change"],
    }  # 30.0% >= threshold 0.3
    assert rows.changes[2]["cells"][0]["change_pct"] == "9.1"
    assert rows.changes[2]["flags"] == []
    assert rows.count(kind="yoy", flag="large_change") == 1
    # the current report's "전기 단기차입금 3,000" equals the prior report's "당기": no mismatch
    assert all(c["label"] != "단기차입금" for c in rows.changes if c["kind"] == "prior_mismatch")
    assert rows.ambiguous_rows == 0


def test_large_change_threshold_sign_flip_and_zero():
    t = Decimal("0.3")
    assert is_large_change(Decimal(100), Decimal(130), t)
    assert not is_large_change(Decimal(100), Decimal(129), t)
    assert is_large_change(Decimal(100), Decimal(-10), Decimal(5))  # sign flip only
    assert is_large_change(Decimal(0), Decimal(5), Decimal(5))  # 0 -> non-zero
    assert not is_large_change(Decimal(-100), Decimal(-110), t)
    rows = compare_rows(
        ["표", "당기", "이익 | 100", "이자율 | 0.035", "비율 | 3.5%"],
        ["표", "당기", "이익 | -10", "이자율 | 0.05", "비율 | 7%"],
        threshold=Decimal(5),
    )
    flags = {c["label"]: c["flags"] for c in rows.changes}
    assert flags == {"이익": ["large_change"], "이자율": [], "비율": []}  # ratio rows never flagged


def test_mismatched_value_counts_are_listed_without_cells():
    rows = compare_rows(["표", "합계 | 1 | 2"], ["표", "합계 | 1 | 2 | 3"])
    assert rows.changes[0]["kind"] == "yoy"
    assert rows.changes[0]["cells"] == [] and rows.changes[0]["current_values"] == ["1", "2", "3"]


def test_repeated_rows_with_different_counts_are_ambiguous():
    prior = ["현황", "합계 | 100", "합계 | 200", "기타 | 5"]
    current = ["현황", "합계 | 100", "합계 | 250", "합계 | 300", "기타 | 6"]
    rows = compare_rows(prior, current)
    assert [(c["kind"], c["label"]) for c in rows.changes] == [("yoy", "기타")]
    assert rows.ambiguous_rows == 3
    same_count = compare_rows(prior, ["현황", "합계 | 100", "합계 | 250", "기타 | 5"])
    assert [(c["label"], c["occurrence"]) for c in same_count.changes] == [("합계", 1)]
    assert same_count.ambiguous_rows == 0


# ---------------------------------------------------------------- watchlist, score, document


def test_watchlist_score_and_sorting():
    assert note_score("unchanged", watchlist=True) == 0
    assert note_score("changed", watchlist=True, text_lines=1) == WATCHLIST_WEIGHT + 1
    assert note_score("added", watchlist=False, text_lines=500) == 40 + 50  # text lines capped
    prior = [
        Section(f"{CONS} > 3. 유형자산 (연결)", "표\n당기\n토지 | 100\n건물 | 50"),
        Section(f"{CONS} > 16. 우발부채와 약정사항 (연결)", "소송은 없습니다."),
        Section(f"{CONS} > 20. 리스 (연결)", "같음"),
    ]
    current = [
        Section(f"{CONS} > 3. 유형자산 (연결)", "표\n당기\n토지 | 300\n건물 | 90"),
        Section(f"{CONS} > 16. 우발부채와 약정사항 (연결)", "소송이 제기되었습니다."),
        Section(f"{CONS} > 20. 리스 (연결)", "같음"),
        Section(f"{CONS} > 30. 보고기간후사건 (연결)", "신규 사건"),
    ]
    result = compare_documents(prior, current)
    order = [(n["index"], n["key"], n["status"], n["watchlist"]) for n in result["notes"]]
    assert order == [
        (0, "보고기간후사건", "added", True),
        (1, "우발부채와약정사항", "changed", True),
        (2, "유형자산", "changed", False),
        (3, "리스", "unchanged", False),
    ]
    scores = [n["score"] for n in result["notes"]]
    assert scores == sorted(scores, reverse=True)
    custom = compare_documents(prior, current, options=DiffOptions(watchlist=("유형자산",)))
    assert custom["notes"][0]["key"] == "유형자산"


FAKE_PRIOR, FAKE_CURRENT = fake_data.FAKE_ANNUAL_PAIR


@pytest.mark.parametrize(
    ("fixture", "rcept_no"),
    [
        ("synthetic_annual_report_prior.xml", FAKE_PRIOR),
        ("synthetic_annual_report_current.xml", FAKE_CURRENT),
    ],
)
def test_fixture_matches_fake_data(fixture, rcept_no):
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    without_comment = re.sub(r"<!-- SYNTHETIC[^\n]*-->\n", "", text)
    assert without_comment == fake_data.build_document_xml(rcept_no).decode("utf-8")


def test_synthetic_pair_detects_every_kind_of_change():
    prior = parse_document_xml((FIXTURES / "synthetic_annual_report_prior.xml").read_bytes())
    current = parse_document_xml((FIXTURES / "synthetic_annual_report_current.xml").read_bytes())
    result = compare_documents(prior.sections, current.sections)
    json.dumps(result, ensure_ascii=False)  # JSON-serializable as stored
    summary = result["summary"]
    assert summary["status_counts"] == {"changed": 2, "added": 1, "removed": 1, "unchanged": 3}
    assert summary["prior_mismatch"] == 1 and summary["large_change"] == 1
    notes = {n["key"]: n for n in result["notes"]}
    assert notes["사업결합"]["status"] == "added"
    assert notes["매각예정비유동자산"]["status"] == "removed"
    contingency = notes["우발부채와약정사항"]
    assert contingency["watchlist"] and contingency["index"] == 0
    assert contingency["line_stats"]["text_added"] == 1
    assert contingency["line_stats"]["text_removed"] == 1
    inserted = [
        e["text"] for e in contingency["lines"] if e["op"] == "insert" and not e["numbers_only"]
    ]
    assert inserted == [fake_data._CONTINGENCY_SENTENCE_2025]
    borrowings = notes["차입금"]
    assert borrowings["counts"]["large_change"] == 1 and borrowings["counts"]["prior_mismatch"] == 1
    assert all(e["numbers_only"] for e in borrowings["lines"])
