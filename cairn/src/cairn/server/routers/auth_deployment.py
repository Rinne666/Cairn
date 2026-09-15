from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from cairn.server.db import get_conn
from cairn.server.models import AuthDeploymentSnapshot
from cairn.server.services import bootstrap_auth_deployment, require_auth_principal

router = APIRouter(tags=["auth-deployment"])


@router.post("/internal/auth/deployment", status_code=204)
def apply_auth_deployment(
    body: AuthDeploymentSnapshot,
    request: Request,
) -> Response:
    """Apply a Dispatcher-owned auth deployment snapshot in one DB transaction."""
    with get_conn() as conn:
        principal = require_auth_principal(request, conn, scope="dispatcher.auth.consume")
        # A helper credential must never be able to cross into deployment control,
        # even if someone accidentally grants it the dispatcher scope.
        if principal.actor_id == "helper" or {
            "helper.event.submit",
            "helper.request.read",
        } & principal.scopes:
            raise HTTPException(403, "Forbidden")
        try:
            bootstrap_auth_deployment(
                conn,
                dispatcher_token=body.dispatcher_token,
                target_configs=body.targets,
                helper_token=body.helper_token,
                helper_actor_id=body.helper_actor_id,
                helper_scopes=body.helper_scopes,
                helper_project_allowlist=body.helper_project_allowlist,
                allow_environment_fallback=False,
            )
        except ValueError as exc:
            raise HTTPException(422, "invalid auth deployment snapshot") from exc
    return Response(status_code=204)
