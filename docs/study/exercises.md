# 직접 해 보는 실습

> 각 실습에는 목표, 힌트, 확인 방법만 있고 **정답 코드는 없다.** 코드를 고치는 실습은 끝나면 `git checkout -- <파일>` 또는 `git stash`로 되돌린다.
>
> 준비: 테스트 DB가 필요한 실습은 [README "로컬 개발"](../../README.md#로컬-개발)대로 `127.0.0.1:55439`에 pgvector 컨테이너를 띄운다. 테스트는 외부 네트워크를 쓰지 않고 `.env`도 읽지 않는다.

## 실습 1. Fake 모드로 전체 흐름 실행하기

**목표**: API 키 없이 로그인 → DART 가져오기 → 작업 완료 확인 → 질문 → 검토 → 감사 로그까지 한 번에 돌려 보고, 각 단계가 [02. 요청 따라가기](02-request-walkthrough.md)의 어느 함수인지 연결한다.

**힌트**
- `docker compose up --build -d` 후 `seed` 명령으로 조직과 관리자를 만든다([README "빠른 시작"](../../README.md#빠른-시작)).
- Fake 모드의 접수번호는 `20260310900001`~`20260310900003`이다(합성 회사).
- `docker compose logs worker`에서 `job.claimed`, `job.succeeded` 이벤트를 찾아본다.
- 응답 헤더의 `X-Request-ID`를 기록해 두면 실습 9에서 쓴다.

**확인 방법**

```bash
curl -s http://localhost:8000/readyz                     # {"status":"ready","database":"ok"}
curl -s http://localhost:8000/jobs/<job_id> -H "$AUTH"   # status: succeeded
curl -s -X POST http://localhost:8000/questions -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"question": "회사는 리스부채를 어떻게 측정하나요?"}'   # footnotes 배열과 [1] 표시
curl -s "http://localhost:8000/audit-logs?limit=20" -H "$AUTH"   # admin 토큰
```

응답의 `usage.model`이 `fake-llm`이고 `cost_usd`가 `null`인 이유를 [08](08-review-audit-usage.md)에서 찾아 설명할 수 있으면 완료.

## 실습 2. `MIN_RETRIEVAL_SCORE`를 바꿔 검토 대기 비율 관찰하기

**목표**: 검색 신뢰도 임계값이 `published`/`pending_review` 비율을 어떻게 바꾸는지 직접 본다.

**힌트**
- 값은 환경변수 `MIN_RETRIEVAL_SCORE`다. Compose라면 `.env`에 넣고 `api`를 다시 띄운다(`.env`는 커밋하지 않는다).
- 문서와 관련 있는 질문, 관련 없는 질문을 섞어 5~10개 보낸다.
- 응답의 `status`와 `review_reason`을 기록하고 표로 만든다. `POST /search`의 `score`도 함께 본다.
- Fake 임베더 점수 분포는 실제 Voyage와 다르다. 그래서 테스트는 0.05를 쓴다(`tests/conftest.py`). 관찰 결과를 실제 서비스 기준으로 일반화하지 않는다.

**확인 방법**
- `curl -s "http://localhost:8000/reviews?status=pending_review" -H "$AUTH"`의 개수가 임계값을 올릴수록 늘어나는지 본다.
- 관련 테스트: `uv run pytest tests/test_answering.py -k "low_score or review_reason"`.

## 실습 3. 새 감사 action 추가하기

**목표**: `GET /documents/{id}` 조회를 `document.viewed`로 감사 로그에 남긴다. 감사 기록이 업무 트랜잭션과 어떻게 묶이는지 체감한다.

**힌트**
- action 이름은 `src/footnote_rag/audit/service.py::Actions`에 모은다.
- 기록은 `src/footnote_rag/audit/service.py::record`로 하고, **커밋은 호출자 몫**이다. 지금 `src/footnote_rag/documents/router.py::get_document`는 읽기만 해서 커밋하지 않는다는 점을 놓치지 말자.
- metadata에 금지 키(`text`, `content` 등)를 넣으면 `ValueError`가 난다.
- 다른 조직 문서 조회(404)도 기록할지 스스로 정하고 이유를 적는다.

**확인 방법**
- 새 테스트를 `tests/` 아래에 추가해 조회 후 `GET /audit-logs?action=document.viewed`가 1건인지 확인한다.
- `uv run pytest tests/test_reviews_audit_usage.py tests/test_auth.py`로 기존 테스트가 깨지지 않는지 본다.

## 실습 4. 동시 claim 테스트를 일부러 깨뜨려 보기

**목표**: `FOR UPDATE SKIP LOCKED`가 정말 중복 처리를 막고 있는지 테스트가 실패하는 모습으로 확인한다.

**힌트**
- 대상은 `src/footnote_rag/jobs/queue.py::claim_job`의 `.with_for_update(skip_locked=True)`다.
- 변형 A: 잠금 자체를 없앤다. 변형 B: 잠금은 두고 `skip_locked`만 뺀다. **실행하기 전에** 각 변형의 결과를 예측해 적는다.
- 변형 B를 생각할 때: 기다리던 트랜잭션은 잠금이 풀린 뒤 바뀐 행을 다시 보고 WHERE(`status='queued'`)를 다시 평가한다.
- 타이밍에 따라 결과가 달라질 수 있으니 여러 번 돌린다.

**확인 방법**

```bash
uv run pytest tests/test_jobs.py -k "concurrent_claims or exactly_once" -q
git checkout -- src/footnote_rag/jobs/queue.py   # 반드시 되돌리기
```

변형 A가 왜 실패하고, 변형 B가 왜 (느리더라도) 통과할 수 있는지 [04](04-job-queue.md)의 원리로 설명할 수 있으면 완료.

## 실습 5. 각주 변환에 테스트 케이스 추가하기

**목표**: `build_footnotes`의 경계 상황을 테스트로 고정한다.

**힌트**
- 대상: `src/footnote_rag/answering/footnotes.py::build_footnotes`, 기존 테스트는 `tests/test_footnotes.py`.
- 아이디어: (1) 같은 청크를 한 블록에서 `source`로 한 번, 잘못된 `source`+유효한 `search_result_index`로 한 번 인용, (2) text가 빈 블록에 인용이 붙은 경우, (3) 모든 블록이 공백뿐일 때 결과 `text`, (4) 인용 순서가 c3 → c1 → c3 → c2일 때 번호.
- 기존 헬퍼 `_ref`, `_cite`를 재사용한다.
- 기대값은 코드를 보기 전에 [06](06-claude-citations.md)의 규칙만으로 먼저 적어 본다.

**확인 방법**: `uv run pytest tests/test_footnotes.py -q`. 각주 테스트 자체는 DB를 쓰지 않지만, `tests/conftest.py`의 autouse 픽스처가 테스트 DB를 준비하므로 DB 컨테이너는 떠 있어야 한다.

## 실습 6. 새 역할 권한 추가하기

**목표**: 감사 로그만 읽을 수 있는 `auditor` 역할을 추가한다. `/audit-logs`는 200, `/usage`·`/reviews`는 403.

**힌트**
- 역할 값은 DB enum `user_role`이다. 이미 적용된 `alembic/versions/0001_initial.py`를 고치지 말고 **새 마이그레이션**을 만든다(Postgres `ALTER TYPE … ADD VALUE`).
- 파이썬 쪽은 `src/footnote_rag/db/models.py`의 `USER_ROLES`, `src/footnote_rag/auth/router.py`의 `UserCreate.role`, `src/footnote_rag/auth/deps.py`의 역할 묶음을 본다.
- `/audit-logs`의 의존성(`src/footnote_rag/audit/router.py::list_audit_logs`)을 어떤 묶음으로 바꿀지 정한다. `ANY_MEMBER`에 넣을지도 결정해야 한다(넣으면 질문·검색도 가능해진다).
- `require_role`은 토큰이 아니라 DB의 `user.role`을 본다는 점을 기억한다.

**확인 방법**
- `tests/conftest.py`의 `make_org`를 참고해 auditor 사용자와 토큰을 만드는 테스트를 추가하고 상태 코드를 확인한다.
- `uv run pytest tests/test_auth.py -q`.

## 실습 7. 실제 키로 DART 보고서 하나 가져와 목차 확인하기

**목표**: 파서가 처리하는 원문 구조가 실제와 맞는지 직접 확인한다. 삼성전자 사업보고서로 확인한 결과는 [docs/data-sources.md](../data-sources.md)에 있으니, 다른 회사(NAVER `20260313001021`, 현대자동차 `20260318001394`)로 같은 확인을 해 본다.

**힌트**
- OpenDART에서 인증키를 발급받아 `.env`의 `OPENDART_API_KEY`에 넣는다. **`.env`는 절대 커밋하지 않는다.** 키 값을 터미널 출력이나 문서에 붙여 넣지 않는다.
- 접수번호는 지어내지 말고 `dart-find` 출력에서 복사한다.
- `dart-fetch` 출력의 목차를 [07](07-dart-integration.md)의 파싱 규칙, [docs/data-sources.md](../data-sources.md)의 "파서가 처리하는 원문 구조" 표와 비교한다. PowerShell에서는 `--stock-codes "005930,035420"`처럼 따옴표로 감싼다.
- 확인할 것: 경로가 "본문" 하나로 뭉치지 않는가, `주석 > 12. 리스` 같은 하위 목차가 나오는가, 표가 `셀 | 셀`로 나오는가, 섹션별 글자 수가 비정상적으로 작거나 크지 않은가.

**확인 방법**

```bash
python -m footnote_rag.cli dart-find --stock-codes 005930 --from 20260101 --to 20260531 --detail A001
python -m footnote_rag.cli dart-fetch --rcept-no <dart-find에서 복사한 rcept_no>
```

발견한 차이를 목록으로 정리한다. 파서를 고친다면 실제 원문은 커밋하지 말고, 같은 구조를 흉내 낸 **합성** 픽스처로 테스트를 추가한다.

## 실습 8. Eval 질문 3개 작성하고 validate 통과시키기

**목표**: 데이터셋 한 행이 어떤 조건을 만족해야 하는지 검증기를 통과시키며 익힌다.

**힌트**
- 형식은 `src/footnote_rag/evals/dataset.py::load_dataset`의 docstring과 `evals/dataset.jsonl`의 예시 행을 본다. 새 행에는 `_example`을 넣지 않는다.
- 3개만 만들면 개수 규칙(15행 이상 등)에 걸리므로 `--no-coverage`를 쓴다.
- `evidence_quote`는 **파서가 뽑은 텍스트에서 그대로 복사**해야 한다. 문서를 가져온 뒤 `POST /search` 응답의 `text`와 `section_path`를 보면 파서 출력을 확인할 수 있다.
- 키가 없으면: Fake 모드에서 `dart-fetch`를 돌리면 합성 ZIP이 `data/dart/fake/`에 캐시된다. 합성 문서로 연습할 때는 `--zip-dir data/dart/fake`를 쓰고, 이 행들을 저장소 데이터셋에 넣지 않는다(합성 데이터는 실제 공시가 아니다).
- 하나는 `answerable: false`(근거가 없는 질문)로 만든다.

**확인 방법**

```bash
python -m footnote_rag.evals.validate --dataset my-eval.jsonl --zip-dir data/dart --no-coverage
# 출력 마지막 줄이 OK 여야 한다
python -m footnote_rag.evals.run --dataset my-eval.jsonl   # Fake 모드 리포트 생성(비용 없음)
```

일부러 인용문 한 글자를 바꿔 `evidence_quote not found` 오류가 나는 것도 확인한다.

## 실습 9. request_id로 요청 하나 추적하기

**목표**: 관측성 장치(request_id, structlog, 감사 로그)가 어떻게 연결되는지 확인한다.

**힌트**
- 요청에 `-H 'X-Request-ID: study-001'`을 붙여 질문을 보낸다.
- 형식 규칙(`src/footnote_rag/main.py::create_app`의 정규식)에 맞지 않는 값을 보내면 어떻게 되는지도 본다.
- `docker compose logs api`에서 그 값을 검색하고, `GET /audit-logs?action=answer.created` 응답의 `request_id`와 비교한다.

**확인 방법**: 응답 헤더 `X-Request-ID: study-001`, `http.request` 로그 한 줄, `question.asked`·`answer.created` 감사 행 두 개가 모두 같은 `request_id`를 가지면 완료.

## 실습 10. 동시 검토 보호를 깨뜨려 보기

**목표**: 조건부 UPDATE의 WHERE 조건 하나가 동시성 버그를 막고 있음을 확인한다.

**힌트**
- 대상: `src/footnote_rag/reviews/router.py::decide`의 `Answer.status == "pending_review"` 조건.
- 조건을 빼면 상태 코드와 감사 행 수가 어떻게 바뀔지 먼저 예측한다.
- "먼저 SELECT로 상태 확인 → 다른 문장으로 UPDATE"로 바꾸는 변형도 시도해 보고, 왜 여전히 안전하지 않은지 생각한다.

**확인 방법**

```bash
uv run pytest tests/test_reviews_audit_usage.py -k concurrent_reviews -q
git checkout -- src/footnote_rag/reviews/router.py   # 반드시 되돌리기
```
