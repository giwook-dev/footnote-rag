"""에이전트 도구: 로컬 모델이 고르고, 코드가 실행한다. 모두 읽기 전용이다.

역할: 도구 6개(``list_documents``, ``search_filings``, ``read_note``, ``compare_notes``, ``get_financials``,
``calculate``)의 이름·한국어 설명·JSON Schema 파라미터·실행 함수를 정의하고, 인자 검사(``validate_args``),
결과 자르기, 근거 번호(``S``/``N``/``C``/``F``/``K``) 매기기, 숫자 모으기를 한다. 실패는 예외 대신 ``ok=False``와
한국어 오류문으로 돌려준다(모델이 고쳐 다시 부를 수 있게).
주요 요소: ``Tool``, ``TOOLS``, ``ToolResult``, ``AgentContext``(문서 별칭·파싱 캐시·근거·숫자),
``load_aliases``, ``run_tool``, ``validate_args``, ``evaluate_expression``, ``find_note``, ``report_period``.
호출 관계: ``agent.runner``가 ``run_tool``을 부른다. ``answering.service.retrieve``(검색),
``comparison.notes``(주석 추출·비교), ``comparison.service.check_pair``, ``jobs.handlers.load_dart_zip``,
``dart.parser.parse_document_zip``, ``DartClient.financial_statements``(OpenDART 재무제표)를 쓴다.
학습 문서: docs/study/12-agent.md, docs/adr/0013-tool-using-agent.md

Rules:

* documents are shown to the model only by alias (``D1``, ``D2`` …): ready DART documents of the
  caller's organization, ordered by company, then newest filing first. Aliases of other
  organizations never exist, and UUIDs are never shown or accepted.
* source ids are unique within a run: ``S#`` search hit, ``N#`` note page (lines ``L#``),
  ``C#`` note comparison (lines ``P#``/``C#``), ``F#`` financial statement row, ``K#`` calculation.
* a result text is cut to ``AGENT_TOOL_RESULT_CHARS``; sources whose ``[id]`` was cut off are
  dropped (the model never saw them). ``numbers`` = ``number_check.extract_numbers`` of the text
  actually sent.
* ``calculate``: ``+ - * /``, parentheses, ``round(x, n)``, names of ``values``; ``Decimal``
  arithmetic, ``round`` is half-up. Every value must occur in an earlier successful result's
  ``numbers`` (``contains_number``: commas and dot thousands separators accepted). Literals in the
  expression: one-digit integers and 10^1 … 10^12 only (unit conversion).
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.answering.number_check import DOT_GROUPED_RE, contains_number, extract_numbers
from footnote_rag.answering.service import (
    DocumentScopeError,
    RetrievalUnavailableError,
    retrieve,
)
from footnote_rag.comparison.notes import SCOPES, compare_documents, extract_notes
from footnote_rag.comparison.service import ComparisonError, check_pair, diff_options
from footnote_rag.config import Settings
from footnote_rag.dart.client import (
    FS_DIVS,
    REPORT_CODES,
    STATEMENT_DIVS,
    DartClient,
    DartError,
)
from footnote_rag.dart.parser import DartParseError, ParsedDocument, parse_document_zip
from footnote_rag.dart.sections import PATH_SEPARATOR, SCOPE_LABELS, labeled_section_path, note_key
from footnote_rag.db.models import Chunk, Document, User
from footnote_rag.jobs.handlers import dart_cache_dir, dart_url, load_dart_zip
from footnote_rag.observability import get_logger
from footnote_rag.retrieval.embedder import Embedder

log = get_logger(__name__)

SOURCE_PREFIXES = ("S", "N", "C", "F", "K")
SEARCH_CHUNK_CHARS = 800
SEARCH_MAX_TOP_K = 8
SEARCH_DEFAULT_TOP_K = 5
NOTE_PAGE_LINES = 120
NOTE_PAGE_CHARS = 6000
NOTE_MATCH_RATIO = 0.6
NOTE_CANDIDATES = 10
COMPARE_MAX_LINES = 40
COMPARE_MAX_ROWS = 20
FINANCIAL_MAX_ROWS = 20
FINANCIAL_ACCOUNT_LIST = 40
EXPRESSION_MAX_CHARS = 300
CALC_MAX_VALUES = 20
ROUND_MAX_DIGITS = 10
# Unit conversion constants allowed as literals: 10, 100, …, 1조 (10^12).
UNIT_CONSTANTS = frozenset(10**k for k in range(1, 13))
TRUNCATED_NOTE = "\n…(결과가 길어 여기서 잘렸습니다)"
REPORT_PERIOD_RE = re.compile(r"\((\d{4})\.(\d{1,2})\)")
# "2025년 01월 01일 부터 2025년 12월 31일 까지" on a report cover: the period's end year and month.
COVER_PERIOD_RE = re.compile(
    r"\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일\s*부\s*터\s*(\d{4})\s*년\s*(\d{1,2})\s*월"
)
COVER_CHUNKS = 3  # first chunks of a document searched for the cover period
_SPACE_RE = re.compile(r"\s+")
_NOTE_NUMBER_QUERY_RE = re.compile(r"^(?:주석\s*)?(\d+)\s*(?:[.)]|번)?$")
_ID_TOKEN_RE = re.compile(r"\[([A-Z]\d+)\]")
_VALUE_RE = re.compile(r"^\(?\s*([+\-−])?\s*(\d[\d,.]*)\s*\)?$")


class ToolError(Exception):
    """A tool failure reported to the model as ``ok=False`` with a Korean message."""


@dataclass
class ToolResult:
    ok: bool
    text: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)
    error: str | None = None
    # Per source id: the normalized numbers it carries (calculate uses it to name a value's source).
    source_numbers: dict[str, list[str]] = field(default_factory=dict)

    @property
    def model_text(self) -> str:
        """What the model receives as the ``tool`` message (``오류: …`` for a failure)."""
        return self.text


@dataclass
class AgentContext:
    """Everything a tool needs for one run. ``aliases`` maps ``D1``… to documents of
    ``user.org_id`` only; ``parse_cache`` keeps parsed filings for the run."""

    session: AsyncSession
    user: User
    settings: Settings
    embedder: Embedder
    dart_client: DartClient
    aliases: dict[str, Document] = field(default_factory=dict)
    # alias -> (fiscal year, end month) from the cover text (``load_periods``).
    periods: dict[str, tuple[str, str]] = field(default_factory=dict)
    parse_cache: dict[uuid.UUID, ParsedDocument] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_numbers: dict[str, list[str]] = field(default_factory=dict)
    numbers: list[str] = field(default_factory=list)

    def next_id(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}{self.counters[prefix]}"

    def alias_of(self, document_id: uuid.UUID) -> str | None:
        return next((a for a, d in self.aliases.items() if d.id == document_id), None)

    def document(self, value: Any) -> tuple[str, Document]:
        alias = str(value or "").strip().strip("[]").upper()
        doc = self.aliases.get(alias)
        if doc is None:
            known = ", ".join(self.aliases) or "없음"
            raise ToolError(
                f"알 수 없는 문서 별칭: {value}. list_documents가 준 별칭만 쓰세요(현재: {known})."
            )
        return alias, doc

    def haystack(self) -> str:
        return " ".join(self.numbers)

    def period(self, alias: str) -> tuple[str, str] | None:
        doc = self.aliases[alias]
        return self.periods.get(alias) or report_period(_meta(doc).get("report_nm"))

    def report_label(self, alias: str) -> str:
        """``사업보고서 (2025.12)``: the report name with its period when known."""
        doc = self.aliases[alias]
        name = str(_meta(doc).get("report_nm") or doc.title)
        period = self.period(alias)
        if period is None or report_period(name) is not None:
            return name
        return f"{name} ({period[0]}.{period[1]})"

    def label(self, alias: str) -> str:
        doc = self.aliases[alias]
        return f"{alias} {_meta(doc).get('corp_name') or doc.title} · {self.report_label(alias)}"

    def fields(self, alias: str) -> dict[str, Any]:
        """Document fields of a source (shown in the web source panel)."""
        doc = self.aliases[alias]
        meta = _meta(doc)
        rcept_no = _rcept_no(doc)
        return {
            "document": alias,
            "corp_name": meta.get("corp_name"),
            "report_nm": self.report_label(alias),
            "document_title": doc.title,
            "rcept_no": rcept_no,
            "dart_url": meta.get("dart_url") or (dart_url(rcept_no) if rcept_no else None),
        }

    def register(self, result: ToolResult) -> None:
        """Remember a finished result: its sources and (when ok) its numbers."""
        for source in result.sources:
            self.sources[source["id"]] = source
        self.source_numbers.update(result.source_numbers)
        if result.ok:
            self.numbers.extend(n for n in result.numbers if n not in self.numbers)

    async def parse(self, alias: str, doc: Document) -> ParsedDocument:
        cached = self.parse_cache.get(doc.id)
        if cached is not None:
            return cached
        rcept_no = _rcept_no(doc)
        try:
            content = await load_dart_zip(
                self.dart_client, dart_cache_dir(self.settings.dart_dir, self.dart_client), rcept_no
            )
        except DartError as exc:
            log.warning("agent.dart_document_failed", rcept_no=rcept_no, code=exc.code)
            raise ToolError(
                f"OpenDART 원문을 가져오지 못했습니다({alias}, 코드 {exc.code})."
            ) from None
        try:
            parsed = await asyncio.to_thread(parse_document_zip, content, rcept_no)
        except DartParseError as exc:
            raise ToolError(f"원문을 파싱하지 못했습니다({alias}): {exc}") from None
        self.parse_cache[doc.id] = parsed
        return parsed


# ---------------------------------------------------------------- documents and aliases


def _rcept_no(doc: Document) -> str:
    return str((doc.meta or {}).get("rcept_no") or doc.source_ref or "")


def _meta(doc: Document) -> dict[str, Any]:
    return doc.meta or {}


def report_period(report_nm: str | None) -> tuple[str, str] | None:
    """``("2025", "12")`` from ``사업보고서 (2025.12)``; None when the name has no period."""
    m = REPORT_PERIOD_RE.search(str(report_nm or ""))
    return (m.group(1), m.group(2).zfill(2)) if m else None


def report_code(report_nm: str | None, period: tuple[str, str] | None = None) -> str | None:
    """OpenDART ``reprt_code``: 사업 11011, 반기 11012, 1분기(03) 11013, 3분기(09) 11014.
    ``period`` (year, end month) is used when the name carries none."""
    name = str(report_nm or "")
    period = report_period(name) or period
    if "사업보고서" in name:
        return "11011"
    if "반기보고서" in name:
        return "11012"
    if "분기보고서" in name and period is not None:
        return {"03": "11013", "09": "11014"}.get(period[1])
    return None


def cover_period(text: str) -> tuple[str, str] | None:
    """(year, end month) of the reporting period written on a cover:
    ``2025년 01월 01일부터 2025년 12월 31일까지`` -> ``("2025", "12")``."""
    m = COVER_PERIOD_RE.search(text)
    return (m.group(1), m.group(2).zfill(2)) if m else None


def _filing_period(doc: Document) -> tuple[str, str] | None:
    """Last resort for an annual report: filed in the first half of year N -> fiscal year N-1."""
    meta = _meta(doc)
    dt = str(meta.get("rcept_dt") or "")
    if "사업보고서" in str(meta.get("report_nm") or "") and len(dt) == 8 and dt[4:6] <= "06":
        return (str(int(dt[:4]) - 1), "12")
    return None


async def load_periods(
    session: AsyncSession, aliases: dict[str, Document]
) -> dict[str, tuple[str, str]]:
    """Reporting period per alias, read from the document's first chunks (the cover), then the
    report name, then the filing date (annual reports only). Unknown periods are left out."""
    if not aliases:
        return {}
    rows = await session.execute(
        select(Chunk.document_id, Chunk.text)
        .where(
            Chunk.document_id.in_([d.id for d in aliases.values()]),
            Chunk.chunk_index < COVER_CHUNKS,
        )
        .order_by(Chunk.document_id, Chunk.chunk_index)
    )
    texts: dict[uuid.UUID, list[str]] = {}
    for document_id, chunk_text in rows:
        texts.setdefault(document_id, []).append(chunk_text)
    out: dict[str, tuple[str, str]] = {}
    for alias, doc in aliases.items():
        period = (
            cover_period("\n".join(texts.get(doc.id, [])))
            or report_period(_meta(doc).get("report_nm"))
            or _filing_period(doc)
        )
        if period is not None:
            out[alias] = period
    return out


async def build_context(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
    embedder: Embedder,
    dart_client: DartClient,
) -> AgentContext:
    """A run's context: the org's document aliases and their periods, nothing of other orgs."""
    aliases = await load_aliases(session, user.org_id)
    return AgentContext(
        session=session,
        user=user,
        settings=settings,
        embedder=embedder,
        dart_client=dart_client,
        aliases=aliases,
        periods=await load_periods(session, aliases),
    )


async def load_aliases(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Document]:
    """``{"D1": doc, …}`` for the ready DART documents of ``org_id`` (nothing else), ordered by
    company name, then the newest filing first."""
    docs = list(
        await session.scalars(
            select(Document).where(
                Document.org_id == org_id, Document.status == "ready", Document.source == "dart"
            )
        )
    )
    docs.sort(key=lambda d: (str(_meta(d).get("rcept_dt") or ""), _rcept_no(d)), reverse=True)
    docs.sort(key=lambda d: str(_meta(d).get("corp_name") or d.title))  # stable: newest first
    return {f"D{i}": doc for i, doc in enumerate(docs, start=1)}


# ---------------------------------------------------------------- argument validation


def _coerce(name: str, schema: dict[str, Any], value: Any) -> Any:
    kind = schema.get("type")
    if kind == "string":
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ToolError(f"인자 {name}는 문자열이어야 합니다.")
        if isinstance(value, float) and value.is_integer():
            value = int(value)  # 6e10 -> "60000000000", not "60000000000.0"
        text = str(value).strip()
        enum = schema.get("enum")
        if enum:
            match = next((e for e in enum if e.lower() == text.lower()), None)
            if match is None:
                raise ToolError(f"인자 {name}는 {', '.join(enum)} 중 하나여야 합니다: {value}")
            return match
        if not text and schema.get("minLength", 0) > 0:
            raise ToolError(f"인자 {name}가 비어 있습니다.")
        return text
    if kind == "integer":
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"인자 {name}는 정수여야 합니다.")
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ToolError(f"인자 {name}는 {lo}~{hi} 사이여야 합니다: {value}")
        return value
    if kind == "array":
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list):
            raise ToolError(f"인자 {name}는 목록이어야 합니다.")
        out = [_coerce(f"{name}[]", schema.get("items") or {"type": "string"}, v) for v in items]
        if len(out) < schema.get("minItems", 0):
            raise ToolError(f"인자 {name}에 값이 하나 이상 있어야 합니다.")
        return out
    if kind == "object":
        if not isinstance(value, dict):
            raise ToolError(f"인자 {name}는 {{이름: 값}} 객체여야 합니다.")
        item_schema = schema.get("additionalProperties") or {"type": "string"}
        return {str(k): _coerce(f"{name}.{k}", item_schema, v) for k, v in value.items()}
    return value


def validate_args(schema: dict[str, Any], args: Any) -> dict[str, Any]:
    """Check ``args`` against a tool's JSON Schema (the subset the tools use) and coerce small
    slips (``"5"`` for 5, one string for a list of strings, a number for a number string).
    Unknown or missing arguments are errors."""
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            raise ToolError("인자가 JSON 객체가 아닙니다.") from None
    if not isinstance(args, dict):
        raise ToolError("인자는 JSON 객체여야 합니다.")
    props: dict[str, Any] = schema.get("properties") or {}
    unknown = [k for k in args if k not in props]
    if unknown:
        raise ToolError(
            f"알 수 없는 인자: {', '.join(unknown)} (가능: {', '.join(props) or '없음'})"
        )
    missing = [k for k in schema.get("required") or [] if args.get(k) in (None, "", [])]
    if missing:
        raise ToolError(f"필수 인자가 없습니다: {', '.join(missing)}")
    return {k: _coerce(k, props[k], v) for k, v in args.items() if v is not None}


# ---------------------------------------------------------------- helpers


def _fmt_amount(value: str | None) -> str:
    """``"38765000000"`` -> ``"38,765,000,000"`` (strings only, no float)."""
    if value is None or value == "":
        return "-"
    m = re.fullmatch(r"([+-]?)(\d+)(\.\d+)?", value)
    if not m:
        return value
    return f"{m.group(1)}{int(m.group(2)):,}{m.group(3) or ''}"


def _amount(raw: Any) -> str | None:
    text = str(raw or "").strip().replace(",", "")
    return text if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text) else None


def _compact(text: str) -> str:
    return _SPACE_RE.sub("", str(text or ""))


@dataclass
class _Named:
    key: str
    title: str
    payload: Any = None


def query_key(query: str) -> str:
    """Comparison key of a note name the model typed, normalized like ``dart.sections.note_key``
    (number, ``(연결)``, spaces and the word "연결" removed)."""
    return note_key(f"연결재무제표 주석{PATH_SEPARATOR}{query.strip()}") or _compact(query)


def _note_number(title: str) -> str | None:
    m = re.match(r"^\s*\(?(\d+)[.)]", title)
    return m.group(1) if m else None


def find_note(items: list[_Named], query: str) -> _Named:
    """A note by number (``20``), by key (``우발부채와약정사항``), else the most similar key with a
    ratio of at least 0.6; otherwise a ``ToolError`` listing up to 10 candidate titles."""
    if not items:
        raise ToolError("이 문서에서 해당 범위의 주석을 찾지 못했습니다.")
    q = str(query).strip()
    number = _NOTE_NUMBER_QUERY_RE.match(q)
    if number:
        found = next((i for i in items if _note_number(i.title) == number.group(1)), None)
        if found is not None:
            return found
    key = query_key(q)
    exact = next((i for i in items if i.key == key), None)
    if exact is not None:
        return exact
    scored = sorted(
        ((difflib.SequenceMatcher(None, key, i.key).ratio(), n, i) for n, i in enumerate(items)),
        key=lambda t: (-t[0], t[1]),
    )
    if scored and scored[0][0] >= NOTE_MATCH_RATIO:
        return scored[0][2]
    candidates = ", ".join(i.title for _r, _n, i in scored[:NOTE_CANDIDATES])
    raise ToolError(f"주석 '{q}'을(를) 찾지 못했습니다. 후보: {candidates}")


def _scope_label(scope: str) -> str:
    return SCOPE_LABELS.get(scope, f"[{scope}]")  # type: ignore[call-overload]


# ---------------------------------------------------------------- tools


ToolHandler = Callable[[AgentContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    label: str  # Korean display name (web timeline)
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def spec(self) -> dict[str, Any]:
        """Ollama / OpenAI ``tools`` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


