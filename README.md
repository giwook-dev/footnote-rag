# footnote-rag

실제 DART 공시 감사보고서를 접수번호(`rcept_no`)로 가져와, 질문에 **문장마다 각주 `[1]`, `[2]`** 를 달아 답하는 백엔드 서비스입니다.

- 각주에는 문서 제목, 회사명, 목차 경로(예: `(첨부)재무제표 > 주석 > 12. 리스`), 인용문, DART 뷰어 링크가 들어갑니다.
- 근거가 약한 답변(각주 없음, 낮은 유사도, 검색 결과 없음, 모델 거절)은 **사람 검토 대기열**로 갑니다.
- 로그인, 가져오기, 질문, 답변, 검토 결정, LLM 사용량·비용이 **감사 로그**와 `llm_calls`에 남습니다.

| 영역 | 구현 |
| --- | --- |
| API | FastAPI, OAuth2 password flow + JWT, 역할(admin / reviewer / member), 조직 격리 |
| 저장소 | PostgreSQL 16 + pgvector(HNSW, cosine), Alembic 마이그레이션 |
| 비동기 작업 | Postgres `FOR UPDATE SKIP LOCKED` 큐, 지수 백오프 재시도, 멈춘 작업 복구 |
| 외부 연동 | OpenDART(`corpCode.xml`, `list.json`, `document.xml`), Voyage `voyage-4` 임베딩, Claude `claude-sonnet-5-5` |
| 답변 | `search_result` 블록 + Citations → 각주 변환, 서버 측 거절 fallback |
| 운영 | structlog JSON 로그, `X-Request-ID`, `/healthz`, `/readyz`, Docker Compose, GitHub Actions CI |
| 품질 | pytest(실제 Postgres, 외부 네트워크 없음), Eval 실행기, MCP 서버 |

설계 근거는 [docs/architecture.md](docs/architecture.md)와 [docs/adr/](docs/adr/)에 있습니다.

## 빠른 시작

### 1) Fake 모드 (키 없이)

API 키가 하나도 없어도 전체 스택이 동작합니다. Claude·Voyage·OpenDART 대신 결정적인 Fake 구현(합성 데이터)이 쓰입니다.

```bash
docker compose up --build -d          # db → migrate → api, worker
curl http://localhost:8000/readyz     # {"status":"ready","database":"ok"}
docker compose exec api python -m footnote_rag.cli seed \
  --org "Demo" --admin-email admin@example.com --admin-password 'change-me-please'
```

Fake 모드의 OpenDART는 `가람전자(합성)` 같은 **합성 회사**의 합성 감사보고서(접수번호 `20260310900001`~`20260310900003`)를 돌려줍니다. 실제 공시가 아닙니다.

### 2) 실제 키 모드

```bash
cp .env.example .env      # 키 3개를 채웁니다. .env는 절대 커밋하지 않습니다.
docker compose up --build -d
```

`LLM_PROVIDER`, `EMBEDDER`, `DART_PROVIDER`의 기본값 `auto`는 해당 키가 있으면 실제 제공자를, 없으면 Fake를 씁니다. 키는 런타임에 `env_file`로 주입되며 이미지에는 들어가지 않습니다(`.dockerignore`가 `.env`와 `data/`를 제외).

### 로컬 개발

```bash
uv sync
docker compose up -d db                 # 127.0.0.1:5433
export DATABASE_URL=postgresql+asyncpg://footnote:footnote@localhost:5433/footnote
uv run alembic upgrade head
uv run uvicorn footnote_rag.main:app --reload
uv run python -m footnote_rag.jobs.worker
```

테스트는 실제 Postgres(pgvector)가 필요합니다. 외부 네트워크는 호출하지 않으며 `.env`도 읽지 않습니다.

```bash
docker run -d --name footnote-testdb -e POSTGRES_USER=footnote -e POSTGRES_PASSWORD=footnote \
  -e POSTGRES_DB=footnote_test -p 127.0.0.1:55439:5432 pgvector/pgvector:pg16
uv run ruff check . && uv run ruff format --check . && uv run pytest
# 다른 DB를 쓰려면 TEST_DATABASE_URL=postgresql+asyncpg://...
```

## API 키 발급

