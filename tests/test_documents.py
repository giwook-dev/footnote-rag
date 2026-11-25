"""DART import requests, uploads and end-to-end ingestion (fake providers)."""

import uuid

import pytest
from sqlalchemy import func, select

from footnote_rag.db.models import AuditLog, Chunk, Document, Job


async def test_dart_import_creates_document_and_job_atomically(client, make_org, sessionmaker):
    org = await make_org()
    resp = await client.post(
        "/documents/dart", json={"rcept_no": "20260310900001"}, headers=org.headers("member")
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued" and body["duplicate"] is False
    async with sessionmaker() as s:
        doc = await s.get(Document, uuid.UUID(body["document_id"]))
        job = await s.get(Job, uuid.UUID(body["job_id"]))
        audit = await s.scalar(
            select(AuditLog).where(
                AuditLog.action == "document.dart_import_requested",
                AuditLog.target_id == body["document_id"],
            )
        )
    assert doc.org_id == org.org_id and doc.source == "dart" and doc.source_ref == "20260310900001"
    assert job.type == "import_dart" and job.status == "queued"
    assert job.payload["document_id"] == body["document_id"]
    assert audit is not None and audit.metadata_["job_id"] == body["job_id"]


async def test_failed_commit_leaves_neither_document_nor_job(make_org, sessionmaker, monkeypatch):
    from footnote_rag.db.models import User
    from footnote_rag.documents import service

    org = await make_org()

    def broken_record(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(service, "record", broken_record)
    async with sessionmaker() as s:
        user = await s.get(User, org.admin_id)
        with pytest.raises(RuntimeError):
            await service.request_dart_import(s, user=user, rcept_no="20260310111111")
        await s.rollback()
    async with sessionmaker() as s:
        assert (
            await s.scalar(select(Document).where(Document.source_ref == "20260310111111")) is None
        )
        assert (await s.scalar(select(func.count()).select_from(Job))) == 0


async def test_concurrent_duplicate_imports_create_one_document(make_org, sessionmaker):
    import asyncio

    from footnote_rag.db.models import User
    from footnote_rag.documents.service import request_dart_import

    org = await make_org()

    async def attempt():
        async with sessionmaker() as s:
            user = await s.get(User, org.admin_id)
            r = await request_dart_import(s, user=user, rcept_no="20260310222222")
            return r.document.id, r.existing

    results = await asyncio.gather(*(attempt() for _ in range(4)))
    assert len({doc_id for doc_id, _ in results}) == 1
    assert sum(1 for _, existing in results if not existing) == 1
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.parametrize("rcept_no", ["123", "2026031090000A", "202603109000011", ""])
async def test_rcept_no_must_be_14_digits(client, make_org, rcept_no):
    org = await make_org()
    resp = await client.post("/documents/dart", json={"rcept_no": rcept_no}, headers=org.headers())
    assert resp.status_code == 422


async def test_duplicate_import_returns_existing_document(client, make_org, sessionmaker):
    org = await make_org()
    first = await client.post(
        "/documents/dart", json={"rcept_no": "20260310900002"}, headers=org.headers()
    )
    second = await client.post(
        "/documents/dart", json={"rcept_no": "20260310900002"}, headers=org.headers("member")
    )
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["document_id"] == first.json()["document_id"]
    assert second.json()["job_id"] == first.json()["job_id"]
    async with sessionmaker() as s:
        n_docs = await s.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.org_id == org.org_id, Document.source_ref == "20260310900002")
        )
        n_jobs = await s.scalar(select(func.count()).select_from(Job))
    assert n_docs == 1 and n_jobs == 1

    # Another organization may import the same filing independently.
    other = await make_org()
    resp = await client.post(
        "/documents/dart", json={"rcept_no": "20260310900002"}, headers=other.headers()
    )
    assert resp.status_code == 202 and resp.json()["document_id"] != first.json()["document_id"]


