"""문서 임베딩 캐시(``embedding_cache`` 테이블)와 캐시를 거치는 임베더 래퍼.

역할: 청크 텍스트의 키(``sha256(model|dim|input_type|text)``)로 캐시를 일괄 조회하고, 없는 텍스트만 실제
임베더로 보낸다. 배치가 성공할 때마다 그 결과를 짧은 별도 트랜잭션으로 바로 저장·커밋한다
(``ON CONFLICT DO NOTHING``). 그래서 작업이 중간에 실패하거나 재처리해도 이미 낸 비용을 다시 쓰지 않는다.
질문 임베딩(``query``)은 캐시하지 않는다.
주요 요소: ``cache_key``, ``EmbeddingCacheStore``(Protocol), ``PgEmbeddingCache``, ``CachedEmbedder``,
``CachedEmbeddingResult``.
호출 관계: ``jobs.handlers._embed_and_store``가 ``CachedEmbedder(ctx.embedder, PgEmbeddingCache(...))``로 쓴다.
학습 문서: docs/study/05-rag-retrieval.md, docs/adr/0007-embedding-rate-limit-and-cache.md

Document-embedding cache shared by every organization (it holds no text and no org id).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from footnote_rag.db.models import EmbeddingCache
from footnote_rag.retrieval.embedder import (
    BatchCallback,
    Embedder,
    EmbeddingError,
    EmbeddingResult,
)

DOCUMENT_INPUT_TYPE = "document"
_LOOKUP_CHUNK = 1000

# (done, total) in chunk units, reported after the cache lookup and after every batch.
ProgressCallback = Callable[[int, int], Awaitable[None]]


def cache_key(model: str, dim: int, input_type: str, text: str) -> str:
    return hashlib.sha256(f"{model}|{dim}|{input_type}|{text}".encode()).hexdigest()


class EmbeddingCacheStore(Protocol):
    async def get_many(self, keys: Sequence[str]) -> dict[str, list[float]]: ...

    async def put_many(
        self, model: str, dim: int, items: Sequence[tuple[str, list[float]]]
    ) -> None:
        """Persist and COMMIT immediately; existing keys are left untouched."""
        ...


def _as_floats(value: Any) -> list[float]:
    raw = value.tolist() if hasattr(value, "tolist") else list(value)
    return [float(v) for v in raw]


class PgEmbeddingCache:
    """``embedding_cache`` table. Each call uses its own short transaction."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def get_many(self, keys: Sequence[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        unique = list(dict.fromkeys(keys))
        if not unique:
            return found
        async with self._sessionmaker() as session:
            for start in range(0, len(unique), _LOOKUP_CHUNK):
                part = unique[start : start + _LOOKUP_CHUNK]
                rows = await session.execute(
                    select(EmbeddingCache.key, EmbeddingCache.embedding).where(
                        EmbeddingCache.key.in_(part)
                    )
                )
                for key, embedding in rows:
                    found[key] = _as_floats(embedding)
        return found

    async def put_many(
        self, model: str, dim: int, items: Sequence[tuple[str, list[float]]]
    ) -> None:
        if not items:
            return
        rows = [{"key": k, "model": model, "dim": dim, "embedding": v} for k, v in items]
        async with self._sessionmaker() as session:
            await session.execute(
                pg_insert(EmbeddingCache)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["key"])
            )
            await session.commit()


@dataclass
class CachedEmbeddingResult(EmbeddingResult):
    cache_hits: int = 0  # chunks served from the cache
    cache_misses: int = 0  # chunks that needed the provider


class CachedEmbedder:
    """Wrap an :class:`Embedder` with the document cache. Queries pass straight through."""

    def __init__(self, embedder: Embedder, cache: EmbeddingCacheStore) -> None:
        self.inner = embedder
        self.cache = cache
        self.model = embedder.model
        self.dim = embedder.dim

    def _key(self, text: str) -> str:
        return cache_key(self.model, self.dim, DOCUMENT_INPUT_TYPE, text)

    async def embed_documents(
        self,
        texts: list[str],
        on_batch: BatchCallback | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> CachedEmbeddingResult:
        keys = [self._key(t) for t in texts]
        total = len(texts)
        vectors_by_key = await self.cache.get_many(keys)
        positions = Counter(keys)  # duplicate chunk texts share one provider call
        misses = sum(1 for k in keys if k not in vectors_by_key)
        to_send: dict[str, str] = {}
        for key, text in zip(keys, texts, strict=True):
            if key not in vectors_by_key and key not in to_send:
                to_send[key] = text

        done = total - misses
        if on_progress is not None:
            await on_progress(done, total)

        async def store(batch: list[str], result: EmbeddingResult) -> None:
            nonlocal done
            if len(result.vectors) != len(batch):
                raise EmbeddingError("임베딩 개수 불일치", retryable=True)
            items = [
                (self._key(t), v)
                for t, v in zip(batch, result.vectors, strict=True)
                if self._key(t) not in vectors_by_key
            ]
            await self.cache.put_many(self.model, self.dim, items)
            for key, vector in items:
                vectors_by_key[key] = vector
                done += positions[key]
            if on_batch is not None:
                await on_batch(batch, result)
            if on_progress is not None:
                await on_progress(done, total)

        tokens = 0
        if to_send:
            pending = list(to_send.values())
            result = await self.inner.embed_documents(pending, on_batch=store)
            tokens = result.total_tokens
            # An embedder that ignores ``on_batch`` still gets its result cached once.
            if any(k not in vectors_by_key for k in to_send):
                await store(pending, result)

        return CachedEmbeddingResult(
            vectors=[vectors_by_key[k] for k in keys],
            total_tokens=tokens,
            cache_hits=total - misses,
            cache_misses=misses,
        )

    async def embed_query(self, text: str) -> EmbeddingResult:
        # Questions are never cached: they are short, unique and on the interactive path.
        return await self.inner.embed_query(text)
