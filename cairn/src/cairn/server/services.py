from __future__ import annotations

import sqlite3
import hashlib
import ipaddress
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import timedelta
from typing import Iterable
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from cairn.server.models import AuthCredential, AuthEvent, Intent, ProjectMeta, ProjectReason

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class AuthPrincipal:
    actor_id: str
    scopes: frozenset[str]
    project_allowlist: frozenset[str]
    credential_id: int
    token_digest: str


def _credential_model(row: sqlite3.Row) -> AuthCredential:
    return AuthCredential(
        id=row["id"],
        token_digest=row["token_digest"],
        actor_id=row["actor_id"],
        scopes=json.loads(row["scopes"]),
        project_allowlist=json.loads(row["project_allowlist"]),
        not_before=row["not_before"],
        expires_at=row["expires_at"],
        replaced_by=row["replaced_by"],
        created_at=row["created_at"],
    )


def provision_auth_credential(
    conn: sqlite3.Connection,
    token: str,
    *,
    actor_id: str,
    scopes: Iterable[str],
    project_allowlist: Iterable[str] = (),
    not_before: str | None = None,
    expires_at: str | None = None,
) -> AuthCredential:
    """Store only an operator-provided bearer token digest.

    This function is deliberately an internal provisioning primitive: callers receive
    the metadata model, while the opaque token is never persisted or returned.
    An empty project allowlist means no projects; use ``*`` for an operator-wide
    credential explicitly.
    """
    if not token:
        raise ValueError("token must not be empty")
    actor_id = actor_id.strip()
    scope_values = sorted({str(value).strip() for value in scopes if str(value).strip()})
    project_values = sorted({str(value).strip() for value in project_allowlist if str(value).strip()})
    if not actor_id or not scope_values:
        raise ValueError("actor_id and scopes are required")
    now = not_before or utcnow()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        cursor = conn.execute(
            """
            INSERT INTO auth_credentials
                (token_digest, actor_id, scopes, project_allowlist, not_before, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (digest, actor_id, json.dumps(scope_values), json.dumps(project_values), now, expires_at, utcnow()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("credential already exists") from exc
    row = conn.execute("SELECT * FROM auth_credentials WHERE id = ?", (cursor.lastrowid,)).fetchone()
    assert row is not None
    return _credential_model(row)


def bootstrap_auth_credentials(
    conn: sqlite3.Connection,
    *,
    helper_token: str | None = None,
    dispatcher_token: str | None = None,
    helper_actor_id: str | None = None,
    helper_scopes: Iterable[str] | None = None,
    helper_project_allowlist: Iterable[str] | None = None,
) -> None:
    """Provision deployment credentials while keeping opaque tokens out of storage."""
    helper_token = helper_token if helper_token is not None else os.getenv("CAIRN_AUTH_HELPER_TOKEN")
    dispatcher_token = dispatcher_token if dispatcher_token is not None else os.getenv("CAIRN_AUTH_DISPATCHER_TOKEN")
    if helper_token:
        _upsert_deployment_credential(
            conn,
            helper_token,
            actor_id=helper_actor_id or os.getenv("CAIRN_AUTH_HELPER_ACTOR_ID", "helper"),
            scopes=helper_scopes if helper_scopes is not None else _csv_env("CAIRN_AUTH_HELPER_SCOPES", "helper.event.submit,helper.request.read"),
            projects=helper_project_allowlist if helper_project_allowlist is not None else _csv_env("CAIRN_AUTH_HELPER_PROJECTS", "*"),
        )
    if dispatcher_token:
        _upsert_deployment_credential(
            conn,
            dispatcher_token,
            actor_id=os.getenv("CAIRN_AUTH_DISPATCHER_ACTOR_ID", "dispatcher"),
            scopes=_csv_env("CAIRN_AUTH_DISPATCHER_SCOPES", "dispatcher.auth.consume"),
            projects=_csv_env("CAIRN_AUTH_DISPATCHER_PROJECTS", "*"),
        )


def _csv_env(name: str, default: str) -> list[str]:
    return sorted({item.strip() for item in os.getenv(name, default).split(",") if item.strip()})


def _upsert_deployment_credential(
    conn: sqlite3.Connection,
    token: str,
    *,
    actor_id: str,
    scopes: Iterable[str],
    projects: Iterable[str],
) -> None:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    encoded_scopes = json.dumps(sorted({value.strip() for value in scopes if value.strip()}))
    encoded_projects = json.dumps(sorted({value.strip() for value in projects if value.strip()}))
    now = utcnow()
    existing = conn.execute("SELECT id FROM auth_credentials WHERE token_digest = ?", (digest,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO auth_credentials (token_digest, actor_id, scopes, project_allowlist, not_before, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (digest, actor_id, encoded_scopes, encoded_projects, now, now),
        )
    else:
        conn.execute(
            "UPDATE auth_credentials SET actor_id = ?, scopes = ?, project_allowlist = ? WHERE id = ?",
            (actor_id, encoded_scopes, encoded_projects, existing["id"]),
        )


def bootstrap_auth_target_configs(conn: sqlite3.Connection) -> None:
    """Load validated HTTPS target authorities from deployment-only JSON."""
    raw = os.getenv("CAIRN_AUTH_TARGET_URLS")
    if not raw:
        return
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(values, dict):
        return
    for auth_ref, login_url in values.items():
        if not isinstance(auth_ref, str) or not isinstance(login_url, str):
            continue
        parsed = urlparse(login_url)
        if parsed.scheme != "https" or not parsed.netloc:
            continue
        conn.execute(
            "INSERT INTO auth_target_configs (auth_ref, login_url) VALUES (?, ?) ON CONFLICT(auth_ref) DO UPDATE SET login_url = excluded.login_url",
            (auth_ref.strip(), login_url),
        )


def lookup_auth_credential(
    conn: sqlite3.Connection, token: str, *, now: str | None = None
) -> AuthPrincipal | None:
    """Resolve a token by SHA-256 digest and enforce its validity window."""
    if not token:
        return None
    current = now or utcnow()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        SELECT * FROM auth_credentials
        WHERE token_digest = ?
          AND not_before <= ?
          AND (expires_at IS NULL OR expires_at > ?)
        """,
        (digest, current, current),
    ).fetchone()
    if row is None:
        return None
    return AuthPrincipal(
        actor_id=row["actor_id"],
        scopes=frozenset(json.loads(row["scopes"])),
        project_allowlist=frozenset(json.loads(row["project_allowlist"])),
        credential_id=row["id"],
        token_digest=row["token_digest"],
    )


