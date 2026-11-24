# 05. RAG 검색: 임베딩, 청킹, pgvector

> 예상 소요: 45분 · 이전: [04. 작업 큐](04-job-queue.md) · 다음: [06. Claude Citations와 각주](06-claude-citations.md)

## 이 문서에서 배울 것

- 임베딩과 cosine 유사도가 무엇이고, 이 저장소의 "점수"가 어떻게 계산되는지 안다.
- 문서와 질문을 서로 다른 `input_type`으로 임베딩하는 이유를 말할 수 있다.
- 청킹 크기·겹침이 검색과 인용에 미치는 영향과, 이 저장소가 표를 행 단위로 자르는 방식을 안다.
- pgvector HNSW 인덱스와 테넌트 필터를 함께 쓸 때 생기는 문제와 `hnsw.iterative_scan`이 그것을 어떻게 막는지 안다.
- `MIN_RETRIEVAL_SCORE`가 무엇을 결정하고, 이 값이 아직 실제 데이터로 보정되지 않았다는 점을 안다.

## 개념

- **[임베딩](glossary.md#임베딩)**: 텍스트를 고정 길이 숫자 벡터로 바꾼 것. 의미가 비슷한 텍스트는 벡터 공간에서 가깝다. 이 저장소의 기본 차원은 1,024(`EMBEDDING_DIM`)다.
- **[cosine 유사도](glossary.md#cosine-유사도)**: 두 벡터 사이 각도의 코사인. 1에 가까울수록 방향이 같다(비슷하다). pgvector의 cosine 거리는 `1 - 유사도`다.
- **비대칭 검색(`input_type`)**: 질문은 짧고 문서 조각은 길다. Voyage는 같은 모델이라도 `input_type="query"`와 `"document"`를 구분해 임베딩할 수 있다([ADR 0003](../adr/0003-embedding-provider-voyage.md)).
- **[청킹](glossary.md#청킹)**: 긴 문서를 검색·인용 단위 조각으로 나누는 것. 너무 크면 관련 없는 내용이 섞이고 인용문이 길어지며, 너무 작으면 문맥이 끊긴다. **겹침(overlap)** 은 조각 경계에서 문장이 잘려 의미를 잃는 것을 줄인다.
- **[HNSW](glossary.md#hnsw)**: 근사 최근접 이웃(ANN) 인덱스. 모든 벡터와 거리를 재지 않고 그래프를 따라가며 가까운 후보를 빨리 찾는다. 정확도를 조금 내주고 속도를 얻는다.

## 이 저장소의 구현

### 1. 청킹: 섹션 경계를 넘지 않는다

출처: `src/footnote_rag/documents/chunking.py::chunk_section_text`

```python
    for line in lines:
        added = len(line) + (1 if current else 0)
        if current and length + added > size:
            chunks.append("\n".join(current))
            current = _tail(current, overlap)
            length = len("\n".join(current))
            added = len(line) + (1 if current else 0)
            if current and length + added > size:
                current, length, added = [], 0, len(line)
        current.append(line)
        length += added
```

- **문자 기준**이다(토큰 아님). 기본 `CHUNK_SIZE=1500`, `CHUNK_OVERLAP=200`.
- **줄 단위**로 채운다. 파서가 표를 행마다 한 줄(`셀 | 셀 | 셀`)로 만들기 때문에 **표 행이 중간에 잘리지 않는다.**
- 다음 청크는 이전 청크 끝의 "통째 줄들" 중 합계 200자 이하를 가져와 시작한다(`_tail`). 겹침이 정확히 200자가 아닐 수 있다.
- `size`보다 긴 한 줄은 슬라이딩 윈도우로 자른다(`_split_long_line`, 1,500자 창, 1,300자 간격).
- `src/footnote_rag/documents/chunking.py::chunk_sections`는 섹션별로 이 함수를 부르므로 **청크가 섹션 경계를 넘지 않고**, 모든 청크가 자기 `section_path`를 가진다. 이것이 각주에 목차 경로를 보여 줄 수 있는 이유다.
- 테스트: `tests/test_footnotes.py::test_chunking_respects_size_overlap_and_rows`, `tests/test_footnotes.py::test_long_line_is_windowed_and_sections_keep_paths`.

### 2. 임베딩: 문서는 배치로, 질문은 하나씩

- 문서: `src/footnote_rag/retrieval/embedder.py::VoyageEmbedder.embed_documents`가 `src/footnote_rag/retrieval/embedder.py::iter_batches`로 나눠 `input_type="document"`로 호출한다. 배치 한도는 텍스트 1,000개(`EMBED_BATCH_MAX_TEXTS`, 코드에서 1,000 초과 불가)와 추정 토큰 100,000(`EMBED_BATCH_MAX_TOKENS`). 토큰은 **문자 수 = 토큰 수**로 보수적으로 추정한다(`estimate_tokens`). 한국어는 실제 토큰보다 크게 잡혀 한도 초과를 피한다.
- 질문: `src/footnote_rag/retrieval/embedder.py::VoyageEmbedder.embed_query`가 `input_type="query"`로 하나만 보낸다.
- 오류: 레이트 리밋·서버·연결·타임아웃은 `EmbeddingError(retryable=True)`, 그 밖의 Voyage 오류는 `retryable=False`. 워커는 이것을 작업 재시도로, API는 503으로 바꾼다.
- Fake: `src/footnote_rag/retrieval/embedder.py::FakeEmbedder`는 단어와 글자 bigram을 해시해 벡터 칸에 ±1을 더하고 L2 정규화한다. 네트워크 없이 "비슷한 글자가 많으면 가깝다"는 성질만 흉내 낸다. 의미 검색 품질과는 무관하다.

### 3. 벡터 검색: 테넌트 필터 + iterative scan

출처: `src/footnote_rag/retrieval/search.py::search_chunks`

```python
    distance = Chunk.embedding.cosine_distance(query_vector)
    stmt = (
        select(Chunk, Document, distance.label("distance"))
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.org_id == org_id,
            Document.org_id == org_id,
            Document.status == "ready",
        )
        .order_by(distance)
        .limit(top_k)
    )
    if document_ids is not None:
        stmt = stmt.where(Document.id.in_(document_ids))
    # With tenant filters an HNSW scan could return fewer than top_k rows; pgvector >= 0.8
    # iterative scans keep scanning until enough rows pass the WHERE clause.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
```

- 점수는 `1.0 - distance`, 즉 cosine 유사도이며 범위는 -1~1이다(`RetrievedChunk.score`).
- 인덱스: `CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)`(`alembic/versions/0001_initial.py`). 인덱스 파라미터는 지정하지 않았으므로 pgvector 기본값이다. `(org_id)`, `(document_id)` 일반 인덱스도 있다.
- **문제**: HNSW는 "가까운 후보 몇 개"를 먼저 찾고 그다음 WHERE로 거른다. 다른 조직 청크가 많으면 후보 대부분이 걸러져 `top_k`보다 적게 나올 수 있다.
- **해결**: `hnsw.iterative_scan = strict_order`(pgvector 0.8 이상)는 조건을 통과한 행이 `LIMIT`만큼 모일 때까지 인덱스를 계속 탐색하고, 거리 순서를 엄격히 지킨다. `SET LOCAL`이라 이 트랜잭션에만 적용된다.
- 조직 조건을 `chunks.org_id`와 `documents.org_id` **양쪽**에 거는 것은 방어를 겹친 것이다([03](03-auth-rbac-isolation.md)). `status == "ready"` 조건 덕분에 처리 중이거나 실패한 문서의 청크는 검색되지 않는다.
- 테스트: `tests/test_answering.py::test_search_is_scoped_to_org_ready_docs_and_document_ids`.

### 4. `MIN_RETRIEVAL_SCORE`: 검색 신뢰도 게이트

`src/footnote_rag/answering/service.py::review_reasons`는 최고 점수가 `MIN_RETRIEVAL_SCORE`(기본 0.35)보다 낮으면 `low_retrieval_score`를 붙여 답변을 `pending_review`로 보낸다. 답변 자체는 만들어지지만 member에게 바로 공개되지 않는다.

- 이 기본값을 **실제 Voyage 점수 분포로 보정한 기록은 저장소에 없다.** 실제 데이터와 Eval 데이터셋이 없기 때문이다.
- 테스트 환경은 `MIN_RETRIEVAL_SCORE=0.05`를 쓴다(`tests/conftest.py`). Fake 임베더 점수는 실제 모델과 분포가 달라서다.
- 값을 올리면 검토 대기가 늘고(안전), 내리면 근거가 약한 답도 공개된다. [실습 2](exercises.md)에서 직접 바꿔 본다.

## 왜 이렇게 했나 / 대안 / 한계와 개선 방향

**왜 Voyage인가** ([ADR 0003](../adr/0003-embedding-provider-voyage.md))
- Anthropic은 임베딩 API를 제공하지 않는다. ADR은 Anthropic 문서가 Voyage를 안내하고, 질의/문서 `input_type`을 지원한다는 점을 근거로 든다.
- 1,024차원으로 고정해 인덱스 크기를 관리한다. 차원은 마이그레이션 시점에 `vector(n)` 컬럼으로 고정되므로 모델·차원을 바꾸면 컬럼 변경과 전체 재임베딩이 필요하다.

**왜 pgvector인가**: 조직 필터, 문서 상태, 벡터 거리를 SQL 한 문장으로 처리하고, 청크 저장을 문서 상태 변경과 같은 트랜잭션에 넣을 수 있다.

**대안**
- 로컬 임베딩 모델: 비용·외부 전송이 없지만 이미지 크기와 CPU 추론 지연이 늘고 한국어 회계 도메인 품질을 따로 검증해야 한다(ADR).
- 키워드 검색(Postgres FTS, BM25): 숫자·고유명사 정확 일치에 강하지만 한국어 형태소 분석기가 필요하다(ADR).
- 하이브리드 검색과 재랭킹(reranker): 이 저장소에는 **없다.** ADR은 Eval로 recall이 부족하다고 확인되면 검토하겠다고 적었다.
- 토큰 기준 청킹: 모델 한도와 더 정확히 맞지만 토크나이저 의존성이 생긴다.

**한계와 개선 방향**
- 실제 보고서로 측정한 recall@k가 없다. 청크 크기 1,500자, 겹침 200자, `TOP_K=8`, `MIN_RETRIEVAL_SCORE=0.35`는 모두 **실험으로 정한 값이 아니라 설정 기본값**이다.
- 청크를 Claude에게 text 블록 하나로 보내기 때문에 인용문(`cited_text`)이 청크 전체가 된다([06](06-claude-citations.md)).
- `document_ids`로 범위를 좁힐 수는 있지만, 회사명·연도 같은 메타데이터 필터 API는 없다.
- HNSW 파라미터(`m`, `ef_construction`, `ef_search`)를 조정하지 않았고, 인덱스 성능을 측정하지 않았다.

## 확인 질문

1. 검색 점수 0.9와 0.2는 각각 무엇을 뜻하나? 점수는 어디서 계산되나?
   <details><summary>답</summary>

   cosine 유사도다. 0.9는 질문 벡터와 청크 벡터의 방향이 매우 비슷하다는 뜻, 0.2는 덜 비슷하다는 뜻이다. `src/footnote_rag/retrieval/search.py::search_chunks`에서 pgvector cosine 거리를 받아 `1.0 - distance`로 바꾼다.
   </details>

2. `hnsw.iterative_scan`을 끄면 어떤 증상이 나타날 수 있나?
   <details><summary>답</summary>

   다른 조직의 청크가 많을 때 HNSW가 찾은 후보 대부분이 `org_id` 조건에 걸러져 `top_k`보다 적은 결과가 나올 수 있다. 심하면 관련 청크가 있는데도 0건이 되어 "근거 없음" 답변이 나온다.
   </details>

3. 표가 청크 경계에서 행 중간이 잘리지 않는 이유는?
   <details><summary>답</summary>

   파서(`src/footnote_rag/dart/parser.py`)가 표를 행마다 한 줄로 만들고, 청킹이 줄 단위로 채우기 때문이다. 한 행이 1,500자를 넘는 예외적인 경우에만 슬라이딩 윈도우로 잘린다.
   </details>

4. `MIN_RETRIEVAL_SCORE`를 0.35에서 0.6으로 올리면 무엇이 달라지나?
   <details><summary>답</summary>

   최고 점수가 0.6 미만인 질문의 답변이 모두 `pending_review`(`low_retrieval_score`)가 된다. 공개되는 답은 줄고 검토 부담은 늘어난다. Claude 호출 여부는 바뀌지 않는다(검색 결과가 1건 이상이면 호출).
   </details>
