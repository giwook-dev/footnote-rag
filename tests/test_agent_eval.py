"""에이전트 Eval(evals/agent.py): 과제 파일, 채점, Fake 모드 실행과 리포트. 실제 Postgres, 네트워크 없음."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from footnote_rag.agent.runner import AgentOutcome
from footnote_rag.evals import agent as agent_eval
from footnote_rag.evals.agent import (
    DEFAULT_LIVE_TASKS,
    DEFAULT_TASKS,
    AgentTask,
    ExpectedNumber,
    fake_config,
    load_tasks,
    number_found,
    render_agent_report,
    run_agent_eval,
    score_task,
    write_agent_report,
)

REPO_ROOT = Path(__file__).parent.parent


def _outcome(**changes) -> AgentOutcome:
    base = {
        "run_id": None,
        "status": "completed",
        "answer": "부채비율은 64.6%입니다 [K1].",
        "citations": [{"id": "K1"}, {"id": "F1"}],
        "review_reasons": [],
        "step_count": 4,
        "tool_call_count": 3,
        "tool_error_count": 0,
        "tool_counts": {},
        "latency_ms": 10,
        "model": "fake-llm",
    }
    return AgentOutcome(**{**base, **changes})


def _task(**changes) -> AgentTask:
    base = {
        "id": "t",
        "question": "q",
        "rcept_nos": [],
        "expected_numbers": [ExpectedNumber("64.6", Decimal("0.5"))],
        "expected_source_kinds": ["F", "K"],
    }
    return AgentTask(**{**base, **changes})


def test_task_files_follow_the_schema():
    fake = load_tasks(REPO_ROOT / DEFAULT_TASKS)
    assert 6 <= len(fake) <= 8 and not any(t.example for t in fake)
    assert {t.kind for t in fake} >= {"ratio", "note_change", "unanswerable"}
    assert all(t.fake_script and t.rcept_nos for t in fake)
    live_rows = [
        json.loads(line)
        for line in (REPO_ROOT / DEFAULT_LIVE_TASKS).read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert live_rows and all(r.get("_example") is True for r in live_rows)  # no invented figures
    assert all(t.example for t in load_tasks(REPO_ROOT / DEFAULT_LIVE_TASKS))


def test_number_tolerance_and_scoring():
    assert number_found(ExpectedNumber("64.6", Decimal("0.5")), [Decimal("64.8")])
    assert not number_found(ExpectedNumber("64.6", Decimal("0.1")), [Decimal("64.8")])
    assert number_found(ExpectedNumber("1.0"), [Decimal("1")])
    assert score_task(_task(), _outcome()) == (True, "ok")
    ok, why = score_task(_task(), _outcome(status="needs_review", review_reasons=["step_limit"]))
    assert not ok and "step_limit" in why
    ok, why = score_task(_task(), _outcome(citations=[{"id": "F1"}]))
    assert not ok and "source kinds not cited K" in why
    ok, why = score_task(_task(), _outcome(answer="부채비율은 70%입니다 [K1]."))
    assert not ok and "missing numbers 64.6" in why
    unanswerable = _task(must_not_answer=True, expected_numbers=[], expected_source_kinds=[])
    assert score_task(unanswerable, _outcome(status="needs_review"))[0]
    assert not score_task(unanswerable, _outcome())[0]
    assert not score_task(_task(), _outcome(status="failed", error="timeout"))[0]


async def test_fake_agent_eval_writes_report_with_metrics(settings, fake_embedder, tmp_path):
    from footnote_rag.dart.client import FakeDartClient

    tasks = load_tasks(REPO_ROOT / DEFAULT_TASKS)
    report = await run_agent_eval(
        tasks,
        [fake_config()],
        settings=settings,
        embedder=fake_embedder,
        dart_client=FakeDartClient(),
        mode="fake",
        tasks_file=str(DEFAULT_TASKS),
        import_poll_seconds=0.01,
    )
    (model,) = report.models
    by_id = {r.task.id: r for r in model.runs}
    assert len(by_id) == len(tasks)
    for task_id in (
        "fake-debt-ratio",
        "fake-debt-ratio-yoy",
        "fake-operating-margin",
        "fake-new-commitment",
        "fake-unanswerable-rnd",
    ):
        assert by_id[task_id].success, (task_id, by_id[task_id].why, by_id[task_id].outcome.answer)
    assert not by_id["fake-direct-calculation"].success
    assert by_id["fake-direct-calculation"].direct_calc
    assert by_id["fake-repeated-call"].has_reason("repeated_call")
    assert by_id["fake-step-limit"].has_reason("step_limit")
    assert model.success_rate == pytest.approx(5 / 8)
    assert model.direct_calc_rate == pytest.approx(1 / 8)
    assert model.step_limit_rate == pytest.approx(1 / 8)
    assert model.repeated_call_rate == pytest.approx(1 / 8)
    assert model.tool_error_rate is not None and 0 < model.tool_error_rate < 0.5
    assert model.avg_steps and model.avg_tool_calls and model.latency(50) is not None

    path = write_agent_report(report, tmp_path, stamp="S")
    assert path.name == "S-fake-agent.md"
    text = path.read_text("utf-8")
    for heading in ("# Agent Eval (fake)", "## Models", "## Tasks", "## Failed tasks"):
        assert heading in text
    assert "| fake-llm (scripted) | 62.5% (5/8) |" in text
    assert "### fake-llm (scripted) · fake-direct-calculation" in text
    assert "numbers not in any tool result: 60.8" in text
    assert "get_financials(" in text  # step records of failed tasks


def test_report_without_runnable_tasks():
    report = agent_eval.AgentEvalReport(
        mode="live",
        tasks_file="evals/agent_tasks_live.jsonl",
        tasks=[],
        skipped_examples=1,
        settings={
            "max_steps": 6,
            "max_calls_per_step": 3,
            "timeout_seconds": 300,
            "tool_result_chars": 4000,
        },
    )
    text = render_agent_report(report)
    assert "No runnable tasks" in text and "1 `_example` rows skipped" in text


def test_cli_rejects_local_models_without_live():
    with pytest.raises(SystemExit):
        agent_eval.main(["--local-models", "qwen3:14b"])
    args = agent_eval.build_parser().parse_args(["--live", "--local-models", "a,b@low"])
    assert [(s.model, s.think) for s in args.local_models] == [("a", None), ("b", "low")]
