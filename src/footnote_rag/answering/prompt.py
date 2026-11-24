"""Fixed system prompt and request construction with ``search_result`` blocks."""

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
