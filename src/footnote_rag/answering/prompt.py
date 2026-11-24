"""고정 시스템 프롬프트와 ``search_result`` 블록 요청 구성(로컬 모델용 번호 프롬프트·JSON 스키마 포함).

역할: 검색 청크마다 ``search_result`` 블록(source, title, content, citations on)을 만들고 끝에 질문을 붙인다.
로컬(Ollama) 경로는 같은 블록을 ``[n] 제목\\n본문`` 형식의 번호 목록으로 바꿔 보낸다.
주요 요소: ``SYSTEM_PROMPT``, ``build_messages``, ``source_id``(``doc:{document_id}#chunk:{chunk_id}``),
``LOCAL_SYSTEM_PROMPT``, ``LOCAL_ANSWER_SCHEMA``, ``build_local_user_prompt``.
호출 관계: ``answering.service``가 호출하고, ``source_id``는 각주 변환 때 청크를 다시 찾는 키가 된다.
학습 문서: docs/study/06-claude-citations.md (프롬프트 설계)

Fixed system prompt and request construction with ``search_result`` blocks.
"""

from __future__ import annotations

from typing import Any

from footnote_rag.retrieval.search import RetrievedChunk

SYSTEM_PROMPT = """당신은 회계 감사보고서와 공시 문서를 근거로 질문에 답하는 분석 보조자입니다.

규칙:
- 사용자 메시지에 포함된 검색 결과만 근거로 답합니다. 검색 결과에 없는 내용은 추측하거나 일반 지식으로 보충하지 않습니다.
- 검색 결과에서 근거를 찾을 수 없으면 정확히 "제공된 문서에서 근거를 찾지 못했습니다."라고만 답합니다.
- 숫자는 원문에 적힌 그대로 옮기고, 원문의 단위(예: 백만원, 천원, %)를 함께 표기합니다.
- 여러 회사를 비교할 때는 회사별로 나누어 서술하고, 각 회사의 주장마다 그 회사 문서의 근거를 인용합니다.
- 답변은 한국어로, 간결한 문장으로 작성합니다."""


# --- local model (Ollama): no Citations API, so sources are numbered in the prompt and the
# model must return JSON matching LOCAL_ANSWER_SCHEMA (enforced by Ollama's ``format``). ---

LOCAL_SYSTEM_PROMPT = """당신은 회계 감사보고서와 공시 문서를 근거로 질문에 답하는 분석 보조자입니다.

규칙:
- 사용자 메시지에 번호([1], [2], ...)를 붙여 제공한 근거만 사용합니다. 근거에 없는 내용은 추측하거나 일반 지식으로 보충하지 않습니다.
- 답변을 문장 단위로 나누고, 문장마다 그 문장을 뒷받침하는 근거 번호를 sources 배열에 넣습니다. 제공된 번호만 씁니다.
- 문장 text 안에는 [1] 같은 번호 표식을 쓰지 않습니다. 번호는 sources에만 넣습니다.
- 제공된 근거에서 답을 찾을 수 없으면 insufficient를 true로 하고 sentences는 빈 배열로 둡니다.
- 숫자는 원문에 적힌 그대로 옮기고, 원문의 단위(예: 백만원, 천원, %)를 함께 표기합니다.
- 여러 회사를 비교할 때는 회사별로 나누어 서술하고, 각 회사에 관한 문장에는 그 회사 문서의 근거 번호를 답니다.
- 답변은 한국어로, 간결한 문장으로 작성합니다.
- 출력은 {"insufficient": true|false, "sentences": [{"text": "문장", "sources": [근거 번호]}]} 형식의 JSON 하나뿐입니다."""

LOCAL_RETRY_NOTE = (
    "직전 응답이 요구한 JSON 형식에 맞지 않았습니다. "
    '{"insufficient": ..., "sentences": [...]} 형식의 JSON 하나만 다시 출력하세요.'
)

LOCAL_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "insufficient": {"type": "boolean"},
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "sources"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["insufficient", "sentences"],
    "additionalProperties": False,
}


def build_local_user_prompt(question: str, sources: list[tuple[str, str]]) -> str:
    """``[n] {문서 제목} · {section_path}\\n{chunk text}`` per source, then the question.

    ``sources`` holds ``(title, text)`` pairs where title is already ``문서 제목 · section_path``
    (the same title the ``search_result`` blocks carry).
    """
    parts = ["다음은 검색된 근거입니다. 각 근거 앞의 번호를 sources에 사용하세요.", ""]
    for n, (title, text) in enumerate(sources, start=1):
        parts += [f"[{n}] {title}\n{text}", ""]
    parts.append(f"질문: {question}")
    return "\n".join(parts)


def source_id(document_id: Any, chunk_id: Any) -> str:
    return f"doc:{document_id}#chunk:{chunk_id}"


def build_messages(question: str, chunks: list[RetrievedChunk]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "search_result",
            "source": source_id(c.document_id, c.chunk_id),
            "title": f"{c.document_title} · {c.section_path}",
            "content": [{"type": "text", "text": c.text}],
            "citations": {"enabled": True},
        }
        for c in chunks
    ]
    blocks.append({"type": "text", "text": question})
    return [{"role": "user", "content": blocks}]
