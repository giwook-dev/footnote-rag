"""Consolidated-first policy (docs/adr/0009): note scope/key, retrieval reordering, prompts."""

from __future__ import annotations

import math
import uuid

import pytest

from footnote_rag.answering.llm import LLMResponse, LLMUsage
from footnote_rag.answering.local_llm import OllamaClient
from footnote_rag.answering.prompt import (
    LOCAL_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_messages,
    source_title,
)
from footnote_rag.answering.service import retrieve, retrieve_ranked, review_reasons
from footnote_rag.dart.sections import (
    is_counterpart,
    labeled_section_path,
    note_key,
    scope_label,
    statement_scope,
)
from footnote_rag.db.models import Chunk, Document, User
from footnote_rag.retrieval.embedder import EmbeddingResult
from footnote_rag.retrieval.search import (
    RetrievedChunk,
    prefer_statement_scope,
    preferred_scope,
    search_chunks,
)

# Real section paths of the 2025 annual reports (Samsung, NAVER, Hyundai) from the Eval report.
CONS = "III. 재무에 관한 사항 > 3. 연결재무제표 주석 > "
SEP = "III. 재무에 관한 사항 > 5. 재무제표 주석 > "
SAMSUNG_BORROWINGS_CONS = CONS + "12. 차입금 (연결)"
SAMSUNG_BORROWINGS_SEP = SEP + "12. 차입금"
HYUNDAI_RELATED_CONS = CONS + "35. 특수관계자 등과의 거래 (연결)"
HYUNDAI_RELATED_SEP = SEP + "33. 특수관계자 등과의 거래"
HYUNDAI_POLICY_CONS = CONS + "2. 연결재무제표 작성기준 및 중요한 회계정책 (연결)"
HYUNDAI_POLICY_SEP = SEP + "2. 재무제표 작성기준 및 중요한 회계정책"
NAVER_POLICY_CONS = CONS + "2. 중요한 회계정책 (연결)"
NAVER_POLICY_SEP = SEP + "2. 중요한 회계정책"
NAVER_INSTRUMENTS_CONS = CONS + "6. 범주별 금융상품 (연결)"
NAVER_CONTINGENT_CONS = CONS + "32. 우발채무 및 약정사항 (연결)"
NAVER_CONTINGENT_SEP = SEP + "31. 우발채무 및 약정사항"
BUSINESS = "II. 사업의 내용 > 1. 사업의 개요"


# ---- dart.sections ----


@pytest.mark.parametrize(
    ("path", "scope", "key"),
    [
        (SAMSUNG_BORROWINGS_CONS, "consolidated", "차입금"),
        (SAMSUNG_BORROWINGS_SEP, "separate", "차입금"),
        (HYUNDAI_RELATED_CONS, "consolidated", "특수관계자등과의거래"),
        (HYUNDAI_RELATED_SEP, "separate", "특수관계자등과의거래"),
        (HYUNDAI_POLICY_CONS, "consolidated", "재무제표작성기준및중요한회계정책"),
        (HYUNDAI_POLICY_SEP, "separate", "재무제표작성기준및중요한회계정책"),
        (NAVER_POLICY_CONS, "consolidated", "중요한회계정책"),
        (NAVER_POLICY_SEP, "separate", "중요한회계정책"),
        # sub-headings inside a note use the note heading's key
        (SAMSUNG_BORROWINGS_CONS + " > 가. 단기차입금", "consolidated", "차입금"),
        # audit reports: a bare "주석" under the statements heading
        ("(첨부)재무제표 > 주석 > 12. 리스", "separate", "리스"),
        ("(첨부)연결재무제표 > 주석 > 12. 리스 (연결)", "consolidated", "리스"),
    ],
)
def test_statement_scope_and_note_key_on_real_paths(path, scope, key):
    assert statement_scope(path) == scope
    assert note_key(path) == key


@pytest.mark.parametrize(
    "path",
    [
        BUSINESS,
        "XI. 그 밖에 투자자 보호를 위하여 필요한 사항 > 2. 우발부채 등에 관한 사항",
        "III. 재무에 관한 사항 > 2. 연결재무제표",  # the statements, not the notes
        "XII. 상세표 > 3. 타법인출자 현황(상세)",
        "주석 > 12. 리스",  # no statements heading above "주석"
        "",
    ],
)
def test_paths_outside_the_notes_are_none(path):
    assert statement_scope(path) is None
    assert note_key(path) is None
    assert scope_label(path) == ""
    assert labeled_section_path(path) == path


