"""Claude citations -> numbered footnotes. Pure functions.

Rules:
* text blocks are concatenated in order; non-text blocks (thinking, fallback, ...) are ignored
* after a text block that carries citations, ``[n]`` markers are appended (one per distinct
  cited chunk, in citation order)
* numbers are assigned by first appearance, starting at 1; citing the same chunk again
  reuses its number
* citations are resolved by ``source`` (``doc:{document_id}#chunk:{chunk_id}``), falling back
  to ``search_result_index``; unresolvable citations are dropped
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
