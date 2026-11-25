"""Eval 실행기(recall@k, 인용 정확도와 연결↔별도 대응 인정 정확도, 거절 비율, 과도한 거절, 비용·지연시간, 진단).

역할: 임시 조직을 만들어 데이터셋의 공시를 정상 서비스·워커로 가져오고, 등록한 문서가 모두 ``ready``/``failed``가
될 때까지 기다린 뒤(``wait_for_imports``) 질문마다 검색·답변 결과를 채점해 Markdown 리포트를 쓴다.
기본은 Fake 모드(비용 없음), ``--live``는 실제 제공자. ``--live --local-models``/``--cascade``는 같은 데이터셋을 여러 로컬
(Ollama) 구성에 돌려 구성별 리포트와 비교 리포트(``{stamp}-live-compare-local.md``)를 쓴다(가져오기·질문 임베딩은 한 번).
주요 요소: ``run_eval``, ``run_eval_configs``, ``EvalConfig``, ``MemoizedQueryEmbedder``, ``wait_for_imports``,
``EvalSummary``(지표 속성), ``ItemResult``, ``DocumentImport``, ``score_citations``, ``item_category``, ``percentile``,
``render_report``, ``render_compare_report``, ``write_report``, ``write_compare_reports``, ``parse_model_spec``,
``preflight_local_models``, ``main``.
호출 관계: ``documents.service``, ``jobs.worker.Worker``, ``answering.service``를 그대로 호출한다.
학습 문서: docs/study/10-eval-and-mcp.md, docs/adr/0009-local-cascade-and-number-grounding.md

Eval runner.

    python -m footnote_rag.evals.run [--dataset evals/dataset.jsonl] [--live] [--out evals/reports]
                                     [--import-timeout 1800]
                                     [--local-models SPEC[,SPEC...]] [--cascade PRIMARY>FALLBACK ...]

Local model comparison (``--live`` only; ``LLM_PROVIDER`` is ignored, only ``OllamaClient`` is
built, with the ``LOCAL_LLM_*`` base_url / num_ctx / max_tokens / timeout / temperature). A SPEC is
``model[@think]`` (think: true, false, low, medium, high; default ``LOCAL_LLM_THINK``)::

    --local-models qwen3.5:9b,gpt-oss:20b@low,qwen3:14b --cascade "qwen3.5:9b>qwen3:14b"

Every model is checked first (``OllamaClient.status``); an unreachable server or a missing model
prints ``ollama pull`` hints and exits with 2 before anything is imported. The filings are imported
once, each question is embedded once (``MemoizedQueryEmbedder``) and every config answers the same
questions in the same order. Single-model configs are warmed up (``OllamaClient.warm_up``) before
timing; a cascade warms only its primary (swapping models on escalation is a real cost).

An ``LLMError`` on one question is recorded as status ``error`` (abstained, no footnotes) and the
run goes on (single-config runs too).

Default is fake mode (FakeEmbedder / FakeLLM / FakeDartClient): no network, no cost; it
exercises the pipeline mechanics. ``--live`` uses the configured providers, including the LLM
selected by ``LLM_PROVIDER`` (``real`` = Claude, ``local`` = Ollama). The report header and file
name carry the provider, model and citation_method so Claude and local runs can be compared.

The runner creates a throw-away organization, imports every filing referenced by the
dataset through the normal service + worker, then asks each question through the normal
answering service and measures:

* retrieval recall@k   - an expected-section chunk is among the top-k results
* citation accuracy    - share of footnotes whose section_path matches an expected prefix
  (also computed over retrieval-hit items only, to separate retrieval from model errors)
* citation accuracy (consolidated/separate counterpart accepted) - the same, but a footnote on
  the counterpart note of an expected section (``dart.sections.is_counterpart``: the separate
  ``5. 재무제표 주석 > 12. 차입금`` for ``3. 연결재무제표 주석 > 12. 차입금 (연결)``) also counts.
  A supplementary metric; the strict one above is unchanged (docs/adr/0009)
* abstention           - unanswerable questions were not answered (no footnotes, "근거 없음",
                         or routed to review)
* answered rate        - answerable questions answered with >= 1 footnote; the rest are
                         false abstentions (listed by id)
* average cost, average / p50 / p95 latency per question

Diagnostics list, per wrong citation, the retrieved top-k and the cited paths (with a
``counterpart`` column), and, per false abstention, the retrieved top-k and the answer's
``review_reason``. The header records ``PREFER_CONSOLIDATED`` / ``RETRIEVAL_CANDIDATES``.

Another worker on the same database (e.g. the compose ``worker`` service) may claim the import
jobs first, so after draining the queue itself the runner polls the documents until every one
it registered is ``ready`` or ``failed`` (``--import-timeout``); a document still busy then is
reported as ``timeout(<status>)``.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from footnote_rag.answering.llm import CITATION_API, NO_EVIDENCE_TEXT, LLMClient, LLMError
from footnote_rag.answering.prompt import source_id
from footnote_rag.answering.service import AskResult, ask_question, retrieve
from footnote_rag.config import (
    LOCAL_MODEL_PREFIX,
    THINK_LEVELS,
    Settings,
    ThinkSetting,
    get_settings,
)
from footnote_rag.dart.client import DartClient
from footnote_rag.dart.sections import SEPARATE, is_counterpart, statement_scope
from footnote_rag.db.models import Document, User
from footnote_rag.documents.service import latest_jobs_for_documents, request_dart_import
from footnote_rag.evals.dataset import DEFAULT_DATASET, EvalItem, load_dataset
from footnote_rag.jobs.handlers import JobContext
from footnote_rag.jobs.worker import Worker
from footnote_rag.observability import get_logger
from footnote_rag.orgs.service import seed_org
from footnote_rag.retrieval.embedder import BatchCallback, Embedder, EmbeddingResult
from footnote_rag.retrieval.search import RetrievedChunk

log = get_logger(__name__)

DEFAULT_IMPORT_TIMEOUT_SECONDS = 1800.0
PREFLIGHT_EXIT_CODE = 2
IMPORT_POLL_SECONDS = 2.0
TERMINAL_DOCUMENT_STATUSES = frozenset({"ready", "failed"})


@dataclass
class RetrievedRef:
    """One top-k search hit, reduced to what the diagnostics show (no chunk text)."""

    section_path: str
    score: float
    expected: bool


@dataclass
class ItemResult:
    item: EvalItem
    retrieval_hit: bool | None
    footnotes_total: int
    footnotes_matching: int
    abstained: bool
    status: str
    cost_usd: Decimal | None
    latency_ms: int
    answer_preview: str
    retrieved: list[RetrievedRef] = field(default_factory=list)
    cited_paths: list[str] = field(default_factory=list)
    # Footnotes outside the expected prefixes that cite an expected section's counterpart note.
    footnotes_counterpart: int = 0
    review_reason: str | None = None
    # Cascade: the fallback model was called for this question.
    escalated: bool = False
    # Numbers of the answer missing from their cited chunks (answering.number_check), as written.
    ungrounded_numbers: list[str] = field(default_factory=list)
    # Per footnote (aligned with ``cited_paths``): ungrounded numbers of sentences citing it.
    footnote_ungrounded: list[list[str]] = field(default_factory=list)
    # ``LLMError.public_message`` when ``status == "error"``.
    error: str | None = None

    @property
    def footnotes_accepted(self) -> int:
        """Footnotes on an expected section or on its consolidated/separate counterpart."""
        return self.footnotes_matching + self.footnotes_counterpart

    @property
    def answered(self) -> bool:
        """Not abstained and cites at least one footnote."""
        return not self.abstained and self.footnotes_total > 0

    @property
    def has_wrong_citation(self) -> bool:
        """An answerable item with a footnote outside the expected sections (unanswerable items
        have no expected section; their citations are not scored, as in citation accuracy)."""
        return self.item.answerable and self.footnotes_matching < self.footnotes_total


@dataclass
class DocumentImport:
    """Per-filing import outcome; counts come from the import job's ``jobs.result``."""

    rcept_no: str
    corp_name: str | None
    status: str
    section_count: int | None = None
    chunk_count: int | None = None
    embedding_tokens: int | None = None
    cache_hits: int | None = None
    cache_misses: int | None = None
    error: str | None = None


