"""임베딩 제공자(Protocol 뒤의 Voyage 실제 구현과 해시 기반 Fake)와 클라이언트 측 속도 제한.

역할: 문서 청크는 ``input_type="document"``, 질문은 ``"query"``로 임베딩하고, 개수·토큰 한도로 배치를 나눈다.
문서 배치는 보내기 전에 ``RateLimiter``(60초 슬라이딩 윈도우, RPM·TPM)를 통과하고, 429·일시 오류가 나면
**그 배치만** 지수 백오프로 재시도한다. 재시도를 다 쓰면 ``EmbeddingError(retryable=True)``로 작업 단위 재시도에 넘긴다.
주요 요소: ``Embedder``(Protocol), ``VoyageEmbedder``, ``FakeEmbedder``, ``RateLimiter``, ``iter_batches``,
``EmbeddingError``(retryable), ``BatchCallback``.
호출 관계: ``providers.make_embedder``가 만들고 ``jobs.handlers``(문서, ``retrieval.embedding_cache.CachedEmbedder``로
감쌈)와 ``answering.service.retrieve``(질문)가 쓴다.
학습 문서: docs/study/05-rag-retrieval.md, docs/adr/0007-embedding-rate-limit-and-cache.md

Embedding providers behind a Protocol.

Documents are embedded with ``input_type="document"``, questions with ``input_type="query"``.
Voyage limits each request to 1,000 texts and a per-model token budget, so documents are
sent in batches bounded by both ``max_texts`` and an estimated token count.

Each document batch first waits on an in-process :class:`RateLimiter` and is retried on its
own (exponential backoff + jitter) on 429 / transient errors, so one rate-limited batch no
longer throws away the batches that already succeeded.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from footnote_rag.observability import get_logger

log = get_logger(__name__)

REDUCED_LIMITS_HINT = (
    "Voyage 결제 수단 미등록: 3 RPM/10K TPM. "
    "EMBED_RPM_LIMIT/EMBED_TPM_LIMIT를 설정하거나 결제 수단을 등록하세요"
)


class EmbeddingError(Exception):
    """Embedding failed. ``str(exc)`` is a Korean, human-readable reason (no provider text).

    ``kind`` is ``"rate_limited"``, ``"temporary"`` or ``"rejected"``.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        kind: str = "temporary",
        payment_method_missing: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind
        self.payment_method_missing = payment_method_missing


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    total_tokens: int


# Called after every successful document batch with the batch texts and their result.
BatchCallback = Callable[[list[str], EmbeddingResult], Awaitable[None]]


class Embedder(Protocol):
    model: str
    dim: int

    async def embed_documents(
        self, texts: list[str], on_batch: BatchCallback | None = None
    ) -> EmbeddingResult: ...

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


Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]


