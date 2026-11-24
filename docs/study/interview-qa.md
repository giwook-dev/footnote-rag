# 면접 예상 질문과 답변 요점

> 사용법: 질문만 보고 30초 안에 소리 내어 답해 본 뒤 요점과 비교한다. 근거 위치를 열어 코드로 확인하고, 꼬리 질문까지 답해 본다.
>
> 원칙: 측정하지 않은 수치는 말하지 않는다. 모르면 "측정하지 않았다", "실제 데이터로 확인하지 못했다"가 정확한 답이다.

## 설계 선택

### Q1. 이 프로젝트를 30초로 소개해 주세요.
- **30초 답변 요점**: DART 감사보고서를 접수번호로 가져와 목차 경로별로 나누고 pgvector에 저장한 뒤, 질문에 Claude Citations 기반으로 문장마다 각주를 달아 답하는 FastAPI 백엔드다. 근거가 약한 답은 사람 검토 대기열로 보내고, 모든 주요 행위와 LLM 비용을 감사 로그·`llm_calls`에 남긴다. 조직 격리, Postgres 작업 큐, Eval, MCP 서버까지 있다. 다만 실제 DART 원문은 아직 받지 못해 합성 데이터로만 검증했다.
- **근거**: [01. 전체 그림](01-big-picture.md), [README](../../README.md)
- **꼬리 질문**: 가장 어려웠던 설계 결정 하나를 고른다면?

### Q2. 작업 큐로 Celery·Redis 대신 Postgres를 쓴 이유는?
- **30초 답변 요점**: 문서 행·작업 행·감사 로그를 한 트랜잭션으로 만들려고. 외부 브로커면 "DB 커밋은 됐는데 메시지는 안 갔다"를 막으려 outbox가 필요하다. 작업량이 조직당 공시 수십 건 수준이라 Postgres 하나로 충분하고, 상태를 SQL로 바로 본다.
- **근거**: `src/footnote_rag/jobs/queue.py::claim_job`, [ADR 0001](../adr/0001-postgres-skip-locked-queue.md), [04](04-job-queue.md)
- **꼬리 질문**: 처리량이 크게 늘면 어떤 문제가 생기고 언제 옮기겠나?

### Q3. 벡터 저장소를 별도 벡터 DB가 아니라 pgvector로 한 이유는?
- **30초 답변 요점**: 조직 필터, 문서 `ready` 상태, 벡터 거리를 SQL 한 문장으로 처리하고, 청크 저장을 문서 상태 변경·작업 성공과 같은 트랜잭션에 넣을 수 있다. 저장소가 하나라 운영이 단순하다.
- **근거**: `src/footnote_rag/retrieval/search.py::search_chunks`, `src/footnote_rag/jobs/handlers.py::_embed_and_store`, [05](05-rag-retrieval.md)
- **꼬리 질문**: 테넌트 필터가 있는 HNSW 검색에서 생기는 문제는?

### Q4. 각주를 프롬프트로 만들게 하지 않고 Citations를 쓴 이유는?
- **30초 답변 요점**: 프롬프트로 "[1] 형식으로 출처를 달라"고 하면 인용문과 번호를 모델이 생성해 지어낸 인용을 막기 어렵다. Citations는 `cited_text`를 API가 원문에서 잘라 주고 위치가 구조화되어 와서, 순수 함수 하나로 각주를 만든다.
- **근거**: `src/footnote_rag/answering/footnotes.py::build_footnotes`, [ADR 0002](../adr/0002-claude-citations-search-result.md), [06](06-claude-citations.md)
- **꼬리 질문**: structured outputs로 `{문장, chunk_ids}`를 받는 방법은 왜 안 되나?

### Q5. 원문을 PDF가 아니라 OpenDART XML로 받고, 위치를 페이지 대신 목차 경로로 표시한 이유는?
- **30초 답변 요점**: PDF는 공식 API로 받을 수 없어 스크래핑이 필요하고 표·레이아웃 추출이 불안정하다. XML에는 `SECTION`·`TITLE` 목차 구조가 있어 근거 위치를 안정적으로 표시할 수 있고, 실무자에게 "주석 12. 리스"가 페이지 번호보다 찾기 쉽다.
- **근거**: `src/footnote_rag/dart/parser.py::parse_document_xml`, [ADR 0004](../adr/0004-opendart-xml-over-pdf.md), [07](07-dart-integration.md)
- **꼬리 질문**: 그 XML 구조를 실제로 확인했나?

