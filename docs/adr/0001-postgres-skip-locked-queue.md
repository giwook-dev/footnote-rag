# ADR 0001: 작업 큐는 Postgres `FOR UPDATE SKIP LOCKED` (Celery/Redis 대신)

- 상태: 채택
- 날짜: 2026-11-24

## 맥락

DART 원문 다운로드·파싱·임베딩은 수 초~수십 초가 걸리고 외부 API 한도(OpenDART 020, Voyage 레이트 리밋)에 걸릴 수 있어 요청 경로 밖에서 재시도와 함께 처리해야 합니다. 작업량은 조직당 공시 수십 건 수준입니다.

## 결정

`jobs` 테이블을 큐로 씁니다.

- claim: `SELECT … WHERE status='queued' AND run_after<=now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1` 후 같은 트랜잭션에서 `running`, `attempts+1`, `locked_at`, `locked_by`를 기록하고 커밋합니다.
- 재시도 가능한 실패: 횟수가 남았으면 `queued` + `run_after = now() + 2^attempts × BASE_BACKOFF_SECONDS`, 아니면 `failed`.
- 재시도 불가 실패: 즉시 `failed`. `last_error`와 문서 `error`를 남깁니다.
- 멈춘 작업: `running`인데 `locked_at`이 `JOB_LOCK_TIMEOUT_SECONDS`보다 오래되면 `queued`로 되돌립니다(워커 시작 시와 주기적으로).
- `attempts`는 **시작한 실행 횟수**입니다(claim에서 증가). 워커가 죽은 경우도 시도 한 번으로 셉니다.

## 근거

- **원자성**: 문서 행·작업 행·감사 로그를 한 트랜잭션으로 만들 수 있습니다(AC-10). 외부 브로커를 쓰면 "DB 커밋은 됐는데 메시지는 안 갔다"를 막기 위해 outbox 패턴이 따로 필요합니다.
- **운영 단순성**: Postgres 하나로 끝납니다. Redis·Celery 설정, 직렬화, 결과 백엔드가 필요 없습니다.
- **관측성**: 작업 상태를 SQL과 `GET /jobs/{id}`로 바로 볼 수 있습니다.
- **정확성 검증 가능**: 동시 claim, 백오프, 멈춘 작업 복구를 실제 Postgres로 테스트합니다(`tests/test_jobs.py`).

## 대안

| 대안 | 기각 이유 |
| --- | --- |
| Celery + Redis | 이 규모에 비해 구성 요소가 많고, DB 트랜잭션과 enqueue의 원자성을 따로 맞춰야 함 |
| FastAPI `BackgroundTasks` | 프로세스가 죽으면 작업 유실, 재시도·다중 워커 없음 |
| `LISTEN/NOTIFY` | 지연은 줄지만 폴링 1초로 충분. 필요하면 claim 로직은 그대로 두고 추가 가능 |

## 결과와 한계

- 폴링 간격(`WORKER_POLL_INTERVAL_SECONDS`, 기본 1초)만큼 지연이 생깁니다.
- 처리량이 초당 수백 건을 넘으면 테이블 bloat·vacuum 비용을 고려해 전용 큐로 옮길 수 있습니다. 서비스 함수가 `enqueue()` 하나로 감싸져 있어 교체 지점이 좁습니다.
- 지금은 처리 중 작업의 heartbeat가 없어, 처리 시간이 `JOB_LOCK_TIMEOUT_SECONDS`(기본 600초)를 넘으면 다른 워커가 다시 가져갈 수 있습니다. 재처리는 청크를 지우고 다시 넣으므로 결과는 중복되지 않습니다.
