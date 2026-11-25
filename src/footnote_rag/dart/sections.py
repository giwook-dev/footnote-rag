"""목차 경로(section_path)로 연결/별도 재무제표 주석을 가리고, 두 주석의 대응 관계를 판별하는 순수 함수.

역할: 사업보고서에는 같은 주석이 ``3. 연결재무제표 주석``과 ``5. 재무제표 주석``(별도) 아래에 두 번 나온다.
번호(``35.`` vs ``33.``)나 제목 문구(``연결재무제표 작성기준 …`` vs ``재무제표 작성기준 …``)가 달라도 같은
주석인지 비교할 키를 만든다.
주요 요소: ``statement_scope``, ``note_key``, ``is_counterpart``, ``scope_label``.
호출 관계: ``retrieval.search.prefer_statement_scope``(검색 재정렬), ``answering.prompt``(근거 제목의
``[연결]``/``[별도]`` 표시), ``evals.run``(대응 주석 인정 지표)이 호출한다.
학습 문서: docs/study/05-rag-retrieval.md, docs/adr/0009-prefer-consolidated-notes.md

Consolidated vs separate financial-statement notes, from ``section_path`` only.

Examples (real 2025 annual-report paths)::

    III. 재무에 관한 사항 > 3. 연결재무제표 주석 > 12. 차입금 (연결)   -> consolidated, "차입금"
    III. 재무에 관한 사항 > 5. 재무제표 주석 > 12. 차입금               -> separate,     "차입금"
    (첨부)재무제표 > 주석 > 12. 리스                                     -> separate,     "리스"
    II. 사업의 내용 > 1. 사업의 개요                                     -> None,         None
"""

from __future__ import annotations

import re
from typing import Literal

StatementScope = Literal["consolidated", "separate"]

CONSOLIDATED: StatementScope = "consolidated"
SEPARATE: StatementScope = "separate"

PATH_SEPARATOR = " > "  # same as dart.parser.PATH_SEP
SCOPE_LABELS: dict[StatementScope, str] = {CONSOLIDATED: "[연결]", SEPARATE: "[별도]"}

# Leading heading number: "12. ", "7-1. ", "2.1. ", "3) ", "(1) ". A separator ("." or ")") is
# required so a title that starts with a year ("2025년 …") keeps its digits.
_NUMBER_RE = re.compile(r"^\s*\(?\d+(?:[-.]\d+)*[.)]\s*")
_CONSOLIDATED_SUFFIX_RE = re.compile(r"\(\s*연결\s*\)\s*$")
_SPACE_RE = re.compile(r"\s+")


def _parts(section_path: str) -> list[str]:
    return [p.strip() for p in section_path.split(PATH_SEPARATOR)]


def _compact(title: str) -> str:
    return _SPACE_RE.sub("", _NUMBER_RE.sub("", title, count=1))


def _scope_of(statement_title: str) -> StatementScope:
    return CONSOLIDATED if "연결" in statement_title else SEPARATE


def _notes_container(parts: list[str]) -> tuple[StatementScope, int] | None:
    """(scope, index of the notes heading) for the first notes heading in ``parts``.

    A notes heading is ``…재무제표 주석`` (``3. 연결재무제표 주석``, ``5. 재무제표 주석``,
    ``별도재무제표 주석``) or a bare ``주석`` directly under a ``…재무제표`` heading
    (``(첨부)재무제표 > 주석``, as in audit reports).
    """
    for i, part in enumerate(parts):
        name = _compact(part)
        if name.endswith("주석") and "재무제표" in name:
            return _scope_of(name), i
        if name == "주석" and i > 0 and "재무제표" in parts[i - 1]:
            return _scope_of(parts[i - 1]), i
    return None


def statement_scope(section_path: str) -> StatementScope | None:
    """``"consolidated"`` under consolidated notes, ``"separate"`` under separate notes, else None."""
    found = _notes_container(_parts(section_path))
    return found[0] if found else None


def note_key(section_path: str) -> str | None:
    """Comparison key of the note heading right below the notes heading, or None.

    Drops the leading number, a trailing ``(연결)``, all whitespace and the word "연결"
    ("연결재무제표"/"별도재무제표" -> "재무제표"), so the consolidated and separate copies of one
    note get the same key: ``12. 차입금 (연결)`` -> ``차입금``,
    ``2. 연결재무제표 작성기준 및 중요한 회계정책 (연결)`` -> ``재무제표작성기준및중요한회계정책``.
    Deeper path components (sub-headings inside the note) are ignored.
    """
    parts = _parts(section_path)
    found = _notes_container(parts)
    if found is None or found[1] + 1 >= len(parts):
        return None
    title = _NUMBER_RE.sub("", parts[found[1] + 1], count=1)
    title = _CONSOLIDATED_SUFFIX_RE.sub("", title)
    key = _SPACE_RE.sub("", title)
    key = key.replace("연결재무제표", "재무제표").replace("별도재무제표", "재무제표")
    key = key.replace("연결", "")
    return key or None


def is_counterpart(path_a: str, path_b: str) -> bool:
    """One path is a consolidated note and the other the separate note with the same key.

    The caller guarantees both paths come from the same document (or, in the Eval, that the
    comparison is per expected section, which carries no document id either).
    """
    scope_a, scope_b = statement_scope(path_a), statement_scope(path_b)
    if scope_a is None or scope_b is None or scope_a == scope_b:
        return False
    key = note_key(path_a)
    return key is not None and key == note_key(path_b)


def scope_label(section_path: str) -> str:
    """``"[연결]"`` / ``"[별도]"`` for notes, ``""`` for anything else."""
    scope = statement_scope(section_path)
    return SCOPE_LABELS[scope] if scope else ""


def labeled_section_path(section_path: str) -> str:
    """``"[연결] III. … > 12. 차입금 (연결)"``; unlabeled paths are returned unchanged."""
    label = scope_label(section_path)
    return f"{label} {section_path}" if label else section_path