async def list_documents(ctx: AgentContext, args: dict[str, Any]) -> ToolResult:
    del args
    if not ctx.aliases:
        return ToolResult(ok=True, text="준비된(ready) DART 문서가 없습니다.")
    lines = [f"조직의 DART 문서 {len(ctx.aliases)}개 (다른 도구에는 별칭 D#로 지정):"]
    for alias, doc in ctx.aliases.items():
        meta = _meta(doc)
        period = ctx.period(alias)
        rcept_dt = str(meta.get("rcept_dt") or "")
        parts = [alias, str(meta.get("corp_name") or doc.title), ctx.report_label(alias)]
        if period:
            parts.append(f"사업연도 {period[0]}")
        if len(rcept_dt) == 8:
            parts.append(f"접수 {rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:]}")
        lines.append(" · ".join(parts))
    return ToolResult(ok=True, text="\n".join(lines))


async def search_filings(ctx: AgentContext, args: dict[str, Any]) -> ToolResult:
    query = args["query"]
    top_k = args.get("top_k", SEARCH_DEFAULT_TOP_K)
    picked = [ctx.document(a) for a in args.get("documents") or []]
    ids = list(dict.fromkeys(doc.id for _a, doc in picked)) or None
    try:
        chunks = await retrieve(
            ctx.session,
            user=ctx.user,
            query=query,
            top_k=top_k,
            document_ids=ids,
            embedder=ctx.embedder,
            settings=ctx.settings,
        )
    except DocumentScopeError:
        raise ToolError("지정한 문서를 찾을 수 없습니다.") from None
    except RetrievalUnavailableError:
        raise ToolError(
            "임베딩 서비스를 쓸 수 없어 검색하지 못했습니다. 잠시 후 다시 시도하세요."
        ) from None
    hits = [(ctx.alias_of(c.document_id), c) for c in chunks]
    hits = [(a, c) for a, c in hits if a is not None]
    if not hits:
        return ToolResult(ok=True, text=f"'{query}' 검색 결과가 없습니다.")
    blocks: list[str] = []
    sources: list[dict[str, Any]] = []
    numbers: dict[str, list[str]] = {}
    for alias, chunk in hits:
        sid = ctx.next_id("S")
        body = chunk.text.strip()[:SEARCH_CHUNK_CHARS]
        path = labeled_section_path(chunk.section_path)
        blocks.append(f"[{sid}] {ctx.label(alias)} · {path} · 유사도 {chunk.score:.3f}\n{body}")
        sources.append(
            {
                "id": sid,
                "kind": "search",
                **ctx.fields(alias),
                "section_path": chunk.section_path,
                "score": round(chunk.score, 4),
                "excerpt": body,
            }
        )
        numbers[sid] = [v for _r, v in extract_numbers(body)]
    return ToolResult(ok=True, text="\n\n".join(blocks), sources=sources, source_numbers=numbers)


