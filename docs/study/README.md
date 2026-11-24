# footnote-rag 학습 안내

이 폴더는 이 저장소의 코드를 **스스로 이해하고 면접에서 설명하기 위한** 학습 자료다. "무엇이 있나"는 [README](../../README.md)와 [docs/architecture.md](../architecture.md)에 있고, 여기서는 "왜 이렇게 동작하나", "어떤 순서로 읽나", "대안과 한계는 무엇인가", "면접에서 어떻게 설명하나"를 다룬다.

대상 독자: Python 백엔드 경험은 있지만 RAG, pgvector, Claude API, OpenDART는 처음인 개발자. 처음 나오는 용어는 [용어집](glossary.md)에 정의가 있다.

> **먼저 알아 둘 사실**
> - 실제 DART 데이터를 아직 받지 못했다([docs/data-sources.md](../data-sources.md)).
> - DART XML 파서 구조는 가정이며 합성 픽스처로만 검증했다([07](07-dart-integration.md)).
> - Eval 데이터셋에는 형식 예시 행만 있다. 검색·답변 품질과 성능을 측정한 수치는 없다([10](10-eval-and-mcp.md)).

## 읽는 순서

예상 소요 시간은 코드를 함께 열어 보며 읽는 기준의 대략적인 값이다.

| 순서 | 문서 | 내용 | 예상 소요 |
| --- | --- | --- | --- |
| 1 | [01. 전체 그림](01-big-picture.md) | 문제, 구성, 모듈 지도, 패키지 의존 관계도 | 30분 |
| 2 | [02. 요청 두 개 따라가기](02-request-walkthrough.md) | `POST /documents/dart`와 `POST /questions`를 함수 단위로 | 60분 |
| 3 | [03. 인증·인가·조직 격리](03-auth-rbac-isolation.md) | OAuth2 password flow, JWT, RBAC, 404 격리, 로그인 시퀀스도 | 40분 |
| 4 | [04. 작업 큐](04-job-queue.md) | SKIP LOCKED, 백오프, 재시도 분류, 복구, 멱등성 | 60분 |
| 5 | [05. RAG 검색](05-rag-retrieval.md) | 임베딩, 청킹, pgvector HNSW, 테넌트 필터 | 45분 |
| 6 | [06. Claude Citations와 각주](06-claude-citations.md) | `search_result`, citations, 각주 알고리즘, 거절·fallback, 응답→각주 흐름도 | 60분 |
| 7 | [07. OpenDART 연동](07-dart-integration.md) | 세 API, 오류 분류, 캐시, 파서 규칙과 그 한계 | 45분 |
| 8 | [08. 검토·감사·사용량](08-review-audit-usage.md) | HITL, 조건부 UPDATE, append-only, 비용, 답변 상태도 | 45분 |
| 9 | [09. 테스트와 운영](09-testing-and-ops.md) | Protocol+Fake, 실제 Postgres 테스트, 비밀값, Docker, CI, 제공자 선택도 | 45분 |
| 10 | [10. Eval과 MCP](10-eval-and-mcp.md) | 지표 정의, 검증기, 데이터셋 상태, MCP 구조 | 30분 |
| 복습 | [면접 Q&A](interview-qa.md) | 질문 32개: 요점, 근거, 꼬리 질문 | 60분 |
| 복습 | [실습](exercises.md) | 직접 고치고 깨뜨려 보는 실습 10개 | 실습당 20~60분 |
| 참고 | [용어집](glossary.md) | 용어 40여 개 | 필요할 때 |

각 해설 문서는 같은 틀을 따른다: 이 문서에서 배울 것 → 개념 → 이 저장소의 구현(코드 경로·함수 이름·발췌) → 설계도 → 왜 이렇게 했나 / 대안 / 한계 → 확인 질문(답은 접혀 있음).

코드 참조는 `파일경로::함수이름` 형식으로 통일했다. 예: `src/footnote_rag/jobs/queue.py::claim_job`. 이 형식의 참조와 문서 속 경로·링크는 `tests/test_study_docs.py`가 실제 코드와 대조해 검사한다.

## 처음 1시간 코스

시간이 없을 때 이 순서로 한다.

1. **실행(15분)**: [실습 1](exercises.md)의 앞부분만. `docker compose up --build -d` → `seed` → 로그인 → `POST /documents/dart`(접수번호 `20260310900001`) → `GET /jobs/{id}`가 `succeeded`가 될 때까지 → `POST /questions`. 응답의 `[1]`, `footnotes`, `status`를 눈으로 확인한다. 키는 필요 없다(Fake 모드).
2. **요청 하나 따라가기(25분)**: [02](02-request-walkthrough.md)의 "요청 ② `POST /questions`" 표를 보며 `src/footnote_rag/answering/router.py::post_question` → `src/footnote_rag/answering/service.py::ask_question` → `src/footnote_rag/answering/footnotes.py::build_footnotes` → `src/footnote_rag/answering/service.py::review_reasons`를 실제로 열어 읽는다.
3. **면접 Q&A(20분)**: [interview-qa.md](interview-qa.md)의 Q1, Q4, Q7, Q11, Q20, Q26을 소리 내어 답해 본다.