### Q6. 외부 API 세 개에 의존하는데 어떻게 테스트하나?
- **30초 답변 요점**: 각 의존성을 Protocol 뒤에 두고 실제/Fake 구현을 `providers.py`가 고른다. 테스트는 import 전에 키를 지우고 `.env`를 막고 제공자를 fake로 고정한다. 실제 SDK 경로는 스텁 클라이언트와 `httpx.MockTransport`로 요청 모양과 오류 매핑을 검증한다.
- **근거**: `src/footnote_rag/providers.py::make_llm`, `tests/conftest.py`, `tests/test_answering.py::test_claude_request_shape`, [09](09-testing-and-ops.md)
- **꼬리 질문**: Fake로 검증할 수 없는 것은 무엇인가?

## 동시성·트랜잭션

### Q7. 워커를 여러 개 띄워도 같은 작업이 두 번 처리되지 않는 이유는?
- **30초 답변 요점**: claim이 `SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`로 행을 잠그고, 같은 트랜잭션에서 `running`으로 바꾼 뒤 커밋한다. 다른 워커는 잠긴 행을 건너뛰고, 커밋 후에는 `queued`가 아니라 조건에 안 걸린다. 실제 Postgres에서 다섯 개 동시 claim 중 하나만 성공하는지 테스트한다.
- **근거**: `src/footnote_rag/jobs/queue.py::claim_job`, `tests/test_jobs.py::test_concurrent_claims_get_one_job_once`
- **꼬리 질문**: SELECT와 UPDATE를 다른 트랜잭션으로 나누면?

### Q8. 재시도 정책을 설명해 주세요.
- **30초 답변 요점**: 핸들러가 외부 오류를 재시도 가능/불가로 분류한다. OpenDART 020·800·900, HTTP 5xx·429, 네트워크 오류, Voyage 레이트 리밋은 재시도. 키 오류·데이터 없음·파싱 실패는 즉시 실패. 재시도면 `2^attempts × BASE`초 뒤로 미루고, `max_attempts`를 다 쓰면 `failed`. 예상하지 못한 예외는 재시도하지 않는다.
- **근거**: `src/footnote_rag/dart/client.py::classify_status`, `src/footnote_rag/jobs/queue.py::mark_failed`, `src/footnote_rag/jobs/worker.py::Worker._execute`, [04](04-job-queue.md)
- **꼬리 질문**: 백오프에 지터가 없는데 어떤 문제가 생길 수 있나?

### Q9. 워커가 작업 중에 죽으면 어떻게 되나?
- **30초 답변 요점**: 작업은 `running`으로 남는다. 워커는 시작 시와 루프 60회마다 `locked_at`이 `JOB_LOCK_TIMEOUT_SECONDS`(기본 600초)보다 오래된 `running`을 `queued`로 되돌린다. `attempts`는 claim 때 늘었으므로 죽은 실행도 한 번으로 센다. 재처리는 청크를 지우고 다시 넣어 결과가 중복되지 않는다.
- **근거**: `src/footnote_rag/jobs/queue.py::recover_stale_jobs`, `tests/test_jobs.py::test_stale_running_job_is_recovered`
- **꼬리 질문**: 정상 작업이 600초를 넘기면? heartbeat를 넣는다면 어떻게?

### Q10. 같은 접수번호를 동시에 두 번 가져오기 요청하면?
- **30초 답변 요점**: `UNIQUE(org_id, source, source_ref)` 제약으로 커밋에서 하나만 성공한다. 진 요청은 `IntegrityError`를 잡아 롤백하고 기존 문서를 돌려준다(200, `duplicate: true`). 처음에 `flush()`에서 위반이 나 예외 처리 경로를 못 타는 문제가 있어 flush를 없앴다.
- **근거**: `src/footnote_rag/documents/service.py::request_dart_import`, `tests/test_documents.py::test_concurrent_duplicate_imports_create_one_document`
- **꼬리 질문**: 다른 조직이 같은 접수번호를 가져오면?

