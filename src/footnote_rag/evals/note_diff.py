"""전기 대비 주석 변동 Eval: 정답 탐지율, 쌍별 통계(짝짓기·상태·전기 불일치·큰 변동), 선택적으로 로컬 모델 요약.

역할: ``evals.run.import_filings``(임시 조직 + 정상 서비스·워커로 가져오기 + ``wait_for_imports``)로 비교 쌍의 문서를
가져오고, 쌍마다 ``comparison.service.create_comparison``을 실행해 ``evals/reports/{stamp}-{mode}-note-diff.md``를 쓴다.
``--summaries N``일 때만 쌍마다 점수 상위 N개 주석을 로컬 모델(``providers.make_local_llm``, Fake 모드는 ``FakeLLM``)로
요약한다. 없으면 LLM 클라이언트를 만들지도 부르지도 않는다.
주요 요소: ``run_note_diff``, ``NoteDiffEval``, ``PairRun``, ``TruthResult``, ``SummaryRun``, ``detect``,
``render_note_diff_report``, ``write_note_diff_report``, ``FAKE_TRUTH``, ``main``.
호출 관계: ``evals.run``(``import_filings``, ``percentile``, ``preflight_local_models``), ``comparison.service``,
``evals.validate_note_changes``(정답 데이터 로더)를 호출한다.
학습 문서: docs/study/11-note-change-analysis.md, docs/adr/0012-note-change-analysis.md

    python -m footnote_rag.evals.note_diff [--live] [--summaries N] [--dataset evals/note_changes.jsonl]
                                          [--out evals/reports] [--import-timeout 1800]

* Fake mode (default): the synthetic 가람전자(합성) pair (``dart.fake_data.FAKE_ANNUAL_PAIR``) and the
  built-in ``FAKE_TRUTH`` items; FakeEmbedder / FakeDartClient, no network, no cost.
* ``--live``: the three real pairs (``validate_note_changes.REAL_PAIRS``) with the configured
  embedder (Voyage, document embeddings cached) and OpenDART; ground truth from ``--dataset``.
* Detection: a line change counts when an insert (``line_added``) / delete (``line_removed``) line
  of the note contains the quote; ``number_changed`` when the note has a ``yoy`` change of the same
  row label; ``note_added``/``note_removed`` when the note has that status.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from footnote_rag.answering.llm import LLMError
from footnote_rag.comparison.notes import CONSOLIDATED
from footnote_rag.comparison.service import ComparisonError, create_comparison, summarize_note
from footnote_rag.config import Settings, get_settings
from footnote_rag.dart import fake_data
from footnote_rag.dart.client import DartClient
from footnote_rag.dart.parser import normalize_ws
from footnote_rag.db.models import User
from footnote_rag.evals.run import (
    DEFAULT_IMPORT_TIMEOUT_SECONDS,
    PREFLIGHT_EXIT_CODE,
    DocumentImport,
    EvalConfig,
    import_filings,
    percentile,
    preflight_local_models,
    render_preflight,
)
from footnote_rag.evals.validate_note_changes import (
    DEFAULT_NOTE_CHANGES,
    REAL_PAIRS,
    NoteChangeItem,
    item_from_row,
    load_note_changes,
)
from footnote_rag.observability import get_logger
from footnote_rag.retrieval.embedder import Embedder

log = get_logger(__name__)

TOP_N = 10
FAKE_PAIRS: tuple[tuple[str, str, str], ...] = (
    (fake_data.FAKE_CORPS[0]["corp_name"], *fake_data.FAKE_ANNUAL_PAIR),
)
_FP, _FC = fake_data.FAKE_ANNUAL_PAIR
# Changes built into the synthetic pair (dart/fake_data.py); the live run uses evals/note_changes.jsonl.
FAKE_TRUTH: tuple[NoteChangeItem, ...] = tuple(
    item_from_row({"prior_rcept_no": _FP, "current_rcept_no": _FC, "scope": CONSOLIDATED, **row})
    for row in (
        {"id": "fake-note-added", "note_key": "사업결합", "kind": "note_added"},
        {"id": "fake-note-removed", "note_key": "매각예정비유동자산", "kind": "note_removed"},
        {
            "id": "fake-line-added",
            "note_key": "우발부채와약정사항",
            "kind": "line_added",
            "current_quote": "합성은행과 운영자금 한도약정을 새로 체결하였습니다.",
        },
        {
            "id": "fake-line-removed",
            "note_key": "우발부채와약정사항",
            "kind": "line_removed",
            "prior_quote": "지급보증을 제공하고 있습니다.",
        },
        {
            "id": "fake-number-changed",
            "note_key": "차입금",
            "kind": "number_changed",
            "row_label": "단기차입금",
            "prior_value": "3,000",
            "current_value": "5,000",
        },
    )
)


@dataclass
class PairRun:
    company: str
    prior_rcept_no: str
    current_rcept_no: str
    scope: str
    result: dict[str, Any] | None = None
    comparison_id: uuid.UUID | None = None
    elapsed_ms: int | None = None
    error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.company} {self.prior_rcept_no} → {self.current_rcept_no} ({self.scope})"

    def note(self, key: str) -> dict[str, Any] | None:
        notes = (self.result or {}).get("notes") or []
        return next((n for n in notes if n["key"] == key), None)


@dataclass
class TruthResult:
    item: NoteChangeItem
    detected: bool
    reason: str


@dataclass
class SummaryRun:
    pair: str
    note_key: str
    status: str | None
    ungrounded_numbers: list[str] = field(default_factory=list)
    latency_ms: int | None = None
    bullets: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class NoteDiffEval:
    mode: str
    dataset: str
    settings: dict[str, Any]
    pairs: list[PairRun] = field(default_factory=list)
    truth: list[TruthResult] = field(default_factory=list)
    summaries: list[SummaryRun] = field(default_factory=list)
    summaries_requested: int = 0
    summary_model: str | None = None
    imports: list[DocumentImport] = field(default_factory=list)
    import_failures: list[str] = field(default_factory=list)

    @property
    def detection_rate(self) -> float | None:
        return sum(t.detected for t in self.truth) / len(self.truth) if self.truth else None

    @property
    def needs_review_rate(self) -> float | None:
        done = [s for s in self.summaries if s.status is not None]
        return sum(s.status == "needs_review" for s in done) / len(done) if done else None


# ---------------------------------------------------------------- detection


def _norm(text: str | None) -> str:
    return normalize_ws(str(text or "").replace("\n", " "))


def _line_has(note: dict[str, Any], op: str, quote: str | None) -> bool:
    needle = _norm(quote)
    return bool(needle) and any(
        e["op"] == op and needle in _norm(e["text"]) for e in note.get("lines") or []
    )


def _same_label(label: str, wanted: str | None) -> bool:
    a, b = _norm(label), _norm(wanted)
    return bool(b) and (a == b or b in a)


def detect(item: NoteChangeItem, pair: PairRun | None) -> TruthResult:
    """Was the ground-truth change found by the comparison of its pair?"""
    if pair is None or pair.result is None:
        return TruthResult(item, False, "pair not compared" + (f": {pair.error}" if pair else ""))
    note = pair.note(item.note_key)
    if note is None:
        return TruthResult(item, False, "note key not in the result")
    status = note["status"]
    if item.kind == "note_added":
        return TruthResult(item, status == "added", f"status {status}")
    if item.kind == "note_removed":
        return TruthResult(item, status == "removed", f"status {status}")
    if item.kind == "line_added":
        found = _line_has(note, "insert", item.current_quote)
        return TruthResult(item, found, "insert line" if found else "no insert line with the quote")
    if item.kind == "line_removed":
        found = _line_has(note, "delete", item.prior_quote)
        return TruthResult(item, found, "delete line" if found else "no delete line with the quote")
    if item.kind == "number_changed":
        found = any(
            c["kind"] == "yoy" and _same_label(c["label"], item.row_label)
            for c in note.get("number_changes") or []
        )
        return TruthResult(item, found, "yoy row" if found else "no yoy row with that label")
    return TruthResult(item, False, f"unknown kind {item.kind}")


# ---------------------------------------------------------------- run


async def _compare_pair(
    pair: PairRun,
    *,
    admin_id: uuid.UUID,
    rcept_to_doc: dict[str, uuid.UUID],
    dart: DartClient,
    settings: Settings,
) -> None:
    from footnote_rag.db.session import get_sessionmaker

    prior_id = rcept_to_doc.get(pair.prior_rcept_no)
    current_id = rcept_to_doc.get(pair.current_rcept_no)
    if prior_id is None or current_id is None:
        pair.error = "not imported"
        return
    started = time.perf_counter()
    async with get_sessionmaker()() as session:
        user = await session.get(User, admin_id)
        assert user is not None
        try:
            comparison = await create_comparison(
                session,
                user=user,
                prior_document_id=prior_id,
                current_document_id=current_id,
                scope=pair.scope,
                dart_client=dart,
                settings=settings,
            )
        except ComparisonError as exc:
            pair.error = f"{exc.status_code} {exc.message}"
            return
        pair.result, pair.comparison_id = comparison.result, comparison.id
    pair.elapsed_ms = int((time.perf_counter() - started) * 1000)


async def _summarize_top(
    pair: PairRun, n: int, *, admin_id: uuid.UUID, llm: Any, settings: Settings
) -> list[SummaryRun]:
    from footnote_rag.db.session import get_sessionmaker

    if pair.result is None or pair.comparison_id is None:
        return []
    out: list[SummaryRun] = []
    notes = [x for x in pair.result["notes"] if x["status"] != "unchanged"][:n]
    for note in notes:
        async with get_sessionmaker()() as session:
            user = await session.get(User, admin_id)
            assert user is not None
            try:
                done = await summarize_note(
                    session,
                    user=user,
                    comparison_id=pair.comparison_id,
                    note_index=note["index"],
                    llm=llm,
                    settings=settings,
                )
            except (LLMError, ComparisonError) as exc:
                message = getattr(exc, "public_message", None) or getattr(exc, "message", str(exc))
                out.append(SummaryRun(pair.label, note["key"], None, error=str(message)))
                continue
        s = done.summary
        out.append(
            SummaryRun(
                pair.label,
                note["key"],
                s.status,
                list(s.ungrounded_numbers or []),
                done.latency_ms,
                list(s.bullets or []),
            )
        )
    return out


async def run_note_diff(
    pairs: tuple[tuple[str, str, str], ...] | list[tuple[str, str, str]],
    truth: list[NoteChangeItem] | tuple[NoteChangeItem, ...],
    *,
    settings: Settings,
    embedder: Embedder,
    dart_client: DartClient,
    mode: str,
    dataset_name: str,
    summaries: int = 0,
    llm: Any = None,
    max_worker_loops: int = 1000,
    import_timeout_seconds: float = DEFAULT_IMPORT_TIMEOUT_SECONDS,
    import_poll_seconds: float = 2.0,
) -> NoteDiffEval:
    """Import the pairs' filings once, compare every (pair, scope) the truth needs (always the
    consolidated scope), score the truth, and summarize the top notes when ``summaries > 0``."""
    report = NoteDiffEval(
        mode=mode,
        dataset=dataset_name,
        settings={
            "change_threshold": str(settings.note_diff_change_threshold),
            "max_lines": settings.note_diff_max_lines,
            "watchlist": list(settings.note_diff_watchlist_words),
        },
        summaries_requested=summaries,
    )
    imported = await import_filings(
        [r for _c, p, c in pairs for r in (p, c)],
        settings=settings,
        embedder=embedder,
        dart_client=dart_client,
        label="note-diff",
        max_worker_loops=max_worker_loops,
        import_timeout_seconds=import_timeout_seconds,
        import_poll_seconds=import_poll_seconds,
    )
    report.imports, report.import_failures = imported.imports, imported.import_failures
    runs: dict[tuple[str, str, str], PairRun] = {}
    for company, prior, current in pairs:
        scopes = [CONSOLIDATED] + sorted(
            {
                t.scope
                for t in truth
                if (t.prior_rcept_no, t.current_rcept_no) == (prior, current)
                and t.scope != CONSOLIDATED
            }
        )
        for scope in scopes:
            run = PairRun(company, prior, current, scope)
            await _compare_pair(
                run,
                admin_id=imported.admin_id,
                rcept_to_doc=imported.rcept_to_doc,
                dart=dart_client,
                settings=settings,
            )
            runs[(prior, current, scope)] = run
            report.pairs.append(run)
    report.truth = [
        detect(t, runs.get((t.prior_rcept_no, t.current_rcept_no, t.scope))) for t in truth
    ]
    if summaries > 0 and llm is not None:
        report.summary_model = str(getattr(llm, "model_id", None) or getattr(llm, "model", ""))
        warm = getattr(llm, "warm_up", None)
        if callable(warm):
            await warm()
        # Summaries run on the local (or fake) model whatever LLM_PROVIDER says (never Claude).
        summary_settings = (
            settings.model_copy(update={"llm_provider": "local"})
            if settings.llm_mode == "real"
            else settings
        )
        for run in report.pairs:
            if run.scope == CONSOLIDATED:
                report.summaries += await _summarize_top(
                    run, summaries, admin_id=imported.admin_id, llm=llm, settings=summary_settings
                )
    return report


# ---------------------------------------------------------------- report


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _cell(text: Any) -> str:
    return str(text if text is not None else "-").replace("|", "\\|").replace("\n", " ")


def _abs_decimal(text: str | None) -> Decimal:
    try:
        return abs(Decimal(str(text)))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _top_changes(result: dict[str, Any], kind: str) -> list[tuple[dict[str, Any], dict, dict]]:
    """(note, change, cell) rows: prior mismatches by |delta|, large changes by |change %|
    (0 -> non-zero first)."""
    rows = []
    for note in result.get("notes") or []:
        for change in note.get("number_changes") or []:
            if kind == "prior_mismatch" and change["kind"] == "prior_mismatch":
                cells = change["cells"] or [{}]
                rows += [(note, change, cell) for cell in cells]
            elif kind == "large_change" and change["kind"] == "yoy":
                rows += [
                    (note, change, cell)
                    for cell in change["cells"]
                    if "large_change" in cell["flags"]
                ]

    def weight(row: tuple[dict, dict, dict]) -> Decimal:
        cell = row[2]
        if kind == "prior_mismatch":
            return _abs_decimal(cell.get("delta"))
        pct = cell.get("change_pct")
        return Decimal("Infinity") if pct is None else _abs_decimal(pct)

    return sorted(rows, key=weight, reverse=True)[:TOP_N]


def _render_pair(run: PairRun) -> list[str]:
    lines = [f"## {run.label}", ""]
    if run.result is None:
        return [*lines, f"- not compared: {_cell(run.error)}", ""]
    r, s = run.result, run.result["summary"]
    notes = r["notes"]
    similar = [n for n in notes if n["matched_by"] == "similar_title"]
    added = [n["key"] for n in notes if n["status"] == "added"]
    removed = [n["key"] for n in notes if n["status"] == "removed"]
    matched = s["matched_by"]
    total = s["notes"] or 1
    lines += [
        f"- processing time: {run.elapsed_ms} ms",
        f"- notes: prior {s['prior_notes']}, current {s['current_notes']}",
        f"- matching: key {matched['key']} / similar title {matched['similar_title']} / "
        f"unmatched {len(added) + len(removed)} "
        f"(matched {(matched['key'] + matched['similar_title']) / total * 100:.1f}% of {s['notes']})",
        "- similar-title pairs: "
        + (", ".join(f"`{n['prior_title']}` → `{n['current_title']}`" for n in similar) or "none"),
        f"- unmatched (added): {', '.join(f'`{k}`' for k in added) or 'none'}",
        f"- unmatched (removed): {', '.join(f'`{k}`' for k in removed) or 'none'}",
        f"- ambiguous rows (repeated rows, not compared): {s['ambiguous_rows']}",
        *([f"- warnings: {_cell(' '.join(r['warnings']))}"] if r.get("warnings") else []),
        "",
        "| status | notes |",
        "| --- | --- |",
        *(f"| {k} | {v} |" for k, v in s["status_counts"].items()),
        "",
        f"### Prior mismatches (top {TOP_N} of {s['prior_mismatch']})",
        "",
        "| note | table | row | prior report 당기 | current report 전기 | difference | refs |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    mismatches = _top_changes(r, "prior_mismatch")
    lines += [
        f"| {_cell(n['key'])} | {_cell(c['table'])} | {_cell(c['label'])} "
        f"| {_cell(cell.get('prior', ', '.join(c['prior_values'])))} "
        f"| {_cell(cell.get('current', ', '.join(c['current_values'])))} "
        f"| {_cell(cell.get('delta'))} | {c['prior_ref']} / {c['current_ref']} |"
        for n, c, cell in mismatches
    ] or ["| - | - | - | - | - | - | - |"]
    lines += [
        "",
        f"### Large changes (top {TOP_N} of {s['large_change']})",
        "",
        "| note | table | row | prior | current | delta | change % |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    large = _top_changes(r, "large_change")
    lines += [
        f"| {_cell(n['key'])} | {_cell(c['table'])} | {_cell(c['label'])} | {cell['prior']} "
        f"| {cell['current']} | {cell['delta']} | {_cell(cell['change_pct'])} |"
        for n, c, cell in large
    ] or ["| - | - | - | - | - | - | - |"]
    return [*lines, ""]


def _render_summaries(report: NoteDiffEval) -> list[str]:
    if report.summaries_requested <= 0:
        return ["## Summaries", "", "Not requested (`--summaries N`): no LLM was called.", ""]
    latencies = [s.latency_ms for s in report.summaries if s.latency_ms is not None]
    p50, p95 = percentile(latencies, 50), percentile(latencies, 95)
    done = [s for s in report.summaries if s.status is not None]
    lines = [
        "## Summaries",
        "",
        f"- model: `{report.summary_model}` (local only, no cascade), top {report.summaries_requested} "
        "notes per pair by score",
        f"- needs_review: {_pct(report.needs_review_rate)} "
        f"({sum(s.status == 'needs_review' for s in done)}/{len(done)})",
        f"- errors: {sum(1 for s in report.summaries if s.error)}",
        f"- latency p50 / p95 ms: {'n/a' if p50 is None else f'{p50:.0f}'} / "
        f"{'n/a' if p95 is None else f'{p95:.0f}'}",
        "",
        "| pair | note | status | ungrounded numbers | latency ms |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {_cell(s.pair)} | {_cell(s.note_key)} | {s.status or 'error: ' + _cell(s.error)} "
        f"| {', '.join(f'`{_cell(x)}`' for x in s.ungrounded_numbers)} | {_cell(s.latency_ms)} |"
        for s in report.summaries
    ]
    lines.append("")
    for s in report.summaries:
        if not s.bullets:
            continue
        lines += [f"### {s.pair} · {s.note_key} ({s.status})", ""]
        for b in s.bullets:
            refs = " ".join(f"[{r}]" for r in b.get("refs") or [])
            mark = "" if b.get("grounded") else " — 확인 필요"
            lines.append(f"- {_cell(b.get('text'))} {refs}{mark}")
        lines.append("")
    return lines


def render_note_diff_report(report: NoteDiffEval) -> str:
    detected = sum(t.detected for t in report.truth)
    lines = [
        f"# Note change Eval ({report.mode})",
        "",
        f"- generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- ground truth: `{report.dataset}` ({len(report.truth)} rows)",
        "- pairs: " + ", ".join(f"`{p.label}`" for p in report.pairs),
        f"- change threshold: {report.settings['change_threshold']}, max lines per note: "
        f"{report.settings['max_lines']}",
        f"- watchlist: {', '.join(report.settings['watchlist'])}",
        "",
    ]
    if report.mode == "fake":
        lines += [
            "> Fake mode: the synthetic 가람전자(합성) pair and its built-in changes. Checks the",
            "> mechanics, not real filings.",
            "",
        ]
    if report.import_failures:
        lines += ["## Import failures", "", *(f"- {f}" for f in report.import_failures), ""]
    lines += [
        "## Ground truth detection",
        "",
        f"- detected: {detected}/{len(report.truth)} ({_pct(report.detection_rate)})",
        "",
        "| id | pair | kind | note_key | detected | why |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {_cell(t.item.id)} | {t.item.prior_rcept_no} → {t.item.current_rcept_no} "
        f"| {t.item.kind} | {_cell(t.item.note_key)} | {'✓' if t.detected else '✗'} "
        f"| {_cell(t.reason)} |"
        for t in report.truth
    ] or ["| - | - | - | - | - | no ground-truth rows |"]
    lines.append("")
    for run in report.pairs:
        lines += _render_pair(run)
    lines += _render_summaries(report)
    return "\n".join(lines).rstrip("\n") + "\n"


def note_diff_report_filename(mode: str, stamp: str) -> str:
    return f"{stamp}-{mode}-note-diff.md"


def write_note_diff_report(report: NoteDiffEval, out_dir: Path, stamp: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / note_diff_report_filename(report.mode, stamp)
    path.write_text(render_note_diff_report(report), encoding="utf-8")
    return path


# ---------------------------------------------------------------- CLI


def make_summary_llm(settings: Settings, live: bool) -> Any:
    """Only called with ``--summaries``: the local model (live) or ``FakeLLM`` (fake)."""
    from footnote_rag.answering.llm import FakeLLM
    from footnote_rag.providers import make_local_llm

    return make_local_llm(settings) if live else FakeLLM()


async def _amain(args: argparse.Namespace) -> int:
    from footnote_rag.dart.client import FakeDartClient
    from footnote_rag.db.session import dispose_engine
    from footnote_rag.providers import make_dart_client, make_embedder
    from footnote_rag.retrieval.embedder import FakeEmbedder

    settings = get_settings()
    llm = make_summary_llm(settings, args.live) if args.summaries > 0 else None
    if args.live and llm is not None:
        problems = await preflight_local_models([EvalConfig(llm=llm)])
        if problems:
            print(render_preflight(problems), file=sys.stderr)
            return PREFLIGHT_EXIT_CODE
    if args.live:
        embedder, dart, mode = make_embedder(settings), make_dart_client(settings), "live"
        pairs: tuple[tuple[str, str, str], ...] = REAL_PAIRS
        truth = [t for t in load_note_changes(args.dataset) if not t.example]
        dataset = str(args.dataset)
    else:
        embedder, dart, mode = FakeEmbedder(settings.embedding_dim), FakeDartClient(), "fake"
        pairs, truth, dataset = FAKE_PAIRS, list(FAKE_TRUTH), "built-in FAKE_TRUTH"
    try:
        report = await run_note_diff(
            pairs,
            truth,
            settings=settings,
            embedder=embedder,
            dart_client=dart,
            mode=mode,
            dataset_name=dataset,
            summaries=args.summaries,
            llm=llm,
            import_timeout_seconds=args.import_timeout,
        )
    finally:
        await dispose_engine()
    path = write_note_diff_report(report, args.out)
    print(f"report written: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="footnote_rag.evals.note_diff")
    parser.add_argument("--live", action="store_true", help="real filings (Voyage, OpenDART keys)")
    parser.add_argument(
        "--summaries",
        type=int,
        default=0,
        metavar="N",
        help="summarize the top N notes of each pair with the local model (default 0: no LLM)",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_NOTE_CHANGES)
    parser.add_argument("--out", type=Path, default=Path("evals/reports"))
    parser.add_argument("--import-timeout", type=float, default=DEFAULT_IMPORT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if args.summaries < 0:
        build_parser().error("--summaries must be >= 0")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