## 채용 공고 항목 대응표

삼일회계법인 AX Node Backend Engineer 공고의 담당업무·우대사항 항목을 이 저장소의 코드 위치와 학습 문서에 연결했다. 저장소에 해당 구현이 없으면 **없음**으로 표시했다.

| 공고 항목 | 이 저장소의 코드 위치 | 학습 문서 | 없는 부분 |
| --- | --- | --- | --- |
| 인증·인가 | `src/footnote_rag/auth/deps.py`, `src/footnote_rag/auth/security.py`, `src/footnote_rag/auth/router.py` | [03](03-auth-rbac-isolation.md) | OAuth2/OIDC SSO(외부 IdP 연동): **없음**. 리프레시 토큰·토큰 폐기: **없음** |
| 비동기·배치 처리 | `src/footnote_rag/jobs/queue.py`, `src/footnote_rag/jobs/worker.py`, `src/footnote_rag/jobs/handlers.py` | [04](04-job-queue.md) | 정기 실행 스케줄러(cron 배치): **없음** |
| Agent Tool / MCP | `src/footnote_rag/mcp_server.py` | [10](10-eval-and-mcp.md) | Claude tool use를 쓰는 에이전트 루프: **없음**(MCP 서버가 도구를 제공하는 쪽만 구현) |
| HITL·재시도·오류 복구 | `src/footnote_rag/reviews/router.py`, `src/footnote_rag/answering/service.py`, `src/footnote_rag/jobs/queue.py` | [04](04-job-queue.md), [08](08-review-audit-usage.md) | 실패 작업 재실행 API, 데드레터 큐: **없음** |
| RAG·임베딩·벡터 검색 | `src/footnote_rag/retrieval/embedder.py`, `src/footnote_rag/retrieval/search.py`, `src/footnote_rag/documents/chunking.py`, `src/footnote_rag/answering/` | [05](05-rag-retrieval.md), [06](06-claude-citations.md) | 하이브리드 검색(BM25)·재랭킹: **없음** |
| Eval·비용·지연시간·가드레일·감사 로그 | `src/footnote_rag/evals/`, `src/footnote_rag/usage/`, `src/footnote_rag/answering/service.py`, `src/footnote_rag/audit/` | [08](08-review-audit-usage.md), [10](10-eval-and-mcp.md) | 실제 데이터 Eval 결과: **없음**(데이터셋이 예시뿐). LLM 채점기: **없음** |
| RDBMS 모델링 | `src/footnote_rag/db/models.py`, `alembic/versions/0001_initial.py` | [01](01-big-picture.md), ERD는 [architecture](../architecture.md#erd) | Row-Level Security: **없음**([ADR 0005](../adr/0005-org-isolation-app-layer.md)) |
| 외부 시스템 연동 | `src/footnote_rag/dart/client.py`, `src/footnote_rag/retrieval/embedder.py`, `src/footnote_rag/answering/llm.py` | [07](07-dart-integration.md), [06](06-claude-citations.md) | 실제 OpenDART 호출 검증: **아직 못 함**(키 미인식) |
| Docker / CI / 관측성 | `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`, `src/footnote_rag/observability.py` | [09](09-testing-and-ops.md) | Kubernetes: **없음**. 메트릭(Prometheus)·분산 트레이싱(OpenTelemetry): **없음** |
| 보안 | `src/footnote_rag/config.py`, `src/footnote_rag/observability.py`, `src/footnote_rag/audit/service.py`, `.gitignore`, `.dockerignore` | [03](03-auth-rbac-isolation.md), [09](09-testing-and-ops.md) | 로그인 시도 제한·계정 잠금: **없음** |
| AI-Native 개발 | [docs/ai-usage.md](../ai-usage.md), `src/footnote_rag/mcp_server.py`(Claude Code 연결) | [면접 Q&A "AI 활용"](interview-qa.md#ai-활용) | 지원자 본인의 리뷰 내역: `docs/ai-usage.md`에 `[직접 확인 필요]`로 남아 있음 |

## 이 자료를 고칠 때

- 문서에 쓰는 경로·함수 이름은 실제 코드를 열어 확인한 것만 쓴다. `uv run pytest tests/test_study_docs.py`가 경로, `path::name` 참조, 문서 간 링크를 검사한다.
- 측정하지 않은 성능 수치는 쓰지 않는다.
- 기존 문서([architecture](../architecture.md), [ADR](../adr/0001-postgres-skip-locked-queue.md))와 겹치는 내용은 복사하지 말고 링크한다.
