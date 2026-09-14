from __future__ import annotations

from dataclasses import dataclass

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.server.models import AuthRequest


@dataclass(slots=True)
class AuthHelperClient:
    """Thin wrapper over ``CairnClient`` exposing the auth-request operations the
    desktop helper needs. Kept separate so the helper can be unit-tested with a fake
    without importing the full protocol client.
    """

    client: CairnClient

    def list_pending(self) -> list[AuthRequest]:
        response = self.client.list_auth_requests(status="pending")
        if not response.ok or not isinstance(response.data, list):
            return []
        result: list[AuthRequest] = []
        for item in response.data:
            if isinstance(item, dict):
                try:
                    result.append(AuthRequest.model_validate(item))
                except Exception:
                    continue
        return result

    def claim(self, request_id: str, helper_id: str) -> bool:
        response: ApiResult = self.client.claim_auth_request(request_id, helper_id)
        return response.ok

    def mark_waiting(self, request_id: str) -> bool:
        return self.client.auth_request_waiting(request_id).ok

    def mark_verifying(self, request_id: str) -> bool:
        return self.client.auth_request_verifying(request_id).ok

    def mark_complete(self, request_id: str) -> bool:
        return self.client.auth_request_complete(request_id).ok

    def mark_fail(self, request_id: str, reason: str | None = None) -> bool:
        return self.client.auth_request_fail(request_id, reason).ok
