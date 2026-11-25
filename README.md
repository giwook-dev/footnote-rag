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
| 외부 연동 | OpenDART(`corpCode.xml`, `list.json`, `document.xml`), Voyage `voyage-4` 임베딩, Claude `claude-sonnet-5-5`, 선택: 호스트의 Ollama(로컬 LLM) |
| 답변 | `search_result` 블록 + Citations → 각주 변환, 서버 측 거절 fallback. 로컬 LLM은 번호 붙은 근거 + JSON 스키마 출력 → 서버 검증 → 같은 각주 변환 |
| 운영 | structlog JSON 로그, `X-Request-ID`, `/healthz`, `/readyz`, Docker Compose, GitHub Actions CI |
| 품질 | pytest(실제 Postgres, 외부 네트워크 없음), Eval 실행기, MCP 서버 |

설계 근거는 [docs/architecture.md](docs/architecture.md)와 [docs/adr/](docs/adr/)에 있습니다.

## Eval 결과 (실측)

**데이터**: 실제 2025 사업연도 사업보고서(A001) 3건(삼성전자 `20260310002820`, NAVER `20260313001021`, 현대자동차 `20260318001394`)으로 만든 25문항([`evals/dataset.jsonl`](evals/dataset.jsonl)). 회사별 5~6문항(삼성전자 6, NAVER 6, 현대자동차 5), 두 회사 이상 비교 4문항, 문서에 근거가 없어 답하면 안 되는 질문 4문항입니다. 답할 수 있는 문항의 근거 인용문(`evidence_quote`)이 이 저장소 파서가 만든 원문 텍스트의 기대 목차 아래에 그대로 있는지 `python -m footnote_rag.evals.validate`로 확인했습니다(지어낸 인용문이면 실패).

**측정 결과**: 2026-11-24, `python -m footnote_rag.evals.run --live`, 검색 상위 k=8. 로컬 모델은 Ollama `qwen3:14b`를 AMD RX 9070 XT(16GB)에서 실행했습니다. 리포트: [evals/reports/20261124T160504Z-live-local-qwen3-14b.md](evals/reports/20261124T160504Z-live-local-qwen3-14b.md)

| 지표 | 로컬 `ollama:qwen3:14b` | Claude `claude-sonnet-5-5` |
| --- | --- | --- |
| 검색 recall@8 (답할 수 있는 21문항) | 95.2% | 미측정 |
| 인용 정확도 (기대 목차를 가리키는 각주 비율) | 61.3% | 미측정 |
| 답할 수 없는 질문 거절 (4문항) | 100% | 미측정 |
| 답할 수 있는데 거절 (리포트 문항 표 기준) | 2/21 (`compare-02`, `compare-04`) | 미측정 |
| 평균 지연 / 질문 | 11,639 ms | 미측정 |
| 비용 | $0 (로컬) | 미측정 |

Claude는 API 크레딧이 없어 아직 돌리지 않았습니다. 크레딧을 충전한 뒤 `LLM_PROVIDER=real`로 같은 명령을 실행하면 같은 형식의 리포트(`…-live-real-claude-sonnet-5-5.md`)가 생겨 나란히 비교할 수 있습니다.

**해석**

