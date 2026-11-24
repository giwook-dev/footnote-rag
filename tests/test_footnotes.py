"""Pure footnote conversion and chunking tests."""

from footnote_rag.answering.footnotes import SourceRef, build_footnotes
from footnote_rag.documents.chunking import chunk_section_text, chunk_sections


def _ref(i: int) -> SourceRef:
    return SourceRef(
        source=f"doc:d{i}#chunk:c{i}",
        chunk_id=f"c{i}",
        document_id=f"d{i}",
        document_title=f"문서{i}",
        corp_name=f"회사{i}",
        section_path=f"주석 > {i}. 항목",
        dart_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={i}",
    )


def _cite(i: int, text: str = "인용") -> dict:
    return {
        "type": "search_result_location",
        "source": f"doc:d{i}#chunk:c{i}",
        "title": f"문서{i}",
        "cited_text": text,
        "search_result_index": i,
        "start_block_index": 0,
        "end_block_index": 1,
    }


SOURCES = [_ref(0), _ref(1), _ref(2)]


def test_numbers_by_first_appearance_and_reuse_for_same_chunk():
    content = [
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": "서론 "},
        {"type": "text", "text": "A 문장.", "citations": [_cite(2, "근거2")]},
        {"type": "text", "text": " B 문장.", "citations": [_cite(0, "근거0")]},
        {"type": "text", "text": " C 문장.", "citations": [_cite(2, "다른 인용")]},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
    ]
    out = build_footnotes(content, SOURCES)
    assert out.text == "서론 A 문장.[1] B 문장.[2] C 문장.[1]"
    assert [f["n"] for f in out.footnotes] == [1, 2]
    assert out.footnotes[0]["chunk_id"] == "c2" and out.footnotes[0]["cited_text"] == "근거2"
    assert out.footnotes[1]["chunk_id"] == "c0"
    assert set(out.footnotes[0]) == {
        "n",
        "document_id",
        "document_title",
        "corp_name",
        "section_path",
        "chunk_id",
        "cited_text",
        "dart_url",
    }


def test_multiple_citations_on_one_block_and_index_fallback():
    unknown_source = {**_cite(1), "source": "unknown"}  # resolved via search_result_index
    content = [{"type": "text", "text": "비교.", "citations": [_cite(0), unknown_source, _cite(0)]}]
    out = build_footnotes(content, SOURCES)
    assert out.text == "비교.[1][2]"
    assert [f["chunk_id"] for f in out.footnotes] == ["c0", "c1"]


def test_non_text_blocks_and_foreign_citations_are_ignored():
    content = [
        {"type": "tool_use", "id": "x", "name": "y", "input": {}},
        {
            "type": "text",
            "text": "문장.",
            "citations": [{"type": "char_location", "cited_text": "x"}],
        },
        {"type": "text", "text": " 끝.", "citations": [{**_cite(9), "search_result_index": 99}]},
    ]
    out = build_footnotes(content, SOURCES)
    assert out.text == "문장. 끝."
    assert out.footnotes == []


def test_chunking_respects_size_overlap_and_rows():
    rows = [f"행{i:03d} | {'값' * 40}" for i in range(100)]
    chunks = chunk_section_text("\n".join(rows), size=500, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    for c in chunks:  # tables are cut on row boundaries
        assert all(line in rows for line in c.splitlines())
    assert chunks[1].splitlines()[0] in chunks[0]  # overlap carries the tail


def test_long_line_is_windowed_and_sections_keep_paths():
    long_line = "".join(chr(0xAC00 + i % 500) for i in range(3200))
    chunks = chunk_section_text(long_line, size=1500, overlap=200)
    assert all(len(c) <= 1500 for c in chunks)
    assert chunks[0] == long_line[:1500]
    assert chunks[1].startswith(long_line[1300:1500])  # 200-char overlap
    assert long_line.endswith(chunks[-1][-100:])
    drafts = chunk_sections([("A", "짧은 글"), ("A > B", long_line)], 1500, 200)
    assert drafts[0].section_path == "A" and drafts[1].section_path == "A > B"
    assert [d.chunk_index for d in drafts] == list(range(len(drafts)))
