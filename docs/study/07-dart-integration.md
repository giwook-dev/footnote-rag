# 07. OpenDART 연동과 XML 파서

> 예상 소요: 45분 · 이전: [06. Claude Citations](06-claude-citations.md) · 다음: [08. 검토·감사·사용량](08-review-audit-usage.md)

## 이 문서에서 배울 것

- OpenDART의 세 API(corpCode, list, document)가 각각 무엇을 주고 이 저장소 어디서 쓰이는지 안다.
- OpenDART 오류 코드와 HTTP 오류를 재시도 가능/불가로 나누는 규칙을 말할 수 있다.
- 캐시 전략(corpCode 7일, 원문 ZIP 영구, Fake 캐시 분리)과 `crtfc_key` 마스킹을 설명할 수 있다.
- XML → `section_path` 파싱 규칙을 안다. 그리고 **이 파서는 실제 데이터로 확인되지 않은 가정**이라는 점과 확인 방법을 정확히 말할 수 있다.

## 개념

- **OpenDART**: 금융감독원 전자공시시스템(DART)의 공식 Open API. 인증키(`crtfc_key` 쿼리 파라미터)가 필요하다.
- **[corp_code](glossary.md#corp_code)**: DART 고유번호(8자리). 종목코드(6자리, 상장사만 있음)와 다르다. 공시 목록 API는 `corp_code`로 조회한다.
- **[rcept_no](glossary.md#rcept_no)**: 접수번호(14자리). 공시 문서 하나를 가리킨다. 앞 8자리는 접수일이다(이 저장소는 `rcept_dt = rcept_no[:8]`로 쓴다).
- **[section_path](glossary.md#section_path)**: 이 저장소가 만든 목차 경로. 예: `(첨부)재무제표 > 주석 > 12. 리스`. 각주 위치를 페이지 번호 대신 이 경로로 보여 준다([ADR 0004](../adr/0004-opendart-xml-over-pdf.md)).

## 이 저장소의 구현

### 세 API

| API | 용도 | 코드 | 결과 |
| --- | --- | --- | --- |
| `corpCode.xml` | 전체 회사 목록 ZIP | `src/footnote_rag/dart/client.py::HttpDartClient.download_corp_codes` → `src/footnote_rag/dart/corp_codes.py::parse_corp_code_zip` | 상장사(종목코드 있음)만 `CorpInfo`로 |
| `list.json` | 공시 목록 검색 | `src/footnote_rag/dart/client.py::HttpDartClient.list_reports` | `pblntf_ty=F`, `pblntf_detail_ty=F001`(감사보고서)/`F002`(연결감사보고서), `last_reprt_at=Y`, 100건씩 모든 페이지 |
| `document.xml` | 공시 원문 ZIP | `src/footnote_rag/dart/client.py::HttpDartClient.download_document` | ZIP 바이트. ZIP이 아니면 오류 본문으로 해석 |

모든 호출은 `src/footnote_rag/dart/client.py::HttpDartClient._get`을 지난다. 여기서 `crtfc_key`를 쿼리에 붙이고, 로그에는 마스킹한 URL만 남기며, HTTP·네트워크 오류를 분류한다.

### 오류 분류

출처: `src/footnote_rag/dart/client.py::classify_status`

```python
RETRYABLE_CODES = frozenset({"020", "800", "900"})
NON_RETRYABLE_CODES = frozenset({"010", "011", "012", "013", "014", "901"})


def classify_status(code: str, message: str = "") -> DartError:
    """Map an OpenDART ``status`` value to a DartError with the right retry policy."""
    if code in RETRYABLE_CODES:
        return DartError(code, message or "temporary OpenDART failure", retryable=True)
    if code in NON_RETRYABLE_CODES:
        return DartError(code, message or "OpenDART request rejected", retryable=False)
    # Unknown codes: be conservative and do not hammer the API.
    return DartError(code, message or "unknown OpenDART status", retryable=False)
```

| 실패 | 분류 | 위치 |
| --- | --- | --- |
| 020(요청 한도), 800(점검), 900(정의되지 않은 오류) | 재시도 | `classify_status` |
| 010/011/012/901(키·권한·IP), 013(데이터 없음), 014(파일 없음) | 즉시 실패 | `classify_status` |
| 알 수 없는 코드 | 즉시 실패(보수적) | `classify_status` |
| HTTP 5xx, 429 | 재시도 | `_get` |
| HTTP 4xx(429 제외) | 즉시 실패 | `_get` |
| 네트워크 오류(`httpx.TransportError`) | 재시도 | `_get` |
| ZIP 엔드포인트가 JSON/XML 오류 본문을 줌 | 그 `status` 코드로 분류 | `src/footnote_rag/dart/client.py::parse_error_body` |
| ZIP도 아니고 해석 가능한 오류 본문도 아님 | 재시도(`invalid_response`) | `parse_error_body` |
| 파싱 실패(텍스트 없음, ZIP 아님, XML 없음) | 즉시 실패 | `src/footnote_rag/jobs/handlers.py::handle_import_dart`가 `DartParseError`를 영구 실패로 |

`list.json`의 `013`(데이터 없음)은 오류가 아니라 **빈 목록**으로 처리한다(`list_reports`). 원문 다운로드에서의 013/014는 즉시 실패다.

테스트: `tests/test_dart_client.py::test_retryable_status_codes`, `tests/test_dart_client.py::test_non_retryable_status_codes`, `tests/test_dart_client.py::test_http_and_network_failures_are_classified`, `tests/test_dart_client.py::test_zip_endpoint_returning_error_document`, `tests/test_dart_client.py::test_list_reports_paginates_and_handles_no_data`.

### 캐시 전략

- **corpCode**: `src/footnote_rag/dart/corp_codes.py::CorpCodeCache`가 `data/dart/corp_codes.json`에 상장사 목록을 저장하고, 파일 수정 시각이 `DART_CORP_CODE_TTL_DAYS`(기본 7일) 안이면 다시 받지 않는다.
- **원문 ZIP**: `src/footnote_rag/jobs/handlers.py::load_dart_zip`이 `data/dart/{rcept_no}.zip`이 있으면 재사용한다. 저장은 임시 파일(`.{pid}.part`)에 쓴 뒤 `os.replace`로 바꾸는 원자적 쓰기다(`_atomic_write`). 이 캐시에는 TTL이 없어서, 한 번 받은 원문은 파일을 지우기 전까지 재사용한다.
- **Fake 캐시 분리**: Fake 모드는 `data/dart/fake/`를 쓴다(`src/footnote_rag/jobs/handlers.py::dart_cache_dir`). 처음에는 Fake 합성 목록이 실제 캐시 파일에 들어가 이후 실제 조회가 7일간 합성 데이터를 쓰는 문제가 있었고, 이를 고친 것이다([docs/ai-usage.md](../ai-usage.md)).
- `data/`는 `.gitignore`와 `.dockerignore`에 들어 있어 원문이 저장소나 이미지에 들어가지 않는다.

### `crtfc_key` 마스킹

- `src/footnote_rag/observability.py::mask_url`이 정규식으로 `crtfc_key=` 뒤 값을 `***`로 바꾼다.
- `_get`은 요청 URL을 만든 뒤 마스킹한 URL만 로그에 넘긴다.
- httpx는 INFO 레벨에서 요청 URL 전체를 로그로 남기므로 `configure_logging`이 `httpx`, `httpcore` 등의 로거를 WARNING으로 올리고, 표준 로그 핸들러에도 마스킹 필터(`_StdlibMaskFilter`)를 붙인다.
- 테스트: `tests/test_dart_client.py::test_logged_request_url_masks_crtfc_key`, `tests/test_dart_client.py::test_mask_url`.

### XML → section_path 파싱 규칙

진입점은 `src/footnote_rag/dart/parser.py::parse_document_zip`이다.

1. **본문 XML 고르기** (`src/footnote_rag/dart/parser.py::pick_main_member`): `{rcept_no}.xml`이 있으면 그것, 없으면 가장 큰 XML. 첨부 XML(예: 외부감사 실시내용)은 포함하지 않는다(`tests/test_dart_parser.py::test_fake_zip_excludes_attachment`).
2. **인코딩** (`_decode_xml`): XML 선언에 `encoding=`이 있으면 그것을 따르고(EUC-KR 등), 없으면 UTF-8, 실패하면 CP949로 읽는다. `lxml`을 `recover=True`, `resolve_entities=False`, `no_network=True`로 쓴다.
3. **메타데이터**: `DOCUMENT-NAME`(보고서명), `COMPANY-NAME`(회사명)과 그 `AREGCIK` 속성(corp_code).
4. **목차 경로** (`_walk`): `SECTION-1/2/3…` 안의 `TITLE`이 경로 한 단계가 된다. 자간이 벌어진 제목(`재 무 상 태 표`)은 붙인다(`src/footnote_rag/dart/parser.py::normalize_title`). `COVER`는 `COVER-TITLE`(없으면 "표지")을 경로로 쓴다.
5. **투명 컨테이너**: `LIBRARY`처럼 제목 없이 하위 `SECTION-n`을 품은 태그는 경로를 늘리지 않고 통과한다.
6. **표**: `TABLE > TR > TH/TD/TE/TU`를 행마다 `셀 | 셀 | 셀` 한 줄로 바꾼다(`_table_text`). 빈 행은 버린다.
7. **주석 번호 제목 승격**: 제목에 "주석"이 들어간 섹션 안에서 `12. 리스`처럼 짧은 번호 문단은 하위 목차가 된다(`src/footnote_rag/dart/parser.py::is_note_heading`). `(1)`, `1)` 하위 항목, `1.5` 같은 숫자, `…다.`/`…음`/`…함`으로 끝나는 문장은 제목으로 보지 않는다.
8. **텍스트 없음**: 추출된 텍스트가 하나도 없으면 `DartParseError`(재시도 불가).

### 중요: 이 파서는 가정이다

**실제 OpenDART 원문을 아직 한 건도 받지 못했다.** 작업 환경에서 `OPENDART_API_KEY`가 인식되지 않아 실제 모드가 `MissingKeyError`로 중단됐다([docs/data-sources.md](../data-sources.md)). 따라서 위 규칙은 OpenDART 원문 XML의 알려진 형식을 기준으로 작성한 **가정**이고, 다음 두 합성 데이터로만 검증했다.

- `tests/fixtures/synthetic_audit_report.xml`: 중첩 목차, `LIBRARY`, 표, 주석 번호 문단, `SECTION-3`을 담은 손으로 만든 XML.
- `src/footnote_rag/dart/fake_data.py::build_document_xml`: Fake 모드용 합성 감사보고서. 회사명에 `(합성)`, corp_code는 `99`로 시작한다.

**실제 데이터로 확인하는 방법** (키를 `.env`에 넣은 뒤):

```bash
python -m footnote_rag.cli dart-find --stock-codes 005930,035420,005380 \
  --from 20260101 --to 20260531 --detail F001
python -m footnote_rag.cli dart-fetch --rcept-no <위에서 찾은 rcept_no>
```

`src/footnote_rag/cli.py::cmd_dart_fetch`는 원문 ZIP을 `data/dart/`에 캐시하고 섹션마다 `[글자 수] 목차 경로`를 출력한다. 확인할 것:
- 경로가 `(첨부)재무제표 > 주석 > 12. 리스`처럼 의미 있게 나오는가, 아니면 "본문" 하나로 뭉치는가.
- 주석 제목이 `TITLE`이나 표 셀로 되어 있어 승격 규칙이 놓치지 않는가(ADR 0004 "한계").
- 첨부 XML에 필요한 내용이 있지 않은가.
- 다르면 파서와 [docs/data-sources.md](../data-sources.md)의 표를 실제 구조에 맞춰 고친다.

## 왜 이렇게 했나 / 대안 / 한계와 개선 방향

**왜 XML과 목차 경로인가** ([ADR 0004](../adr/0004-opendart-xml-over-pdf.md))
- 공식 API라 약관·안정성 문제가 없고 오류 코드가 정의돼 있어 재시도 정책을 명확히 만들 수 있다.
- XML에 목차 구조가 있어 PDF 텍스트 추출(레이아웃 깨짐, 표 붕괴, OCR)보다 근거 위치를 안정적으로 표시할 수 있다.
- 실무자에게는 "몇 페이지"보다 "주석 12. 리스"가 찾기 쉽다. 각주에는 DART 뷰어 링크도 함께 준다.

**대안**: DART 뷰어 PDF 파싱(공식 API 없음, 스크래핑 필요), 뷰어 HTML 스크래핑(비공식, 구조 변경에 취약), 재무제표 수치 API만 사용(주석 서술이 없음). 모두 ADR 0004에서 기각했다.

**한계와 개선 방향**
- 파서 구조가 가정이다(위 참조). 실제 원문 하나로 검증하는 것이 가장 먼저 할 일이다.
- 주석 번호 제목 인식은 휴리스틱이다.
- 이미지·차트 안의 정보는 추출하지 않는다(`IMG` 등은 건너뜀).
- OpenDART 일일 요청 한도를 관리하는 클라이언트 측 레이트 리미터는 없다. 020을 받으면 작업 큐의 백오프에 맡긴다.
- `HttpDartClient`는 요청마다 `httpx.AsyncClient`를 새로 만든다. 요청 수가 많아지면 연결 재사용이 이득일 수 있다(측정하지 않음).

## 확인 질문

1. 종목코드 `005930`으로 감사보고서 목록을 얻기까지 어떤 API를 어떤 순서로 부르나?
   <details><summary>답</summary>

   `corpCode.xml`로 회사 목록을 받아(또는 7일 캐시) 종목코드 → `corp_code`를 찾고, `list.json`에 `corp_code`, 기간, `pblntf_ty=F`, `pblntf_detail_ty=F001`을 넣어 접수번호 목록을 받는다. `src/footnote_rag/cli.py::cmd_dart_find`가 이 순서다.
   </details>

2. 원문 다운로드에서 014가 오면 작업과 문서는 어떻게 되나? 020이면?
   <details><summary>답</summary>

   014는 재시도 불가라 작업과 문서가 바로 `failed`가 된다(`tests/test_jobs.py::test_non_retryable_dart_error_fails_job_and_document_immediately`). 020은 재시도 가능이라 작업은 백오프 후 `queued`, 문서도 `queued`로 돌아간다(`tests/test_jobs.py::test_retryable_dart_error_requeues_document`).
   </details>

3. 이 파서가 실제 삼성전자 감사보고서에서 잘 동작한다고 말할 수 있나?
   <details><summary>답</summary>

   말할 수 없다. 실제 원문을 받지 못했고 합성 픽스처로만 검증했다. `cli dart-fetch`로 실제 원문의 목차를 출력해 확인해야 한다.
   </details>

4. `crtfc_key`가 로그에 남지 않게 하는 장치 세 가지는?
   <details><summary>답</summary>

   (1) `_get`이 마스킹한 URL만 로그에 넘긴다. (2) httpx·httpcore 로거를 WARNING으로 올려 요청 URL 로그를 끈다. (3) structlog 프로세서와 표준 로그 필터가 `crtfc_key=` 값을 `***`로 바꾼다.
   </details>
