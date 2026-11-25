"""답변 숫자 근거 검증: 인용이 달린 문장의 숫자가 그 문장이 인용한 검색 청크 원문에 실제로 있는지 확인하는 순수 함수.

역할: 모델이 직접 계산하거나 단위를 환산한 숫자(예: ``1,234,567백만원`` → ``약 1.2조 원``)를 잡아낸다. 계산은 모델이
아니라 코드가 해야 한다는 원칙이라 단위 환산은 일부러 하지 않는다(docs/adr/0009-local-cascade-and-number-grounding.md).
주요 요소: ``ungrounded_numbers``, ``UngroundedNumber``, ``extract_numbers``, ``contains_number``.
호출 관계: ``answering.service.ask_question``이 호출하고, 결과가 있으면 ``review_reasons``가 ``ungrounded_number``를 붙인다.
I/O가 없어 tests/test_number_check.py로 단위 테스트한다.

Rules:
* only ``type == "text"`` blocks with at least one resolvable ``search_result_location`` citation are
  checked (uncited blocks are the ``no_citations`` rule's business)
* numbers are ``\\d[\\d,]*(?:\\.\\d+)?`` with the commas removed; skipped: integers of 1-2 digits
  without a decimal point (``3개 부문``, ``16. 우발부채``), numbers already in the question, and
  footnote markers ``[n]``
* a number is grounded when the cited chunks' text (commas removed) contains the same digit string
  on a number boundary: ``12`` does not match inside ``123`` or ``1.12``; a ``.`` only counts as part
  of a number when a digit follows it, so a sentence-final ``1,200.`` still matches ``1200``
* provider-agnostic: works on Claude (``api_citation``) and local (``model_reference``) blocks alike
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
FOOTNOTE_MARKER_RE = re.compile(r"\[\d+\]")
MAX_REPORTED = 10  # numbers kept in logs and audit metadata


@dataclass(frozen=True)
class UngroundedNumber:
    """A number of a cited sentence that none of the sentence's cited chunks contains."""

    text: str  # as written in the answer, e.g. "1.2" or "1,234"
    value: str  # commas removed, e.g. "1234"
    block_index: int  # index into the LLM ``content`` list
    sources: tuple[str, ...]  # ``source`` ids of the cited chunks (``doc:…#chunk:…``)


def _normalize(text: str) -> str:
    return text.replace(",", "")


def extract_numbers(text: str) -> list[tuple[str, str]]:
    """``(as written, normalized)`` numbers worth checking in ``text`` (markers and 1-2 digit
    integers skipped)."""
    out: list[tuple[str, str]] = []
    for match in NUMBER_RE.finditer(FOOTNOTE_MARKER_RE.sub(" ", text)):
        raw = match.group(0).rstrip(",")
        value = _normalize(raw)
        if "." not in value and len(value) <= 2:
            continue
        out.append((raw, value))
    return out


def contains_number(haystack: str, value: str) -> bool:
    """``value`` (normalized) occurs in ``haystack`` (already normalized) on a number boundary."""
    pattern = rf"(?<!\d)(?<!\d\.){re.escape(value)}(?!\d)(?!\.\d)"
    return re.search(pattern, haystack) is not None


def _cited_indices(
    citations: Sequence[dict[str, Any]], texts: Sequence[str], sources: Sequence[str] | None
) -> list[int]:
    """Indices of the cited search results, resolved like ``footnotes.build_footnotes``:
    ``source`` first (when ``sources`` is given), then ``search_result_index``."""
    by_source = {s: i for i, s in enumerate(sources or [])}
    indices: list[int] = []
    for citation in citations:
        if citation.get("type") != "search_result_location":
            continue
        idx = by_source.get(str(citation.get("source", "")))
        if idx is None:
            raw = citation.get("search_result_index")
            idx = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        if idx is not None and 0 <= idx < len(texts) and idx not in indices:
            indices.append(idx)
    return indices


def ungrounded_numbers(
    content_blocks: Sequence[dict[str, Any]],
    search_result_texts: Sequence[str],
    question: str,
    *,
    sources: Sequence[str] | None = None,
) -> list[UngroundedNumber]:
    """Numbers of cited text blocks that their cited chunks do not contain (in answer order).

    ``search_result_texts[i]`` is the text of the search result ``i`` (``search_result_index``);
    ``sources[i]``, when given, is its ``source`` id. No unit conversion: a converted amount is
    reported on purpose.
    """
    asked = {value for _raw, value in extract_numbers(question)}
    normalized = [_normalize(t) for t in search_result_texts]
    out: list[UngroundedNumber] = []
    for block_index, block in enumerate(content_blocks):
        if block.get("type") != "text":
            continue
        indices = _cited_indices(block.get("citations") or [], search_result_texts, sources)
        if not indices:
            continue
        cited_sources = tuple(
            sources[i] if sources is not None and i < len(sources) else f"#{i}" for i in indices
        )
        for raw, value in extract_numbers(str(block.get("text", ""))):
            if value in asked:
                continue
            if any(contains_number(normalized[i], value) for i in indices):
                continue
            out.append(UngroundedNumber(raw, value, block_index, cited_sources))
    return out