def item_category(item: EvalItem) -> str:
    """Question type for the per-type table.

    The dataset's ``category`` field if present, else the id prefix (``samsung-01`` ->
    ``samsung``, ``compare-03`` -> ``compare``, ``unans-02`` -> ``unans``), else derived from
    the row (``unans`` / ``compare`` / first company name).
    """
    explicit = item.raw.get("category") if isinstance(item.raw, dict) else None
    if explicit:
        return str(explicit)
    m = re.fullmatch(r"([A-Za-z]+)-\d+", item.id)
    if m:
        return m.group(1)
    if not item.answerable:
        return "unans"
    if item.is_comparison:
        return "compare"
    return item.corp_names[0] if item.corp_names else "other"


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile (``pct`` in 0-100): the smallest value with at least pct% of
    the values <= it. None for an empty input."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


# ---- metrics over any list of results (whole run or one question type) ----


def _answerable(rows: Sequence[ItemResult]) -> list[ItemResult]:
    return [r for r in rows if r.item.answerable]


def recall_at_k(rows: Sequence[ItemResult]) -> float | None:
    scored = [r for r in _answerable(rows) if r.retrieval_hit is not None]
    return sum(r.retrieval_hit for r in scored) / len(scored) if scored else None


def citation_accuracy(rows: Sequence[ItemResult]) -> float | None:
    answerable = _answerable(rows)
    total = sum(r.footnotes_total for r in answerable)
    return sum(r.footnotes_matching for r in answerable) / total if total else None


def citation_accuracy_retrieval_hit(rows: Sequence[ItemResult]) -> float | None:
    """Citation accuracy over answerable items whose expected section was retrieved."""
    return citation_accuracy([r for r in rows if r.retrieval_hit is True])


def citation_counts_counterpart(rows: Sequence[ItemResult]) -> tuple[int, int]:
    """(accepted, total) footnotes of answerable items, counterpart notes accepted."""
    answerable = _answerable(rows)
    return sum(r.footnotes_accepted for r in answerable), sum(r.footnotes_total for r in answerable)


def citation_accuracy_counterpart(rows: Sequence[ItemResult]) -> float | None:
    """Citation accuracy where a consolidated/separate counterpart of an expected section also
    counts as correct. Supplementary: ``citation_accuracy`` stays the strict metric."""
    accepted, total = citation_counts_counterpart(rows)
    return accepted / total if total else None


def citation_accuracy_counterpart_retrieval_hit(rows: Sequence[ItemResult]) -> float | None:
    return citation_accuracy_counterpart([r for r in rows if r.retrieval_hit is True])


def abstention_rate(rows: Sequence[ItemResult]) -> float | None:
    unanswerable = [r for r in rows if not r.item.answerable]
    return sum(r.abstained for r in unanswerable) / len(unanswerable) if unanswerable else None


def answered_rate(rows: Sequence[ItemResult]) -> float | None:
    answerable = _answerable(rows)
    return sum(r.answered for r in answerable) / len(answerable) if answerable else None


def false_abstentions(rows: Sequence[ItemResult]) -> list[str]:
    """Ids of answerable questions that were not answered (abstained or no footnote)."""
    return [r.item.id for r in _answerable(rows) if not r.answered]


def latency_percentile(rows: Sequence[ItemResult], pct: float) -> float | None:
    return percentile([r.latency_ms for r in rows], pct)


def ungrounded_answer_count(rows: Sequence[ItemResult]) -> int:
    """Answers with at least one number missing from their cited chunks."""
    return sum(1 for r in rows if r.ungrounded_numbers)


def escalated_count(rows: Sequence[ItemResult]) -> int:
    return sum(1 for r in rows if r.escalated)


def escalation_rate(rows: Sequence[ItemResult]) -> float | None:
    """Escalated questions / all questions (cascade configs)."""
    return escalated_count(rows) / len(rows) if rows else None


def error_count(rows: Sequence[ItemResult]) -> int:
    return sum(1 for r in rows if r.status == "error")


def _think_text(think: ThinkSetting) -> str:
    return str(think).lower() if isinstance(think, bool) else str(think)


def model_label(model: str, think: ThinkSetting | None = None) -> str:
    """``qwen3.5:9b`` / ``gpt-oss:20b@low`` (``ollama:`` prefix dropped, think shown unless off)."""
    label = model.removeprefix(LOCAL_MODEL_PREFIX)
    if think is not None and think is not False:
        label += f"@{_think_text(think)}"
    return label


@dataclass
class EvalSummary:
    mode: str
    dataset: str
    k: int
    results: list[ItemResult] = field(default_factory=list)
    skipped_examples: int = 0
    import_failures: list[str] = field(default_factory=list)
    imports: list[DocumentImport] = field(default_factory=list)
    # Which LLM produced the answers, so Claude and local (Ollama) reports can be compared.
    provider: str = "unknown"
    model: str = "unknown"
    citation_method: str = CITATION_API
    # Retrieval settings of the run (docs/adr/0009), so reports of different settings compare.
    prefer_consolidated: bool | None = None
    retrieval_candidates: int | None = None
    # Local comparison runs only: Ollama ``think`` of the model(s) and the cascade fallback.
    # None = not recorded (the default single-provider run keeps its header and file name).
    think: ThinkSetting | None = None
    fallback_model: str | None = None
    fallback_think: ThinkSetting | None = None

    @property
    def is_cascade(self) -> bool:
        return self.fallback_model is not None

    @property
    def label(self) -> str:
        """Config name in the comparison report: ``qwen3:14b``, ``gpt-oss:20b@low``,
        ``qwen3.5:9b > qwen3:14b``."""
        primary = model_label(self.model, self.think)
        if self.fallback_model is None:
            return primary
        return f"{primary} > {model_label(self.fallback_model, self.fallback_think)}"

    @property
    def ungrounded_answers(self) -> int:
        return ungrounded_answer_count(self.results)

    @property
    def escalation_rate(self) -> float | None:
        """None unless this is a cascade config."""
        return escalation_rate(self.results) if self.is_cascade else None

    @property
    def error_count(self) -> int:
        return error_count(self.results)

    @property
    def recall_at_k(self) -> float | None:
        return recall_at_k(self.results)

    @property
    def citation_accuracy_counterpart(self) -> float | None:
        return citation_accuracy_counterpart(self.results)

    @property
    def citation_accuracy_counterpart_retrieval_hit(self) -> float | None:
        return citation_accuracy_counterpart_retrieval_hit(self.results)

    @property
    def citation_accuracy(self) -> float | None:
        return citation_accuracy(self.results)

    @property
    def citation_accuracy_retrieval_hit(self) -> float | None:
        return citation_accuracy_retrieval_hit(self.results)

    @property
    def abstention_rate(self) -> float | None:
        return abstention_rate(self.results)

    @property
    def answered_rate(self) -> float | None:
        return answered_rate(self.results)

    @property
    def false_abstentions(self) -> list[str]:
        return false_abstentions(self.results)

    @property
    def avg_cost_usd(self) -> Decimal | None:
        costs = [r.cost_usd for r in self.results if r.cost_usd is not None]
        return (sum(costs, Decimal(0)) / len(costs)) if costs else None

    @property
    def total_cost_usd(self) -> Decimal:
        return sum((r.cost_usd for r in self.results if r.cost_usd is not None), Decimal(0))

    @property
    def avg_latency_ms(self) -> float | None:
        return sum(r.latency_ms for r in self.results) / len(self.results) if self.results else None

    @property
    def latency_p50_ms(self) -> float | None:
        return latency_percentile(self.results, 50)

    @property
    def latency_p95_ms(self) -> float | None:
        return latency_percentile(self.results, 95)

    def by_category(self) -> dict[str, list[ItemResult]]:
        groups: dict[str, list[ItemResult]] = {}
        for r in self.results:
            groups.setdefault(item_category(r.item), []).append(r)
        return groups