def test_notes_heading_itself_has_a_scope_but_no_note_key():
    assert statement_scope("III. 재무에 관한 사항 > 3. 연결재무제표 주석") == "consolidated"
    assert note_key("III. 재무에 관한 사항 > 3. 연결재무제표 주석") is None


def test_counterparts_across_numbering_and_title_wording():
    # same number and title
    assert is_counterpart(SAMSUNG_BORROWINGS_CONS, SAMSUNG_BORROWINGS_SEP)
    assert is_counterpart(SAMSUNG_BORROWINGS_SEP, SAMSUNG_BORROWINGS_CONS)
    # Hyundai: note 35 (consolidated) vs note 33 (separate)
    assert is_counterpart(HYUNDAI_RELATED_CONS, HYUNDAI_RELATED_SEP)
    # "연결재무제표 작성기준 … (연결)" vs "재무제표 작성기준 …"
    assert is_counterpart(HYUNDAI_POLICY_CONS, HYUNDAI_POLICY_SEP)
    assert is_counterpart(NAVER_POLICY_CONS, NAVER_POLICY_SEP)
    assert is_counterpart(NAVER_CONTINGENT_CONS, NAVER_CONTINGENT_SEP)


def test_not_counterparts():
    # same scope
    assert not is_counterpart(
        SAMSUNG_BORROWINGS_CONS, SAMSUNG_BORROWINGS_CONS + " > 가. 단기차입금"
    )
    assert not is_counterpart(SAMSUNG_BORROWINGS_SEP, SAMSUNG_BORROWINGS_SEP)
    # different notes (the real errors of the 2026-11-25 run)
    assert not is_counterpart(NAVER_INSTRUMENTS_CONS, NAVER_CONTINGENT_SEP)
    assert not is_counterpart(HYUNDAI_POLICY_SEP, HYUNDAI_RELATED_CONS)
    # outside the notes
    assert not is_counterpart(BUSINESS, NAVER_POLICY_CONS)
    assert not is_counterpart(BUSINESS, BUSINESS)


def test_scope_labels():
    assert scope_label(SAMSUNG_BORROWINGS_CONS) == "[연결]"
    assert scope_label(SAMSUNG_BORROWINGS_SEP) == "[별도]"
    assert labeled_section_path(SAMSUNG_BORROWINGS_CONS) == f"[연결] {SAMSUNG_BORROWINGS_CONS}"


# ---- retrieval.search.prefer_statement_scope (pure) ----

DOC_A, DOC_B = uuid.uuid4(), uuid.uuid4()


def _chunk(path: str, score: float, doc: uuid.UUID = DOC_A, title: str = "문서") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=doc,
        document_title=title,
        document_meta={},
        source="dart",
        source_ref=None,
        section_path=path,
        chunk_index=0,
        text=f"본문 {path}",
        score=score,
    )


def _paths(chunks: list[RetrievedChunk]) -> list[tuple[str, float]]:
    return [(c.section_path, c.score) for c in chunks]


def test_consolidated_counterpart_moves_ahead_and_other_order_is_kept():
    candidates = [
        _chunk(BUSINESS, 0.95),
        _chunk(SAMSUNG_BORROWINGS_SEP, 0.90),  # has a consolidated counterpart -> demoted
        _chunk(NAVER_INSTRUMENTS_CONS, 0.85),
        _chunk(SAMSUNG_BORROWINGS_CONS, 0.80),
        _chunk(SEP + "14. 리스", 0.75),  # separate, no counterpart -> stays
        _chunk(HYUNDAI_RELATED_SEP, 0.72),  # paired with the note-35 chunk below (lower score)
        _chunk(HYUNDAI_RELATED_CONS, 0.70),
        _chunk(SAMSUNG_BORROWINGS_SEP, 0.65, doc=DOC_B),  # other document: no pair -> stays
    ]
    out = prefer_statement_scope(candidates, query="차입금 내역은?", top_k=8)
    assert _paths(out) == [
        (BUSINESS, 0.95),
        (NAVER_INSTRUMENTS_CONS, 0.85),
        (SAMSUNG_BORROWINGS_CONS, 0.80),
        (SEP + "14. 리스", 0.75),
        (HYUNDAI_RELATED_CONS, 0.70),
        (SAMSUNG_BORROWINGS_SEP, 0.65),
        # demoted, still included while slots remain, in score order
        (SAMSUNG_BORROWINGS_SEP, 0.90),
        (HYUNDAI_RELATED_SEP, 0.72),
    ]
    assert out[-1].document_id == DOC_A and out[5].document_id == DOC_B
    # fewer slots: demoted chunks are the first to go
    short = prefer_statement_scope(candidates, query="차입금 내역은?", top_k=3)
    assert _paths(short) == [
        (BUSINESS, 0.95),
        (NAVER_INSTRUMENTS_CONS, 0.85),
        (SAMSUNG_BORROWINGS_CONS, 0.80),
    ]


