# AI 활용 내역 (초안)

> 초안입니다. `[직접 확인 필요]` 표시는 지원자가 사실을 확인하거나 직접 채워야 하는 곳입니다. 이 문서에는 저장소에서 확인할 수 있는 사실만 적었습니다.

## 직접 내린 결정

- 채용 공고에 맞춘 프로젝트를 만들기로 함.
- 저장소 이름 `footnote-rag` 유지.
- 언어·프레임워크는 Python/FastAPI, LLM 제공자는 Claude, 모델은 Sonnet.
- 가상 데이터 대신 실제 공시 자료 사용.
- 대상 회사는 삼성전자·NAVER·현대자동차.
- 데이터 수집은 OpenDART API 방식(권장안)을 채택.
- 일정에 맞춰 M1→M3 단계 분할.
- API 키 3종을 직접 발급해 `.env`에 저장.

## AI에게 맡긴 작업

- 계획 수립: 아키텍처와 기능 범위 제안(마일스톤 M1~M3, 데이터 모델, 수용 기준)을 AI 코딩 에이전트(Claude)가 작성했습니다.
- 구현: 티켓 큐를 통해 AI 코딩 에이전트가 구현했습니다. 저장소에서 확인할 수 있는 범위는 다음과 같습니다.
  - 애플리케이션 코드: `src/footnote_rag/` (인증·조직, OpenDART 클라이언트와 XML 파서, 작업 큐와 워커, 임베딩·검색, 각주 답변, 검토, 감사 로그, 사용량, Eval, MCP 서버)
  - 스키마: `alembic/versions/0001_initial.py`
  - 테스트: `tests/` (합성 픽스처만 사용)
  - 실행 환경: `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`
  - 문서: `README.md`, `docs/architecture.md`, `docs/adr/`, `docs/data-sources.md`, 이 초안
- Eval 질문 초안을 AI가 작성, 근거 인용은 validate로 자동 검증
- 커밋 이력: [직접 확인 필요] `git log`로 어떤 커밋이 AI 생성인지(`Co-Authored-By` 표기) 확인해 적기.

## 검증 방법

- **자동 테스트**: `pytest`가 실제 PostgreSQL(pgvector)에서 돌고, Claude·Voyage·OpenDART는 Fake로 대체되어 네트워크를 호출하지 않습니다. 다루는 항목: 401/403/교차 조직 404, 작업 큐 동시 claim·백오프·즉시 실패·최대 시도·멈춘 작업 복구·청크 중복 없음, DART 파서(중첩 목차·표·빈 문서·EUC-KR), OpenDART 오류 코드 분류와 `crtfc_key` 마스킹, 각주 변환과 신뢰도 규칙, 검토 동시성 409, 감사 로그 UPDATE/DELETE 거부, 사용량 집계, Eval 실행기·검증기, 설정의 비밀값 마스킹.
- **CI**: GitHub Actions에서 `ruff check`, `ruff format --check`, `alembic upgrade head`, `pytest`를 비밀값 없이 실행합니다(`.github/workflows/ci.yml`).
- **API 형식 확인**: Claude Citations(`search_result_location`)와 서버 측 fallback 파라미터를 공식 문서와 설치된 SDK로 확인했습니다([ADR 0002](adr/0002-claude-citations-search-result.md)).
- **Eval**: `python -m footnote_rag.evals.run`(검색 recall@k, 인용 정확도, 답하지 말아야 할 질문 처리, 비용·지연시간). 실제 보고서 기반 데이터셋은 아직 없습니다([data-sources.md](data-sources.md)).
- **사람 리뷰**: [직접 확인 필요] 지원자가 코드·문서를 직접 리뷰했는지, 무엇을 확인했는지 적기.

## AI 결과를 고친 사례

구현 과정에서 테스트·문서 확인으로 발견해 고친 내용입니다(저장소 코드에 반영됨).

- **롤백 후 만료된 ORM 객체 접근**: LLM 오류 처리와 검토 결정의 409 경로에서 트랜잭션을 롤백한 뒤 `user.org_id`를 읽어 500이 났습니다. 테스트(`test_llm_error_returns_502_and_is_audited`, `test_only_pending_answers_can_be_decided`)로 발견했고, 롤백 전에 ID를 지역 변수로 잡도록 고쳤습니다.
- **중복 가져오기 경쟁 조건**: 첫 구현은 `flush()` 단계에서 UNIQUE 위반이 나 IntegrityError 처리 경로를 타지 못했습니다. flush를 없애 커밋에서만 위반이 나게 하고 동시 요청 테스트(`test_concurrent_duplicate_imports_create_one_document`)를 추가했습니다.
- **MCP SDK 버전 차이**: 설치된 `mcp` 2.x에서 `FastMCP`가 `MCPServer`로 이름이 바뀌어 import가 실패했습니다. 두 버전을 모두 지원하도록 고쳤습니다.
- **API 키 로그 노출 위험**: httpx가 INFO 레벨에서 요청 URL 전체(OpenDART `crtfc_key` 포함)를 로그로 남기는 점을 확인하고, 해당 로거를 WARNING으로 낮추고 표준 로그에도 마스킹 필터를 붙였습니다.
- **Fake 캐시와 실제 캐시 충돌**: Fake 모드가 `data/dart/corp_codes.json`에 합성 회사 목록을 캐시하면 이후 실제 조회가 7일간 합성 캐시를 쓰는 문제가 있어, Fake 캐시를 `data/dart/fake/`로 분리했습니다.
- **가격표**: 계획에 적힌 Sonnet 5.5 cache read 단가($0.20)가 공식 가격 페이지($0.10)와 달라 문서 값을 따랐습니다.
- [직접 확인 필요] 지원자가 직접 리뷰하며 고친 내용이 있으면 추가.
