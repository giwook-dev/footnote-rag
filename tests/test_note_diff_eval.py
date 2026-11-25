"""주석 변동 Eval(evals/note_diff.py)과 정답 데이터 검증기(evals/validate_note_changes.py). Fake 모드만."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from footnote_rag.dart import fake_data
from footnote_rag.evals import note_diff
from footnote_rag.evals.note_diff import (
    FAKE_PAIRS,
    FAKE_TRUTH,
    render_note_diff_report,
    run_note_diff,
    write_note_diff_report,
)
from footnote_rag.evals.validate_note_changes import (
    load_note_changes,
    validate_note_changes,
)
from footnote_rag.evals.validate_note_changes import (
    main as validate_main,
)

REPO_ROOT = Path(__file__).parent.parent
PRIOR, CURRENT = fake_data.FAKE_ANNUAL_PAIR


@pytest.fixture
def zip_dir(tmp_path) -> Path:
    d = tmp_path / "dart"
    d.mkdir()
    for r in (PRIOR, CURRENT):
        (d / f"{r}.zip").write_bytes(fake_data.build_document_zip(r))
    return d


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")
    return path


def _row(item, **changes) -> dict:
    raw = {
        k: v
        for k, v in dataclasses.asdict(item).items()
        if k not in ("example", "raw") and v is not None
    }
    return {**raw, **changes}


# ---------------------------------------------------------------- validator


def test_fake_truth_passes_the_validator(zip_dir):
    report = validate_note_changes(list(FAKE_TRUTH), zip_dir, check_coverage=False)
    assert report.ok, report.errors
    assert report.checked == len(FAKE_TRUTH) == 5


def test_validator_rejects_invented_or_wrong_rows(tmp_path, zip_dir):
    by_id = {t.id: t for t in FAKE_TRUTH}
    rows = [
        _row(by_id["fake-line-added"], id="invented", current_quote="원문에 없는 문장입니다."),
        # "소송사건은" is in the 우발부채 note of both reports: neither added nor removed
        _row(by_id["fake-line-added"], id="in-both", current_quote="소송사건은"),
        _row(by_id["fake-line-removed"], id="still-there", prior_quote="소송사건은"),
        _row(by_id["fake-number-changed"], id="bad-value", current_value="9,999"),
        _row(by_id["fake-note-added"], id="not-added", note_key="리스"),
        _row(by_id["fake-note-added"], id="bad-kind", kind="note_moved"),
        _row(by_id["fake-note-added"], id="no-zip", prior_rcept_no="20990101000000"),
        {"_example": True, "id": "ex"},
    ]
    items = load_note_changes(_write(tmp_path / "bad.jsonl", rows))
    report = validate_note_changes(items, zip_dir, check_coverage=False)
    failed = {e.split(":", 1)[0] for e in report.errors}
    assert failed == {
        "invented",
        "in-both",
        "still-there",
        "bad-value",
        "not-added",
        "bad-kind",
        "no-zip",
    }
    assert report.skipped_examples == 1
    path = _write(tmp_path / "one.jsonl", rows[:1])
    assert validate_main(["--dataset", str(path), "--zip-dir", str(zip_dir), "--no-coverage"]) == 1


def test_validator_coverage_rules(zip_dir):
    report = validate_note_changes(list(FAKE_TRUTH), zip_dir)
    joined = "\n".join(report.errors)
    assert ">= 8 rows" in joined and "삼성전자" in joined and "NAVER" in joined


def test_repository_note_changes_file_is_valid(tmp_path):
    """Every row of evals/note_changes.jsonl passes the validator; without cached filings (CI) the
    only allowed errors are missing ZIPs."""
    items = load_note_changes(REPO_ROOT / "evals" / "note_changes.jsonl")
    assert items and all(i.example or i.id for i in items)
    report = validate_note_changes(items, REPO_ROOT / "data" / "dart")
    assert report.ok or all("missing" in e for e in report.errors), report.errors
    if report.checked == 0:
        assert validate_main(["--dataset", str(REPO_ROOT / "evals" / "note_changes.jsonl")]) == 0


# ---------------------------------------------------------------- Eval


async def test_fake_note_diff_eval_detects_every_synthetic_change(
    settings, fake_embedder, fake_dart, tmp_path
):
    report = await run_note_diff(
        FAKE_PAIRS,
        list(FAKE_TRUTH),
        settings=settings,
        embedder=fake_embedder,
        dart_client=fake_dart,
        mode="fake",
        dataset_name="built-in FAKE_TRUTH",
        import_poll_seconds=0.01,
    )
    assert report.detection_rate == 1.0
    assert [t.detected for t in report.truth] == [True] * 5
    (pair,) = report.pairs
    assert pair.result is not None and pair.elapsed_ms is not None
    assert report.summaries == [] and report.import_failures == []
    path = write_note_diff_report(report, tmp_path, stamp="20261125T000000Z")
    assert path.name == "20261125T000000Z-fake-note-diff.md"
    md = path.read_text(encoding="utf-8")
    assert "- detected: 5/5 (100.0%)" in md
    assert "| fake-number-changed |" in md and "✓" in md
    assert "- matching: key 5 / similar title 0 / unmatched 2" in md
    assert "unmatched (added): `사업결합`" in md
    assert (
        "### Prior mismatches (top 10 of 1)" in md
        and "| 차입금 | 차입금 내역 | 장기차입금 | 11000 | 11500 | 500 |" in md
    )
    assert (
        "### Large changes (top 10 of 1)" in md
        and "| 단기차입금 | 3000 | 5000 | 2000 | 66.7 |" in md
    )
    assert "ambiguous rows (repeated rows, not compared): 0" in md
    assert "Not requested (`--summaries N`): no LLM was called." in md


def test_detection_misses_are_reported():
    item = dataclasses.replace(FAKE_TRUTH[2], current_quote="없는 문장")
    run = note_diff.PairRun("x", PRIOR, CURRENT, "consolidated", result={"notes": []})
    assert note_diff.detect(item, run).detected is False
    assert note_diff.detect(item, None).reason.startswith("pair not compared")
    md = render_note_diff_report(
        note_diff.NoteDiffEval(
            mode="live",
            dataset="d",
            settings={"change_threshold": "0.3", "max_lines": 400, "watchlist": []},
        )
    )
    assert "no ground-truth rows" in md


async def test_cli_without_summaries_never_builds_an_llm(settings, monkeypatch, tmp_path):
    def _no_llm(*args, **kwargs):
        raise AssertionError("no LLM without --summaries")

    async def _keep_engine() -> None:
        return None

    monkeypatch.setattr(note_diff, "make_summary_llm", _no_llm)
    monkeypatch.setattr("footnote_rag.db.session.dispose_engine", _keep_engine)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    args = note_diff.build_parser().parse_args(["--out", str(tmp_path / "reports")])
    assert args.summaries == 0 and args.live is False
    assert await note_diff._amain(args) == 0
    (report,) = (tmp_path / "reports").glob("*-fake-note-diff.md")
    assert "- detected: 5/5" in report.read_text(encoding="utf-8")


async def test_fake_summaries_report_status_latency_and_bullets(
    settings, fake_embedder, fake_dart, fake_llm
):
    report = await run_note_diff(
        FAKE_PAIRS,
        list(FAKE_TRUTH),
        settings=settings,
        embedder=fake_embedder,
        dart_client=fake_dart,
        mode="fake",
        dataset_name="built-in",
        summaries=2,
        llm=fake_llm,
        import_poll_seconds=0.01,
    )
    assert [s.note_key for s in report.summaries] == ["우발부채와약정사항", "사업결합"]
    assert all(s.status == "ok" for s in report.summaries)
    assert report.needs_review_rate == 0.0
    md = render_note_diff_report(report)
    assert "- model: `fake-llm`" in md and "- needs_review: 0.0% (0/2)" in md
    assert "latency p50 / p95 ms: 5 / 5" in md
    assert "### 가람전자(합성)" in md
