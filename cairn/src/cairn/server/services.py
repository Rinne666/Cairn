from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from cairn.server.models import Intent, ProjectMeta, ProjectReason

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def get_auth_claim_ttl(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT auth_claim_ttl FROM settings WHERE rowid = 1").fetchone()
    return row["auth_claim_ttl"]


def get_auth_request_ttl(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT auth_request_ttl FROM settings WHERE rowid = 1").fetchone()
    return row["auth_request_ttl"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        bootstrap_enabled=bool(row["bootstrap_enabled"]),
        created_at=row["created_at"],
        reason=project_reason_from_row(row),
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def next_auth_request_id(conn: sqlite3.Connection) -> str:
    """Return the next global auth request id (``auth_001`` ...)."""
    conn.execute(
        "INSERT OR IGNORE INTO counters (name, value) VALUES ('auth_request', 0)"
    )
    conn.execute(
        "UPDATE counters SET value = value + 1 WHERE name = 'auth_request'"
    )
    row = conn.execute(
        "SELECT value FROM counters WHERE name = 'auth_request'"
    ).fetchone()
    assert row is not None
    return f"auth_{row['value']:03d}"


def find_active_auth_request(
    conn: sqlite3.Connection, project_id: str, auth_ref: str
) -> sqlite3.Row | None:
    """Return an in-flight auth request for the logical key, if any.

    The logical dedup key is ``(project_id, auth_ref, active-status)`` so Reason never
    creates a duplicate request while one is still being handled.
    """
    return conn.execute(
        """
        SELECT * FROM auth_requests
        WHERE project_id = ?
          AND auth_ref = ?
          AND status IN ('pending', 'claimed', 'waiting_user', 'verifying')
        """,
        (project_id, auth_ref),
    ).fetchone()


def get_auth_request_or_404(
    conn: sqlite3.Connection, request_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM auth_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Auth request not found")
    return row


def auth_request_to_model(row: sqlite3.Row) -> "AuthRequest":
    from cairn.server.models import AuthRequest

    return AuthRequest(
        id=row["id"],
        project_id=row["project_id"],
        source_fact_ids=_split_source_fact_ids(row["source_fact_ids"]),
        auth_ref=row["auth_ref"],
        role=row["role"],
        login_url=row["login_url"],
        reason=row["reason"],
        status=row["status"],
        claimed_by=row["claimed_by"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
        failure_reason=row["failure_reason"],
    )


def _split_source_fact_ids(raw: str) -> list[str]:
    if not raw:
        return []
    return [part for part in raw.split("\n") if part]


def _join_source_fact_ids(fact_ids: list[str]) -> str:
    return "\n".join(fact_ids)


def claim_auth_request_atomic(
    conn: sqlite3.Connection, request_id: str, helper_id: str
) -> bool:
    """Atomically claim a ``pending`` auth request.

    Returns ``True`` only if exactly one row was transitioned. A ``0`` rowcount means
    another helper already claimed it (or it is no longer pending), so the caller must
    back off to avoid duplicate popups across multiple helpers / dispatchers.
    """
    now = utcnow()
    cursor = conn.execute(
        """
        UPDATE auth_requests
        SET status = 'claimed',
            claimed_by = ?,
            claimed_at = ?
        WHERE id = ?
          AND status = 'pending'
        """,
        (helper_id, now, request_id),
    )
    return cursor.rowcount == 1


class AuthStateResolver:
    """Resolve the current authentication state for ``(project_id, auth_ref)``.

    Authentication state is a *temporal* fact: a later ``AuthSessionInvalid`` fact
    overrides an earlier ``AuthSessionVerified`` fact, and vice-versa. This resolver
    inspects the fact descriptions in insertion order and returns the latest state.
    """

    VERIFIED_MARKER = "AuthSessionVerified"
    INVALID_MARKER = "AuthSessionInvalid"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def resolve(self, project_id: str, auth_ref: str) -> str:
        rows = self._conn.execute(
            "SELECT description FROM facts WHERE project_id = ? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        state = "missing"
        for row in rows:
            description = row["description"] or ""
            if auth_ref not in description:
                continue
            if self.VERIFIED_MARKER in description:
                state = "valid"
            elif self.INVALID_MARKER in description:
                state = "invalid"
        return state


def build_auth_state_resolver(conn: sqlite3.Connection) -> AuthStateResolver:
    return AuthStateResolver(conn)


def _parse_ts(value: str | None) -> datetime:
    """Parse a stored ``YYYY-MM-DDTHH:MM:SSZ`` timestamp to a tz-aware datetime."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _age_seconds(value: str | None, now: datetime) -> float:
    return (now - _parse_ts(value)).total_seconds()


def expire_stale_claims(conn: sqlite3.Connection, claim_ttl: int) -> int:
    """Return ``claimed`` auth requests stuck past ``claim_ttl`` seconds to ``pending``.

    A helper claims a request, then immediately opens the browser and transitions it to
    ``waiting_user``. If the helper crashes (or the operator never acts) before that
    transition, the request must not stay ``claimed`` forever, otherwise the dedup guard
    in :func:`find_active_auth_request` would block re-claiming forever. This releases
    the stale claim so another helper can pick it up.

    Returns the number of rows released.
    """
    if claim_ttl <= 0:
        return 0
    now = datetime.now(timezone.utc)
    cutoff = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = conn.execute(
        """
        UPDATE auth_requests
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at = NULL
        WHERE status = 'claimed'
          AND claimed_at IS NOT NULL
          AND (julianday(?) - julianday(claimed_at)) * 86400 > ?
        """,
        (cutoff, claim_ttl),
    )
    return cursor.rowcount


def expire_stale_requests(conn: sqlite3.Connection, request_ttl: int) -> int:
    """Mark active auth requests older than ``request_ttl`` seconds as ``expired``.

    Applies to ``pending`` / ``claimed`` / ``waiting_user`` / ``verifying``. Expired
    requests no longer participate in dedup (they are not in the active status set), so
    a later Reason run may create a fresh request.
    """
    if request_ttl <= 0:
        return 0
    now = datetime.now(timezone.utc)
    cutoff = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = conn.execute(
        """
        UPDATE auth_requests
        SET status = 'expired',
            completed_at = ?
        WHERE status IN ('pending', 'claimed', 'waiting_user', 'verifying')
          AND (julianday(?) - julianday(created_at)) * 86400 > ?
        """,
        (cutoff, cutoff, request_ttl),
    )
    return cursor.rowcount
