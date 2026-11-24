# 02. 요청 두 개를 코드 순서대로 따라가기

> 예상 소요: 60분 · 이전: [01. 전체 그림](01-big-picture.md) · 다음: [03. 인증·인가·조직 격리](03-auth-rbac-isolation.md)

## 이 문서에서 배울 것

- `POST /documents/dart` 하나가 202 응답을 받은 뒤 워커에서 청크가 저장되기까지 거치는 모든 함수를 순서대로 말할 수 있다.
- `POST /questions` 하나가 각주 달린 답변이 되기까지의 단계와, 각 단계에서 실패하면 어떤 HTTP 상태가 나가는지 안다.
- 각 요청에서 **트랜잭션이 어디서 열리고 커밋되는지** 짚을 수 있다.
- 기존 시퀀스 다이어그램의 각 화살표가 어느 `파일::함수`에 해당하는지 연결할 수 있다.

## 개념

- **202 Accepted**: "요청은 받았고 처리는 나중에 한다"는 응답. 원문 다운로드·임베딩은 수 초~수십 초가 걸릴 수 있어 요청 경로에서 하지 않는다. 클라이언트는 `GET /jobs/{id}`로 상태를 확인한다.
- **트랜잭션 경계**: 여러 행을 "모두 반영되거나 모두 반영되지 않게" 묶는 범위. 이 저장소는 세션을 요청마다 하나 열고(`src/footnote_rag/db/session.py::get_session`), **서비스 함수가 직접 `commit()`** 한다.
- **임베딩**: 텍스트를 의미가 비슷하면 가까운 숫자 벡터로 바꾸는 것([용어집](glossary.md#임베딩)). 문서용과 질문용 모드가 따로 있다([05](05-rag-retrieval.md)).

## 이 저장소의 구현

### 요청 ① `POST /documents/dart` → 청크 저장

기존 시퀀스 다이어그램: [docs/architecture.md "가져오기 흐름"](../architecture.md#가져오기-흐름). 아래 표는 그 화살표마다 코드 위치를 붙인 것이다.

| # | 단계 | 코드 위치 | 핵심 |
| --- | --- | --- | --- |
| 1 | 요청 검증 | `src/footnote_rag/documents/router.py::import_dart` | `DartImportIn.rcept_no`가 정규식 `^\d{14}$`. 권한은 `require_role(*ANY_MEMBER)` |
| 2 | 중복 확인 | `src/footnote_rag/documents/service.py::_find_dart_document` | 같은 조직·`source="dart"`·같은 `rcept_no`가 있으면 기존 문서를 돌려줌(HTTP 200, `duplicate: true`) |
| 3 | 문서·작업·감사 생성 | `src/footnote_rag/documents/service.py::request_dart_import` | `Document(status="queued")` + `src/footnote_rag/jobs/queue.py::enqueue` + `src/footnote_rag/audit/service.py::record`를 **한 번에 커밋** |
| 4 | 동시 중복 처리 | 같은 함수의 `except IntegrityError` | `UNIQUE(org_id, source, source_ref)` 위반이면 롤백 후 기존 문서 반환 |
| 5 | 응답 | `import_dart` | 처음이면 202 `{document_id, job_id, status}` |
| 6 | 워커가 작업 claim | `src/footnote_rag/jobs/worker.py::Worker.run_once` → `src/footnote_rag/jobs/queue.py::claim_job` | `FOR UPDATE SKIP LOCKED`, `attempts+1`, `running`, 커밋 |
| 7 | 핸들러 선택 | `src/footnote_rag/jobs/worker.py::Worker._execute` | `HANDLERS["import_dart"]` |
| 8 | 문서 processing 표시 | `src/footnote_rag/jobs/handlers.py::_mark_processing` | 별도 세션에서 바로 커밋 |
| 9 | 원문 ZIP 얻기 | `src/footnote_rag/jobs/handlers.py::load_dart_zip` → `src/footnote_rag/dart/client.py::HttpDartClient.download_document` | `data/dart/{rcept_no}.zip` 캐시가 있으면 네트워크 호출 없음 |
| 10 | 파싱 | `src/footnote_rag/dart/parser.py::parse_document_zip` | 본문 XML 선택 → 목차 경로별 섹션 목록 |
| 11 | 종목코드 보강 | `src/footnote_rag/jobs/handlers.py::_lookup_stock_code` | 실패해도 경고 로그만 남김(best effort) |
| 12 | 청킹 | `src/footnote_rag/documents/chunking.py::chunk_sections` | 섹션 경계 유지, 기본 1,500자 / 200자 겹침 |
| 13 | 임베딩 | `src/footnote_rag/retrieval/embedder.py::VoyageEmbedder.embed_documents` | `input_type="document"`, 개수·토큰 한도로 배치 |
| 14 | 저장 | `src/footnote_rag/jobs/handlers.py::_embed_and_store` | 한 트랜잭션: 문서 행 잠금 → 기존 청크 삭제 → 새 청크 삽입 → 문서 `ready` → `src/footnote_rag/jobs/queue.py::mark_succeeded` → 감사 기록 → 커밋 |
| 15 | 실패 시 | `src/footnote_rag/jobs/worker.py::Worker._record_failure` → `src/footnote_rag/jobs/queue.py::mark_failed` | 재시도 가능하면 `queued` + 백오프, 아니면 `failed`. 문서 상태와 감사 로그도 같이 갱신 |

3단계의 핵심 발췌:

출처: `src/footnote_rag/documents/service.py::request_dart_import`

```python
    session.add(doc)  # INSERT happens at commit; ids are generated client-side
    job = enqueue(
        session,
        type="import_dart",
        payload={"document_id": str(doc.id), "org_id": str(org_id), "rcept_no": rcept_no},
        max_attempts=settings.job_max_attempts,
    )
    record(
        session,
        action=Actions.DART_IMPORT_REQUESTED,
        ...
    )
    try:
        await session.commit()  # document + job + audit in one transaction
    except IntegrityError:
        # A concurrent request created the same (org, dart, rcept_no) first.
        await session.rollback()
        existing = await _find_dart_document(session, org_id, rcept_no)
```

읽는 포인트:
- `enqueue`와 `record`는 **커밋하지 않고** 세션에 행을 추가만 한다. 그래서 문서·작업·감사가 셋 다 생기거나 셋 다 안 생긴다. 이를 검증하는 테스트가 `tests/test_documents.py::test_failed_commit_leaves_neither_document_nor_job`이다.
- `flush()`를 하지 않는 이유가 있다. flush 시점에 UNIQUE 위반이 나면 `except IntegrityError` 경로를 타지 못했기 때문에, 커밋에서만 위반이 나도록 고쳤다([docs/ai-usage.md](../ai-usage.md) "AI 결과를 고친 사례"). 동시 요청 테스트: `tests/test_documents.py::test_concurrent_duplicate_imports_create_one_document`.
- `org_id = user.org_id`를 미리 지역 변수로 잡는다. 롤백하면 ORM 객체 `user`의 속성이 만료되어 다시 읽을 때 문제가 생기기 때문이다.

14단계의 핵심 발췌:

출처: `src/footnote_rag/jobs/handlers.py::_embed_and_store`

```python
    async with ctx.sessionmaker() as session:
        doc = await session.get(Document, document_id, with_for_update=True)
        if doc is None:
            raise PermanentJobError(f"document {document_id} not found")
        await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
        session.add_all(
            Chunk(
                id=uuid.uuid4(),
                document_id=document_id,
                org_id=doc.org_id,
                ...
            )
            for d, vec in zip(drafts, embedded.vectors, strict=True)
        )
        ...
        doc.status = "ready"
        await mark_succeeded(session, job.id, result)
        record(session, action=Actions.INGEST_SUCCEEDED, ...)
        await session.commit()
```

다운로드·파싱·임베딩(느리고 실패할 수 있는 외부 호출)은 **트랜잭션 밖**에서 끝내고, DB 쓰기만 짧은 트랜잭션으로 묶는다. 청크의 `org_id`는 요청자가 아니라 **문서 행에서** 복사한다. 이 값이 나중에 벡터 검색의 테넌트 필터가 된다.

### 요청 ② `POST /questions` → 각주 달린 답변

기존 시퀀스 다이어그램: [docs/architecture.md "질문 처리 시퀀스"](../architecture.md#질문-처리-시퀀스).

| # | 단계 | 코드 위치 | 핵심 |
| --- | --- | --- | --- |
| 1 | 요청 검증 | `src/footnote_rag/answering/router.py::post_question` | `QuestionIn.question` 2~2,000자, 선택 `document_ids`. 임베더·LLM은 `app.state`에서 주입 |
| 2 | 문서 범위 확인 | `src/footnote_rag/answering/service.py::_check_document_scope` | 요청한 `document_ids` 중 하나라도 다른 조직 것이면 `DocumentScopeError` → 404 |
| 3 | 질문 임베딩 | `src/footnote_rag/answering/service.py::retrieve` → `embed_query` | `input_type="query"`. 실패하면 `RetrievalUnavailableError` → 503 |
| 4 | 벡터 검색 | `src/footnote_rag/retrieval/search.py::search_chunks` | 같은 조직, `ready` 문서, cosine 거리순 `TOP_K`(기본 8)개 |
| 5a | 결과 0건 | `src/footnote_rag/answering/service.py::ask_question` | Claude를 **호출하지 않고** "제공된 문서에서 근거를 찾지 못했습니다." |
| 5b | 요청 구성 | `src/footnote_rag/answering/prompt.py::build_messages` | 청크마다 `search_result` 블록 + 마지막에 질문 text 블록 |
| 6 | Claude 호출 | `src/footnote_rag/answering/llm.py::ClaudeClient.answer` | adaptive thinking, `effort`, 서버 측 fallback. 오류는 `LLMError`(502/503) |
| 7 | 오류 시 | `ask_question`의 `except LLMError` | 롤백 → `question.failed` 감사 기록만 커밋 → 예외 다시 던짐 → 라우터가 502/503 |
| 8 | 거절 처리 | `ask_question` | `stop_reason == "refusal"`이면 content를 읽지 않고 고정 문구 |
| 9 | 각주 변환 | `src/footnote_rag/answering/footnotes.py::build_footnotes` | citations → `[n]`과 각주 목록 |
| 10 | 신뢰도 규칙 | `src/footnote_rag/answering/service.py::review_reasons` | 이유가 하나라도 있으면 `pending_review`, 없으면 `published` |
| 11 | 비용 계산 | `src/footnote_rag/usage/pricing.py::compute_cost` | 응답의 **실제** `model`로 가격표 조회 |
| 12 | 저장·감사 | `ask_question` | `questions`, `answers`, `llm_calls`, 감사 2건(`question.asked`, `answer.created`)을 한 번에 커밋 |
| 13 | 응답 | `src/footnote_rag/answering/router.py::_visible_to` | member는 `pending_review` 답변의 본문·각주 대신 안내 문구를 받음 |

핵심 분기 발췌:

출처: `src/footnote_rag/answering/service.py::ask_question`

```python
    llm_response: LLMResponse | None = None
    if not retrieved:
        text, footnotes = NO_EVIDENCE_TEXT, []
    else:
        try:
            llm_response = await llm.answer(
                system=SYSTEM_PROMPT, messages=build_messages(question, retrieved)
            )
        except LLMError as exc:
            org_id, user_id = user.org_id, user.id  # read before rollback expires `user`
            await session.rollback()
            record(session, action=Actions.QUESTION_FAILED, ...)
            await session.commit()
            raise
        if llm_response.stop_reason == "refusal":
            text, footnotes = REFUSAL_TEXT, []
        else:
            built = build_footnotes(llm_response.content, to_source_refs(retrieved))
            text, footnotes = built.text or NO_EVIDENCE_TEXT, built.footnotes
```

읽는 포인트:
- 검색 결과가 없으면 LLM 비용을 쓰지 않는다. 이 경우 `llm_calls` 행도 생기지 않는다(`tests/test_answering.py::test_no_retrieval_results_skips_claude_and_goes_to_review`).
- LLM이 실패해도 **실패 사실은 감사 로그에 남는다.** 롤백 뒤 새로 기록하고 커밋하는 순서가 중요하다. 이 경로의 500 버그를 잡은 테스트가 `tests/test_answering.py::test_llm_error_returns_502_and_is_audited`이다.
- `answer.created` 감사 metadata에는 상태, 검토 이유, 각주 수, 모델, `stop_reason`, 비용이 들어가지만 **질문·답변 본문은 들어가지 않는다**(`FORBIDDEN_METADATA_KEYS`에 `text`, `content`).

### 응답에 들어 있는 것

`POST /questions` 응답(`AnswerOut`)의 필드: `answer_id`, `question_id`, `status`, `review_reason`, `text`, `footnotes`, `usage`(모델·토큰·비용), `latency_ms`, `created_at`. `latency_ms`는 **요청 전체**(임베딩+검색+Claude) 시간이고, `llm_calls.latency_ms`는 **Claude 호출만**의 시간이다. 둘을 혼동하지 말자([08](08-review-audit-usage.md)).

## 왜 이렇게 했나 / 대안 / 한계와 개선 방향

**왜**
- 가져오기를 비동기로 뺀 이유와 큐 선택: [ADR 0001](../adr/0001-postgres-skip-locked-queue.md), [04. 작업 큐](04-job-queue.md).
- 질문 답변은 **동기**로 처리한다. 사용자는 답을 기다리고 있고, 실패 시 502/503으로 바로 알 수 있다.

**대안**
- 질문도 비동기 작업으로 만들고 결과를 폴링하게 할 수 있다. 응답 시간이 긴 경우 HTTP 타임아웃을 피할 수 있지만, 클라이언트가 복잡해진다.
- 스트리밍(SSE)으로 답변을 조금씩 보내는 방법도 있다. 지금은 비스트리밍 요청이며(`CLAUDE_MAX_TOKENS` 기본 16,000), 각주 변환이 응답 전체를 받은 뒤에 일어난다.

**한계와 개선 방향**
- 질문 처리 시간은 측정한 기록이 없다. `/usage`의 p50/p95는 운영 중 쌓인 `llm_calls`로 계산할 수 있지만 실제 키로 돌린 데이터가 없다.
- 같은 질문을 반복해도 매번 Claude를 부른다(답변 캐시 없음).
- 가져오기 진행률(몇 % 임베딩됨)은 없다. 상태는 `queued → processing → ready/failed`뿐이다.

## 확인 질문

1. `POST /documents/dart`에서 문서 행은 만들어졌는데 작업 행이 없는 상황이 생길 수 있나?
   <details><summary>답</summary>

   생기지 않도록 설계했다. `request_dart_import`가 문서·작업·감사를 같은 세션에 추가한 뒤 한 번만 커밋한다. 커밋이 실패하면 셋 다 롤백된다. `tests/test_documents.py::test_dart_import_creates_document_and_job_atomically`와 `tests/test_documents.py::test_failed_commit_leaves_neither_document_nor_job`이 검증한다.
   </details>

2. 같은 조직의 두 사용자가 같은 `rcept_no`를 동시에 요청하면?
   <details><summary>답</summary>

   둘 다 `_find_dart_document`에서 "없음"을 볼 수 있지만, 커밋 시 `UNIQUE(org_id, source, source_ref)` 제약 때문에 하나만 성공한다. 진 쪽은 `IntegrityError`를 받아 롤백하고 기존 문서를 다시 조회해 200 `duplicate: true`로 응답한다.
   </details>

3. 검색 결과가 0건일 때 Claude를 호출하지 않는 것의 장단점은?
   <details><summary>답</summary>

   장점: 비용이 0이고, 근거 없이 모델이 일반 지식으로 답할 위험이 없다. 단점: 사용자는 "근거 없음" 답을 받고 그 답은 `pending_review`(`no_retrieval_results`)로 간다. 질문이 문서와 무관한지, 아직 문서가 `ready`가 아닌지는 이 응답만으로 구분되지 않는다.
   </details>

4. 워커가 임베딩을 트랜잭션 밖에서 하는 이유는?
   <details><summary>답</summary>

   외부 API 호출은 느리고 실패할 수 있다. 그동안 트랜잭션과 행 잠금을 잡고 있으면 다른 작업이 막힌다. 외부 작업을 끝낸 뒤 DB 쓰기만 짧은 트랜잭션(`_embed_and_store`의 `async with` 블록)으로 처리한다.
   </details>
