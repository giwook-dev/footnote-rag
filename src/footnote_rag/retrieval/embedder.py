"""Embedding providers behind a Protocol.

Documents are embedded with ``input_type="document"``, questions with ``input_type="query"``.
Voyage limits each request to 1,000 texts and a per-model token budget, so documents are
sent in batches bounded by both ``max_texts`` and an estimated token count.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


class EmbeddingError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    total_tokens: int


class Embedder(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult: ...

    async def embed_query(self, text: str) -> EmbeddingResult: ...


def estimate_tokens(text: str) -> int:
    # One token per character over-estimates for Korean and English alike: safe for limits.
    return max(1, len(text))


def iter_batches(texts: list[str], max_texts: int, max_tokens: int) -> Iterator[list[str]]:
    batch: list[str] = []
    tokens = 0
    for text in texts:
        t = estimate_tokens(text)
        if batch and (len(batch) >= max_texts or tokens + t > max_tokens):
            yield batch
            batch, tokens = [], 0
        batch.append(text)
        tokens += t
    if batch:
        yield batch


class VoyageEmbedder:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voyage-4",
        dim: int = 1024,
        max_texts: int = 1000,
        max_tokens: int = 100_000,
    ) -> None:
        import voyageai

        self._client = voyageai.AsyncClient(api_key=api_key, max_retries=2, timeout=60)
        self.model = model
        self.dim = dim
        self._max_texts = min(max_texts, 1000)
        self._max_tokens = max_tokens

    async def _embed(self, texts: list[str], input_type: str) -> EmbeddingResult:
        import voyageai.error as verr

        try:
            result = await self._client.embed(
                texts, model=self.model, input_type=input_type, output_dimension=self.dim
            )
        except (
            verr.RateLimitError,
            verr.ServiceUnavailableError,
            verr.ServerError,
            verr.APIConnectionError,
            verr.Timeout,
            verr.TryAgain,
        ) as exc:
            raise EmbeddingError(
                f"voyage temporary failure: {type(exc).__name__}", retryable=True
            ) from None
        except verr.VoyageError as exc:
            raise EmbeddingError(
                f"voyage request failed: {type(exc).__name__}", retryable=False
            ) from None
        return EmbeddingResult(
            vectors=[list(v) for v in result.embeddings], total_tokens=int(result.total_tokens)
        )

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult:
        vectors: list[list[float]] = []
        total = 0
        for batch in iter_batches(texts, self._max_texts, self._max_tokens):
            r = await self._embed(batch, "document")
            vectors.extend(r.vectors)
            total += r.total_tokens
        return EmbeddingResult(vectors=vectors, total_tokens=total)

    async def embed_query(self, text: str) -> EmbeddingResult:
        return await self._embed([text], "query")


_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


class FakeEmbedder:
    """Deterministic, network-free embedding.

    Feature hashing over words and character bigrams, L2-normalised. Similar texts get
    similar vectors, so retrieval behaves sensibly in fake mode and in tests.
    """

    def __init__(self, dim: int = 1024, model: str = "fake-hash-embedding") -> None:
        self.dim = dim
        self.model = model
        self.calls: list[tuple[str, int]] = []

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        features: list[str] = []
        for word in _TOKEN_RE.findall(text.lower()):
            features.append("w:" + word)
            features.extend("b:" + word[i : i + 2] for i in range(len(word) - 1))
        if not features:
            features = ["empty"]
        for f in features:
            h = hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(("document", len(texts)))
        return EmbeddingResult(
            vectors=[self._vector(t) for t in texts],
            total_tokens=sum(estimate_tokens(t) for t in texts),
        )

    async def embed_query(self, text: str) -> EmbeddingResult:
        self.calls.append(("query", 1))
        return EmbeddingResult(vectors=[self._vector(text)], total_tokens=estimate_tokens(text))
