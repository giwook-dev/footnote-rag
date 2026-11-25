"""로컬 LLM(호스트의 Ollama) 클라이언트: 번호 붙은 근거 + JSON 스키마 강제 출력 → 서버 측 검증.

역할: ``search_result`` 블록을 ``[n] 제목\\n본문`` 목록으로 바꿔 Ollama ``/api/chat``에 보내고, 문장별 근거 번호
JSON을 검증해 Claude와 같은 모양의 text 블록 + ``search_result_location`` citations로 바꾼다. 그래서 각주 번호
매기기(``footnotes.build_footnotes``)와 신뢰도·검토·감사·사용량 흐름을 그대로 탄다.
주요 요소: ``OllamaClient``(``request_body``, ``answer``, ``status``, ``warm_up``), ``parse_local_answer``,
``salvage_local_answer``, ``to_content_blocks``.
호출 관계: ``providers.make_local_llm``이 만들고(``make_llm``·``make_fallback_llm``·비교 Eval) ``answering.service.ask_question``이
``answer``를 호출한다. ``GET /llm/status``가 ``status``를, 비교 Eval이 ``status``(사전 점검)와 ``warm_up``(예열)을 호출한다.
학습 문서: docs/study/06-claude-citations.md (로컬 모델 경로), docs/adr/0008-local-llm-ollama.md

Ollama-backed ``LLMClient``.

* native ``POST {base}/api/chat`` (not the OpenAI-compatible endpoint, which ignores
  ``num_ctx`` and can silently truncate long prompts), ``stream: false``,
  ``format: LOCAL_ANSWER_SCHEMA``, ``options: {num_ctx, num_predict, temperature}``, ``think``
  (a bool, or a level string such as ``"low"`` for gpt-oss, sent as-is)
* ``warm_up``: ``POST {base}/api/generate`` with an empty prompt loads the model; never raises
* invalid JSON / schema -> one retry; a second failure -> ``LLMError(502)``.
  ``done_reason == "length"`` is not retried: complete sentences are salvaged and the answer is
  flagged ``truncated`` (pending review)
* source numbers outside ``1..k`` are dropped and counted (``dropped_source_refs``)
* ``cited_text`` is approximated with ``select_cited_text``; ``citation_method`` is
  ``model_reference``
* model id in ``llm_calls`` is ``ollama:{model}`` (priced at 0)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from footnote_rag.answering.footnotes import select_cited_text
from footnote_rag.answering.llm import (
    CITATION_MODEL_REFERENCE,
    NO_EVIDENCE_TEXT,
    LLMError,
    LLMResponse,
    LLMUsage,
)
from footnote_rag.answering.prompt import (
    LOCAL_ANSWER_SCHEMA,
    LOCAL_RETRY_NOTE,
    LOCAL_SYSTEM_PROMPT,
    build_local_user_prompt,
)
from footnote_rag.config import LOCAL_MODEL_PREFIX, ThinkSetting
from footnote_rag.observability import get_logger

log = get_logger(__name__)

STATUS_TIMEOUT_SECONDS = 3.0
CONNECT_TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = 2  # first try + one retry on invalid JSON
UNREACHABLE_MESSAGE = "로컬 LLM 서버(Ollama)에 연결할 수 없습니다"
TIMEOUT_MESSAGE = "로컬 LLM 서버(Ollama) 응답 시간이 초과되었습니다"
INVALID_OUTPUT_MESSAGE = "로컬 LLM 응답이 JSON 형식에 맞지 않습니다"
DONE_REASON_TO_STOP_REASON = {"stop": "end_turn", "length": "max_tokens"}
# A model sometimes writes "[1]" into the sentence itself; those markers would collide with the
# footnote numbers we assign, so they are stripped (numbers only count from ``sources``).
_INLINE_MARKER_RE = re.compile(r"\s*\[\d+\]")


def missing_model_message(model: str) -> str:
    return f"모델 {model}이 없습니다: ollama pull {model}"


@dataclass
class LocalSentence:
    text: str
    sources: list[int]


@dataclass
class LocalAnswer:
    insufficient: bool
    sentences: list[LocalSentence] = field(default_factory=list)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sentence_from(obj: Any) -> LocalSentence | None:
    if not isinstance(obj, dict) or set(obj) != {"text", "sources"}:
        return None
    text, sources = obj["text"], obj["sources"]
    if not isinstance(text, str) or not isinstance(sources, list):
        return None
    if not all(_is_int(n) for n in sources):
        return None
    return LocalSentence(text=text, sources=list(sources))


def parse_local_answer(raw: str) -> LocalAnswer | None:
    """Strict parse against ``LOCAL_ANSWER_SCHEMA``; ``None`` when it does not match."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"insufficient", "sentences"}:
        return None
    if not isinstance(data["insufficient"], bool) or not isinstance(data["sentences"], list):
        return None
    sentences = [_sentence_from(s) for s in data["sentences"]]
    if any(s is None for s in sentences):
        return None
    return LocalAnswer(data["insufficient"], [s for s in sentences if s is not None])


