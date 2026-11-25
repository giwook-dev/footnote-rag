"""Eval runner: waiting for imports held by another worker, new metrics, report sections."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from footnote_rag.evals import run as eval_run
from footnote_rag.evals.dataset import EvalItem, load_dataset
from footnote_rag.evals.run import (
    DocumentImport,
    EvalSummary,
    ItemResult,
    RetrievedRef,
    item_category,
    percentile,
    render_report,
    run_eval,
)
from footnote_rag.jobs.handlers import HANDLERS, JobContext
from footnote_rag.jobs.worker import Worker

LEASE = "(첨부)재무제표 > 주석 > 12. 리스"
LITIGATION = "(첨부)재무제표 > 주석 > 20. 우발부채와 약정사항"
FIRST, SECOND = "20260310900001", "20260310900002"

ROWS = [
    {
        "id": "syn-1",
        "rcept_no": FIRST,
        "corp_name": "가람전자(합성)",
        "question": "리스부채는 어떻게 측정하나요?",
        "expected_section_prefixes": [LEASE],
        "answerable": True,
    },
    {
        "id": "syn-2",
        "rcept_no": SECOND,
        "corp_name": "나래포털(합성)",
        "question": "회사의 2030년 매출 목표는 얼마인가요?",
        "expected_section_prefixes": [],
        "answerable": False,
    },
]


def _dataset(tmp_path: Path) -> list[EvalItem]:
    path = tmp_path / "ds.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in ROWS), encoding="utf-8")
    return load_dataset(path)


def _service_worker(sessionmaker, settings, fake_embedder, fake_dart, gate):
    """A second worker (like the compose ``worker`` service) whose import waits on ``gate``."""
    claimed: list[str] = []
    started = asyncio.Event()

    async def slow_import(ctx, job):
        claimed.append(job.payload["rcept_no"])
        started.set()
        await gate()
        return await HANDLERS["import_dart"](ctx, job)

    worker = Worker(
        sessionmaker=sessionmaker,
        context=JobContext(sessionmaker, settings, fake_embedder, fake_dart),
        handlers={"import_dart": slow_import},
        worker_id="service-worker",
    )
    return worker, started, claimed


def _claim_first_import(monkeypatch, other: Worker, started: asyncio.Event) -> list[asyncio.Task]:
    """Right after the runner registers its first filing, the other worker claims that job."""
    tasks: list[asyncio.Task] = []
    original = eval_run.request_dart_import

    async def import_then_claim(session, *, user, rcept_no):
        result = await original(session, user=user, rcept_no=rcept_no)
        if not tasks:
            tasks.append(asyncio.create_task(other.run_once()))
            await started.wait()  # the job is now `running`, owned by the other worker
        return result

    monkeypatch.setattr(eval_run, "request_dart_import", import_then_claim)
    return tasks


async def test_eval_waits_for_import_claimed_by_another_worker(
    tmp_path, monkeypatch, sessionmaker, settings, fake_embedder, fake_llm, fake_dart
):
    async def finish_shortly():
        await asyncio.sleep(0.5)

    other, started, claimed = _service_worker(
        sessionmaker, settings, fake_embedder, fake_dart, finish_shortly
    )
    tasks = _claim_first_import(monkeypatch, other, started)

    summary = await run_eval(
        _dataset(tmp_path),
        settings=settings,
        embedder=fake_embedder,
        llm=fake_llm,
        dart_client=fake_dart,
        mode="fake",
        dataset_name="synthetic",
        import_timeout_seconds=30,
        import_poll_seconds=0.05,
    )
    assert claimed == [FIRST]  # the other worker really held the first import
    assert tasks[0].done() and tasks[0].result() is True
    assert summary.import_failures == []
    assert [(d.rcept_no, d.status) for d in summary.imports] == [
        (FIRST, "ready"),
        (SECOND, "ready"),
    ]
    assert all(d.section_count and d.chunk_count for d in summary.imports)
    assert all(d.embedding_tokens is not None and d.cache_hits is not None for d in summary.imports)
    # Scoring ran against the finished import: the first question retrieves its chunks.
    first = summary.results[0]
    assert first.item.id == "syn-1" and first.retrieved
    assert first.retrieval_hit is not None


async def test_eval_records_timeout_when_import_never_finishes(
    tmp_path, monkeypatch, sessionmaker, settings, fake_embedder, fake_llm, fake_dart
):
    release = asyncio.Event()
    other, started, _claimed = _service_worker(
        sessionmaker, settings, fake_embedder, fake_dart, release.wait
    )
    tasks = _claim_first_import(monkeypatch, other, started)
    try:
        summary = await run_eval(
            _dataset(tmp_path),
            settings=settings,
            embedder=fake_embedder,
            llm=fake_llm,
            dart_client=fake_dart,
            mode="fake",
            dataset_name="synthetic",
            import_timeout_seconds=0.3,
            import_poll_seconds=0.05,
        )
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert len(summary.import_failures) == 1
    assert summary.import_failures[0].startswith(f"{FIRST}: timeout(queued)")
    statuses = {d.rcept_no: d.status for d in summary.imports}
    assert statuses == {FIRST: "queued", SECOND: "ready"}
    assert len(summary.results) == 2  # questions are still scored
    assert f"{FIRST}: timeout(queued)" in render_report(summary)


# ---- metrics on fixed inputs ----


def _item(item_id: str, *, answerable: bool = True, corp: tuple[str, ...] = ("가람전자",)):
    return EvalItem(
        id=item_id,
        rcept_nos=["20260310900001"],
        corp_names=list(corp),
        question="q",
        expected_section_prefixes=[LEASE] if answerable else [],
        answerable=answerable,
        evidence_quotes=[],
    )


def _result(item, hit, ok, total, abstained, latency, *, retrieved=(), cited=()):
    return ItemResult(
        item=item,
        retrieval_hit=hit,
        footnotes_total=total,
        footnotes_matching=ok,
        abstained=abstained,
        status="pending_review" if abstained else "published",
        cost_usd=Decimal("0.01"),
        latency_ms=latency,
        answer_preview="SECRET-ANSWER-BODY",
        retrieved=list(retrieved),
        cited_paths=list(cited),
    )


def _fixed_summary() -> EvalSummary:
    other = "(첨부)재무제표 > 주석 > 3. 현금"
    results = [
        _result(
            _item("samsung-01"),
            True,
            1,
            2,
            False,
            100,
            retrieved=[RetrievedRef(LEASE + " > 1", 0.81, True), RetrievedRef(other, 0.52, False)],
            cited=[LEASE + " > 1", other],
        ),
        _result(_item("samsung-02"), True, 2, 2, False, 200, cited=[LEASE, LEASE]),
        _result(
            _item("naver-01", corp=("NAVER",)),
            False,
            0,
            1,
            False,
            300,
            retrieved=[RetrievedRef(other, 0.44, False)],
            cited=[other],
        ),
        _result(_item("compare-01", corp=("가람전자", "NAVER")), True, 0, 0, True, 400),
        # an unanswerable item that cited something: not a "wrong citation" (no expected section)
        _result(_item("unans-01", answerable=False), None, 0, 1, True, 1000, cited=[other]),
    ]
    summary = EvalSummary(mode="live", dataset="fixed", k=8, results=results)
    summary.imports = [
        DocumentImport(
            rcept_no="20260310900001",
            corp_name="가람전자(합성)",
            status="ready",
            section_count=110,
            chunk_count=512,
            embedding_tokens=4321,
            cache_hits=7,
            cache_misses=505,
        )
    ]
    return summary


def test_existing_metric_definitions_are_unchanged():
    s = _fixed_summary()
    assert s.recall_at_k == 3 / 4
    assert s.citation_accuracy == 3 / 5  # all footnotes of answerable items
    assert s.abstention_rate == 1.0
    assert s.avg_latency_ms == 400
    assert s.avg_cost_usd == Decimal("0.01")


def test_new_metrics_on_fixed_input():
    s = _fixed_summary()
    assert s.answered_rate == 3 / 4
    assert s.false_abstentions == ["compare-01"]
    # retrieval-hit items: samsung-01 (1/2), samsung-02 (2/2), compare-01 (0/0) -> 3/4
    assert s.citation_accuracy_retrieval_hit == 3 / 4
    assert s.latency_p50_ms == 300
    assert s.latency_p95_ms == 1000
    groups = s.by_category()
    assert list(groups) == ["samsung", "naver", "compare", "unans"]
    assert [len(v) for v in groups.values()] == [2, 1, 1, 1]


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([7], 95) == 7
    assert percentile(list(range(1, 21)), 50) == 10
    assert percentile(list(range(1, 21)), 95) == 19
    assert percentile([5, 1, 3], 50) == 3


def test_item_category_prefers_dataset_field_then_id_prefix():
    assert item_category(_item("hyundai-04")) == "hyundai"
    assert item_category(_item("unans-02", answerable=False)) == "unans"
    custom = _item("q1")
    custom.raw = {"category": "삼성전자"}
    assert item_category(custom) == "삼성전자"
    assert item_category(_item("free form", answerable=False)) == "unans"
    assert item_category(_item("free form", corp=("A", "B"))) == "compare"
    assert item_category(_item("free form", corp=("NAVER",))) == "NAVER"


def test_report_renders_new_sections_without_document_text():
    md = render_report(_fixed_summary())
    # import table in the header, before the metrics
    assert md.index("## Imports") < md.index("## Metrics")
    assert "| 20260310900001 | 가람전자(합성) | ready | 110 | 512 | 4321 | 7 | 505 |" in md
    # unchanged rows + new rows
    assert "| citation accuracy | 60.0% |" in md
    assert "| answerable answered rate | 75.0% (3/4) |" in md
    assert "| false abstentions (answerable, not answered) | 1 (compare-01) |" in md
    assert "| citation accuracy (retrieval-hit items only) | 75.0% |" in md
    assert "| latency p50 (ms) | 300 |" in md and "| latency p95 (ms) | 1000 |" in md
    # per-type table
    assert "## Metrics by question type" in md
    assert "| samsung | 2 | 100.0% | 75.0% | 75.0% | 100.0% (2/2) | 0 | n/a | 100 | 200 |" in md
    assert "| compare | 1 | 100.0% | n/a | n/a | 0.0% (0/1) | 1 | n/a | 400 | 400 |" in md
    assert "| unans | 1 | n/a | n/a | n/a | n/a | - | 100.0% | 1000 | 1000 |" in md
    # citation diagnostics: only items with a wrong footnote
    diag = md[md.index("## Citation diagnostics") :]
    assert "### samsung-01" in diag and "### naver-01" in diag
    assert "### samsung-02" not in diag and "### compare-01" not in diag
    assert "### unans-01" not in diag
    assert f"- expected section prefixes: `{LEASE}`" in diag
    assert "expected section retrieved (rank 1); the model cited other evidence" in diag
    assert "expected section not in top-k; retrieval miss" in diag
    assert f"| 1 | 0.810 | ✓ | {LEASE} > 1 |" in diag
    assert "| [2] |  | (첨부)재무제표 > 주석 > 3. 현금 |" in diag
    # paths and scores only: answer text never reaches the report
    assert "SECRET-ANSWER-BODY" not in md


def test_report_header_records_retrieval_settings():
    s = _fixed_summary()
    assert "- prefer_consolidated: -" in render_report(s)  # unknown when not set
    s.prefer_consolidated, s.retrieval_candidates = True, 16
    md = render_report(s)
    assert md.index("- prefer_consolidated: true") < md.index("## Metrics")
    assert "- retrieval_candidates: 16\n" in md
    s.prefer_consolidated = False
    assert "- retrieval_candidates: 16 (unused: prefer_consolidated=false)" in render_report(s)


def test_fixed_summary_counterpart_metric_equals_strict_without_counterparts():
    s = _fixed_summary()
    assert s.citation_accuracy_counterpart == s.citation_accuracy == 3 / 5
    md = render_report(s)
    assert "| citation accuracy (consolidated/separate counterpart accepted) | 60.0% (3/5) |" in md
    assert "| [1] | ✓ | (첨부)재무제표 > 주석 > 12. 리스 > 1 |  |" in md


# ---- consolidated/separate counterpart metric on the 2026-11-25 local run (fixed input) ----

CONS = "III. 재무에 관한 사항 > 3. 연결재무제표 주석 > "
SEP = "III. 재무에 관한 사항 > 5. 재무제표 주석 > "
SAM_POLICY = CONS + "2. 중요한 회계처리방침 (연결)"
HYU_POLICY = CONS + "2. 연결재무제표 작성기준 및 중요한 회계정책 (연결)"
NAV_POLICY = CONS + "2. 중요한 회계정책 (연결)"
OK_NOTE = CONS + "9. 관계기업 및 공동기업 투자 (연결)"


def _scored(
    item_id, prefixes, cited, *, hit=True, abstained=False, review_reason=None, retrieved=()
):
    item = EvalItem(
        id=item_id,
        rcept_nos=["20260310002820"],
        corp_names=["삼성전자"],
        question="q",
        expected_section_prefixes=list(prefixes),
        answerable=bool(prefixes),
        evidence_quotes=[],
    )
    matching, counterpart = eval_run.score_citations(cited, item.expected_section_prefixes)
    return ItemResult(
        item=item,
        retrieval_hit=hit if prefixes else None,
        footnotes_total=len(cited),
        footnotes_matching=matching,
        abstained=abstained,
        status="pending_review" if abstained else "published",
        cost_usd=Decimal("0"),
        latency_ms=1000,
        answer_preview="SECRET-ANSWER-BODY",
        retrieved=list(retrieved),
        cited_paths=list(cited),
        footnotes_counterpart=counterpart if prefixes else 0,
        review_reason=review_reason,
    )


def _ok(item_id, n=1):
    return _scored(item_id, [OK_NOTE], [OK_NOTE] * n)


def _live_local_summary() -> EvalSummary:
    """Footnote paths of 20261125T020155Z-live-local-qwen3-14b.md (correct ones simplified)."""
    abstained = {"abstained": True, "review_reason": "no_citations"}
    results = [
        _scored("samsung-01", [SAM_POLICY], [SEP + "2. 중요한 회계처리방침", SAM_POLICY]),
        _scored(
            "samsung-02",
            [CONS + "12. 차입금 (연결)"],
            [SEP + "12. 차입금", CONS + "12. 차입금 (연결)"],
            retrieved=[
                RetrievedRef(SEP + "12. 차입금", 0.427, False),
                RetrievedRef(CONS + "12. 차입금 (연결)", 0.368, True),
            ],
        ),
        _ok("samsung-03"),
        _scored(
            "samsung-04",
            [CONS + "16. 우발부채와 약정사항 (연결)"],
            [SEP + "16. 우발부채와 약정사항"],
        ),
        _ok("samsung-05"),
        _ok("samsung-06"),
        _scored("naver-01", [NAV_POLICY], [SEP + "2. 중요한 회계정책"]),
        _scored("naver-02", [NAV_POLICY], ["II. 사업의 내용 > 1. 사업의 개요"], hit=False),
        _ok("naver-03", 3),
        _scored(
            "naver-04",
            [CONS + "32. 우발채무 및 약정사항 (연결)"],
            [CONS + "6. 범주별 금융상품 (연결)"],
            retrieved=[
                RetrievedRef(CONS + "6. 범주별 금융상품 (연결)", 0.424, False),
                RetrievedRef(CONS + "32. 우발채무 및 약정사항 (연결)", 0.413, True),
            ],
        ),
        _ok("naver-05"),
        _ok("naver-06"),
        _ok("hyundai-01"),
        _ok("hyundai-02"),
        _scored("hyundai-03", [CONS + "35. 특수관계자 등과의 거래 (연결)"], [HYU_POLICY]),
        _scored(
            "hyundai-04",
            [HYU_POLICY],
            [SEP + "2. 재무제표 작성기준 및 중요한 회계정책", HYU_POLICY, HYU_POLICY],
        ),
        _ok("hyundai-05"),
        _ok("compare-01", 3),
        _scored(
            "compare-02",
            [SAM_POLICY, NAV_POLICY],
            [],
            retrieved=[
                RetrievedRef(SEP + "2. 중요한 회계처리방침", 0.471, False),
                RetrievedRef(SAM_POLICY, 0.455, True),
            ],
            **abstained,
        ),
        _scored(
            "compare-03",
            [SAM_POLICY, HYU_POLICY],
            [
                SEP + "2. 중요한 회계처리방침",
                SAM_POLICY,
                SEP + "2. 중요한 회계처리방침",
                SEP + "2. 재무제표 작성기준 및 중요한 회계정책",
                SEP + "2. 재무제표 작성기준 및 중요한 회계정책",
            ],
        ),
        _scored(
            "compare-04",
            [HYU_POLICY],
            [],
            abstained=True,
            review_reason="low_retrieval_score,no_citations",
            retrieved=[RetrievedRef(HYU_POLICY, 0.31, True)],
        ),
        *(
            _scored(f"unans-0{i}", [], [], abstained=True, review_reason="no_citations")
            for i in range(1, 5)
        ),
    ]
    return EvalSummary(
        mode="live",
        dataset="evals/dataset.jsonl",
        k=8,
        results=results,
        provider="local",
        model="ollama:qwen3:14b",
        prefer_consolidated=True,
        retrieval_candidates=16,
    )


def test_counterpart_metric_accepts_the_nine_separate_note_citations():
    s = _live_local_summary()
    # strict metrics: unchanged definitions, the reported values
    assert eval_run.citation_counts_counterpart(s.results)[1] == 31
    assert s.citation_accuracy == 19 / 31
    assert s.citation_accuracy_retrieval_hit == 19 / 30
    assert sum(r.footnotes_counterpart for r in s.results) == 9
    # counterpart accepted: 19 + 9 = 28 of 31
    assert eval_run.citation_counts_counterpart(s.results) == (28, 31)
    assert s.citation_accuracy_counterpart == 28 / 31
    assert s.citation_accuracy_counterpart_retrieval_hit == 28 / 30
    counterparts = {
        r.item.id: r.footnotes_counterpart for r in s.results if r.footnotes_counterpart
    }
    assert counterparts == {
        "samsung-01": 1,
        "samsung-02": 1,
        "samsung-04": 1,
        "naver-01": 1,
        "hyundai-04": 1,
        "compare-03": 4,
    }


def test_counterpart_metric_renders_overall_retrieval_hit_and_per_type():
    md = render_report(_live_local_summary())
    # strict rows keep their names and values
    assert "| citation accuracy | 61.3% |" in md
    assert "| citation accuracy (retrieval-hit items only) | 63.3% |" in md
    assert "| answerable answered rate | 90.5% (19/21) |" in md
    assert (
        "| citation accuracy (consolidated/separate counterpart accepted) | 90.3% (28/31) |" in md
    )
    assert (
        "| citation accuracy (retrieval-hit items only, counterpart accepted) | 93.3% (28/30) |"
        in md
    )
    header = next(line for line in md.splitlines() if line.startswith("| type |"))
    assert header.endswith(
        "| citation acc. (counterpart accepted) | citation acc. (retrieval hit, counterpart accepted) |"
    )
    rows = {line.split(" | ")[0][2:]: line for line in md.splitlines() if line.startswith("| ")}
    # strict per-type values as reported, then the counterpart-accepted columns
    assert rows["samsung"].startswith("| samsung | 6 | 100.0% | 62.5% | 62.5% |")
    assert rows["samsung"].endswith("| 100.0% | 100.0% |")
    assert rows["naver"].startswith("| naver | 6 | 83.3% | 62.5% | 71.4% |")
    assert rows["naver"].endswith("| 75.0% | 85.7% |")
    assert rows["hyundai"].startswith("| hyundai | 5 | 100.0% | 71.4% | 71.4% |")
    assert rows["hyundai"].endswith("| 85.7% | 85.7% |")
    assert rows["compare"].startswith("| compare | 4 | 100.0% | 50.0% | 50.0% |")
    assert rows["compare"].endswith("| 100.0% | 100.0% |")
    assert rows["unans"].endswith("| n/a | n/a |")


def test_citation_diagnostics_mark_counterparts():
    md = render_report(_live_local_summary())
    diag = md[md.index("## Citation diagnostics") :]
    assert "| footnote | expected | cited section_path | counterpart |" in diag
    assert f"| [1] |  | {SEP}12. 차입금 | ✓ |" in diag
    assert f"| [2] | ✓ | {CONS}12. 차입금 (연결) |  |" in diag
    assert f"| [1] |  | {CONS}6. 범주별 금융상품 (연결) |  |" in diag

    def section(item_id: str) -> str:
        start = diag.index(f"### {item_id}\n")
        end = diag.find("### ", start + 4)
        return diag[start : end if end > 0 else None]

    assert "separate-statement counterpart cited (1 of 1 wrong footnotes)" in section("samsung-02")
    assert "- cited ok/total: 1/2 (counterpart accepted: 2/2)" in section("samsung-02")
    assert "separate-statement counterpart cited (4 of 4 wrong footnotes)" in section("compare-03")
    # real errors keep the old wording
    assert "the model cited other evidence" in section("naver-04")
    assert "counterpart cited" not in section("naver-04")
    assert "expected section not in top-k; retrieval miss" in section("naver-02")
    assert "counterpart cited" not in section("hyundai-03")


def test_false_abstentions_get_review_reason_and_top_k():
    md = render_report(_live_local_summary())
    section = md[md.index("## False abstention diagnostics") : md.index("## Citation diagnostics")]
    assert "### compare-02" in section and "### compare-04" in section
    assert "### unans-01" not in section and "### samsung-01" not in section
    c2 = section[section.index("### compare-02") : section.index("### compare-04")]
    assert "- status: pending_review" in c2
    assert "- review_reason: `no_citations` (the model returned no usable citation" in c2
    assert "- footnotes: 0" in c2
    assert "- retrieval: expected section retrieved (rank 2)" in c2
    assert "| top-8 rank | score | expected | retrieved section_path |" in c2
    assert f"| 1 | 0.471 |  | {SEP}2. 중요한 회계처리방침 |" in c2
    assert f"| 2 | 0.455 | ✓ | {SAM_POLICY} |" in c2
    c4 = section[section.index("### compare-04") :]
    assert "`low_retrieval_score` (confidence rule: best candidate similarity below" in c4
    assert "`no_citations`" in c4
    assert "- retrieval: expected section retrieved (rank 1)" in c4
    assert f"| 1 | 0.310 | ✓ | {HYU_POLICY} |" in c4
    assert "SECRET-ANSWER-BODY" not in md


def test_false_abstention_without_retrieval_results():
    s = _fixed_summary()  # compare-01: abstained, nothing retrieved, no review_reason recorded
    md = render_report(s)
    section = md[md.index("## False abstention diagnostics") : md.index("## Citation diagnostics")]
    assert "### compare-01" in section
    assert "- review_reason: none" in section
    assert "- retrieval: expected section not in top-k" in section
    assert "| - | - | - | (no results) |" in section


def test_report_says_when_nothing_was_falsely_abstained():
    s = _fixed_summary()
    s.results = [r for r in s.results if r.answered or not r.item.answerable]
    assert "## False abstention diagnostics" in render_report(s)
    assert "No false abstentions." in render_report(s)


def test_report_says_when_no_citation_is_wrong():
    s = _fixed_summary()
    s.results = [r for r in s.results if not r.has_wrong_citation]
    md = render_report(s)
    assert "## Citation diagnostics" in md and "No wrong citations." in md


async def test_fake_run_report_has_import_table_and_diagnostics_section(
    tmp_path, settings, fake_embedder, fake_llm, fake_dart
):
    summary = await run_eval(
        _dataset(tmp_path),
        settings=settings,
        embedder=fake_embedder,
        llm=fake_llm,
        dart_client=fake_dart,
        mode="fake",
        dataset_name="synthetic",
    )
    md = render_report(summary)
    for needle in (
        "## Imports",
        "## Metrics by question type",
        "## Citation diagnostics",
        "answerable answered rate",
        "latency p95 (ms)",
        "## False abstention diagnostics",
        "citation accuracy (consolidated/separate counterpart accepted)",
        f"- prefer_consolidated: {str(settings.prefer_consolidated).lower()}",
        f"- retrieval_candidates: {settings.retrieval_candidates}",
    ):
        assert needle in md
    assert {d.rcept_no for d in summary.imports} == {FIRST, SECOND}
    assert summary.prefer_consolidated is settings.prefer_consolidated
    # review_reason is copied from the answer: set exactly when the answer went to review
    assert all(bool(r.review_reason) == (r.status == "pending_review") for r in summary.results)
