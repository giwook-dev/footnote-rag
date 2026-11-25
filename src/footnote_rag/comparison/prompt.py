"""주석 변동 요약 프롬프트(로컬 모델용)와 요약 검증.

역할: 코드가 찾은 변동(줄 비교와 숫자 변동 표)만 프롬프트 자료로 만들고, 모델이 돌려준 요약 항목의 근거 줄 번호와
숫자를 그 자료에 비춰 검증한다. 모델은 계산하지 않는다: 숫자는 자료에 있는 값만 쓰게 하고, 자료에 없는 숫자는
``ungrounded_numbers``로 기록해 "확인 필요"(``needs_review``)로 표시한다.
주요 요소: ``SUMMARY_SYSTEM_PROMPT``, ``SUMMARY_SCHEMA``, ``build_summary_prompt``, ``SummaryPrompt``,
``check_bullets``, ``is_grounded_number``, ``CheckedSummary``, ``is_summary_shape``.
호출 관계: ``comparison.service.summarize_note``가 호출한다. 숫자 검사는 ``answering.number_check``의
``extract_numbers``/``contains_number``(점 천 단위 표기 ``39.012`` 인정 포함)를 그대로 쓴다.
학습 문서: docs/study/11-note-change-analysis.md, docs/adr/0012-note-change-analysis.md
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from footnote_rag.answering.number_check import DOT_GROUPED_RE, contains_number, extract_numbers

MAX_LINE_CHARS = 6000
MAX_NUMBER_ROWS = 30

SUMMARY_SYSTEM_PROMPT = """당신은 감사인의 "전기 대비 변동 검토"를 돕는 보조자입니다.
사용자 메시지에는 같은 회사의 전년·당년 사업보고서에서 한 재무제표 주석의 변동 자료가 있습니다.
자료는 코드가 계산했습니다. 당신은 계산하지 않고 요약만 합니다.

규칙:
- 변동을 3~5개 항목으로 요약한다.
- 숫자는 주어진 값만 그대로 쓰고, 계산·환산하지 않는다(합계·차이·비율·단위 변환 금지).
- 각 항목에 근거 줄 번호를 단다. 근거 줄 번호는 자료의 [P12], [C15] 같은 번호만 refs 배열에 넣는다(대괄호 없이 "P12", "C15").
- 판단·평가(예: 중요하다, 위험하다, 긍정적이다)는 하지 않는다. 무엇이 바뀌었는지만 쓴다.
- 한국어로 쓴다.
- 출력은 {"bullets": [{"text": "...", "refs": ["C15"]}]} 형식의 JSON 하나뿐이다."""

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "refs"],
            },
        }
    },
    "required": ["bullets"],
}

STATUS_LABELS = {
    "added": "신규 주석",
    "removed": "삭제된 주석",
    "changed": "변경",
    "unchanged": "변동 없음",
}
KIND_LABELS = {
    "yoy": "전년 대비",
    "prior_mismatch": "전기 수치 불일치(당년 보고서의 전기 vs 전년 보고서의 당기)",
    "row_added": "행 추가",
    "row_removed": "행 삭제",
}
FLAG_LABELS = {"large_change": "큰 변동", "prior_mismatch": "전기 수치 불일치"}


@dataclass
class SummaryPrompt:
    user: str
    evidence: str  # the data part of ``user``: numbers in a summary must come from here
    refs: frozenset[str]  # line numbers actually put in the prompt
    lines_included: int = 0
    number_rows_included: int = 0


def _line_order(lines: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sentence changes first, then numbers-only lines (document order inside each group)."""
    return [e for e in lines if not e.get("numbers_only")] + [
        e for e in lines if e.get("numbers_only")
    ]


