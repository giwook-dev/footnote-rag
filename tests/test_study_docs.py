"""학습 문서(docs/study)가 코드와 어긋나지 않는지 검사한다. 네트워크·DB 불필요.

* 백틱 안과 마크다운 링크 대상의 ``src/``·``tests/``·``docs/``·``alembic/`` 경로가 실제로 있는지
* ``path::name``(또는 ``path:name``) 형식의 함수·클래스 참조가 그 파일에 ``def``/``async def``/``class``로 있는지
  (``Class.method``처럼 점으로 이은 이름은 각 부분을 모두 확인)
* 문서 간 상대 링크(``.md`` 포함)가 가리키는 파일이 있는지
* 필수 문서가 모두 있고, 해설 문서가 정해진 틀(배울 것, 확인 질문, 접힌 답)을 갖췄는지
* 면접 질문 25개 이상, 용어 30개 이상, 실습 8개 이상인지
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STUDY = ROOT / "docs" / "study"

TOPIC_DOCS = [
    "01-big-picture.md",
    "02-request-walkthrough.md",
    "03-auth-rbac-isolation.md",
    "04-job-queue.md",
    "05-rag-retrieval.md",
    "06-claude-citations.md",
    "07-dart-integration.md",
    "08-review-audit-usage.md",
    "09-testing-and-ops.md",
    "10-eval-and-mcp.md",
]
REQUIRED_DOCS = ["README.md", *TOPIC_DOCS, "interview-qa.md", "glossary.md", "exercises.md"]

_FENCE_RE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_PATH_REF_RE = re.compile(
    r"(?<![\w/.-])((?:src|tests|docs|alembic)/[\w./-]*)"
    r"(?:(?:::|:)([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*))?"
)


@pytest.fixture(scope="session")
def _database() -> None:
    """Override conftest's autouse DB setup: these checks only read files."""


@pytest.fixture
def _clean_jobs() -> None:
    """Override conftest's autouse job cleanup: no database is needed here."""


def _study_files() -> list[Path]:
    return sorted(STUDY.glob("*.md"))


def _prose(path: Path) -> str:
    """Document text without fenced code blocks (mermaid, code excerpts, shell examples)."""
    return _FENCE_RE.sub("", path.read_text(encoding="utf-8"))


def _code_refs(path: Path) -> list[tuple[str, str | None]]:
    refs: list[tuple[str, str | None]] = []
    for span in _INLINE_CODE_RE.findall(_prose(path)):
        for raw_path, name in _PATH_REF_RE.findall(span):
            refs.append((raw_path.rstrip("."), name or None))
    return refs


def _defines(source: str, name: str) -> bool:
    pattern = rf"^[ \t]*(?:async[ \t]+def|def|class)[ \t]+{re.escape(name)}\b"
    return re.search(pattern, source, re.MULTILINE) is not None


def test_required_study_docs_exist():
    missing = [name for name in REQUIRED_DOCS if not (STUDY / name).is_file()]
    assert not missing, f"missing study docs: {missing}"


@pytest.mark.parametrize("doc", _study_files(), ids=lambda p: p.name)
def test_backtick_paths_exist(doc: Path):
    missing = sorted({p for p, _ in _code_refs(doc) if not (ROOT / p).exists()})
    assert not missing, f"{doc.name}: paths not found in repository: {missing}"


@pytest.mark.parametrize("doc", _study_files(), ids=lambda p: p.name)
def test_function_and_class_references_exist(doc: Path):
    broken = []
    for raw_path, name in _code_refs(doc):
        if name is None:
            continue
        target = ROOT / raw_path
        if not target.is_file():
            broken.append(f"{raw_path}::{name} (file missing)")
            continue
        source = target.read_text(encoding="utf-8")
        for part in name.split("."):
            if not _defines(source, part):
                broken.append(f"{raw_path}::{name} ({part!r} not defined)")
                break
    assert not broken, f"{doc.name}: broken code references: {broken}"


@pytest.mark.parametrize("doc", _study_files(), ids=lambda p: p.name)
def test_relative_links_resolve(doc: Path):
    broken = []
    for target in _LINK_RE.findall(_prose(doc)):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        relative = target.split("#", 1)[0]
        if relative and not (doc.parent / relative).resolve().exists():
            broken.append(target)
    assert not broken, f"{doc.name}: broken relative links: {broken}"


def test_study_docs_reference_code_with_path_and_name():
    """At least the topic docs point at concrete functions, not just files."""
    for name in TOPIC_DOCS:
        refs = [r for r in _code_refs(STUDY / name) if r[1] is not None]
        assert refs, f"{name}: no `path::name` code reference"


@pytest.mark.parametrize("name", TOPIC_DOCS)
def test_topic_docs_follow_the_template(name: str):
    text = (STUDY / name).read_text(encoding="utf-8")
    for heading in (
        "## 이 문서에서 배울 것",
        "## 개념",
        "## 이 저장소의 구현",
        "## 왜 이렇게 했나",
    ):
        assert heading in text, f"{name}: missing section {heading!r}"
    assert "## 확인 질문" in text, f"{name}: missing review questions"
    assert text.count("<details>") >= 3, f"{name}: review answers must be folded in <details>"


def test_interview_qa_has_enough_complete_questions():
    text = (STUDY / "interview-qa.md").read_text(encoding="utf-8")
    blocks = re.split(r"^### ", text, flags=re.MULTILINE)[1:]
    questions = [b for b in blocks if re.match(r"Q\d+", b)]
    assert len(questions) >= 25, f"only {len(questions)} questions"
    for block in questions:
        title = block.splitlines()[0]
        for label in ("30초 답변 요점", "근거", "꼬리 질문"):
            assert label in block, f"{title}: missing {label!r}"


def test_glossary_has_enough_terms_with_links():
    text = (STUDY / "glossary.md").read_text(encoding="utf-8")
    blocks = re.split(r"^### ", text, flags=re.MULTILINE)[1:]
    assert len(blocks) >= 30, f"only {len(blocks)} terms"
    for block in blocks:
        title = block.splitlines()[0]
        body = "\n".join(block.splitlines()[1:]).strip()
        assert body, f"{title}: empty definition"
        assert _LINK_RE.search(block), f"{title}: no related-doc link"


def test_exercises_have_goal_hint_check_and_no_answer_code():
    text = (STUDY / "exercises.md").read_text(encoding="utf-8")
    blocks = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    exercises = [b for b in blocks if re.match(r"실습 \d+", b)]
    assert len(exercises) >= 8, f"only {len(exercises)} exercises"
    for block in exercises:
        title = block.splitlines()[0]
        for label in ("목표", "힌트", "확인 방법"):
            assert label in block, f"{title}: missing {label!r}"
    fenced_python = re.findall(r"^```python", text, flags=re.MULTILINE)
    assert not fenced_python, "exercises.md must not contain answer code"
