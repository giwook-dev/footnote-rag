"""Eval 데이터셋 검증기: 근거 인용문이 파서 출력에 실제로 있는지 확인한다.

역할: ``evidence_quote``가 캐시된 원문 ZIP의 파서 텍스트에 그대로 있고 기대 목차 아래에 있는지,
데이터셋이 개수 규칙(15행 이상 등)을 지키는지 검사한다. ``_example`` 행은 건너뛴다.
주요 요소: ``validate``, ``ValidationReport``, ``_check_coverage``, ``main``.
호출 관계: ``dart.parser.parse_document_zip``과 ``evals.dataset.load_dataset``을 호출한다.
학습 문서: docs/study/10-eval-and-mcp.md

Validate the eval dataset against the parser's real output.

    python -m footnote_rag.evals.validate [--dataset evals/dataset.jsonl] [--zip-dir data/dart]

For each non-example, answerable row every ``evidence_quote`` must appear verbatim
(whitespace-normalised) in the text the DART parser extracts from the cached filing ZIP,
inside a section whose path starts with one of ``expected_section_prefixes``.
Coverage rules (>=15 rows, >=4 per company, >=3 unanswerable, >=2 comparisons) are
checked whenever the dataset has real (non-example) rows.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from footnote_rag.dart.parser import DartParseError, Section, normalize_ws, parse_document_zip
from footnote_rag.evals.dataset import DEFAULT_DATASET, EvalItem, load_dataset

REQUIRED_COMPANIES = ("삼성전자", "NAVER", "현대자동차")


@dataclass
class ValidationReport:
    checked: int = 0
    skipped_examples: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _sections_for(zip_dir: Path, rcept_no: str, cache: dict[str, list[Section]]) -> list[Section]:
    if rcept_no not in cache:
        path = zip_dir / f"{rcept_no}.zip"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing - download it first: python -m footnote_rag.cli dart-fetch "
                f"--rcept-no {rcept_no}"
            )
        cache[rcept_no] = parse_document_zip(path.read_bytes(), rcept_no).sections
    return cache[rcept_no]


def _check_coverage(items: list[EvalItem], report: ValidationReport) -> None:
    real = [i for i in items if not i.example]
    if len(real) < 15:
        report.errors.append(f"need >= 15 rows, found {len(real)}")
    per_company: Counter[str] = Counter()
    for item in real:
        for name in set(item.corp_names):
            per_company[name] += 1
    for company in REQUIRED_COMPANIES:
        count = sum(v for k, v in per_company.items() if company in k)
        if count < 4:
            report.errors.append(f"need >= 4 rows for {company}, found {count}")
    unanswerable = sum(1 for i in real if not i.answerable)
    if unanswerable < 3:
        report.errors.append(f"need >= 3 unanswerable rows, found {unanswerable}")
    comparisons = sum(1 for i in real if i.is_comparison)
    if comparisons < 2:
        report.errors.append(f"need >= 2 comparison rows, found {comparisons}")


def validate(
    items: list[EvalItem], zip_dir: Path, *, check_coverage: bool = True
) -> ValidationReport:
    report = ValidationReport()
    cache: dict[str, list[Section]] = {}
    for item in items:
        if item.example:
            report.skipped_examples += 1
            continue
        report.checked += 1
        if not item.question.strip():
            report.errors.append(f"{item.id}: empty question")
        if not item.rcept_nos:
            report.errors.append(f"{item.id}: rcept_no missing")
            continue
        if not item.answerable:
            continue
        if not item.evidence_quotes:
            report.errors.append(f"{item.id}: answerable row without evidence_quote")
            continue
        if not item.expected_section_prefixes:
            report.errors.append(f"{item.id}: answerable row without expected_section_prefixes")
        try:
            sections = [s for r in item.rcept_nos for s in _sections_for(zip_dir, r, cache)]
        except (FileNotFoundError, DartParseError) as exc:
            report.errors.append(f"{item.id}: {exc}")
            continue
        for quote in item.evidence_quotes:
            needle = normalize_ws(quote.replace("\n", " "))
            hits = [s for s in sections if needle in normalize_ws(s.text.replace("\n", " "))]
            if not hits:
                report.errors.append(
                    f"{item.id}: evidence_quote not found in parsed text: {quote[:60]!r}"
                )
                continue
            if item.expected_section_prefixes and not any(
                s.path.startswith(p) for s in hits for p in item.expected_section_prefixes
            ):
                report.errors.append(
                    f"{item.id}: quote found only under {[s.path for s in hits][:3]}, "
                    f"not under expected prefixes {item.expected_section_prefixes}"
                )
    if check_coverage and report.checked:
        _check_coverage(items, report)
    return report


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="footnote_rag.evals.validate")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--zip-dir", type=Path, default=Path("data/dart"))
    parser.add_argument("--no-coverage", action="store_true", help="skip coverage rules")
    args = parser.parse_args(argv)

    report = validate(load_dataset(args.dataset), args.zip_dir, check_coverage=not args.no_coverage)
    print(f"checked rows: {report.checked}, example rows skipped: {report.skipped_examples}")
    if report.checked == 0:
        print("dataset contains only _example rows: nothing to validate against real filings")
    for w in report.warnings:
        print(f"WARN  {w}")
    for e in report.errors:
        print(f"ERROR {e}")
    print("OK" if report.ok else f"FAILED ({len(report.errors)} error(s))")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
