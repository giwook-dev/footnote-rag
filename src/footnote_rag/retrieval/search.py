"""pgvector cosine search, always scoped to one organization's ready documents."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.db.models import Chunk, Document


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
