from __future__ import annotations

import hashlib
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from pydantic import ValidationError

from cairn.server import db
from cairn.server.app import app
from cairn.server.models import CreateAuthEvent
from cairn.server.services import (
    bootstrap_auth_deployment,
    lookup_auth_credential,
    provision_auth_credential,
    revoke_auth_credential,
    rotate_auth_credential,
    require_secure_bearer_transport,
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
            "INSERT INTO auth_requests (id, project_id, source_fact_ids, auth_ref, role, login_url, reason, status, created_at) VALUES ('auth_001', 'proj_001', 'origin', 'target-user', 'user', 'https://example.test/login', 'secret reason', 'pending', '2099-01-01T00:00:00Z')"
        )
        provision_auth_credential(
            conn,
            "helper-token",
            actor_id="helper-a",
            scopes={"helper.event.submit", "helper.request.read"},
            project_allowlist={"proj_001"},
        )
    with TestClient(app, base_url="https://testserver") as test_client:
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
    assert pending.json()[0]["login_url"] is None
    assert "reason" not in pending.json()[0]

    view = client.get(
        "/projects/proj_001/auth-requests/auth_001/helper-view", headers=_headers(proto="https")
    )
    assert view.status_code == 200
    assert set(view.json()) == {"id", "auth_ref", "login_url", "status"}


def test_legacy_helper_listing_remains_available_until_helper_event_migration(client: TestClient) -> None:
    response = client.get("/auth-requests", headers=_headers(proto="https"))
    # Raw helper migration is deferred until AuthHelperClient switches to events in Task 3.
    assert response.status_code == 200


def test_deployment_bootstrap_hashes_helper_and_dispatcher_tokens_without_plaintext(tmp_path, monkeypatch) -> None:
    from cairn.dispatcher.config import AuthConfig

    monkeypatch.setenv("AUTH_HELPER_SECRET", "helper-deployment-secret")
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "bootstrap.db")
    with db.get_conn() as conn:
        config = AuthConfig.model_validate({
            "store_root": "/tmp/auth",
            "helper_token_env": "AUTH_HELPER_SECRET",
            "helper_actor_id": "desktop-helper",
            "helper_scopes": ["helper.event.submit", "helper.request.read"],
            "helper_project_allowlist": ["proj_001", "proj_002"],
            "targets": [{"name": "target-user", "base_url": "https://example.test", "login_url": "https://example.test/login", "role": "user", "verify": {"url": "https://example.test/me"}}],
        })
        bootstrap_auth_deployment(conn, auth_config=config, dispatcher_token="dispatcher-deployment-secret")
        rows = conn.execute("SELECT * FROM auth_credentials ORDER BY actor_id").fetchall()
        assert [row["actor_id"] for row in rows] == ["desktop-helper", "dispatcher"]
        assert all("deployment-secret" not in row["token_digest"] for row in rows)
        helper = next(row for row in rows if row["actor_id"] == "desktop-helper")
        assert helper["project_allowlist"] == '["proj_001", "proj_002"]'


def test_helper_view_uses_configured_target_authority(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "target-authority.db")
    with db.get_conn() as conn:
        bootstrap_auth_deployment(conn, target_configs={"target-user": "https://configured.example/login"})
        conn.execute("INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) VALUES ('proj_001', 'test', 'active', 1, '2099-01-01T00:00:00Z')")
        conn.execute("INSERT INTO auth_requests (id, project_id, source_fact_ids, auth_ref, role, login_url, reason, status, created_at) VALUES ('auth_001', 'proj_001', 'origin', 'target-user', 'user', 'https://untrusted.example/login', 'secret', 'pending', '2099-01-01T00:00:00Z')")
        provision_auth_credential(conn, "helper-token", actor_id="helper-a", scopes={"helper.request.read"}, project_allowlist={"proj_001"})
    with TestClient(app, base_url="https://testserver") as configured:
        response = configured.get("/projects/proj_001/auth-requests/auth_001/helper-view", headers=_headers())
    assert response.status_code == 200
    assert response.json()["login_url"] == "https://configured.example/login"


def test_target_authority_snapshot_replaces_stale_targets_atomically(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "target-snapshot.db")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO auth_target_configs VALUES ('stale', 'https://stale.example/login')")
        bootstrap_auth_deployment(conn, target_configs={"fresh": "https://fresh.example/login"})
        rows = conn.execute("SELECT auth_ref, login_url FROM auth_target_configs ORDER BY auth_ref").fetchall()
    assert [(row["auth_ref"], row["login_url"]) for row in rows] == [("fresh", "https://fresh.example/login")]


def test_transport_guard_rejects_non_loopback_cleartext(client: TestClient) -> None:
    with TestClient(app, base_url="http://remote.example") as insecure:
        response = insecure.post("/auth-events", json=_event(), headers=_headers())
    assert response.status_code == 400


def test_transport_guard_rejects_untrusted_forwarded_https() -> None:
    scope = {
        "type": "http", "scheme": "http", "server": ("remote.example", 80),
        "client": ("203.0.113.10", 12345), "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": [(b"x-forwarded-proto", b"https")], "http_version": "1.1",
    }
    with pytest.raises(Exception):
        require_secure_bearer_transport(Request(scope))


def test_transport_guard_accepts_actual_loopback_cleartext() -> None:
    scope = {
        "type": "http", "scheme": "http", "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 12345), "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": [], "http_version": "1.1",
    }
    require_secure_bearer_transport(Request(scope))


def test_helper_views_reap_stale_claims_and_requests(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE auth_requests SET status = 'claimed', claimed_at = '2000-01-01T00:00:00Z' WHERE id = 'auth_001'")
        conn.execute("UPDATE auth_requests SET created_at = '2000-01-01T00:00:00Z' WHERE id = 'auth_001'")
    result = client.get("/projects/proj_001/auth-requests/helper-pending", headers=_headers())
    assert result.status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM auth_requests WHERE id = 'auth_001'").fetchone()["status"] == "expired"


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