def _matches(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(p) for p in prefixes)


def is_expected_counterpart(path: str, prefixes: list[str]) -> bool:
    """Not an expected prefix, but the consolidated/separate counterpart note of one.

    Like the strict match, this compares paths only (expected prefixes carry no document id).
    """
    return not _matches(path, prefixes) and any(is_counterpart(path, p) for p in prefixes)


def score_citations(cited_paths: Sequence[str], prefixes: list[str]) -> tuple[int, int]:
    """(strict matches, counterpart matches) over the cited section paths."""
    matching = sum(1 for p in cited_paths if _matches(p, prefixes))
    counterpart = sum(1 for p in cited_paths if is_expected_counterpart(p, prefixes))
    return matching, counterpart


def describe_llm(llm: LLMClient) -> tuple[str, str, str]:
    """(provider, model id as stored in llm_calls, citation_method) of an LLM client."""
    provider = str(getattr(llm, "provider", "unknown"))
    model = str(getattr(llm, "model_id", None) or getattr(llm, "model", "unknown"))
    method = str(getattr(llm, "citation_method", CITATION_API))
    return provider, model, method


def _model_slug(model: str, think: ThinkSetting | None) -> str:
    slug = re.sub(r"[^A-Za-z0-9._]+", "-", model.removeprefix(LOCAL_MODEL_PREFIX)).strip("-")
    slug = slug or "unknown"
    if think is not None and think is not False:
        slug += f"-think-{_think_text(think)}"
    return slug


def report_filename(summary: EvalSummary, stamp: str) -> str:
    """``{stamp}-{mode}-{provider}-{model}.md`` (provider omitted when it equals the mode).

    Local comparison configs: ``-think-low`` after a model whose think is set (not off), and
    ``{primary}_to_{fallback}`` for a cascade (``qwen3.5-9b_to_qwen3-14b``)."""
    slug = _model_slug(summary.model, summary.think)
    if summary.fallback_model is not None:
        slug += "_to_" + _model_slug(summary.fallback_model, summary.fallback_think)
    parts = [stamp, summary.mode]
    if summary.provider != summary.mode:
        parts.append(summary.provider)
    parts.append(slug)
    return "-".join(parts) + ".md"


def compare_report_filename(stamp: str) -> str:
    return f"{stamp}-live-compare-local.md"


# ---- rendering ----


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}"


def _opt(value: object) -> str:
    return "-" if value is None else str(value)


