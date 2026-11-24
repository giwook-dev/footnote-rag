"""Factories that pick the real or fake implementation of each external dependency."""

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
    from footnote_rag.retrieval.embedder import FakeEmbedder, VoyageEmbedder

    s = settings or get_settings()
    if s.embedder_mode == "fake":
        return FakeEmbedder(dim=s.embedding_dim)
    return VoyageEmbedder(
        api_key=_require(s.voyage_api_key, "VOYAGE_API_KEY"),
        model=s.embedding_model,
        dim=s.embedding_dim,
        max_texts=s.embed_batch_max_texts,
        max_tokens=s.embed_batch_max_tokens,
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
