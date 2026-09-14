from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import Settings

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=Settings)
def get_settings():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT intent_timeout, reason_timeout, auth_claim_ttl, auth_request_ttl "
            "FROM settings WHERE rowid = 1"
        ).fetchone()
        return Settings(
            intent_timeout=row["intent_timeout"],
            reason_timeout=row["reason_timeout"],
            auth_claim_ttl=row["auth_claim_ttl"],
            auth_request_ttl=row["auth_request_ttl"],
        )


@router.put("/settings", response_model=Settings)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute(
            "UPDATE settings SET intent_timeout = ?, reason_timeout = ?, "
            "auth_claim_ttl = ?, auth_request_ttl = ? WHERE rowid = 1",
            (
                body.intent_timeout,
                body.reason_timeout,
                body.auth_claim_ttl,
                body.auth_request_ttl,
            ),
        )
        return body