async def read_note(ctx: AgentContext, args: dict[str, Any]) -> ToolResult:
    alias, doc = ctx.document(args["document"])
    scope = args.get("scope", "consolidated")
    offset = args.get("offset", 0)
    parsed = await ctx.parse(alias, doc)
    notes = extract_notes(parsed.sections, scope)
    if not notes:
        raise ToolError(
            f"{alias}에서 {_scope_label(scope)} 재무제표 주석을 찾지 못했습니다."
            + (
                " scope=separate(별도)로 다시 시도할 수 있습니다."
                if scope == "consolidated"
                else ""
            )
        )
    note = find_note([_Named(n.key, n.title, n) for n in notes], args["note"]).payload
    total = len(note.lines)
    if offset >= total:
        raise ToolError(f"offset {offset}은(는) 주석 줄 수({total})보다 작아야 합니다.")
    budget = min(NOTE_PAGE_CHARS, max(500, ctx.settings.agent_tool_result_chars - 400))
    page: list[tuple[str, str]] = []
    used = 0
    for i in range(offset, total):
        line = f"L{i + 1} {note.lines[i]}"
        if page and (len(page) >= NOTE_PAGE_LINES or used + len(line) + 1 > budget):
            break
        page.append((f"L{i + 1}", note.lines[i]))
        used += len(line) + 1
    end = offset + len(page)
    nid = ctx.next_id("N")
    head = (
        f"[{nid}] {ctx.label(alias)} · {_scope_label(scope)} {note.title} · "
        f"줄 L{offset + 1}-L{end} / 전체 {total}줄 (근거는 [{nid}:L번호]로 표시)"
    )
    more = f"\n(이어서 읽으려면 offset={end})" if end < total else ""
    text = head + "\n" + "\n".join(f"{ref} {t}" for ref, t in page) + more
    lines = dict(page)
    source = {
        "id": nid,
        "kind": "note",
        **ctx.fields(alias),
        "section_path": note.section_path,
        "note_title": note.title,
        "scope": scope,
        "line_start": offset + 1,
        "line_end": end,
        "total_lines": total,
        "refs": list(lines),
        "lines": lines,
        "excerpt": "\n".join(t for _r, t in page[:8])[:600],
    }
    numbers = [v for t in lines.values() for _r, v in extract_numbers(t)]
    return ToolResult(ok=True, text=text, sources=[source], source_numbers={nid: numbers})


