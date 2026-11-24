# footnote-rag

실제 DART 공시(상장사 사업보고서 `A001` 기본, 감사보고서 `F001`/`F002`)를 접수번호(`rcept_no`)로 가져와, 질문에 **문장마다 각주 `[1]`, `[2]`** 를 달아 답하는 백엔드 서비스입니다.

- 각주에는 문서 제목, 회사명, 목차 경로(예: `III. 재무에 관한 사항 > 3. 연결재무제표 주석 > 16. 우발부채와 약정사항 (연결)`), 인용문, DART 뷰어 링크가 들어갑니다.
- 상장사는 감사받은 재무제표·주석을 **사업보고서(A001)** 에 넣어 공시하므로 A001이 기본 검색 대상입니다(상장사는 F001/F002 조회 결과가 0건). 이유는 [docs/data-sources.md](docs/data-sources.md)와 [ADR 0006](docs/adr/0006-annual-report-a001.md)을 보세요.
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

Fake 모드의 OpenDART는 `가람전자(합성)` 같은 **합성 회사**의 합성 사업보고서(A001, 접수번호 `20260310900101`~`20260310900103`)와 합성 감사보고서(F001, `20260310900001`~`20260310900003`)를 돌려줍니다. 실제 공시가 아닙니다.

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

## 웹 화면으로 테스트하기

FastAPI가 빌드 없는 단일 페이지 화면(`src/footnote_rag/web/static/`의 HTML·JS·CSS 3개)을 `/ui`에서 직접 서빙합니다. 화면은 기존 HTTP API를 그대로 호출하므로 권한 검사와 조직 격리도 그대로 적용됩니다. 역할에 따라 탭을 숨기는 것은 편의 기능일 뿐이고, 권한 판단은 서버가 합니다.

1. 스택을 띄우고 첫 관리자를 만듭니다.

   bash:

   ```bash
   docker compose up --build -d
   curl http://localhost:8000/readyz
   docker compose exec api python -m footnote_rag.cli seed \
     --org "Demo" --admin-email admin@example.com --admin-password 'change-me-please'
   ```

   PowerShell:

   ```powershell
   docker compose up --build -d
   Invoke-RestMethod http://localhost:8000/readyz
   docker compose exec api python -m footnote_rag.cli seed `
     --org "Demo" --admin-email admin@example.com --admin-password 'change-me-please'
   ```

2. 브라우저에서 http://localhost:8000/ui 를 엽니다(`http://localhost:8000/`도 `/ui/`로 리다이렉트). 위 계정으로 로그인합니다. 토큰은 탭을 닫으면 사라지는 `sessionStorage`에만 저장됩니다.
3. **문서** 탭: 종목코드(프리셋: 삼성전자 005930, NAVER 035420, 현대자동차 005380)·기간·보고서 종류(기본 A001 사업보고서, F001/F002 감사보고서)로 검색하고 결과 행의 **가져오기**를 누릅니다. F001/F002 결과가 0건이면 "상장사는 감사보고서를 사업보고서(A001)에 포함해 공시합니다."라는 안내가 나옵니다. 새 문서는 202, 이미 가져온 문서는 200으로 구분해 안내합니다. 아래 "내 문서" 표는 `queued`/`processing` 문서가 있는 동안 3초마다 새로고침되고, `failed`면 오류 내용을 보여 줍니다. admin에게는 `ready`/`failed` 문서 옆에 **다시 처리** 버튼이 보입니다(확인 대화상자 → 재처리 작업 등록 → 자동 새로고침 재시작).
   - Fake 모드(OpenDART 키 없음)에서는 상단에 "Fake 모드(합성 데이터)" 배지가 뜨고, 합성 종목코드 `990001`~`990003`만 검색됩니다. 접수번호 직접 입력란에 `20260310900101`(합성 사업보고서)을 넣어 가져와도 됩니다.
4. **질문** 탭: `ready` 문서를 고르고(안 고르면 전체) 질문합니다. 답변 본문의 `[n]`을 누르면 아래 각주(회사명·문서 제목·목차 경로·인용문·DART 원문 링크)가 강조됩니다. 상태 배지와 토큰·비용·지연시간도 표시됩니다. 이번 세션의 질문은 오른쪽 목록에 남습니다(새로고침하면 사라짐).
5. **검토** 탭(reviewer·admin): 검토 대기 답변을 승인·반려합니다. 다른 검토자가 먼저 처리했다면 409를 받아 "이미 다른 검토자가 처리함"을 알리고 목록을 새로고침합니다. 검토 대기 답변을 확실히 만들어 보려면 `.env`에 `REVIEW_MODE=always`를 넣고 다시 띄웁니다(모든 답변이 검토 대기로 감).
6. **관리** 탭(admin): 사용량(기본 최근 7일), 감사 로그 최근 50건(action 필터), 사용자 추가 폼이 있습니다. member·reviewer 계정을 만들어 다시 로그인하면 역할별로 보이는 탭이 달라지는 것을 확인할 수 있습니다.