def salvage_local_answer(raw: str) -> LocalAnswer:
    """Complete sentence objects from output cut off by ``num_predict`` (``done_reason=length``)."""
    parsed = parse_local_answer(raw)
    if parsed is not None:
        return parsed
    sentences: list[LocalSentence] = []
    key = raw.find('"sentences"')
    start = raw.find("[", key) if key >= 0 else -1
    if start >= 0:
        decoder = json.JSONDecoder()
        pos = start + 1
        while True:
            while pos < len(raw) and raw[pos] in " \t\r\n,":
                pos += 1
            if pos >= len(raw) or raw[pos] != "{":
                break
            try:
                obj, pos = decoder.raw_decode(raw, pos)
            except ValueError:
                break
            sentence = _sentence_from(obj)
            if sentence is not None:
                sentences.append(sentence)
    insufficient = re.search(r'"insufficient"\s*:\s*true', raw) is not None
    return LocalAnswer(insufficient=insufficient, sentences=sentences)


def search_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        block
        for msg in messages
        if msg.get("role") == "user" and isinstance(msg.get("content"), list)
        for block in msg["content"]
        if block.get("type") == "search_result"
    ]


def question_text(messages: list[dict[str, Any]]) -> str:
    texts = [
        str(block.get("text", ""))
        for msg in messages
        if msg.get("role") == "user" and isinstance(msg.get("content"), list)
        for block in msg["content"]
        if block.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t)


def _chunk_text(block: dict[str, Any]) -> str:
    return "\n".join(
        str(part.get("text", ""))
        for part in block.get("content") or []
        if part.get("type") == "text"
    )


@dataclass
class ConvertedAnswer:
    content: list[dict[str, Any]]
    dropped_source_refs: int
    valid_refs: int


def to_content_blocks(answer: LocalAnswer, results: list[dict[str, Any]]) -> ConvertedAnswer:
    """Validated sentences -> Messages-API-shaped text blocks with search_result citations.

    Numbers outside ``1..k`` are dropped (and counted). ``insufficient`` or no valid number at
    all yields the standard "근거 없음" text with no citations (-> ``no_citations`` review).
    """
    k = len(results)
    dropped = 0
    valid = 0
    content: list[dict[str, Any]] = []
    for sentence in answer.sentences:
        indices: list[int] = []
        for n in sentence.sources:
            if 1 <= n <= k:
                if n - 1 not in indices:
                    indices.append(n - 1)
            else:
                dropped += 1
        if answer.insufficient:
            continue
        text = _INLINE_MARKER_RE.sub("", sentence.text).strip()
        if not text:
            continue
        block: dict[str, Any] = {"type": "text", "text": (" " if content else "") + text}
        if indices:
            block["citations"] = [
                {
                    "type": "search_result_location",
                    "source": results[i].get("source"),
                    "title": results[i].get("title"),
                    "cited_text": select_cited_text(_chunk_text(results[i]), text),
                    "search_result_index": i,
                }
                for i in indices
            ]
            valid += len(indices)
        content.append(block)
    if answer.insufficient or valid == 0:
        content = [{"type": "text", "text": NO_EVIDENCE_TEXT}]
    return ConvertedAnswer(content=content, dropped_source_refs=dropped, valid_refs=valid)