def _cell(text: str) -> str:
    """Make a value safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _ratio(rate: float | None, rows: Sequence[ItemResult]) -> str:
    answerable = _answerable(rows)
    if rate is None:
        return "n/a"
    return f"{_pct(rate)} ({sum(r.answered for r in answerable)}/{len(answerable)})"


def _render_imports(summary: EvalSummary) -> list[str]:
    if not summary.imports:
        return []
    lines = [
        "## Imports",
        "",
        "| rcept_no | company | status | sections | chunks | embedding tokens "
        "| cache hits | cache misses |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for d in summary.imports:
        lines.append(
            f"| {d.rcept_no} | {_cell(d.corp_name or '-')} | {d.status} | {_opt(d.section_count)} "
            f"| {_opt(d.chunk_count)} | {_opt(d.embedding_tokens)} | {_opt(d.cache_hits)} "
            f"| {_opt(d.cache_misses)} |"
        )
    return [*lines, ""]


def _render_categories(summary: EvalSummary) -> list[str]:
    groups = summary.by_category()
    if not groups:
        return []
    lines = [
        "## Metrics by question type",
        "",
        f"| type | items | recall@{summary.k} | citation accuracy | citation acc. (retrieval hit) "
        "| answered (answerable) | false abstentions | unanswerable abstained "
        "| latency p50 ms | latency p95 ms "
        "| citation acc. (counterpart accepted) | citation acc. (retrieval hit, counterpart accepted) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, rows in groups.items():
        false_ids = false_abstentions(rows)
        lines.append(
            f"| {_cell(name)} | {len(rows)} | {_pct(recall_at_k(rows))} "
            f"| {_pct(citation_accuracy(rows))} | {_pct(citation_accuracy_retrieval_hit(rows))} "
            f"| {_ratio(answered_rate(rows), rows)} "
            f"| {len(false_ids) if _answerable(rows) else '-'} "
            f"| {_pct(abstention_rate(rows))} "
            f"| {_ms(latency_percentile(rows, 50))} | {_ms(latency_percentile(rows, 95))} "
            f"| {_pct(citation_accuracy_counterpart(rows))} "
            f"| {_pct(citation_accuracy_counterpart_retrieval_hit(rows))} |"
        )
    return [*lines, ""]


def _counterpart_ratio(rows: Sequence[ItemResult]) -> str:
    accepted, total = citation_counts_counterpart(rows)
    if not total:
        return "n/a"
    return f"{_pct(accepted / total)} ({accepted}/{total})"


def _retrieval_diagnosis(r: ItemResult) -> tuple[str, bool]:
    """("expected section retrieved (rank …)" | "expected section not in top-k", retrieved?)."""
    expected_ranks = [i for i, ref in enumerate(r.retrieved, start=1) if ref.expected]
    if expected_ranks:
        ranks = ", ".join(str(i) for i in expected_ranks)
        return f"expected section retrieved (rank {ranks})", True
    return "expected section not in top-k", False


def _diagnosis(r: ItemResult) -> str:
    retrieval, hit = _retrieval_diagnosis(r)
    prefixes = r.item.expected_section_prefixes
    wrong = [p for p in r.cited_paths if not _matches(p, prefixes)]
    counterparts = [p for p in wrong if is_expected_counterpart(p, prefixes)]
    if not counterparts:
        return (
            f"{retrieval}; the model cited other evidence"
            if hit
            else f"{retrieval}; retrieval miss"
        )
    side = (
        "separate-statement"
        if all(statement_scope(p) == SEPARATE for p in counterparts)
        else "consolidated/separate"
    )
    text = (
        f"{retrieval}; {side} counterpart cited "
        f"({len(counterparts)} of {len(wrong)} wrong footnotes)"
    )
    if len(counterparts) < len(wrong):
        text += "; the model also cited other evidence"
    return text


def _render_retrieved_table(summary: EvalSummary, r: ItemResult) -> list[str]:
    lines = [
        f"| top-{summary.k} rank | score | expected | retrieved section_path |",
        "| --- | --- | --- | --- |",
    ]
    if r.retrieved:
        lines += [
            f"| {i} | {ref.score:.3f} | {'✓' if ref.expected else ''} | {_cell(ref.section_path)} |"
            for i, ref in enumerate(r.retrieved, start=1)
        ]
    else:
        lines.append("| - | - | - | (no results) |")
    return lines


def _expected_prefixes(r: ItemResult) -> str:
    return ", ".join(f"`{p}`" for p in r.item.expected_section_prefixes) or "-"


def _render_diagnostics(summary: EvalSummary) -> list[str]:
    wrong = [r for r in summary.results if r.has_wrong_citation]
    lines = [
        "## Citation diagnostics",
        "",
        "Answerable items with at least one footnote outside the expected sections. Paths and similarity "
        "scores only (no document text). `✓` = path starts with an expected prefix. `counterpart` ✓ = "
        "not an expected prefix, but the consolidated/separate counterpart note of one (same note in "
        "`연결재무제표 주석` and `재무제표 주석`); the counterpart-accepted metric counts it as correct. "
        "`ungrounded numbers` = numbers of the sentences citing the footnote that its chunk does not "
        "contain (no unit conversion).",
        "",
    ]
    if not wrong:
        return [*lines, "No wrong citations.", ""]
    for r in wrong:
        prefixes_list = r.item.expected_section_prefixes
        lines += [
            f"### {r.item.id}",
            "",
            f"- expected section prefixes: {_expected_prefixes(r)}",
            f"- cited ok/total: {r.footnotes_matching}/{r.footnotes_total}"
            f" (counterpart accepted: {r.footnotes_accepted}/{r.footnotes_total})",
            f"- diagnosis: {_diagnosis(r)}",
            "",
            *_render_retrieved_table(summary, r),
            "",
            "| footnote | expected | cited section_path | counterpart | ungrounded numbers |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| [{i}] | {'✓' if _matches(path, prefixes_list) else ''} | {_cell(path)} "
            f"| {'✓' if is_expected_counterpart(path, prefixes_list) else ''} "
            f"| {_numbers(_footnote_numbers(r, i - 1))} |"
            for i, path in enumerate(r.cited_paths, start=1)
        ]
        lines.append("")
    return lines


def _numbers(values: Sequence[str]) -> str:
    return ", ".join(f"`{_cell(v)}`" for v in values)


def _footnote_numbers(r: ItemResult, index: int) -> list[str]:
    return r.footnote_ungrounded[index] if index < len(r.footnote_ungrounded) else []


def _render_ungrounded(summary: EvalSummary) -> list[str]:
    rows = [r for r in summary.results if r.ungrounded_numbers]
    lines = [
        "## Ungrounded number diagnostics",
        "",
        "Answers with a number (amount, ratio) that none of the sentence's cited chunks contains "
        "(`answering.number_check`: commas ignored, 1-2 digit integers and numbers from the question "
        "skipped, no unit conversion). Numbers only (no document text).",
        "",
    ]
    if not rows:
        return [*lines, "No ungrounded numbers.", ""]
    lines += ["| id | status | ungrounded numbers |", "| --- | --- | --- |"]
    lines += [f"| {r.item.id} | {r.status} | {_numbers(r.ungrounded_numbers)} |" for r in rows]
    return [*lines, ""]


def _render_errors(summary: EvalSummary) -> list[str]:
    errors = [r for r in summary.results if r.status == "error"]
    if not errors:
        return []
    lines = ["## Errors", "", "Questions whose LLM call failed (`LLMError`); the run went on.", ""]
    lines += [f"- {r.item.id}: {_cell(r.error or 'error')}" for r in errors]
    return [*lines, ""]


# review_reason codes (answering.service.review_reasons) -> what they mean for an abstention.
REVIEW_REASON_HINTS = {
    "no_citations": "the model returned no usable citation (local: insufficient or no valid "
    "source number)",
    "low_retrieval_score": "confidence rule: best candidate similarity below MIN_RETRIEVAL_SCORE",
    "no_retrieval_results": "retrieval returned nothing; the model was not called",
    "refusal": "the model refused",
    "truncated": "the output hit the token limit",
    "ungrounded_number": "a cited sentence has a number its cited chunks do not contain",
    "review_mode_always": "REVIEW_MODE=always",
}


def _review_reason(r: ItemResult) -> str:
    if not r.review_reason:
        return "none"
    codes = [c for c in r.review_reason.split(",") if c]
    return ", ".join(
        f"`{c}`" + (f" ({REVIEW_REASON_HINTS[c]})" if c in REVIEW_REASON_HINTS else "")
        for c in codes
    )


def _render_abstention_diagnostics(summary: EvalSummary) -> list[str]:
    abstained = [r for r in _answerable(summary.results) if not r.answered]
    lines = [
        "## False abstention diagnostics",
        "",
        "Answerable items that were not answered (abstained, routed to review, or no footnote), "
        "with the answer's `review_reason` and the retrieved top-k. Paths and similarity scores "
        "only (no document text). `✓` = path starts with an expected prefix.",
        "",
    ]
    if not abstained:
        return [*lines, "No false abstentions.", ""]
    for r in abstained:
        retrieval, _hit = _retrieval_diagnosis(r)
        lines += [
            f"### {r.item.id}",
            "",
            f"- expected section prefixes: {_expected_prefixes(r)}",
            f"- status: {r.status}",
            *([f"- error: {_cell(r.error)}"] if r.error else []),
            f"- review_reason: {_review_reason(r)}",
            f"- footnotes: {r.footnotes_total}",
            f"- retrieval: {retrieval}",
            "",
            *_render_retrieved_table(summary, r),
            "",
        ]
    return lines


def _flag(value: bool | None) -> str:
    return "-" if value is None else str(value).lower()


def _local_config_header(summary: EvalSummary) -> list[str]:
    """Header lines of a local comparison config (absent from the default run's report)."""
    lines = []
    if summary.think is not None:
        lines.append(f"- think: {_think_text(summary.think)}")
    if summary.fallback_model is not None:
        lines.append(f"- fallback model (cascade): `{summary.fallback_model}`")
        if summary.fallback_think is not None:
            lines.append(f"- fallback think: {_think_text(summary.fallback_think)}")
    return lines


def _optional_metric_rows(summary: EvalSummary) -> list[str]:
    rows = []
    if summary.is_cascade:
        rows.append(
            f"| escalation rate (fallback called) | {_pct(summary.escalation_rate)} "
            f"({escalated_count(summary.results)}/{len(summary.results)}) |"
        )
    if summary.error_count:
        rows.append(f"| errors (LLMError, question skipped) | {summary.error_count} |")
    return rows


def render_report(summary: EvalSummary) -> str:
    false_ids = summary.false_abstentions
    title_model = summary.model + (
        f" > {summary.fallback_model}" if summary.fallback_model is not None else ""
    )
    lines = [
        f"# Eval report ({summary.mode}, {summary.provider}, {title_model})",
        "",
        f"- generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- provider: {summary.provider}",
        f"- model: `{summary.model}`",
        *_local_config_header(summary),
        f"- citation_method: {summary.citation_method}",
        f"- dataset: `{summary.dataset}`",
        f"- evaluated rows: {len(summary.results)} (skipped `_example` rows: {summary.skipped_examples})",
        f"- k: {summary.k}",
        f"- prefer_consolidated: {_flag(summary.prefer_consolidated)}",
        f"- retrieval_candidates: {_opt(summary.retrieval_candidates)}"
        + (" (unused: prefer_consolidated=false)" if summary.prefer_consolidated is False else ""),
        "",
        *_render_imports(summary),
        "## Metrics",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| retrieval recall@{summary.k} | {_pct(summary.recall_at_k)} |",
        f"| citation accuracy | {_pct(summary.citation_accuracy)} |",
        f"| unanswerable correctly abstained | {_pct(summary.abstention_rate)} |",
        f"| avg cost / question (USD) | {summary.avg_cost_usd if summary.avg_cost_usd is not None else 'n/a'} |",
        f"| total cost (USD) | {summary.total_cost_usd} |",
        f"| avg latency (ms) | {summary.avg_latency_ms:.0f} |"
        if summary.avg_latency_ms is not None
        else "| avg latency (ms) | n/a |",
        f"| answerable answered rate | {_ratio(summary.answered_rate, summary.results)} |",
        f"| false abstentions (answerable, not answered) | {len(false_ids)}"
        + (f" ({', '.join(false_ids)})" if false_ids else "")
        + " |",
        f"| citation accuracy (retrieval-hit items only) "
        f"| {_pct(summary.citation_accuracy_retrieval_hit)} |",
        f"| citation accuracy (consolidated/separate counterpart accepted) "
        f"| {_counterpart_ratio(summary.results)} |",
        f"| citation accuracy (retrieval-hit items only, counterpart accepted) "
        f"| {_counterpart_ratio([r for r in summary.results if r.retrieval_hit is True])} |",
        f"| latency p50 (ms) | {_ms(summary.latency_p50_ms)} |",
        f"| latency p95 (ms) | {_ms(summary.latency_p95_ms)} |",
        f"| ungrounded number answers | {summary.ungrounded_answers} |",
        *_optional_metric_rows(summary),
        "",
    ]
    if summary.mode == "fake":
        lines += [
            "> Fake mode: FakeEmbedder/FakeLLM/FakeDartClient. Numbers check pipeline mechanics,",
            "> not answer quality.",
            "",
        ]
    lines += _render_categories(summary)
    if summary.import_failures:
        lines += ["## Import failures", ""] + [f"- {f}" for f in summary.import_failures] + [""]
    lines += [
        "## Items",
        "",
        "| id | answerable | status | recall hit | cited ok/total | abstained | cost | latency ms |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in summary.results:
        lines.append(
            f"| {r.item.id} | {r.item.answerable} | {r.status} | "
            f"{'-' if r.retrieval_hit is None else r.retrieval_hit} | "
            f"{r.footnotes_matching}/{r.footnotes_total} | {r.abstained} | "
            f"{r.cost_usd if r.cost_usd is not None else 'n/a'} | {r.latency_ms} |"
        )
    lines.append("")
    lines += _render_errors(summary)
    lines += _render_abstention_diagnostics(summary)
    lines += _render_diagnostics(summary)
    lines += _render_ungrounded(summary)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---- import wait ----


async def _document_statuses(
    sm: async_sessionmaker[AsyncSession], doc_ids: dict[str, uuid.UUID]
) -> dict[str, Document | None]:
    async with sm() as session:
        return {
            rcept_no: await session.get(Document, doc_id) for rcept_no, doc_id in doc_ids.items()
        }


async def wait_for_imports(
    sm: async_sessionmaker[AsyncSession],
    worker: Worker,
    doc_ids: dict[str, uuid.UUID],
    *,
    timeout_seconds: float = DEFAULT_IMPORT_TIMEOUT_SECONDS,
    poll_seconds: float = IMPORT_POLL_SECONDS,
    max_worker_loops: int = 1000,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> list[str]:
    """Run ``worker`` and poll until every document is ``ready`` or ``failed``.

    Another worker may hold some of the jobs, so an empty queue does not mean the imports are
    done. Between polls our own worker keeps draining whatever is runnable (a retry whose
    backoff expired, or every job when no service worker is running). Returns the import
    failure lines: ``"{rcept_no}: failed {error}"`` or ``"{rcept_no}: timeout({status})"``.
    """
    started = clock()
    while True:
        for _ in range(max_worker_loops):
            if not await worker.run_once():
                break
        docs = await _document_statuses(sm, doc_ids)
        pending = {
            r: d for r, d in docs.items() if d is None or d.status not in TERMINAL_DOCUMENT_STATUSES
        }
        if not pending:
            break
        if clock() - started >= timeout_seconds:
            return [
                *_failed_lines(docs),
                *(
                    f"{r}: timeout({d.status if d else 'missing'}) after {timeout_seconds:.0f}s"
                    for r, d in pending.items()
                ),
            ]
        await sleep(poll_seconds)
    return _failed_lines(docs)


def _failed_lines(docs: dict[str, Document | None]) -> list[str]:
    return [
        f"{r}: failed {d.error or ''}".rstrip()
        for r, d in docs.items()
        if d and d.status == "failed"
    ]


async def _collect_imports(
    sm: async_sessionmaker[AsyncSession], org_id: uuid.UUID, doc_ids: dict[str, uuid.UUID]
) -> list[DocumentImport]:
    async with sm() as session:
        jobs = await latest_jobs_for_documents(session, org_id, list(doc_ids.values()))
        out = []
        for rcept_no, doc_id in doc_ids.items():
            doc = await session.get(Document, doc_id)
            job = jobs.get(doc_id)
            result = job.result if job is not None and isinstance(job.result, dict) else {}
            out.append(
                DocumentImport(
                    rcept_no=rcept_no,
                    corp_name=(doc.meta or {}).get("corp_name") if doc else None,
                    status=doc.status if doc else "missing",
                    section_count=result.get("section_count"),
                    chunk_count=result.get("chunk_count"),
                    embedding_tokens=result.get("embedding_tokens"),
                    cache_hits=result.get("embedding_cache_hits"),
                    cache_misses=result.get("embedding_cache_misses"),
                    error=doc.error if doc else None,
                )
            )
    return out


class MemoizedQueryEmbedder:
    """Eval-only wrapper: ``embed_query`` is cached per question text for the whole process, so
    each question is embedded once however many configs answer it (and ``retrieve`` plus
    ``ask_question`` share one call). Documents pass straight through."""

    def __init__(self, inner: Embedder) -> None:
        self.inner = inner
        self.model = inner.model
        self.dim = inner.dim
        self._queries: dict[str, EmbeddingResult] = {}

    async def embed_documents(
        self, texts: list[str], on_batch: BatchCallback | None = None
    ) -> EmbeddingResult:
        return await self.inner.embed_documents(texts, on_batch=on_batch)

    async def embed_query(self, text: str) -> EmbeddingResult:
        cached = self._queries.get(text)
        if cached is None:
            cached = await self.inner.embed_query(text)
            self._queries[text] = cached
        return cached


@dataclass
class EvalConfig:
    """One answering setup: an LLM, optionally with a cascade fallback (local comparison)."""

    llm: LLMClient
    fallback_llm: LLMClient | None = None
    # Recorded in the report header and file name (local comparison only).
    think: ThinkSetting | None = None
    fallback_think: ThinkSetting | None = None


@dataclass
class PreparedEval:
    """Imports and retrieval shared by every config of a run."""

    runnable: list[EvalItem]
    skipped_examples: int
    admin_id: uuid.UUID | None = None
    rcept_to_doc: dict[str, uuid.UUID] = field(default_factory=dict)
    import_failures: list[str] = field(default_factory=list)
    imports: list[DocumentImport] = field(default_factory=list)
    hits: list[list[RetrievedChunk]] = field(default_factory=list)  # aligned with ``runnable``


def _doc_ids(item: EvalItem, prepared: PreparedEval) -> list[uuid.UUID]:
    return [prepared.rcept_to_doc[r] for r in item.rcept_nos if r in prepared.rcept_to_doc]


async def prepare_eval(
    items: list[EvalItem],
    *,
    settings: Settings,
    embedder: Embedder,
    query_embedder: Embedder,
    dart_client: DartClient,
    max_worker_loops: int = 1000,
    import_timeout_seconds: float = DEFAULT_IMPORT_TIMEOUT_SECONDS,
    import_poll_seconds: float = IMPORT_POLL_SECONDS,
) -> PreparedEval:
    """Seed an org, import every filing once, wait for the imports, retrieve every question."""
    from footnote_rag.db.session import get_sessionmaker

    sm = get_sessionmaker()
    runnable = [i for i in items if not i.example]
    prepared = PreparedEval(runnable=runnable, skipped_examples=len(items) - len(runnable))
    if not runnable:
        return prepared

    run_id = uuid.uuid4().hex[:8]
    async with sm() as session:
        _org, admin = await seed_org(
            session,
            org_name=f"eval-{run_id}",
            admin_email=f"eval-{run_id}@eval.local",
            admin_password=uuid.uuid4().hex,
        )
    org_id = admin.org_id
    rcept_to_doc: dict[str, uuid.UUID] = {}
    async with sm() as session:
        user = await session.get(User, admin.id)
        assert user is not None
        for rcept_no in sorted({r for i in runnable for r in i.rcept_nos}):
            result = await request_dart_import(session, user=user, rcept_no=rcept_no)
            rcept_to_doc[rcept_no] = result.document.id

    worker = Worker(
        sessionmaker=sm,
        context=JobContext(sm, settings, embedder, dart_client),
        worker_id=f"eval-{run_id}",
    )
    prepared.admin_id, prepared.rcept_to_doc = admin.id, rcept_to_doc
    prepared.import_failures = await wait_for_imports(
        sm,
        worker,
        rcept_to_doc,
        timeout_seconds=import_timeout_seconds,
        poll_seconds=import_poll_seconds,
        max_worker_loops=max_worker_loops,
    )
    prepared.imports = await _collect_imports(sm, org_id, rcept_to_doc)

    for item in runnable:
        doc_ids = _doc_ids(item, prepared)
        async with sm() as session:
            user = await session.get(User, admin.id)
            assert user is not None
            prepared.hits.append(
                await retrieve(
                    session,
                    user=user,
                    query=item.question,
                    top_k=settings.top_k,
                    document_ids=doc_ids or None,
                    embedder=query_embedder,
                    settings=settings,
                )
            )
    return prepared


def _retrieval_fields(item: EvalItem, hits: list[RetrievedChunk]) -> dict:
    prefixes = item.expected_section_prefixes
    return {
        "retrieval_hit": any(_matches(h.section_path, prefixes) for h in hits)
        if item.answerable and prefixes
        else None,
        "retrieved": [
            RetrievedRef(
                h.section_path,
                h.score,
                bool(prefixes) and _matches(h.section_path, prefixes),
            )
            for h in hits
        ],
    }


def _item_result(item: EvalItem, hits: list[RetrievedChunk], ask: AskResult) -> ItemResult:
    prefixes = item.expected_section_prefixes
    cited_paths = [str(f.get("section_path", "")) for f in ask.footnotes]
    matching, counterpart = score_citations(cited_paths, prefixes)
    abstained = (
        not ask.footnotes
        or ask.answer.status == "pending_review"
        or NO_EVIDENCE_TEXT in ask.answer.text
    )
    cost = ask.usage.get("cost_usd")
    footnote_sources = [source_id(f.get("document_id"), f.get("chunk_id")) for f in ask.footnotes]
    return ItemResult(
        item=item,
        footnotes_total=len(ask.footnotes),
        footnotes_matching=matching,
        abstained=abstained,
        status=ask.answer.status,
        cost_usd=Decimal(cost) if cost is not None else None,
        latency_ms=ask.latency_ms,
        answer_preview=ask.answer.text[:80],
        cited_paths=cited_paths,
        footnotes_counterpart=counterpart if item.answerable else 0,
        review_reason=ask.answer.review_reason,
        escalated=ask.escalated,
        ungrounded_numbers=list(dict.fromkeys(u.text for u in ask.ungrounded_numbers)),
        footnote_ungrounded=[
            list(dict.fromkeys(u.text for u in ask.ungrounded_numbers if src in u.sources))
            for src in footnote_sources
        ],
        **_retrieval_fields(item, hits),
    )


def _error_result(
    item: EvalItem, hits: list[RetrievedChunk], exc: LLMError, latency_ms: int
) -> ItemResult:
    return ItemResult(
        item=item,
        footnotes_total=0,
        footnotes_matching=0,
        abstained=True,
        status="error",
        cost_usd=None,
        latency_ms=latency_ms,
        answer_preview="",
        error=exc.public_message,
        **_retrieval_fields(item, hits),
    )


def _new_summary(
    config: EvalConfig, *, settings: Settings, mode: str, dataset_name: str
) -> EvalSummary:
    provider, model, method = describe_llm(config.llm)
    return EvalSummary(
        mode=mode,
        dataset=dataset_name,
        k=settings.top_k,
        provider=provider,
        model=model,
        citation_method=method,
        prefer_consolidated=settings.prefer_consolidated,
        retrieval_candidates=settings.retrieval_candidates,
        think=config.think,
        fallback_model=describe_llm(config.fallback_llm)[1] if config.fallback_llm else None,
        fallback_think=config.fallback_think,
    )


async def run_config(
    prepared: PreparedEval,
    config: EvalConfig,
    *,
    settings: Settings,
    query_embedder: Embedder,
    mode: str,
    dataset_name: str,
    warm_up: bool = False,
) -> EvalSummary:
    """Ask every prepared question with one config. ``LLMError`` -> an ``error`` row, go on."""
    from footnote_rag.db.session import get_sessionmaker

    sm = get_sessionmaker()
    summary = _new_summary(config, settings=settings, mode=mode, dataset_name=dataset_name)
    summary.skipped_examples = prepared.skipped_examples
    summary.import_failures = list(prepared.import_failures)
    summary.imports = list(prepared.imports)
    if not prepared.runnable:
        return summary
    warm = getattr(config.llm, "warm_up", None)
    if warm_up and callable(warm):
        await warm()  # primary only: a cascade's model swap on escalation is a real cost
    for item, hits in zip(prepared.runnable, prepared.hits, strict=True):
        doc_ids = _doc_ids(item, prepared)
        started = time.perf_counter()
        async with sm() as session:
            user = await session.get(User, prepared.admin_id)
            assert user is not None
            try:
                ask = await ask_question(
                    session,
                    user=user,
                    question=item.question,
                    document_ids=doc_ids or None,
                    embedder=query_embedder,
                    llm=config.llm,
                    settings=settings,
                    fallback_llm=config.fallback_llm,
                )
            except LLMError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                log.warning(
                    "eval.item_error", item=item.id, model=summary.label, status=exc.status_code
                )
                summary.results.append(_error_result(item, hits, exc, latency_ms))
                continue
        summary.results.append(_item_result(item, hits, ask))
    return summary


async def run_eval_configs(
    items: list[EvalItem],
    *,
    settings: Settings,
    embedder: Embedder,
    configs: Sequence[EvalConfig],
    dart_client: DartClient,
    mode: str,
    dataset_name: str,
    warm_up: bool = False,
    max_worker_loops: int = 1000,
    import_timeout_seconds: float = DEFAULT_IMPORT_TIMEOUT_SECONDS,
    import_poll_seconds: float = IMPORT_POLL_SECONDS,
) -> list[EvalSummary]:
    """Import once, embed each question once, then run every config on the same questions."""
    query_embedder = (
        embedder if isinstance(embedder, MemoizedQueryEmbedder) else MemoizedQueryEmbedder(embedder)
    )
    prepared = await prepare_eval(
        items,
        settings=settings,
        embedder=embedder,
        query_embedder=query_embedder,
        dart_client=dart_client,
        max_worker_loops=max_worker_loops,
        import_timeout_seconds=import_timeout_seconds,
        import_poll_seconds=import_poll_seconds,
    )
    return [
        await run_config(
            prepared,
            config,
            settings=settings,
            query_embedder=query_embedder,
            mode=mode,
            dataset_name=dataset_name,
            warm_up=warm_up,
        )
        for config in configs
    ]


async def run_eval(
    items: list[EvalItem],
    *,
    settings: Settings,
    embedder: Embedder,
    llm: LLMClient,
    dart_client: DartClient,
    mode: str,
    dataset_name: str,
    max_worker_loops: int = 1000,
    import_timeout_seconds: float = DEFAULT_IMPORT_TIMEOUT_SECONDS,
    import_poll_seconds: float = IMPORT_POLL_SECONDS,
) -> EvalSummary:
    """Single-provider run (the default CLI path): one config, one summary."""
    (summary,) = await run_eval_configs(
        items,
        settings=settings,
        embedder=embedder,
        configs=[EvalConfig(llm=llm)],
        dart_client=dart_client,
        mode=mode,
        dataset_name=dataset_name,
        max_worker_loops=max_worker_loops,
        import_timeout_seconds=import_timeout_seconds,
        import_poll_seconds=import_poll_seconds,
    )
    return summary


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def write_report(summary: EvalSummary, out_dir: Path, stamp: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / report_filename(summary, stamp or _stamp())
    path.write_text(render_report(summary), encoding="utf-8")
    return path


# ---- local model comparison report ----

STATUS_ABBREVIATIONS = {"published": "pub", "pending_review": "rev", "error": "err"}


def _escalation_cell(summary: EvalSummary) -> str:
    if not summary.is_cascade:
        return "n/a"
    return (
        f"{_pct(summary.escalation_rate)} ({escalated_count(summary.results)}"
        f"/{len(summary.results)})"
    )


def _matrix_cell(r: ItemResult | None) -> str:
    if r is None:
        return "-"
    cell = (
        f"{r.footnotes_matching}/{r.footnotes_total} {STATUS_ABBREVIATIONS.get(r.status, r.status)}"
    )
    return cell + (" ↑" if r.escalated else "")


def render_compare_report(
    summaries: Sequence[EvalSummary], report_names: Sequence[str], *, dataset: str, k: int
) -> str:
    """Side-by-side view of local configs; every number comes from the per-config metrics."""
    labels = [_cell(s.label) for s in summaries]
    rows = len(summaries[0].results) if summaries else 0
    lines = [
        "# Eval comparison (live, local models)",
        "",
        f"- generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- dataset: `{dataset}` ({rows} rows), k: {k}",
        "- answers: local Ollama only · question embeddings: Voyage (document chunks cached, "
        "one query embedding per question shared by every config)",
        "- configs: " + ", ".join(f"`{label}`" for label in labels),
        "- per-config reports:",
        *(f"  - [{name}]({name})" for name in report_names),
        "",
        "## Configs",
        "",
        f"| config | recall@{k} | citation acc. | citation acc. (hit) | answered (answerable) "
        "| false abst. | unans. abstained | ungrounded number answers | escalation rate | errors "
        "| p50 ms | p95 ms | cost USD |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, s in zip(labels, summaries, strict=True):
        lines.append(
            f"| {label} | {_pct(s.recall_at_k)} | {_pct(s.citation_accuracy)} "
            f"| {_pct(s.citation_accuracy_retrieval_hit)} | {_ratio(s.answered_rate, s.results)} "
            f"| {len(s.false_abstentions)} | {_pct(s.abstention_rate)} | {s.ungrounded_answers} "
            f"| {_escalation_cell(s)} | {s.error_count} | {_ms(s.latency_p50_ms)} "
            f"| {_ms(s.latency_p95_ms)} | {s.total_cost_usd:.4f} |"
        )
    header = "| id | " + " | ".join(labels) + " |"
    separator = "| --- |" + " --- |" * len(labels)
    lines += [
        "",
        "## Items",
        "",
        "`cited ok/total` + status: `pub` = published, `rev` = pending_review, `err` = error, "
        "`↑` = escalated to fallback",
        "",
        header,
        separator,
    ]
    by_id = [{r.item.id: r for r in s.results} for s in summaries]
    ids = list(dict.fromkeys(r.item.id for s in summaries for r in s.results))
    for item_id in ids:
        cells = [_matrix_cell(index.get(item_id)) for index in by_id]
        lines.append(f"| {_cell(item_id)} | " + " | ".join(cells) + " |")
    groups = [s.by_category() for s in summaries]
    categories = list(dict.fromkeys(name for g in groups for name in g))
    lines += [
        "",
        "## By question type (citation accuracy)",
        "",
        header.replace("| id |", "| type |"),
    ]
    lines.append(separator)
    for name in categories:
        cells = [_pct(citation_accuracy(g[name])) if name in g else "-" for g in groups]
        lines.append(f"| {_cell(name)} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write_compare_reports(
    summaries: Sequence[EvalSummary], out_dir: Path, *, dataset: str, k: int
) -> tuple[list[Path], Path]:
    """Per-config reports and ``{stamp}-live-compare-local.md``, all with one timestamp."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    paths: list[Path] = []
    for summary in summaries:
        name = report_filename(summary, stamp)
        n = 2
        while any(p.name == name for p in paths):  # the same config given twice
            name = report_filename(summary, stamp).removesuffix(".md") + f"-{n}.md"
            n += 1
        path = out_dir / name
        path.write_text(render_report(summary), encoding="utf-8")
        paths.append(path)
    compare = out_dir / compare_report_filename(stamp)
    compare.write_text(
        render_compare_report(summaries, [p.name for p in paths], dataset=dataset, k=k),
        encoding="utf-8",
    )
    return paths, compare


# ---- local model specs (CLI) and preflight ----


@dataclass(frozen=True)
class ModelSpec:
    """``model[@think]``; ``think`` None = ``LOCAL_LLM_THINK``."""

    model: str
    think: ThinkSetting | None = None


def parse_think(text: str) -> ThinkSetting:
    value = text.strip().lower()
    if value in ("true", "false"):
        return value == "true"
    if value in THINK_LEVELS:
        return value  # type: ignore[return-value]
    raise ValueError(f"think must be one of true, false, {', '.join(THINK_LEVELS)}: {text!r}")


def parse_model_spec(text: str) -> ModelSpec:
    name, sep, think = text.strip().partition("@")
    name = name.strip()
    if not name:
        raise ValueError(f"empty model name in {text!r}")
    return ModelSpec(name, parse_think(think) if sep else None)


def parse_local_models(text: str) -> list[ModelSpec]:
    specs = [parse_model_spec(part) for part in text.split(",") if part.strip()]
    if not specs:
        raise ValueError("no model given")
    return specs


def parse_cascade(text: str) -> tuple[ModelSpec, ModelSpec]:
    primary, sep, fallback = text.partition(">")
    if not sep or ">" in fallback:
        raise ValueError(f"expected PRIMARY>FALLBACK: {text!r}")
    return parse_model_spec(primary), parse_model_spec(fallback)


def _arg_type(parse: Callable[[str], object]) -> Callable[[str], object]:
    def wrapped(text: str) -> object:
        try:
            return parse(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    wrapped.__name__ = parse.__name__
    return wrapped


def build_local_configs(
    settings: Settings,
    local_models: Sequence[ModelSpec],
    cascades: Sequence[tuple[ModelSpec, ModelSpec]],
) -> list[EvalConfig]:
    """``OllamaClient`` per config via ``providers.make_local_llm`` (never a Claude client)."""
    from footnote_rag import providers

    def resolve(spec: ModelSpec) -> ThinkSetting:
        return settings.local_llm_think if spec.think is None else spec.think

    configs = [
        EvalConfig(
            llm=providers.make_local_llm(settings, model=spec.model, think=resolve(spec)),
            think=resolve(spec),
        )
        for spec in local_models
    ]
    for primary, fallback in cascades:
        configs.append(
            EvalConfig(
                llm=providers.make_local_llm(settings, model=primary.model, think=resolve(primary)),
                fallback_llm=providers.make_local_llm(
                    settings, model=fallback.model, think=resolve(fallback)
                ),
                think=resolve(primary),
                fallback_think=resolve(fallback),
            )
        )
    return configs


@dataclass
class PreflightProblem:
    model: str
    base_url: str | None
    connected: bool | None
    detail: str | None


async def preflight_local_models(configs: Sequence[EvalConfig]) -> list[PreflightProblem]:
    """``status()`` of every distinct model; problems = unreachable server or missing model."""
    clients: dict[tuple[str, str], LLMClient] = {}
    for config in configs:
        for llm in (config.llm, config.fallback_llm):
            if llm is not None:
                key = (str(getattr(llm, "base_url", "")), str(getattr(llm, "model", "")))
                clients.setdefault(key, llm)
    problems: list[PreflightProblem] = []
    for (base_url, model), llm in clients.items():
        status = getattr(llm, "status", None)
        if not callable(status):
            continue
        data = await status()
        if data.get("connected") is not True or data.get("model_available") is not True:
            problems.append(
                PreflightProblem(model, base_url or None, data.get("connected"), data.get("detail"))
            )
    return problems


def render_preflight(problems: Sequence[PreflightProblem]) -> str:
    lines = ["로컬 모델 사전 점검 실패: 공시를 가져오기 전에 중단합니다."]
    for p in problems:
        state = "Ollama 연결 실패" if p.connected is not True else "모델 없음"
        where = f" ({p.base_url})" if p.base_url else ""
        lines.append(f"- {p.model}: {state}{where}" + (f" - {p.detail}" if p.detail else ""))
    if any(p.connected is not True for p in problems):
        lines.append("Ollama 서버를 실행했는지, LOCAL_LLM_BASE_URL이 맞는지 확인하세요.")
    lines.append("필요한 모델을 받은 뒤 다시 실행하세요:")
    lines += [f"  ollama pull {m}" for m in dict.fromkeys(p.model for p in problems)]
    return "\n".join(lines)


async def _amain(args: argparse.Namespace) -> int:
    from footnote_rag.answering.llm import FakeLLM
    from footnote_rag.dart.client import FakeDartClient
    from footnote_rag.db.session import dispose_engine
    from footnote_rag.providers import make_dart_client, make_embedder, make_llm
    from footnote_rag.retrieval.embedder import FakeEmbedder

    settings = get_settings()
    if args.local_models or args.cascade:
        return await _amain_compare(args, settings)
    if args.live:
        embedder, llm, dart = (
            make_embedder(settings),
            make_llm(settings),
            make_dart_client(settings),
        )
        mode = "live"
    else:
        embedder, llm, dart = FakeEmbedder(settings.embedding_dim), FakeLLM(), FakeDartClient()
        mode = "fake"
    items = load_dataset(args.dataset)
    try:
        summary = await run_eval(
            items,
            settings=settings,
            embedder=embedder,
            llm=llm,
            dart_client=dart,
            mode=mode,
            dataset_name=str(args.dataset),
            import_timeout_seconds=args.import_timeout,
        )
    finally:
        await dispose_engine()
    path = write_report(summary, args.out)
    print(f"report written: {path}")
    return 0


async def _amain_compare(args: argparse.Namespace, settings: Settings) -> int:
    """``--live --local-models``/``--cascade``: preflight, one import, every config, reports."""
    from footnote_rag.db.session import dispose_engine
    from footnote_rag.providers import make_dart_client, make_embedder

    configs = build_local_configs(settings, args.local_models or [], args.cascade or [])
    problems = await preflight_local_models(configs)
    if problems:
        print(render_preflight(problems), file=sys.stderr)
        return PREFLIGHT_EXIT_CODE
    embedder, dart = make_embedder(settings), make_dart_client(settings)
    items = load_dataset(args.dataset)
    try:
        summaries = await run_eval_configs(
            items,
            settings=settings,
            embedder=embedder,
            configs=configs,
            dart_client=dart,
            mode="live",
            dataset_name=str(args.dataset),
            warm_up=True,
            import_timeout_seconds=args.import_timeout,
        )
    finally:
        await dispose_engine()
    paths, compare = write_compare_reports(
        summaries, args.out, dataset=str(args.dataset), k=settings.top_k
    )
    for path in paths:
        print(f"report written: {path}")
    print(f"comparison written: {compare}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="footnote_rag.evals.run")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--live", action="store_true", help="use real providers (costs money)")
    parser.add_argument("--out", type=Path, default=Path("evals/reports"))
    parser.add_argument(
        "--import-timeout",
        type=float,
        default=DEFAULT_IMPORT_TIMEOUT_SECONDS,
        help="seconds to wait for every imported filing to become ready/failed (default 1800)",
    )
    parser.add_argument(
        "--local-models",
        type=_arg_type(parse_local_models),
        default=None,
        metavar="SPEC[,SPEC...]",
        help="compare local Ollama models (needs --live); SPEC = model[@think], think = true, "
        "false, low, medium or high (default LOCAL_LLM_THINK)",
    )
    parser.add_argument(
        "--cascade",
        type=_arg_type(parse_cascade),
        action="append",
        default=None,
        metavar="PRIMARY>FALLBACK",
        help="add a local cascade config (needs --live; repeatable), e.g. 'qwen3.5:9b>qwen3:14b'",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.local_models or args.cascade) and not args.live:
        parser.error("--local-models and --cascade need --live")
    return args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    return asyncio.run(_amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
