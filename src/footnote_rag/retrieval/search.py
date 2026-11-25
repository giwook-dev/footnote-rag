"""pgvector cosine 벡터 검색. 항상 한 조직의 ``ready`` 문서로 범위를 제한한다.

역할: 질문 벡터와 가까운 청크 top_k개를 cosine 거리순으로 찾고 점수(1 - 거리)를 붙인다.
``prefer_statement_scope``는 넓게 가져온 후보에서 같은 문서의 연결↔별도 대응 주석 중 질문이 원하지 않는
쪽(기본은 별도)을 뒤로 보내 최종 top_k를 고른다(docs/adr/0009-prefer-consolidated-notes.md).
주요 요소: ``search_chunks``, ``RetrievedChunk``, ``prefer_statement_scope``, ``preferred_scope``.
호출 관계: ``answering.service.retrieve``가 호출한다. HNSW 인덱스는 alembic/versions/0001_initial.py에서 만든다.
학습 문서: docs/study/05-rag-retrieval.md (테넌트 필터와 hnsw.iterative_scan, 연결 주석 우선)

pgvector cosine search, always scoped to one organization's ready documents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.dart.sections import CONSOLIDATED, SEPARATE, StatementScope, note_key
from footnote_rag.dart.sections import statement_scope as scope_of
from footnote_rag.db.models import Chunk, Document

# A question containing one of these asks about the separate (parent-only) statements.
SEPARATE_QUERY_TERMS = ("별도", "개별")


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    document_meta: dict[str, Any]
    source: str
    source_ref: str | None
    section_path: str
    chunk_index: int
    text: str
    score: float  # cosine similarity in [-1, 1]


async def search_chunks(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    query_vector: list[float],
    top_k: int,
    document_ids: list[uuid.UUID] | None = None,
) -> list[RetrievedChunk]:
    distance = Chunk.embedding.cosine_distance(query_vector)
    stmt = (
        select(Chunk, Document, distance.label("distance"))
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.org_id == org_id,
            Document.org_id == org_id,
            Document.status == "ready",
        )
        .order_by(distance)
        .limit(top_k)
    )
    if document_ids is not None:
        stmt = stmt.where(Document.id.in_(document_ids))
    # With tenant filters an HNSW scan could return fewer than top_k rows; pgvector >= 0.8
    # iterative scans keep scanning until enough rows pass the WHERE clause.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    rows = (await session.execute(stmt)).all()
    return [
        RetrievedChunk(
            chunk_id=chunk.id,
            document_id=doc.id,
            document_title=doc.title,
            document_meta=dict(doc.meta or {}),
            source=doc.source,
            source_ref=doc.source_ref,
            section_path=chunk.section_path,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            score=1.0 - float(dist),
        )
        for chunk, doc, dist in rows
    ]


def preferred_scope(query: str) -> StatementScope:
    """``separate`` when the question names the separate statements ("별도", "개별"), else
    ``consolidated`` (the primary statements in Korean filings)."""
    return SEPARATE if any(term in query for term in SEPARATE_QUERY_TERMS) else CONSOLIDATED


def prefer_statement_scope(
    candidates: list[RetrievedChunk], *, query: str, top_k: int
) -> list[RetrievedChunk]:
    """Final ``top_k`` from similarity-ordered ``candidates``.

    A chunk of the non-preferred scope whose note has a preferred-scope counterpart among the
    candidates of the same document (``dart.sections.note_key``) moves behind every other
    candidate; everything else keeps its similarity order. Demoted chunks are not removed:
    they fill whatever slots remain.
    """
    preferred = preferred_scope(query)
    other = SEPARATE if preferred == CONSOLIDATED else CONSOLIDATED
    preferred_notes = {
        (c.document_id, key)
        for c in candidates
        if scope_of(c.section_path) == preferred and (key := note_key(c.section_path))
    }
    kept: list[RetrievedChunk] = []
    demoted: list[RetrievedChunk] = []
    for c in candidates:
        has_counterpart = (
            scope_of(c.section_path) == other
            and (c.document_id, note_key(c.section_path)) in preferred_notes
        )
        (demoted if has_counterpart else kept).append(c)
    return (kept + demoted)[:top_k]