async def test_import_end_to_end_sets_meta_and_chunks(client, make_org, ingest, sessionmaker):
    org = await make_org()
    doc_id = await ingest(org, "20260310900001")
    resp = await client.get(f"/documents/{doc_id}", headers=org.headers("member"))
    doc = resp.json()
    assert doc["status"] == "ready", doc
    assert doc["title"] == "가람전자(합성) 감사보고서"
    assert doc["meta"]["corp_name"] == "가람전자(합성)"
    assert doc["meta"]["corp_code"] == "99000001"
    assert doc["meta"]["stock_code"] == "990001"
    assert doc["meta"]["rcept_dt"] == "20260310"
    assert doc["meta"]["dart_url"].endswith("rcpNo=20260310900001")
    assert doc["section_count"] >= 4
    async with sessionmaker() as s:
        paths = set(
            await s.scalars(
                select(Chunk.section_path).where(Chunk.document_id == uuid.UUID(doc_id))
            )
        )
        job = await s.scalar(select(Job).where(Job.payload["document_id"].astext == doc_id))
        audit = await s.scalar(
            select(AuditLog).where(
                AuditLog.action == "document.ingest_succeeded", AuditLog.target_id == doc_id
            )
        )
    assert "(첨부)재무제표 > 주석 > 12. 리스" in paths
    assert job.status == "succeeded" and job.result["embedding_tokens"] > 0
    assert audit.metadata_["embedding_tokens"] == job.result["embedding_tokens"]
    assert audit.metadata_["embedding_model"] == "fake-hash-embedding"

    resp = await client.get(f"/jobs/{job.id}", headers=org.headers("member"))
    assert resp.status_code == 200 and resp.json()["status"] == "succeeded"
    other = await make_org()
    assert (await client.get(f"/jobs/{job.id}", headers=other.headers())).status_code == 404

    listing = await client.get("/documents", headers=org.headers("member"))
    assert [d["id"] for d in listing.json()] == [doc_id]


async def test_zip_is_cached_in_data_dir(make_org, ingest, fake_dart, settings):
    org = await make_org()
    await ingest(org, "20260310900002")
    # Synthetic downloads are cached apart from real ones (data/dart/fake/).
    assert (settings.dart_dir / "fake" / "20260310900002.zip").exists()
    assert not (settings.dart_dir / "20260310900002.zip").exists()
    other = await make_org()
    await ingest(other, "20260310900002")
    assert fake_dart.download_calls.count("20260310900002") <= 1


async def test_markdown_upload_uses_headings_as_section_path(
    client, make_org, worker, sessionmaker
):
    org = await make_org()
    content = "# 정책\n개요 문장\n## 리스\n리스 회계처리 설명\n".encode()
    resp = await client.post(
        "/documents/upload",
        files={"file": ("policy.md", content, "text/markdown")},
        headers=org.headers("member"),
    )
    assert resp.status_code == 202, resp.text
    while await worker.run_once():
        pass
    doc_id = uuid.UUID(resp.json()["document_id"])
    async with sessionmaker() as s:
        doc = await s.get(Document, doc_id)
        paths = list(
            await s.scalars(
                select(Chunk.section_path)
                .where(Chunk.document_id == doc_id)
                .order_by(Chunk.chunk_index)  # document order; without it the heap order decides
            )
        )
        audit = await s.scalar(
            select(AuditLog).where(
                AuditLog.action == "document.text_uploaded", AuditLog.target_id == str(doc_id)
            )
        )
    assert doc.status == "ready" and doc.source == "upload" and doc.title == "policy"
    assert paths == ["정책", "정책 > 리스"]
    assert audit.metadata_["filename"] == "policy.md" and "text" not in audit.metadata_


async def test_plain_text_without_headings_uses_filename(client, make_org, worker, sessionmaker):
    org = await make_org()
    resp = await client.post(
        "/documents/upload",
        files={"file": ("memo.txt", "그냥 텍스트".encode(), "text/plain")},
        headers=org.headers("member"),
    )
    while await worker.run_once():
        pass
    async with sessionmaker() as s:
        paths = list(
            await s.scalars(
                select(Chunk.section_path).where(
                    Chunk.document_id == uuid.UUID(resp.json()["document_id"])
                )
            )
        )
    assert paths == ["memo.txt"]


async def test_upload_rejects_other_types_and_large_files(client, make_org, settings, monkeypatch):
    org = await make_org()
    resp = await client.post(
        "/documents/upload",
        files={"file": ("report.pdf", b"%PDF-1.7", "application/pdf")},
        headers=org.headers("member"),
    )
    assert resp.status_code == 415
    monkeypatch.setattr(settings, "upload_max_bytes", 10)
    resp = await client.post(
        "/documents/upload",
        files={"file": ("big.txt", b"x" * 11, "text/plain")},
        headers=org.headers("member"),
    )
    assert resp.status_code == 413
