"""외부 의존성(OpenDART, Voyage, Claude)의 실제/Fake 구현을 고르는 팩토리.

역할: 설정의 ``dart_mode``·``embedder_mode``·``llm_mode``를 보고 구현체를 만든다. real인데 키가 없으면
``MissingKeyError``.
주요 요소: ``make_dart_client``, ``make_embedder``, ``make_llm``.
호출 관계: ``main.lifespan``, ``jobs.worker._main``, ``cli``, ``evals.run``이 호출한다.
학습 문서: docs/study/09-testing-and-ops.md (실제/Fake 제공자 선택 구조)

Factories that pick the real or fake implementation of each external dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from footnote_rag.config import Settings, get_settings

if TYPE_CHECKING:
    from footnote_rag.answering.llm import LLMClient
    from footnote_rag.dart.client import DartClient
    from footnote_rag.retrieval.embedder import Embedder


class MissingKeyError(RuntimeError):
    pass


def _require(value, name: str) -> str:
    if value is None or not value.get_secret_value():
        raise MissingKeyError(f"{name} is required when the provider is 'real'")
    return value.get_secret_value()


def make_dart_client(settings: Settings | None = None) -> DartClient:
    from footnote_rag.dart.client import FakeDartClient, HttpDartClient

    s = settings or get_settings()
    if s.dart_mode == "fake":
        return FakeDartClient()
    return HttpDartClient(
        _require(s.opendart_api_key, "OPENDART_API_KEY"),
        base_url=s.dart_base_url,
        timeout=s.dart_timeout_seconds,
    )


def make_embedder(settings: Settings | None = None) -> Embedder:
    from footnote_rag.retrieval.embedder import FakeEmbedder, RateLimiter, VoyageEmbedder

    s = settings or get_settings()
    if s.embedder_mode == "fake":
        return FakeEmbedder(dim=s.embedding_dim)
    return VoyageEmbedder(
        api_key=_require(s.voyage_api_key, "VOYAGE_API_KEY"),
        model=s.embedding_model,
        dim=s.embedding_dim,
        max_texts=s.embed_batch_max_texts,
        max_tokens=s.embed_effective_batch_max_tokens,
        # One limiter per process: with N workers, divide EMBED_RPM_LIMIT/EMBED_TPM_LIMIT by N.
        rate_limiter=RateLimiter(rpm=s.embed_rpm_limit, tpm=s.embed_tpm_limit),
        max_retries=s.embed_batch_max_retries,
        retry_base_seconds=s.embed_retry_base_seconds,
        retry_max_seconds=s.embed_retry_max_seconds,
    )


def make_llm(settings: Settings | None = None) -> LLMClient:
    from footnote_rag.answering.llm import ClaudeClient, FakeLLM

    s = settings or get_settings()
    if s.llm_mode == "fake":
        return FakeLLM()
    return ClaudeClient(
        api_key=_require(s.anthropic_api_key, "ANTHROPIC_API_KEY"),
        model=s.claude_model,
        effort=s.claude_effort,
        max_tokens=s.claude_max_tokens,
        timeout=s.claude_timeout_seconds,
    )
