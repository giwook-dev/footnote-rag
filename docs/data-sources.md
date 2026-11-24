# 데이터 출처

## 대상 공시

삼성전자(005930), NAVER(035420), 현대자동차(005380)의 **2025 사업연도 감사보고서**(`pblntf_detail_ty=F001`).

| 회사 | report_nm | rcept_no | rcept_dt | DART 뷰어 |
| --- | --- | --- | --- | --- |
| | | | | |

**표가 비어 있는 이유: 아직 실제로 조회하지 못했습니다.** 접수번호는 지어내지 않습니다.

- 조회 시도: 2026-11-24
- 실패 사유: 작업 환경의 `.env`에서 `OPENDART_API_KEY`가 인식되지 않았습니다(비어 있거나 없음). 실제 모드로 실행하면 `MissingKeyError: OPENDART_API_KEY is required when the provider is 'real'`로 중단됩니다. 키 값 자체는 확인·출력하지 않았습니다.
- 재현 명령(키를 `.env`에 넣은 뒤):

```bash
python -m footnote_rag.cli dart-find \
  --stock-codes 005930,035420,005380 --from 20260101 --to 20260531 --detail F001
# 연결감사보고서가 필요하면 --detail F002
python -m footnote_rag.cli dart-fetch --rcept-no <위에서 찾은 rcept_no>   # 원문 ZIP 캐시 + 목차 확인
```

조회되면 위 표에 `dart-find` 출력의 회사명, report_nm, rcept_no, rcept_dt를 그대로 옮기고, 링크는 `https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}` 형식으로 적습니다.

## 수집 방법

1. `corpCode.xml`(ZIP)을 받아 종목코드 → 고유번호(corp_code)를 찾습니다. `data/dart/corp_codes.json`에 7일 캐시합니다(상장사만 저장).
2. `list.json`에 `corp_code`, 기간, `pblntf_ty=F`, `pblntf_detail_ty=F001`, `last_reprt_at=Y`(최종 보고서만)를 넣어 접수번호를 찾습니다. 여러 페이지면 모두 읽습니다. `013`(데이터 없음)은 빈 목록으로 처리합니다.
3. `document.xml?rcept_no=`로 원문 ZIP을 받아 `data/dart/{rcept_no}.zip`에 저장하고, 있으면 재사용합니다. ZIP 대신 JSON/XML 오류 본문이 오면 `status` 코드로 분류합니다.
4. 원문 XML을 목차 경로별 섹션으로 파싱하고(→ [ADR 0004](adr/0004-opendart-xml-over-pdf.md)), 청킹·임베딩 후 pgvector에 저장합니다.

요청 URL은 로그에 남기되 `crtfc_key`는 `***`로 마스킹합니다. httpx 자체 요청 로그는 꺼 두었습니다.

## 파서가 가정한 원문 구조 (실제 확인 필요)

실제 ZIP을 받지 못해 아래 구조는 **OpenDART 원문 XML의 알려진 형식에 따른 가정**입니다. 합성 픽스처(`tests/fixtures/synthetic_audit_report.xml`, `dart/fake_data.py`)로만 검증했습니다.

| 요소 | 처리 |
| --- | --- |
| `DOCUMENT-NAME` | 보고서명(문서 제목 일부) |
| `COMPANY-NAME`(`AREGCIK` 속성) | 회사명, corp_code |
| `BODY` | 본문 시작점 |
| `COVER` / `COVER-TITLE` | 표지 섹션(`감사보고서`) |
| `SECTION-1/2/3…` + `TITLE` | 목차 경로. 자간이 벌어진 제목(`재 무 상 태 표`)은 붙여 씀 |
| `LIBRARY` 등 제목 없는 컨테이너 | 투명하게 통과 |
| `P`, `SPAN` 등 | 본문 텍스트 |
| `TABLE` > `TR` > `TH/TD/TE/TU` | 행 단위 `셀 | 셀` |
| 주석 섹션의 `12. 리스` 같은 짧은 번호 문단 | 하위 목차로 승격 |

실제 원문을 받으면 `dart-fetch` 출력으로 목차를 확인하고, 다르면 실제 구조에 맞춰 파서와 이 표를 고쳐야 합니다.

## 원문을 커밋하지 않는 이유

- 공시 원문은 회사와 금융감독원 DART의 자료이고, 이 저장소의 목적은 수집·검색·인용 **파이프라인**을 보여 주는 것입니다. 원문 재배포는 필요하지 않습니다.
- 원문 ZIP은 수 MB이고 언제든 접수번호로 다시 받을 수 있어 저장소에 둘 이유가 없습니다.
- 그래서 `data/`(ZIP, corpCode 캐시, 업로드 파일)는 `.gitignore`와 `.dockerignore`에 들어 있고, 테스트는 테스트 코드가 직접 만든 소형 **합성** XML/ZIP만 씁니다.
- Fake 모드에서 받은 합성 데이터는 `data/dart/fake/`에 따로 캐시되어 실제 다운로드와 섞이지 않습니다.

## Eval 데이터셋 상태

`evals/dataset.jsonl`에는 형식 예시 행(`"_example": true`) 3개만 있습니다. 실제 보고서를 받지 못해 질문과 근거 인용을 만들 수 없었기 때문입니다(문서 내용을 추측해 만들지 않음). 실제 보고서를 받은 뒤 할 일:

1. `dart-fetch`로 세 보고서를 받아 목차와 텍스트를 확인합니다.
2. 회사당 4개 이상, 답할 수 없는 질문 3개 이상, 두 회사 이상 비교 질문 2개 이상, 총 15개 이상을 작성합니다. `evidence_quote`는 파서가 뽑은 텍스트에서 그대로 복사합니다.
3. `python -m footnote_rag.evals.validate`로 인용문 존재와 개수 규칙을 검사합니다.
4. 키가 있으면 `python -m footnote_rag.evals.run --live`로 리포트를 만들고 비용을 리포트에 남깁니다.