_KIND_LABELS = {
    "yoy": "전년 대비",
    "prior_mismatch": "전기 수치 불일치",
    "row_added": "행 추가",
    "row_removed": "행 삭제",
}
_KIND_ORDER = {"prior_mismatch": 0, "yoy": 1, "row_added": 2, "row_removed": 3}


def _abs_delta(change: dict[str, Any]) -> Decimal:
    cells = change.get("cells") or []
    try:
        return max((abs(Decimal(str(c.get("delta")))) for c in cells), default=Decimal(0))
    except InvalidOperation:
        return Decimal(0)


def _change_rank(change: dict[str, Any]) -> tuple[int, int, Decimal]:
    large = any("large_change" in (c.get("flags") or []) for c in change.get("cells") or [])
    return (_KIND_ORDER.get(change.get("kind"), 9), 0 if large else 1, -_abs_delta(change))


def _change_line(change: dict[str, Any]) -> str:
    occ = f" ({change['occurrence'] + 1}번째)" if change.get("occurrence") else ""
    unit = f" (단위 {change['unit']})" if change.get("unit") else ""
    prior = ", ".join(_fmt_amount(v) for v in change.get("prior_values") or []) or "-"
    current = ", ".join(_fmt_amount(v) for v in change.get("current_values") or []) or "-"
    cells = [
        f"증감 {_fmt_amount(c.get('delta'))}"
        + (f", {c['change_pct']}%" if c.get("change_pct") is not None else "")
        for c in change.get("cells") or []
    ]
    flags = sorted({f for c in change.get("cells") or [] for f in c.get("flags") or []})
    refs = "/".join(r for r in (change.get("prior_ref"), change.get("current_ref")) if r)
    return (
        f"- {_KIND_LABELS.get(change.get('kind'), change.get('kind'))} · {change.get('table')} / "
        f"{change.get('label')}{occ}{unit}: {prior} → {current}"
        + (f" ({'; '.join(cells)})" if cells else "")
        + (f" [{', '.join(flags)}]" if flags else "")
        + (f" 줄 {refs}" if refs else "")
    )


