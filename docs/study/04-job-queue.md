# 04. Postgres 작업 큐: SKIP LOCKED, 재시도, 복구, 멱등성

> 예상 소요: 60분 · 이전: [03. 인증·인가](03-auth-rbac-isolation.md) · 다음: [05. RAG 검색](05-rag-retrieval.md)

## 이 문서에서 배울 것

- 트랜잭션과 행 잠금으로 `FOR UPDATE SKIP LOCKED`가 "한 작업은 한 워커만" 처리하게 만드는 원리를 설명할 수 있다.
- 지수 백오프 계산식과 `attempts`가 언제 늘어나는지 정확히 말할 수 있다.
- 어떤 실패가 재시도되고 어떤 실패가 즉시 `failed`가 되는지, 그 분류가 코드 어디에 있는지 안다.
- 워커가 죽었을 때(멈춘 작업)와 같은 작업이 두 번 돌 때(멱등성) 무슨 일이 일어나는지 안다.
- 동시 claim 테스트가 무엇을 어떻게 검증하는지 읽을 수 있다.

## 개념

- **트랜잭션**: 여러 SQL을 하나로 묶어 모두 반영(COMMIT)하거나 모두 취소(ROLLBACK)한다.
- **행 잠금 `SELECT … FOR UPDATE`**: 읽은 행을 트랜잭션이 끝날 때까지 잠근다. 다른 트랜잭션이 같은 행을 `FOR UPDATE`로 읽으려 하면 기다린다.
- **[SKIP LOCKED](glossary.md#skip-locked)**: 기다리지 않고 **이미 잠긴 행을 건너뛴다.** 워커 여러 개가 동시에 "가장 오래된 대기 작업 하나"를 고르면, 각자 서로 다른 행을 집게 된다.
- **[지수 백오프](glossary.md#지수-백오프)**: 실패할 때마다 다음 시도까지의 대기 시간을 2배씩 늘린다. 외부 API 한도에 걸렸을 때 계속 두드리지 않기 위해서다.
- **[멱등성](glossary.md#멱등성)**: 같은 작업을 여러 번 실행해도 결과가 한 번 실행한 것과 같은 성질. 재시도가 있는 시스템에서는 필수다.

## 이 저장소의 구현

### 상태와 테이블

작업 상태 전이 다이어그램은 [docs/architecture.md "작업 상태 전이"](../architecture.md#작업-상태-전이)에 있다. 코드 위치를 붙이면 다음과 같다.

| 전이 | 함수 |
| --- | --- |
| (없음) → `queued` | `src/footnote_rag/jobs/queue.py::enqueue` (호출자 트랜잭션에 추가만, 커밋 안 함) |
| `queued` → `running` | `src/footnote_rag/jobs/queue.py::claim_job` |
| `running` → `succeeded` | `src/footnote_rag/jobs/queue.py::mark_succeeded` (청크 저장과 같은 트랜잭션) |
| `running` → `queued` / `failed` | `src/footnote_rag/jobs/queue.py::mark_failed` |
| 멈춘 `running` → `queued` | `src/footnote_rag/jobs/queue.py::recover_stale_jobs` |

`jobs` 테이블에는 대기 작업을 빨리 찾기 위한 부분 인덱스 `ix_jobs_claim ON jobs (status, run_after, created_at) WHERE status = 'queued'`가 있다(`alembic/versions/0001_initial.py`).

### claim: SKIP LOCKED

출처: `src/footnote_rag/jobs/queue.py::claim_job`

```python
    stmt = (
        select(Job)
        .where(Job.status == "queued", Job.run_after <= func.now())
        .order_by(Job.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = await session.scalar(stmt)
    if job is None:
        await session.rollback()
        return None
    job.status = "running"
    job.attempts = job.attempts + 1
    job.locked_at = func.now()
    job.locked_by = worker_id
    await session.commit()
```

왜 중복 처리가 안 되나:
1. 워커 A의 SELECT가 작업 X를 잠근다.
2. 같은 순간 워커 B의 SELECT는 X가 잠겨 있으므로 **건너뛰고** 다음 행(없으면 0행)을 본다.
3. A는 **같은 트랜잭션 안에서** X를 `running`으로 바꾸고 커밋한다. 잠금이 풀린 뒤에는 X가 `queued`가 아니므로 누구의 WHERE에도 걸리지 않는다.

"SELECT로 고르고, 다른 트랜잭션에서 UPDATE"로 나누면 두 워커가 같은 행을 고를 수 있다. 잠금과 상태 변경이 한 트랜잭션에 있어야 한다.

### 재시도와 지수 백오프

출처: `src/footnote_rag/jobs/queue.py::mark_failed`

```python
    job = await session.get(Job, job_id, with_for_update=True)
    assert job is not None
    error = error[:4000]
    if retryable and job.attempts < job.max_attempts:
        delay = backoff_seconds(job.attempts, base_backoff_seconds)
        job.status = "queued"
        job.run_after = func.now() + timedelta(seconds=delay)
        ...
        return FailureOutcome("queued", job.attempts, delay)
    job.status = "failed"
```

- `attempts`는 **시작한 실행 횟수**다. claim에서 1 늘어난다. 워커가 처리 도중 죽어도 한 번으로 센다([ADR 0001](../adr/0001-postgres-skip-locked-queue.md)).
- 지연 = `2^attempts × BASE_BACKOFF_SECONDS`(`src/footnote_rag/jobs/queue.py::backoff_seconds`). 기본 BASE 5초면 첫 실패 뒤 10초, 두 번째 20초, 세 번째 40초. `tests/test_jobs.py::test_backoff_is_exponential`이 `[10, 20, 40, 80]`을 확인한다.
- `JOB_MAX_ATTEMPTS` 기본 5. 횟수를 다 쓰면 재시도 가능한 실패도 `failed`다.
- 지터(무작위 흔들림)는 없다. 같은 순간 실패한 작업들은 같은 시각에 다시 깨어난다.

### 재시도 가능 / 불가 분류

분류는 두 층에서 일어난다.

1. **외부 오류 → 작업 오류**: 핸들러가 외부 예외를 `RetryableJobError` 또는 `PermanentJobError`로 바꾼다.
   - `src/footnote_rag/jobs/handlers.py::handle_import_dart`: `DartError.retryable`을 그대로 따른다. 파싱 실패(`DartParseError`)는 항상 영구 실패.
   - `src/footnote_rag/jobs/handlers.py::_embed_and_store`: `EmbeddingError.retryable`을 따른다. 임베딩 개수 불일치는 재시도.
2. **작업 오류 → 큐 상태**: `src/footnote_rag/jobs/worker.py::Worker._execute`가 `JobError`면 그 `retryable` 값을, **예상하지 못한 예외는 재시도 불가**로 처리한다(스택 로그를 남김).

외부 오류 표(OpenDART 020/800/900 재시도, 010/011/012/013/014/901 즉시 실패 등)는 [docs/architecture.md](../architecture.md#작업-상태-전이)와 [07. OpenDART 연동](07-dart-integration.md)에 있다.

### 실패 기록: 큐·문서·감사를 한 트랜잭션으로

`src/footnote_rag/jobs/worker.py::Worker._record_failure`는 한 세션 안에서 `mark_failed` → 문서 상태 갱신(`failed` 또는 재시도 예정이면 `queued`, `error`에 사유) → 감사 기록(`document.ingest_failed` 또는 `document.ingest_retry_scheduled`) → 커밋한다. 사용자는 `GET /documents/{id}`의 `error`로 이유를 본다.

### 멈춘 작업 복구

출처: `src/footnote_rag/jobs/queue.py::recover_stale_jobs`

```python
    cutoff = func.now() - timedelta(seconds=lock_timeout_seconds)
    result = await session.execute(
        update(Job)
        .where(Job.status == "running", Job.locked_at < cutoff)
        .values(
            status="queued",
            locked_at=None,
            locked_by=None,
            run_after=func.now(),
            last_error="lock timeout: worker presumed dead",
            updated_at=func.now(),
        )
        .returning(Job.id)
    )
```

- 워커는 시작할 때와 루프 60회마다 이 함수를 부른다(`src/footnote_rag/jobs/worker.py::Worker.run_forever`).
- 기준은 `JOB_LOCK_TIMEOUT_SECONDS` 기본 600초. 복구된 작업은 다음 claim에서 `attempts`가 또 늘어난다. `attempts`가 `max_attempts`를 넘은 작업은 `Worker._execute`가 "max attempts exceeded"로 영구 실패시킨다.
- 테스트: `tests/test_jobs.py::test_stale_running_job_is_recovered`(오래된 것만 복구, 최근 것은 그대로).

### 멱등성: 청크 삭제 후 재삽입

같은 문서의 가져오기 작업이 두 번 돌아도(재시도, 복구 후 재처리, 수동 재등록) 청크가 늘어나지 않는다. `src/footnote_rag/jobs/handlers.py::_embed_and_store`가 한 트랜잭션에서 **문서 행을 잠그고** → 그 문서의 청크를 모두 지우고 → 새 청크를 넣기 때문이다. 문서 행 잠금(`with_for_update=True`) 덕분에 같은 문서를 두 워커가 동시에 저장하더라도 차례로 실행된다.

테스트: `tests/test_jobs.py::test_reingest_does_not_duplicate_chunks`는 같은 `import_dart` 작업을 두 번 더 등록해 돌린 뒤 청크 수와 `chunk_index`가 `0..n-1`로 그대로인지 확인한다.

### 워커 종료 처리

`src/footnote_rag/jobs/worker.py::_main`은 SIGTERM/SIGINT에 `Worker.request_stop`을 연결한다(Windows처럼 `add_signal_handler`가 없으면 `signal.signal`로 대체). `run_forever`는 작업 하나를 끝낼 때마다 `_stop` 이벤트를 확인하므로 **처리 중인 작업은 끝까지 하고** 멈춘다. 대기 중에는 `asyncio.wait_for(self._stop.wait(), timeout=폴링간격)`으로 자기 때문에 신호를 받으면 바로 깨어난다. Docker Compose의 `worker`는 `stop_grace_period: 60s`다. 작업이 그보다 오래 걸리면 강제 종료되고, 그 작업은 멈춘 작업 복구로 다시 큐에 돌아온다.

### 동시 claim 테스트는 어떻게 검증하나

출처: `tests/test_jobs.py::test_concurrent_claims_get_one_job_once`

```python
[job_id] = await _enqueue(sessionmaker)


async def claim(worker_id):
    async with sessionmaker() as s:
        return await claim_job(s, worker_id)


results = await asyncio.gather(*(claim(f"w{i}") for i in range(5)))
claimed = [r for r in results if r is not None]
assert len(claimed) == 1
```

- 작업 하나에 `claim_job` 다섯 개를 **각자 다른 세션(=다른 DB 커넥션·트랜잭션)** 으로 동시에 실행한다. 세션을 공유하면 같은 트랜잭션이 되어 잠금 경쟁 자체가 생기지 않으므로 의미가 없다.
- 실제 Postgres에서 돌기 때문에 SKIP LOCKED가 진짜로 동작한다. SQLite 같은 대체 DB로는 이 검증을 할 수 없다.
- `tests/test_jobs.py::test_two_workers_process_each_job_exactly_once`는 작업 12개를 워커 2개로 비우며 처리 목록에 중복이 없고 빠진 것도 없는지 본다.

## 왜 이렇게 했나 / 대안 / 한계와 개선 방향

**왜 Postgres 큐인가** ([ADR 0001](../adr/0001-postgres-skip-locked-queue.md))
- 문서 행·작업 행·감사 로그를 한 트랜잭션으로 만들 수 있다. 외부 브로커면 "DB 커밋은 됐는데 메시지 발행은 실패"를 막기 위해 outbox 패턴이 필요하다.
- 구성 요소가 Postgres 하나다. 작업 상태를 SQL과 `GET /jobs/{id}`로 바로 본다.

**대안 비교**

| 대안 | 장점 | 이 저장소에서 택하지 않은 이유 |
| --- | --- | --- |
| Celery + Redis | 성숙한 생태계, 스케줄링·재시도 기능 | 구성 요소가 늘고, DB 트랜잭션과 enqueue의 원자성을 따로 맞춰야 함(ADR) |
| FastAPI `BackgroundTasks` | 코드가 가장 짧음 | 프로세스가 죽으면 작업 유실, 재시도·다중 워커 없음(ADR) |
| `LISTEN/NOTIFY` 추가 | 폴링 지연 감소 | 폴링 1초로 충분하다고 봄. claim 로직은 그대로 두고 나중에 추가 가능(ADR) |
| Temporal 같은 워크플로 엔진 | 긴 다단계 작업의 상태·재시도를 엔진이 관리 | ADR에서 검토하지 않았다. 이 저장소의 작업은 단계가 하나(가져오기)뿐이다 |

**한계와 개선 방향**
- 처리 중 heartbeat가 없다. 작업이 `JOB_LOCK_TIMEOUT_SECONDS`보다 오래 걸리면 다른 워커가 다시 가져갈 수 있다. 결과는 멱등하지만 비용(다운로드·임베딩)은 두 번 든다.
- 폴링 간격(`WORKER_POLL_INTERVAL_SECONDS`, 기본 1초)만큼 시작이 늦어진다.
- `failed` 작업을 다시 돌리는 API가 없다. 지금은 SQL로 상태를 바꾸거나 작업을 새로 등록해야 한다. 데드레터 큐도 없다.
- 백오프에 지터가 없다.
- 처리량이 아주 커지면 `jobs` 테이블의 bloat·vacuum 비용을 봐야 한다(ADR). 처리량은 측정하지 않았다.

## 확인 질문

1. `claim_job`에서 SELECT와 UPDATE를 서로 다른 트랜잭션으로 나누면 무엇이 깨지나?
   <details><summary>답</summary>

   SELECT 트랜잭션이 끝나는 순간 잠금이 풀리므로, 두 워커가 같은 `queued` 행을 각각 고른 뒤 둘 다 `running`으로 바꾸고 둘 다 실행할 수 있다. 잠금을 잡은 트랜잭션 안에서 상태를 바꿔야 한다.
   </details>

2. 기본 설정(`BASE_BACKOFF_SECONDS=5`, `JOB_MAX_ATTEMPTS=5`)에서 OpenDART가 계속 020을 돌려주면 작업은 어떻게 되나?
   <details><summary>답</summary>

   020은 재시도 가능이다. 1~4번째 시도 실패 뒤 각각 10, 20, 40, 80초 뒤로 `run_after`가 미뤄지고, 5번째 시도도 실패하면 `attempts`(5)가 `max_attempts`(5)보다 작지 않으므로 `failed`가 된다. 문서도 `failed`가 되고 `error`에 사유가 남는다.
   </details>

3. 핸들러 안에서 `KeyError` 같은 예상하지 못한 예외가 나면?
   <details><summary>답</summary>

   `Worker._execute`가 `JobError`가 아니므로 재시도 불가로 보고 `log.exception`으로 스택을 남긴 뒤 `failed`로 기록한다.
   </details>

4. 같은 작업이 두 번 실행돼도 청크가 중복되지 않는 이유를 두 가지 장치로 설명하라.
   <details><summary>답</summary>

   (1) 저장 트랜잭션이 그 문서의 청크를 먼저 모두 지우고 다시 넣는다. (2) 그 트랜잭션이 문서 행을 `FOR UPDATE`로 잠가 같은 문서의 저장이 동시에 겹치지 않는다.
   </details>

5. 동시 claim 테스트에서 각 claim이 별도 세션을 쓰는 이유는?
   <details><summary>답</summary>

   행 잠금 경쟁은 서로 다른 트랜잭션 사이에서만 생긴다. 별도 세션이어야 별도 커넥션·트랜잭션이 되어 실제 워커 여러 개가 동시에 claim하는 상황을 재현한다.
   </details>