### Q11. 두 검토자가 같은 답변을 동시에 승인·거절하면?
- **30초 답변 요점**: 결정은 `UPDATE answers … WHERE id=? AND org_id=? AND status='pending_review' RETURNING` 한 번이다. 먼저 실행된 UPDATE만 1행을 바꾸고, 뒤 요청은 0행이라 409를 받는다. 감사 기록은 성공한 쪽만 같은 트랜잭션에 남긴다. 세 요청 동시 테스트가 `[200, 409, 409]`와 감사 1행을 확인한다.
- **근거**: `src/footnote_rag/reviews/router.py::decide`, `tests/test_reviews_audit_usage.py::test_concurrent_reviews_one_wins_other_409`
- **꼬리 질문**: `SELECT … FOR UPDATE`로 풀면 무엇이 다른가?

### Q12. 감사 로그를 업무 변경과 같은 트랜잭션에 기록하는 이유는?
- **30초 답변 요점**: 따로 커밋하면 "변경은 됐는데 기록이 없음" 또는 "기록은 있는데 변경은 롤백됨"이 생긴다. `record`는 세션에 추가만 하고 호출자가 함께 커밋한다. 단, LLM 오류·로그인 실패처럼 실패 자체를 남겨야 하면 롤백 뒤 감사만 커밋한다.
- **근거**: `src/footnote_rag/audit/service.py::record`, `src/footnote_rag/answering/service.py::ask_question`, [08](08-review-audit-usage.md)
- **꼬리 질문**: 감사 로그를 외부 시스템으로 보내야 한다면 원자성은 어떻게 지키나?

## 보안

### Q13. 인증 흐름과 JWT에 무엇이 들어 있는지 설명해 주세요.
- **30초 답변 요점**: OAuth2 password flow로 `POST /auth/token`에 이메일·비밀번호를 보내면 argon2로 검증하고 HS256 JWT를 준다. 클레임은 `sub`, `org_id`, `role`, `iat`, `exp`(기본 60분). 보호된 요청마다 서명·만료·필수 클레임을 검사한 뒤 **DB에서 사용자를 다시 읽어** 비활성·조직 불일치면 401, 역할은 DB 값으로 검사해 부족하면 403이다.
- **근거**: `src/footnote_rag/auth/security.py::create_access_token`, `src/footnote_rag/auth/deps.py::get_current_user`, [03](03-auth-rbac-isolation.md)
- **꼬리 질문**: 토큰을 즉시 무효화하려면?

### Q14. 조직 격리는 어떻게 하고, 다른 조직 리소스에 왜 404를 주나?
- **30초 답변 요점**: 모든 조회·변경에 현재 사용자의 `org_id` 조건을 넣는다. 벡터 검색은 `chunks.org_id`와 `documents.org_id`를 둘 다 건다. 403을 주면 ID를 바꿔 가며 리소스 존재 여부를 알아낼 수 있어 404로 숨긴다. 문서·답변·작업·검토·검색에서 교차 조직이 404인지 테스트한다.
- **근거**: `src/footnote_rag/retrieval/search.py::search_chunks`, `tests/test_auth.py::test_other_org_resources_are_404`, [ADR 0005](../adr/0005-org-isolation-app-layer.md)
- **꼬리 질문**: 새 엔드포인트에서 `org_id` 조건을 빠뜨리면 무엇이 막아 주나?

### Q15. Postgres RLS를 쓰지 않은 이유는?
- **30초 답변 요점**: RLS는 쿼리 실수에도 격리를 보장하는 두 번째 방어선이지만, 요청마다 `SET LOCAL`을 걸고, 여러 조직을 처리하는 워커용 역할을 따로 두고, 커넥션 풀에서 설정 누수를 조심해야 한다. 이번 범위에서는 앱 계층 필터와 테스트로 충분하다고 보고 후속 과제로 뒀다.
- **근거**: [ADR 0005](../adr/0005-org-isolation-app-layer.md), [03](03-auth-rbac-isolation.md)
- **꼬리 질문**: RLS를 도입한다면 워커는 어떻게 처리하겠나?

### Q16. API 키가 새지 않게 한 장치는?
- **30초 답변 요점**: 키는 `SecretStr`로 보관해 `repr`·덤프에 안 나온다. 로그는 structlog 프로세서와 표준 로그 필터가 민감 필드와 `crtfc_key` 값을 가리고, httpx 요청 로그는 WARNING으로 올렸다. `.env`와 `data/`는 `.gitignore`·`.dockerignore`에서 빠지고 키는 런타임 `env_file`로만 주입한다. 테스트·CI는 `.env`를 읽지 않는다.
- **근거**: `src/footnote_rag/observability.py::configure_logging`, `src/footnote_rag/config.py::Settings`, `tests/test_config.py::test_secrets_are_masked_in_repr_and_dump`
- **꼬리 질문**: 운영에서 키를 교체(rotate)하려면?

