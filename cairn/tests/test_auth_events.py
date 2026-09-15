from __future__ import annotations

import hashlib
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cairn.server import db
from cairn.server.app import app
from cairn.server.models import CreateAuthEvent
from cairn.server.services import (
    lookup_auth_credential,
    provision_auth_credential,
    revoke_auth_credential,
    rotate_auth_credential,
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) VALUES ('proj_001', 'test', 'active', 1, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO auth_requests (id, project_id, source_fact_ids, auth_ref, role, login_url, reason, status, created_at) VALUES ('auth_001', 'proj_001', 'origin', 'target-user', 'user', 'https://example.test/login', 'secret reason', 'pending', '2026-01-01T00:00:00Z')"
        )
        provision_auth_credential(
            conn,
            "helper-token",
            actor_id="helper-a",
            scopes={"helper.event.submit", "helper.request.read"},
            project_allowlist={"proj_001"},
        )
    with TestClient(app) as test_client:
        yield test_client


def _headers(token: str = "helper-token", proto: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if proto:
        headers["X-Forwarded-Proto"] = proto
    return headers


def _event(kind: str = "launch_requested", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_id": "proj_001",
        "request_id": "auth_001",
        "auth_ref": "target-user",
        "kind": kind,
        "idempotency_key": str(uuid4()),
        "occurred_at": "2026-01-01T00:00:00Z",
    }
    payload.update(extra)
    return payload


def test_event_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateAuthEvent.model_validate(_event(actor_id="forged"))


def test_event_ingress_rejects_project_request_auth_ref_mismatch(client: TestClient) -> None:
    response = client.post(
        "/auth-events",
        json={**_event(), "auth_ref": "other-target"},
        headers=_headers(proto="https"),
    )
    assert response.status_code == 409


def test_event_ingress_is_idempotent_per_actor_and_rejects_conflict(client: TestClient) -> None:
    key = str(uuid4())
    payload = _event(idempotency_key=key)
    first = client.post("/auth-events", json=payload, headers=_headers(proto="https"))
    second = client.post("/auth-events", json=payload, headers=_headers(proto="https"))
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()

    conflict = client.post(
        "/auth-events",
        json={**payload, "kind": "browser_opened"},
        headers=_headers(proto="https"),
    )
    assert conflict.status_code == 409


def test_helper_view_is_scoped_and_redacts_legacy_fields(client: TestClient) -> None:
    pending = client.get(
        "/projects/proj_001/auth-requests/helper-pending", headers=_headers(proto="https")
    )
    assert pending.status_code == 200
    assert set(pending.json()[0]) == {"id", "auth_ref", "login_url", "status"}
    assert "reason" not in pending.json()[0]

    view = client.get(
        "/projects/proj_001/auth-requests/auth_001/helper-view", headers=_headers(proto="https")
    )
    assert view.status_code == 200
    assert set(view.json()) == {"id", "auth_ref", "login_url", "status"}


def test_transport_guard_rejects_non_loopback_cleartext(client: TestClient) -> None:
    response = client.post("/auth-events", json=_event(), headers=_headers())
    assert response.status_code == 400


def test_credential_digest_rotation_scope_and_revocation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "credentials.db")
    with db.get_conn() as conn:
        credential = provision_auth_credential(
            conn,
            "current-token",
            actor_id="helper-a",
            scopes={"helper.event.submit"},
            project_allowlist={"proj_001"},
        )
        assert credential.token_digest == hashlib.sha256(b"current-token").hexdigest()
        assert credential.actor_id == "helper-a"

        principal = lookup_auth_credential(conn, "current-token")
        assert principal is not None
        assert principal.actor_id == "helper-a"
        assert principal.project_allowlist == frozenset({"proj_001"})

        rotated = rotate_auth_credential(conn, "current-token", "next-token", overlap_seconds=300)
        assert lookup_auth_credential(conn, "current-token") is not None
        assert lookup_auth_credential(conn, "next-token") is not None
        assert rotated.actor_id == "helper-a"
        assert revoke_auth_credential(conn, "next-token") is True
        assert lookup_auth_credential(conn, "next-token") is None
