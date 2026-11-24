# 용어집

학습 문서에서 쓰는 용어를 한두 줄로 정의하고, 자세히 다루는 문서로 연결한다. 순서는 주제별이다.

## RAG와 검색

### RAG
Retrieval-Augmented Generation. 질문과 관련된 문서 조각을 먼저 검색해 모델에게 근거로 주고 답하게 하는 방식. 모델 기억 대신 주어진 문서에 기대게 한다. → [01. 전체 그림](01-big-picture.md)

### 임베딩
텍스트를 의미가 비슷하면 가까운 고정 길이 숫자 벡터로 바꾼 것. 이 저장소는 Voyage `voyage-4`, 기본 1,024차원을 쓴다. → [05. RAG 검색](05-rag-retrieval.md)

### cosine 유사도
두 벡터 사이 각도의 코사인 값(-1~1). 클수록 비슷하다. pgvector의 cosine 거리는 `1 - 유사도`이고, 이 저장소의 검색 점수는 유사도다. → [05. RAG 검색](05-rag-retrieval.md)

### pgvector
PostgreSQL에서 벡터 컬럼(`vector(n)`), 거리 연산, 벡터 인덱스를 쓰게 해 주는 확장. → [05. RAG 검색](05-rag-retrieval.md)

### HNSW
Hierarchical Navigable Small World. 그래프를 따라가며 가까운 벡터를 빠르게 찾는 근사 최근접 이웃 인덱스. 정확도를 조금 내주고 속도를 얻는다. → [05. RAG 검색](05-rag-retrieval.md)

### iterative scan
pgvector 0.8 이상의 `hnsw.iterative_scan` 설정. WHERE 조건을 통과한 행이 `LIMIT`만큼 모일 때까지 인덱스를 계속 탐색한다. 조직 필터 때문에 결과가 모자라는 문제를 막는다. → [05. RAG 검색](05-rag-retrieval.md)

### 청킹
긴 문서를 검색·인용 단위 조각(청크)으로 나누는 것. 이 저장소는 섹션 경계를 넘지 않고 문자 기준 1,500자, 겹침 200자가 기본이다. → [05. RAG 검색](05-rag-retrieval.md)

### input_type
Voyage 임베딩에서 문서(`document`)와 질문(`query`)을 구분하는 옵션. 짧은 질문과 긴 문서 조각의 비대칭을 반영한다. → [05. RAG 검색](05-rag-retrieval.md)

## Claude API

### Citations
Claude가 답변 text 블록에 "어느 근거의 어느 부분을 인용했는지"를 구조화해 붙여 주는 기능. 인용문(`cited_text`)은 API가 원문에서 잘라 준다. → [06. Claude Citations](06-claude-citations.md)

### search_result 블록
RAG 검색 결과를 Claude에게 보내는 content 블록 형식(`source`, `title`, `content`, `citations`). 인용 응답의 타입은 `search_result_location`이다. → [06. Claude Citations](06-claude-citations.md)

### adaptive thinking
`thinking: {"type": "adaptive"}`. 모델이 문제에 따라 생각할지, 얼마나 생각할지 스스로 정한다. 고정 사고 예산(`budget_tokens`)을 대신한다. → [06. Claude Citations](06-claude-citations.md)

### effort
`output_config.effort`(`low`~`max`). 사고 깊이와 전체 토큰 사용량을 조절하는 설정. 이 저장소는 `CLAUDE_EFFORT` 기본 `medium`을 명시적으로 보낸다. → [06. Claude Citations](06-claude-citations.md)

### refusal
안전 분류기가 요청을 거절했을 때의 `stop_reason` 값. HTTP 200으로 오므로 content를 읽기 전에 확인해야 한다. 이 저장소는 `pending_review(refusal)`로 보낸다. → [06. Claude Citations](06-claude-citations.md)

### fallback
서버 측 거절 fallback. `fallbacks="default"`와 beta 헤더를 보내면 일부 범주의 거절을 API가 같은 호출 안에서 다른 모델로 다시 시도한다. 그래서 응답 `model`이 요청 모델과 다를 수 있다. → [06. Claude Citations](06-claude-citations.md)

### stop_reason
응답이 왜 끝났는지 알려 주는 필드(`end_turn`, `max_tokens`, `refusal` 등). → [06. Claude Citations](06-claude-citations.md)

## 인증·보안

### JWT
JSON Web Token. 서명된 JSON 토큰. 이 저장소는 HS256으로 서명하고 `sub`(사용자 ID), `org_id`, `role`, `iat`, `exp`를 담는다. → [03. 인증·인가](03-auth-rbac-isolation.md)

### OAuth2 password flow
클라이언트가 사용자 이메일·비밀번호를 폼으로 토큰 엔드포인트(`POST /auth/token`)에 보내 액세스 토큰을 받는 방식. → [03. 인증·인가](03-auth-rbac-isolation.md)

### argon2
느리고 메모리를 많이 쓰게 설계된 비밀번호 해시 알고리즘. 이 저장소는 `pwdlib`로 쓴다. → [03. 인증·인가](03-auth-rbac-isolation.md)

### RBAC
역할 기반 접근 제어. 역할은 `admin`, `reviewer`, `member`이며 `require_role` 의존성이 검사한다(부족하면 403). → [03. 인증·인가](03-auth-rbac-isolation.md)

### 테넌트 격리
여러 조직이 한 DB를 쓰되 서로의 데이터를 못 보게 하는 것. 이 저장소는 모든 쿼리에 `org_id` 조건을 넣고 다른 조직 리소스는 404로 숨긴다. → [03. 인증·인가](03-auth-rbac-isolation.md)