### Q17. 감사 로그 변조는 어떻게 막나?
- **30초 답변 요점**: DB 트리거가 `audit_logs`의 UPDATE·DELETE(행 단위)와 TRUNCATE(문장 단위)에 예외를 던진다. 애플리케이션 코드 경로와 무관하게 적용된다. metadata에는 비밀번호·토큰·본문 키를 금지한다. 한계는 DB 소유자가 트리거를 지울 수 있다는 점이다.
- **근거**: `alembic/versions/0001_initial.py::upgrade`, `tests/test_reviews_audit_usage.py::test_audit_log_rejects_update_and_delete`
- **꼬리 질문**: 슈퍼유저까지 고려하면 무엇을 더 하겠나?

## RAG 품질

### Q18. 청킹 전략을 설명해 주세요.
- **30초 답변 요점**: 섹션(목차 경로) 경계를 넘지 않고, 문자 기준 1,500자에 약 200자 겹침, 줄 단위로 채운다. 파서가 표를 행마다 한 줄로 만들어 표 행이 잘리지 않는다. 모든 청크가 `section_path`를 가져 각주에 목차를 보여 줄 수 있다. 크기·겹침은 설정 기본값이고 실험으로 정한 값이 아니다.
- **근거**: `src/footnote_rag/documents/chunking.py::chunk_section_text`, `tests/test_footnotes.py::test_chunking_respects_size_overlap_and_rows`
- **꼬리 질문**: 청크 크기를 어떻게 정하는 게 맞나?

### Q19. 조직 필터와 HNSW를 같이 쓸 때의 문제와 해결은?
- **30초 답변 요점**: HNSW는 가까운 후보를 먼저 찾고 WHERE로 거른다. 다른 조직 청크가 많으면 후보 대부분이 걸러져 `top_k`보다 적게 나온다. pgvector 0.8의 `SET LOCAL hnsw.iterative_scan = strict_order`로 조건을 만족하는 행이 모일 때까지 계속 탐색하게 했다.
- **근거**: `src/footnote_rag/retrieval/search.py::search_chunks`, [05](05-rag-retrieval.md)
- **꼬리 질문**: 조직이 아주 많아지면 다른 방법은?

### Q20. 근거가 약한 답변은 어떻게 처리하나? (가드레일)
- **30초 답변 요점**: 검색 0건이면 Claude를 부르지 않는다. 그 외에 거절, 최고 유사도가 `MIN_RETRIEVAL_SCORE` 미만, 각주 0개면 `pending_review`로 보내고 member에게는 본문을 숨긴다. `REVIEW_MODE=always`로 전부 검토로 보낼 수도 있다. 시스템 프롬프트도 검색 결과만 근거로 쓰고 없으면 정해진 문장으로 답하게 한다.
- **근거**: `src/footnote_rag/answering/service.py::review_reasons`, `src/footnote_rag/answering/router.py::_visible_to`, [08](08-review-audit-usage.md)
- **꼬리 질문**: 임계값 0.35는 어떻게 정했나?

### Q21. 검색 품질을 어떻게 평가하나?
- **30초 답변 요점**: Eval 실행기가 정상 서비스 경로로 recall@k(기대 목차 청크가 상위 k에 있는 비율), 인용 정확도, 답할 수 없는 질문의 거절 비율, 평균 비용·지연을 계산한다. 근거 인용문이 파서 출력에 실제로 있는지 검증기로 먼저 확인한다. 하지만 지금 데이터셋은 예시 행뿐이라 측정 결과는 없다.
- **근거**: `src/footnote_rag/evals/run.py::EvalSummary`, `src/footnote_rag/evals/validate.py::validate`, [10](10-eval-and-mcp.md)
- **꼬리 질문**: 목차 접두어로 판정하는 방식의 약점은?

## LLM 운영 (비용·거절·평가)