`/ui` 응답에는 `Content-Security-Policy`(`script-src 'self'` 등 외부 리소스 차단), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` 헤더가 붙습니다. Swagger(`/docs`)는 CDN 리소스를 쓰므로 이 CSP를 적용하지 않습니다.

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
| `EMBED_BATCH_MAX_TOKENS` | `100000` | 요청 하나의 추정 토큰(문자 수) 상한. `EMBED_TPM_LIMIT`가 있으면 `min(이 값, EMBED_TPM_LIMIT)` |
| `EMBED_RPM_LIMIT` | 없음(제한 없음) | 클라이언트 측 분당 요청 수 제한(60초 슬라이딩 윈도우, **워커 프로세스별**) |
| `EMBED_TPM_LIMIT` | 없음(제한 없음) | 클라이언트 측 분당 추정 토큰 제한(워커 프로세스별). 결제 수단 미등록 Voyage 계정은 `9000` 권장 |
| `EMBED_BATCH_MAX_RETRIES` | `6` | 429·일시 오류 시 **같은 배치만** 재시도하는 횟수. 다 쓰면 작업 단위 재시도로 넘김 |
| `EMBED_RETRY_BASE_SECONDS` / `EMBED_RETRY_MAX_SECONDS` | `20` / `120` | 배치 재시도 대기 = `min(BASE × 2^n, MAX)` + 지터(최대 10%) |
| `TOP_K` | `8` | 검색 청크 수 |
| `MIN_RETRIEVAL_SCORE` | `0.35` | 최고 유사도가 이보다 낮으면 검토 대기 |
| `REVIEW_MODE` | `on_low_confidence` | `always`면 모든 답변을 검토 대기로 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1500` / `200` | 문자 기준 |
| `JOB_MAX_ATTEMPTS` | `5` | 작업 최대 시도 횟수 |
| `BASE_BACKOFF_SECONDS` | `5` | 재시도 지연 = `2^attempts × BASE` |
| `JOB_LOCK_TIMEOUT_SECONDS` | `600` | 이보다 오래된 `running` 작업은 `queued`로 복구 |
| `DATA_DIR` | `data` | DART ZIP·corpCode 캐시, 업로드 파일(gitignore) |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | structlog |

### Voyage 레이트 리밋

Voyage 계정에 **결제 수단을 등록하지 않으면 한도가 3 RPM / 10K TPM**으로 낮아집니다. 이 상태에서 사업보고서(약 100만 자)를 기본 설정으로 가져오면 요청마다 429가 납니다. 다음과 같이 설정하세요.

```bash
EMBED_RPM_LIMIT=3
EMBED_TPM_LIMIT=9000   # 10K보다 약간 낮게 잡아 여유를 둠
```

- 워커는 보내기 전에 한도를 넘지 않을 때까지 기다립니다. 그래도 429가 나면 그 배치만 백오프로 다시 보냅니다. 이미 임베딩한 청크는 `embedding_cache`에 저장되므로 작업이 실패했다가 재시도하거나 재처리해도 다시 비용을 내지 않습니다.
- **소요 시간 계산**: 대략 `총 추정 토큰 ÷ EMBED_TPM_LIMIT` 분입니다. 토큰은 문자 수로 추정합니다. 예를 들어 100만 자 사업보고서는 `1,000,000 ÷ 9,000 ≈ 111분`입니다. 진행률은 웹 화면 문서 목록(`임베딩 120/700`)과 `GET /documents/{id}`·`GET /jobs/{id}`의 `progress`에서 볼 수 있습니다.
- 429 메시지에 결제 수단 안내가 있으면 워커 로그에 `embedding.provider_reduced_limits` 경고가 한 번 남습니다. 최종 실패하면 문서 오류에 `임베딩 제공자 요청 한도 초과(결제 수단 미등록 가능성)`이 기록됩니다.
- 결제 수단을 등록하면 한도가 올라갑니다. 등록해도 **무료 토큰은 그대로 유지**되므로 무료 범위 안에서는 비용이 나가지 않습니다. 등록한 뒤에는 `EMBED_RPM_LIMIT`·`EMBED_TPM_LIMIT`를 비워 두면 됩니다.
- 이 제한은 **워커 프로세스마다 따로** 적용됩니다. 워커를 여러 개 띄우면 한도를 워커 수로 나눠 설정하세요(예: 워커 2개 → `EMBED_RPM_LIMIT=1`, `EMBED_TPM_LIMIT=4500`).