def test_no_counterparts_means_plain_score_order():
    candidates = [_chunk(BUSINESS, 0.9), _chunk(SEP + "14. 리스", 0.8), _chunk(CONS + "9. X", 0.7)]
    assert prefer_statement_scope(candidates, query="q", top_k=2) == candidates[:2]


@pytest.mark.parametrize("query", ["별도재무제표 기준 차입금은?", "개별 기준 차입금은?"])
def test_separate_question_prefers_separate(query):
    assert preferred_scope(query) == "separate"
    candidates = [
        _chunk(SAMSUNG_BORROWINGS_CONS, 0.9),
        _chunk(BUSINESS, 0.8),
        _chunk(SAMSUNG_BORROWINGS_SEP, 0.7),
    ]
    out = prefer_statement_scope(candidates, query=query, top_k=3)
    assert _paths(out) == [
        (BUSINESS, 0.8),
        (SAMSUNG_BORROWINGS_SEP, 0.7),
        (SAMSUNG_BORROWINGS_CONS, 0.9),
    ]


def test_default_question_prefers_consolidated():
    assert preferred_scope("차입금 내역은?") == "consolidated"
    assert preferred_scope("연결 기준 차입금은?") == "consolidated"


# ---- retrieve() against Postgres ----


def _axis_vector(dim: int, score: float) -> list[float]:
    """Unit vector whose cosine similarity with the query axis (e0) is ``score``."""
    vec = [0.0] * dim
    vec[0], vec[1] = score, math.sqrt(1 - score * score)
    return vec


