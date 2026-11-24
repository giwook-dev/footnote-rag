"""401 / 403 / cross-organization 404."""

import uuid

import jwt
import pytest
from sqlalchemy import select

from footnote_rag.auth.security import create_access_token
from footnote_rag.db.models import AuditLog
from tests.conftest import PASSWORD

PROTECTED = [
    ("GET", "/users/me"),
    ("GET", "/documents"),
    ("POST", "/questions"),
    ("GET", "/reviews"),
    ("GET", "/audit-logs"),
    ("GET", "/usage"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_missing_token_is_401(client, method, path):
    resp = await client.request(method, path, json={"question": "x?"})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_invalid_and_expired_tokens_are_401(client, make_org, settings):
    org = await make_org()
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert (await client.get("/users/me", headers=bad)).status_code == 401
    forged = jwt.encode(
        {"sub": str(org.admin_id), "org_id": str(org.org_id), "role": "admin", "exp": 9999999999},
        "a-completely-different-signing-secret-0123456789",
        algorithm="HS256",
    )
    resp = await client.get("/users/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401
    expired = jwt.encode(
        {"sub": str(org.admin_id), "org_id": str(org.org_id), "role": "admin", "exp": 1},
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    resp = await client.get("/users/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


async def test_token_with_mismatched_org_is_401(client, make_org):
    org = await make_org()
    token = create_access_token(user_id=org.admin_id, org_id=uuid.uuid4(), role="admin")
    resp = await client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_login_and_me(client, make_org):
    org = await make_org()
    resp = await client.get("/users/me", headers=org.headers("member"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "member" and body["org_id"] == str(org.org_id)
    payload = jwt.decode(org.tokens["member"], options={"verify_signature": False})
    assert {"sub", "org_id", "role", "exp"} <= payload.keys()


async def test_wrong_password_is_401_and_audited(client, make_org, sessionmaker):
    org = await make_org()
    resp = await client.post(
        "/auth/token", data={"username": org.emails["member"], "password": "nope-nope"}
    )
    assert resp.status_code == 401
    resp = await client.post(
        "/auth/token", data={"username": "ghost@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 401
    async with sessionmaker() as s:
        actions = list(
            await s.scalars(select(AuditLog.action).where(AuditLog.org_id == org.org_id))
        )
        assert "auth.login_failed" in actions and "auth.login_succeeded" in actions
        ghost = await s.scalar(
            select(AuditLog).where(
                AuditLog.action == "auth.login_failed",
                AuditLog.metadata_["email"].astext == "ghost@example.com",
            )
        )
        assert ghost is not None and ghost.org_id is None
        assert "password" not in ghost.metadata_


async def test_role_checks_are_403(client, make_org):
    org = await make_org()
    new_user = {"email": "x-" + uuid.uuid4().hex[:6] + "@example.com", "password": "longenough1"}
    for role in ("member", "reviewer"):
        resp = await client.post("/users", json=new_user, headers=org.headers(role))
        assert resp.status_code == 403
    assert (await client.get("/reviews", headers=org.headers("member"))).status_code == 403
    assert (await client.get("/audit-logs", headers=org.headers("reviewer"))).status_code == 403
    assert (await client.get("/usage", headers=org.headers("member"))).status_code == 403


async def test_admin_creates_user_in_own_org(client, make_org):
    org = await make_org()
    email = f"new-{uuid.uuid4().hex[:6]}@example.com"
    resp = await client.post(
        "/users",
        json={"email": email, "password": "longenough1", "role": "reviewer"},
        headers=org.headers("admin"),
    )
    assert resp.status_code == 201
    assert resp.json()["org_id"] == str(org.org_id)
    dup = await client.post(
        "/users", json={"email": email, "password": "longenough1"}, headers=org.headers("admin")
    )
    assert dup.status_code == 409


async def test_other_org_resources_are_404(client, make_org, ingest):
    org_a = await make_org()
    org_b = await make_org()
    doc_id = await ingest(org_a)

    resp = await client.get(f"/documents/{doc_id}", headers=org_b.headers("admin"))
    assert resp.status_code == 404
    assert (
        await client.get(f"/documents/{doc_id}", headers=org_a.headers("member"))
    ).status_code == 200

    ask = await client.post(
        "/questions",
        json={"question": "리스부채는 어떻게 측정하나요?"},
        headers=org_a.headers("member"),
    )
    assert ask.status_code == 200, ask.text
    answer_id = ask.json()["answer_id"]
    assert (
        await client.get(f"/answers/{answer_id}", headers=org_b.headers("admin"))
    ).status_code == 404

    # Asking org B with org A's document id is a 404, not a data leak.
    resp = await client.post(
        "/questions",
        json={"question": "리스?", "document_ids": [doc_id]},
        headers=org_b.headers("member"),
    )
    assert resp.status_code == 404
    review = await client.post(
        f"/reviews/{answer_id}", json={"decision": "approve"}, headers=org_b.headers("reviewer")
    )
    assert review.status_code == 404
