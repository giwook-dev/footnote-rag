"""전기 대비 주석 변동 탐지 엔진(I/O 없는 순수 함수). 계산과 판정은 모두 여기 코드가 한다.

역할: 전년·당년 사업보고서의 ``ParsedDocument.sections``에서 연결(또는 별도) 주석을 뽑아 짝짓고, 주석마다
줄 비교(추가·삭제 줄, 숫자만 바뀐 줄 구분), 표 행 숫자 비교(전년 대비 증감·증감률, 전기 수치 불일치), 관심 주석
표시와 우선순위 점수를 만든다. 로컬 모델 요약(``comparison.service.summarize_note``)은 이 결과만 받는다.
주요 요소: ``extract_notes``, ``match_notes``, ``diff_lines``, ``parse_number``, ``parse_table_rows``,
``compare_rows``, ``note_score``, ``compare_documents``, ``DiffOptions``.
호출 관계: ``comparison.service.create_comparison``이 ``asyncio.to_thread``로 ``compare_documents``를 부른다.
``dart.sections``의 ``statement_scope``/``note_key``/``note_title``과 ``dart.parser.normalize_ws``를 쓴다.
학습 문서: docs/study/11-note-change-analysis.md, docs/adr/0012-note-change-analysis.md

Rules (docs/adr/0012-note-change-analysis.md):

* notes: sections with ``statement_scope(path) == scope`` grouped by ``note_key(path)``; several
  sections of one key (sub-headings) are joined in document order. Lines are ``normalize_ws``-ed,
  empty lines dropped.
* matching: equal ``key`` first (``matched_by="key"``); then the rest one-to-one by
  ``difflib.SequenceMatcher(None, key_a, key_b).ratio() >= 0.85``, highest ratio first
  (``"similar_title"``); still unmatched -> ``added`` (current only) / ``removed`` (prior only).
* line diff: ``SequenceMatcher`` over the note's lines; prior lines are ``P1..``, current ``C1..``.
  In a ``replace`` block, a prior/current line pair that is equal once every number is masked
  (``#``) is ``numbers_only``. At most ``max_lines`` non-equal lines are kept (``truncated``).
* tables (``parse_table_rows``): a line with ``|`` is a row. ``당기 | (단위 : 백만원)`` / ``전기`` /
  ``당기말`` / ``전기말`` lines open a period block that lasts until the next block marker or a
  description line (no digit, no ``|``), which is also the table title. A header line with both
  ``당기`` and ``전기`` cells switches to column form: the first value of each row below is the
  current period, the second the prior period (only when ``당기`` comes first; otherwise the rows
  are left out). A marker line that carries numbers (``당기 | 1,000 | 800``, periods written as
  rows) is a row of that period instead. Label = the non-numeric cells before the first number; values = the numeric
  cells (``1,234``, ``(1,234)``/``△1,234`` negative, ``-`` = 0; dates and bare 4-digit years
  are not numbers). Row id = (title, label, occurrence among rows of the same period, title and
  label).
* number changes (``compare_rows``): ``yoy`` = current-period rows of both reports differ (also
  rows outside any period block, compared with each other); ``prior_mismatch`` = the current
  report's prior-period row differs from the prior report's current-period row (restatement /
  reclassification *candidate*); ``row_added``/``row_removed`` = a current-period row on one side
  only. When a (title, label) repeats and the two sides have a different number of such rows,
  position cannot pair them: they are skipped and counted in ``ambiguous_rows``.
* ``large_change`` (``yoy`` only): prior != 0 and |delta| / |prior| >= threshold (default 0.3), a
  sign flip, or 0 -> non-zero. Rows of ratios (a ``%`` cell, or only values below 1) are never
  flagged.
* watchlist: a note whose key contains a ``NOTE_DIFF_WATCHLIST`` word.
* score (``note_score``): 0 for an unchanged note, else ``WATCHLIST_WEIGHT`` (watchlist) +
  ``ADDED_REMOVED_WEIGHT`` (added/removed note) + ``PRIOR_MISMATCH_WEIGHT`` per prior mismatch +
  ``LARGE_CHANGE_WEIGHT`` per large change + ``TEXT_LINE_WEIGHT`` per changed sentence line
  (not ``numbers_only``, at most ``TEXT_LINE_CAP``). Notes are sorted by score, highest first.
* numbers are serialized as strings (no float rounding).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from footnote_rag.dart.parser import Section, normalize_ws
from footnote_rag.dart.sections import (
    CONSOLIDATED,
    SEPARATE,
    StatementScope,
    note_key,
    note_title,
    statement_scope,
)

NoteStatus = Literal["added", "removed", "changed", "unchanged"]
MatchedBy = Literal["key", "similar_title"]
ChangeKind = Literal["yoy", "prior_mismatch", "row_added", "row_removed"]
Period = Literal["current", "prior"]

SCOPES: tuple[StatementScope, ...] = (CONSOLIDATED, SEPARATE)
SIMILAR_TITLE_RATIO = 0.85
DEFAULT_MAX_LINES = 400
DEFAULT_CHANGE_THRESHOLD = Decimal("0.3")
DEFAULT_WATCHLIST: tuple[str, ...] = (
    "우발부채",
    "약정",
    "소송",
    "특수관계자",
    "담보",
    "지급보증",
    "보고기간후사건",
    "계속기업",
    "회계정책의변경",
)

# Priority score weights (see ``note_score``). The watchlist weight is larger than anything the
# other terms can add up to in practice, so a changed watchlist note always comes first.
WATCHLIST_WEIGHT = 1000
ADDED_REMOVED_WEIGHT = 40
PRIOR_MISMATCH_WEIGHT = 15
LARGE_CHANGE_WEIGHT = 5
TEXT_LINE_WEIGHT = 1
TEXT_LINE_CAP = 50

PERIOD_CURRENT: Period = "current"
PERIOD_PRIOR: Period = "prior"

# Period block marker: "당기 | (단위 : 백만원)", "전기말 |", "당기".
_PERIOD_MARK_RE = re.compile(r"^(당기|전기)(말)?\s*(?:\||$)")
# A column header cell ("당기", "전기말", "당기(제57기)") with spaces removed.
_PERIOD_CELL_RE = re.compile(r"^(당기|전기)(말)?(?:\(.*\))?$")
_UNIT_LINE_RE = re.compile(r"^\(?\s*단위\s*[:：]")
_DIGIT_RE = re.compile(r"\d")
_NUMBER_CELL_RE = re.compile(
    r"^(?P<open>\()?\s*(?P<sign>[△\-−])?\s*(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"\s*(?P<close>\))?\s*(?P<pct>%)?$"
)
_DASH_CELL_RE = re.compile(r"^[-–—]$")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
# Masking for ``numbers_only``: every digit run (with its separators) becomes "#".
_DIGITS_RE = re.compile(r"\d[\d,.]*")
_MASKED_NUMBER_CELL_RE = re.compile(r"[-–—]|\(?\s*[△\-−]?\s*#\s*\)?\s*%?")
_SPACE_RE = re.compile(r"\s+")
_PCT_QUANTUM = Decimal("0.1")


# ---------------------------------------------------------------- notes and matching


@dataclass
class Note:
    """One financial-statement note of one report."""

    key: str
    title: str
    section_path: str  # the first section of the note
    lines: list[str] = field(default_factory=list)
    order: int = 0  # position in its document


@dataclass
class NotePair:
    prior: Note | None
    current: Note | None
    matched_by: MatchedBy | None

    @property
    def status(self) -> NoteStatus | None:
        if self.prior is None:
            return "added"
        if self.current is None:
            return "removed"
        return None  # changed / unchanged is decided by the line diff


def _clean_lines(text: str) -> list[str]:
    return [line for line in (normalize_ws(raw) for raw in text.split("\n")) if line]


def extract_notes(sections: Iterable[Section], scope: StatementScope = CONSOLIDATED) -> list[Note]:
    """Notes of ``scope`` in document order; sections of one ``note_key`` are joined."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}: {scope!r}")
    notes: dict[str, Note] = {}
    for section in sections:
        if statement_scope(section.path) != scope:
            continue
        key = note_key(section.path)
        if key is None:
            continue
        note = notes.get(key)
        if note is None:
            title = note_title(section.path) or key
            note = Note(key=key, title=title, section_path=section.path, order=len(notes))
            notes[key] = note
        note.lines.extend(_clean_lines(section.text))
    return list(notes.values())


