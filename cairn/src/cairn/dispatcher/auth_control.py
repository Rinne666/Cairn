from __future__ import annotations

import logging
from dataclasses import dataclass

from cairn.dispatcher.protocol.client import ApiResult, CairnClient, ProtocolError

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class DispatcherAuthControl:
    """Dispatcher-side queue consumer for authentication events.

    This module intentionally only advances the Server request state. Graph effects
    and AuthStore verification are added by the later outbox phase.
    """

    client: CairnClient
    dispatcher_id: str = "dispatcher"

    def recover(self) -> ApiResult:
        return self.client.recover_auth_events()

    def reap(self) -> ApiResult:
        return self.client.expire_auth_requests()

    @staticmethod
    def select_operation(event: dict[str, object]) -> tuple[str, str | None]:
        """Choose the only legal storage operation for a claimed event.

        This is intentionally Dispatcher-owned policy. The Server receives the
        selected operation and only checks that it is compatible with storage.
        """
        kind = event.get("kind")
        status = event.get("request_status")
        helper_actor = event.get("helper_actor_id")
        actor = event.get("actor_id")
        if "helper_actor_id" in event and (
            (kind != "launch_requested" and actor != helper_actor)
            or (kind == "launch_requested" and status != "pending" and actor != helper_actor)
        ):
            return "reject", "not_request_owner"
        if kind == "launch_requested" and status == "pending":
            return "bind_actor", None
        if kind == "browser_opened" and status == "claimed":
            return "mark_waiting_user", None
        if kind == "login_succeeded" and status in {"waiting_user", "verifying"}:
            if status == "verifying" and "verification_event_id" in event and event.get("verification_event_id") != event.get("id"):
                return "reject", "invalid_transition"
            return "begin_verification", None
        if kind == "login_failed" and status in {"claimed", "waiting_user", "verifying"}:
            return "mark_failed", "login_failed"
        return "reject", "invalid_transition"

    def consume_once(self) -> ApiResult | None:
        claimed = self.client.claim_auth_event(self.dispatcher_id)
        if claimed.status_code == 204:
            return None
        if not claimed.ok:
            raise ProtocolError(
                f"auth event claim failed with status {claimed.status_code}",
                claimed.status_code,
                claimed.text,
            )
        event = claimed.data or {}
        if not isinstance(event, dict):
            raise ProtocolError("auth event claim returned malformed payload", 502, claimed.text)
        event_id = event.get("id")
        if not event_id:
            raise ProtocolError("auth event claim returned malformed payload", 502, claimed.text)
        operation, outcome_code = self.select_operation(event)
        apply_kwargs: dict[str, str] = {"operation": operation}
        if outcome_code is not None:
            apply_kwargs["outcome_code"] = outcome_code
        result = self.client.apply_auth_event(event_id, self.dispatcher_id, **apply_kwargs)
        if not result.ok:
            LOG.warning("auth event apply failed event=%s status=%s body=%s", event_id, result.status_code, result.text)
        return result

    def run_cycle(self) -> list[ApiResult]:
        """Recover claims, expire requests, then drain currently eligible events."""
        results = [self.recover(), self.reap()]
        if any(not result.ok for result in results):
            return results
        while True:
            result = self.consume_once()
            if result is None:
                break
            results.append(result)
            if not result.ok:
                break
        return results