### Q22. LLM 비용은 어떻게 추적하나?
- **30초 답변 요점**: 호출마다 응답의 실제 `model`, input·output·cache read·cache write 토큰, `Decimal` 비용, 지연시간, `stop_reason`을 `llm_calls`에 저장한다. 비용은 설정 가격표로 계산하고 가격표에 없는 모델은 NULL과 경고 로그. `/usage`가 조직별 합계, 모델별 합계, 지연 p50/p95를 준다.
- **근거**: `src/footnote_rag/usage/pricing.py::compute_cost`, `src/footnote_rag/usage/router.py::get_usage`
- **꼬리 질문**: fallback이 일어나면 비용 집계에 어떤 오차가 생기나?

### Q23. 모델이 답변을 거절하면?
- **30초 답변 요점**: 거절은 HTTP 200에 `stop_reason: "refusal"`로 온다. content를 읽기 전에 확인해 거절이면 content를 버리고 고정 문구로 `pending_review(refusal)`. 서버 측 `fallbacks="default"`로 일부 범주는 API가 다른 모델로 재시도하지만, 최종 응답도 거절일 수 있어 이 처리가 필요하다.
- **근거**: `src/footnote_rag/answering/llm.py::ClaudeClient.answer`, `tests/test_answering.py::test_refusal_is_pending_review`, [06](06-claude-citations.md)
- **꼬리 질문**: 응답 `model`을 저장하는 이유는?

### Q24. Claude API가 장애일 때 사용자는 무엇을 받나?
- **30초 답변 요점**: SDK가 최대 2번 재시도한 뒤에도 실패하면 레이트 리밋·5xx·연결 오류는 503, 그 밖의 4xx는 502로 바꾼다. 질문·답변 저장은 롤백하고 `question.failed` 감사만 남긴다. 롤백 뒤 만료된 ORM 객체를 읽어 500이 나던 문제를 테스트로 잡아 고쳤다.
- **근거**: `src/footnote_rag/answering/llm.py::ClaudeClient.answer`, `tests/test_answering.py::test_llm_error_returns_502_and_is_audited`
- **꼬리 질문**: 질문 처리도 큐로 돌리면 장애 대응이 어떻게 달라지나?

### Q25. `temperature`를 0으로 두지 않은 이유는? 보내지 않는 파라미터는?
- **30초 답변 요점**: Sonnet 5.5에서는 기본값이 아닌 샘플링 값, `budget_tokens`, 사고 끄기, assistant prefill, 강제 `tool_choice`가 400이다. `output_config.format`은 citations와 함께 쓰면 400이다. 그래서 adaptive thinking과 `effort`만 보내고, 테스트가 요청에 이 파라미터들이 없는지 검사한다.
- **근거**: `src/footnote_rag/answering/llm.py::ClaudeClient.request_params`, `tests/test_answering.py::test_claude_request_shape`
- **꼬리 질문**: 답변 일관성은 그러면 무엇으로 확보하나?

## 한계와 개선

### Q26. 이 프로젝트의 가장 큰 한계는?
- **30초 답변 요점**: 실제 DART 데이터를 아직 받지 못했다. 그래서 XML 파서 구조는 가정이고 합성 픽스처로만 검증했으며, Eval 데이터셋도 예시 행뿐이라 품질 수치가 없다. 다음 할 일은 실제 키로 `dart-fetch`를 돌려 목차를 확인하고 파서를 맞춘 뒤 Eval 데이터셋을 만드는 것이다.
- **근거**: [docs/data-sources.md](../data-sources.md), [ADR 0004](../adr/0004-opendart-xml-over-pdf.md) "한계", [07](07-dart-integration.md)
- **꼬리 질문**: 그 상태에서 무엇을 근거로 "동작한다"고 말할 수 있나?

### Q27. 프로덕션에 올린다면 무엇을 더하겠나?
- **30초 답변 요점**: 실제 데이터 검증과 Eval이 먼저다. 그다음 RLS 같은 두 번째 격리 방어선, 리프레시 토큰·로그인 시도 제한, 작업 heartbeat와 실패 작업 재실행 API, 메트릭·트레이싱, 감사 로그 보존 정책, 부하 테스트. Kubernetes와 SSO는 지금 없다.
- **근거**: [03](03-auth-rbac-isolation.md), [04](04-job-queue.md), [09](09-testing-and-ops.md) 각 "한계" 절
- **꼬리 질문**: 그중 하나만 고른다면 무엇이고 왜?