def match_notes(
    prior: Sequence[Note], current: Sequence[Note], *, ratio: float = SIMILAR_TITLE_RATIO
) -> list[NotePair]:
    """Pairs in current-document order, then the removed notes in prior-document order."""
    prior_by_key = {n.key: n for n in prior}
    pairs: dict[str, NotePair] = {}
    used_prior: set[str] = set()
    for note in current:
        match = prior_by_key.get(note.key)
        if match is not None and match.key not in used_prior:
            pairs[note.key] = NotePair(match, note, "key")
            used_prior.add(match.key)
    rest_current = [n for n in current if n.key not in pairs]
    rest_prior = [n for n in prior if n.key not in used_prior]
    candidates: list[tuple[float, int, int]] = []
    for ci, c in enumerate(rest_current):
        for pi, p in enumerate(rest_prior):
            score = difflib.SequenceMatcher(None, p.key, c.key).ratio()
            if score >= ratio:
                candidates.append((score, ci, pi))
    candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
    taken_c: set[int] = set()
    taken_p: set[int] = set()
    for _score, ci, pi in candidates:
        if ci in taken_c or pi in taken_p:
            continue
        taken_c.add(ci)
        taken_p.add(pi)
        pairs[rest_current[ci].key] = NotePair(rest_prior[pi], rest_current[ci], "similar_title")
        used_prior.add(rest_prior[pi].key)
    out = [pairs.get(n.key) or NotePair(None, n, None) for n in current]
    out += [NotePair(n, None, None) for n in prior if n.key not in used_prior]
    return out