class OllamaClient:
    """``LLMClient`` for a local Ollama server. No API key; the ``system`` argument is replaced
    by ``LOCAL_SYSTEM_PROMPT`` because the Claude prompt asks for citations the model cannot
    produce."""

    provider = "local"
    citation_method = CITATION_MODEL_REFERENCE

    def __init__(
        self,
        *,
        base_url: str = "http://host.docker.internal:11434",
        model: str = "qwen3:14b",
        num_ctx: int = 16384,
        max_tokens: int = 2048,
        timeout: float = 180.0,
        think: ThinkSetting = False,
        temperature: float = 0.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.think = think
        self.temperature = temperature
        self._transport = transport

    @property
    def model_id(self) -> str:
        """Value stored in ``llm_calls.model`` (``ollama:`` prefix -> cost 0)."""
        return f"{LOCAL_MODEL_PREFIX}{self.model}"

    def chat_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        results = search_results(messages)
        sources = [(str(b.get("title") or ""), _chunk_text(b)) for b in results]
        return [
            {"role": "system", "content": LOCAL_SYSTEM_PROMPT},
            {"role": "user", "content": build_local_user_prompt(question_text(messages), sources)},
        ]

    def request_body(self, chat: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": chat,
            "stream": False,
            "format": LOCAL_ANSWER_SCHEMA,
            "think": self.think,
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": self.max_tokens,
                "temperature": self.temperature,
            },
        }

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self._transport)

    def _request_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.timeout, connect=min(self.timeout, CONNECT_TIMEOUT_SECONDS))

    async def warm_up(self) -> bool:
        """Load the model into memory (``POST /api/generate`` with an empty prompt) before timing.

        Never raises: a failure is logged as a warning and returns False.
        """
        body = {"model": self.model, "prompt": "", "stream": False}
        started = time.perf_counter()
        try:
            async with self._client(self._request_timeout()) as client:
                resp = await client.post("/api/generate", json=body)
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            log.warning("llm.local_warm_up_failed", model=self.model, error=type(exc).__name__)
            return False
        if resp.status_code >= 400:
            log.warning("llm.local_warm_up_failed", model=self.model, status=resp.status_code)
            return False
        log.info(
            "llm.local_warm_up",
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return True

    async def _chat(self, body: dict[str, Any]) -> dict[str, Any]:
        timeout = self._request_timeout()
        try:
            async with self._client(timeout) as client:
                resp = await client.post("/api/chat", json=body)
        except httpx.ConnectTimeout:
            log.error("llm.local_unreachable", base_url=self.base_url, error="ConnectTimeout")
            raise LLMError(503, UNREACHABLE_MESSAGE) from None
        except httpx.TimeoutException as exc:
            log.error("llm.local_timeout", base_url=self.base_url, error=type(exc).__name__)
            raise LLMError(504, TIMEOUT_MESSAGE) from None
        except httpx.TransportError as exc:
            log.error("llm.local_unreachable", base_url=self.base_url, error=type(exc).__name__)
            raise LLMError(503, UNREACHABLE_MESSAGE) from None
        if resp.status_code == 404:
            log.error("llm.local_model_missing", model=self.model, body=resp.text[:300])
            raise LLMError(503, missing_model_message(self.model))
        if resp.status_code >= 500:
            log.error("llm.local_server_error", status=resp.status_code, body=resp.text[:500])
            raise LLMError(503, "로컬 LLM 서버(Ollama) 오류")
        if resp.status_code >= 400:
            log.error("llm.local_request_failed", status=resp.status_code, body=resp.text[:500])
            raise LLMError(502, "로컬 LLM 요청이 실패했습니다")
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def answer(self, *, system: str, messages: list[dict[str, Any]]) -> LLMResponse:
        del system  # see class docstring
        results = search_results(messages)
        chat = self.chat_messages(messages)
        usage = LLMUsage()
        started = time.perf_counter()
        parsed: LocalAnswer | None = None
        done_reason: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            request_chat = (
                chat if attempt == 1 else [*chat, {"role": "user", "content": LOCAL_RETRY_NOTE}]
            )
            data = await self._chat(self.request_body(request_chat))
            usage.input_tokens += int(data.get("prompt_eval_count") or 0)
            usage.output_tokens += int(data.get("eval_count") or 0)
            done_reason = data.get("done_reason")
            message = data.get("message")
            raw = message.get("content") if isinstance(message, dict) else None
            raw = raw if isinstance(raw, str) else ""
            if done_reason == "length":
                # Retrying would hit the same num_predict limit: keep what is complete.
                parsed = salvage_local_answer(raw)
                break
            parsed = parse_local_answer(raw)
            if parsed is not None:
                break
            log.warning(
                "llm.local_invalid_output",
                model=self.model,
                attempt=attempt,
                done_reason=done_reason,
                chars=len(raw),
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if parsed is None:
            log.error("llm.local_invalid_output_final", model=self.model, attempts=MAX_ATTEMPTS)
            raise LLMError(502, INVALID_OUTPUT_MESSAGE)

        converted = to_content_blocks(parsed, results)
        stop_reason = DONE_REASON_TO_STOP_REASON.get(str(done_reason), done_reason)
        flags = ["truncated"] if stop_reason == "max_tokens" else []
        if converted.dropped_source_refs:
            log.warning(
                "llm.local_dropped_source_refs",
                model=self.model,
                dropped=converted.dropped_source_refs,
                k=len(results),
            )
        log.info(
            "llm.local_answer",
            model=self.model,
            stop_reason=stop_reason,
            insufficient=parsed.insufficient,
            sentences=len(parsed.sentences),
            valid_refs=converted.valid_refs,
            dropped_source_refs=converted.dropped_source_refs,
            latency_ms=latency_ms,
        )
        return LLMResponse(
            model=self.model_id,
            stop_reason=stop_reason,
            content=converted.content,
            usage=usage,
            latency_ms=latency_ms,
            citation_method=CITATION_MODEL_REFERENCE,
            dropped_source_refs=converted.dropped_source_refs,
            review_flags=flags,
        )

    async def status(self) -> dict[str, Any]:
        """Reachability and model presence via ``GET {base}/api/tags`` (3 s timeout)."""
        out: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "connected": False,
            "model_available": None,
            "detail": None,
        }
        try:
            async with self._client(httpx.Timeout(STATUS_TIMEOUT_SECONDS)) as client:
                resp = await client.get("/api/tags")
        except httpx.TimeoutException:
            out["detail"] = f"{TIMEOUT_MESSAGE}({STATUS_TIMEOUT_SECONDS:.0f}초)"
            return out
        except httpx.TransportError:
            out["detail"] = UNREACHABLE_MESSAGE
            return out
        if resp.status_code != 200:
            out["detail"] = f"Ollama /api/tags 응답 상태 {resp.status_code}"
            return out
        out["connected"] = True
        try:
            models = resp.json().get("models") or []
        except (ValueError, AttributeError):
            models = []
        names = {
            str(m.get(key))
            for m in models
            if isinstance(m, dict)
            for key in ("name", "model")
            if m.get(key)
        }
        wanted = {self.model} if ":" in self.model else {self.model, f"{self.model}:latest"}
        out["model_available"] = bool(names & wanted)
        if not out["model_available"]:
            out["detail"] = missing_model_message(self.model)
        return out
