"""Application settings.

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
    llm_provider: ProviderChoice = "auto"
    embedder: ProviderChoice = "auto"
    dart_provider: ProviderChoice = "auto"

    # --- Claude ---
    claude_model: str = "claude-sonnet-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    claude_max_tokens: int = 16000
    claude_timeout_seconds: float = 120.0
    llm_prices: dict[str, ModelPrice] = DEFAULT_LLM_PRICES

    # --- embeddings ---
    embedding_model: str = "voyage-4"
    embedding_dim: int = 1024
    embed_batch_max_texts: int = 1000
    # Conservative per-request token budget. Tokens are estimated as one per character,
    # which over-counts for Korean, so real requests stay well below Voyage's limit.
    embed_batch_max_tokens: int = 100_000

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
    def llm_mode(self) -> Literal["fake", "real"]:
        return self._resolve(self.llm_provider, self.anthropic_api_key)

    @property
    def embedder_mode(self) -> Literal["fake", "real"]:
        return self._resolve(self.embedder, self.voyage_api_key)

    @property
    def dart_mode(self) -> Literal["fake", "real"]:
        return self._resolve(self.dart_provider, self.opendart_api_key)

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
