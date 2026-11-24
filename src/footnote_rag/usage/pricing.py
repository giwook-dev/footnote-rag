"""Token cost calculation from the configured price table (USD per 1M tokens)."""

from __future__ import annotations

from decimal import Decimal

from footnote_rag.answering.llm import LLMUsage
from footnote_rag.config import ModelPrice
from footnote_rag.observability import get_logger

log = get_logger(__name__)
_MILLION = Decimal(1_000_000)


def compute_cost(model: str, usage: LLMUsage, prices: dict[str, ModelPrice]) -> Decimal | None:
    """Cost in USD, or None (with a warning) when the model is not in the price table."""
    price = prices.get(model)
    if price is None:
        log.warning("usage.unpriced_model", model=model)
        return None
    total = (
        Decimal(usage.input_tokens) * price.input
        + Decimal(usage.output_tokens) * price.output
        + Decimal(usage.cache_read_input_tokens) * price.cache_read
        + Decimal(usage.cache_creation_input_tokens) * price.cache_write
    ) / _MILLION
    return total.quantize(Decimal("0.000001"))
