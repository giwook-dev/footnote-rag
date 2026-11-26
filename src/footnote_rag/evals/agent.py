"""에이전트 Eval: 과제(질문 + 기대 숫자·근거 종류)를 모델별로 실행해 성공률·근거 확인률·단계 수·도구 오류율·직접 계산 시도율·지연을 잰다.

역할: ``evals.run.import_filings``(임시 조직 + 정상 서비스·워커)로 과제의 공시를 한 번 가져오고, 모델마다
``agent.runner.run_agent``를 과제별로 실행해(앱과 같은 코드 경로, ``agent_runs``에도 저장) 결과를 채점하고
``evals/reports/{stamp}-{mode}-agent.md``를 쓴다. Fake 모드는 과제의 ``fake_script``를 재생하는 스크립트형
``FakeLLM``, ``--live``는 ``--local-models``의 Ollama 모델(모델마다 다른 모델을 내리고 예열)이다.
주요 요소: ``AgentTask``, ``load_tasks``, ``score_task``, ``TaskRun``, ``ModelRuns``(지표), ``run_agent_eval``,
``render_agent_report``, ``write_agent_report``, ``main``.
호출 관계: ``evals.run``(``import_filings``, ``parse_local_models``, ``preflight_local_models``,
``unload_unused_models``, ``percentile``), ``agent.runner``, ``providers``를 호출한다.
학습 문서: docs/study/12-agent.md, docs/adr/0013-tool-using-agent.md

    python -m footnote_rag.evals.agent [--live --local-models SPEC[,SPEC…]] [--tasks PATH]
                                       [--out evals/reports] [--import-timeout 1800]

Task row (JSONL)::

    {"id": "...", "question": "...", "rcept_nos": ["2026..."],
     "expected_numbers": [{"value": "64.6", "tolerance_pct": 0.5}],
     "expected_source_kinds": ["F", "K"], "must_not_answer": false,
     "fake_script": [...]}            # Fake mode only: the scripted model turns

Rows with ``"_example": true`` are skipped. Success: status ``completed``, every expected number
within its tolerance among the answer's numbers, and every expected source kind cited. A
``must_not_answer`` task succeeds when the run is ``needs_review`` or the answer says "찾지 못".
"직접 계산 시도" = the final answer had a number that no tool result (``calculate`` included) has.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from footnote_rag.agent.runner import CITATION_RE, REASON_LABELS, AgentOutcome, run_agent
from footnote_rag.answering.number_check import extract_numbers
from footnote_rag.config import Settings, get_settings
from footnote_rag.dart.client import DartClient
from footnote_rag.evals.run import (
    DEFAULT_IMPORT_TIMEOUT_SECONDS,
    PREFLIGHT_EXIT_CODE,
    EvalConfig,
    ModelSpec,
    _arg_type,
    import_filings,
    model_label,
    parse_local_models,
    percentile,
    preflight_local_models,
    render_preflight,
    unload_unused_models,
)
from footnote_rag.observability import get_logger
from footnote_rag.retrieval.embedder import Embedder

log = get_logger(__name__)

DEFAULT_TASKS = Path("evals/agent_tasks.jsonl")
DEFAULT_LIVE_TASKS = Path("evals/agent_tasks_live.jsonl")
NOT_FOUND_WORD = "찾지 못"


# ---------------------------------------------------------------- tasks


@dataclass(frozen=True)
class ExpectedNumber:
    value: str
    tolerance_pct: Decimal = Decimal(0)


@dataclass
class AgentTask:
    id: str
    question: str
    rcept_nos: list[str]
    expected_numbers: list[ExpectedNumber]
    expected_source_kinds: list[str]
    must_not_answer: bool = False
    fake_script: list[Any] | None = None
    example: bool = False

    @property
    def kind(self) -> str:
        if self.must_not_answer:
            return "unanswerable"
        if "K" in self.expected_source_kinds:
            return "ratio"
        if "C" in self.expected_source_kinds:
            return "note_change"
        return "lookup"


def _task(row: dict[str, Any], lineno: int) -> AgentTask:
    numbers = [
        ExpectedNumber(str(n["value"]), Decimal(str(n.get("tolerance_pct", 0))))
        for n in row.get("expected_numbers") or []
    ]
    rcept = row.get("rcept_nos") or []
    return AgentTask(
        id=str(row.get("id", f"line-{lineno}")),
        question=str(row.get("question", "")),
        rcept_nos=[str(r) for r in (rcept if isinstance(rcept, list) else [rcept])],
        expected_numbers=numbers,
        expected_source_kinds=[str(k) for k in row.get("expected_source_kinds") or []],
        must_not_answer=bool(row.get("must_not_answer", False)),
        fake_script=row.get("fake_script"),
        example=bool(row.get("_example", False)),
    )


def load_tasks(path: Path) -> list[AgentTask]:
    tasks: list[AgentTask] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON ({exc})") from None
        tasks.append(_task(row, lineno))
    return tasks


# ---------------------------------------------------------------- scoring


def answer_numbers(answer: str) -> list[Decimal]:
    out = []
    for _raw, value in extract_numbers(CITATION_RE.sub(" ", answer or "")):
        try:
            out.append(Decimal(value))
        except InvalidOperation:
            continue
    return out


def number_found(expected: ExpectedNumber, numbers: Sequence[Decimal]) -> bool:
    target = Decimal(expected.value.replace(",", ""))
    allowed = abs(target) * expected.tolerance_pct / Decimal(100)
    return any(abs(n - target) <= allowed for n in numbers)


def cited_kinds(outcome: AgentOutcome) -> list[str]:
    return sorted({str(c.get("id", ""))[:1] for c in outcome.citations if c.get("id")})


def score_task(task: AgentTask, outcome: AgentOutcome) -> tuple[bool, str]:
    """(success, why) of one run of one task."""
    answer = outcome.answer or ""
    if outcome.status == "failed":
        return False, f"failed ({outcome.error})"
    if task.must_not_answer:
        if outcome.status == "needs_review" or NOT_FOUND_WORD in answer:
            return True, "abstained / sent to review"
        return False, "answered an unanswerable question"
    problems = []
    if outcome.status != "completed":
        problems.append("needs_review: " + ", ".join(outcome.review_reasons))
    numbers = answer_numbers(answer)
    missing = [n.value for n in task.expected_numbers if not number_found(n, numbers)]
    if missing:
        problems.append("missing numbers " + ", ".join(missing))
    kinds = cited_kinds(outcome)
    absent = [k for k in task.expected_source_kinds if k not in kinds]
    if absent:
        problems.append("source kinds not cited " + ", ".join(absent))
    return (not problems), ("ok" if not problems else "; ".join(problems))


@dataclass
class TaskRun:
    task: AgentTask
    outcome: AgentOutcome | None
    success: bool
    why: str
    error: str | None = None

    @property
    def status(self) -> str:
        return self.outcome.status if self.outcome else "error"

    @property
    def direct_calc(self) -> bool:
        return bool(self.outcome and self.outcome.ungrounded_numbers)

    def has_reason(self, reason: str) -> bool:
        return bool(self.outcome and reason in self.outcome.review_reasons)


@dataclass
class ModelRuns:
    label: str
    model_id: str
    runs: list[TaskRun] = field(default_factory=list)

    def _rate(self, flags: Sequence[bool]) -> float | None:
        return sum(flags) / len(flags) if flags else None

    @property
    def success_rate(self) -> float | None:
        return self._rate([r.success for r in self.runs])

    @property
    def verified_rate(self) -> float | None:
        """Share of runs whose answer passed the code checks (status ``completed``)."""
        return self._rate([r.status == "completed" for r in self.runs])

    def _outcomes(self) -> list[AgentOutcome]:
        return [r.outcome for r in self.runs if r.outcome is not None]

    @property
    def avg_steps(self) -> float | None:
        done = self._outcomes()
        return sum(o.step_count for o in done) / len(done) if done else None

    @property
    def avg_tool_calls(self) -> float | None:
        done = self._outcomes()
        return sum(o.tool_call_count for o in done) / len(done) if done else None

    @property
    def tool_error_rate(self) -> float | None:
        done = self._outcomes()
        calls = sum(o.tool_call_count for o in done)
        return sum(o.tool_error_count for o in done) / calls if calls else None

    @property
    def step_limit_rate(self) -> float | None:
        return self._rate([r.has_reason("step_limit") for r in self.runs])

    @property
    def repeated_call_rate(self) -> float | None:
        return self._rate([r.has_reason("repeated_call") for r in self.runs])

    @property
    def direct_calc_rate(self) -> float | None:
        return self._rate([r.direct_calc for r in self.runs])

    @property
    def errors(self) -> int:
        return sum(1 for r in self.runs if r.status in ("failed", "error"))

    def latency(self, pct: float) -> float | None:
        return percentile([o.latency_ms for o in self._outcomes()], pct)


@dataclass
class AgentEvalReport:
    mode: str
    tasks_file: str
    tasks: list[AgentTask]
    skipped_examples: int
    settings: dict[str, Any]
    models: list[ModelRuns] = field(default_factory=list)
    import_failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- run


@dataclass
class AgentEvalConfig:
    """One model to evaluate: ``make_llm(task)`` gives the client for a task (Fake: the task's
    script; live: the same Ollama client every time)."""

    label: str
    make_llm: Callable[[AgentTask], Any]
    model_id: str


async def run_agent_eval(
    tasks: Sequence[AgentTask],
    configs: Sequence[AgentEvalConfig],
    *,
    settings: Settings,
    embedder: Embedder,
    dart_client: DartClient,
    mode: str,
    tasks_file: str,
    max_worker_loops: int = 1000,
    import_timeout_seconds: float = DEFAULT_IMPORT_TIMEOUT_SECONDS,
    import_poll_seconds: float = 2.0,
) -> AgentEvalReport:
    runnable = [t for t in tasks if not t.example]
    report = AgentEvalReport(
        mode=mode,
        tasks_file=tasks_file,
        tasks=runnable,
        skipped_examples=len(tasks) - len(runnable),
        settings={
            "max_steps": settings.agent_max_steps,
            "max_calls_per_step": settings.agent_max_calls_per_step,
            "timeout_seconds": settings.agent_timeout_seconds,
            "tool_result_chars": settings.agent_tool_result_chars,
        },
    )
    if not runnable:
        report.models = [ModelRuns(c.label, c.model_id) for c in configs]
        return report
    imported = await import_filings(
        [r for t in runnable for r in t.rcept_nos],
        settings=settings,
        embedder=embedder,
        dart_client=dart_client,
        label="agent-eval",
        max_worker_loops=max_worker_loops,
        import_timeout_seconds=import_timeout_seconds,
        import_poll_seconds=import_poll_seconds,
    )
    report.import_failures = imported.import_failures
    for config in configs:
        runs = ModelRuns(config.label, config.model_id)
        probe = config.make_llm(runnable[0])
        if callable(getattr(probe, "warm_up", None)):
            await unload_unused_models(EvalConfig(llm=probe))
            await probe.warm_up()
        for task in runnable:
            try:
                outcome = await run_agent(
                    task.question,
                    org_id=imported.org_id,
                    user_id=imported.admin_id,
                    settings=settings,
                    llm=config.make_llm(task),
                    embedder=embedder,
                    dart_client=dart_client,
                )
            except Exception as exc:  # noqa: BLE001 - one broken task must not stop the Eval
                log.exception("eval.agent_task_failed", task=task.id)
                runs.runs.append(TaskRun(task, None, False, "error", error=type(exc).__name__))
                continue
            success, why = score_task(task, outcome)
            runs.runs.append(TaskRun(task, outcome, success, why))
        report.models.append(runs)
    return report


# ---------------------------------------------------------------- report


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}"


def _cell(text: Any) -> str:
    return str(text if text is not None else "-").replace("|", "\\|").replace("\n", " ")


def _count(runs: ModelRuns, flags: Sequence[bool]) -> str:
    return f"{sum(flags)}/{len(flags)}"


def _model_rows(report: AgentEvalReport) -> list[str]:
    lines = [
        "| model | success | verified (completed) | avg steps | avg tool calls | tool error rate "
        "| step_limit | repeated_call | direct calc tries | errors | p50 ms | p95 ms |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m in report.models:
        lines.append(
            f"| {_cell(m.label)} | {_pct(m.success_rate)} ({_count(m, [r.success for r in m.runs])}) "
            f"| {_pct(m.verified_rate)} | {_num(m.avg_steps)} | {_num(m.avg_tool_calls)} "
            f"| {_pct(m.tool_error_rate)} | {_pct(m.step_limit_rate)} | {_pct(m.repeated_call_rate)} "
            f"| {_pct(m.direct_calc_rate)} | {m.errors} | {_ms(m.latency(50))} | {_ms(m.latency(95))} |"
        )
    return lines


STATUS_ABBREVIATIONS = {"completed": "ok", "needs_review": "rev", "failed": "fail", "error": "err"}


def _task_cell(run: TaskRun | None) -> str:
    if run is None:
        return "-"
    mark = "✓" if run.success else "✗"
    steps = run.outcome.step_count if run.outcome else 0
    return f"{mark} {steps} steps {STATUS_ABBREVIATIONS.get(run.status, run.status)}"


def _step_line(step: dict[str, Any]) -> str:
    if step.get("kind") == "model":
        label = "final answer" if step.get("final") else "model"
        forced = " (forced, no tools)" if step.get("forced_final") else ""
        return f"{step['idx']}. {label}{forced}: {_cell(step.get('summary'))}"
    ok = "ok" if step.get("ok") else "error"
    return (
        f"{step['idx']}. `{_cell(step.get('args_summary'))}` → {ok}: {_cell(step.get('summary'))}"
    )


def _failure_sections(report: AgentEvalReport) -> list[str]:
    lines = ["## Failed tasks", ""]
    failed = [(m, r) for m in report.models for r in m.runs if not r.success]
    if not failed:
        return [*lines, "None.", ""]
    for model, run in failed:
        o = run.outcome
        lines += [f"### {model.label} · {run.task.id}", ""]
        lines.append(f"- question: {_cell(run.task.question)}")
        lines.append(f"- status: {run.status} · why: {_cell(run.why)}")
        if o is not None:
            reasons = ", ".join(f"{r} ({REASON_LABELS.get(r, r)})" for r in o.review_reasons)
            lines.append(f"- review reasons: {reasons or 'none'}")
            if o.ungrounded_numbers:
                lines.append(f"- numbers not in any tool result: {', '.join(o.ungrounded_numbers)}")
            if o.dropped_citations:
                lines.append(f"- removed citation marks: {', '.join(o.dropped_citations)}")
            lines.append(f"- answer: {_cell(o.answer)}")
            lines += ["- steps:", *(f"  {_step_line(s)}" for s in o.steps)]
        elif run.error:
            lines.append(f"- error: {run.error}")
        lines.append("")
    return lines


def render_agent_report(report: AgentEvalReport) -> str:
    s = report.settings
    lines = [
        f"# Agent Eval ({report.mode})",
        "",
        f"- generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- tasks: `{report.tasks_file}` ({len(report.tasks)} runnable, "
        f"{report.skipped_examples} `_example` rows skipped)",
        "- models: " + (", ".join(f"`{m.label}`" for m in report.models) or "none"),
        f"- limits: {s['max_steps']} steps, {s['max_calls_per_step']} calls per step, "
        f"{s['timeout_seconds']:.0f} s per run, {s['tool_result_chars']} chars per tool result",
        "- success = completed + expected numbers (within tolerance) + expected source kinds cited; "
        "unanswerable tasks succeed when the run abstains or goes to review",
        "- direct calc tries = the final answer had a number found in no tool result "
        "(calculate results included)",
        "",
    ]
    if report.mode == "fake":
        lines += [
            "> Fake mode: a scripted FakeLLM replays each task's `fake_script` over the synthetic",
            "> 가람전자(합성) filings. It checks the loop, the tools and the checks, not a model.",
            "",
        ]
    if not report.tasks:
        lines += ["No runnable tasks (only `_example` rows).", ""]
        return "\n".join(lines)
    if report.import_failures:
        lines += ["## Import failures", "", *(f"- {f}" for f in report.import_failures), ""]
    lines += ["## Models", "", *_model_rows(report), ""]
    header = "| id | type | " + " | ".join(_cell(m.label) for m in report.models) + " |"
    lines += [
        "## Tasks",
        "",
        "`ok` = completed, `rev` = needs_review, `fail` = failed",
        "",
        header,
        "| --- | --- | " + " | ".join("---" for _ in report.models) + " |",
    ]
    for task in report.tasks:
        cells = [
            _task_cell(next((r for r in m.runs if r.task.id == task.id), None))
            for m in report.models
        ]
        lines.append(f"| {_cell(task.id)} | {task.kind} | " + " | ".join(cells) + " |")
    lines.append("")
    lines += _failure_sections(report)
    return "\n".join(lines).rstrip("\n") + "\n"


def agent_report_filename(mode: str, stamp: str) -> str:
    return f"{stamp}-{mode}-agent.md"


def write_agent_report(report: AgentEvalReport, out_dir: Path, stamp: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / agent_report_filename(report.mode, stamp)
    path.write_text(render_agent_report(report), encoding="utf-8")
    return path


# ---------------------------------------------------------------- CLI


def fake_config() -> AgentEvalConfig:
    from footnote_rag.answering.llm import FakeLLM

    return AgentEvalConfig(
        label="fake-llm (scripted)",
        make_llm=lambda task: FakeLLM(agent_script=task.fake_script or []),
        model_id="fake-llm",
    )


def live_configs(settings: Settings, specs: Sequence[ModelSpec]) -> list[AgentEvalConfig]:
    from footnote_rag.providers import make_local_llm

    configs = []
    for spec in specs:
        llm = make_local_llm(settings, model=spec.model, think=spec.think)
        configs.append(
            AgentEvalConfig(
                label=model_label(spec.model, spec.think),
                make_llm=lambda _task, llm=llm: llm,
                model_id=llm.model_id,
            )
        )
    return configs


async def _amain(args: argparse.Namespace) -> int:
    from footnote_rag.dart.client import FakeDartClient
    from footnote_rag.db.session import dispose_engine
    from footnote_rag.providers import make_dart_client, make_embedder
    from footnote_rag.retrieval.embedder import FakeEmbedder

    settings = get_settings()
    tasks_path = args.tasks or (DEFAULT_LIVE_TASKS if args.live else DEFAULT_TASKS)
    tasks = load_tasks(tasks_path)
    if args.live:
        specs = args.local_models or [ModelSpec(settings.local_llm_model)]
        configs = live_configs(settings, specs)
        problems = await preflight_local_models(
            [EvalConfig(llm=c.make_llm(tasks[0] if tasks else None)) for c in configs]
        )
        if problems:
            print(render_preflight(problems), file=sys.stderr)
            return PREFLIGHT_EXIT_CODE
        embedder, dart, mode = make_embedder(settings), make_dart_client(settings), "live"
    else:
        configs = [fake_config()]
        embedder, dart, mode = FakeEmbedder(settings.embedding_dim), FakeDartClient(), "fake"
    try:
        report = await run_agent_eval(
            tasks,
            configs,
            settings=settings,
            embedder=embedder,
            dart_client=dart,
            mode=mode,
            tasks_file=str(tasks_path),
            import_timeout_seconds=args.import_timeout,
        )
    finally:
        await dispose_engine()
    path = write_agent_report(report, args.out)
    print(f"report written: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="footnote_rag.evals.agent")
    parser.add_argument(
        "--live", action="store_true", help="real filings and local Ollama models (no Claude)"
    )
    parser.add_argument(
        "--local-models",
        type=_arg_type(parse_local_models),
        default=None,
        metavar="SPEC[,SPEC…]",
        help="Ollama models to compare (model[@think]), e.g. qwen3:14b,gpt-oss:20b@low",
    )
    parser.add_argument("--tasks", type=Path, default=None, help="task file (JSONL)")
    parser.add_argument("--out", type=Path, default=Path("evals/reports"))
    parser.add_argument("--import-timeout", type=float, default=DEFAULT_IMPORT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if args.local_models and not args.live:
        build_parser().error("--local-models needs --live")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
