"""DART 원문 XML을 목차 경로(section_path)가 붙은 섹션 목록으로 바꾸는 순수 함수.

역할: ``SECTION-n``+``TITLE``로 경로를 만들고, 표는 행 단위 ``셀 | 셀``로 바꾼다. 주석 섹션 안에서는
섹션 제목 뒤의 추가 ``TITLE``(실제 사업보고서의 주석 번호별 소제목)을 하위 목차로 만들고, 그런 ``TITLE``이
없을 때만 ``12. 리스`` 같은 짧은 번호 문단을 하위 목차로 승격한다.
구조 확인: 삼성전자 2025 사업보고서(20260310002820) 원문으로 확인한 내용은 docs/data-sources.md에 있다.
주요 요소: ``parse_document_zip``, ``parse_document_xml``, ``pick_main_member``, ``is_note_heading``, ``normalize_title``.
호출 관계: ``jobs.handlers.handle_import_dart``, ``cli.cmd_dart_fetch``, ``evals.validate``가 호출한다.
학습 문서: docs/study/07-dart-integration.md

DART filing XML -> ordered sections with table-of-contents paths.

Pure functions, no I/O. Input is the ZIP returned by ``/api/document.xml``.

Structure handled (DART "dart3.xsd" style documents):

    <DOCUMENT>
      <DOCUMENT-NAME ACODE="...">감사보고서</DOCUMENT-NAME>
      <COMPANY-NAME AREGCIK="00126380">삼성전자주식회사</COMPANY-NAME>
      <BODY>
        <COVER><COVER-TITLE>감 사 보 고 서</COVER-TITLE> <P>...</P></COVER>
        <SECTION-1><TITLE ATOC="Y">독립된 감사인의 감사보고서</TITLE><P>...</P></SECTION-1>
        <SECTION-1><TITLE>(첨부)재 무 제 표</TITLE>
          <SECTION-2><TITLE>재 무 상 태 표</TITLE><TABLE>...<TR><TD/><TE/><TU/></TR></TABLE></SECTION-2>
          <SECTION-2><TITLE>주석</TITLE><P>1. 일반사항</P><P>...</P><P>12. 리스</P>...</SECTION-2>
        </SECTION-1>
      </BODY>
    </DOCUMENT>

* ``SECTION-n`` + ``TITLE`` build the path (``(첨부)재무제표 > 주석``).
* Inside a section whose title contains "주석" (notes), every ``TITLE`` after the section's own
  title is a note sub-heading (annual reports: ``<TITLE ATOC="Y">1. 일반적 사항 (연결)</TITLE>``)
  and starts a sub-section: ``... > 3. 연결재무제표 주석 > 1. 일반적 사항 (연결)``. The heading
  text is not repeated in the body; text before the first sub-heading stays on the notes path.
  In real filings each note is a ``TABLE-GROUP`` holding that ``TITLE`` and the note's ``TABLE``
  elements; such titled groups are flattened so the same rule applies.
* Only when a notes section has no such ``TITLE`` sub-headings, short numbered paragraphs such
  as ``12. 리스`` start a sub-section instead (``... > 주석 > 12. 리스``).
* Lower-level headings ("가.", "나.") are not split.
* Tables become one line per row: ``cell | cell | cell``.
* Container tags without a TITLE (``LIBRARY`` etc.) are descended into transparently.
* The main document is ``{rcept_no}.xml`` when present, otherwise the largest XML member.
  Other XML members (attachments such as 외부감사 실시내용) are *not* included.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from footnote_rag.dart.client import read_zip_members

PATH_SEP = " > "
_SECTION_RE = re.compile(r"^SECTION-\d+$")
# Top-level note heading: "12. 리스", "3. 중요한 회계정책". Not "(1) ..." / "1) ..." sub-items,
# not numbers like "1.5", and not full sentences (which end in "다." in Korean filings).
_NOTE_HEADING_RE = re.compile(r"^(\d{1,2})\.\s*[^\d\s.][^\n]{0,38}$")


def is_note_heading(text: str) -> bool:
    text = text.strip()
    if not _NOTE_HEADING_RE.match(text):
        return False
    return not text.rstrip(" .").endswith(("다", "음", "함"))


_WS_RE = re.compile(r"[ \t\r\f\v　\xa0]+")
_TABLE_CELL_TAGS = {"TD", "TH", "TE", "TU"}
_TABLE_LIKE_TAGS = {"TABLE", "COVER"}
_SKIP_TAGS = {"TITLE", "COVER-TITLE", "IMG", "IMAGE", "PGBRK", "IMG-CAPTION"}


class DartParseError(Exception):
    """Raised when a filing cannot be turned into sections. Not retryable."""


@dataclass
class Section:
    path: str
    text: str


@dataclass
class ParsedDocument:
    title: str
    company_name: str
    corp_code: str
    document_name: str
    main_member: str
    sections: list[Section] = field(default_factory=list)


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def normalize_title(text: str) -> str:
    """Collapse whitespace; join letter-spaced titles ("재 무 상 태 표" -> "재무상태표")."""
    text = normalize_ws(text.replace("\n", " "))
    tokens = text.split(" ")
    if len(tokens) >= 3 and sum(len(t) == 1 for t in tokens) >= len(tokens) - 1:
        # Letter-spaced title, optionally with a one-off longer token like "(첨부)재".
        return "".join(tokens)
    return text


def _tag(el: etree._Element) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""  # comments / processing instructions
    return tag.split("}", 1)[-1].upper()


def _text_of(el: etree._Element) -> str:
    return normalize_ws(" ".join(t for t in el.itertext()))


def _table_text(table: etree._Element) -> str:
    rows: list[str] = []
    for tr in table.iter():
        if _tag(tr) != "TR":
            continue
        cells = [_text_of(c) for c in tr if _tag(c) in _TABLE_CELL_TAGS]
        cells = [c for c in cells if c]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _has_section_descendant(el: etree._Element) -> bool:
    return any(_SECTION_RE.match(_tag(d)) for d in el.iterdescendants())


class _Collector:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.parts: dict[str, list[str]] = {}

    def add(self, path: list[str], text: str) -> None:
        text = text.strip()
        if not text:
            return
        key = PATH_SEP.join(p for p in path if p) or "본문"
        if key not in self.parts:
            self.order.append(key)
            self.parts[key] = []
        self.parts[key].append(text)

    def sections(self) -> list[Section]:
        return [Section(path=k, text="\n".join(self.parts[k])) for k in self.order]


def _is_notes_title(title: str) -> bool:
    return "주석" in title


def _is_titled_group(el: etree._Element) -> bool:
    """A non-section container whose direct children include a ``TITLE``.

    Real annual reports wrap each note in ``<TABLE-GROUP><TITLE>1. 일반적 사항 (연결)</TITLE>
    <TABLE>…</TABLE>…</TABLE-GROUP>`` directly under the notes ``SECTION-2``.
    """
    tag = _tag(el)
    if not tag or tag in _TABLE_LIKE_TAGS or _SECTION_RE.match(tag):
        return False
    return any(_tag(c) == "TITLE" for c in el)


def _notes_children(el: etree._Element) -> list[etree._Element]:
    """Children of a notes element with titled groups flattened, so a group's ``TITLE`` reads as a
    sub-heading and its tables/paragraphs follow it in document order."""
    flat: list[etree._Element] = []
    for child in el:
        if _is_titled_group(child):
            flat.extend(_notes_children(child))
        else:
            flat.append(child)
    return flat


def _walk(el: etree._Element, path: list[str], out: _Collector, *, own_title: bool = False) -> None:
    """Walk ``el``'s children. ``own_title``: the first ``TITLE`` child is ``el``'s own title
    (already the last element of ``path``) and is skipped."""
    in_notes = bool(path) and _is_notes_title(path[-1])
    own_title_el = next((c for c in el if _tag(c) == "TITLE"), None) if own_title else None
    children = _notes_children(el) if in_notes else list(el)
    # Notes sub-headings written as extra TITLE elements (real annual reports) take priority over
    # the short numbered-paragraph heuristic, so the two rules never promote headings twice.
    title_headings = in_notes and any(
        _tag(c) == "TITLE" and c is not own_title_el for c in children
    )
    note_heading: str | None = None

    def current() -> list[str]:
        return [*path, note_heading] if note_heading else path

    for child in children:
        tag = _tag(child)
        if not tag:
            continue
        if tag == "TITLE" and title_headings and child is not own_title_el:
            heading = normalize_title(_text_of(child))
            if heading:
                note_heading = heading  # the heading itself is not repeated in the body text
            continue
        if tag in _SKIP_TAGS:
            continue
        if _SECTION_RE.match(tag):
            title_el = next((c for c in child if _tag(c) == "TITLE"), None)
            title = normalize_title(_text_of(title_el)) if title_el is not None else ""
            _walk(child, [*path, title] if title else path, out, own_title=bool(title))
            continue
        if tag == "COVER":
            cover_title_el = next((c for c in child if _tag(c) == "COVER-TITLE"), None)
            cover_title = (
                normalize_title(_text_of(cover_title_el)) if cover_title_el is not None else ""
            )
            _walk(child, [*path, cover_title or "표지"], out)
            continue
        if tag == "TABLE":
            out.add(current(), _table_text(child))
            continue
        if _has_section_descendant(child):
            _walk(child, path, out)  # transparent container (LIBRARY, BODY, ...)
            continue
        text = _text_of(child)
        if in_notes and not title_headings and is_note_heading(text):
            note_heading = text
            continue
        out.add(current(), text)


def _decode_xml(xml_bytes: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    head = xml_bytes[:200].lower()
    if b"encoding=" in head:
        root = etree.fromstring(xml_bytes, parser=parser)  # honour the declared encoding
    else:
        try:
            text = xml_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = xml_bytes.decode("cp949", errors="replace")
        root = etree.fromstring(text.encode("utf-8"), parser=parser)
    if root is None:
        raise DartParseError("XML could not be parsed")
    return root


def pick_main_member(members: dict[str, bytes], rcept_no: str | None) -> str:
    xml_names = [n for n in members if n.lower().endswith(".xml")]
    if not xml_names:
        raise DartParseError("ZIP contains no XML document")
    if rcept_no:
        for name in xml_names:
            if name.rsplit("/", 1)[-1].lower() == f"{rcept_no}.xml":
                return name
    return max(xml_names, key=lambda n: len(members[n]))


def parse_document_xml(xml_bytes: bytes, *, main_member: str = "") -> ParsedDocument:
    root = _decode_xml(xml_bytes)
    doc_name_el = next((e for e in root.iter() if _tag(e) == "DOCUMENT-NAME"), None)
    company_el = next((e for e in root.iter() if _tag(e) == "COMPANY-NAME"), None)
    document_name = normalize_title(_text_of(doc_name_el)) if doc_name_el is not None else ""
    company_name = normalize_ws(_text_of(company_el)) if company_el is not None else ""
    corp_code = (company_el.get("AREGCIK") or "").strip() if company_el is not None else ""

    body = next((e for e in root.iter() if _tag(e) == "BODY"), root)
    out = _Collector()
    _walk(body, [], out)
    sections = out.sections()
    if not any(s.text.strip() for s in sections):
        raise DartParseError("document has no extractable text")
    title = " ".join(p for p in (company_name, document_name) if p) or "DART 문서"
    return ParsedDocument(
        title=title,
        company_name=company_name,
        corp_code=corp_code,
        document_name=document_name,
        main_member=main_member,
        sections=sections,
    )


def parse_document_zip(content: bytes, rcept_no: str | None = None) -> ParsedDocument:
    try:
        members = read_zip_members(content)
    except Exception as exc:  # noqa: BLE001 - BadZipFile and friends
        raise DartParseError(f"invalid ZIP: {type(exc).__name__}") from None
    name = pick_main_member(members, rcept_no)
    return parse_document_xml(members[name], main_member=name)
