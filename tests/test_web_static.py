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
