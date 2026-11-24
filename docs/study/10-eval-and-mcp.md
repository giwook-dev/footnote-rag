# 10. Eval과 MCP

> 예상 소요: 30분 · 이전: [09. 테스트와 운영](09-testing-and-ops.md) · 다음: [면접 Q&A](interview-qa.md)

## 이 문서에서 배울 것

- Eval 지표 세 가지(검색 recall@k, 인용 정확도, 답하지 말아야 할 질문의 거절 비율)의 정의와 계산 위치를 안다.
- `evals.validate`가 왜 필요한지(근거 인용문이 파서 출력에 실제로 있는지 확인)를 설명할 수 있다.
- **현재 데이터셋에는 예시 행만 있어 실제 측정 결과가 없다**는 점을 정확히 말할 수 있다.
- MCP 서버가 DB 대신 HTTP API를 호출해 권한과 조직 격리를 유지하는 구조를 설명할 수 있다.

## 개념

- **Eval**: 미리 정답(또는 판단 기준)을 붙인 질문 묶음으로 시스템을 돌려 품질을 수치로 보는 것. 프롬프트·청킹·임계값을 바꿨을 때 좋아졌는지 나빠졌는지 판단하는 근거가 된다.
- **[recall@k](glossary.md#recallk)**: 정답 근거가 상위 k개 검색 결과 안에 들어 있는 질문의 비율. 검색 단계만 따로 평가한다.
- **인용 정확도**: 답변 각주 중 기대한 목차 아래를 가리키는 각주의 비율.
- **거절(abstention)**: 문서에 근거가 없는 질문에 답하지 않는 것. RAG에서는 "모르면 모른다고 하기"가 중요하다.
- **[MCP](glossary.md#mcp)**(Model Context Protocol): AI 클라이언트(예: Claude Code)가 외부 도구를 표준 방식으로 부르게 하는 프로토콜. 이 저장소의 MCP 서버는 stdio로 통신한다.

## 이 저장소의 구현

### 데이터셋 형식

`src/footnote_rag/evals/dataset.py::load_dataset`이 `evals/dataset.jsonl`을 읽는다. 한 줄에 질문 하나이며 주요 필드는 `id`, `rcept_no`(하나 또는 목록), `corp_name`, `question`, `expected_section_prefixes`, `answerable`, `evidence_quote`다. `corp_name`이 두 개 이상이면 비교 질문이다(`EvalItem.is_comparison`). `"_example": true` 행은 실행기와 검증기가 건너뛴다.

**현재 상태: `evals/dataset.jsonl`에는 `_example` 행 3개만 있다.** 실제 보고서를 받지 못해 질문과 근거 인용을 만들 수 없었기 때문이다(문서 내용을 추측해 만들지 않음). 해야 할 일은 [docs/data-sources.md "Eval 데이터셋 상태"](../data-sources.md#eval-데이터셋-상태)에 있다.

### 검증기: 근거가 진짜 있는가

`src/footnote_rag/evals/validate.py::validate`는 답할 수 있는 실제 행마다 다음을 확인한다.
1. `evidence_quote`가 캐시된 원문 ZIP(`data/dart/{rcept_no}.zip`)을 **이 저장소의 파서로 읽은 텍스트**에 공백 정규화 후 그대로 있는가.
2. 그 인용문이 `expected_section_prefixes` 중 하나로 시작하는 목차 아래에서 발견되는가.
3. 데이터셋 전체가 개수 규칙을 지키는가(`_check_coverage`): 15행 이상, 삼성전자·NAVER·현대자동차 각 4행 이상, 답할 수 없는 질문 3개 이상, 비교 질문 2개 이상.

왜 필요한가: 사람이 원문을 보고 인용문을 옮기다 보면 띄어쓰기·표 구분자 차이로 파서 출력과 달라질 수 있다. 그러면 recall@k와 인용 정확도가 실제보다 낮게 나온다. 검증기는 데이터셋이 **파서가 실제로 만든 텍스트와 목차**에 맞는지 먼저 보장한다. 예시 행만 있으면 "검사할 실제 행 없음"을 출력하고 성공한다(`tests/test_evals_cli_mcp.py::test_example_only_dataset_validates_trivially`).

### 실행기: 정상 경로를 그대로 쓴다

`src/footnote_rag/evals/run.py::run_eval`의 순서:
1. 임시 조직과 관리자를 만든다(`seed_org`).
2. 데이터셋에 나온 `rcept_no`마다 `request_dart_import`로 가져오기를 등록하고, `Worker.run_once`를 반복해 큐를 비운다. **API·워커와 같은 서비스 함수**를 쓰므로 Eval이 따로 노는 코드가 아니다.
3. 질문마다 `retrieve`로 검색 결과를, `ask_question`으로 답변을 얻는다.
4. 결과를 `ItemResult`로 모아 Markdown 리포트(`evals/reports/{timestamp}-{mode}[-{provider}]-{model}.md`)를 쓴다. 머리말과 파일명에 LLM 제공자·모델·`citation_method`가 들어가 Claude(`api_citation`)와 로컬 Ollama(`model_reference`) 결과를 비교할 수 있다(`src/footnote_rag/evals/run.py::report_filename`).

지표 계산 위치(`src/footnote_rag/evals/run.py::EvalSummary`):

| 지표 | 코드 | 정의 |
| --- | --- | --- |
| recall@k | `src/footnote_rag/evals/run.py::EvalSummary.recall_at_k` | 답할 수 있는 질문 중, 상위 `TOP_K` 검색 결과에 `expected_section_prefixes`로 시작하는 청크가 하나라도 있는 비율 |
| 인용 정확도 | `src/footnote_rag/evals/run.py::EvalSummary.citation_accuracy` | 답할 수 있는 질문들의 전체 각주 중 `section_path`가 기대 접두어로 시작하는 각주의 비율 |
| 거절 비율 | `src/footnote_rag/evals/run.py::EvalSummary.abstention_rate` | 답할 수 없는 질문 중 "거절"로 본 비율. 거절 = 각주 없음 **또는** `pending_review` **또는** 답에 "근거를 찾지 못했습니다" 문구 포함 |
| 평균 비용·지연 | `EvalSummary.avg_cost_usd`, `EvalSummary.avg_latency_ms` | 질문당 평균. 비용은 가격표에 있는 모델만 |

기본은 Fake 모드(비용 없음)이며 리포트에 "Fake mode … not answer quality"라는 경고가 붙는다. `--live`는 설정된 실제 제공자를 쓰고 비용이 발생한다. 테스트: `tests/test_evals_cli_mcp.py::test_eval_runner_fake_mode_writes_markdown_report`.

지표의 해석 주의:
- 판정이 **목차 접두어 일치**라서, 다른 목차에도 같은 내용이 있으면 정답인데도 오답으로 셀 수 있다.
- 거절 판정이 넓다(`pending_review`면 거절로 셈). 답할 수 없는 질문에 모델이 답했어도 검토 대기로 갔다면 거절로 계산된다.
- 답변 문장의 사실 정확성(숫자가 맞는지 등)은 재지 않는다. LLM 채점기도 없다.

### MCP 서버

출처: `src/footnote_rag/mcp_server.py::_api_client`, `src/footnote_rag/mcp_server.py::ask` (`_post`가 `_api_client`로 요청을 보낸다)

```python
def _api_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    base_url = os.environ.get("FOOTNOTE_API_URL", "http://localhost:8000").rstrip("/")
    token = os.environ.get("FOOTNOTE_API_TOKEN", "")
    if not token:
        raise ApiError("FOOTNOTE_API_TOKEN is not set (get one from POST /auth/token)")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        ...
    )


@mcp.tool()
async def ask(question: str) -> dict[str, Any]:
    """Ask a question; the answer cites its evidence with numbered footnotes [n]. ..."""
    return await _post("/questions", {"question": question})
```

- 도구는 둘이다. `search_documents(query, k)` → `POST /search`, `ask(question)` → `POST /questions`.
- MCP 서버는 DB 연결도, 서비스 import도 없다. 사용자 JWT를 `Authorization` 헤더에 실어 HTTP API를 부르므로 **API와 똑같이** `require_role`, 조직 격리, member의 검토 대기 답변 숨김, 감사 로그가 적용된다.
- API 오류(예: 403)는 `ApiError("API 403: …")`로 MCP 클라이언트에 전달된다.
- `mcp` 패키지 2.x에서 `FastMCP`가 `MCPServer`로 이름이 바뀌어 두 버전을 모두 지원하도록 import를 분기한다.
- 테스트: `tests/test_evals_cli_mcp.py::test_mcp_tools_call_http_api_with_token`, `tests/test_evals_cli_mcp.py::test_mcp_requires_token`.
- 연결 방법(`claude mcp add …`, `.mcp.json`)은 [README](../../README.md)의 "MCP 서버" 절에 있다.

## 왜 이렇게 했나 / 대안 / 한계와 개선 방향

**왜**
- Eval이 정상 서비스 경로를 그대로 쓰면, Eval 점수가 실제 API 동작을 반영한다.
- 근거 인용문을 사람이 쓰되 **검증기로 파서 출력과 대조**하게 해서, 데이터셋 오류와 시스템 오류를 구분할 수 있게 했다.
- MCP 서버를 HTTP 클라이언트로 만들면 권한 검사를 두 번 구현하지 않아도 된다. 대가로 API 서버가 떠 있어야 한다.

**대안**
- LLM을 채점기로 쓰는 평가(LLM-as-judge): 답변의 사실성·완결성까지 볼 수 있지만 채점 비용과 채점기 자체의 검증이 필요하다. 이 저장소에는 없다.
- MCP 서버가 서비스 함수를 직접 호출: 지연이 줄지만 인증·격리를 MCP 쪽에 다시 구현해야 한다.

**한계와 개선 방향**
- **실제 측정 결과가 없다.** 데이터셋에 예시 행만 있고, 실제 보고서를 받지 못했다.
- Eval은 CI에서 돌지 않는다. 회귀를 자동으로 잡으려면 실제 데이터셋을 만든 뒤 Fake/라이브 중 무엇을 CI에 넣을지 정해야 한다.
- MCP 토큰은 만료(기본 60분)되면 다시 발급해야 한다. 자동 갱신이 없다.
- MCP 도구는 검색과 질문 둘뿐이다. 가져오기·검토 도구는 없다.

## 확인 질문

1. recall@k는 높은데 인용 정확도가 낮다면 어디를 의심하나?
   <details><summary>답</summary>

   검색은 맞는 청크를 찾았지만 모델이 다른 청크를 인용했거나, 각주가 기대 목차 밖을 가리킨다는 뜻이다. 프롬프트, 검색 결과 순서·개수(`TOP_K`), 청크 크기, 또는 기대 접두어가 너무 좁게 잡힌 데이터셋을 본다.
   </details>

2. `evals.validate`가 실패했다. Eval을 돌리기 전에 무엇을 해야 하나?
   <details><summary>답</summary>

   오류 메시지에 따라 원문 ZIP을 `cli dart-fetch`로 받거나, 인용문을 파서 출력에서 다시 복사하거나, `expected_section_prefixes`를 실제 목차에 맞게 고친다. 데이터셋이 파서 출력과 맞지 않으면 Eval 숫자를 믿을 수 없다.
   </details>

3. 지금 저장소의 recall@k는 얼마인가?
   <details><summary>답</summary>

   측정된 값이 없다. 데이터셋에 `_example` 행만 있고, Fake 모드 테스트는 실행기 동작만 확인한다.
   </details>

4. member 토큰으로 MCP의 `ask`를 불렀는데 답이 검토 대기로 갔다. MCP 클라이언트는 무엇을 받나?
   <details><summary>답</summary>

   `POST /questions`의 member 응답 그대로다. `status: "pending_review"`, 본문 대신 "검토 대기 중" 안내 문구, 빈 각주 목록. MCP 서버는 API 응답을 그대로 전달할 뿐이다.
   </details>