### Q28. 사용량이 늘면 어디가 먼저 병목이 될 것 같나?
- **30초 답변 요점**: 측정하지 않았으므로 추정이라고 먼저 말한다. 후보는 동기 질문 처리 중 Claude 호출 시간, 외부 API 한도(OpenDART 020, Voyage 레이트 리밋), 폴링 큐의 `jobs` 테이블, 조직이 많을 때 필터 있는 HNSW 검색이다. 먼저 `/usage`의 p95와 로그의 `duration_ms`로 실제 분포를 보겠다.
- **근거**: `src/footnote_rag/usage/router.py::get_usage`, `src/footnote_rag/main.py::create_app`, [ADR 0001](../adr/0001-postgres-skip-locked-queue.md)
- **꼬리 질문**: 부하 테스트를 설계한다면?

## AI 활용

> 이 절의 답변 요점은 [docs/ai-usage.md](../ai-usage.md)에 적힌 사실만 쓴다. `[직접 확인 필요]` 부분은 지원자가 자기 경험으로 채워야 한다. 확인하지 않은 내용을 직접 했다고 말하지 않는다.

### Q29. 이 프로젝트에서 AI를 어떻게 활용했나요?
- **30초 답변 요점**: 계획 수립(마일스톤 M1~M3, 데이터 모델, 수용 기준)과 구현을 AI 코딩 에이전트(Claude)에게 맡겼다. 직접 내린 결정은 공고에 맞춘 주제 선정, Python/FastAPI와 Claude Sonnet 선택, 가상 데이터 대신 실제 공시 사용, 대상 회사, OpenDART API 방식 채택, M1→M3 분할, API 키 발급이다. `[직접 확인 필요: 본인이 실제로 맡은 역할과 리뷰 범위를 구체적으로]`
- **근거**: [docs/ai-usage.md](../ai-usage.md) "직접 내린 결정", "AI에게 맡긴 작업"
- **꼬리 질문**: AI가 만든 코드를 어떻게 믿을 수 있었나?

### Q30. AI가 만든 결과를 어떻게 검증했나요?
- **30초 답변 요점**: 저장소에 있는 검증 장치는 실제 Postgres에서 도는 pytest(Fake 제공자, 네트워크 없음), 비밀값 없이 도는 CI(ruff, alembic, pytest), 공식 문서·설치된 SDK로 Citations와 fallback 파라미터 형식 확인(ADR 0002), Eval 실행기다. 사람 리뷰는 `[직접 확인 필요: 직접 리뷰했는지, 무엇을 확인했는지]`.
- **근거**: [docs/ai-usage.md](../ai-usage.md) "검증 방법", `.github/workflows/ci.yml`
- **꼬리 질문**: 테스트가 통과해도 놓칠 수 있는 것은?

### Q31. AI 결과가 틀렸던 사례가 있나요?
- **30초 답변 요점**: 구현 과정에서 테스트·문서 확인으로 발견해 고친 사례가 기록돼 있다. 롤백 후 만료된 ORM 객체 접근으로 인한 500, `flush()` 때문에 IntegrityError 처리 경로를 못 타던 중복 가져오기 경쟁, `mcp` 2.x의 이름 변경, httpx 로그의 `crtfc_key` 노출 위험, Fake 캐시와 실제 캐시 충돌, 계획 문서의 가격 오기. 누가 발견했는지는 `[직접 확인 필요]`.
- **근거**: [docs/ai-usage.md](../ai-usage.md) "AI 결과를 고친 사례", `tests/test_documents.py::test_concurrent_duplicate_imports_create_one_document`
- **꼬리 질문**: 그중 하나를 골라 원인과 수정 방법을 코드로 설명해 달라.

### Q32. 구현을 AI에게 맡겼는데 코드를 이해하고 있다는 것을 어떻게 보여 줄 수 있나요?
- **30초 답변 요점**: 이 학습 자료로 요청 두 개를 함수 단위로 따라가고, 핵심 동시성 장치(SKIP LOCKED, 조건부 UPDATE, UNIQUE 제약)와 그 테스트를 직접 깨뜨려 보는 실습을 했다고 말할 수 있으면 가장 좋다. `[직접 확인 필요: 실제로 해 본 실습과 그 결과]`
- **근거**: [exercises.md](exercises.md), [02](02-request-walkthrough.md)
- **꼬리 질문**: 지금 화면에서 `claim_job`을 열고 한 줄씩 설명해 보라.