async def compare_notes(ctx: AgentContext, args: dict[str, Any]) -> ToolResult:
    prior_alias, prior = ctx.document(args["prior"])
    current_alias, current = ctx.document(args["current"])
    scope = args.get("scope", "consolidated")
    try:
        check_pair(prior, current)
    except ComparisonError as exc:
        raise ToolError(exc.message) from None
    prior_parsed = await ctx.parse(prior_alias, prior)
    current_parsed = await ctx.parse(current_alias, current)
    result = await asyncio.to_thread(
        compare_documents,
        prior_parsed.sections,
        current_parsed.sections,
        scope=scope,  # type: ignore[arg-type]
        options=diff_options(ctx.settings),
    )
    note = find_note(
        [_Named(n["key"], n["title"], n) for n in result["notes"]], args["note"]
    ).payload
    stats = note["line_stats"]
    cid = ctx.next_id("C")
    text_lines = [e for e in note.get("lines") or [] if not e.get("numbers_only")]
    shown_lines = text_lines[:COMPARE_MAX_LINES]
    changes = sorted(note.get("number_changes") or [], key=_change_rank)[:COMPARE_MAX_ROWS]
    head = (
        f"[{cid}] {prior_alias} → {current_alias} {_meta(current).get('corp_name') or ''} · "
        f"{_scope_label(scope)} {note['title']} · 상태 {note['status']} · 문장 +{stats['text_added']} / "
        f"−{stats['text_removed']} · 숫자만 바뀐 줄 {stats['numbers_only']} "
        f"(근거는 [{cid}:P번호] 전년 줄, [{cid}:C번호] 당년 줄)"
    )
    parts = [head]
    if shown_lines:
        more = len(text_lines) - len(shown_lines)
        parts.append(
            f"문장 추가·삭제 줄 {len(shown_lines)}개"
            + (f" (나머지 {more}줄 생략)" if more else "")
            + ":"
        )
        parts += [
            f"{'+' if e['op'] == 'insert' else '-'} {e['ref']} {e['text']}" for e in shown_lines
        ]
    else:
        parts.append("문장 추가·삭제 줄 없음.")
    if changes:
        total = len(note.get("number_changes") or [])
        parts.append(f"숫자 변동 상위 {len(changes)}행 (전체 {total}행):")
        parts += [_change_line(c) for c in changes]
    else:
        parts.append("숫자 변동 행 없음.")
    lines = {e["ref"]: e["text"] for e in shown_lines}
    refs = list(lines) + [
        r
        for c in changes
        for r in (c.get("prior_ref"), c.get("current_ref"))
        if r and r not in lines
    ]
    source = {
        "id": cid,
        "kind": "compare",
        **ctx.fields(current_alias),
        "prior_document": prior_alias,
        "prior_report_nm": ctx.report_label(prior_alias),
        "prior_dart_url": ctx.fields(prior_alias)["dart_url"],
        "section_path": note.get("current_section_path") or note.get("prior_section_path"),
        "note_title": note["title"],
        "scope": scope,
        "status": note["status"],
        "refs": list(dict.fromkeys(refs)),
        "lines": lines,
        "number_changes": [
            {
                "kind": c.get("kind"),
                "table": c.get("table"),
                "label": c.get("label"),
                "unit": c.get("unit"),
                "prior_values": c.get("prior_values"),
                "current_values": c.get("current_values"),
                "cells": c.get("cells"),
            }
            for c in changes
        ],
        "excerpt": "\n".join(parts[1:6])[:600],
    }
    text = "\n".join(parts)
    numbers = [v for _r, v in extract_numbers("\n".join(parts[1:]))]
    return ToolResult(ok=True, text=text, sources=[source], source_numbers={cid: numbers})


