# ADR 0008: 로컬 LLM 제공자(Ollama)와 프롬프트 기반 인용

- 상태: 채택
- 날짜: 2026-11-24
- 관련: [ADR 0002](0002-claude-citations-search-result.md)(Claude Citations), [ADR 0003](0003-embedding-provider-voyage.md)(Voyage 임베딩 유지)

## 맥락

답변 생성은 Claude Messages API의 `search_result` 블록과 Citations로 문장별 근거를 받습니다(ADR 0002). Anthropic API 크레딧이 없으면 Fake 모드로만 돌릴 수 있어, 실제 공시로 실제 답변을 시연하거나 테스트할 수 없었습니다.

개발 PC는 AMD Radeon RX 9070 XT(VRAM 16GB), RAM 31GB, Windows + Docker Desktop입니다. 14B 모델을 4bit로 양자화하면 VRAM에 들어갑니다. 그래서 호스트에서 Ollama를 돌리고, API 컨테이너가 `host.docker.internal:11434`로 호출하기로 했습니다.

문제는 로컬 모델에는 **Citations API가 없다**는 점입니다. 모델이 어느 문장이 어느 근거에서 왔는지 직접 말하게 하고, 서버가 그 값을 검증해야 합니다.

## 결정

`LLM_PROVIDER=local`을 명시하면 `OllamaClient`(`src/footnote_rag/answering/local_llm.py`)를 씁니다. `auto`는 지금처럼 키가 있으면 Claude, 없으면 Fake입니다. `local`을 자동으로 고르지는 않습니다.

1. **번호를 붙인 근거 + JSON 스키마 강제 출력**
   - 검색 결과를 `[n] {문서 제목} · {section_path}\n{청크 본문}`으로 나열하고 질문을 붙입니다.
   - Ollama의 `format`에 JSON 스키마 `{"insufficient": bool, "sentences": [{"text": string, "sources": [integer]}]}`(`additionalProperties: false`)를 넘깁니다. Ollama는 이 스키마를 문법(grammar) 제약으로 디코딩에 적용합니다.
2. **서버 측 검증**
   - JSON 파싱이나 스키마 검증에 실패하면 한 번 다시 요청합니다. 두 번째도 실패하면 502입니다. `done_reason=length`(출력 상한 도달)는 다시 요청해도 같은 상한에 걸리므로 재요청하지 않습니다. 완성된 문장 객체만 살리고 `truncated`로 검토 대기에 보냅니다.
   - `sources`의 번호는 `1..k`만 인정합니다. 범위 밖 번호는 버리고 개수를 로그와 `llm_calls.dropped_source_refs`에 남깁니다. 문장 안에 모델이 직접 쓴 `[n]`은 지웁니다. 서버가 매기는 각주 번호와 섞이지 않게 하기 위해서입니다.
   - `insufficient: true`이거나 유효한 근거가 하나도 없으면 "근거 없음" 답변이 됩니다. 각주가 0개이므로 기존 신뢰도 규칙에 따라 `pending_review`(`no_citations`)입니다.
3. **같은 각주 흐름 재사용**
   - 검증한 문장을 Claude 응답과 같은 모양(`text` 블록 + `search_result_location` citations)으로 바꿉니다. 그래서 각주 번호 매기기(`footnotes.build_footnotes`), 신뢰도 규칙, 검토, 감사 로그, 사용량 기록이 Claude 경로와 같은 코드를 탑니다.
   - `cited_text`는 모델이 주지 않으므로 서버가 고릅니다. 청크를 문장으로 나누고, 답변 문장과 겹치는 문자 2-gram이 가장 많은 문장을 300자 이내로 씁니다(`footnotes.select_cited_text`).
   - 각주마다 `citation_method`를 붙입니다. Claude는 `api_citation`, 로컬은 `model_reference`입니다. 웹 화면은 `model_reference`이면 "로컬 모델이 지정한 근거 — 인용문은 근사 매칭"이라고 안내합니다.
