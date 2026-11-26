"""웹 화면 정적 파일 규칙 검사. 네트워크·DB 불필요.

* index.html: 인라인 ``<script>`` 본문과 ``on*=`` 이벤트 속성이 없다(CSP ``script-src 'self'``와 맞춘다).
* 세 파일 모두 외부 ``http(s)://`` 리소스(``src=``, ``href=``, ``@import``, ``url(``)를 참조하지 않는다.
  JS의 ``https://dart.fss.or.kr/`` 접두어 상수(각주 링크 검사용)만 예외다.
* app.js는 ``innerHTML``/``outerHTML`` 대입이나 ``insertAdjacentHTML``로 HTML 문자열을 넣지 않는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "footnote_rag" / "web" / "static"
FILES = ["index.html", "app.js", "style.css"]
ALLOWED_URLS = {"app.js": {"https://dart.fss.or.kr/"}}


@pytest.fixture(scope="session")
def _database() -> None:
    """Override conftest's autouse DB setup: these checks only read files."""


@pytest.fixture
def _clean_jobs() -> None:
    """Override conftest's autouse job cleanup: no database is needed here."""


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_static_files_exist():
    for name in FILES:
        assert (STATIC / name).is_file(), name


def test_index_has_no_inline_script_or_event_handlers():
    html = _read("index.html")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL)
    assert scripts, "index.html must load app.js"
    for attrs, body in scripts:
        assert body.strip() == "", "inline <script> body is not allowed"
        assert re.search(r"\bsrc\s*=", attrs), "every <script> must use src="
    tags = re.findall(r"<[a-zA-Z][^>]*>", html)
    handlers = [t for t in tags if re.search(r"\son[a-z]+\s*=", t, re.IGNORECASE)]
    assert not handlers, f"inline event handler attributes: {handlers}"
    assert not re.search(r"javascript:", html, re.IGNORECASE)
    assert "style=" not in html, "inline style attributes are blocked by the CSP"


@pytest.mark.parametrize("name", FILES)
def test_no_external_resource_references(name: str):
    text = _read(name)
    external = [
        r"""\b(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""",
        r"""@import\s+(?:url\()?\s*["']?\s*(?:https?:)?//""",
        r"""url\(\s*["']?\s*(?:https?:)?//""",
    ]
    for pattern in external:
        assert not re.search(pattern, text, re.IGNORECASE), f"{name}: external resource {pattern}"
    urls = set(re.findall(r"https?://[^\s\"'`)]*", text))
    assert urls <= ALLOWED_URLS.get(name, set()), f"{name}: unexpected URLs {urls}"


def test_app_js_never_writes_html_strings():
    js = _read("app.js")
    assert not re.search(r"\.(?:innerHTML|outerHTML)\s*\+?=", js)
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js
    assert not re.search(r"\beval\s*\(|new\s+Function\s*\(", js)


def test_app_js_builds_footnote_markers_from_text_and_checks_dart_prefix():
    js = _read("app.js")
    assert r"/\[(\d+)\]/" in js
    assert 'const DART_URL_PREFIX = "https://dart.fss.or.kr/";' in js
    assert '"noopener noreferrer"' in js
    assert "sessionStorage" in js and "localStorage" not in js


def test_report_kind_defaults_to_a001_annual_report():
    html = _read("index.html")
    select = re.search(r'<select id="dart-detail"[^>]*>(.*?)</select>', html, re.DOTALL)
    assert select, "report kind <select> is missing"
    options = re.findall(r"<option\b([^>]*)>(.*?)</option>", select.group(1))
    assert [re.search(r'value="([^"]+)"', a).group(1) for a, _ in options] == [
        "A001",
        "F001",
        "F002",
    ]
    first_attrs, first_label = options[0]
    assert "selected" in first_attrs
    assert first_label == "A001 사업보고서(상장사 — 감사받은 재무제표·주석 포함)"
    assert all("selected" not in a for a, _ in options[1:])


def test_zero_audit_report_results_show_annual_report_hint_via_text_content():
    html = _read("index.html")
    js = _read("app.js")
    assert re.search(r'<p id="dart-hint" class="notice" hidden></p>', html)
    assert '"상장사는 감사보고서를 사업보고서(A001)에 포함해 공시합니다."' in js
    assert 'const AUDIT_REPORT_DETAILS = ["F001", "F002"];' in js
    assert "hint.textContent = ANNUAL_REPORT_HINT;" in js
    assert re.search(r"if \(empty && AUDIT_REPORT_DETAILS\.includes\(detail\)\)", js)


