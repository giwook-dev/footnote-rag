"""POST /documents/{id}/reprocess and ``cli dart-reprocess`` (real Postgres, fake providers)."""

import uuid

import pytest
from sqlalchemy import func, select, update

from footnote_rag.db.models import AuditLog, Chunk, Document, Job

ANNUAL_RCEPT_NO = "20260310900101"  # synthetic A001 annual report (dart/fake_data.py)
NOTES = "III. 재무에 관한 사항 > 3. 연결재무제표 주석"


async def _set_status(sessionmaker, doc_id: str, status: str, error: str | None = None) -> None:
    async with sessionmaker() as s:
        await s.execute(
            update(Document)
            .where(Document.id == uuid.UUID(doc_id))
            .values(status=status, error=error)
        )
        await s.commit()


async def _jobs_for(sessionmaker, doc_id: str) -> list[Job]:
    async with sessionmaker() as s:
        return list(
            await s.scalars(
                select(Job)
                .where(Job.payload["document_id"].astext == doc_id)
                .order_by(Job.created_at)
            )
        )


async def test_admin_reprocess_queues_new_job_and_audits(client, make_org, ingest, sessionmaker):
    org = await make_org()
    doc_id = await ingest(org, ANNUAL_RCEPT_NO)
    await _set_status(sessionmaker, doc_id, "failed", "parse failed: old parser")
    before = await _jobs_for(sessionmaker, doc_id)

    resp = await client.post(f"/documents/{doc_id}/reprocess", headers=org.headers("admin"))
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["document_id"] == doc_id
    job_id = body["job_id"]
    assert job_id not in {str(j.id) for j in before}

    after = await _jobs_for(sessionmaker, doc_id)
    new_job = next(j for j in after if str(j.id) == job_id)
    assert new_job.type == "import_dart" and new_job.status == "queued"
    assert new_job.payload == {
        "document_id": doc_id,
        "org_id": str(org.org_id),
        "rcept_no": ANNUAL_RCEPT_NO,
    }
    async with sessionmaker() as s:
        doc = await s.get(Document, uuid.UUID(doc_id))
        audit = await s.scalar(
            select(AuditLog).where(
                AuditLog.action == "document.reprocess_requested", AuditLog.target_id == doc_id
            )
        )
    assert doc.status == "queued" and doc.error is None
    assert audit is not None
    assert audit.org_id == org.org_id and audit.actor_user_id == org.admin_id
    assert audit.metadata_["job_id"] == job_id
    assert audit.metadata_["previous_status"] == "failed"


@pytest.mark.parametrize("role", ["member", "reviewer"])
async def test_non_admin_reprocess_is_403(client, make_org, ingest, sessionmaker, role):
    org = await make_org()
    doc_id = await ingest(org, ANNUAL_RCEPT_NO)
    resp = await client.post(f"/documents/{doc_id}/reprocess", headers=org.headers(role))
    assert resp.status_code == 403
    assert len(await _jobs_for(sessionmaker, doc_id)) == 1


async def test_other_org_or_unknown_document_is_404(client, make_org, ingest, sessionmaker):
    org = await make_org()
    other = await make_org()
    doc_id = await ingest(org, ANNUAL_RCEPT_NO)
    resp = await client.post(f"/documents/{doc_id}/reprocess", headers=other.headers("admin"))
    assert resp.status_code == 404
    resp = await client.post(f"/documents/{uuid.uuid4()}/reprocess", headers=org.headers("admin"))
    assert resp.status_code == 404
    async with sessionmaker() as s:
        doc = await s.get(Document, uuid.UUID(doc_id))
        audits = await s.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "document.reprocess_requested", AuditLog.target_id == doc_id)
        )
    assert doc.status == "ready" and audits == 0


@pytest.mark.parametrize("busy", ["queued", "processing"])
async def test_busy_document_is_409(client, make_org, ingest, sessionmaker, busy):
    org = await make_org()
    doc_id = await ingest(org, ANNUAL_RCEPT_NO)
    await _set_status(sessionmaker, doc_id, busy)
    resp = await client.post(f"/documents/{doc_id}/reprocess", headers=org.headers("admin"))
    assert resp.status_code == 409
    assert len(await _jobs_for(sessionmaker, doc_id)) == 1


