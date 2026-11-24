"""Eval 실행기(recall@k, 인용 정확도, 거절 비율, 평균 비용·지연시간).

역할: 임시 조직을 만들어 데이터셋의 공시를 정상 서비스·워커로 가져오고, 질문마다 검색·답변 결과를 채점해
Markdown 리포트를 쓴다. 기본은 Fake 모드(비용 없음), ``--live``는 실제 제공자.
주요 요소: ``run_eval``, ``EvalSummary``(지표 속성), ``render_report``, ``write_report``, ``main``.
호출 관계: ``documents.service``, ``jobs.worker.Worker``, ``answering.service``를 그대로 호출한다.
학습 문서: docs/study/10-eval-and-mcp.md

Eval runner.

    python -m footnote_rag.evals.run [--dataset evals/dataset.jsonl] [--live] [--out evals/reports]

Default is fake mode (FakeEmbedder / FakeLLM / FakeDartClient): no network, no cost; it
exercises the pipeline mechanics. ``--live`` uses the configured providers, including the LLM
selected by ``LLM_PROVIDER`` (``real`` = Claude, ``local`` = Ollama). The report header and file
name carry the provider, model and citation_method so Claude and local runs can be compared.

The runner creates a throw-away organization, imports every filing referenced by the
dataset through the normal service + worker, then asks each question through the normal
answering service and measures:

* retrieval recall@k   - an expected-section chunk is among the top-k results
* citation accuracy    - share of footnotes whose section_path matches an expected prefix
* abstention           - unanswerable questions were not answered (no footnotes, "근거 없음",
                         or routed to review)
* average cost / latency per question
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from footnote_rag.answering.llm import CITATION_API, NO_EVIDENCE_TEXT, LLMClient
from footnote_rag.answering.service import ask_question, retrieve
from footnote_rag.config import LOCAL_MODEL_PREFIX, Settings, get_settings
from footnote_rag.dart.client import DartClient
from footnote_rag.db.models import Document, User
from footnote_rag.documents.service import request_dart_import
from footnote_rag.evals.dataset import DEFAULT_DATASET, EvalItem, load_dataset
from footnote_rag.jobs.handlers import JobContext
from footnote_rag.jobs.worker import Worker
from footnote_rag.orgs.service import seed_org
from footnote_rag.retrieval.embedder import Embedder


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


@dataclass
class EvalSummary:
    mode: str
    dataset: str
    k: int
    results: list[ItemResult] = field(default_factory=list)
    skipped_examples: int = 0
    import_failures: list[str] = field(default_factory=list)
    # Which LLM produced the answers, so Claude and local (Ollama) reports can be compared.
    provider: str = "unknown"
    model: str = "unknown"
    citation_method: str = CITATION_API

    def _answerable(self) -> list[ItemResult]:
        return [r for r in self.results if r.item.answerable]

    @property
    def recall_at_k(self) -> float | None:
        rows = [r for r in self._answerable() if r.retrieval_hit is not None]
        return sum(r.retrieval_hit for r in rows) / len(rows) if rows else None

    @property
    def citation_accuracy(self) -> float | None:
        total = sum(r.footnotes_total for r in self._answerable())
        return sum(r.footnotes_matching for r in self._answerable()) / total if total else None

    @property
    def abstention_rate(self) -> float | None:
        rows = [r for r in self.results if not r.item.answerable]
        return sum(r.abstained for r in rows) / len(rows) if rows else None

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


def _matches(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(p) for p in prefixes)


def describe_llm(llm: LLMClient) -> tuple[str, str, str]:
    """(provider, model id as stored in llm_calls, citation_method) of an LLM client."""
    provider = str(getattr(llm, "provider", "unknown"))
    model = str(getattr(llm, "model_id", None) or getattr(llm, "model", "unknown"))
    method = str(getattr(llm, "citation_method", CITATION_API))
    return provider, model, method


def report_filename(summary: EvalSummary, stamp: str) -> str:
    """``{stamp}-{mode}-{provider}-{model}.md`` (provider omitted when it equals the mode)."""
    model = summary.model.removeprefix(LOCAL_MODEL_PREFIX)
    slug = re.sub(r"[^A-Za-z0-9._]+", "-", model).strip("-") or "unknown"
    parts = [stamp, summary.mode]
    if summary.provider != summary.mode:
        parts.append(summary.provider)
    parts.append(slug)
    return "-".join(parts) + ".md"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_report(summary: EvalSummary) -> str:
    lines = [
        f"# Eval report ({summary.mode}, {summary.provider}, {summary.model})",
        "",
        f"- generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- provider: {summary.provider}",
        f"- model: `{summary.model}`",
        f"- citation_method: {summary.citation_method}",
        f"- dataset: `{summary.dataset}`",
        f"- evaluated rows: {len(summary.results)} (skipped `_example` rows: {summary.skipped_examples})",
        f"- k: {summary.k}",
        "",
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
        "",
    ]
    if summary.mode == "fake":
        lines += [
            "> Fake mode: FakeEmbedder/FakeLLM/FakeDartClient. Numbers check pipeline mechanics,",
            "> not answer quality.",
            "",
        ]
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
    return "\n".join(lines) + "\n"


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
) -> EvalSummary:
    from footnote_rag.db.session import get_sessionmaker

    sm = get_sessionmaker()
    provider, model, method = describe_llm(llm)
    summary = EvalSummary(
        mode=mode,
        dataset=dataset_name,
        k=settings.top_k,
        provider=provider,
        model=model,
        citation_method=method,
    )
    runnable = [i for i in items if not i.example]
    summary.skipped_examples = len(items) - len(runnable)
    if not runnable:
        return summary

    run_id = uuid.uuid4().hex[:8]
    async with sm() as session:
        _org, admin = await seed_org(
            session,
            org_name=f"eval-{run_id}",
            admin_email=f"eval-{run_id}@eval.local",
            admin_password=uuid.uuid4().hex,
        )
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
    for _ in range(max_worker_loops):
        if not await worker.run_once():
            break
    async with sm() as session:
        for rcept_no, doc_id in rcept_to_doc.items():
            doc = await session.get(Document, doc_id)
            if doc is None or doc.status != "ready":
                summary.import_failures.append(
                    f"{rcept_no}: {doc.status if doc else 'missing'} {doc.error if doc else ''}"
                )

    for item in runnable:
        doc_ids = [rcept_to_doc[r] for r in item.rcept_nos if r in rcept_to_doc]
        async with sm() as session:
            user = await session.get(User, admin.id)
            assert user is not None
            hits = await retrieve(
                session,
                user=user,
                query=item.question,
                top_k=settings.top_k,
                document_ids=doc_ids or None,
                embedder=embedder,
            )
        async with sm() as session:
            user = await session.get(User, admin.id)
            assert user is not None
            ask = await ask_question(
                session,
                user=user,
                question=item.question,
                document_ids=doc_ids or None,
                embedder=embedder,
                llm=llm,
                settings=settings,
            )
        prefixes = item.expected_section_prefixes
        matching = sum(1 for f in ask.footnotes if _matches(f.get("section_path", ""), prefixes))
        abstained = (
            not ask.footnotes
            or ask.answer.status == "pending_review"
            or NO_EVIDENCE_TEXT in ask.answer.text
        )
        cost = ask.usage.get("cost_usd")
        summary.results.append(
            ItemResult(
                item=item,
                retrieval_hit=any(_matches(h.section_path, prefixes) for h in hits)
                if item.answerable and prefixes
                else None,
                footnotes_total=len(ask.footnotes),
                footnotes_matching=matching,
                abstained=abstained,
                status=ask.answer.status,
                cost_usd=Decimal(cost) if cost is not None else None,
                latency_ms=ask.latency_ms,
                answer_preview=ask.answer.text[:80],
            )
        )
    return summary


def write_report(summary: EvalSummary, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / report_filename(summary, stamp)
    path.write_text(render_report(summary), encoding="utf-8")
    return path


async def _amain(args: argparse.Namespace) -> int:
    from footnote_rag.answering.llm import FakeLLM
    from footnote_rag.dart.client import FakeDartClient
    from footnote_rag.db.session import dispose_engine
    from footnote_rag.providers import make_dart_client, make_embedder, make_llm
    from footnote_rag.retrieval.embedder import FakeEmbedder

    settings = get_settings()
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
        )
    finally:
        await dispose_engine()
    path = write_report(summary, args.out)
    print(f"report written: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="footnote_rag.evals.run")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--live", action="store_true", help="use real providers (costs money)")
    parser.add_argument("--out", type=Path, default=Path("evals/reports"))
    return asyncio.run(_amain(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
