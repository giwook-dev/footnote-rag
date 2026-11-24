"""TXT / Markdown uploads -> sections. Markdown ``#`` headings become the section path."""

from __future__ import annotations

import re

from footnote_rag.dart.parser import PATH_SEP, Section

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
ALLOWED_EXTENSIONS = (".txt", ".md", ".markdown")


class UploadParseError(Exception):
    pass


def decode_upload(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UploadParseError("file is not UTF-8 or CP949 text")


def parse_text_document(text: str, filename: str) -> list[Section]:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stack: list[tuple[int, str]] = []
    order: list[str] = []
    parts: dict[str, list[str]] = {}
    in_code = False

    def current_path() -> str:
        return PATH_SEP.join(t for _, t in stack) if stack else stem

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            continue
        if not line.strip():
            continue
        path = current_path()
        if path not in parts:
            order.append(path)
            parts[path] = []
        parts[path].append(line.rstrip())

    sections = [Section(path=p, text="\n".join(parts[p])) for p in order]
    if not any(s.text.strip() for s in sections):
        raise UploadParseError("document has no text")
    return sections