async def test_reprocess_replaces_chunks_with_new_section_paths(
    client, make_org, ingest, worker, sessionmaker, fake_dart
):
    org = await make_org()
    doc_id = await ingest(org, ANNUAL_RCEPT_NO)
    document_id = uuid.UUID(doc_id)

    async def chunks() -> list[tuple[str, int]]:
        async with sessionmaker() as s:
            rows = await s.execute(
                select(Chunk.section_path, Chunk.chunk_index).where(
                    Chunk.document_id == document_id
                )
            )
            return [(r.section_path, r.chunk_index) for r in rows]

    first = await chunks()
    assert first
    # Simulate chunks written by the previous parser (whole notes section, no sub-headings).
    async with sessionmaker() as s:
        await s.execute(
            update(Chunk).where(Chunk.document_id == document_id).values(section_path=NOTES)
        )
        await s.commit()

    resp = await client.post(f"/documents/{doc_id}/reprocess", headers=org.headers("admin"))
    assert resp.status_code == 202, resp.text
    while await worker.run_once():
        pass

    second = await chunks()
    assert len(second) == len(first)  # replaced, not appended
    assert len(set(second)) == len(second)
    assert sorted(i for _, i in second) == sorted(i for _, i in first)
    paths = {p for p, _ in second}
    assert f"{NOTES} > 1. 일반사항 (연결)" in paths
    assert f"{NOTES} > 12. 리스 (연결)" in paths
    async with sessionmaker() as s:
        doc = await s.get(Document, document_id)
        job = await s.get(Job, uuid.UUID(resp.json()["job_id"]))
    assert doc.status == "ready" and job.status == "succeeded"
    # The cached ZIP (data/dart/fake/{rcept_no}.zip) is reused: at most the first import downloads.
    assert fake_dart.download_calls.count(ANNUAL_RCEPT_NO) <= 1


async def test_reprocess_upload_reuses_stored_file(client, make_org, worker, sessionmaker):
    org = await make_org()
    content = "# 정책\n개요 문장\n## 리스\n리스 회계처리 설명\n".encode()
    resp = await client.post(
        "/documents/upload",
        files={"file": ("policy.md", content, "text/markdown")},
        headers=org.headers("member"),
    )
    doc_id = resp.json()["document_id"]
    while await worker.run_once():
        pass
    resp = await client.post(f"/documents/{doc_id}/reprocess", headers=org.headers("admin"))
    assert resp.status_code == 202, resp.text
    jobs = await _jobs_for(sessionmaker, doc_id)
    assert [j.type for j in jobs] == ["ingest_text", "ingest_text"]
    assert jobs[1].payload == jobs[0].payload
    while await worker.run_once():
        pass
    async with sessionmaker() as s:
        doc = await s.get(Document, uuid.UUID(doc_id))
        paths = list(await s.scalars(select(Chunk.section_path).where(Chunk.document_id == doc.id)))
    assert doc.status == "ready" and paths == ["정책", "정책 > 리스"]


async def test_cli_dart_reprocess_uses_the_service(
    capsys, client, make_org, ingest, sessionmaker, monkeypatch
):
    from footnote_rag import cli
    from footnote_rag.db import session as db_session
    from footnote_rag.documents import service

    org = await make_org()
    annual_id = await ingest(org, ANNUAL_RCEPT_NO)
    audit_id = await ingest(org, "20260310900001")
    calls: list[uuid.UUID] = []
    original = service.request_reprocess

    async def spy(session, *, user, document_id):
        calls.append(document_id)
        return await original(session, user=user, document_id=document_id)

    monkeypatch.setattr(service, "request_reprocess", spy)

    async def no_dispose():  # keep the shared test engine alive
        return None

    monkeypatch.setattr(db_session, "dispose_engine", no_dispose)
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "dart-reprocess",
            "--org-admin-email",
            org.emails["admin"],
            "--rcept-no",
            f"{ANNUAL_RCEPT_NO},20269999999999",
        ]
    )
    assert await cli.cmd_dart_reprocess(args) == 1  # unknown receipt number
    assert calls == [uuid.UUID(annual_id)]
    out = capsys.readouterr()
    assert f"{ANNUAL_RCEPT_NO}\tqueued" in out.out
    assert "20269999999999\tnot found" in out.err

    # --all: the annual report is still queued (skipped, 409 rule), the audit report is queued.
    args = parser.parse_args(["dart-reprocess", "--org-admin-email", org.emails["admin"], "--all"])
    assert await cli.cmd_dart_reprocess(args) == 1
    assert calls[1:] == [uuid.UUID(annual_id), uuid.UUID(audit_id)]
    out = capsys.readouterr()
    assert f"{ANNUAL_RCEPT_NO}\tskipped: document is queued" in out.err
    assert "20260310900001\tqueued" in out.out
    async with sessionmaker() as s:
        audits = await s.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.org_id == org.org_id, AuditLog.action == "document.reprocess_requested")
        )
    assert audits == 2

    with pytest.raises(SystemExit):  # --rcept-no and --all are mutually exclusive and required
        parser.parse_args(["dart-reprocess", "--org-admin-email", org.emails["admin"]])
