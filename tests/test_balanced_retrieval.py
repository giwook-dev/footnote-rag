"""Balanced multi-document retrieval (docs/adr/0011): per-document search, round robin by rank,
score order, the low-score rule over every document, and the unchanged single-search paths."""

from __future__ import annotations

import math
import uuid

import pytest

from footnote_rag.answering.service import interleave_by_rank, retrieve, retrieve_ranked
from footnote_rag.db.models import Chunk, Document, User
from footnote_rag.retrieval.embedder import EmbeddingResult
from footnote_rag.retrieval.search import RetrievedChunk, search_chunks

AUDITOR = "V. 회계감사인의 감사의견 등 > 1. 외부감사에 관한 사항"


def _axis_vector(dim: int, score: float) -> list[float]:
    """Unit vector whose cosine similarity with the query axis (e0) is ``score``."""
    vec = [0.0] * dim
    vec[0], vec[1] = score, math.sqrt(1 - score * score)
    return vec


class _AxisEmbedder:
    model = "axis"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed_query(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(vectors=[_axis_vector(self.dim, 1.0)], total_tokens=1)


async def _add_document(sessionmaker, org, dim: int, name: str, scores: list[float]) -> uuid.UUID:
    async with sessionmaker() as session:
        doc = Document(
            id=uuid.uuid4(),
            org_id=org.org_id,
            created_by=org.admin_id,
            title=f"{name} 사업보고서",
            source="upload",
            source_ref=uuid.uuid4().hex,
            meta={"corp_name": name},
            status="ready",
        )
        session.add(doc)
        await session.flush()
        for i, score in enumerate(scores):
            session.add(
                Chunk(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    org_id=org.org_id,
                    section_path=f"{AUDITOR} > {name} {i}",
                    chunk_index=i,
                    text=f"{name} 본문 {i}",
                    embedding=_axis_vector(dim, score),
                )
            )
        await session.commit()
        return doc.id


@pytest.fixture
async def three_documents(make_org, sessionmaker, settings):
    """compare-04 shape: one company's chunks are all more similar than the other two's."""
    org = await make_org()
    dim = settings.embedding_dim
    high = await _add_document(sessionmaker, org, dim, "삼성전자", [0.95, 0.94, 0.93, 0.92, 0.91])
    mid = await _add_document(sessionmaker, org, dim, "NAVER", [0.60, 0.59, 0.58, 0.57, 0.56])
    low = await _add_document(sessionmaker, org, dim, "현대자동차", [0.50, 0.49, 0.48, 0.47])
    return org, [high, mid, low]


def _settings(settings, **update):
    return settings.model_copy(update=update)


async def _retrieve(sessionmaker, org, settings, document_ids, *, ranked=False, top_k=8):
    async with sessionmaker() as session:
        user = await session.get(User, org.admin_id)
        fn = retrieve_ranked if ranked else retrieve
        return await fn(
            session,
            user=user,
            query="세 회사의 외부감사인은?",
            top_k=top_k,
            document_ids=document_ids,
            embedder=_AxisEmbedder(settings.embedding_dim),
            settings=settings,
        )


def _per_document(chunks: list[RetrievedChunk], doc_ids: list[uuid.UUID]) -> list[int]:
    return [sum(1 for c in chunks if c.document_id == d) for d in doc_ids]


@pytest.mark.parametrize("prefer", [True, False], ids=["prefer-consolidated", "plain"])
async def test_three_documents_get_3_3_2_in_score_order(
    three_documents, sessionmaker, settings, prefer
):
    org, docs = three_documents
    s = _settings(settings, balance_multi_document_retrieval=True, prefer_consolidated=prefer)
    chunks = await _retrieve(sessionmaker, org, s, docs)
    assert len(chunks) == 8
    assert _per_document(chunks, docs) == [3, 3, 2]
    scores = [c.score for c in chunks]
    assert scores == sorted(scores, reverse=True)
    # each document contributes its own best chunks
    assert [round(c.score, 2) for c in chunks if c.document_id == docs[2]] == [0.5, 0.49]
    # the unbalanced search would have taken only the top company's chunks plus NAVER's best
    off = _settings(settings, balance_multi_document_retrieval=False, prefer_consolidated=prefer)
    assert _per_document(await _retrieve(sessionmaker, org, off, docs), docs) == [5, 3, 0]


async def test_short_document_leaves_its_slots_to_the_others(make_org, sessionmaker, settings):
    org = await make_org()
    dim = settings.embedding_dim
    a = await _add_document(sessionmaker, org, dim, "A", [0.9, 0.89, 0.88, 0.87, 0.86])
    b = await _add_document(sessionmaker, org, dim, "B", [0.7, 0.69, 0.68, 0.67, 0.66])
    c = await _add_document(sessionmaker, org, dim, "C", [0.5])
    chunks = await _retrieve(sessionmaker, org, settings, [a, b, c])
    assert settings.balance_multi_document_retrieval is True  # the default
    # rank 1 from all three; C has no rank 2, so A and B fill the rest rank by rank (A first)
    assert _per_document(chunks, [a, b, c]) == [4, 3, 1]


async def test_top_score_is_the_best_candidate_over_every_document(
    three_documents, sessionmaker, settings
):
    org, docs = three_documents
    result = await _retrieve(sessionmaker, org, settings, list(reversed(docs)), ranked=True)
    assert result.top_score == pytest.approx(0.95, abs=1e-3)
    assert _per_document(result.chunks, docs) == [2, 3, 3]  # document order decides the 8th slot


@pytest.mark.parametrize("prefer", [True, False], ids=["prefer-consolidated", "plain"])
async def test_off_single_document_and_org_wide_are_unchanged(
    three_documents, sessionmaker, settings, prefer
):
    org, docs = three_documents
    on = _settings(settings, balance_multi_document_retrieval=True, prefer_consolidated=prefer)
    off = _settings(settings, balance_multi_document_retrieval=False, prefer_consolidated=prefer)

    def ids(chunks):
        return [(c.chunk_id, round(c.score, 6)) for c in chunks]

    # BALANCE off: the single search over all three documents
    async with sessionmaker() as session:
        baseline = await search_chunks(
            session,
            org_id=org.org_id,
            query_vector=_axis_vector(settings.embedding_dim, 1.0),
            top_k=8,
            document_ids=docs,
        )
    assert ids(await _retrieve(sessionmaker, org, off, docs)) == ids(baseline)
    # one document (also given twice) and the org-wide search: the same with the flag on or off
    for scope in ([docs[1]], [docs[1], docs[1]], None):
        assert ids(await _retrieve(sessionmaker, org, on, scope)) == ids(
            await _retrieve(sessionmaker, org, off, scope)
        )
    one = await _retrieve(sessionmaker, org, on, [docs[1]], ranked=True)
    assert _per_document(one.chunks, docs) == [0, 5, 0]
    assert one.top_score == pytest.approx(0.60, abs=1e-3)


def _chunk(doc: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid5(uuid.NAMESPACE_URL, doc),
        document_title=doc,
        document_meta={},
        source="dart",
        source_ref=None,
        section_path=f"{doc} {score}",
        chunk_index=0,
        text="",
        score=score,
    )


def test_interleave_by_rank_round_robin_then_score_order():
    a = [_chunk("a", s) for s in (0.9, 0.8, 0.7)]
    b = [_chunk("b", s) for s in (0.6, 0.5, 0.4)]
    c = [_chunk("c", s) for s in (0.95,)]
    picked = interleave_by_rank([a, b, c], 5)
    assert [x.section_path for x in picked] == ["c 0.95", "a 0.9", "a 0.8", "b 0.6", "b 0.5"]
    assert interleave_by_rank([a, b, c], 0) == []
    assert interleave_by_rank([[], []], 8) == []
    assert len(interleave_by_rank([a, b, c], 20)) == 7  # every candidate, no duplicates
