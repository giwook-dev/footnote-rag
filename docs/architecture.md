# 아키텍처

> 이 문서의 그림을 코드 위치와 함께 단계별로 해설한 학습 자료는 [docs/study/README.md](study/README.md)에 있습니다.

## 구성도

```mermaid
flowchart LR
    browser["브라우저"] --> webui["Web UI (/ui)<br/>정적 HTML·JS·CSS"]
    webui -->|"fetch + JWT (sessionStorage)"| api
    client["Client / MCP 서버"] -->|"JWT"| api["FastAPI API<br/>auth · RBAC · org 격리"]
    reviewer["Reviewer"] -->|"approve / reject"| api
    api <--> db[("PostgreSQL 16 + pgvector<br/>chunks · jobs · audit_logs · llm_calls")]
    api -->|"search_result + citations"| claude["Claude Sonnet 5.5<br/>(server-side fallback)"]
    api -->|"input_type=query"| voyage["Voyage voyage-4"]
    worker["Worker (N개)"] -->|"FOR UPDATE SKIP LOCKED"| db
    worker -->|"document.xml ZIP"| dart["OpenDART"]
    worker -->|"input_type=document"| voyage
    worker <--> cache[("data/dart<br/>ZIP · corpCode 캐시")]
```

- **Web UI**는 FastAPI가 `/ui`에서 직접 서빙하는 빌드 없는 정적 파일(`src/footnote_rag/web/static/`)입니다. 브라우저에서 같은 출처의 HTTP API를 Bearer 토큰으로 호출하므로 권한 검사·조직 격리는 API와 같습니다. `/ui` 응답에만 CSP(`script-src 'self'` 등)·`nosniff`·`Referrer-Policy` 헤더를 붙입니다. 종목코드 검색은 CLI `dart-find`와 같은 서비스 함수(`dart/service.py::find_reports`)를 쓰는 `GET /dart/reports`가 제공합니다.
- **API**는 요청마다 `X-Request-ID`를 붙이고(없으면 생성) 처리 시간을 JSON 로그로 남깁니다. 감사 로그 행에도 같은 request_id가 저장됩니다.
- **워커**는 API와 같은 이미지에서 `python -m footnote_rag.jobs.worker`로 돌며 여러 개를 띄워도 됩니다.
- 외부 의존성(OpenDART, Voyage, Claude)은 모두 Protocol 뒤에 있고 `providers.py`가 실제/Fake 구현을 고릅니다. 테스트와 CI는 항상 Fake입니다.

## ERD

```mermaid
erDiagram
    organizations ||--o{ users : has
    organizations ||--o{ documents : owns
    users ||--o{ documents : created_by
    documents ||--o{ chunks : "split into"
    organizations ||--o{ questions : has
    users ||--o{ questions : asked_by
    questions ||--|| answers : "answered by"
    answers ||--o{ llm_calls : "cost of"
    organizations ||--o{ audit_logs : "append-only"

    organizations { uuid id PK  text name  timestamptz created_at }
    users { uuid id PK  uuid org_id FK  text email UK  text password_hash  user_role role  bool is_active }
    documents { uuid id PK  uuid org_id FK  uuid created_by FK  text title  document_source source  text source_ref  jsonb meta  document_status status  int section_count  text error }
    chunks { uuid id PK  uuid document_id FK  uuid org_id  text section_path  int chunk_index  text text  vector embedding }
    jobs { uuid id PK  text type  jsonb payload  job_status status  int attempts  int max_attempts  timestamptz run_after  timestamptz locked_at  text locked_by  text last_error  jsonb result }
    questions { uuid id PK  uuid org_id FK  uuid asked_by FK  text text }
    answers { uuid id PK  uuid question_id FK  uuid org_id  text text  jsonb footnotes  answer_status status  text review_reason  uuid reviewed_by  timestamptz reviewed_at  text review_comment }
    llm_calls { uuid id PK  uuid org_id  uuid answer_id FK  text model  int input_tokens  int output_tokens  int cache_read_input_tokens  int cache_creation_input_tokens  numeric cost_usd  int latency_ms  text stop_reason }
    audit_logs { uuid id PK  uuid org_id  uuid actor_user_id  text action  text target_type  text target_id  jsonb metadata  text request_id }
```

- `documents`: `UNIQUE(org_id, source, source_ref)` — 같은 조직의 같은 접수번호는 하나만 존재합니다.
- `chunks.embedding`: `vector(EMBEDDING_DIM)`, HNSW(`vector_cosine_ops`) 인덱스와 `(org_id)` 인덱스.
- `jobs`: 조직 컬럼 대신 `payload.org_id`를 두고 `GET /jobs/{id}`가 이 값으로 범위를 제한합니다. 수집 결과(청크 수, 임베딩 토큰)는 `result`에 저장합니다.
- `audit_logs`: `BEFORE UPDATE OR DELETE` 행 트리거와 `BEFORE TRUNCATE` 문장 트리거가 예외를 던집니다. 실패한 로그인처럼 조직을 알 수 없는 행은 `org_id`가 NULL입니다.

## 조직 격리

