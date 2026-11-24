"""Claude 응답의 citations를 번호 각주 ``[n]``으로 바꾸는 순수 함수.

역할: text 블록을 이어 붙이고 인용이 달린 블록 뒤에 ``[n]``을 붙이며, 각주 목록(문서·목차 경로·인용문)을 만든다.
주요 요소: ``build_footnotes``, ``SourceRef``(검색 청크 메타데이터), ``FootnotedAnswer``.
호출 관계: ``answering.service.ask_question``이 호출한다. I/O가 없어 tests/test_footnotes.py로 단위 테스트한다.
학습 문서: docs/study/06-claude-citations.md (각주 번호 매기기 알고리즘)

Claude citations -> numbered footnotes. Pure functions.

Rules:
* text blocks are concatenated in order; non-text blocks (thinking, fallback, ...) are ignored
* after a text block that carries citations, ``[n]`` markers are appended (one per distinct
  cited chunk, in citation order)
* numbers are assigned by first appearance, starting at 1; citing the same chunk again
  reuses its number
* citations are resolved by ``source`` (``doc:{document_id}#chunk:{chunk_id}``), falling back
  to ``search_result_index``; unresolvable citations are dropped

``build_footnotes`` is shared by every provider: the local (Ollama) client emits the same
wire-shaped text blocks with ``search_result_location`` citations (one block per sentence),
so numbering is identical for Claude and local answers. Local models have no Citations API,
so their ``cited_text`` comes from ``select_cited_text`` (approximate character 2-gram match).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CITED_TEXT_MAX_CHARS = 300
# Sentence boundaries: whitespace after . ! ? (Korean "…다." included) or any line break
# (table rows and list items are one "sentence" each).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|\s*\n+\s*")
_WHITESPACE_RE = re.compile(r"\s+")


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part and part.strip()]


def _bigrams(text: str) -> set[str]:
    compact = _WHITESPACE_RE.sub("", text)
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def select_cited_text(chunk_text: str, sentence: str, limit: int = CITED_TEXT_MAX_CHARS) -> str:
    """The chunk sentence sharing the most character 2-grams with ``sentence``, cut to ``limit``.

    Ties go to the earlier chunk sentence. Used for local models, which return only source
    numbers, so the quote is an approximation rather than the exact span the model read.
    """
    candidates = split_sentences(chunk_text)
    if not candidates:
        return chunk_text.strip()[:limit]
    target = _bigrams(sentence)
    best = max(candidates, key=lambda c: len(_bigrams(c) & target))
    return best[:limit]


@dataclass(frozen=True)
class SourceRef:
    source: str
    chunk_id: str
    document_id: str
    document_title: str
    corp_name: str | None
    section_path: str
    dart_url: str | None


@dataclass
class FootnotedAnswer:
    text: str
    footnotes: list[dict[str, Any]]


def build_footnotes(content: list[dict[str, Any]], sources: list[SourceRef]) -> FootnotedAnswer:
    by_source = {s.source: s for s in sources}
    numbers: dict[str, int] = {}
    footnotes: list[dict[str, Any]] = []
    parts: list[str] = []

    for block in content:
        if block.get("type") != "text":
            continue
        parts.append(str(block.get("text", "")))
        markers: list[int] = []
        for citation in block.get("citations") or []:
            if citation.get("type") != "search_result_location":
                continue
            ref = by_source.get(str(citation.get("source", "")))
            if ref is None:
                idx = citation.get("search_result_index")
                if isinstance(idx, int) and 0 <= idx < len(sources):
                    ref = sources[idx]
            if ref is None:
                continue
            # 번호는 청크 기준: 처음 보는 청크면 새 번호와 각주 항목을 만들고, 이미 본 청크면 번호만 재사용한다.
            if ref.chunk_id not in numbers:
                n = len(numbers) + 1
                numbers[ref.chunk_id] = n
                footnotes.append(
                    {
                        "n": n,
                        "document_id": ref.document_id,
                        "document_title": ref.document_title,
                        "corp_name": ref.corp_name,
                        "section_path": ref.section_path,
                        "chunk_id": ref.chunk_id,
                        "cited_text": str(citation.get("cited_text", "")),
                        "dart_url": ref.dart_url,
                    }
                )
            n = numbers[ref.chunk_id]
            if n not in markers:
                markers.append(n)
        if markers:
            parts.append("".join(f"[{n}]" for n in markers))

    return FootnotedAnswer(text="".join(parts).strip(), footnotes=footnotes)
