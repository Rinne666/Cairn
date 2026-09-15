from __future__ import annotations

from fastapi import APIRouter, Request

from cairn.server.db import get_conn
from cairn.server.models import AuthHelperRequestView
from cairn.server.services import get_auth_request_or_404, get_project_or_404, require_auth_principal

router = APIRouter(tags=["auth-helper"])


def _view(row) -> AuthHelperRequestView:
    return AuthHelperRequestView(
        id=row["id"],
        auth_ref=row["auth_ref"],
        login_url=row["login_url"],
        status=row["status"],
    )


@router.get(
    "/projects/{project_id}/auth-requests/helper-pending",
    response_model=list[AuthHelperRequestView],
)
def list_helper_pending(project_id: str, request: Request):
    with get_conn() as conn:
        require_auth_principal(request, conn, scope="helper.request.read", project_id=project_id)
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            """
            SELECT id, auth_ref, login_url, status
            FROM auth_requests
            WHERE project_id = ? AND status = 'pending'
            ORDER BY created_at, id
            """,
            (project_id,),
        ).fetchall()
        return [_view(row) for row in rows]


@router.get(
    "/projects/{project_id}/auth-requests/{request_id}/helper-view",
    response_model=AuthHelperRequestView,
)
def get_helper_view(project_id: str, request_id: str, request: Request):
    with get_conn() as conn:
        require_auth_principal(request, conn, scope="helper.request.read", project_id=project_id)
        get_project_or_404(conn, project_id)
        row = get_auth_request_or_404(conn, request_id)
        if row["project_id"] != project_id:
            # Avoid cross-project existence leaks from helper-scoped views.
            from fastapi import HTTPException

            raise HTTPException(404, "Auth request not found")
        return _view(row)