class _AxisEmbedder:
    model = "axis"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed_query(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(vectors=[_axis_vector(self.dim, 1.0)], total_tokens=1)


async def _add_document(sessionmaker, org, dim: int, chunks: list[tuple[str, float]]):
    async with sessionmaker() as session:
        doc = Document(
            id=uuid.uuid4(),
            org_id=org.org_id,
            created_by=org.admin_id,
            title="삼성전자 사업보고서",
            source="upload",
            source_ref=uuid.uuid4().hex,
            meta={},
            status="ready",
        )
        session.add(doc)
        await session.flush()
        for i, (path, score) in enumerate(chunks):
            session.add(
                Chunk(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    org_id=org.org_id,
                    section_path=path,
                    chunk_index=i,
                    text=f"본문 {i}",
                    embedding=_axis_vector(dim, score),
                )
            )
        await session.commit()
        return doc.id


async def test_retrieve_reorders_scopes_and_keeps_org_isolation(make_org, sessionmaker, settings):
    org, other = await make_org(), await make_org()
    dim = settings.embedding_dim
    doc = await _add_document(
        sessionmaker,
        org,
        dim,
        [(SAMSUNG_BORROWINGS_SEP, 0.9), (BUSINESS, 0.8), (SAMSUNG_BORROWINGS_CONS, 0.7)],
    )
    # another org's chunks score higher and must never appear
    await _add_document(
        sessionmaker, other, dim, [(SAMSUNG_BORROWINGS_CONS, 0.99), (BUSINESS, 0.98)]
    )
    embedder = _AxisEmbedder(dim)
    on = settings.model_copy(update={"prefer_consolidated": True, "retrieval_candidates": 16})
    off = settings.model_copy(update={"prefer_consolidated": False})

    async with sessionmaker() as session:
        user = await session.get(User, org.admin_id)
        kwargs = {"user": user, "top_k": 3, "document_ids": None, "embedder": embedder}
        ranked = await retrieve_ranked(session, query="차입금은?", settings=on, **kwargs)
        plain = await retrieve(session, query="차입금은?", settings=off, **kwargs)
        separate = await retrieve(session, query="별도 기준 차입금은?", settings=on, **kwargs)
        baseline = await search_chunks(
            session, org_id=org.org_id, query_vector=_axis_vector(dim, 1.0), top_k=3
        )
        top1 = await retrieve(session, query="차입금은?", settings=on, **{**kwargs, "top_k": 2})

    assert {c.document_id for c in ranked.chunks + plain + separate} == {doc}
    assert [c.section_path for c in ranked.chunks] == [
        BUSINESS,
        SAMSUNG_BORROWINGS_CONS,
        SAMSUNG_BORROWINGS_SEP,
    ]
    # confidence rule input: the best candidate similarity before reordering
    assert ranked.top_score == pytest.approx(0.9, abs=1e-4)
    # PREFER_CONSOLIDATED=false is the plain top-k search
    assert [(c.chunk_id, c.score) for c in plain] == [(c.chunk_id, c.score) for c in baseline]
    assert [c.section_path for c in plain] == [
        SAMSUNG_BORROWINGS_SEP,
        BUSINESS,
        SAMSUNG_BORROWINGS_CONS,
    ]
    assert [c.section_path for c in separate] == [
        SAMSUNG_BORROWINGS_SEP,
        BUSINESS,
        SAMSUNG_BORROWINGS_CONS,
    ]
    # a lower-ranked consolidated note replaces its separate counterpart in a short top-k
    assert [c.section_path for c in top1] == [BUSINESS, SAMSUNG_BORROWINGS_CONS]


async def test_search_endpoint_returns_reordered_order(
    client, make_org, sessionmaker, settings, app
):
    org, other = await make_org(), await make_org()
    dim = settings.embedding_dim
    await _add_document(
        sessionmaker, org, dim, [(HYUNDAI_RELATED_SEP, 0.9), (HYUNDAI_RELATED_CONS, 0.7)]
    )
    await _add_document(sessionmaker, other, dim, [(HYUNDAI_RELATED_CONS, 0.99)])
    app.state.embedder = _AxisEmbedder(dim)
    hits = (
        await client.post(
            "/search", json={"query": "특수관계자 거래", "k": 5}, headers=org.headers("member")
        )
    ).json()
    assert [h["section_path"] for h in hits] == [HYUNDAI_RELATED_CONS, HYUNDAI_RELATED_SEP]
    assert [h["score"] for h in hits] == [
        pytest.approx(0.7, abs=1e-3),
        pytest.approx(0.9, abs=1e-3),
    ]


def test_low_score_rule_uses_candidate_top_score(settings):
    low = _chunk(SAMSUNG_BORROWINGS_CONS, -0.5)
    ok = LLMResponse(content=[], stop_reason="end_turn", model="m", usage=LLMUsage(), latency_ms=1)
    kwargs = {"retrieved": [low], "llm_response": ok, "footnote_count": 1, "settings": settings}
    assert review_reasons(**kwargs) == ["low_retrieval_score"]
    assert review_reasons(**kwargs, top_score=0.9) == []
    assert review_reasons(**kwargs, top_score=-0.5) == ["low_retrieval_score"]


# ---- prompts ----

RULE = "질문이 별도재무제표를 지정하지 않는 한 연결재무제표 주석"


def test_both_system_prompts_prefer_consolidated_notes():
    for prompt in (SYSTEM_PROMPT, LOCAL_SYSTEM_PROMPT):
        assert "연결재무제표 주석과 (별도)재무제표 주석에 모두 있으면" in prompt
        assert RULE in prompt
        assert "[연결]" in prompt and "[별도]" in prompt
        assert "{" not in prompt.split("출력은")[0]  # fixed text, no per-request placeholders


def _prompt_chunks() -> list[RetrievedChunk]:
    return [
        _chunk(SAMSUNG_BORROWINGS_SEP, 0.9, title="삼성전자 사업보고서"),
        _chunk(SAMSUNG_BORROWINGS_CONS, 0.8, title="삼성전자 사업보고서"),
        _chunk(BUSINESS, 0.7, title="삼성전자 사업보고서"),
    ]


def test_search_result_titles_carry_scope_labels():
    chunks = _prompt_chunks()
    blocks = build_messages("차입금은?", chunks)[0]["content"]
    titles = [b["title"] for b in blocks if b["type"] == "search_result"]
    assert titles == [
        f"삼성전자 사업보고서 · [별도] {SAMSUNG_BORROWINGS_SEP}",
        f"삼성전자 사업보고서 · [연결] {SAMSUNG_BORROWINGS_CONS}",
        f"삼성전자 사업보고서 · {BUSINESS}",
    ]
    assert titles == [source_title(c) for c in chunks]


def test_local_prompt_source_headers_carry_scope_labels():
    chat = OllamaClient().chat_messages(build_messages("차입금은?", _prompt_chunks()))
    assert chat[0] == {"role": "system", "content": LOCAL_SYSTEM_PROMPT}
    user = chat[1]["content"]
    assert f"[1] 삼성전자 사업보고서 · [별도] {SAMSUNG_BORROWINGS_SEP}\n" in user
    assert f"[2] 삼성전자 사업보고서 · [연결] {SAMSUNG_BORROWINGS_CONS}\n" in user
    assert f"[3] 삼성전자 사업보고서 · {BUSINESS}\n" in user
