"""answering.number_check: numbers of cited sentences must appear in the cited chunks. No DB."""

from __future__ import annotations

import pytest

from footnote_rag.answering.number_check import (
    contains_number,
    extract_numbers,
    ungrounded_numbers,
)

CHUNK = (
    "보고기간말 현재 리스부채는 1,234,567백만원이며 할인율은 연 4.25%입니다. "
    "소송사건은 3건이고 소송가액은 1,200."
)
OTHER = "자산총계는 98,765백만원입니다."


@pytest.fixture(scope="session")
def _database() -> None:
    """Override conftest's autouse DB setup: pure functions only."""


@pytest.fixture
def _clean_jobs() -> None:
    """Override conftest's autouse job cleanup: no database is needed here."""


def cited(text: str, *indices: int) -> dict:
    return {
        "type": "text",
        "text": text,
        "citations": [
            {"type": "search_result_location", "search_result_index": i, "cited_text": "x"}
            for i in indices
        ],
    }


def values(blocks, texts=(CHUNK, OTHER), question="리스부채는 얼마인가요?") -> list[str]:
    return [u.value for u in ungrounded_numbers(blocks, list(texts), question)]


def test_extract_normalizes_commas_and_skips_short_integers_and_markers():
    found = extract_numbers("3개 부문, 16. 우발부채[12] 1,234,567백만원과 4.25%, 0.5배, 2025년")
    assert found == [("1,234,567", "1234567"), ("4.25", "4.25"), ("0.5", "0.5"), ("2025", "2025")]
    # a trailing comma of an enumeration is not part of the number
    assert extract_numbers("100, 200") == [("100", "100"), ("200", "200")]


def test_number_boundaries():
    assert contains_number("금액 123백만원", "123")
    assert not contains_number("금액 123백만원", "12")  # 12 inside 123
    assert not contains_number("금액 1123", "123")
    assert not contains_number("비율 1.123", "123")  # fraction digits of another number
    assert not contains_number("비율 123.5", "123")  # integer part of a decimal
    assert contains_number("소송가액은 1200.", "1200")  # sentence-final period is not a decimal
    assert contains_number("비율은 4.25%", "4.25")


def test_comma_formatting_does_not_matter():
    assert values([cited("리스부채는 1234567백만원입니다.", 0)]) == []
    assert values([cited("리스부채는 1,234,567백만원입니다.", 0)]) == []
    assert values([cited("소송가액은 1,200백만원입니다.", 0)]) == []


def test_decimals_and_percent_match():
    assert values([cited("할인율은 연 4.25%입니다.", 0)]) == []
    assert values([cited("할인율은 연 4.3%입니다.", 0)]) == ["4.3"]


# hyundai-05 on 2026-11-25: the audit-hours table cell is written "39.012" (dot as the thousands
# separator) and the model answered "39,012"; every config sent it to review (ungrounded_number).
AUDIT_TABLE = "감사보수 | 감사시간\n4,250 | 39.012\n합계 1.234.567"


@pytest.mark.parametrize(
    ("source", "answer"),
    [("39.012", "39,012"), ("1.234.567", "1,234,567"), ("시간 39.012.", "39012")],
)
def test_dot_thousands_separator_in_the_source_is_grounded(source, answer):
    assert values([cited(f"감사시간은 {answer}시간입니다.", 0)], texts=(source,)) == []


@pytest.mark.parametrize(
    ("source", "answer"),
    [("1.5", "15"), ("39.01", "3,901"), ("1.2345", "12,345"), ("1234.567", "1,234,567")],
    ids=["one-digit-fraction", "two-digit-group", "four-digit-group", "four-digit-lead"],
)
def test_other_dotted_numbers_stay_ungrounded(source, answer):
    assert not contains_number(source, answer.replace(",", ""))


def test_dot_thousands_on_a_real_table():
    blocks = [cited("감사시간은 39,012시간, 합계는 1,234,567입니다.", 0)]
    assert values(blocks, texts=(AUDIT_TABLE,)) == []
    # the dotted text itself still matches as written
    assert values([cited("감사시간은 39.012시간입니다.", 0)], texts=(AUDIT_TABLE,)) == []
    # a number the table does not have is still reported
    assert values([cited("감사시간은 39,013시간입니다.", 0)], texts=(AUDIT_TABLE,)) == ["39013"]


def test_dot_thousands_known_limitation():
    """Widening only: a decimal with a three-digit fraction also grounds the digits without the
    dot (documented in the module docstring)."""
    assert contains_number("원주율 3.141", "3141")
    assert contains_number("원주율 3.141", "3.141")


def test_converted_amount_is_ungrounded():
    # 1,234,567백만원 rewritten as 약 1.2조 원: no unit conversion on purpose.
    blocks = [cited("리스부채는 약 1.2조 원입니다.", 0)]
    found = ungrounded_numbers(blocks, [CHUNK, OTHER], "리스부채는 얼마인가요?")
    assert [(u.text, u.value, u.block_index) for u in found] == [("1.2", "1.2", 0)]
    assert values([cited("리스부채는 1,234,568백만원입니다.", 0)]) == ["1234568"]


def test_only_the_cited_chunks_count():
    # 98,765 is in OTHER, but the sentence cites CHUNK only
    assert values([cited("자산총계는 98,765백만원입니다.", 0)]) == ["98765"]
    assert values([cited("자산총계는 98,765백만원입니다.", 0, 1)]) == []


def test_short_integers_question_numbers_and_markers_are_skipped():
    assert values([cited("소송은 3건이고 주석 16번에 있습니다.[1]", 0)]) == []
    assert values([cited("2030년 목표는 없습니다.", 0)], question="2030년 매출 목표는?") == []
    assert values([cited("2030년 목표는 없습니다.", 0)]) == ["2030"]


def test_uncited_and_non_text_blocks_are_skipped():
    blocks = [
        {"type": "text", "text": "검색된 문서에 따르면 9,999백만원 "},  # no citations
        {"type": "thinking", "thinking": "7,777"},
        cited("리스부채는 1,234,567백만원입니다.", 0),
        cited("범위 밖 인용 5,555", 9),  # unresolvable citation -> treated as uncited
    ]
    assert values(blocks) == []


def test_source_ids_resolve_before_the_index():
    block = {
        "type": "text",
        "text": "자산총계는 98,765백만원입니다.",
        "citations": [
            {"type": "search_result_location", "source": "doc:b", "search_result_index": 0}
        ],
    }
    found = ungrounded_numbers([block], [CHUNK, OTHER], "q", sources=["doc:a", "doc:b"])
    assert found == []
    wrong = {**block, "citations": [{**block["citations"][0], "source": "doc:a"}]}
    found = ungrounded_numbers([wrong], [CHUNK, OTHER], "q", sources=["doc:a", "doc:b"])
    assert [u.sources for u in found] == [("doc:a",)]