| 키 | 발급처 | 용도 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | [Anthropic Console](https://platform.claude.com/) → API Keys | Claude 답변 생성 |
| `VOYAGE_API_KEY` | [Voyage AI dashboard](https://dashboard.voyageai.com/) → API keys | 문서·질문 임베딩 |
| `OPENDART_API_KEY` | [OpenDART](https://opendart.fss.or.kr/) 회원가입 → 인증키 신청 | 공시 목록·원문 다운로드 |

> **`.env`는 커밋하지 마세요.** `.gitignore`와 `.dockerignore`가 `.env`, `.env.*`(단 `.env.example` 제외), `data/`를 제외합니다. 설정 객체는 키를 `SecretStr`로 보관해 `repr`·로그에 값이 나오지 않으며, OpenDART 요청 URL의 `crtfc_key`는 로그에서 `***`로 마스킹됩니다.

## 환경변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://footnote:footnote@localhost:5432/footnote` | asyncpg URL |
| `JWT_SECRET` | 개발용 값 | `APP_ENV=production`이면 반드시 설정(기본값이면 기동 거부) |
| `APP_ENV` | `dev` | `production`이면 JWT 비밀값 검사 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | JWT 만료 |
| `ANTHROPIC_API_KEY` / `VOYAGE_API_KEY` / `OPENDART_API_KEY` | 없음 | 비어 있으면 미설정으로 간주 |
| `LLM_PROVIDER` / `EMBEDDER` / `DART_PROVIDER` | `auto` | `auto` \| `fake` \| `real` |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | 날짜 접미사 없는 모델 ID |
| `CLAUDE_EFFORT` | `medium` | `output_config.effort` |
| `CLAUDE_MAX_TOKENS` | `16000` | 비스트리밍 요청 상한 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `voyage-4` / `1024` | 차원은 마이그레이션 시점에 `vector(n)`으로 고정 |
| `TOP_K` | `8` | 검색 청크 수 |
| `MIN_RETRIEVAL_SCORE` | `0.35` | 최고 유사도가 이보다 낮으면 검토 대기 |
| `REVIEW_MODE` | `on_low_confidence` | `always`면 모든 답변을 검토 대기로 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1500` / `200` | 문자 기준 |
| `JOB_MAX_ATTEMPTS` | `5` | 작업 최대 시도 횟수 |
| `BASE_BACKOFF_SECONDS` | `5` | 재시도 지연 = `2^attempts × BASE` |
| `JOB_LOCK_TIMEOUT_SECONDS` | `600` | 이보다 오래된 `running` 작업은 `queued`로 복구 |
| `DATA_DIR` | `data` | DART ZIP·corpCode 캐시, 업로드 파일(gitignore) |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | structlog |

## curl 예시: 로그인 → DART 가져오기 → 질문

```bash
API=http://localhost:8000

# 1. 로그인 (OAuth2 password flow)
TOKEN=$(curl -s -X POST $API/auth/token \
  -d 'username=admin@example.com' -d 'password=change-me-please' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# 2. 접수번호 찾기 (실제 키 모드) — 삼성전자·NAVER·현대자동차 감사보고서
docker compose exec api python -m footnote_rag.cli dart-find \
  --stock-codes 005930,035420,005380 --from 20260101 --to 20260531 --detail F001

# 3. DART 가져오기: 202 {document_id, job_id}
curl -s -X POST $API/documents/dart -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"rcept_no": "20260310900001"}'
curl -s $API/jobs/<job_id> -H "$AUTH"            # queued → running → succeeded
curl -s $API/documents/<document_id> -H "$AUTH"  # status: ready

# 4. 질문: 각주가 달린 답변
curl -s -X POST $API/questions -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"question": "회사는 리스부채를 어떻게 측정하나요?"}'

# 5. 검토 (reviewer/admin)
curl -s "$API/reviews?status=pending_review" -H "$AUTH"
curl -s -X POST $API/reviews/<answer_id> -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"decision": "approve", "comment": "근거 확인"}'

# 6. 감사 로그와 사용량 (admin)
curl -s "$API/audit-logs?action=answer.created&limit=20" -H "$AUTH"
curl -s "$API/usage?from=2026-11-01T00:00:00Z" -H "$AUTH"
```

### 중복 가져오기

같은 조직이 같은 `rcept_no`를 다시 요청하면 **새 문서나 작업을 만들지 않고 `200`** 과 기존 문서를 돌려줍니다(`"duplicate": true`, 기존 `job_id` 포함). 처음 요청은 `202`입니다. `UNIQUE(org_id, source, source_ref)` 제약이 동시 요청에서도 하나만 만들어지게 보장합니다. 다른 조직은 같은 공시를 독립적으로 가져올 수 있습니다.

### 주요 엔드포인트

| 메서드 | 경로 | 권한 |
| --- | --- | --- |
| POST | `/auth/token` | 공개 |
| GET | `/users/me` | 모든 역할 |
| POST | `/users` | admin (같은 조직) |
| POST | `/documents/dart` | member 이상 |
| POST | `/documents/upload` (TXT/MD, 5MB) | member 이상 |
| GET | `/documents`, `/documents/{id}`, `/jobs/{id}` | member 이상 |
| POST | `/questions`, `/search` | member 이상 |
| GET | `/answers/{id}` | member는 `published`/`approved`만, reviewer·admin은 전부 |
| GET/POST | `/reviews`, `/reviews/{answer_id}` | reviewer, admin |
| GET | `/audit-logs`, `/usage` | admin |

토큰이 없거나 잘못되면 `401`, 역할이 부족하면 `403`, 다른 조직의 리소스는 `404`입니다. 전체 스키마는 `http://localhost:8000/docs`(Swagger)에서 볼 수 있습니다.

## CLI

```bash
python -m footnote_rag.cli seed --org "Demo" --admin-email ... --admin-password ...
python -m footnote_rag.cli dart-find --stock-codes 005930,035420,005380 --from 20260101 --to 20260531 [--detail F001]
python -m footnote_rag.cli dart-fetch --rcept-no <rcept_no>      # 원문 ZIP 캐시 + 목차 출력(파서 확인용)
python -m footnote_rag.cli dart-import --org-admin-email ... --rcept-no <rcept_no>[,<rcept_no>...]
```

`dart-import`는 API와 같은 서비스 함수(`documents.service.request_dart_import`)로 작업을 등록합니다.

## Eval

```bash
python -m footnote_rag.evals.validate            # evidence_quote가 파서 출력에 그대로 있는지 검사
python -m footnote_rag.evals.run                 # Fake 모드(기본): 비용 없음
python -m footnote_rag.evals.run --live          # 실제 제공자: 비용 발생
```

리포트는 `evals/reports/{timestamp}-{mode}.md`로 저장됩니다(검색 recall@k, 인용 정확도, 답하지 말아야 할 질문의 거절 비율, 평균 비용·지연시간). CI에서는 돌리지 않습니다. 현재 `evals/dataset.jsonl`에는 형식 예시(`_example: true`)만 있습니다. 이유는 [docs/data-sources.md](docs/data-sources.md)를 보세요.

## MCP 서버 (Claude Code 연결)

MCP 서버는 DB에 직접 접근하지 않고 HTTP API를 사용자 토큰으로 호출하므로, 같은 권한 검사와 조직 격리가 적용됩니다. 도구는 `search_documents(query, k)`와 `ask(question)`입니다.

```bash
claude mcp add footnote-rag \
  --env FOOTNOTE_API_URL=http://localhost:8000 \
  --env FOOTNOTE_API_TOKEN=<POST /auth/token 으로 받은 토큰> \
  -- uv --directory /path/to/footnote-rag run python -m footnote_rag.mcp_server
```

또는 프로젝트의 `.mcp.json`:

```json
{
  "mcpServers": {
    "footnote-rag": {
      "command": "uv",
      "args": ["--directory", "/path/to/footnote-rag", "run", "python", "-m", "footnote_rag.mcp_server"],
      "env": {"FOOTNOTE_API_URL": "http://localhost:8000", "FOOTNOTE_API_TOKEN": "<token>"}
    }
  }
}
```

토큰은 만료(기본 60분)되므로 장기 사용 시 다시 발급해야 합니다.

## 문서

- [docs/architecture.md](docs/architecture.md): 구성도, ERD, 질문 처리 시퀀스, 작업 상태 전이
- [docs/adr/](docs/adr/): 기술 결정 기록
- [docs/data-sources.md](docs/data-sources.md): 사용한 공시와 수집 방법
- [docs/ai-usage.md](docs/ai-usage.md): AI 활용 내역(초안)
- [docs/study/README.md](docs/study/README.md): 학습 자료. 코드를 읽는 순서, 요청 흐름 해설, 설계 이유·대안·한계, 면접 Q&A, 용어집, 실습