# ---------------------------------------------------------------- line diff


@dataclass
class LineDiff:
    entries: list[dict[str, Any]]  # non-equal lines, at most ``max_lines``
    equal: int
    text_added: int
    text_removed: int
    numbers_only: int  # numbers-only line pairs
    truncated: bool

    @property
    def changed(self) -> bool:
        return self.text_added + self.text_removed + self.numbers_only > 0

    @property
    def text_lines(self) -> int:
        return self.text_added + self.text_removed

    def stats(self) -> dict[str, int]:
        return {
            "equal": self.equal,
            "text_added": self.text_added,
            "text_removed": self.text_removed,
            "numbers_only": self.numbers_only,
        }


def mask_numbers(line: str) -> str:
    """``line`` with every number written as ``#`` (and numeric table cells such as ``-`` or
    ``(#)`` as a bare ``#``), used to tell numbers-only changes from sentence changes."""
    masked = _DIGITS_RE.sub("#", line)
    if "|" in masked:
        cells = [c.strip() for c in masked.split("|")]
        masked = " | ".join("#" if _MASKED_NUMBER_CELL_RE.fullmatch(c) else c for c in cells)
    return masked


def _entry(op: str, ref: str, text: str, *, pair: str | None = None) -> dict[str, Any]:
    return {"op": op, "ref": ref, "text": text, "numbers_only": pair is not None, "pair": pair}


def _replace_entries(
    prior: Sequence[str], i1: int, i2: int, current: Sequence[str], j1: int, j2: int
) -> list[dict[str, Any]]:
    """A replace block: pairs that match once numbers are masked are ``numbers_only``."""
    a = [mask_numbers(x) for x in prior[i1:i2]]
    b = [mask_numbers(x) for x in current[j1:j2]]
    out: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(a2 - a1):
                p, c = i1 + a1 + k, j1 + b1 + k
                out.append(_entry("delete", f"P{p + 1}", prior[p], pair=f"C{c + 1}"))
                out.append(_entry("insert", f"C{c + 1}", current[c], pair=f"P{p + 1}"))
            continue
        out += [_entry("delete", f"P{i1 + k + 1}", prior[i1 + k]) for k in range(a1, a2)]
        out += [_entry("insert", f"C{j1 + k + 1}", current[j1 + k]) for k in range(b1, b2)]
    return out


