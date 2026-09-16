from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.server.models import Intent, ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    def __init__(self, base_url: str, timeout: float = 10.0, server_token: str | None = None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._server_token = server_token
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def bootstrap_auth_deployment(self, snapshot: dict[str, Any]) -> ApiResult:
        """Apply the Dispatcher-owned auth deployment snapshot on the Server."""
        return self._request_json(
            "POST",
            "/internal/auth/deployment",
            json=snapshot,
        )

    def create_auth_request_internal(
        self,
        project_id: str,
        source_fact_ids: list[str],
        auth_ref: str,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            "/internal/auth/requests",
            json={
                "project_id": project_id,
                "source_fact_ids": source_fact_ids,
                "auth_ref": auth_ref,
            },
        )

    def claim_auth_event(self, dispatcher_id: str) -> ApiResult:
        return self._request_json(
            "POST", "/internal/auth/events/claim", json={"dispatcher_id": dispatcher_id}
        )

    def apply_auth_event(
        self,
        event_id: str,
        dispatcher_id: str,
        *,
        operation: str,
        outcome_code: str | None = None,
    ) -> ApiResult:
        body: dict[str, Any] = {
            "event_id": event_id,
            "dispatcher_id": dispatcher_id,
            "operation": operation,
        }
        if outcome_code is not None:
            body["outcome_code"] = outcome_code
        return self._request_json("POST", "/internal/auth/events/apply", json=body)

    def recover_auth_events(self) -> ApiResult:
        return self._request_json("POST", "/internal/auth/events/recover", json={})

    def expire_auth_requests(self) -> ApiResult:
        return self._request_json("POST", "/internal/auth/requests/expire", json={})

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker},
        )

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str, worker: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json={"from": from_ids, "description": description, "creator": creator, "worker": worker},
        )

    def create_auth_request(
        self,
        project_id: str,
        source_fact_ids: list[str],
        auth_ref: str,
        role: str,
        reason: str,
        login_url: str | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/auth-requests",
            json={
                "source_fact_ids": source_fact_ids,
                "auth_ref": auth_ref,
                "role": role,
                "reason": reason,
                "login_url": login_url,
            },
        )

    def list_auth_requests(self, status: str | None = None) -> ApiResult:
        path = "/auth-requests"
        if status is not None:
            path = f"{path}?status={status}"
        return self._request_json("GET", path, json={})

    def claim_auth_request(self, request_id: str, helper_id: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/auth-requests/{request_id}/claim",
            json={"helper_id": helper_id},
        )

    def auth_request_waiting(self, request_id: str) -> ApiResult:
        return self._request_json("POST", f"/auth-requests/{request_id}/waiting", json={})

    def auth_request_verifying(self, request_id: str) -> ApiResult:
        return self._request_json("POST", f"/auth-requests/{request_id}/verifying", json={})

    def auth_request_complete(self, request_id: str) -> ApiResult:
        return self._request_json("POST", f"/auth-requests/{request_id}/complete", json={})

    def auth_request_fail(self, request_id: str, failure_reason: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/auth-requests/{request_id}/fail",
            json={"failure_reason": failure_reason},
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        if self._server_token:
            session.headers.update({"Authorization": f"Bearer {self._server_token}"})
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
