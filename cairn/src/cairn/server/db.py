from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15,
    auth_claim_ttl INTEGER NOT NULL DEFAULT 300,
    auth_request_ttl INTEGER NOT NULL DEFAULT 1800
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS auth_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_fact_ids TEXT NOT NULL,
    auth_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    login_url TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    failure_reason TEXT,
    helper_actor_id TEXT,
    expires_at TEXT,
    expiry_generation INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_auth_requests_project ON auth_requests (project_id, auth_ref);

CREATE TABLE IF NOT EXISTS auth_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    auth_ref TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('launch_requested', 'browser_opened', 'login_succeeded', 'login_failed')),
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'claimed', 'retryable', 'applied', 'rejected')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    claimed_by TEXT,
    claim_expires_at TEXT,
    processed_at TEXT,
    outcome_code TEXT,
    capture_generation INTEGER,
    UNIQUE (actor_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_auth_events_queue ON auth_events (state, next_attempt_at, received_at);

CREATE TABLE IF NOT EXISTS auth_lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    outcome_code TEXT NOT NULL,
    UNIQUE (request_id, event_id, kind)
);

CREATE TABLE IF NOT EXISTS auth_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_digest TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    project_allowlist TEXT NOT NULL,
    not_before TEXT NOT NULL,
    expires_at TEXT,
    replaced_by TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_credentials_actor ON auth_credentials (actor_id);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_project_columns(conn)
        _ensure_settings_columns(conn)
        _ensure_auth_request_columns(conn)
        _ensure_auth_schema(conn)


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )


def _ensure_settings_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "auth_claim_ttl" not in columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN auth_claim_ttl INTEGER NOT NULL DEFAULT 300"
        )
    if "auth_request_ttl" not in columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN auth_request_ttl INTEGER NOT NULL DEFAULT 1800"
        )


def _ensure_auth_request_columns(conn: sqlite3.Connection) -> None:
    """Add Phase 1 auth request columns to databases created by older Cairn versions."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_requests)")}
    if "helper_actor_id" not in columns:
        conn.execute("ALTER TABLE auth_requests ADD COLUMN helper_actor_id TEXT")
    if "expires_at" not in columns:
        conn.execute("ALTER TABLE auth_requests ADD COLUMN expires_at TEXT")
    if "expiry_generation" not in columns:
        conn.execute(
            "ALTER TABLE auth_requests ADD COLUMN expiry_generation INTEGER NOT NULL DEFAULT 1"
        )

    ttl_row = conn.execute(
        "SELECT auth_request_ttl FROM settings WHERE rowid = 1"
    ).fetchone()
    ttl = int(ttl_row["auth_request_ttl"]) if ttl_row is not None else 1800
    rows = conn.execute(
        "SELECT id, created_at FROM auth_requests WHERE expires_at IS NULL"
    ).fetchall()
    for row in rows:
        expires_at = _expiry_for(row["created_at"], ttl)
        conn.execute(
            "UPDATE auth_requests SET expires_at = ? WHERE id = ?",
            (expires_at, row["id"]),
        )


def _expiry_for(created_at: str, ttl: int) -> str | None:
    if ttl <= 0:
        return None
    try:
        parsed = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(seconds=ttl)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _ensure_auth_schema(conn: sqlite3.Connection) -> None:
    """Backfill deterministic lifecycle records for pre-event auth requests."""
    rows = conn.execute(
        "SELECT id, status, created_at, completed_at FROM auth_requests ORDER BY rowid"
    ).fetchall()
    terminal_statuses = {"completed", "failed", "cancelled", "expired"}
    for row in rows:
        request_id = row["id"]
        existing = conn.execute(
            "SELECT COUNT(*) AS count FROM auth_lifecycle_events WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing["count"]:
            continue
        created_at = row["created_at"]
        conn.execute(
            """
            INSERT INTO auth_lifecycle_events
                (request_id, event_id, sequence, kind, recorded_at, outcome_code)
            VALUES (?, ?, 1, 'created', ?, 'created')
            """,
            (request_id, f"backfill:create:{request_id}", created_at),
        )
        if row["status"] in terminal_statuses:
            recorded_at = row["completed_at"] or created_at
            conn.execute(
                """
                INSERT INTO auth_lifecycle_events
                    (request_id, event_id, sequence, kind, recorded_at, outcome_code)
                VALUES (?, ?, 2, ?, ?, ?)
                """,
                (
                    request_id,
                    f"backfill:terminal:{request_id}",
                    row["status"],
                    recorded_at,
                    row["status"],
                ),
            )


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