def diff_lines(
    prior: Sequence[str], current: Sequence[str], *, max_lines: int = DEFAULT_MAX_LINES
) -> LineDiff:
    """Line diff of one note (``P{n}``/``C{n}`` are 1-based line numbers inside the note)."""
    matcher = difflib.SequenceMatcher(None, list(prior), list(current), autojunk=False)
    entries: list[dict[str, Any]] = []
    equal = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            equal += i2 - i1
        elif tag == "delete":
            entries += [_entry("delete", f"P{i + 1}", prior[i]) for i in range(i1, i2)]
        elif tag == "insert":
            entries += [_entry("insert", f"C{j + 1}", current[j]) for j in range(j1, j2)]
        else:
            entries += _replace_entries(prior, i1, i2, current, j1, j2)
    text_added = sum(1 for e in entries if e["op"] == "insert" and not e["numbers_only"])
    text_removed = sum(1 for e in entries if e["op"] == "delete" and not e["numbers_only"])
    numbers_only = sum(1 for e in entries if e["op"] == "insert" and e["numbers_only"])
    return LineDiff(
        entries=entries[:max_lines],
        equal=equal,
        text_added=text_added,
        text_removed=text_removed,
        numbers_only=numbers_only,
        truncated=len(entries) > max_lines,
    )


# ---------------------------------------------------------------- table rows


@dataclass(frozen=True)
class ParsedNumber:
    value: Decimal
    percent: bool = False


def parse_number(cell: str) -> ParsedNumber | None:
    """A numeric table cell: ``1,234``, ``(1,234)`` and ``△1,234`` (negative), ``-`` (0), ``12.5%``.

    Not numbers: dates (``2025-12-23``, ``2025.12.31``), a bare 4-digit year (``2025``), text.
    """
    text = cell.strip()
    if _DASH_CELL_RE.fullmatch(text):
        return ParsedNumber(Decimal(0))
    m = _NUMBER_CELL_RE.fullmatch(text)
    if m is None or bool(m.group("open")) != bool(m.group("close")):
        return None
    if (
        not m.group("open")
        and not m.group("sign")
        and not m.group("frac")
        and not m.group("pct")
        and _YEAR_RE.fullmatch(m.group("int"))
    ):
        return None
    try:
        value = Decimal(m.group("int").replace(",", "") + (m.group("frac") or ""))
    except InvalidOperation:  # pragma: no cover - the regex only lets digits through
        return None
    if m.group("open") or m.group("sign"):
        value = -value
    return ParsedNumber(value, percent=bool(m.group("pct")))


@dataclass(frozen=True)
class TableRow:
    table: str
    label: str
    occurrence: int
    period: Period | None
    values: tuple[Decimal, ...]
    percent: bool
    line: int  # 0-based index in the note's lines

    @property
    def row_id(self) -> tuple[str, str, int]:
        return (self.table, self.label, self.occurrence)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split("|")]


def _column_order(cells: Sequence[str]) -> Literal["current_first", "prior_first"] | None:
    """For a header row naming both periods: which comes first; None when it is not one."""
    current_at = prior_at = None
    for i, cell in enumerate(cells):
        m = _PERIOD_CELL_RE.fullmatch(_SPACE_RE.sub("", cell))
        if m is None:
            continue
        if m.group(1) == "당기" and current_at is None:
            current_at = i
        elif m.group(1) == "전기" and prior_at is None:
            prior_at = i
    if current_at is None or prior_at is None:
        return None
    return "current_first" if current_at < prior_at else "prior_first"