배경과 대안은 [ADR 0007](docs/adr/0007-embedding-rate-limit-and-cache.md)을 보세요.

## curl 예시: 로그인 → DART 가져오기 → 질문

```bash
API=http://localhost:8000

# 1. 로그인 (OAuth2 password flow)
TOKEN=$(curl -s -X POST $API/auth/token \
  -d 'username=admin@example.com' -d 'password=change-me-please' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# 2. 접수번호 찾기 (실제 키 모드) — 삼성전자·NAVER·현대자동차 감사보고서
docker compose exec api python -m footnote_rag.cli dart-find \
  --stock-codes 005930,035420,005380 --from 20260101 --to 20260531 --detail A001
# 같은 검색을 API로: {mode, reports:[...], not_found:[...]} (detail을 빼면 A001)
curl -s "$API/dart/reports?stock_codes=005930,035420,005380&date_from=20260101&date_to=20260531&detail=A001" -H "$AUTH"

# 3. DART 가져오기: 202 {document_id, job_id}
curl -s -X POST $API/documents/dart -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"rcept_no": "20260310900001"}'
curl -s $API/jobs/<job_id> -H "$AUTH"            # queued → running → succeeded
curl -s $API/documents/<document_id> -H "$AUTH"  # status: ready

# 3-1. 파서가 바뀐 뒤 다시 처리 (admin): 202 {document_id, job_id}, 처리 중이면 409
curl -s -X POST $API/documents/<document_id>/reprocess -H "$AUTH"

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
| GET | `/ui` (웹 화면, `/`는 `/ui/`로 리다이렉트) | 공개(화면 안의 API 호출은 각 권한 적용) |
| POST | `/auth/token` | 공개 |
| GET | `/users/me` | 모든 역할 |
| GET | `/dart/reports?stock_codes=&date_from=&date_to=&detail=` (종목코드로 보고서 검색. `detail`은 `A001`(기본)·`F001`·`F002`, 그 밖의 값은 422) | member 이상 |
| POST | `/users` | admin (같은 조직) |
| POST | `/documents/dart` | member 이상 |
| POST | `/documents/upload` (TXT/MD, 5MB) | member 이상 |
| GET | `/documents`, `/documents/{id}`, `/jobs/{id}` (문서 응답에 최신 작업의 `progress`·`next_retry_at` 추가) | member 이상 |
| POST | `/documents/{id}/reprocess` (다시 파싱·임베딩. 202 `{document_id, job_id}`, `queued`/`processing`이면 409, 감사 로그 `document.reprocess_requested`) | admin (같은 조직) |
| POST | `/questions`, `/search` | member 이상 |
| GET | `/answers/{id}` | member는 `published`/`approved`만, reviewer·admin은 전부 |
| GET/POST | `/reviews`, `/reviews/{answer_id}` | reviewer, admin |
| GET | `/audit-logs`, `/usage` | admin |

토큰이 없거나 잘못되면 `401`, 역할이 부족하면 `403`, 다른 조직의 리소스는 `404`입니다. 전체 스키마는 `http://localhost:8000/docs`(Swagger)에서 볼 수 있습니다.

## CLI

```bash
python -m footnote_rag.cli seed --org "Demo" --admin-email ... --admin-password ...
python -m footnote_rag.cli dart-find --stock-codes 005930,035420,005380 --from 20260101 --to 20260531 [--detail A001|F001|F002]   # 기본 A001
python -m footnote_rag.cli dart-fetch --rcept-no <rcept_no>      # 원문 ZIP 캐시 + 목차 출력(파서 확인용)
python -m footnote_rag.cli dart-import --org-admin-email ... --rcept-no <rcept_no>[,<rcept_no>...]
python -m footnote_rag.cli dart-reprocess --org-admin-email ... (--rcept-no <rcept_no>[,<rcept_no>...] | --all)
```

`dart-import`는 API와 같은 서비스 함수(`documents.service.request_dart_import`)로, `dart-reprocess`는 `POST /documents/{id}/reprocess`와 같은 `documents.service.request_reprocess`로 작업을 등록합니다. `--all`은 그 조직의 DART 문서 전부이며 `queued`/`processing` 문서는 건너뜁니다. 원문 ZIP 캐시(`data/dart/{rcept_no}.zip`)를 재사용합니다.

> **PowerShell 주의:** 쉼표로 구분한 종목코드·접수번호는 따옴표로 감싸세요: `--stock-codes "005930,035420,005380"`. 따옴표가 없으면 PowerShell이 쉼표를 배열로 해석해 숫자로 바꾸면서 앞자리 0이 사라집니다(`005930` → `5930`).

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