def test_reprocess_button_is_admin_only_for_ready_or_failed_documents():
    js = _read("app.js")
    assert 'const REPROCESSABLE_STATUSES = ["ready", "failed"];' in js
    assert 'const isAdmin = () => state.user !== null && state.user.role === "admin";' in js
    assert "if (!isAdmin() || !REPROCESSABLE_STATUSES.includes(d.status)) return null;" in js
    assert '"다시 처리"' in js
    assert "window.confirm(" in js
    # After the request the list is reloaded, which restarts polling for the queued document.
    body = js[js.index("async function reprocessDocument") :]
    body = body[: body.index("\n  }\n")]
    assert "/reprocess`" in body and 'method: "POST"' in body
    assert body.index("window.confirm(") < body.index("await api(")
    assert "await loadDocuments();" in body


def test_note_diff_tab_markup_follows_the_tab_pattern():
    html = _read("index.html")
    assert re.search(
        r'<button type="button" role="tab" id="tab-diff" data-tab="diff" '
        r'aria-controls="panel-diff" aria-selected="false">변동 분석</button>',
        html,
    )
    assert re.search(
        r'<section id="panel-diff" class="panel" role="tabpanel" aria-labelledby="tab-diff" hidden>',
        html,
    )
    for element_id in (
        "diff-form",
        "diff-current",
        "diff-prior",
        "diff-btn",
        "diff-prior-empty",
        "diff-recent",
        "diff-stats",
        "diff-changed-only",
        "diff-notes",
        "diff-detail",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert re.search(r'<label for="diff-current">당년 보고서</label>', html)
    assert re.search(r'<label for="diff-prior">전년 보고서[^<]*</label>', html)
    assert 'name="diff-scope" value="consolidated" checked' in html
    assert 'name="diff-scope" value="separate"' in html
    assert re.search(r'<input id="diff-changed-only" type="checkbox" checked>', html)
    assert "전년 보고서를 먼저 가져오세요" in html


def _function_body(js: str, signature: str) -> str:
    body = js[js.index(signature) :]
    return body[: body.index("\n  }\n")]


def test_note_diff_prior_report_choice_is_same_corp_and_earlier():
    js = _read("app.js")
    body = _function_body(js, "function priorCandidates(current, docs)")
    assert "d.meta.corp_code === meta.corp_code" in body
    assert 'String(d.meta.rcept_dt || "") < String(meta.rcept_dt)' in body
    assert ".sort(byNewest)" in body and "localeCompare" in js  # newest first
    # split into the same report kind and the other kinds
    assert "same: earlier.filter((d) => sameReportKind(" in body
    assert "other: earlier.filter((d) => !sameReportKind(" in body
    assert (
        'const isComparable = (d) => d.status === "ready" && d.source === "dart" && d.meta;' in js
    )
    # prior pick: same kind exactly 12 months earlier -> newest same kind -> newest other kind
    pick = _function_body(js, "function pickPrior(current, candidates)")
    assert "docMonths(d) === months - 12" in pick
    assert "yearAgo || candidates.same[0] || candidates.other[0] || null" in pick
    # current pick: the newest annual report that has a same-kind prior report comes first
    current = _function_body(js, "function pickCurrent(docs)")
    assert 'withSame.find((d) => reportKind(docReportNm(d)).kind === "annual")' in current
    assert current.index("withSame.find(") < current.index("withSame[0]")
    assert current.index("withSame[0]") < current.index("withAny ||")
    assert "diff: () => loadDiffTab()," in js


def test_note_diff_report_selects_group_and_label_reports():
    js = _read("app.js")
    assert '{ label: "같은 종류", docs: candidates.same }' in js
    assert '{ label: "다른 종류 (권장하지 않음)", docs: candidates.other }' in js
    assert js.index('label: "같은 종류"') < js.index('label: "다른 종류 (권장하지 않음)"')
    assert 'el("optgroup", { attrs: { label: g.label } }' in js
    label = _function_body(js, "function reportLabel(d)")
    assert "rcept_no" not in label  # 접수번호는 option title로만
    assert "접수 ${fmtDate(meta.rcept_dt)}" in label
    option = _function_body(js, "function reportOption(d, selectedId)")
    assert "attrs.title = `접수번호 ${rcept}`" in option
    # report kinds ignore correction marks; periods are YYYY.MM
    assert 'if (base.includes("사업보고서")) return { kind: "annual"' in js
    assert 'if (base.includes("반기보고서")) return { kind: "half"' in js
    assert 'if (base.includes("분기보고서")) return { kind: "quarter"' in js
    assert 'return { kind: "other"' in js
    assert r"const CORRECTION_MARK_RE = /^(?:\s*\[[^\]]*\])+\s*/;" in js


def test_note_diff_pair_warning_before_running():
    html = _read("index.html")
    js = _read("app.js")
    assert re.search(
        r'<div id="diff-pair-warning" class="notice pair-warning" role="status" hidden></div>', html
    )
    warning = _function_body(js, "function pairWarning(priorNm, currentNm)")
    assert '"보고서 종류가 다름"' in warning
    assert "cm - pm !== 12" in warning
    assert (
        'const PAIR_WARNING_TEXT = "같은 종류의 1년 전 보고서가 아니어서 숫자 변동이 크게 부풀 수 '
        '있습니다.";'
    ) in js
    update = _function_body(js, "function updatePairWarning()")
    assert 'button.textContent = warning === null ? "비교" : "그래도 비교";' in update
    assert "button.disabled" not in update  # warn only, never block
    assert '$("diff-prior").addEventListener("change", updatePairWarning);' in js
    assert "updatePairWarning();" in _function_body(js, "function updatePriorOptions()")
    # recent comparisons: corp · kind period → kind period · scope, ⚠ for a mismatched pair
    recent = _function_body(js, "function recentLabel(c)")
    assert "reportShort(c.prior_report_nm)" in recent
    assert "reportShort(c.current_report_nm)" in recent
    assert 'pairWarning(c.prior_report_nm, c.current_report_nm) ? "⚠ " : ""' in recent
    assert '${warn}${c.corp_name || "회사"} · ${prior} → ${current} · ${scope}' in recent


def test_note_diff_stats_cards_chips_search_and_sort():
    html = _read("index.html")
    js = _read("app.js")
    for element_id in ("diff-note-search", "diff-note-sort", "diff-notes-count", "diff-warnings"):
        assert f'id="{element_id}"' in html, element_id
    chips = re.findall(r'data-note-chip="([a-z_]+)" aria-pressed="false">([^<]+)<', html)
    assert chips == [
        ("watchlist", "관심 주석"),
        ("large", "큰 변동"),
        ("mismatch", "전기 불일치"),
        ("text", "문장 변경"),
        ("added_removed", "신규·삭제"),
    ]
    sort = re.search(r'<select id="diff-note-sort">(.*?)</select>', html, re.DOTALL)
    assert sort and re.findall(r'value="([a-z]+)"', sort.group(1)) == [
        "score",
        "large",
        "text",
        "number",
    ]
    # stat cards are aria-pressed buttons; the ambiguous-rows card is not a button
    stats = _function_body(js, "function renderDiffStats()")
    assert 'className: "stat-card"' in stats and '"aria-pressed": "false"' in stats
    for key in ('"changed"', '"added"', '"removed"', '"prior_mismatch"', '"large_change"'):
        assert key in stats, key
    assert 'el("div", { className: "stat-card static", attrs: { title: AMBIGUOUS_ROWS_HELP } }' in (
        stats
    )
    assert (
        'const AMBIGUOUS_ROWS_HELP = "같은 표·행 이름이 한 보고서에 여러 번 나와 짝을 확정할 수 없어 '
        '비교에서 뺀 행";'
    ) in js
    assert "${fmtInt(allNotes().length)}개 중 ${fmtInt(notes.length)}개 표시" in js
    # the kind warning offers a re-run with a same-kind report one year earlier
    assert '"같은 종류의 1년 전 보고서로 다시 비교"' in js
    for name in (
        "filterNotes",
        "sortNotes",
        "filterNumberRows",
        "sortNumberRows",
        "groupNumberRows",
        "fmtDecimal",
        "reportKind",
        "reportPeriod",
        "pairWarning",
    ):
        assert f"function {name}(" in js, name
    assert "state.diffView = freshDiffView();" in _function_body(js, "function showComparison(")


def test_note_diff_detail_has_inner_tabs():
    html = _read("index.html")
    js = _read("app.js")
    assert re.search(r'<div id="diff-subtabs" class="subtabs" role="tablist"', html)
    for name in ("numbers", "lines", "summary"):
        assert re.search(
            rf'<button type="button" role="tab" id="diff-tab-{name}" data-diff-tab="{name}" '
            rf'aria-controls="diff-pane-{name}" aria-selected="(?:true|false)"',
            html,
        ), name
        assert re.search(
            rf'<section id="diff-pane-{name}" class="diff-pane" role="tabpanel" '
            rf'aria-labelledby="diff-tab-{name}"',
            html,
        ), name
    detail = _function_body(js, "function renderNoteDetail()")
    assert "숫자 변동 (${fmtInt(rows.length)})" in detail
    assert "문장 변경 (${fmtInt(textLines(n))})" in detail
    assert "showDetailTab(state.diffView.tab || defaultDetailTab(n));" in detail
    # the summary's line links switch to the sentence tab first
    scroll = _function_body(js, "function scrollToLine(ref)")
    assert scroll.index('selectDetailTab("lines");') < scroll.index("scrollIntoView")


def test_note_diff_number_table_layout_and_formatting():
    js = _read("app.js")
    css = _read("style.css")
    # multi-value cells are never joined into one long line
    assert "prior_values.join(" not in js and "current_values.join(" not in js
    assert "${fmtInt(values.length)}개 값" in js
    assert 'el("details", { className: "vals" }' in js
    # no "표" column: one full-width group header row per table name
    assert 'const NUMBER_HEADERS = ["행", "전년", "당년", "증감", "증감률", "플래그"];' in js
    assert (
        'const MISMATCH_HEADERS = ["행", "전년 보고서의 당기", "당년 보고서의 전기", "차이", '
        '"차이율", "플래그"];'
    ) in js
    assert 'el("tr", { className: "group" }' in js
    assert "colspan: NUMBER_COLUMNS.length" in js
    assert "${r.occurrence + 1}번째" in js
    assert "const NUMBER_ROWS_PAGE = 50;" in js and "MAX_NUMBER_ROWS_SHOWN" not in js
    assert "더 보기 (" in js
    fmt = _function_body(js, "function fmtDecimal(value, { sign = false } = {})")
    assert "Number(" not in fmt and "parseFloat" not in fmt  # string-only formatting
    assert '"−"' in fmt and '"+"' in fmt
    # fixed table layout, column widths and Korean word wrapping
    rule = re.search(r"\.num-table \{([^}]*)\}", css)
    assert rule and "table-layout: fixed" in rule.group(1)
    assert re.search(r"\.num-table col\.c-num \{ width: 7\.5em; \}", css)
    assert re.search(r"\.num-table col\.c-pct \{ width: 6em; \}", css)
    assert re.search(r"\.num-table col\.c-flag \{ width: 8em; \}", css)
    lbl = re.search(r"\.num-table td\.lbl, \.num-table tr\.group th \{([^}]*)\}", css)
    assert lbl and "word-break: keep-all" in lbl.group(1)
    assert "overflow-wrap: anywhere" in lbl.group(1)
    assert re.search(r"\.num-table td\.n\.many \{ white-space: normal; \}", css)
    assert ".num-table .up { color: var(--ok); }" in css
    assert ".num-table .down { color: var(--fail); }" in css
    # global td: keep-all, while the existing anywhere cells keep their rule
    assert re.search(r"^td \{ word-break: keep-all;", css, re.MULTILINE)
    assert "td.error { color: var(--fail); max-width: 320px; overflow-wrap: anywhere; }" in css
    # the note list column stays visible on wide screens
    sticky = re.search(r"@media \(min-width: 901px\) \{\s*\.diff-list-col \{([^}]*)\}", css)
    assert sticky and "position: sticky" in sticky.group(1)
    assert "max-height: calc(100vh" in sticky.group(1) and "overflow-y: auto" in sticky.group(1)


def test_note_diff_new_css_colors_are_existing_variables():
    css = _read("style.css")
    start = css.index("/* ---- note change analysis (변동 분석) ---- */")
    section = css[start : css.index("/* ---- reviews ---- */")]
    defined = set(re.findall(r"(--[a-z-]+):", css[: css.index("* { box-sizing")]))
    used = set(re.findall(r"var\((--[a-z-]+)\)", section))
    assert used <= defined, used - defined
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", section)  # no new raw colors


def test_note_diff_detail_shows_tables_lines_and_local_summary():
    js = _read("app.js")
    css = _read("style.css")
    assert '"요약 생성 (로컬 LLM)"' in js and '"요약 다시 생성 (로컬 LLM)"' in js
    assert 'const SUMMARY_NOTICE = "모델 요약 — 변동 판단은 담당자가 확정";' in js
    assert '"● 숫자 원문 확인됨"' in js
    assert "확인 필요: 원문에 없는 숫자 ${numbers}" in js
    assert "const SUMMARY_ERROR_STATUSES = [501, 502, 503, 504];" in js
    assert "전기 수치 불일치 — 당년 보고서의 전기 값 vs 전년 보고서의 당기 값" in js
    assert '["행", "전년", "당년", "증감", "증감률", "플래그"]' in js
    assert 'el("details", { className: "fold-numbers" }' in js  # numbers-only lines folded
    assert "scrollToLine(ref)" in js and "diff-line-${e.ref}" in js
    assert ".difflines .del { background: var(--fail-bg); }" in css
    assert ".difflines .add { background: var(--ok-bg); }" in css
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 2fr)" in css  # list 1/3, detail 2/3


