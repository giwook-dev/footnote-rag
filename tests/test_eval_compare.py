"""Local model comparison Eval: CLI specs, one import and one query embedding for every config,
per-config + comparison reports, cascade marks, per-question error isolation, preflight."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from footnote_rag.answering.llm import FakeLLM, LLMError, LLMResponse
from footnote_rag.answering.local_llm import OllamaClient
from footnote_rag.evals import run as eval_run
from footnote_rag.evals.dataset import EvalItem, load_dataset
from footnote_rag.evals.run import (
    EvalConfig,
    EvalSummary,
    ItemResult,
    ModelSpec,
    parse_args,
    render_compare_report,
    render_report,
    report_filename,
    run_eval,
    run_eval_configs,
    write_compare_reports,
)
from footnote_rag.retrieval.embedder import FakeEmbedder

LEASE = "(첨부)재무제표 > 주석 > 12. 리스"
FIRST, SECOND = "20260310900001", "20260310900002"
ROWS = [
    {
        "id": "garam-01",
        "rcept_no": FIRST,
        "corp_name": "가람전자(합성)",
        "question": "리스부채는 어떻게 측정하나요?",
        "expected_section_prefixes": [LEASE],
        "answerable": True,
    },
    {
        "id": "compare-01",
        "rcept_no": [FIRST, SECOND],
        "corp_name": ["가람전자(합성)", "나래포털(합성)"],
        "question": "두 회사의 소송 현황을 비교해 주세요.",
        "expected_section_prefixes": ["(첨부)재무제표 > 주석 > 20. 우발부채와 약정사항"],
        "answerable": True,
    },
    {
        "id": "unans-01",
        "rcept_no": SECOND,
        "corp_name": "나래포털(합성)",
        "question": "회사의 2030년 매출 목표는 얼마인가요?",
        "expected_section_prefixes": [],
        "answerable": False,
    },
]
STAMP_RE = r"\d{8}T\d{6}Z"


def _dataset(tmp_path: Path) -> Path:
    path = tmp_path / "ds.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in ROWS), encoding="utf-8")
    return path


class CountingEmbedder(FakeEmbedder):
    def __init__(self, dim: int) -> None:
        super().__init__(dim=dim)
        self.queries: list[str] = []

    async def embed_query(self, text: str):
        self.queries.append(text)
        return await super().embed_query(text)


def _question(messages: list[dict[str, Any]]) -> str:
    return next(b["text"] for b in reversed(messages[0]["content"]) if b.get("type") == "text")


class LocalFake:
    """FakeLLM answers under an ``ollama:`` model id; ``abstain`` returns no citation and
    ``fail_on`` raises ``LLMError`` for questions containing that text."""

    provider = "local"
    citation_method = "model_reference"

    def __init__(self, model: str, *, abstain: bool = False, fail_on: str | None = None) -> None:
        self.model = model
        self.model_id = f"ollama:{model}"
        self.abstain = abstain
        self.fail_on = fail_on
        self.questions: list[str] = []
        self.warmed = 0

    async def warm_up(self) -> bool:
        self.warmed += 1
        return True

    async def answer(self, *, system: str, messages: list[dict[str, Any]]) -> LLMResponse:
        question = _question(messages)
        self.questions.append(question)
        if self.fail_on and self.fail_on in question:
            raise LLMError(504, "로컬 LLM 서버(Ollama) 응답 시간이 초과되었습니다")
        response = await FakeLLM(model=self.model_id).answer(system=system, messages=messages)
        if self.abstain:
            response.content = [{"type": "text", "text": "제공된 문서에서 근거를 찾지 못했습니다."}]
        return response


@pytest.fixture
def import_calls(monkeypatch) -> list[str]:
    calls: list[str] = []
    original = eval_run.request_dart_import

    async def spy(session, *, user, rcept_no):
        calls.append(rcept_no)
        return await original(session, user=user, rcept_no=rcept_no)

    monkeypatch.setattr(eval_run, "request_dart_import", spy)
    return calls


@pytest.fixture
def keep_engine(monkeypatch):
    from footnote_rag.db import session as db_session

    async def no_dispose():  # keep the shared test engine alive
        return None

    monkeypatch.setattr(db_session, "dispose_engine", no_dispose)


# ---- CLI parsing ----


def test_local_models_and_cascade_specs():
    args = parse_args(
        [
            "--live",
            "--local-models",
            "qwen3.5:9b, gpt-oss:20b@low ,qwen3:14b@false",
            "--cascade",
            "qwen3.5:9b>qwen3:14b@HIGH",
            "--cascade",
            "a@true>b",
        ]
    )
    assert args.local_models == [
        ModelSpec("qwen3.5:9b", None),
        ModelSpec("gpt-oss:20b", "low"),
        ModelSpec("qwen3:14b", False),
    ]
    assert args.cascade == [
        (ModelSpec("qwen3.5:9b"), ModelSpec("qwen3:14b", "high")),
        (ModelSpec("a", True), ModelSpec("b")),
    ]
    plain = parse_args([])
    assert plain.local_models is None and plain.cascade is None and not plain.live


@pytest.mark.parametrize(
    "argv",
    [
        ["--live", "--local-models", "qwen3:14b@max"],
        ["--live", "--local-models", "@low"],
        ["--live", "--local-models", ","],
        ["--live", "--cascade", "qwen3:14b"],
        ["--live", "--cascade", "a>b>c"],
        ["--local-models", "qwen3:14b"],  # needs --live
        ["--cascade", "a>b"],
    ],
)
def test_invalid_specs_and_missing_live_are_rejected(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(argv)
    assert exc.value.code == 2


def test_build_local_configs_resolves_think_and_never_builds_claude(monkeypatch, settings):
    from footnote_rag.answering import llm as llm_module

    def no_claude(*a, **k):
        raise AssertionError("ClaudeClient must not be built")

    monkeypatch.setattr(llm_module.ClaudeClient, "__init__", no_claude)
    s = settings.model_copy(update={"local_llm_think": "medium", "llm_provider": "real"})
    configs = eval_run.build_local_configs(
        s,
        [ModelSpec("a:1b"), ModelSpec("b:2b", "low")],
        [(ModelSpec("a:1b", False), ModelSpec("c:3b"))],
    )
    assert all(isinstance(c.llm, OllamaClient) for c in configs)
    assert [(c.llm.model, c.llm.think) for c in configs] == [
        ("a:1b", "medium"),
        ("b:2b", "low"),
        ("a:1b", False),
    ]
    assert isinstance(configs[2].fallback_llm, OllamaClient)
    assert (configs[2].fallback_llm.model, configs[2].fallback_llm.think) == ("c:3b", "medium")
    assert configs[0].llm.base_url == s.local_llm_base_url
    assert configs[0].llm.num_ctx == s.local_llm_num_ctx


# ---- file names ----


def test_report_filenames_for_local_configs():
    single = EvalSummary(mode="live", dataset="d", k=8, provider="local", model="ollama:qwen3.5:9b")
    assert report_filename(single, "S") == "S-live-local-qwen3.5-9b.md"
    single.think = False
    assert report_filename(single, "S") == "S-live-local-qwen3.5-9b.md"
    single.think = "low"
    assert report_filename(single, "S") == "S-live-local-qwen3.5-9b-think-low.md"
    assert single.label == "qwen3.5:9b@low"
    cascade = EvalSummary(
        mode="live",
        dataset="d",
        k=8,
        provider="local",
        model="ollama:qwen3.5:9b",
        think=False,
        fallback_model="ollama:qwen3:14b",
        fallback_think=False,
    )
    assert report_filename(cascade, "S") == "S-live-local-qwen3.5-9b_to_qwen3-14b.md"
    assert cascade.label == "qwen3.5:9b > qwen3:14b"


# ---- multi-config run ----


async def test_configs_share_one_import_and_one_query_embedding_per_question(
    tmp_path, settings, fake_dart, import_calls
):
    items = load_dataset(_dataset(tmp_path))
    embedder = CountingEmbedder(settings.embedding_dim)
    small, big, other = LocalFake("small:1b", abstain=True), LocalFake("big:9b"), LocalFake("o:3b")
    configs = [
        EvalConfig(llm=other, think="low"),
        EvalConfig(llm=small, fallback_llm=big, think=False, fallback_think=False),
    ]
    summaries = await run_eval_configs(
        items,
        settings=settings,
        embedder=embedder,
        configs=configs,
        dart_client=fake_dart,
        mode="live",
        dataset_name="synthetic",
        warm_up=True,
    )
    assert sorted(import_calls) == [FIRST, SECOND]  # once, not once per config
    assert sorted(embedder.queries) == sorted(r["question"] for r in ROWS)  # once per question
    # same retrieval for every config, same question order
    a, b = summaries
    assert (
        [r.item.id for r in a.results] == [r.item.id for r in b.results] == [r["id"] for r in ROWS]
    )
    assert [r.retrieved for r in a.results] == [r.retrieved for r in b.results]
    # warm-up: single model and cascade primary only
    assert (other.warmed, small.warmed, big.warmed) == (1, 1, 0)
    # cascade: every abstaining primary answer escalated to the fallback
    assert all(r.escalated for r in b.results) and not any(r.escalated for r in a.results)
    assert b.escalation_rate == 1.0 and a.escalation_rate is None
    assert len(big.questions) == len(ROWS)

    paths, compare = write_compare_reports(summaries, tmp_path / "out", dataset="ds", k=8)
    names = [p.name for p in paths]
    assert re.fullmatch(STAMP_RE + r"-live-local-o-3b-think-low\.md", names[0])
    assert re.fullmatch(STAMP_RE + r"-live-local-small-1b_to_big-9b\.md", names[1])
    assert re.fullmatch(STAMP_RE + r"-live-compare-local\.md", compare.name)
    assert compare.name[:16] == names[0][:16] == names[1][:16]  # one timestamp

    md = compare.read_text(encoding="utf-8")
    assert "# Eval comparison (live, local models)" in md
    assert "- dataset: `ds` (3 rows), k: 8" in md
    assert "question embeddings: Voyage" in md
    assert "- configs: `o:3b@low`, `small:1b > big:9b`" in md
    assert f"  - [{names[0]}]({names[0]})" in md
    assert "## Configs" in md and "## Items" in md
    assert "## By question type (citation accuracy)" in md
    header = next(line for line in md.splitlines() if line.startswith("| config |"))
    for column in ("ungrounded number answers", "escalation rate", "errors", "cost USD"):
        assert column in header
    rows = {line.split(" | ")[0][2:]: line for line in md.splitlines() if line.startswith("| ")}
    assert "| n/a | 0 |" in rows["o:3b@low"]  # escalation n/a, no errors
    assert "| 100.0% (3/3) | 0 |" in rows["small:1b > big:9b"]
    assert rows["o:3b@low"].endswith("| 0.0000 |")
    assert re.search(r"\| garam-01 \| \d+/\d+ (pub|rev) \| \d+/\d+ (pub|rev) ↑ \|", md)
    assert "| type | o:3b@low | small:1b > big:9b |" in md
    assert rows["garam"].count("%") == 2 and rows["unans"].endswith("| n/a | n/a |")

    cascade_md = paths[1].read_text(encoding="utf-8")
    assert "- fallback model (cascade): `ollama:big:9b`" in cascade_md
    assert "| escalation rate (fallback called) | 100.0% (3/3) |" in cascade_md
    assert "| ungrounded number answers | 0 |" in cascade_md


async def test_comparison_metrics_reuse_the_per_config_functions():
    def result(item_id, ok, total, status, escalated=False, numbers=()):
        item = EvalItem(item_id, ["r"], ["c"], "q", [LEASE], True, [])
        return ItemResult(
            item=item,
            retrieval_hit=True,
            footnotes_total=total,
            footnotes_matching=ok,
            abstained=status != "published",
            status=status,
            cost_usd=None,
            latency_ms=10,
            answer_preview="",
            escalated=escalated,
            ungrounded_numbers=list(numbers),
        )

    s = EvalSummary(
        mode="live",
        dataset="d",
        k=8,
        provider="local",
        model="ollama:a",
        fallback_model="ollama:b",
        results=[
            result("x-01", 1, 2, "published", escalated=True, numbers=["1.2"]),
            result("x-02", 0, 0, "error"),
        ],
    )
    md = render_compare_report([s], ["r.md"], dataset="d", k=8)
    row = next(line for line in md.splitlines() if line.startswith("| a > b |"))
    assert row == (
        f"| a > b | {eval_run._pct(s.recall_at_k)} | 50.0% | 50.0% | 50.0% (1/2) | 1 | n/a "
        "| 1 | 50.0% (1/2) | 1 | 10 | 10 | 0.0000 |"
    )
    assert "| x-01 | 1/2 pub ↑ |" in md and "| x-02 | 0/0 err |" in md
    per_config = render_report(s)
    assert "| ungrounded number answers | 1 |" in per_config
    assert "| errors (LLMError, question skipped) | 1 |" in per_config
    assert "## Ungrounded number diagnostics" in per_config and "`1.2`" in per_config


async def test_llm_error_on_one_question_is_recorded_and_the_run_goes_on(
    tmp_path, settings, fake_embedder, fake_dart
):
    llm = LocalFake("flaky:1b", fail_on="2030")
    summary = await run_eval(
        load_dataset(_dataset(tmp_path)),
        settings=settings,
        embedder=fake_embedder,
        llm=llm,
        dart_client=fake_dart,
        mode="live",
        dataset_name="synthetic",
    )
    assert len(summary.results) == 3
    failed = summary.results[2]
    assert failed.item.id == "unans-01" and failed.status == "error"
    assert failed.abstained is True and failed.footnotes_total == 0
    assert failed.error == "로컬 LLM 서버(Ollama) 응답 시간이 초과되었습니다"
    assert summary.results[0].status != "error"
    md = render_report(summary)
    assert "| unans-01 | False | error |" in md
    assert "## Errors" in md and "- unans-01: 로컬 LLM 서버(Ollama) 응답 시간이" in md


async def test_single_config_run_embeds_each_question_once(tmp_path, settings, fake_dart, fake_llm):
    embedder = CountingEmbedder(settings.embedding_dim)
    await run_eval(
        load_dataset(_dataset(tmp_path)),
        settings=settings,
        embedder=embedder,
        llm=fake_llm,
        dart_client=fake_dart,
        mode="fake",
        dataset_name="synthetic",
    )
    assert len(embedder.queries) == len(ROWS)


# ---- the CLI path end to end (OllamaClient with its HTTP calls replaced) ----


def _patch_ollama(monkeypatch, available: set[str] | None, created: list[str]):
    from footnote_rag.answering import llm as llm_module

    def no_claude(*a, **k):
        raise AssertionError("ClaudeClient must not be built")

    monkeypatch.setattr(llm_module.ClaudeClient, "__init__", no_claude)

    async def status(self):
        created.append(self.model)
        ok = available is None or self.model in available
        return {
            "provider": "local",
            "model": self.model,
            "base_url": self.base_url,
            "connected": available is not None,
            "model_available": ok if available is not None else None,
            "detail": None,
        }

    warmed: list[str] = []

    async def warm_up(self):
        warmed.append(self.model)
        return True

    async def answer(self, *, system, messages):
        return await FakeLLM(model=self.model_id).answer(system=system, messages=messages)

    monkeypatch.setattr(OllamaClient, "status", status)
    monkeypatch.setattr(OllamaClient, "warm_up", warm_up)
    monkeypatch.setattr(OllamaClient, "answer", answer)
    return warmed


async def test_cli_local_models_runs_every_config_with_ollama_clients(
    tmp_path, monkeypatch, capsys, import_calls, keep_engine
):
    created: list[str] = []
    warmed = _patch_ollama(monkeypatch, {"a:1b", "b:2b"}, created)
    out = tmp_path / "reports"
    args = parse_args(
        [
            "--live",
            "--dataset",
            str(_dataset(tmp_path)),
            "--out",
            str(out),
            "--local-models",
            "a:1b,b:2b@low",
        ]
    )
    assert await eval_run._amain(args) == 0
    assert created == ["a:1b", "b:2b"]  # preflight on each model
    assert warmed == ["a:1b", "b:2b"]
    assert sorted(import_calls) == [FIRST, SECOND]
    names = sorted(p.name for p in out.iterdir())
    assert len(names) == 3
    assert any(re.fullmatch(STAMP_RE + r"-live-local-a-1b\.md", n) for n in names)
    assert any(re.fullmatch(STAMP_RE + r"-live-local-b-2b-think-low\.md", n) for n in names)
    assert any(re.fullmatch(STAMP_RE + r"-live-compare-local\.md", n) for n in names)
    printed = capsys.readouterr().out
    assert "comparison written:" in printed


@pytest.mark.parametrize("available", [None, {"a:1b"}], ids=["unreachable", "missing-model"])
async def test_preflight_failure_exits_2_before_importing(
    tmp_path, monkeypatch, capsys, import_calls, keep_engine, available
):
    created: list[str] = []
    _patch_ollama(monkeypatch, available, created)
    out = tmp_path / "reports"
    args = parse_args(
        [
            "--live",
            "--dataset",
            str(_dataset(tmp_path)),
            "--out",
            str(out),
            "--local-models",
            "a:1b",
            "--cascade",
            "a:1b>gone:70b",
        ]
    )
    assert await eval_run._amain(args) == 2
    assert import_calls == [] and not out.exists()
    err = capsys.readouterr().err
    assert "ollama pull gone:70b" in err
    if available is None:
        assert "ollama pull a:1b" in err and "Ollama 연결 실패" in err
    else:
        assert "ollama pull a:1b" not in err and "모델 없음" in err
