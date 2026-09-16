from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cairn.server.db import get_conn
from cairn.server.models import Settings

router = APIRouter(tags=["settings"])


@router.get("/settings")
def get_settings():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT intent_timeout, reason_timeout, auth_claim_ttl, auth_request_ttl, auth_control_plane_mode "
            "FROM settings WHERE rowid = 1"
        ).fetchone()
        settings = Settings(
            intent_timeout=row["intent_timeout"],
            reason_timeout=row["reason_timeout"],
            auth_claim_ttl=row["auth_claim_ttl"],
            auth_request_ttl=row["auth_request_ttl"],
            auth_control_plane_mode=row["auth_control_plane_mode"],
        )
        payload = settings.model_dump()
        # Preserve the pre-control-plane response shape for the default mode.
        # New modes are explicit so Dispatcher parity checks can observe them.
        if settings.auth_control_plane_mode == "legacy":
            payload.pop("auth_control_plane_mode", None)
        return JSONResponse(payload)


@router.put("/settings", response_model=Settings)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute(
            "UPDATE settings SET intent_timeout = ?, reason_timeout = ?, "
            "auth_claim_ttl = ?, auth_request_ttl = ?, auth_control_plane_mode = ? WHERE rowid = 1",
            (
                body.intent_timeout,
                body.reason_timeout,
                body.auth_claim_ttl,
                body.auth_request_ttl,
                body.auth_control_plane_mode,
            ),
        )
        return body