def parse_table_rows(lines: Sequence[str]) -> list[TableRow]:
    """Numeric table rows of a note with their table title and period (see module docstring)."""
    rows: list[TableRow] = []
    seen: dict[tuple[Period | None, str, str], int] = {}
    title = ""
    period: Period | None = None
    columns: Literal["current_first", "prior_first"] | None = None

    def add(label: str, values: tuple[Decimal, ...], per: Period | None, pct: bool, i: int) -> None:
        key = (per, title, label)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        rows.append(TableRow(title, label, occurrence, per, values, pct, i))

    for i, line in enumerate(lines):
        if "|" not in line:
            if _UNIT_LINE_RE.match(line):
                continue
            mark = _PERIOD_MARK_RE.match(line)
            if mark is not None:
                period = PERIOD_CURRENT if mark.group(1) == "당기" else PERIOD_PRIOR
                columns = None
                continue
            if not _DIGIT_RE.search(line):
                title, period, columns = line, None, None
            continue
        cells = _cells(line)
        order = _column_order(cells)
        if order is not None:
            columns, period = order, None
            continue
        mark = _PERIOD_MARK_RE.match(line)
        if mark is not None:
            row_period = PERIOD_CURRENT if mark.group(1) == "당기" else PERIOD_PRIOR
            rest = cells[1:]
            if not any(parse_number(c) for c in rest):
                period, columns = row_period, None  # "당기 | (단위 : 백만원)": a block marker
                continue
            # "당기 | 1,000 | 800": a row named after its period (periods as rows).
            parsed = [parse_number(c) for c in rest]
            label = " | ".join(c for c, p in zip(rest, parsed, strict=True) if p is None and c)
            found = [p for p in parsed if p is not None]
            add(label, tuple(p.value for p in found), row_period, any(p.percent for p in found), i)
            continue
        label_cells: list[str] = []
        numbers: list[ParsedNumber] = []
        for cell in cells:
            parsed = parse_number(cell)
            if parsed is not None:
                numbers.append(parsed)
            elif not numbers:
                label_cells.append(cell)
        if not numbers:
            continue  # a header row (hierarchical column names) or a text-only row
        label = " | ".join(c for c in label_cells if c)
        pct = any(n.percent for n in numbers)
        values = tuple(n.value for n in numbers)
        if columns == "prior_first":
            continue  # left out of the comparison: only "당기 first" headers are trusted
        if columns == "current_first":
            add(label, values[:1], PERIOD_CURRENT, pct, i)
            if len(values) >= 2:
                add(label, values[1:2], PERIOD_PRIOR, pct, i)
            continue
        add(label, values, period, pct, i)
    return rows


# ---------------------------------------------------------------- number changes