def _number_rows(changes: Sequence[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for change in changes:
        kind = KIND_LABELS.get(change["kind"], change["kind"])
        refs = " ".join(f"[{r}]" for r in (change.get("prior_ref"), change.get("current_ref")) if r)
        label = change.get("label") or "-"
        table = change.get("table") or "-"
        cells = change.get("cells") or []
        if cells:
            many = len(change.get("current_values") or []) > 1
            for cell in cells:
                flags = ", ".join(FLAG_LABELS.get(f, f) for f in cell.get("flags") or [])
                if change["kind"] == "prior_mismatch":
                    flags = ", ".join(x for x in ("전기 수치 불일치", flags) if x)
                name = f"{label} ({cell['position']}열)" if many else label
                pct = f"{cell['change_pct']}%" if cell.get("change_pct") is not None else "-"
                rows.append(
                    f"| {table} | {name} | {cell['prior']} | {cell['current']} | {cell['delta']} "
                    f"| {pct} | {flags or kind} | {refs} |"
                )
        else:
            prior = ", ".join(change.get("prior_values") or []) or "-"
            current = ", ".join(change.get("current_values") or []) or "-"
            rows.append(f"| {table} | {label} | {prior} | {current} | - | - | {kind} | {refs} |")
    return rows


def build_summary_prompt(
    note: dict[str, Any], *, prior_report: str, current_report: str
) -> SummaryPrompt:
    """Prompt for one note of a stored comparison result (``comparison.notes`` note dict)."""
    header = [
        f"주석: {note.get('title') or note.get('key')}",
        f"전년 보고서: {prior_report}",
        f"당년 보고서: {current_report}",
        f"주석 상태: {STATUS_LABELS.get(note.get('status', ''), note.get('status', ''))}",
    ]
    line_parts: list[str] = []
    refs: set[str] = set()
    used = 0
    for entry in _line_order(note.get("lines") or []):
        sign = "삭제" if entry["op"] == "delete" else "추가"
        text = f"[{entry['ref']}] ({sign}) {entry['text']}"
        if used + len(text) + 1 > MAX_LINE_CHARS:
            break
        line_parts.append(text)
        refs.add(entry["ref"])
        used += len(text) + 1
    rows = _number_rows(note.get("number_changes") or [])[:MAX_NUMBER_ROWS]
    for change in note.get("number_changes") or []:
        for ref in (change.get("prior_ref"), change.get("current_ref")):
            if ref and any(f"[{ref}]" in row for row in rows):
                refs.add(ref)
    sections = [*header, "", "## 바뀐 줄 (P = 전년 보고서 줄, C = 당년 보고서 줄)"]
    sections += line_parts or ["(없음)"]
    sections += [
        "",
        "## 숫자 변동 (코드 계산)",
        "| 표 | 행 | 전년 | 당년 | 증감 | 증감률 | 플래그 | 근거 줄 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    sections += rows or ["(없음)"]
    evidence = "\n".join(sections)
    instructions = (
        "\n\n## 지시\n"
        "위 변동을 3~5개 항목으로 요약하세요. 숫자는 위에 있는 값만 그대로 쓰고 계산·환산하지 마세요. "
        "각 항목의 refs에 근거 줄 번호(P12, C15 등)를 넣으세요. 판단·평가는 하지 마세요."
    )
    return SummaryPrompt(
        user=evidence + instructions,
        evidence=evidence,
        refs=frozenset(refs),
        lines_included=len(line_parts),
        number_rows_included=len(rows),
    )


def is_summary_shape(data: dict[str, Any]) -> bool:
    """``{"bullets": [{"text": str, "refs": [str, ...]}, ...]}`` (extra keys ignored)."""
    bullets = data.get("bullets")
    if not isinstance(bullets, list):
        return False
    return all(
        isinstance(b, dict)
        and isinstance(b.get("text"), str)
        and isinstance(b.get("refs"), list)
        and all(isinstance(r, str) for r in b["refs"])
        for b in bullets
    )


@dataclass
class CheckedSummary:
    bullets: list[dict[str, Any]] = field(default_factory=list)
    ungrounded_numbers: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.bullets:
            return "needs_review"
        return "ok" if all(b["grounded"] for b in self.bullets) else "needs_review"


def _clean_ref(ref: str) -> str:
    return ref.strip().strip("[]").strip().upper()


def is_grounded_number(haystack: str, value: str) -> bool:
    """``contains_number`` (the data may write ``39.012`` for ``39,012``), and the other way round:
    a summary number written with dot thousands separators (``39.012``) also matches the data's
    ``39,012``. Same known limitation as ``number_check``: a decimal ``3.141`` matches ``3141``."""
    if contains_number(haystack, value):
        return True
    return DOT_GROUPED_RE.fullmatch(value) is not None and contains_number(
        haystack, value.replace(".", "")
    )


def check_bullets(data: dict[str, Any], prompt: SummaryPrompt) -> CheckedSummary:
    """Keep only refs that were in the prompt; flag bullets with a number the prompt data does not
    contain (``contains_number``: commas ignored, ``39.012`` grounds ``39,012``) or no valid ref."""
    haystack = prompt.evidence.replace(",", "")
    out = CheckedSummary()
    for raw in data.get("bullets") or []:
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        refs = list(dict.fromkeys(_clean_ref(str(r)) for r in raw.get("refs") or []))
        valid = [r for r in refs if r in prompt.refs]
        dropped = [r for r in refs if r not in prompt.refs]
        missing = list(
            dict.fromkeys(
                written
                for written, value in extract_numbers(text)
                if not is_grounded_number(haystack, value)
            )
        )
        out.bullets.append(
            {
                "text": text,
                "refs": valid,
                "dropped_refs": dropped,
                "ungrounded_numbers": missing,
                "grounded": bool(valid) and not missing,
            }
        )
        out.ungrounded_numbers += [n for n in missing if n not in out.ungrounded_numbers]
    return out