- **검색은 양호합니다.** 답할 수 있는 21문항 중 기대 목차가 상위 8개 검색 결과에 없었던 문항은 `naver-02` 하나입니다.
- **약점은 모델이 근거 번호를 고르는 단계입니다.** `samsung-04`, `naver-01`, `naver-04`, `hyundai-03`은 기대 목차가 검색됐는데도 각주가 모두 다른 목차를 가리켰고, 비교 질문 `compare-03`은 각주 5개 중 1개만 맞았습니다. 로컬 모델은 Citations API 없이 근거 번호를 직접 고릅니다([ADR 0008](docs/adr/0008-local-llm-ollama.md)). 다음 실행부터는 리포트의 "Citation diagnostics" 표(기대 목차, 검색 상위 k의 경로·유사도, 모델이 인용한 경로)로 문항별 원인을 바로 확인할 수 있습니다.
- 비교 질문 `compare-02`, `compare-04`는 검색은 성공했지만 검토 대기(`pending_review`)로 가서 답하지 않았습니다. 리포트의 새 지표 "answerable answered rate"·"false abstentions"가 이 경우를 따로 셉니다.
- **파서 수정의 효과**: 원문 XML 오류 문자를 정리하기 전에는 NAVER 보고서가 11개, 현대차 보고서가 6개 섹션으로 무너져 한 섹션에 수십만 자가 들어갔고, 목차 경로가 근거 위치를 가리키지 못했습니다. 수정 후 삼성전자 110개, NAVER 117개, 현대자동차 125개 섹션으로 나뉩니다([docs/data-sources.md](docs/data-sources.md)).
- **재측정과 진단(2026-11-25)**: 같은 로컬 모델로 다시 측정했습니다([evals/reports/20261125T020155Z-live-local-qwen3-14b.md](evals/reports/20261125T020155Z-live-local-qwen3-14b.md)). recall@8 95.2%, 인용 정확도 61.3%(19/31, 검색 성공 문항만 63.3%), answered 90.5%(19/21, false abstention `compare-02`·`compare-04`), 답할 수 없는 질문 거절 100%, p50 12,132 ms / p95 25,120 ms입니다. Citation diagnostics를 보면 **틀린 각주 12개 중 9개는 기대한 연결재무제표 주석과 같은 주석을 별도재무제표 주석에서 인용한 경우**였습니다(예: 기대 `3. 연결재무제표 주석 > 12. 차입금 (연결)` / 인용 `5. 재무제표 주석 > 12. 차입금`). 대부분 기대 섹션도 검색 1~4위 안에 있었지만 별도 주석 청크가 더 높은 순위였습니다. 실제 오류는 3개(`naver-02` 검색 실패, `naver-04`·`hyundai-03` 다른 주석 인용)입니다. 그래서 질문이 별도를 지정하지 않으면 연결 주석을 우선하도록 했습니다. 검색은 후보를 넓게 가져와 대응하는 별도 청크를 뒤로 보내고(`PREFER_CONSOLIDATED`), 두 시스템 프롬프트에는 연결 주석 우선 규칙과 근거 제목의 `[연결]`/`[별도]` 표시를 넣었습니다. Eval에는 기존 엄격 지표를 그대로 두고, 대응 주석을 인정하는 보조 지표와 거절 문항 진단을 추가했습니다([ADR 0009](docs/adr/0009-prefer-consolidated-notes.md)). 위 표의 수치는 이 변경 전 측정값이며, 변경 후 수치는 다시 측정해 갱신합니다.
- 2026-11-24 리포트의 "Import failures"(`20260318001394: processing None`)는 Eval과 compose의 `worker` 서비스가 같은 큐를 처리하면서 생긴 오판입니다. 현대차 문항은 정상 채점됐습니다. 지금은 Eval이 등록한 문서가 모두 `ready`/`failed`가 될 때까지 기다립니다([Eval](#eval)).

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

## 로컬 LLM(Ollama)로 실행하기

Anthropic API 크레딧 없이 실제 답변을 만들어 보려면 호스트에서 [Ollama](https://ollama.com/)를 돌리고 `LLM_PROVIDER=local`로 설정합니다. 임베딩은 그대로 Voyage를 쓰므로 실제 공시를 가져오려면 `VOYAGE_API_KEY`가 필요합니다. Fake 임베딩(키 없음)으로도 합성 공시에 대한 로컬 답변은 시험할 수 있습니다.

1. **Ollama 설치와 모델 다운로드** (호스트, Docker 밖)

   ```bash
   # Windows/macOS: https://ollama.com/download 설치 프로그램, Linux: 공식 설치 스크립트
   ollama pull qwen3:14b        # 약 9GB (4bit 양자화)
   ollama list                  # qwen3:14b 가 보이면 준비 완료
   ```

2. **`.env` 설정**

   ```bash
   LLM_PROVIDER=local
   # 기본값이면 생략 가능: LOCAL_LLM_BASE_URL=http://host.docker.internal:11434, LOCAL_LLM_MODEL=qwen3:14b
   ```

   `auto`는 `local`을 고르지 않으므로 반드시 명시합니다. API 키는 필요 없습니다. uvicorn을 컨테이너가 아니라 호스트에서 직접 띄우면 `LOCAL_LLM_BASE_URL=http://localhost:11434`로 바꿉니다.

3. **스택 실행**

   ```bash
   docker compose up -d
   ```

   api 서비스에는 `extra_hosts: host.docker.internal:host-gateway`가 있어 Linux에서도 호스트의 Ollama에 닿습니다(Docker Desktop은 원래 이 이름을 제공). 워커는 LLM을 호출하지 않습니다. Ollama가 다른 PC나 WSL 밖에서 돈다면 `OLLAMA_HOST=0.0.0.0`으로 띄워야 컨테이너에서 접속됩니다.

4. **연결 확인**: 웹 화면 **관리** 탭의 "LLM 상태" 카드나 `GET /llm/status`(admin)가 제공자·모델·연결 여부·모델 존재 여부를 보여 줍니다. 모델이 없으면 `ollama pull` 안내가 나옵니다. `/readyz`는 LLM 상태와 무관합니다.

5. **GPU 사용 확인**: 질문을 한 번 보낸 뒤

   ```bash
   ollama ps     # PROCESSOR 열이 "100% GPU"면 GPU에 모두 올라감, "CPU"가 섞이면 VRAM 부족 또는 GPU 미지원
   ```

   AMD GPU는 Ollama가 ROCm(Linux)·Windows용 AMD 드라이버로 지원하며, **지원하는 GPU 목록이 Ollama 버전마다 다릅니다.** RX 9000 시리즈 같은 최신 GPU는 최신 Ollama와 최신 AMD 드라이버가 필요할 수 있으니 설치한 버전의 Ollama 공식 문서(GPU 지원 목록)에서 확인하세요. GPU를 못 쓰면 CPU로 돌아가며 매우 느립니다.

**VRAM별 모델 예시** (4bit 양자화, `LOCAL_LLM_NUM_CTX=16384` 기준 대략치)

| VRAM | 모델 예시 | 비고 |
| --- | --- | --- |
| 8GB | `qwen3:8b`, `llama3.1:8b` | 컨텍스트를 줄이면(`LOCAL_LLM_NUM_CTX=8192`) 여유가 생김 |
| 12GB | `qwen3:8b`, `gemma3:12b` | 한국어 품질은 모델별로 직접 비교 권장 |
| 16GB | `qwen3:14b`(기본) | RX 9070 XT(16GB)에서 GPU에 모두 올라가는 크기 |

모델을 바꾸면 `LOCAL_LLM_MODEL`만 바꾸고 `ollama pull <모델>` 후 api를 다시 띄웁니다(`docker compose up -d api`).

**동작 방식과 알려진 한계**

- 로컬 모델에는 Claude의 Citations API가 없습니다. 검색 결과에 `[1]`, `[2]` 번호를 붙여 보내고, Ollama의 JSON 스키마 강제 출력으로 문장마다 근거 번호를 받습니다. 서버가 번호를 검증하고(범위 밖 번호는 버리고 `llm_calls.dropped_source_refs`에 기록) Claude와 같은 각주·신뢰도·검토 흐름에 태웁니다.
- **인용 정확도**: 근거 번호는 모델이 고르므로 문장과 근거가 맞지 않을 수 있습니다. 인용문(`cited_text`)은 서버가 청크에서 가장 비슷한 문장을 고른 **근사값**입니다. 각주에 `citation_method: "model_reference"`가 붙고, 화면에 "로컬 모델이 지정한 근거 — 인용문은 근사 매칭"이 표시됩니다. Claude 답변은 `api_citation`입니다.
- **속도**: GPU와 모델 크기에 따라 질문 하나에 수십 초가 걸릴 수 있습니다. `LOCAL_LLM_TIMEOUT_SECONDS`(기본 180초)를 넘으면 504입니다. 첫 질문은 모델을 VRAM에 올리느라 더 느립니다.
- 출력이 `LOCAL_LLM_MAX_TOKENS`에 걸리면 완성된 문장만 남기고 `pending_review`(`truncated`)로 보냅니다. JSON이 깨지면 한 번 다시 요청하고, 또 실패하면 502입니다.
- Ollama가 꺼져 있으면 503("로컬 LLM 서버(Ollama)에 연결할 수 없습니다"), 모델이 없으면 503("모델 … 이 없습니다: ollama pull …")입니다.
- 비용은 `ollama:<모델>` 이름으로 0원 기록됩니다. Claude와 품질을 비교하려면 `python -m footnote_rag.evals.run --live`를 제공자별로 돌려 리포트를 비교합니다([Eval](#eval)). 로컬 모델끼리는 한 번에 비교할 수 있습니다([로컬 모델 비교](#로컬-모델-비교)).
- **임베딩 비용**: 답변은 로컬이어도 질문 임베딩은 여전히 Voyage를 씁니다. 다만 문서 청크 임베딩은 캐시되므로(`embedding_cache`) 같은 공시를 다시 가져와도 청크는 다시 임베딩하지 않고, Voyage 호출은 질문 수만큼만 일어납니다.

**think 수준**: `LOCAL_LLM_THINK`는 `false`/`true` 외에 `low`/`medium`/`high`도 받아 Ollama `think` 필드에 그대로 넣습니다. gpt-oss 계열은 bool이 아니라 수준 문자열을 받습니다(예: `LOCAL_LLM_THINK=low`).

**로컬 상향(cascade)**: `LOCAL_LLM_FALLBACK_MODEL`을 설정하면 "단순한 질문은 작은 모델, 애매한 답만 큰 모델"로 동작합니다. primary(`LOCAL_LLM_MODEL`) 답이 `refusal`·`truncated`·`no_citations`·`ungrounded_number`에 걸리거나 JSON이 두 번 깨지면(502) 같은 근거로 fallback 모델에 **한 번** 더 묻고 그 답을 씁니다. 검색 사유(`low_retrieval_score`, `no_retrieval_results`), `REVIEW_MODE=always`, 503(연결 실패·모델 없음)·504(시간 초과)는 모델을 바꿔도 나아지지 않으므로 상향하지 않습니다. 두 호출 모두 `llm_calls`에 같은 답변 id로 남고, `question.asked` 감사 로그에 `escalated`·`escalation_reasons`가 기록됩니다. 질문 API 응답 모양은 그대로입니다. 관리 탭 "LLM 상태" 카드와 `GET /llm/status`의 `fallback` 필드가 fallback 모델의 연결·존재 여부를 보여 줍니다.

```bash
LLM_PROVIDER=local
LOCAL_LLM_MODEL=qwen3.5:9b
LOCAL_LLM_FALLBACK_MODEL=qwen3:14b     # 비우면 상향 끔(기본)
# LOCAL_LLM_FALLBACK_THINK=low         # 비우면 LOCAL_LLM_THINK와 같음
```

VRAM 16GB에서는 두 모델을 동시에 올리기 어려워 상향할 때 모델 교체 시간이 더 듭니다. 메모리가 충분하면 Ollama를 `OLLAMA_MAX_LOADED_MODELS=2`로 띄워 둘 다 올려 둘 수 있습니다.

**숫자 근거 검증**: `NUMBER_GROUNDING_CHECK=review`(기본)이면 답변 문장의 숫자(금액·비율)가 그 문장이 인용한 청크 원문에 없을 때 `pending_review`(`ungrounded_number`)로 보냅니다. 쉼표는 무시하고, 1~2자리 정수(번호·개수)와 질문에 나온 숫자는 검사하지 않습니다. 단위 환산은 하지 않으므로 모델이 `1,234,567백만원`을 `약 1.2조 원`으로 바꿔 쓰면 일부러 걸립니다(계산은 모델이 아니라 코드가 해야 한다는 원칙). Claude와 로컬 답변 모두에 적용되고, `off`로 끌 수 있습니다.

설계 이유와 대안은 [ADR 0008](docs/adr/0008-local-llm-ollama.md)과 [ADR 0009(로컬 상향·숫자 검증)](docs/adr/0009-local-cascade-and-number-grounding.md)을 보세요.

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
4. **질문** 탭: `ready` 문서를 고르고(안 고르면 전체) 질문합니다. 답변 본문의 `[n]`을 누르면 아래 각주(회사명·문서 제목·목차 경로·인용문·DART 원문 링크)가 강조됩니다. 상태 배지와 토큰·비용·지연시간·모델명도 표시됩니다. 로컬 LLM 답변이면 각주 위에 "로컬 모델이 지정한 근거 — 인용문은 근사 매칭"이 표시됩니다. 이번 세션의 질문은 오른쪽 목록에 남습니다(새로고침하면 사라짐).
5. **검토** 탭(reviewer·admin): 검토 대기 답변을 승인·반려합니다. 다른 검토자가 먼저 처리했다면 409를 받아 "이미 다른 검토자가 처리함"을 알리고 목록을 새로고침합니다. 검토 대기 답변을 확실히 만들어 보려면 `.env`에 `REVIEW_MODE=always`를 넣고 다시 띄웁니다(모든 답변이 검토 대기로 감).
6. **관리** 탭(admin): LLM 상태(제공자·모델·연결 여부), 사용량(기본 최근 7일), 감사 로그 최근 50건(action 필터), 사용자 추가 폼이 있습니다. member·reviewer 계정을 만들어 다시 로그인하면 역할별로 보이는 탭이 달라지는 것을 확인할 수 있습니다.

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
| `LLM_PROVIDER` / `EMBEDDER` / `DART_PROVIDER` | `auto` | `auto` \| `fake` \| `real`. `LLM_PROVIDER`만 `local`(호스트 Ollama, 키 불필요)도 받음. `auto`는 `local`을 고르지 않음 |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | 날짜 접미사 없는 모델 ID |
| `CLAUDE_EFFORT` | `medium` | `output_config.effort` |
| `CLAUDE_MAX_TOKENS` | `16000` | 비스트리밍 요청 상한 |
| `LOCAL_LLM_BASE_URL` | `http://host.docker.internal:11434` | `LLM_PROVIDER=local`일 때 Ollama 네이티브 API 주소. 호스트에서 uvicorn을 직접 띄우면 `http://localhost:11434` |
| `LOCAL_LLM_MODEL` | `qwen3:14b` | Ollama 모델 이름(미리 `ollama pull`). `llm_calls.model`에는 `ollama:<이름>`으로 저장, 비용 0 |
| `LOCAL_LLM_NUM_CTX` | `16384` | 컨텍스트 길이(`options.num_ctx`). 작으면 프롬프트 앞부분이 조용히 잘림 |
| `LOCAL_LLM_MAX_TOKENS` | `2048` | 출력 토큰 상한(`options.num_predict`). 도달하면 `pending_review`(`truncated`) |
| `LOCAL_LLM_TIMEOUT_SECONDS` | `180` | 요청 응답 대기 시간(초). 넘으면 504 |
| `LOCAL_LLM_THINK` | `false` | Ollama `think`(추론 모델의 생각 단계). `false`/`true` 또는 수준 `low`/`medium`/`high`(gpt-oss 계열). 값을 그대로 보냄. 켜면 느려짐 |
| `LOCAL_LLM_TEMPERATURE` | `0` | `options.temperature` |
| `LOCAL_LLM_FALLBACK_MODEL` | 빈 값(상향 끔) | 로컬 상향 대상 모델. `LLM_PROVIDER=local`일 때만 사용. 상향 사유에 걸린 답만 이 모델로 한 번 더 물음([ADR 0009](docs/adr/0009-local-cascade-and-number-grounding.md)) |
| `LOCAL_LLM_FALLBACK_THINK` | 없음(`LOCAL_LLM_THINK`와 같음) | fallback 모델의 `think`. 값 형식은 `LOCAL_LLM_THINK`와 같음 |
| `NUMBER_GROUNDING_CHECK` | `review` | `review`면 인용 청크에 없는 숫자가 든 답변을 검토 대기(`ungrounded_number`)로. `off`면 검사 안 함 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `voyage-4` / `1024` | 차원은 마이그레이션 시점에 `vector(n)`으로 고정 |
| `EMBED_BATCH_MAX_TOKENS` | `100000` | 요청 하나의 추정 토큰(문자 수) 상한. `EMBED_TPM_LIMIT`가 있으면 `min(이 값, EMBED_TPM_LIMIT)` |
| `EMBED_RPM_LIMIT` | 없음(제한 없음) | 클라이언트 측 분당 요청 수 제한(60초 슬라이딩 윈도우, **워커 프로세스별**) |
| `EMBED_TPM_LIMIT` | 없음(제한 없음) | 클라이언트 측 분당 추정 토큰 제한(워커 프로세스별). 결제 수단 미등록 Voyage 계정은 `9000` 권장 |
| `EMBED_BATCH_MAX_RETRIES` | `6` | 429·일시 오류 시 **같은 배치만** 재시도하는 횟수. 다 쓰면 작업 단위 재시도로 넘김 |
| `EMBED_RETRY_BASE_SECONDS` / `EMBED_RETRY_MAX_SECONDS` | `20` / `120` | 배치 재시도 대기 = `min(BASE × 2^n, MAX)` + 지터(최대 10%) |
| `TOP_K` | `8` | 검색 청크 수 |
| `PREFER_CONSOLIDATED` | `true` | 같은 문서에 연결 주석과 그 대응 별도 주석이 둘 다 검색되면 별도 청크를 뒤로 보냄. 질문에 "별도"·"개별"이 있으면 반대. `false`면 유사도 순 `TOP_K`개 그대로([ADR 0009](docs/adr/0009-prefer-consolidated-notes.md)) |
| `RETRIEVAL_CANDIDATES` | `16` | `PREFER_CONSOLIDATED=true`일 때 재정렬 전에 가져오는 후보 수. 실제로는 `max(TOP_K, 이 값)` |
| `MIN_RETRIEVAL_SCORE` | `0.35` | 최고 유사도가 이보다 낮으면 검토 대기. 재정렬 전 후보의 최고 유사도와 비교 |
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
| GET | `/llm/status` (현재 LLM 제공자·모델·연결 여부. 로컬이면 Ollama `/api/tags`를 3초 타임아웃으로 확인) | admin |

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
python -m footnote_rag.evals.run --live          # 설정한 제공자(LLM_PROVIDER=real → Claude, local → Ollama)
python -m footnote_rag.evals.run --live --import-timeout 3600   # 가져오기 대기 상한(초, 기본 1800)
```

**Docker로 실행하기**: `evals/`는 이미지에 들어 있지 않으므로 볼륨으로 마운트합니다. `data/`(원문 ZIP 캐시)는 compose 설정이 이미 마운트합니다. `db`가 떠 있어야 합니다(`docker compose up -d`). 로컬 LLM이면 호스트의 Ollama에 닿도록 `extra_hosts`가 있는 `api` 서비스로 실행합니다.

bash:

```bash
docker compose run --rm --no-deps -v "$PWD/evals:/app/evals" api python -m footnote_rag.evals.validate
docker compose run --rm --no-deps -v "$PWD/evals:/app/evals" api python -m footnote_rag.evals.run --live
```

PowerShell:

```powershell
docker compose run --rm --no-deps -v "${PWD}/evals:/app/evals" api python -m footnote_rag.evals.validate
docker compose run --rm --no-deps -v "${PWD}/evals:/app/evals" api python -m footnote_rag.evals.run --live
```

출력을 파일로 남기려면 `… | tee evals/reports/live-run.log`(PowerShell은 `| Tee-Object evals/reports/live-run.log`)처럼 쓰세요. `evals/reports/*.log`는 커밋되지 않습니다.

실행기는 임시 조직을 만들어 데이터셋의 공시를 정상 서비스로 가져오기 등록하고, 자체 워커로 큐를 처리합니다. compose의 `worker` 서비스가 같은 DB에서 작업을 먼저 가져갈 수 있으므로, 큐가 비어도 **등록한 문서가 모두 `ready` 또는 `failed`가 될 때까지 2초 간격으로 DB를 확인하며 기다립니다**(그동안 자체 워커도 계속 큐를 처리하므로 서비스 워커가 없어도 됩니다). `--import-timeout`을 넘기면 그 문서는 "Import failures"에 `timeout(<상태>)`로 기록됩니다.

리포트는 `evals/reports/{timestamp}-{mode}[-{provider}]-{model}.md`로 저장됩니다(예: `20261124T120000Z-live-local-qwen3-14b.md`, `…-live-real-claude-sonnet-5-5.md`, `…-fake-fake-llm.md`). 머리말에 제공자·모델·`citation_method`(`api_citation` / `model_reference`), 검색 설정(`prefer_consolidated`, `retrieval_candidates`)과 문서별 가져오기 결과(섹션 수, 청크 수, 임베딩 토큰, 캐시 적중·미스. 값은 `jobs.result`)가 적혀 있어 Claude와 로컬 결과를 나란히 비교할 수 있습니다. 리포트 내용:

- **Metrics**: 검색 recall@k, 인용 정확도, 답하지 말아야 할 질문의 거절 비율, 평균 비용·지연시간, 그리고 answerable answered rate(답할 수 있는 질문 중 거절하지 않고 각주가 1개 이상인 비율), false abstentions(답할 수 있는데 거절한 문항 수와 id), 검색 성공 문항만으로 계산한 인용 정확도, 지연 p50/p95(nearest-rank). 보조 지표 citation accuracy (consolidated/separate counterpart accepted)는 기대 목차의 연결↔별도 대응 주석(예: `5. 재무제표 주석 > 12. 차입금` ↔ `3. 연결재무제표 주석 > 12. 차입금 (연결)`)을 인용한 각주도 맞음으로 셉니다(전체·검색 성공 문항). 엄격한 인용 정확도는 그대로입니다.
- **Metrics by question type**: 같은 지표를 문항 유형(id 접두어 `samsung`/`naver`/`hyundai`/`compare`/`unans`, 데이터셋에 `category` 필드가 있으면 그 값)별로 나눈 표. 끝의 두 열이 대응 주석 인정 정확도입니다.
- **False abstention diagnostics**: 답할 수 있는데 거절한 문항마다 답변 상태, `review_reason`(`no_citations` = 모델이 쓸 만한 근거를 내지 않음, `low_retrieval_score` = 신뢰도 규칙 등), 검색 상위 k개의 `section_path`·유사도.
- **Citation diagnostics**: 답할 수 있는 문항 중 각주가 하나라도 기대 목차 밖을 가리킨 문항마다 기대 목차 접두어, 검색 상위 k개의 `section_path`·유사도(기대 목차면 ✓), 모델이 인용한 `section_path`와 `counterpart` 열(대응 주석이면 ✓), `ungrounded numbers` 열(그 각주를 인용한 문장의 숫자 중 청크에 없는 것). "정답이 검색됐는데 다른 근거를 골랐다", "같은 주석의 별도재무제표 판을 인용했다"(separate-statement counterpart cited), "정답이 검색되지 않았다"를 구분합니다. 본문 텍스트는 넣지 않습니다.
- **ungrounded number answers**(Metrics)와 **Ungrounded number diagnostics**: 인용 청크에 없는 숫자가 하나라도 든 답변 수와 그 숫자 목록([숫자 근거 검증](#로컬-llmollama로-실행하기)).
- 한 문항에서 LLM 호출이 실패(`LLMError`)하면 실행을 멈추지 않고 그 문항을 `error` 상태(거절, 각주 0)로 기록합니다. 오류가 있으면 Metrics에 errors 행과 **Errors** 절이 붙습니다.
- Eval 안에서는 질문 임베딩을 질문 텍스트 기준으로 메모해 문항당 Voyage 질문 임베딩 호출이 한 번입니다.

CI에서는 돌리지 않습니다. 데이터셋 구성과 측정 결과는 위 [Eval 결과 (실측)](#eval-결과-실측)을 보세요.

### 로컬 모델 비교

API 비용 없이 같은 데이터셋(`evals/dataset.jsonl`, 25문항)을 여러 로컬 모델과 로컬 상향 구성에 돌려 정확도·지연을 한 리포트에서 비교합니다. `--live`가 필요하고, `LLM_PROVIDER` 값과 상관없이 Ollama 클라이언트만 만듭니다(Claude는 호출하지 않음). base_url·num_ctx·max_tokens·timeout·temperature는 `LOCAL_LLM_*` 설정을 씁니다.

```bash
# 필요한 모델을 먼저 받습니다(호스트)
ollama pull qwen3.5:9b
ollama pull gpt-oss:20b
ollama pull qwen3:14b

python -m footnote_rag.evals.run --live \
  --local-models "qwen3.5:9b,gpt-oss:20b@low,qwen3:14b" \
  --cascade "qwen3.5:9b>qwen3:14b"
```

- SPEC은 `모델이름[@think]`입니다. think는 `true`, `false`, `low`, `medium`, `high` 중 하나이고, 생략하면 `LOCAL_LLM_THINK`를 씁니다. `--cascade PRIMARY>FALLBACK`은 여러 번 줄 수 있고 양쪽에 `@think`를 붙일 수 있습니다.
- **사전 점검**: 모든 모델의 연결·존재 여부를 먼저 확인합니다. 하나라도 없거나 Ollama에 연결되지 않으면 공시를 가져오기 전에 `ollama pull <모델>` 안내를 출력하고 종료 코드 2로 끝납니다.
- 공시 가져오기와 질문 임베딩은 **한 번만** 하고 모든 구성이 같은 검색 결과·같은 문항 순서를 씁니다. 질문 임베딩은 Voyage, 답변은 로컬입니다.
- 단일 모델 구성은 측정 전에 모델을 한 번 예열합니다(`/api/generate`). 상향 구성은 primary만 예열하고, 상향 때 모델을 교체하는 시간은 지연에 포함합니다.
- 출력: 구성별 리포트(위와 같은 형식. 예: `…-live-local-gpt-oss-20b-think-low.md`, 상향 구성은 `…-live-local-qwen3.5-9b_to_qwen3-14b.md`)와 비교 리포트 `evals/reports/{timestamp}-live-compare-local.md`. 비교 리포트에는 구성 비교 표, 문항별 행렬(`cited ok/total` + `pub`/`rev`/`err`, 상향된 문항은 `↑`), 문항 유형별 인용 정확도 표가 있습니다.
- 두 옵션을 주지 않으면 지금처럼 제공자 하나로 리포트 하나를 씁니다.

구성 비교 표(직접 실행한 뒤 채웁니다):

| config | recall@8 | citation acc. | citation acc. (hit) | answered (answerable) | false abst. | unans. abstained | ungrounded number answers | escalation rate | errors | p50 ms | p95 ms | cost USD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| qwen3.5:9b | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 |
| gpt-oss:20b@low | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 |
| qwen3:14b | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 |
| qwen3.5:9b > qwen3:14b | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 | 미측정 |

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
