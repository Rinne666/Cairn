from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


def utcnow() -> str:
    """Return an ISO-8601 UTC timestamp string (matching ``cairn.server.services``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class AuthVerificationResult:
    """Outcome of independently re-validating a saved storage state.

    Mirrors the ``AuthVerificationResult`` contract from the plan. A state is ``valid``
    only when every configured evidence layer that could be checked passed. The three
    layers are:

    - ``page_ok``     -- protected URL returned the expected HTTP status.
    - ``selector_ok`` -- authenticated selector was present on the page.
    - ``api_ok``      -- authenticated API returned the expected HTTP status.

    A layer is ``None`` when it was not configured (and therefore not checked); it does
    not block validity.
    """

    valid: bool
    page_ok: bool
    selector_ok: bool
    api_ok: bool
    reason: str | None = None

    def methods(self) -> list[str]:
        """Return the list of evidence methods that passed (for the Fact verbatim)."""
        methods: list[str] = []
        if self.page_ok:
            methods.append("protected_page")
        if self.selector_ok:
            methods.append("selector")
        if self.api_ok:
            methods.append("api")
        return methods


@dataclass(slots=True)
class AuthCaptureResult:
    """Result of an interactive (human-assisted) login capture."""

    storage_state: dict[str, Any] | None
    verification: AuthVerificationResult


class AuthMeta(BaseModel):
    """Metadata written alongside a storage state.

    Deliberately stores *no* secrets: no cookies, tokens, passwords or Authorization
    headers. It records only the logical identity of the profile and the outcome of the
    last verification.
    """

    target: str
    role: str
    base_url: str
    created_at: str
    verified_at: str | None = None
    verification: dict[str, bool] = field(default_factory=dict)