def auth_token_digest(token: str) -> str:
    """Return the persisted representation for an opaque bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Names kept intentionally small and descriptive for deployment bootstrap callers.
resolve_bearer_credential = lookup_auth_credential
resolve_auth_credential = lookup_auth_credential


def rotate_auth_credential(
    conn: sqlite3.Connection,
    old_token: str,
    new_token: str,
    *,
    overlap_seconds: int = 300,
) -> AuthCredential:
    if overlap_seconds < 0:
        raise ValueError("overlap_seconds must be non-negative")
    old_digest = hashlib.sha256(old_token.encode("utf-8")).hexdigest()
    old = conn.execute("SELECT * FROM auth_credentials WHERE token_digest = ?", (old_digest,)).fetchone()
    if old is None:
        raise ValueError("credential not found")
    new = provision_auth_credential(
        conn,
        new_token,
        actor_id=old["actor_id"],
        scopes=json.loads(old["scopes"]),
        project_allowlist=json.loads(old["project_allowlist"]),
    )
    overlap_dt = datetime.now(timezone.utc) + timedelta(seconds=overlap_seconds)
    if old["expires_at"]:
        try:
            old_expiry = datetime.strptime(old["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            old_expiry = datetime.fromisoformat(old["expires_at"].replace("Z", "+00:00"))
            if old_expiry.tzinfo is None:
                old_expiry = old_expiry.replace(tzinfo=timezone.utc)
        overlap_dt = min(overlap_dt, old_expiry)
    overlap = overlap_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE auth_credentials SET expires_at = ?, replaced_by = ? WHERE id = ?",
        (overlap, str(new.id), old["id"]),
    )
    return new


def revoke_auth_credential(conn: sqlite3.Connection, token_or_digest: str) -> bool:
    digest = token_or_digest
    if len(token_or_digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in token_or_digest):
        digest = hashlib.sha256(token_or_digest.encode("utf-8")).hexdigest()
    cursor = conn.execute(
        "UPDATE auth_credentials SET expires_at = ? WHERE token_digest = ?",
        (utcnow(), digest.lower()),
    )
    return cursor.rowcount == 1


def require_auth_principal(
    request: Request,
    conn: sqlite3.Connection,
    *,
    scope: str,
    project_id: str | None = None,
) -> AuthPrincipal:
    require_secure_bearer_transport(request)
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token.strip():
        raise HTTPException(401, "Authentication required")
    principal = lookup_auth_credential(conn, token.strip())
    if principal is None:
        raise HTTPException(401, "Authentication required")
    if scope not in principal.scopes and "*" not in principal.scopes:
        raise HTTPException(403, "Forbidden")
    if project_id is not None and "*" not in principal.project_allowlist and project_id not in principal.project_allowlist:
        raise HTTPException(403, "Forbidden")
    return principal


def require_secure_bearer_transport(request: Request) -> None:
    """Require HTTPS unless the request is clearly loopback or proxy-marked HTTPS."""
    forwarded = request.headers.get("forwarded", "")
    forwarded_proto = next(
        (part.split("=", 1)[1].strip(' "') for part in forwarded.split(";") if part.strip().lower().startswith("proto=")),
        "",
    )
    proxy_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    client_host = (request.client.host if request.client else "").strip("[]").lower()
    proxy_marked_https = (proxy_proto == "https" or forwarded_proto.lower() == "https") and _is_trusted_proxy(client_host)
    if request.url.scheme.lower() == "https" or proxy_marked_https:
        return
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(400, "Bearer credentials require HTTPS")


def _is_trusted_proxy(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    configured = os.getenv("CAIRN_TRUSTED_PROXY_NETWORKS", "127.0.0.0/8,::1/128")
    for raw_network in configured.split(","):
        try:
            if address in ipaddress.ip_network(raw_network.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def reject_migrated_helper_raw_listing(request: Request, conn: sqlite3.Connection) -> None:
    """Deny migrated helper credentials while retaining unauthenticated legacy access."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return
    principal = lookup_auth_credential(conn, token.strip())
    if principal is not None and {"helper.request.read", "helper.event.submit"} & principal.scopes:
        raise HTTPException(403, "Forbidden")


def auth_event_to_model(row: sqlite3.Row) -> AuthEvent:
    from cairn.server.models import AuthEvent

    return AuthEvent(
        id=row["id"], project_id=row["project_id"], request_id=row["request_id"], auth_ref=row["auth_ref"],
        kind=row["kind"], actor_id=row["actor_id"], idempotency_key=row["idempotency_key"], occurred_at=row["occurred_at"],
        received_at=row["received_at"], state=row["state"], attempt_count=row["attempt_count"], next_attempt_at=row["next_attempt_at"],
        claimed_by=row["claimed_by"], claim_expires_at=row["claim_expires_at"], processed_at=row["processed_at"],
        outcome_code=row["outcome_code"], capture_generation=row["capture_generation"],
    )


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