### RLS
Row-Level Security. Postgres가 행 단위로 접근을 제한하는 기능. 이 저장소는 쓰지 않고 후속 과제로 둔다([ADR 0005](../adr/0005-org-isolation-app-layer.md)). → [03. 인증·인가](03-auth-rbac-isolation.md)

### SecretStr
pydantic 타입. 감싼 값이 `repr`·직렬화에서 가려진다. API 키와 JWT 비밀을 이것으로 보관한다. → [09. 테스트와 운영](09-testing-and-ops.md)

## 작업 큐와 동시성

### 트랜잭션
여러 SQL을 하나로 묶어 모두 반영하거나 모두 취소하는 단위. 이 저장소는 서비스 함수가 명시적으로 커밋한다. → [02. 요청 따라가기](02-request-walkthrough.md)

### SKIP LOCKED
`SELECT … FOR UPDATE SKIP LOCKED`. 다른 트랜잭션이 잠근 행을 기다리지 않고 건너뛴다. 여러 워커가 같은 작업을 집지 않게 한다. → [04. 작업 큐](04-job-queue.md)

### 지수 백오프
실패할 때마다 재시도 대기 시간을 2배씩 늘리는 것. 이 저장소는 `2^attempts × BASE_BACKOFF_SECONDS`. → [04. 작업 큐](04-job-queue.md)

### 멱등성
같은 작업을 여러 번 실행해도 한 번 실행한 것과 결과가 같은 성질. 가져오기 작업은 청크를 지우고 다시 넣어 멱등하다. → [04. 작업 큐](04-job-queue.md)

### 멈춘 작업 복구
`running`인데 `locked_at`이 `JOB_LOCK_TIMEOUT_SECONDS`(기본 600초)보다 오래된 작업을 `queued`로 되돌리는 것. 워커가 죽은 경우를 처리한다. → [04. 작업 큐](04-job-queue.md)

### 조건부 UPDATE
확인 조건을 UPDATE의 WHERE에 넣어 "읽고 쓰기"를 한 문장으로 처리하는 것. 검토 결정이 `WHERE status='pending_review'`로 동시 결정을 막는다. → [08. 검토·감사·사용량](08-review-audit-usage.md)

## 검토·감사·운영

### HITL
Human-in-the-loop. 자동 결과 중 위험한 것을 사람이 확인하는 단계. 이 저장소의 `pending_review` 대기열과 `POST /reviews/{answer_id}`. → [08. 검토·감사·사용량](08-review-audit-usage.md)

### append-only
추가만 되고 수정·삭제가 안 되는 기록 방식. `audit_logs`는 DB 트리거가 UPDATE/DELETE/TRUNCATE를 거부한다. → [08. 검토·감사·사용량](08-review-audit-usage.md)

### p50 / p95
지연시간을 정렬했을 때 50%, 95% 위치 값. `/usage`가 Claude 호출 지연시간으로 계산한다. → [08. 검토·감사·사용량](08-review-audit-usage.md)

### request_id
요청 하나를 식별하는 값. `X-Request-ID` 헤더로 받거나 새로 만들어 모든 로그와 감사 행에 붙인다. → [09. 테스트와 운영](09-testing-and-ops.md)

### Protocol과 Fake
`typing.Protocol`로 외부 의존성의 인터페이스를 정하고, 네트워크 없는 결정적 구현(Fake)으로 바꿔 끼우는 구조. → [09. 테스트와 운영](09-testing-and-ops.md)

### Alembic
SQLAlchemy용 DB 마이그레이션 도구. 이 저장소의 스키마는 `alembic/versions/0001_initial.py`가 만든다. → [01. 전체 그림](01-big-picture.md)

## Eval과 도구

### recall@k
정답 근거가 상위 k개 검색 결과 안에 있는 질문의 비율. 이 저장소는 기대 목차 접두어 일치로 판정한다. → [10. Eval과 MCP](10-eval-and-mcp.md)

### 인용 정확도
각주 중 기대 목차 아래를 가리키는 각주의 비율. → [10. Eval과 MCP](10-eval-and-mcp.md)

### MCP
Model Context Protocol. AI 클라이언트가 외부 도구를 표준 방식으로 부르게 하는 프로토콜. 이 저장소의 MCP 서버는 HTTP API를 사용자 토큰으로 호출한다. → [10. Eval과 MCP](10-eval-and-mcp.md)

## OpenDART

### OpenDART
금융감독원 전자공시시스템(DART)의 공식 Open API. 인증키(`crtfc_key`)가 필요하다. → [07. OpenDART 연동](07-dart-integration.md)

### rcept_no
접수번호(14자리). 공시 문서 하나를 가리키며 앞 8자리는 접수일이다. `POST /documents/dart`의 입력. → [07. OpenDART 연동](07-dart-integration.md)

### corp_code
DART 고유번호(8자리). 종목코드와 다르며 `corpCode.xml`로 종목코드에서 찾는다. → [07. OpenDART 연동](07-dart-integration.md)

### section_path
이 저장소가 원문 XML의 `SECTION-n`·`TITLE`로 만든 목차 경로. 예: `(첨부)재무제표 > 주석 > 12. 리스`. 각주 위치 표시에 쓴다. → [07. OpenDART 연동](07-dart-integration.md)

### crtfc_key
OpenDART 인증키 쿼리 파라미터. 로그에서는 `***`로 가린다. → [07. OpenDART 연동](07-dart-integration.md)
