# ADR 0009: 로컬 상향(cascade)과 답변 숫자 근거 검증

- 상태: 채택
- 날짜: 2026-11-25
- 관련: [ADR 0008](0008-local-llm-ollama.md)(로컬 LLM), [ADR 0002](0002-claude-citations-search-result.md)(Citations), [ADR 0009 연결 주석 우선](0009-prefer-consolidated-notes.md)(같은 번호의 별개 결정)

## 맥락

API 비용 없이 로컬(Ollama) 모델만으로 답변 품질을 올리고 재고 싶었습니다. 두 가지 문제가 있었습니다.

1. **모델 고르기**: 작은 모델(예: `qwen3.5:9b`)은 빠르지만 근거 번호를 빠뜨리거나(`no_citations`) JSON을 깨뜨리는 일이 잦고, 큰 모델(예: `qwen3:14b`)은 낫지만 느립니다. 같은 데이터셋으로 여러 모델을 비교할 방법도 없었습니다(Eval은 제공자 하나만 돌림).
2. **숫자 오류**: 각주는 "이 문장은 이 청크를 근거로 한다"만 말합니다. 문장 속 숫자가 그 청크에 정말 있는지는 아무도 확인하지 않았습니다. 모델이 `1,234,567백만원`을 `약 1.2조 원`으로 바꿔 쓰거나 두 값을 더해 새 숫자를 만들어도 각주가 붙어 있으면 `published`가 됐습니다.

## 결정

### 1. 로컬 상향(cascade)

`ask_question(…, fallback_llm=…)`: primary(작은 모델) 답을 평소처럼 검증하고, **모델을 바꾸면 나아질 수 있는 사유**에 걸렸을 때만 같은 검색 결과·같은 메시지로 fallback(큰 모델)을 **한 번** 더 부릅니다. fallback 답도 같은 검증(각주·숫자·신뢰도 규칙)을 거치고, 또 걸리면 그대로 `pending_review`입니다.

| 사유 | 상향 | 이유 |
| --- | --- | --- |
| `refusal`, `truncated`, `no_citations`, `ungrounded_number` | 예 | 모델의 출력 품질 문제. 다른 모델은 거절하지 않거나, 근거 번호를 제대로 달거나, 숫자를 옮겨 적을 수 있음 |
| primary의 `LLMError(502)` | 예 | 출력 JSON이 두 번 다 깨진 경우. 형식 준수는 모델마다 다름 |
| `no_retrieval_results`, `low_retrieval_score` | 아니요 | 검색 문제. 같은 근거를 큰 모델에 줘도 근거 자체가 약함 |
| `review_mode_always` | 아니요 | 운영 정책이지 품질 문제가 아님 |
| `LLMError(503)`·`(504)` | 아니요 | 서버 연결 실패·모델 없음·시간 초과. 같은 Ollama 서버의 다른 모델도 대개 같이 실패하고, 시간 초과 뒤 더 큰 모델을 부르면 지연만 두 배가 됨 |

- 설정: `LOCAL_LLM_FALLBACK_MODEL`(빈 값 = 끔), `LOCAL_LLM_FALLBACK_THINK`(없으면 `LOCAL_LLM_THINK`). `LLM_PROVIDER=local`일 때만 켜집니다(`providers.make_fallback_llm`).
- 기록: 성공한 호출마다 `llm_calls` 행을 같은 `answer_id`로 남깁니다(스키마 변경 없음). `question.asked` 감사 로그 metadata에 `escalated`, `escalation_reasons`(primary의 사유 목록, 502면 `llm_error_502`), 상향했으면 `models`를 넣습니다. `latency_ms`는 두 호출을 합친 시간, 응답의 `usage` 토큰·비용은 두 호출의 합이고 `model`은 최종 답변의 모델입니다.
- primary가 502이고 fallback도 `LLMError`면 fallback 오류로 기존 실패 처리(rollback + `question.failed` + 예외)를 탑니다. primary 답이 있는데 fallback만 실패하면 primary 답(검토 대기)을 그대로 쓰고 `escalation_error`를 감사 로그에 남깁니다.
- 상향 대상은 `LLMClient` Protocol을 따르는 아무 클라이언트나 됩니다. 지금은 Ollama 모델만 연결하지만, 나중에 Claude로 바꿀 때 서비스 코드는 고치지 않아도 됩니다.

### 2. 답변 숫자 근거 검증

