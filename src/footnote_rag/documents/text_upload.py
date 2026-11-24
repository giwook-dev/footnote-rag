"""TXT·Markdown 업로드를 섹션으로 나누는 파서.

역할: UTF-8(BOM 허용) 또는 CP949로 디코드하고, Markdown ``#`` 제목을 목차 경로로 쓴다(코드 블록 안은 무시).
주요 요소: ``decode_upload``, ``parse_text_document``, ``ALLOWED_EXTENSIONS``, ``UploadParseError``.
호출 관계: ``documents.service.create_text_upload``(확장자 검사)와 ``jobs.handlers.handle_ingest_text``가 쓴다.
학습 문서: docs/study/02-request-walkthrough.md

TXT / Markdown uploads -> sections. Markdown ``#`` headings become the section path.
"""

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
