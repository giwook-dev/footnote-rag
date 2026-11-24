# ADR 0004: 원문은 DART PDF 대신 OpenDART XML, 각주 위치는 페이지 대신 목차 경로

- 상태: 채택
- 날짜: 2026-11-24

## 맥락

실제 공시 감사보고서를 자동으로 가져와야 합니다. DART 웹 뷰어의 PDF는 공식 API로 받을 수 없고 화면 스크래핑이 필요합니다. 반면 OpenDART는 인증키로 원문을 XML(ZIP)로 내려 줍니다(`/api/document.xml`).

## 결정

- OpenDART API만 씁니다: `corpCode.xml`(종목코드 → 고유번호, 7일 캐시), `list.json`(`pblntf_ty=F`, `pblntf_detail_ty=F001` 감사보고서 / `F002` 연결감사보고서, `last_reprt_at=Y`), `document.xml`(원문 ZIP, `data/dart/{rcept_no}.zip`에 캐시).
- 원문 XML의 `SECTION-n` + `TITLE` 구조로 **목차 경로**(`section_path`)를 만들고, 각주 위치를 페이지 번호 대신 목차 경로로 표시합니다. 예: `(첨부)재무제표 > 주석 > 12. 리스`.
- 파서(`dart/parser.py`)는 순수 함수이며 `lxml`(`recover=True`)로 파싱하고 XML 선언의 인코딩(UTF-8/EUC-KR)을 따릅니다. 선언이 없으면 UTF-8, 실패하면 CP949로 읽습니다.
- 표(`TABLE/TR/TH/TD/TE/TU`)는 행마다 `셀 | 셀 | 셀` 한 줄로 바꿉니다. 청킹이 행 경계에서 자르므로 표 행이 쪼개지지 않습니다.
- "주석" 섹션 안의 짧은 번호 문단(`12. 리스`)은 하위 목차로 승격합니다. `(1)`, `1)` 같은 하위 항목과 `…다.`로 끝나는 문장은 제목으로 보지 않습니다.
- ZIP의 본문 XML은 `{rcept_no}.xml`, 없으면 가장 큰 XML입니다. **첨부 XML(예: 외부감사 실시내용)은 포함하지 않습니다.** 감사의견·재무제표·주석이 본문에 있고, 첨부는 감사 시간·인원 같은 행정 정보라 질의응답 근거로서 가치가 낮고 검색 잡음이 되기 때문입니다.
- 텍스트가 하나도 없으면 `DartParseError`(재시도 불가)입니다.

## 근거

- 공식 API라 약관·안정성 문제가 없고, 오류 코드가 정의돼 있어 재시도 정책을 명확히 만들 수 있습니다(000 정상, 020/800/900 재시도, 010/011/012/013/014/901 재시도 불가).
- XML은 목차 구조를 그대로 담고 있어, PDF 텍스트 추출(레이아웃 깨짐, 표 붕괴, OCR)보다 근거 위치를 안정적으로 표시할 수 있습니다.
- 감사 실무자에게는 "몇 페이지"보다 "주석 12. 리스"가 더 찾기 쉽습니다. 각주에는 DART 뷰어 링크(`https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}`)를 함께 줍니다.

## 대안

| 대안 | 기각 이유 |
| --- | --- |
| DART 뷰어 PDF 다운로드 + PDF 파싱 | 공식 API 없음(스크래핑), 표·레이아웃 추출 품질 문제, 범위 밖(PDF·OCR) |
| DART 뷰어 HTML 스크래핑 | 비공식, 구조 변경에 취약 |
| 재무제표 수치 API(`fnlttSinglAcnt` 등)만 사용 | 숫자만 있고 주석 서술(회계정책, 우발부채 등)이 없음 |

## 한계 (중요)

- **실제 원문 구조 확인을 하지 못했습니다.** 계획은 실제 ZIP 하나를 받아 태그 구조를 확인하고 파서를 맞추도록 했지만, 이번 작업 환경에서 `OPENDART_API_KEY`가 설정되어 있지 않아 다운로드하지 못했습니다(`docs/data-sources.md`). 현재 파서는 OpenDART 원문 XML의 알려진 구조(`DOCUMENT-NAME`, `COMPANY-NAME AREGCIK`, `BODY`, `COVER`, `SECTION-n`, `TITLE`, `LIBRARY`, `P`, `TABLE`)를 기준으로 작성했고 합성 픽스처로만 검증했습니다. 실제 키로 `python -m footnote_rag.cli dart-fetch --rcept-no …`를 실행해 출력 목차를 보고 파서를 맞춰야 합니다.
- 주석 번호 제목 인식은 휴리스틱입니다. 실제 보고서에서 주석 제목이 `TITLE`이나 표 셀로 표현되면 규칙을 추가해야 합니다.
