"""Eval dataset (JSONL) loading.

Row format::

    {"id": "...", "rcept_no": "2026..." | ["...", "..."], "corp_name": "삼성전자" | [...],
     "question": "...", "expected_section_prefixes": ["(첨부)재무제표 > 주석 > 12. 리스"],
     "answerable": true, "evidence_quote": "..." | ["...", "..."]}

Comparison questions list several ``rcept_no`` / ``corp_name`` / ``evidence_quote`` values.
Rows with ``"_example": true`` are format examples only and are skipped by the runner and
the validator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATASET = Path("evals/dataset.jsonl")


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


@dataclass
class EvalItem:
    id: str
    rcept_nos: list[str]
    corp_names: list[str]
    question: str
    expected_section_prefixes: list[str]
    answerable: bool
    evidence_quotes: list[str]
    example: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def is_comparison(self) -> bool:
        return len(set(self.corp_names)) >= 2


def load_dataset(path: Path = DEFAULT_DATASET) -> list[EvalItem]:
    items: list[EvalItem] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON ({exc})") from None
        items.append(
            EvalItem(
                id=str(row.get("id", f"line-{lineno}")),
                rcept_nos=_as_list(row.get("rcept_no")),
                corp_names=_as_list(row.get("corp_name")),
                question=str(row.get("question", "")),
                expected_section_prefixes=_as_list(row.get("expected_section_prefixes")),
                answerable=bool(row.get("answerable", True)),
                evidence_quotes=_as_list(row.get("evidence_quote")),
                example=bool(row.get("_example", False)),
                raw=row,
            )
        )
    return items
