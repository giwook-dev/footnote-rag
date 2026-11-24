# ADR 0002: 근거 표시는 Claude Citations(`search_result` 블록) (프롬프트로 만든 인용 대신)

- 상태: 채택
- 날짜: 2026-11-24

## 맥락

답변의 문장마다 각주 `[n]`을 달고, 각주는 문서 제목·목차 경로·인용문을 보여야 합니다. 감사·회계 도메인에서는 "모델이 그럴듯한 출처를 지어내는" 실패가 특히 치명적입니다.

## 결정

검색된 청크를 user 메시지 안의 `search_result` 블록으로 보내고 모든 블록에 `citations: {"enabled": true}`를 켭니다.

```json
{"type": "search_result",
 "source": "doc:{document_id}#chunk:{chunk_id}",
 "title": "{문서 제목} · {section_path}",
 "content": [{"type": "text", "text": "<청크 본문>"}],
 "citations": {"enabled": true}}
```

응답의 text 블록에 붙은 `search_result_location` 인용(`source`, `search_result_index`, `cited_text`, `start_block_index`, `end_block_index`)을 `answering/footnotes.py`의 순수 함수가 각주로 바꿉니다.

- text 블록을 순서대로 이어 붙이고 citations가 있는 블록 뒤에 `[n]`을 붙입니다.
- 번호는 첫 등장 순으로 1부터, 같은 청크는 같은 번호를 다시 씁니다.
- `source`로 청크를 찾고, 없으면 `search_result_index`로 찾습니다. 둘 다 안 되면 버립니다.
- thinking, fallback 등 text가 아닌 블록은 무시합니다.

## 근거

- `cited_text`는 API가 원문에서 잘라 주는 값이라 모델이 인용문을 바꿔 쓸 수 없습니다. 각주의 인용문은 항상 실제 청크 본문입니다.
- 인용 위치(`search_result_index`)가 구조화되어 와서 정규식으로 `[출처: …]`를 파싱할 필요가 없습니다.
- `cited_text`는 출력 토큰으로 계산되지 않습니다.

## 구현 전 확인한 사항 (공식 문서·설치된 SDK, 2026-11-24)

- [Search results 문서](https://platform.claude.com/docs/en/build-with-claude/search-results): 블록 형식과 인용 필드가 위와 같음을 확인. 한 요청의 모든 `search_result` 블록은 같은 citations 설정이어야 합니다.
- 설치된 `anthropic` 2.13.0의 `BetaCitationSearchResultLocation` 필드: `cited_text, end_block_index, search_result_index, source, start_block_index, title, type` — 계획과 일치.
- `client.beta.messages.create`가 `fallbacks`(`"default"` 또는 목록)와 `output_config`, `thinking`, `betas`를 받음을 확인.
- Sonnet 5.5는 `fallbacks="default"`와 beta `server-side-fallback-2026-07-01` 조합만 지원(Claude API). 기본 fallback은 `cyber`·`frontier_llm` 범주 거절을 Claude Sonnet 5로 재시도하고, `bio`·`reasoning_extraction`·`general_harms` 거절은 재시도하지 않습니다. 그래서 응답 `stop_reason`이 여전히 `refusal`일 수 있고, 이때 content를 읽지 않고 `pending_review(reason=refusal)`로 둡니다.
- `output_config.format`(structured outputs)은 citations와 함께 쓰면 400이라 쓰지 않습니다. `temperature`/`top_p`/`top_k`, `budget_tokens`, `thinking: disabled`, assistant prefill, 강제 `tool_choice`도 보내지 않습니다(`tests/test_answering.py::test_claude_request_shape`가 검사).

## 계획과 다른 점

- **가격표**: 계획은 Sonnet 5.5 cache read를 $0.20/MTok로 적었지만, 2026-11-24 [공식 가격 페이지](https://platform.claude.com/docs/en/about-claude/pricing)는 **$0.10/MTok**(기본 입력가의 0.05배)입니다. 문서를 따랐습니다. cache write는 5분 캐시 기준 $2.50입니다. fallback 대상인 `claude-sonnet-5`와 `claude-opus-5-5`, `claude-opus-5`, `claude-opus-4-8`도 같은 페이지 값으로 넣었습니다. 가격표에 없는 모델은 비용을 `NULL`로 두고 경고 로그를 남깁니다.
- **사용량 집계 범위**: fallback이 일어나면 응답의 최상위 `usage`는 최종 응답을 만든 시도만 담고, 시도별 내역은 `usage.iterations`에 있습니다. 지금은 최상위 `usage`와 응답의 실제 `model`만 `llm_calls`에 저장합니다. 거절된 앞 시도의 과금 여부는 거절 범주에 따라 다르므로, 정확한 청구액은 Console 사용량과 대조해야 합니다.

## 대안

| 대안 | 기각 이유 |
| --- | --- |
| 프롬프트로 `[1]` 형식과 출처 목록을 요구 | 인용문·번호를 모델이 생성 → 지어낸 인용, 형식 붕괴를 검증할 방법이 약함 |
| `document` 블록 + `char_location` 인용 | 청크 메타데이터(`source`)를 직접 실을 수 없어 매핑이 번거로움. RAG에는 `search_result`가 맞음 |
| structured outputs로 `{sentence, chunk_ids}` JSON | citations와 동시 사용 불가(400), 인용문 원문 보장이 없음 |

## 한계와 다음 단계

- 청크를 text 블록 하나로 보내므로 `cited_text`는 청크 전체(최대 약 1,500자)입니다. 문장·표 행 단위로 블록을 쪼개면 더 짧은 인용문을 얻을 수 있습니다(문서: 텍스트 블록이 최소 인용 단위).
- 시스템 프롬프트가 고정이라 prompt caching 대상이 되지만, 요청마다 검색 결과가 달라 캐시 효과는 작습니다.
