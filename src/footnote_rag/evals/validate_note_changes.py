"""주석 변동 정답 데이터(``evals/note_changes.jsonl``) 검증기: 인용문이 해당 문서의 파싱된 주석에 실제로 있는지 확인한다.

역할: 각 항목의 인용문·값을 캐시된 원문 ZIP(``data/dart/{rcept_no}.zip``)의 주석 텍스트(``comparison.notes.extract_notes``)
에서 찾는다. 공백 정규화는 ``evals.validate``와 같다(줄바꿈 → 공백, ``normalize_ws``). 지어낸 인용문이면 실패한다.
``_example`` 행은 건너뛴다.
주요 요소: ``NoteChangeItem``, ``load_note_changes``, ``validate_note_changes``, ``note_text``, ``KINDS``, ``main``.
호출 관계: ``dart.parser.parse_document_zip``과 ``comparison.notes.extract_notes``를 호출한다. ``evals.note_diff``가
``load_note_changes``·``NoteChangeItem``을 쓴다.
학습 문서: docs/study/11-note-change-analysis.md

    python -m footnote_rag.evals.validate_note_changes [--dataset evals/note_changes.jsonl]
                                                       [--zip-dir data/dart] [--no-coverage]

Row schema: ``{id, prior_rcept_no, current_rcept_no, scope, note_key, kind, current_quote,
prior_quote}``; ``kind`` is one of ``KINDS``; ``number_changed`` rows add ``row_label``,
``prior_value`` and ``current_value``.

* ``line_added``: ``current_quote`` is in the current note and not in the prior note
* ``line_removed``: ``prior_quote`` is in the prior note and not in the current note
* ``number_changed``: ``prior_value`` is in the prior note and ``current_value`` in the current note
* ``note_added``: the current report has the note, the prior report does not (``current_quote``
  must be in it when given)
* ``note_removed``: the other way round (``prior_quote`` likewise)

Coverage (when the file has real rows): >= 8 rows, every company of ``REAL_PAIRS``, and at least
half of the rows on a watchlist note (``NOTE_DIFF_WATCHLIST``).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from footnote_rag.comparison.notes import (
    DEFAULT_WATCHLIST,
    SCOPES,
    Note,
    extract_notes,
    is_watchlist,
)
from footnote_rag.dart.parser import DartParseError, Section, normalize_ws, parse_document_zip

DEFAULT_NOTE_CHANGES = Path("evals/note_changes.jsonl")
KINDS = ("line_added", "line_removed", "number_changed", "note_added", "note_removed")
# (company, prior rcept_no = 2024 사업연도, current rcept_no = 2025 사업연도), from OpenDART list.json.
REAL_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("삼성전자", "20250311001085", "20260310002820"),
    ("NAVER", "20250318000645", "20260313001021"),
    ("현대자동차", "20250312001148", "20260318001394"),
)
MIN_ROWS = 8


@dataclass
class NoteChangeItem:
    id: str
    prior_rcept_no: str
    current_rcept_no: str
    scope: str
    note_key: str
    kind: str
    current_quote: str | None = None
    prior_quote: str | None = None
    row_label: str | None = None
    prior_value: str | None = None
    current_value: str | None = None
    example: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def _opt(value: Any) -> str | None:
    return None if value is None or str(value).strip() == "" else str(value)


def item_from_row(row: dict[str, Any]) -> NoteChangeItem:
    return NoteChangeItem(
        id=str(row.get("id", "")),
        prior_rcept_no=str(row.get("prior_rcept_no", "")),
        current_rcept_no=str(row.get("current_rcept_no", "")),
        scope=str(row.get("scope") or "consolidated"),
        note_key=str(row.get("note_key", "")),
        kind=str(row.get("kind", "")),
        current_quote=_opt(row.get("current_quote")),
        prior_quote=_opt(row.get("prior_quote")),
        row_label=_opt(row.get("row_label")),
        prior_value=_opt(row.get("prior_value")),
        current_value=_opt(row.get("current_value")),
        example=bool(row.get("_example")),
        raw=row,
    )


def load_note_changes(path: Path) -> list[NoteChangeItem]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(item_from_row(json.loads(line)))
    return items


def _norm(text: str) -> str:
    """Same whitespace normalization as ``evals.validate``."""
    return normalize_ws(text.replace("\n", " "))


def note_text(note: Note | None) -> str:
    return _norm("\n".join(note.lines)) if note is not None else ""


@dataclass
class NoteChangeReport:
    checked: int = 0
    skipped_examples: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class _Notes:
    """Parsed notes per (rcept_no, scope), from ``{zip_dir}/{rcept_no}.zip``."""

    def __init__(self, zip_dir: Path) -> None:
        self.zip_dir = zip_dir
        self._sections: dict[str, list[Section]] = {}

    def get(self, rcept_no: str, scope: str, key: str) -> Note | None:
        if rcept_no not in self._sections:
            path = self.zip_dir / f"{rcept_no}.zip"
            if not path.exists():
                raise FileNotFoundError(
                    f"{path} missing - download it first: python -m footnote_rag.cli dart-fetch "
                    f"--rcept-no {rcept_no}"
                )
            self._sections[rcept_no] = parse_document_zip(path.read_bytes(), rcept_no).sections
        notes = extract_notes(self._sections[rcept_no], scope)  # type: ignore[arg-type]
        return next((n for n in notes if n.key == key), None)


def _contains(haystack: str, needle: str | None) -> bool:
    return bool(needle) and _norm(needle) in haystack


def _check(item: NoteChangeItem, prior: Note | None, current: Note | None) -> list[str]:
    p, c = note_text(prior), note_text(current)
    k = item.kind
    if k == "line_added":
        if not item.current_quote:
            return ["line_added needs current_quote"]
        if not _contains(c, item.current_quote):
            return [f"current_quote not in the current note: {item.current_quote[:60]!r}"]
        if _contains(p, item.current_quote):
            return ["current_quote is also in the prior note (not an added line)"]
    elif k == "line_removed":
        if not item.prior_quote:
            return ["line_removed needs prior_quote"]
        if not _contains(p, item.prior_quote):
            return [f"prior_quote not in the prior note: {item.prior_quote[:60]!r}"]
        if _contains(c, item.prior_quote):
            return ["prior_quote is also in the current note (not a removed line)"]
    elif k == "number_changed":
        missing = [f for f in ("row_label", "prior_value", "current_value") if not getattr(item, f)]
        if missing:
            return [f"number_changed needs {', '.join(missing)}"]
        errors = []
        if not _contains(p, item.prior_value):
            errors.append(f"prior_value {item.prior_value!r} not in the prior note")
        if not _contains(c, item.current_value):
            errors.append(f"current_value {item.current_value!r} not in the current note")
        return errors
    elif k == "note_added":
        if current is None:
            return ["note not found in the current report"]
        if prior is not None:
            return ["note also exists in the prior report (not an added note)"]
        if item.current_quote and not _contains(c, item.current_quote):
            return [f"current_quote not in the current note: {item.current_quote[:60]!r}"]
    elif k == "note_removed":
        if prior is None:
            return ["note not found in the prior report"]
        if current is not None:
            return ["note also exists in the current report (not a removed note)"]
        if item.prior_quote and not _contains(p, item.prior_quote):
            return [f"prior_quote not in the prior note: {item.prior_quote[:60]!r}"]
    else:
        return [f"unknown kind {k!r} (expected one of {', '.join(KINDS)})"]
    if k in ("line_added", "line_removed", "number_changed") and (prior is None or current is None):
        side = "prior" if prior is None else "current"
        return [f"note {item.note_key!r} not found in the {side} report"]
    return []


def _check_coverage(items: list[NoteChangeItem], report: NoteChangeReport) -> None:
    real = [i for i in items if not i.example]
    if len(real) < MIN_ROWS:
        report.errors.append(f"need >= {MIN_ROWS} rows, found {len(real)}")
    for company, prior, current in REAL_PAIRS:
        if not any((i.prior_rcept_no, i.current_rcept_no) == (prior, current) for i in real):
            report.errors.append(f"need rows for {company} ({prior} -> {current})")
    watch = sum(1 for i in real if is_watchlist(i.note_key, DEFAULT_WATCHLIST))
    if real and watch * 2 < len(real):
        report.errors.append(
            f"need >= half of the rows on watchlist notes, found {watch}/{len(real)}"
        )


def validate_note_changes(
    items: list[NoteChangeItem], zip_dir: Path, *, check_coverage: bool = True
) -> NoteChangeReport:
    report = NoteChangeReport()
    notes = _Notes(zip_dir)
    seen: set[str] = set()
    for item in items:
        if item.example:
            report.skipped_examples += 1
            continue
        report.checked += 1
        if not item.id or item.id in seen:
            report.errors.append(f"{item.id or '(no id)'}: missing or duplicate id")
        seen.add(item.id)
        if item.scope not in SCOPES:
            report.errors.append(f"{item.id}: scope must be one of {SCOPES}")
            continue
        if not (item.prior_rcept_no and item.current_rcept_no and item.note_key):
            report.errors.append(
                f"{item.id}: prior_rcept_no, current_rcept_no and note_key required"
            )
            continue
        try:
            prior = notes.get(item.prior_rcept_no, item.scope, item.note_key)
            current = notes.get(item.current_rcept_no, item.scope, item.note_key)
        except (FileNotFoundError, DartParseError) as exc:
            report.errors.append(f"{item.id}: {exc}")
            continue
        report.errors += [f"{item.id}: {e}" for e in _check(item, prior, current)]
    if check_coverage and report.checked:
        _check_coverage(items, report)
    return report


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="footnote_rag.evals.validate_note_changes")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_NOTE_CHANGES)
    parser.add_argument("--zip-dir", type=Path, default=Path("data/dart"))
    parser.add_argument("--no-coverage", action="store_true", help="skip coverage rules")
    args = parser.parse_args(argv)
    report = validate_note_changes(
        load_note_changes(args.dataset), args.zip_dir, check_coverage=not args.no_coverage
    )
    print(f"checked rows: {report.checked}, example rows skipped: {report.skipped_examples}")
    if report.checked == 0:
        print("dataset contains only _example rows: nothing to validate against real filings")
    for e in report.errors:
        print(f"ERROR {e}")
    print("OK" if report.ok else f"FAILED ({len(report.errors)} error(s))")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
