"""LLM 클라이언트(Protocol 뒤의 Claude 실제 구현과 Fake).

역할: Claude Messages API 요청을 만들고, 오류를 502/503으로 바꾸고, 응답을 ``LLMResponse``(dict 블록)로 정규화한다.
주요 요소: ``LLMClient``(Protocol), ``ClaudeClient``(``request_params``, ``answer``), ``FakeLLM``, ``LLMError``.
호출 관계: ``providers.make_llm``이 만들고 ``answering.service.ask_question``이 ``answer``를 호출한다.
학습 문서: docs/study/06-claude-citations.md

LLM clients behind a Protocol.

``ClaudeClient`` uses the official ``anthropic`` SDK only:

* model ``CLAUDE_MODEL`` (default ``claude-sonnet-5-5``), non-streaming, ``max_tokens=16000``
* ``thinking={"type": "adaptive"}`` and an explicit ``output_config={"effort": ...}``
* server-side refusal fallback: ``client.beta.messages.create`` with
  ``betas=["server-side-fallback-2026-07-01"]`` and ``fallbacks="default"``
* never sends temperature/top_p/top_k, budget_tokens, disabled thinking, assistant
  prefill, forced tool_choice or ``output_config.format`` (incompatible with citations)

Responses are normalised into ``LLMResponse`` whose ``content`` is a list of plain dicts in
the Messages API wire shape, so footnote conversion is provider-agnostic and testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from footnote_rag.observability import get_logger

log = get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
NO_EVIDENCE_TEXT = "제공된 문서에서 근거를 찾지 못했습니다."


class LLMError(Exception):
    """Upstream LLM failure mapped to an HTTP status for the client (502 / 503)."""

    def __init__(self, status_code: int, public_message: str) -> None:
        super().__init__(public_message)
        self.status_code = status_code
        self.public_message = public_message


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class LLMResponse:
    model: str
    stop_reason: str | None
    content: list[dict[str, Any]] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0


class LLMClient(Protocol):
    async def answer(self, *, system: str, messages: list[dict[str, Any]]) -> LLMResponse: ...


class ClaudeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-sonnet-5-5",
        effort: str = "medium",
        max_tokens: int = 16000,
        timeout: float = 120.0,
        client: Any | None = None,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self._client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def request_params(self, *, system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }

    async def answer(self, *, system: str, messages: list[dict[str, Any]]) -> LLMResponse:
        import anthropic

        params = self.request_params(system=system, messages=messages)
        started = time.perf_counter()
        try:
            resp = await self._client.beta.messages.create(**params)
        except anthropic.RateLimitError as exc:
            log.warning("llm.rate_limited", status=exc.status_code, error=str(exc)[:500])
            raise LLMError(503, "LLM rate limit reached, retry later") from None
        except anthropic.APIStatusError as exc:
            log.error("llm.api_error", status=exc.status_code, error=str(exc)[:1000])
            if exc.status_code >= 500:
                raise LLMError(503, "LLM service unavailable") from None
            raise LLMError(502, "LLM request failed") from None
        except anthropic.APIConnectionError as exc:
            log.error("llm.connection_error", error=type(exc).__name__)
            raise LLMError(503, "LLM service unreachable") from None
        latency_ms = int((time.perf_counter() - started) * 1000)

        usage = LLMUsage(
            input_tokens=int(resp.usage.input_tokens or 0),
            output_tokens=int(resp.usage.output_tokens or 0),
            cache_read_input_tokens=int(resp.usage.cache_read_input_tokens or 0),
            cache_creation_input_tokens=int(resp.usage.cache_creation_input_tokens or 0),
        )
        # Check stop_reason BEFORE reading content: a refusal carries no usable answer.
        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            log.warning("llm.refusal", model=resp.model, category=category)
            return LLMResponse(resp.model, "refusal", [], usage, latency_ms)
        content = [block.model_dump(mode="json", exclude_none=True) for block in resp.content]
        return LLMResponse(resp.model, resp.stop_reason, content, usage, latency_ms)


class FakeLLM:
    """Network-free stand-in that mimics a Claude response with search_result citations.

    Cites the first two search results. With no search results it answers "근거 없음".
    ``stop_reason`` can be forced (e.g. "refusal") for tests.
    """

    model = "fake-llm"

    def __init__(self, *, stop_reason: str = "end_turn", model: str = "fake-llm") -> None:
        self.stop_reason = stop_reason
        self.model = model
        self.calls: list[dict[str, Any]] = []

    async def answer(self, *, system: str, messages: list[dict[str, Any]]) -> LLMResponse:
        self.calls.append({"system": system, "messages": messages})
        results = [
            block
            for msg in messages
            if msg.get("role") == "user" and isinstance(msg.get("content"), list)
            for block in msg["content"]
            if block.get("type") == "search_result"
        ]
        prompt_chars = sum(len(b["content"][0]["text"]) for b in results) + len(system)
        usage = LLMUsage(input_tokens=max(1, prompt_chars // 2), output_tokens=60)
        if self.stop_reason == "refusal":
            return LLMResponse(self.model, "refusal", [], usage, 5)
        if not results:
            content = [{"type": "text", "text": NO_EVIDENCE_TEXT}]
            return LLMResponse(self.model, self.stop_reason, content, usage, 5)
        content: list[dict[str, Any]] = [{"type": "text", "text": "검색된 문서에 따르면 "}]
        for index, block in enumerate(results[:2]):
            cited = block["content"][0]["text"]
            sentence = cited.splitlines()[0][:120].rstrip()
            content.append(
                {
                    "type": "text",
                    "text": f"{sentence}",
                    "citations": [
                        {
                            "type": "search_result_location",
                            "cited_text": cited,
                            "source": block["source"],
                            "title": block.get("title"),
                            "search_result_index": index,
                            "start_block_index": 0,
                            "end_block_index": 1,
                        }
                    ],
                }
            )
            content.append({"type": "text", "text": " " if index == 0 else ""})
        return LLMResponse(self.model, self.stop_reason, content, usage, 5)