`answering/number_check.py::ungrounded_numbers`(순수 함수)가 인용이 달린 text 블록마다 숫자(`\d[\d,]*(?:\.\d+)?`, 쉼표 제거)를 뽑아, 그 블록이 인용한 청크 원문(쉼표 제거)에 숫자 경계를 지켜 같은 문자열이 있는지 봅니다. 소수점 없는 1~2자리 정수(번호·개수), 질문에 이미 있는 숫자, 각주 마커 `[n]`은 검사하지 않습니다. `NUMBER_GROUNDING_CHECK=review`(기본)면 하나라도 없을 때 `ungrounded_number`로 검토 대기에 보냅니다. 근거 없는 숫자(최대 10개)는 구조화 로그 `answer.ungrounded_numbers`와 감사 로그에 남깁니다. Claude(`api_citation`)와 로컬(`model_reference`) 모두 같은 content 블록 모양이라 제공자와 무관하게 동작합니다.

**단위 환산을 하지 않는 이유**: 회계 답변의 숫자는 원문 그대로 옮기는 것이 원칙입니다(시스템 프롬프트에도 있음). `1,234,567백만원`을 `1.2조 원`으로 바꾸면 반올림·단위 착오가 끼어들 수 있고, 감사인이 원문과 대조하기도 어려워집니다. 계산이나 환산이 필요하면 모델이 아니라 코드가 해야 합니다. 그래서 환산된 숫자는 **일부러** 근거 없음으로 잡습니다.

### 3. 로컬 모델 비교 Eval

`python -m footnote_rag.evals.run --live --local-models SPEC[,SPEC...] [--cascade PRIMARY>FALLBACK]`이 여러 로컬 구성을 같은 문항 순서로 돌려 구성별 리포트와 비교 리포트(`{stamp}-live-compare-local.md`)를 씁니다. 공시 가져오기는 한 번, 질문 임베딩도 문항당 한 번(`MemoizedQueryEmbedder`)이라 구성을 늘려도 Voyage 호출은 늘지 않습니다. 단일 모델 구성은 측정 전에 예열(`OllamaClient.warm_up`)하고, 상향 구성은 primary만 예열합니다. 상향 때 모델을 바꿔 올리는 시간은 실제 비용이므로 지연에 포함합니다. 한 문항의 `LLMError`는 그 문항을 `error`로 기록하고 실행을 계속합니다.

## 대안

| 대안 | 장점 | 기각 이유 |
| --- | --- | --- |
| Claude로 상향 | 품질이 가장 좋음 | API 비용. 이번 목표는 비용 0. 구조는 `LLMClient`라 나중에 바꿀 수 있음 |
| 모든 질문을 큰 모델로 | 단순함 | 쉬운 질문까지 느려짐. 상향 비율을 재야 작은 모델의 실제 가치가 보임 |
| 상향을 여러 번(사다리) | 더 많은 질문을 살림 | 지연 상한이 없어지고 16GB VRAM에서 모델 교체가 반복됨 |
| 숫자 단위 환산 후 비교 | 표현이 달라도 맞는 숫자를 인정 | 환산 규칙(백만원·억·조·%p) 오류가 새 위험이 됨. 원칙상 원문 그대로가 정답 |
| LLM 채점기로 숫자 검증 | 문맥까지 판단 | 채점 모델 자체를 검증해야 하고 비용·지연이 큼 |
| 로컬 임베딩으로 교체 | 완전 오프라인 | 차원·인덱스가 바뀌어 전체 재임베딩 필요. 문서 청크는 이미 캐시돼 질문 수만큼만 Voyage를 부름 |

## 알려진 한계

- **모델 교체 지연**: VRAM 16GB에서는 9B와 14B를 동시에 올리기 어렵습니다. 상향할 때마다 Ollama가 모델을 내리고 올려 수 초~수십 초가 더 걸릴 수 있습니다. 메모리가 충분하면 `OLLAMA_MAX_LOADED_MODELS=2`로 두 모델을 함께 둘 수 있지만, 모자라면 CPU로 넘어가 더 느려집니다. 비교 리포트의 p95 지연에 이 비용이 드러납니다.
- 숫자 검증은 같은 숫자 문자열이 있는지만 봅니다. 숫자가 청크에 있지만 다른 항목의 값인 경우(엉뚱한 행을 옮김)는 잡지 못합니다. 날짜·조항 번호처럼 3자리 이상인 숫자도 청크에 없으면 걸립니다.
- 답할 수 없는 질문은 primary가 근거 없음(`no_citations`)을 내므로 항상 상향됩니다. 상향 비율에 이 몫이 포함됩니다.
- 상향 여부는 질문 감사 로그에만 있고 DB 컬럼은 없습니다(마이그레이션 없음). 상향 통계를 화면에서 보려면 나중에 컬럼이나 집계가 필요합니다.
- 이 ADR 번호는 [연결 주석 우선 ADR](0009-prefer-consolidated-notes.md)과 같은 0009입니다. 파일 이름으로 구분합니다.
