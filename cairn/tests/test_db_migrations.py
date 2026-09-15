from __future__ import annotations

import hashlib
import json
import sqlite3

from cairn.server import db
from cairn.server.services import bootstrap_auth_deployment


def test_configure_adds_bootstrap_enabled_to_legacy_projects_table(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                reason_worker TEXT,
                reason_trigger TEXT,
                reason_started_at TEXT,
                reason_last_heartbeat_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, title, created_at) VALUES ('proj_001', 'legacy', '2026-01-01T00:00:00Z')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        row = conn.execute("SELECT bootstrap_enabled FROM projects WHERE id = 'proj_001'").fetchone()
    assert row["bootstrap_enabled"] == 1


def test_configure_maps_disabled_bootstrap_mode_to_false(tmp_path, monkeypatch) -> None:
    path = tmp_path / "intermediate.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                bootstrap_mode TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL,
                reason_worker TEXT,
                reason_trigger TEXT,
                reason_started_at TEXT,
                reason_last_heartbeat_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, title, bootstrap_mode, created_at) VALUES ('proj_001', 'disabled', 'disabled', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO projects (id, title, bootstrap_mode, created_at) VALUES ('proj_002', 'enabled', 'enabled', '2026-01-01T00:00:00Z')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        rows = conn.execute("SELECT id, bootstrap_enabled FROM projects ORDER BY id").fetchall()
    assert [(row["id"], row["bootstrap_enabled"]) for row in rows] == [
        ("proj_001", 0),
        ("proj_002", 1),
    ]


def test_configure_adds_auth_control_plane_tables_and_backfills_legacy_requests(tmp_path, monkeypatch) -> None:
    path = tmp_path / "auth-legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE settings (intent_timeout INTEGER NOT NULL, reason_timeout INTEGER NOT NULL)")
        conn.execute("INSERT INTO settings VALUES (15, 15)")
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO projects VALUES ('proj_001', 'legacy', 'active', '2026-01-01T00:00:00Z')")
        conn.execute(
            """
            CREATE TABLE auth_requests (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_fact_ids TEXT NOT NULL,
                auth_ref TEXT NOT NULL, role TEXT NOT NULL, login_url TEXT,
                reason TEXT NOT NULL, status TEXT NOT NULL, claimed_by TEXT,
                created_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT,
                failure_reason TEXT
            )
            """
        )
        conn.execute(
            """INSERT INTO auth_requests
                (id, project_id, source_fact_ids, auth_ref, role, reason, status, created_at, completed_at)
                VALUES ('auth_001', 'proj_001', 'origin', 'target-user', 'user', 'legacy', 'completed',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z')"""
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        for table in ("auth_events", "auth_lifecycle_events", "auth_credentials"):
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_requests)")}
        assert {"helper_actor_id", "expires_at", "expiry_generation"} <= columns
        credential_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(auth_credentials)")
        }
        assert {"deployment_owned", "deployment_slot"} <= credential_columns
        lifecycle = conn.execute(
            "SELECT event_id, kind, outcome_code FROM auth_lifecycle_events WHERE request_id = 'auth_001' ORDER BY sequence"
        ).fetchall()
        assert [(row["event_id"], row["kind"]) for row in lifecycle] == [
            ("backfill:create:auth_001", "created"),
            ("backfill:terminal:auth_001", "completed"),
        ]
        expiry = conn.execute(
            "SELECT expires_at, expiry_generation FROM auth_requests WHERE id = 'auth_001'"
        ).fetchone()
        assert expiry["expires_at"] == "2026-01-01T00:30:00Z"
        assert expiry["expiry_generation"] == 1

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM auth_lifecycle_events WHERE request_id = 'auth_001'"
        ).fetchone()["count"] == 2


def test_configure_backfills_unambiguous_legacy_deployment_credentials(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy-credentials.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE settings (intent_timeout INTEGER NOT NULL, reason_timeout INTEGER NOT NULL)")
        conn.execute("INSERT INTO settings VALUES (15, 15)")
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO projects VALUES ('proj_001', 'legacy', 'active', '2026-01-01T00:00:00Z')")
        conn.execute(
            """
            CREATE TABLE auth_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_digest TEXT NOT NULL UNIQUE,
                actor_id TEXT NOT NULL,
                scopes TEXT NOT NULL,
                project_allowlist TEXT NOT NULL,
                not_before TEXT NOT NULL,
                expires_at TEXT,
                replaced_by TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        rows = [
            ("legacy-dispatcher", "dispatcher", ["dispatcher.auth.consume"], ["*"]),
            ("legacy-helper", "helper", ["helper.event.submit", "helper.request.read"], ["*"]),
            ("manual-same-shape", "operator", ["helper.event.submit", "helper.request.read"], ["*"]),
        ]
        for token, actor_id, scopes, projects in rows:
            conn.execute(
                """INSERT INTO auth_credentials
                (token_digest, actor_id, scopes, project_allowlist, not_before, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    actor_id,
                    json.dumps(scopes),
                    json.dumps(projects),
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )

    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.delenv("CAIRN_AUTH_DEPLOYMENT_SNAPSHOT", raising=False)
    monkeypatch.delenv("CAIRN_AUTH_DISPATCHER_TOKEN", raising=False)
    monkeypatch.delenv("CAIRN_AUTH_HELPER_TOKEN", raising=False)
    db.configure(path)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT actor_id, deployment_owned, deployment_slot FROM auth_credentials ORDER BY actor_id"
        ).fetchall()
    assert [(row["actor_id"], row["deployment_owned"], row["deployment_slot"]) for row in rows] == [
        ("dispatcher", 1, "dispatcher"),
        ("helper", 1, "helper"),
        ("operator", 0, None),
    ]

    with db.get_conn() as conn:
        bootstrap_auth_deployment(
            conn,
            dispatcher_token="new-dispatcher",
            allow_environment_fallback=False,
        )
        rows = conn.execute(
            "SELECT token_digest, expires_at FROM auth_credentials"
        ).fetchall()
    expiry_by_token = {row["token_digest"]: row["expires_at"] for row in rows}
    assert expiry_by_token[hashlib.sha256(b"legacy-dispatcher").hexdigest()] is not None
    assert expiry_by_token[hashlib.sha256(b"legacy-helper").hexdigest()] is not None
    assert expiry_by_token[hashlib.sha256(b"manual-same-shape").hexdigest()] is None
