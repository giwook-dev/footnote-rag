# ADR 0003: 임베딩 제공자는 Voyage `voyage-4`

- 상태: 채택
- 날짜: 2026-11-24

## 맥락

LLM 제공자는 Claude로 정했지만 Anthropic은 임베딩 API를 제공하지 않습니다. 한국어 감사보고서(서술형 주석 + 숫자 표)를 의미 검색해야 합니다.

## 결정

Voyage AI `voyage-4`(1,024차원, `EMBEDDING_MODEL`/`EMBEDDING_DIM`으로 변경 가능)를 공식 `voyageai` SDK로 호출합니다.

- 문서 청크는 `input_type="document"`, 질문은 `input_type="query"`.
- 요청당 텍스트 1,000개 한도와 토큰 한도를 지키도록 배치를 나눕니다. 토큰은 문자 수로 보수적으로 추정합니다(한국어에서는 과대 추정 → 한도 초과 방지). 기본 배치 토큰 상한 `EMBED_BATCH_MAX_TOKENS=100000`.
- 레이트 리밋·서버·연결·타임아웃 오류는 재시도 가능한 실패로 분류해 작업 큐의 백오프에 맡깁니다. (보완: [ADR 0007](0007-embedding-rate-limit-and-cache.md)에서 먼저 배치 단위로 재시도하고, 클라이언트 측 속도 제한과 임베딩 캐시를 더했습니다.)
- `Embedder` Protocol 뒤에 두고, 테스트·Fake 모드는 해시 기반 결정적 벡터(`FakeEmbedder`: 단어·문자 bigram feature hashing, L2 정규화)를 씁니다.

## 근거

- Anthropic 문서가 임베딩 제공자로 Voyage를 안내하며, 다국어 검색 품질이 좋습니다.
- 질의/문서 비대칭 `input_type`을 지원해 짧은 질문과 긴 청크의 매칭이 좋아집니다.
- 차원을 1,024로 고정해 pgvector HNSW 인덱스 크기와 검색 속도를 관리할 수 있습니다.

## 대안

| 대안 | 장점 | 기각 이유 |
| --- | --- | --- |
| 로컬 임베딩 모델(예: 다국어 sentence-transformers) | API 비용 없음, 데이터 외부 전송 없음 | 이미지 크기·CPU 추론 지연 증가, 한국어 회계 도메인 품질을 따로 검증해야 함 |
| 키워드 검색(Postgres FTS, BM25) | 숫자·고유명사 정확 일치에 강함 | 한국어 형태소 분석기 설정 필요, 의미 질의("리스는 어떻게 측정?")에 약함 |
| 하이브리드(키워드 + 벡터) | 두 장점 결합 | 이번 범위에서는 복잡도 대비 효과 측정이 먼저. Eval로 recall이 부족하면 다음 단계로 검토 |

## 결과

- `EMBEDDING_DIM`은 마이그레이션 시점에 `vector(n)` 컬럼으로 고정됩니다. 모델·차원을 바꾸면 컬럼 변경과 전체 재임베딩이 필요합니다.
- 임베딩 토큰 사용량은 수집 작업 `result`와 `document.ingest_succeeded` 감사 metadata에 남습니다(비용 계산은 하지 않음).