4. **Ollama 네이티브 API**(`POST /api/chat`, `stream: false`)를 씁니다. `options.num_ctx`로 컨텍스트 길이를 지정할 수 있기 때문입니다. 기본 컨텍스트(수천 토큰)로는 근거 8개(청크 1,500자씩)와 지시문이 들어가지 않고, Ollama는 넘치는 앞부분을 **조용히 잘라** 버립니다. OpenAI 호환 엔드포인트(`/v1/chat/completions`)는 `num_ctx`를 받지 않습니다.
5. **사용량**: `prompt_eval_count`·`eval_count`를 입력·출력 토큰으로, `done_reason`을 `stop_reason`(`stop`→`end_turn`, `length`→`max_tokens`)으로 옮깁니다. `llm_calls.model`은 `ollama:{모델}`이고, 가격표는 `ollama:` 접두어를 0원으로 계산합니다.
6. **오류**: 연결 거부·DNS 실패·연결 타임아웃은 503, 응답 타임아웃은 504, 모델 없음(Ollama 404)은 `ollama pull` 안내가 담긴 503입니다. 기존 `question.failed` 감사 로그 경로를 그대로 씁니다.
7. **상태 확인**: `/readyz`는 그대로 둡니다. LLM이 꺼져 있어도 문서 가져오기·검색은 동작해야 하기 때문입니다. 대신 admin 전용 `GET /llm/status`가 `GET {base}/api/tags`(3초 타임아웃)로 연결과 모델 존재를 확인합니다.

## Claude Citations와의 차이

| 항목 | Claude Citations(`api_citation`) | 로컬 모델(`model_reference`) |
| --- | --- | --- |
| 근거 연결 | API가 인용 위치를 반환 | 모델이 근거 번호를 JSON으로 반환 |
| `cited_text` | 모델이 실제로 인용한 원문 구간 | 서버가 고른 근사 문장(2-gram 겹침) |
| 잘못된 근거 | 구조상 존재하지 않는 근거는 인용 불가 | 범위 밖 번호 가능 → 서버가 버리고 셈 |
| 근거와 문장의 일치 | API가 보장 | 보장 없음. 번호는 맞아도 내용이 다른 근거일 수 있음 |
| 출력 형식 | 텍스트 블록 | JSON(스키마 강제), 실패 시 1회 재요청 |

## 대안

| 대안 | 장점 | 기각 이유 |
| --- | --- | --- |
| 자유 텍스트에 `[n]`을 쓰게 하고 정규식으로 추출 | 프롬프트가 단순함 | 형식이 흔들리면 문장 경계와 근거 연결이 모호해짐. 검증할 구조가 없음 |
| JSON 스키마 없이 "JSON으로 답하라"는 지시만 | 모델·서버 제약이 적음 | 작은 모델은 형식을 자주 어김. Ollama `format`은 문법 제약이라 실패율이 훨씬 낮음 |
| LM Studio / OpenAI 호환 서버 | 여러 서버를 같은 클라이언트로 지원 | `num_ctx`를 요청마다 지정할 수 없어 긴 프롬프트가 잘릴 수 있음. 서버별 JSON 스키마 지원도 제각각 |
| vLLM | 처리량이 높고 구조화 출력 지원 | AMD GPU + Windows에서 설치·운영 부담이 큼. 시연·개발 용도에는 과함 |
| Ollama를 Docker 컨테이너로 실행 | compose 하나로 끝남 | Windows Docker Desktop에서 AMD GPU 전달이 어려움. 모델 파일(수 GB)을 볼륨으로 관리해야 함 |
| 로컬 임베딩도 함께 도입 | 완전 오프라인 | 임베딩 차원·인덱스가 바뀌어 재처리가 필요함. 이번 범위는 답변 생성만(Voyage 유지) |

## 결과

- API 키 없이 실제 공시로 실제 답변을 만들 수 있습니다. 비용은 0으로 기록됩니다.
- 인용 정확도는 Claude보다 낮을 수 있습니다. 모델이 고른 번호가 문장 내용과 맞는지는 보장되지 않습니다. 그래서 각주에 `citation_method`를 남기고, 화면에서 근사 매칭임을 알립니다. Eval 리포트 머리말과 파일명에 제공자·모델·`citation_method`를 적어 두 경로를 비교할 수 있게 했습니다.
- 속도는 GPU와 모델 크기에 따라 크게 달라집니다. 14B 4bit 모델로 근거 8개 질문 하나에 수십 초가 걸릴 수 있어 `LOCAL_LLM_TIMEOUT_SECONDS` 기본값을 180초로 두었습니다.
- `llm_calls`에 `dropped_source_refs` 컬럼(마이그레이션 0003, 기본값 0)이 생겼습니다.