async def get_financials(ctx: AgentContext, args: dict[str, Any]) -> ToolResult:
    alias, doc = ctx.document(args["document"])
    statement = args.get("statement")
    fs_div = args.get("fs", "CFS")
    meta = _meta(doc)
    corp_code = str(meta.get("corp_code") or "")
    period = ctx.period(alias)
    code = report_code(meta.get("report_nm"), period)
    if not corp_code or period is None or code is None:
        raise ToolError(
            f"{alias}({ctx.report_label(alias)})는 OpenDART 재무제표 API로 조회할 수 없습니다"
            "(사업·반기·1분기·3분기보고서이고 보고 기간을 알 수 있어야 합니다)."
        )
    year = period[0]
    try:
        rows = await ctx.dart_client.financial_statements(corp_code, year, code, fs_div)
    except DartError as exc:
        log.warning("agent.financials_failed", corp_code=corp_code, code=exc.code)
        raise ToolError(f"OpenDART 재무제표를 가져오지 못했습니다(코드 {exc.code}).") from None
    if not rows:
        hint = " fs=OFS(별도)로 다시 시도할 수 있습니다." if fs_div == "CFS" else ""
        raise ToolError(
            f"OpenDART에 {alias}의 재무제표 자료가 없습니다(사업연도 {year}, {REPORT_CODES[code]}, "
            f"{fs_div}).{hint}"
        )
    wanted = {statement, "CIS"} if statement == "IS" else {statement}
    pool = [r for r in rows if statement is None or r.get("sj_div") in wanted]
    matched: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for query in args["accounts"]:
        needle = _compact(query)
        for row in pool:
            ident = (str(row.get("sj_div")), str(row.get("account_nm")), str(row.get("account_id")))
            if needle and needle in _compact(str(row.get("account_nm"))) and ident not in seen:
                seen.add(ident)
                matched.append(row)
    if not matched:
        names = list(dict.fromkeys(str(r.get("account_nm")) for r in pool))[:FINANCIAL_ACCOUNT_LIST]
        where = STATEMENT_DIVS.get(statement or "", "전체 재무제표")
        raise ToolError(
            f"일치하는 계정이 없습니다: {', '.join(args['accounts'])}. {where} 계정명(최대 "
            f"{FINANCIAL_ACCOUNT_LIST}개): {', '.join(names) or '없음'}"
        )
    extra = len(matched) - FINANCIAL_MAX_ROWS
    matched = matched[:FINANCIAL_MAX_ROWS]
    head = (
        f"{ctx.label(alias)} · OpenDART 재무제표 {FS_DIVS[fs_div]}({fs_div}) · 사업연도 {year} · "
        f"보고서 코드 {code} · 금액 단위 원"
    )
    lines = [head]
    sources: list[dict[str, Any]] = []
    numbers: dict[str, list[str]] = {}
    for row in matched:
        fid = ctx.next_id("F")
        current, prior = _amount(row.get("thstrm_amount")), _amount(row.get("frmtrm_amount"))
        sj = str(row.get("sj_div") or "")
        sj_nm = str(row.get("sj_nm") or STATEMENT_DIVS.get(sj, sj))
        currency = str(row.get("currency") or "KRW")
        line = (
            f"[{fid}] {row.get('account_nm')} · {sj} {sj_nm} · 당기({row.get('thstrm_nm') or '당기'}) "
            f"{_fmt_amount(current)} · 전기({row.get('frmtrm_nm') or '전기'}) {_fmt_amount(prior)} · "
            f"통화 {currency} · 단위 원"
        )
        lines.append(line)
        sources.append(
            {
                "id": fid,
                "kind": "financial",
                **ctx.fields(alias),
                "account": row.get("account_nm"),
                "statement": sj,
                "statement_name": sj_nm,
                "fs_div": fs_div,
                "bsns_year": year,
                "reprt_code": code,
                "current": {"label": row.get("thstrm_nm"), "amount": current},
                "prior": {"label": row.get("frmtrm_nm"), "amount": prior},
                "currency": currency,
                "unit": "원",
                "excerpt": line,
            }
        )
        numbers[fid] = [a.lstrip("+-") for a in (current, prior) if a]
    if extra > 0:
        lines.append(f"(일치한 나머지 {extra}행 생략: 계정명을 더 구체적으로 지정하세요)")
    return ToolResult(ok=True, text="\n".join(lines), sources=sources, source_numbers=numbers)


# ---------------------------------------------------------------- calculate


class CalcError(ToolError):
    pass


