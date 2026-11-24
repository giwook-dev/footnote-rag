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