def test_agent_tab_markup_and_main_elements():
    html = _read("index.html")
    assert re.search(
        r'<button type="button" role="tab" id="tab-agent" data-tab="agent" '
        r'aria-controls="panel-agent" aria-selected="false">에이전트</button>',
        html,
    )
    assert re.search(
        r'<section id="panel-agent" class="panel agent-split" role="tabpanel" '
        r'aria-labelledby="tab-agent" hidden>',
        html,
    )
    for element_id in (
        "agent-form",
        "agent-question",
        "agent-run-btn",
        "agent-stop-btn",
        "agent-model",
        "agent-limits",
        "agent-examples",
        "agent-timeline-card",
        "agent-progress",
        "agent-elapsed",
        "agent-answer-card",
        "agent-status",
        "agent-answer",
        "agent-source",
        "agent-recent",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert re.search(r'<ol id="agent-timeline" class="agent-tl" aria-live="polite"', html)
    assert len(re.findall(r'data-agent-example="[^"]+"', html)) == 3
    assert "로컬 모델 · 단계 최대 6 · 계산은 도구가 수행" in html
    assert re.search(r'<button id="agent-stop-btn" type="button" disabled>중지</button>', html)
    assert '<option value="agent.run.finished"></option>' in html


def test_agent_js_streams_sse_with_fetch_and_abort():
    js = _read("app.js")
    assert "agent: () => loadAgentTab()," in js
    assert "new EventSource" not in js  # Authorization header needs fetch
    run = _function_body(js, "async function runAgent(ev)")
    assert 'fetch("/agent/runs"' in run and "signal: view.controller.signal" in run
    assert "headers.Authorization = `Bearer ${token}`" in run
    assert 'err.name === "AbortError"' in run
    assert "resp.body.getReader()" in _function_body(js, "async function readAgentStream(")
    assert "view.controller.abort()" in _function_body(js, "function stopAgent()")
    for name in (
        "parseSseBlock",
        "splitAgentAnswer",
        "agentStatusText",
        "appendAgentStep",
        "renderAgentAnswer",
        "showAgentSource",
        "openAgentRun",
        "loadAgentRecent",
    ):
        assert f"function {name}(" in js, name
    # citation marks become buttons that open the source panel; amounts use fmtDecimal
    answer = _function_body(js, "function renderAgentAnswer(payload)")
    assert 'className: "agent-ref"' in answer and "showAgentSource(part.mark)" in answer
    source = _function_body(js, "function showAgentSource(mark)")
    assert "fmtDecimal(" in source and "s.inputs" in source and "dartLink(" in js
    assert '"● 근거 확인됨"' in js and "${label}: ${list.join" in js
    assert 'get_financials: "재무제표 조회"' in js and 'calculate: "계산"' in js


def test_agent_css_layout_and_existing_colors_only():
    css = _read("style.css")
    section = css[css.index("/* ---- agent (에이전트) ---- */") :]
    defined = set(re.findall(r"(--[a-z-]+):", css[: css.index("* { box-sizing")]))
    used = set(re.findall(r"var\((--[a-z-]+)\)", section))
    assert used and used <= defined, used - defined
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", section)
    assert "grid-template-columns: minmax(0, 2fr) minmax(0, 1fr)" in section
    assert re.search(r"\.agent-values \{ table-layout: fixed;", section)
    assert "word-break: keep-all" in section


def test_document_status_shows_progress_and_retry_wait_via_text_content():
    js = _read("app.js")
    body = js[js.index("function progressNote(d)") :]
    body = body[: body.index("function renderDocuments()")]
    # "임베딩 120/700" and "재시도 대기(한도 초과) — 14:03:05", built with el(..., { text })
    assert 'embed: "임베딩"' in js
    assert "재시도 대기${reason} — ${fmtClock(d.next_retry_at)}" in body
    assert '"(한도 초과)"' in body
    assert "`${label} ${fmtInt(p.done)}/${fmtInt(p.total)}`" in body
    assert body.count('el("span", { className:') == 2 and "text })" in body
    assert 'el("td", {}, [statusBadge(d.status), progressNote(d)])' in js
    assert ".doc-progress" in _read("style.css")
