"""애플리케이션 설정(환경변수 → ``Settings``).

역할: 모든 설정값과 기본값, Claude 가격표(``DEFAULT_LLM_PRICES``), 제공자 모드(auto/fake/real, LLM은 local 추가) 해석.
주요 요소: ``Settings``, ``ModelPrice``, ``load_settings``, ``get_settings``(lru_cache).
호출 관계: 거의 모든 모듈이 ``get_settings()``를 읽는다. ``providers``가 ``*_mode``로 실제/Fake를 고른다.
학습 문서: docs/study/09-testing-and-ops.md (SecretStr, Fake 모드)

Application settings.

Values come from environment variables and (unless disabled) the repository ``.env``
file. API keys are held as ``SecretStr`` so they never appear in ``repr()`` or logs.
Set ``FOOTNOTE_DISABLE_DOTENV=1`` to ignore ``.env`` entirely (the test suite does this).
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderChoice = Literal["auto", "fake", "real"]
# The LLM additionally accepts "local" (Ollama on the host). Never chosen by "auto".
LLMProviderChoice = Literal["auto", "fake", "real", "local"]
LLMMode = Literal["fake", "real", "local"]

# llm_calls.model prefix for local (Ollama) models. Local inference costs nothing per token,
# so usage.pricing prices every model with this prefix at 0 (see compute_cost).
LOCAL_MODEL_PREFIX = "ollama:"


class ModelPrice(BaseModel):
    """USD per 1M tokens."""

    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal


# Source: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-24).
# cache_write is the 5-minute cache write rate.
DEFAULT_LLM_PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-5-5": ModelPrice(
        input=Decimal("2"),
        output=Decimal("10"),
        cache_write=Decimal("2.50"),
        cache_read=Decimal("0.10"),
    ),
    # Server-side fallback target for Sonnet 5.5 ("cyber" / "frontier_llm" declines).
    "claude-sonnet-5": ModelPrice(
        input=Decimal("2"),
        output=Decimal("10"),
        cache_write=Decimal("2.50"),
        cache_read=Decimal("0.20"),
    ),
    "claude-opus-5-5": ModelPrice(
        input=Decimal("4"),
        output=Decimal("20"),
        cache_write=Decimal("5"),
        cache_read=Decimal("0.20"),
    ),
    "claude-opus-5": ModelPrice(
        input=Decimal("5"),
        output=Decimal("25"),
        cache_write=Decimal("6.25"),
        cache_read=Decimal("0.50"),
    ),
    "claude-opus-4-8": ModelPrice(
        input=Decimal("5"),
        output=Decimal("25"),
        cache_write=Decimal("6.25"),
        cache_read=Decimal("0.50"),
    ),
}


DEV_JWT_SECRET = "dev-only-insecure-secret-change-me"  # rejected when APP_ENV=production


class Settings(BaseSettings):
    # env_ignore_empty: `KEY=` in .env means "unset" (fake mode / defaults), never an empty secret.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    app_env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://footnote:footnote@localhost:5432/footnote"

    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # --- provider keys (never logged) ---
    anthropic_api_key: SecretStr | None = None
    voyage_api_key: SecretStr | None = None
    opendart_api_key: SecretStr | None = None

    # --- provider selection: auto = real if the key is set, else fake ---
    llm_provider: LLMProviderChoice = "auto"
    embedder: ProviderChoice = "auto"
    dart_provider: ProviderChoice = "auto"

    # --- Claude ---
    claude_model: str = "claude-sonnet-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    claude_max_tokens: int = 16000
    claude_timeout_seconds: float = 120.0
    llm_prices: dict[str, ModelPrice] = DEFAULT_LLM_PRICES

    # --- local LLM (Ollama on the host, LLM_PROVIDER=local) ---
    local_llm_base_url: str = "http://host.docker.internal:11434"
    local_llm_model: str = "qwen3:14b"
    local_llm_num_ctx: int = 16384
    local_llm_max_tokens: int = 2048
    local_llm_timeout_seconds: float = 180.0
    local_llm_think: bool = False
    local_llm_temperature: float = 0.0

    # --- embeddings ---
    embedding_model: str = "voyage-4"
    embedding_dim: int = 1024
    embed_batch_max_texts: int = 1000
    # Conservative per-request token budget. Tokens are estimated as one per character,
    # which over-counts for Korean, so real requests stay well below Voyage's limit.
    embed_batch_max_tokens: int = 100_000
    # Client-side rate limit (60 s sliding window, per worker process). None = unlimited.
    # Voyage accounts without a payment method get 3 RPM / 10K TPM: use 3 / 9000.
    embed_rpm_limit: int | None = None
    embed_tpm_limit: int | None = None
    # Per-batch retry on 429 / transient errors before falling back to a job-level retry.
    embed_batch_max_retries: int = 6
    embed_retry_base_seconds: float = 20.0
    embed_retry_max_seconds: float = 120.0

    # --- retrieval / answering ---
    top_k: int = 8
    min_retrieval_score: float = 0.35
    review_mode: Literal["on_low_confidence", "always"] = "on_low_confidence"

    # --- chunking ---
    chunk_size: int = 1500
    chunk_overlap: int = 200

    # --- jobs ---
    job_max_attempts: int = 5
    base_backoff_seconds: float = 5.0
    job_lock_timeout_seconds: int = 600
    worker_poll_interval_seconds: float = 1.0

    # --- storage / ingest ---
    data_dir: Path = Path("data")
    upload_max_bytes: int = 5 * 1024 * 1024
    dart_base_url: str = "https://opendart.fss.or.kr/api"
    dart_corp_code_ttl_days: int = 7
    dart_timeout_seconds: float = 60.0

    def _resolve(self, choice: ProviderChoice, key: SecretStr | None) -> Literal["fake", "real"]:
        if choice == "auto":
            return "real" if key is not None and key.get_secret_value() else "fake"
        return choice

    @property
    def llm_mode(self) -> LLMMode:
        if self.llm_provider == "local":
            return "local"
        return self._resolve(self.llm_provider, self.anthropic_api_key)

    @property
    def embedder_mode(self) -> Literal["fake", "real"]:
        return self._resolve(self.embedder, self.voyage_api_key)

    @property
    def dart_mode(self) -> Literal["fake", "real"]:
        return self._resolve(self.dart_provider, self.opendart_api_key)

    @property
    def embed_effective_batch_max_tokens(self) -> int:
        """Per-request token budget: never larger than the TPM limit, if one is set."""
        if self.embed_tpm_limit:
            return min(self.embed_batch_max_tokens, self.embed_tpm_limit)
        return self.embed_batch_max_tokens

    @property
    def dart_dir(self) -> Path:
        return self.data_dir / "dart"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


def load_settings(**overrides) -> Settings:
    if os.environ.get("FOOTNOTE_DISABLE_DOTENV") == "1":
        return Settings(_env_file=None, **overrides)
    return Settings(**overrides)


@lru_cache
def get_settings() -> Settings:
    return load_settings()