_BINOPS: dict[type, Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def _check_literal(node: ast.Constant) -> Decimal:
    value = node.value
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalcError(
            f"수식에 숫자가 아닌 값이나 소수 {value!r}를 직접 쓸 수 없습니다. 값은 values에 넣으세요."
        )
    if not (0 <= value <= 9 or value in UNIT_CONSTANTS):
        raise CalcError(
            f"수식에 직접 쓴 숫자 {value}는 허용되지 않습니다. 단위 환산 상수(10, 100, 1000, 1000000 …)와 "
            "한 자리 정수만 쓸 수 있습니다. 다른 값은 도구 결과에서 values로 넘기세요."
        )
    return Decimal(value)


def _eval_node(node: ast.AST, names: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, names)
    if isinstance(node, ast.Constant):
        return _check_literal(node)
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise CalcError(f"values에 없는 이름: {node.id}")
        return names[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        value = _eval_node(node.operand, names)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval_node(node.left, names), _eval_node(node.right, names)
        if isinstance(node.op, ast.Div) and right == 0:
            raise CalcError("0으로 나눌 수 없습니다.")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name) and node.func.id == "round") or node.keywords:
            raise CalcError("함수는 round(x, n)만 쓸 수 있습니다.")
        if not 1 <= len(node.args) <= 2:
            raise CalcError("round는 round(x) 또는 round(x, n) 형태여야 합니다.")
        value = _eval_node(node.args[0], names)
        digits = 0
        if len(node.args) == 2:
            arg = node.args[1]
            if not (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, int)
                and not isinstance(arg.value, bool)
                and 0 <= arg.value <= ROUND_MAX_DIGITS
            ):
                raise CalcError(f"round의 자릿수는 0~{ROUND_MAX_DIGITS} 정수여야 합니다.")
            digits = arg.value
        try:
            return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            raise CalcError("반올림 결과가 너무 큽니다.") from None
    if isinstance(node, ast.BinOp):
        raise CalcError("연산자는 + - * / 만 쓸 수 있습니다.")
    raise CalcError(f"허용되지 않는 수식 구문입니다: {type(node).__name__}")


def evaluate_expression(expression: str, names: dict[str, Decimal]) -> Decimal:
    """Safe ``Decimal`` evaluation of ``+ - * /``, parentheses, ``round(x, n)``, literals allowed by
    ``_check_literal`` and ``names``. Anything else (attributes, other calls, ``**``, strings,
    subscripts …) is a ``CalcError``."""
    if len(expression) > EXPRESSION_MAX_CHARS:
        raise CalcError(f"수식이 너무 깁니다(최대 {EXPRESSION_MAX_CHARS}자).")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise CalcError("수식을 해석하지 못했습니다. 예: round(a / b * 100, 1)") from None
    with localcontext() as context:
        context.prec = 34
        try:
            return +_eval_node(tree, names)
        except InvalidOperation:
            raise CalcError("계산할 수 없는 수식입니다.") from None


def resolve_value(raw: str, haystack: str) -> tuple[Decimal, str] | None:
    """``(value, matched digits)`` when ``raw`` occurs in ``haystack`` (earlier results' numbers),
    accepting comma and dot thousands separators, a sign and ``(1,234)`` negatives; else None."""
    text = raw.strip().replace("−", "-")
    m = _VALUE_RE.match(text)
    if not m:
        return None
    negative = m.group(1) == "-" or (text.startswith("(") and text.endswith(")"))
    digits = m.group(2).replace(",", "")
    candidates = [digits]
    if DOT_GROUPED_RE.fullmatch(digits):
        candidates.append(digits.replace(".", ""))
    for cand in candidates:
        if cand.count(".") > 1:
            continue
        if contains_number(haystack, cand):
            value = Decimal(cand)
            return (-value if negative else value), cand
    return None


async def calculate(ctx: AgentContext, args: dict[str, Any]) -> ToolResult:
    expression = args["expression"]
    values: dict[str, str] = args.get("values") or {}
    if len(values) > CALC_MAX_VALUES:
        raise CalcError(f"values는 최대 {CALC_MAX_VALUES}개입니다.")
    bad_names = [n for n in values if not n.isidentifier() or n == "round"]
    if bad_names:
        raise CalcError(f"values의 이름은 영문 식별자여야 합니다: {', '.join(bad_names)}")
    haystack = ctx.haystack()
    names: dict[str, Decimal] = {}
    inputs: list[dict[str, Any]] = []
    unsourced: list[str] = []
    for name, raw in values.items():
        found = resolve_value(raw, haystack)
        if found is None:
            unsourced.append(raw)
            continue
        value, digits = found
        names[name] = value
        refs = [
            sid
            for sid, nums in ctx.source_numbers.items()
            if contains_number(" ".join(nums), digits)
        ]
        inputs.append({"name": name, "value": format(value, "f"), "sources": refs})
    if unsourced:
        raise CalcError(
            f"출처 없는 값: {', '.join(unsourced)} — 앞선 도구 결과에 있는 값을 그대로 넣으세요."
        )
    result = evaluate_expression(expression, names)
    result_text = format(result, "f")
    kid = ctx.next_id("K")
    shown = ", ".join(
        f"{i['name']} = {i['value']}" + (f" ({', '.join(i['sources'])})" if i["sources"] else "")
        for i in inputs
    )
    text = f"[{kid}] {expression} = {result_text}" + (f"\n값: {shown}" if shown else "")
    source = {
        "id": kid,
        "kind": "calc",
        "expression": expression,
        "result": result_text,
        "inputs": inputs,
        "excerpt": f"{expression} = {result_text}",
    }
    return ToolResult(
        ok=True,
        text=text,
        sources=[source],
        source_numbers={kid: [result_text.lstrip("-")]},
    )


# ---------------------------------------------------------------- registry

