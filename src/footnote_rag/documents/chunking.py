"""Section-aware chunking.

Chunks never cross section boundaries, so every chunk keeps its ``section_path``.
Inside a section, text is packed line by line (table rows are lines, so tables are cut
on row boundaries) up to ``size`` characters, with roughly ``overlap`` characters of the
previous chunk's tail carried into the next one. Lines longer than ``size`` are split
with a sliding window.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChunkDraft:
    section_path: str
    chunk_index: int  # position within the whole document
    text: str


def _split_long_line(line: str, size: int, overlap: int) -> list[str]:
    step = max(1, size - overlap)
    return [line[i : i + size] for i in range(0, max(1, len(line) - overlap), step)]


def _tail(lines: list[str], overlap: int) -> list[str]:
    """Last whole lines totalling at most ``overlap`` characters."""
    out: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > overlap:
            break
        out.insert(0, line)
        total += len(line) + 1
    return out


def chunk_section_text(text: str, size: int = 1500, overlap: int = 200) -> list[str]:
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    lines: list[str] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        lines.extend(_split_long_line(raw, size, overlap) if len(raw) > size else [raw])

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        added = len(line) + (1 if current else 0)
        if current and length + added > size:
            chunks.append("\n".join(current))
            current = _tail(current, overlap)
            length = len("\n".join(current))
            added = len(line) + (1 if current else 0)
            if current and length + added > size:
                current, length, added = [], 0, len(line)
        current.append(line)
        length += added
    if current:
        chunks.append("\n".join(current))
    return chunks


def chunk_sections(
    sections: list[tuple[str, str]], size: int = 1500, overlap: int = 200
) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    for path, text in sections:
        for piece in chunk_section_text(text, size, overlap):
            drafts.append(ChunkDraft(section_path=path, chunk_index=len(drafts), text=piece))
    return drafts