class RateLimiter:
    """Requests-per-minute and tokens-per-minute limit over a sliding window.

    Valid inside ONE process only: with several workers, divide the provider limit between
    them (e.g. 2 workers on a 3 RPM account -> ``EMBED_RPM_LIMIT=1``). ``clock`` and ``sleep``
    are injectable so tests never actually wait.
    """

    def __init__(
        self,
        *,
        rpm: int | None = None,
        tpm: int | None = None,
        window_seconds: float = 60.0,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.rpm = rpm if rpm and rpm > 0 else None
        self.tpm = tpm if tpm and tpm > 0 else None
        self.window = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._events: deque[tuple[float, int]] = deque()  # (sent_at, tokens)
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.rpm is not None or self.tpm is not None

    def _prune(self, now: float) -> None:
        while self._events and self._events[0][0] + self.window <= now:
            self._events.popleft()

    def delay_for(self, tokens: int) -> float:
        """Seconds to wait before a request of ``tokens`` fits in the window (0 = send now)."""
        now = self._clock()
        self._prune(now)
        wait = 0.0
        events = self._events
        if self.rpm is not None and len(events) >= self.rpm:
            oldest_to_expire = events[len(events) - self.rpm][0]
            wait = max(wait, oldest_to_expire + self.window - now)
        if self.tpm is not None and events:
            excess = sum(t for _, t in events) + tokens - self.tpm
            if excess > 0:
                freed = 0
                for sent_at, t in events:
                    freed += t
                    if freed >= excess:
                        wait = max(wait, sent_at + self.window - now)
                        break
                else:
                    # A single request larger than the TPM limit goes alone in an empty window.
                    wait = max(wait, events[-1][0] + self.window - now)
        return max(0.0, wait)

    async def acquire(self, tokens: int) -> float:
        """Wait until the request fits, record it, and return the seconds waited."""
        if not self.enabled:
            return 0.0
        waited = 0.0
        async with self._lock:
            while (delay := self.delay_for(tokens)) > 0:
                await self._sleep(delay)
                waited += delay
            self._events.append((self._clock(), tokens))
        return waited


_reduced_limits_warned = False


def _warn_reduced_limits_once() -> None:
    """Log the "no payment method" hint once per process. Never logs the key or the raw body."""
    global _reduced_limits_warned
    if _reduced_limits_warned:
        return
    _reduced_limits_warned = True
    log.warning("embedding.provider_reduced_limits", hint=REDUCED_LIMITS_HINT)


def reset_reduced_limits_warning() -> None:
    """Test helper: allow the once-per-process warning to fire again."""
    global _reduced_limits_warned
    _reduced_limits_warned = False


def _is_payment_method_message(exc: BaseException) -> bool:
    message = getattr(exc, "user_message", None) or str(exc)
    return "payment method" in str(message).lower()


def retry_delay(attempt: int, base_seconds: float, max_seconds: float, rand: float) -> float:
    """``min(base × 2^attempt, max)`` plus up to 10% jitter (``rand`` in [0, 1))."""
    delay = min(base_seconds * (2**attempt), max_seconds)
    return delay + delay * 0.1 * rand


class VoyageEmbedder:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voyage-4",
        dim: int = 1024,
        max_texts: int = 1000,
        max_tokens: int = 100_000,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 6,
        retry_base_seconds: float = 20.0,
        retry_max_seconds: float = 120.0,
        sleep: Sleep = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        client: Any | None = None,
    ) -> None:
        import voyageai

        if client is None:
            # Document batches are retried here (rate limit aware), so the SDK must not retry
            # them on its own: hidden retries would spend the RPM budget behind our back.
            self._client = voyageai.AsyncClient(api_key=api_key, max_retries=0, timeout=60)
            # Questions keep the SDK's short retries (interactive path, unchanged behaviour).
            self._query_client = voyageai.AsyncClient(api_key=api_key, max_retries=2, timeout=60)
        else:
            self._client = self._query_client = client
        self.model = model
        self.dim = dim
        self._max_texts = min(max_texts, 1000)
        self._max_tokens = max_tokens
        self._limiter = rate_limiter or RateLimiter()
        self._max_retries = max(0, max_retries)
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._sleep = sleep
        self._rand = rand

    async def _embed(self, client: Any, texts: list[str], input_type: str) -> EmbeddingResult:
        import voyageai.error as verr

        try:
            result = await client.embed(
                texts, model=self.model, input_type=input_type, output_dimension=self.dim
            )
        except verr.RateLimitError as exc:
            payment = _is_payment_method_message(exc)
            if payment:
                _warn_reduced_limits_once()
            reason = "결제 수단 미등록 가능성" if payment else "RateLimitError"
            raise EmbeddingError(
                f"임베딩 제공자 요청 한도 초과({reason})",
                retryable=True,
                kind="rate_limited",
                payment_method_missing=payment,
            ) from None
        except (
            verr.ServiceUnavailableError,
            verr.ServerError,
            verr.APIConnectionError,
            verr.Timeout,
            verr.TryAgain,
        ) as exc:
            raise EmbeddingError(
                f"임베딩 제공자 일시 오류({type(exc).__name__})", retryable=True
            ) from None
        except verr.VoyageError as exc:
            raise EmbeddingError(
                f"임베딩 요청 거부({type(exc).__name__})", retryable=False, kind="rejected"
            ) from None
        return EmbeddingResult(
            vectors=[list(v) for v in result.embeddings], total_tokens=int(result.total_tokens)
        )

    async def _embed_batch(self, batch: list[str]) -> EmbeddingResult:
        """Send one document batch, retrying only this batch on retryable errors."""
        tokens = sum(estimate_tokens(t) for t in batch)
        attempt = 0
        while True:
            await self._limiter.acquire(tokens)
            try:
                return await self._embed(self._client, batch, "document")
            except EmbeddingError as exc:
                if not exc.retryable:
                    raise
                if attempt >= self._max_retries:
                    raise EmbeddingError(
                        f"{exc}: 배치 재시도 {self._max_retries}회 소진",
                        retryable=True,
                        kind=exc.kind,
                        payment_method_missing=exc.payment_method_missing,
                    ) from None
                delay = retry_delay(attempt, self._retry_base, self._retry_max, self._rand())
                attempt += 1
                log.warning(
                    "embedding.batch_retry",
                    attempt=attempt,
                    max_retries=self._max_retries,
                    delay_seconds=round(delay, 1),
                    kind=exc.kind,
                    texts=len(batch),
                )
                await self._sleep(delay)

    async def embed_documents(
        self, texts: list[str], on_batch: BatchCallback | None = None
    ) -> EmbeddingResult:
        vectors: list[list[float]] = []
        total = 0
        for batch in iter_batches(texts, self._max_texts, self._max_tokens):
            r = await self._embed_batch(batch)
            vectors.extend(r.vectors)
            total += r.total_tokens
            if on_batch is not None:
                await on_batch(batch, r)
        return EmbeddingResult(vectors=vectors, total_tokens=total)

    async def embed_query(self, text: str) -> EmbeddingResult:
        return await self._embed(self._query_client, [text], "query")


_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


class FakeEmbedder:
    """Deterministic, network-free embedding.

    Feature hashing over words and character bigrams, L2-normalised. Similar texts get
    similar vectors, so retrieval behaves sensibly in fake mode and in tests.
    ``batch_size`` splits document calls like a real provider (default: one batch).
    """

    def __init__(
        self, dim: int = 1024, model: str = "fake-hash-embedding", batch_size: int | None = None
    ) -> None:
        self.dim = dim
        self.model = model
        self.batch_size = batch_size
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

    async def embed_documents(
        self, texts: list[str], on_batch: BatchCallback | None = None
    ) -> EmbeddingResult:
        size = self.batch_size or max(1, len(texts))
        vectors: list[list[float]] = []
        total = 0
        for start in range(0, len(texts), size):
            batch = texts[start : start + size]
            self.calls.append(("document", len(batch)))
            r = EmbeddingResult(
                vectors=[self._vector(t) for t in batch],
                total_tokens=sum(estimate_tokens(t) for t in batch),
            )
            vectors.extend(r.vectors)
            total += r.total_tokens
            if on_batch is not None:
                await on_batch(batch, r)
        return EmbeddingResult(vectors=vectors, total_tokens=total)

    async def embed_query(self, text: str) -> EmbeddingResult:
        self.calls.append(("query", 1))
        return EmbeddingResult(vectors=[self._vector(text)], total_tokens=estimate_tokens(text))