_DOC = {"type": "string", "description": "list_documents가 준 문서 별칭(예: D1)", "minLength": 1}
_SCOPE = {
    "type": "string",
    "enum": list(SCOPES),
    "description": "consolidated = 연결재무제표 주석(기본), separate = 별도",
}

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "list_documents",
            "문서 목록",
            "조직이 가져온 준비된(ready) DART 공시 문서를 별칭(D1, D2 …), 회사명, 보고서 종류·기간, 접수일로 "
            "보여 줍니다. 다른 도구에 문서를 지정하기 전에 먼저 부르세요.",
            {"type": "object", "properties": {}, "required": []},
            list_documents,
        ),
        Tool(
            "search_filings",
            "공시 검색",
            "공시 본문(주석 포함)을 의미 검색합니다. 결과마다 근거 번호 [S#], 목차 경로, 유사도, 본문이 나옵니다.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색어(한국어)", "minLength": 1},
                    "documents": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "검색할 문서 별칭 목록(생략하면 전체)",
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": SEARCH_MAX_TOP_K,
                        "description": f"결과 수(기본 {SEARCH_DEFAULT_TOP_K}, 최대 {SEARCH_MAX_TOP_K})",
                    },
                },
                "required": ["query"],
            },
            search_filings,
        ),
        Tool(
            "read_note",
            "주석 읽기",
            "문서 하나의 재무제표 주석 하나를 줄 번호 L#와 함께 읽습니다. note는 주석 이름(예: 우발부채) 또는 "
            "번호(예: 20)입니다. 근거는 [N#:L#]로 표시합니다. 길면 offset으로 이어 읽습니다.",
            {
                "type": "object",
                "properties": {
                    "document": _DOC,
                    "note": {
                        "type": "string",
                        "description": "주석 이름 또는 번호",
                        "minLength": 1,
                    },
                    "scope": _SCOPE,
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "건너뛸 줄 수(이어 읽기, 기본 0)",
                    },
                },
                "required": ["document", "note"],
            },
            read_note,
        ),
        Tool(
            "compare_notes",
            "주석 비교",
            "같은 회사의 전년(prior)·당년(current) 보고서에서 주석 하나를 코드로 비교합니다. 상태, 추가·삭제된 "
            "문장 줄(P# 전년, C# 당년), 숫자 변동 상위 행이 나옵니다. 근거는 [C#:C번호] 또는 [C#:P번호]입니다.",
            {
                "type": "object",
                "properties": {
                    "prior": {**_DOC, "description": "전년 보고서 별칭"},
                    "current": {**_DOC, "description": "당년 보고서 별칭"},
                    "note": {
                        "type": "string",
                        "description": "주석 이름 또는 번호",
                        "minLength": 1,
                    },
                    "scope": _SCOPE,
                },
                "required": ["prior", "current", "note"],
            },
            compare_notes,
        ),
        Tool(
            "get_financials",
            "재무제표 조회",
            "OpenDART 단일회사 전체 재무제표에서 계정(예: 부채총계, 자본총계, 매출액)을 찾아 당기·전기 금액(원)을 "
            "근거 번호 [F#]와 함께 돌려줍니다. 계정명은 부분 일치입니다.",
            {
                "type": "object",
                "properties": {
                    "document": _DOC,
                    "accounts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "찾을 계정명 목록(부분 일치, 공백 무시)",
                    },
                    "statement": {
                        "type": "string",
                        "enum": ["BS", "IS", "CIS", "CF"],
                        "description": "BS 재무상태표, IS 손익계산서, CIS 포괄손익계산서, CF 현금흐름표",
                    },
                    "fs": {
                        "type": "string",
                        "enum": ["CFS", "OFS"],
                        "description": "CFS 연결(기본), OFS 별도",
                    },
                },
                "required": ["document", "accounts"],
            },
            get_financials,
        ),
        Tool(
            "calculate",
            "계산",
            "사칙연산(+ - * /), 괄호, round(x, n)만 계산합니다. values의 값은 앞선 도구 결과에 나온 숫자를 그대로 "
            "넣어야 합니다(출처 없는 값은 거부). 수식에는 한 자리 정수와 단위 상수(100, 1000, 1000000 …)만 직접 "
            "쓸 수 있습니다. 결과는 근거 번호 [K#]입니다. 예: expression 'round(a / b * 100, 1)', "
            "values {'a': '38,765,000,000', 'b': '60,000,000,000'}",
            {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "수식", "minLength": 1},
                    "values": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "이름 → 도구 결과의 숫자(문자열)",
                    },
                },
                "required": ["expression", "values"],
            },
            calculate,
        ),
    )
}


def tool_specs() -> list[dict[str, Any]]:
    return [t.spec() for t in TOOLS.values()]


def _finish(ctx: AgentContext, result: ToolResult) -> ToolResult:
    cap = max(200, ctx.settings.agent_tool_result_chars)
    if len(result.text) > cap:
        result.text = result.text[: cap - len(TRUNCATED_NOTE)] + TRUNCATED_NOTE
        seen = set(_ID_TOKEN_RE.findall(result.text))
        result.sources = [s for s in result.sources if s["id"] in seen]
    kept = {s["id"] for s in result.sources}
    result.source_numbers = {k: v for k, v in result.source_numbers.items() if k in kept}
    result.numbers = list(dict.fromkeys(v for _r, v in extract_numbers(result.text)))
    return result


async def run_tool(ctx: AgentContext, name: str, raw_args: Any) -> ToolResult:
    """Run one tool call. Never raises for a bad name, bad arguments or a tool failure: those
    come back as ``ok=False`` with a Korean ``error`` (cancellation still propagates)."""
    started = time.perf_counter()
    tool = TOOLS.get(name)
    try:
        if tool is None:
            raise ToolError(f"알 수 없는 도구: {name}. 쓸 수 있는 도구: {', '.join(TOOLS)}")
        args = validate_args(tool.parameters, raw_args)
        result = await tool.handler(ctx, args)
    except ToolError as exc:
        result = ToolResult(ok=False, text="", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - reported to the model, logged for us
        log.exception("agent.tool_crashed", tool=name, error=type(exc).__name__)
        result = ToolResult(ok=False, text="", error="도구 실행 중 예상하지 못한 오류가 났습니다.")
    if not result.ok:
        result.text = f"오류: {result.error}"
    result = _finish(ctx, result)
    ctx.register(result)
    log.info(
        "agent.tool",
        tool=name,
        ok=result.ok,
        sources=[s["id"] for s in result.sources],
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return result