모든 조회·변경은 현재 사용자의 `org_id`로 범위를 제한합니다(애플리케이션 계층). 다른 조직의 리소스 ID로 조회하면 존재 여부를 드러내지 않도록 `404`를 돌려줍니다. 벡터 검색은 `chunks.org_id`와 `documents.org_id`를 모두 조건에 넣고 `status='ready'` 문서만 대상으로 합니다. 테넌트 필터가 있는 HNSW 검색에서 결과가 `top_k`보다 적게 나오지 않도록 `SET LOCAL hnsw.iterative_scan = strict_order`(pgvector 0.8+)를 씁니다.

## 가져오기 흐름

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as API
    participant DB as Postgres
    participant W as Worker
    participant D as OpenDART
    participant V as Voyage
    C->>A: POST /documents/dart {rcept_no}
    A->>DB: BEGIN; INSERT documents(queued), jobs(import_dart), audit_logs; COMMIT
    A-->>C: 202 {document_id, job_id}
    W->>DB: SELECT … FOR UPDATE SKIP LOCKED → running, attempts+1
    W->>D: document.xml (data/dart/{rcept_no}.zip 캐시 우선)
    W->>W: XML 파싱 → section_path별 섹션 → 청킹(1,500자/200자 겹침)
    W->>V: embed(input_type=document), 1,000개·토큰 한도로 배치
    W->>DB: BEGIN; DELETE chunks; INSERT chunks; documents ready; jobs succeeded; audit; COMMIT
```

재시도 시 기존 청크를 지우고 다시 넣으므로 청크가 중복되지 않습니다.

## 질문 처리 시퀀스

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as API
    participant V as Voyage
    participant DB as Postgres
    participant L as Claude
    C->>A: POST /questions {question, document_ids?}
    A->>DB: document_ids가 같은 조직 문서인지 확인 (아니면 404)
    A->>V: embed(question, input_type=query)
    A->>DB: pgvector cosine top_k (같은 org, ready 문서)
    alt 검색 결과 0건
        A->>A: "근거 없음" 답변, Claude 호출 안 함
    else
        A->>L: beta.messages.create(search_result 블록 × k + 질문, citations on,<br/>adaptive thinking, effort, fallbacks="default")
        L-->>A: stop_reason, content(text + citations)
        A->>A: refusal이면 content를 읽지 않음 / 아니면 citations → 각주 [n]
    end
    A->>A: 신뢰도 규칙 → published | pending_review(review_reason)
    A->>DB: BEGIN; questions, answers, llm_calls(실제 model·토큰·비용·지연), audit ×2; COMMIT
    A-->>C: {answer_id, status, text, footnotes, usage, latency_ms}
```

신뢰도 규칙(`REVIEW_MODE=on_low_confidence`): `refusal`, `no_retrieval_results`, `low_retrieval_score`(최고 유사도 < `MIN_RETRIEVAL_SCORE`), `no_citations` 중 하나라도 해당하면 `pending_review`입니다. `REVIEW_MODE=always`면 모든 답변이 검토 대기로 갑니다. member는 `pending_review` 답변의 본문을 볼 수 없습니다.

## 작업 상태 전이

```mermaid
stateDiagram-v2
    [*] --> queued: enqueue (문서와 같은 트랜잭션)
    queued --> running: claim (SKIP LOCKED, attempts+1)
    running --> succeeded: 처리 완료 (청크·문서·감사와 같은 트랜잭션)
    running --> queued: 재시도 가능 실패 & attempts < max_attempts<br/>run_after = now + 2^attempts × BASE
    running --> failed: 재시도 불가 실패 또는 attempts 소진
    running --> queued: locked_at이 JOB_LOCK_TIMEOUT 초과 (멈춘 작업 복구)
    succeeded --> [*]
    failed --> [*]
```

| 실패 | 분류 |
| --- | --- |
| OpenDART 020(요청 한도), 800(점검), 900, HTTP 5xx/429, 네트워크 오류 | 재시도 |
| OpenDART 010/011/012/901(키·권한), 013(데이터 없음), 014(파일 없음), HTTP 4xx | 즉시 실패 |
| 파싱 실패(텍스트 없음, ZIP 아님) | 즉시 실패 |
| Voyage 레이트 리밋·서버·연결 오류 | 재시도 |

작업이 실패하면 `jobs.last_error`와 `documents.error`가 채워지고 문서는 `failed`(재시도 예정이면 `queued`)가 됩니다. 워커는 SIGTERM을 받으면 처리 중인 작업을 끝낸 뒤 종료합니다.

## 동시성 보장

| 상황 | 방법 |
| --- | --- |
| 워커 여러 개가 같은 작업 claim | `FOR UPDATE SKIP LOCKED` + 같은 트랜잭션에서 `running`으로 변경 |
| 같은 접수번호 동시 가져오기 | `UNIQUE(org_id, source, source_ref)` → IntegrityError 시 기존 문서 반환 |
| 두 검토자가 동시에 결정 | `UPDATE … WHERE status='pending_review' RETURNING` 한 번 — 패자는 0행 → 409 |
| 감사 로그 변조 | DB 트리거가 UPDATE/DELETE/TRUNCATE 거부 |