def _fmt(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def is_large_change(prior: Decimal, current: Decimal, threshold: Decimal) -> bool:
    """|delta| / |prior| >= threshold, a sign flip, or 0 -> non-zero."""
    if prior == 0:
        return current != 0
    if current != 0 and (prior > 0) != (current > 0):
        return True
    return abs(current - prior) / abs(prior) >= threshold


def _is_ratio_row(*rows: TableRow) -> bool:
    if any(r.percent for r in rows):
        return True
    values = [v for r in rows for v in r.values if v != 0]
    return bool(values) and all(abs(v) < 1 for v in values)


def _cells_of(
    prior: TableRow, current: TableRow, threshold: Decimal, *, flag_large: bool
) -> list[dict[str, Any]]:
    if len(prior.values) != len(current.values):
        return []
    ratio = _is_ratio_row(prior, current)
    cells = []
    for position, (p, c) in enumerate(zip(prior.values, current.values, strict=True), start=1):
        if p == c:
            continue
        delta = c - p
        pct = (delta / abs(p) * 100).quantize(_PCT_QUANTUM) if p != 0 else None
        flags = (
            ["large_change"]
            if flag_large and not ratio and is_large_change(p, c, threshold)
            else []
        )
        cells.append(
            {
                "position": position,
                "prior": _fmt(p),
                "current": _fmt(c),
                "delta": _fmt(delta),
                "change_pct": _fmt(pct) if pct is not None else None,
                "flags": flags,
            }
        )
    return cells


def _change(
    kind: ChangeKind,
    prior: TableRow | None,
    current: TableRow | None,
    threshold: Decimal,
) -> dict[str, Any]:
    base = current or prior
    assert base is not None
    cells = (
        _cells_of(prior, current, threshold, flag_large=kind == "yoy")
        if prior is not None and current is not None
        else []
    )
    flags = sorted({f for cell in cells for f in cell["flags"]})
    if kind == "prior_mismatch":
        flags = ["prior_mismatch", *flags]
    return {
        "kind": kind,
        "table": base.table,
        "label": base.label,
        "occurrence": base.occurrence,
        "prior_values": [_fmt(v) for v in prior.values] if prior is not None else [],
        "current_values": [_fmt(v) for v in current.values] if current is not None else [],
        "prior_ref": f"P{prior.line + 1}" if prior is not None else None,
        "current_ref": f"C{current.line + 1}" if current is not None else None,
        "cells": cells,
        "flags": flags,
    }


def _by_base(rows: Iterable[TableRow]) -> dict[tuple[str, str], list[TableRow]]:
    groups: dict[tuple[str, str], list[TableRow]] = {}
    for row in rows:
        groups.setdefault((row.table, row.label), []).append(row)
    return groups


def _compare_group(
    prior_rows: Iterable[TableRow],
    current_rows: Iterable[TableRow],
    kind: Literal["yoy", "prior_mismatch"],
    threshold: Decimal,
) -> tuple[list[dict[str, Any]], int]:
    prior_groups, current_groups = _by_base(prior_rows), _by_base(current_rows)
    changes: list[dict[str, Any]] = []
    ambiguous = 0
    for base in dict.fromkeys([*current_groups, *prior_groups]):
        p, c = prior_groups.get(base, []), current_groups.get(base, [])
        if p and c and len(p) != len(c) and max(len(p), len(c)) > 1:
            ambiguous += max(len(p), len(c))  # repeated rows cannot be paired by position
            continue
        for k in range(max(len(p), len(c))):
            pr = p[k] if k < len(p) else None
            cr = c[k] if k < len(c) else None
            if pr is not None and cr is not None:
                if pr.values != cr.values:
                    changes.append(_change(kind, pr, cr, threshold))
            elif kind == "yoy":
                changes.append(_change("row_added" if cr else "row_removed", pr, cr, threshold))
    return changes, ambiguous


_KIND_ORDER = {"prior_mismatch": 0, "yoy": 1, "row_added": 2, "row_removed": 3}


@dataclass
class RowComparison:
    changes: list[dict[str, Any]]
    ambiguous_rows: int

    def count(self, *, kind: str | None = None, flag: str | None = None) -> int:
        return sum(
            1
            for c in self.changes
            if (kind is None or c["kind"] == kind) and (flag is None or flag in c["flags"])
        )


def compare_rows(
    prior_note: Note | Sequence[str],
    current_note: Note | Sequence[str],
    *,
    threshold: Decimal = DEFAULT_CHANGE_THRESHOLD,
) -> RowComparison:
    """Number changes between two versions of one note (notes or their line lists).

    Ordered: prior mismatches, large changes, other ``yoy``, added rows, removed rows.
    """
    prior_rows = parse_table_rows(prior_note.lines if isinstance(prior_note, Note) else prior_note)
    current_rows = parse_table_rows(
        current_note.lines if isinstance(current_note, Note) else current_note
    )
    changes: list[dict[str, Any]] = []
    ambiguous = 0
    for period in (PERIOD_CURRENT, None):
        found, skipped = _compare_group(
            (r for r in prior_rows if r.period == period),
            (r for r in current_rows if r.period == period),
            "yoy",
            threshold,
        )
        changes += found
        ambiguous += skipped
    found, skipped = _compare_group(
        (r for r in prior_rows if r.period == PERIOD_CURRENT),
        (r for r in current_rows if r.period == PERIOD_PRIOR),
        "prior_mismatch",
        threshold,
    )
    changes += found
    ambiguous += skipped
    changes.sort(
        key=lambda c: (_KIND_ORDER[c["kind"]], "large_change" not in c["flags"]),
    )
    return RowComparison(changes, ambiguous)


# ---------------------------------------------------------------- scoring and the document


@dataclass(frozen=True)
class DiffOptions:
    change_threshold: Decimal = DEFAULT_CHANGE_THRESHOLD
    max_lines: int = DEFAULT_MAX_LINES
    watchlist: tuple[str, ...] = DEFAULT_WATCHLIST


def is_watchlist(key: str, watchlist: Sequence[str]) -> bool:
    compact = _SPACE_RE.sub("", key)
    return any(word and _SPACE_RE.sub("", word) in compact for word in watchlist)


def note_score(
    status: NoteStatus,
    *,
    watchlist: bool,
    prior_mismatch: int = 0,
    large_change: int = 0,
    text_lines: int = 0,
) -> int:
    """Priority of a note for the reviewer; an unchanged note is always 0."""
    if status == "unchanged":
        return 0
    score = WATCHLIST_WEIGHT if watchlist else 0
    if status in ("added", "removed"):
        score += ADDED_REMOVED_WEIGHT
    score += PRIOR_MISMATCH_WEIGHT * prior_mismatch + LARGE_CHANGE_WEIGHT * large_change
    return score + TEXT_LINE_WEIGHT * min(text_lines, TEXT_LINE_CAP)


def _note_result(pair: NotePair, options: DiffOptions) -> dict[str, Any]:
    prior_lines = pair.prior.lines if pair.prior else []
    current_lines = pair.current.lines if pair.current else []
    diff = diff_lines(prior_lines, current_lines, max_lines=options.max_lines)
    status: NoteStatus = pair.status or ("changed" if diff.changed else "unchanged")
    if pair.prior is not None and pair.current is not None:
        rows = compare_rows(pair.prior, pair.current, threshold=options.change_threshold)
    else:
        rows = RowComparison([], 0)  # a whole note added or removed: no row comparison
    key = (pair.current or pair.prior).key  # type: ignore[union-attr]
    watch = is_watchlist(key, options.watchlist)
    counts = {
        "prior_mismatch": rows.count(kind="prior_mismatch"),
        "large_change": rows.count(kind="yoy", flag="large_change"),
        "yoy": rows.count(kind="yoy"),
        "row_added": rows.count(kind="row_added"),
        "row_removed": rows.count(kind="row_removed"),
    }
    title_source = pair.current or pair.prior
    return {
        "index": 0,
        "key": key,
        "title": title_source.title if title_source else key,
        "prior_title": pair.prior.title if pair.prior else None,
        "current_title": pair.current.title if pair.current else None,
        "prior_section_path": pair.prior.section_path if pair.prior else None,
        "current_section_path": pair.current.section_path if pair.current else None,
        "status": status,
        "matched_by": pair.matched_by,
        "watchlist": watch,
        "score": note_score(
            status,
            watchlist=watch,
            prior_mismatch=counts["prior_mismatch"],
            large_change=counts["large_change"],
            text_lines=diff.text_lines,
        ),
        "prior_line_count": len(prior_lines),
        "current_line_count": len(current_lines),
        "line_stats": diff.stats(),
        "lines": diff.entries,
        "truncated": diff.truncated,
        "number_changes": rows.changes,
        "counts": counts,
        "ambiguous_rows": rows.ambiguous_rows,
    }


def summarize(notes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    statuses = {s: 0 for s in ("changed", "added", "removed", "unchanged")}
    matched = {"key": 0, "similar_title": 0}
    for n in notes:
        statuses[n["status"]] += 1
        if n["matched_by"] in matched:
            matched[n["matched_by"]] += 1
    return {
        "notes": len(notes),
        "prior_notes": sum(1 for n in notes if n["status"] != "added"),
        "current_notes": sum(1 for n in notes if n["status"] != "removed"),
        "status_counts": statuses,
        "matched_by": matched,
        "prior_mismatch": sum(n["counts"]["prior_mismatch"] for n in notes),
        "large_change": sum(n["counts"]["large_change"] for n in notes),
        "ambiguous_rows": sum(n["ambiguous_rows"] for n in notes),
        "watchlist_changed": sum(1 for n in notes if n["watchlist"] and n["status"] != "unchanged"),
    }


def compare_documents(
    prior_sections: Iterable[Section],
    current_sections: Iterable[Section],
    *,
    scope: StatementScope = CONSOLIDATED,
    options: DiffOptions | None = None,
) -> dict[str, Any]:
    """Every note pair compared and sorted by score (``index`` = position in that order).

    JSON-serializable: ``{"scope", "settings", "summary", "notes"}``.
    """
    options = options or DiffOptions()
    prior = extract_notes(prior_sections, scope)
    current = extract_notes(current_sections, scope)
    pairs = match_notes(prior, current)
    notes = [_note_result(pair, options) for pair in pairs]
    notes.sort(key=lambda n: -n["score"])  # stable: document order among equal scores
    for index, note in enumerate(notes):
        note["index"] = index
    return {
        "scope": scope,
        "settings": {
            "change_threshold": _fmt(options.change_threshold),
            "max_lines": options.max_lines,
            "watchlist": list(options.watchlist),
        },
        "summary": summarize(notes),
        "notes": notes,
    }
