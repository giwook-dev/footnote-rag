"""LLM 클라이언트(Protocol 뒤의 Claude 실제 구현과 Fake).

역할: Claude Messages API 요청을 만들고, 오류를 502/503으로 바꾸고, 응답을 ``LLMResponse``(dict 블록)로 정규화한다.
로컬 모델(Ollama) 구현은 ``answering/local_llm.py``의 ``OllamaClient``다.
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


CITATION_API = "api_citation"  # Claude Citations API (search_result blocks)
CITATION_MODEL_REFERENCE = "model_reference"  # local model picked source numbers itself


class LLMError(Exception):
    """Upstream LLM failure mapped to an HTTP status for the client (502 / 503 / 504)."""

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
    # How the citations in ``content`` were produced; copied onto every footnote.
    citation_method: str = CITATION_API
    # Source numbers the model returned that were outside 1..k and were dropped (local only).
    dropped_source_refs: int = 0
    # Extra review-gate reasons decided by the client (e.g. "truncated" for local models).
    review_flags: list[str] = field(default_factory=list)
    # The model explicitly answered "no evidence" (local: ``insufficient: true``). Such an
    # answer still goes to review (``no_citations``) but is not escalated for that reason.
    abstained: bool = False


class LLMClient(Protocol):
    async def answer(self, *, system: str, messages: list[dict[str, Any]]) -> LLMResponse: ...


class ClaudeClient:
    provider = "real"
    citation_method = CITATION_API

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

    async def status(self) -> dict[str, Any]:
        # No probe call: it would spend tokens (and the key) just to render an admin card.
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": None,
            "connected": None,
            "model_available": None,
            "detail": "Claude API는 상태 확인용 호출을 하지 않습니다(호출 비용 방지).",
        }

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
    provider = "fake"
    citation_method = CITATION_API

    def __init__(self, *, stop_reason: str = "end_turn", model: str = "fake-llm") -> None:
        self.stop_reason = stop_reason
        self.model = model
        self.calls: list[dict[str, Any]] = []

    async def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": None,
            "connected": True,
            "model_available": True,
            "detail": "Fake 모드: 외부 호출 없이 합성 답변을 만듭니다.",
        }

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
