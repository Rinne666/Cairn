from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)
    auth_claim_ttl: int = Field(default=300, ge=0)
    auth_request_ttl: int = Field(default=1800, ge=0)
    auth_control_plane_mode: Literal["legacy", "dual_write", "enforced"] = "legacy"


class Fact(BaseModel):
    id: str
    description: str


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


AuthRequestStatus = Literal[
    "pending",
    "claimed",
    "waiting_user",
    "verifying",
    "completed",
    "failed",
    "cancelled",
    "expired",
]

ACTIVE_AUTH_REQUEST_STATUSES = (
    "pending",
    "claimed",
    "waiting_user",
    "verifying",
)


class AuthRequest(BaseModel):
    id: str
    project_id: str
    source_fact_ids: list[str]
    auth_ref: str
    role: str
    login_url: str | None = None
    reason: str
    status: AuthRequestStatus
    claimed_by: str | None = None
    created_at: str
    claimed_at: str | None = None
    completed_at: str | None = None
    failure_reason: str | None = None
    helper_actor_id: str | None = None
    expires_at: str | None = None
    expiry_generation: int = 1


class CreateAuthRequest(BaseModel):
    source_fact_ids: list[str] = Field(min_length=1)
    auth_ref: str
    role: str
    login_url: str | None = None
    reason: str

    @field_validator("auth_ref", "role", "reason")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("source_fact_ids")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ClaimAuthRequest(BaseModel):
    helper_id: str

    @field_validator("helper_id")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class FailAuthRequest(BaseModel):
    failure_reason: str | None = None


class CreateAuthRequestInternal(BaseModel):
    """Dispatcher-only request creation payload.

    The Dispatcher supplies config-derived role/reason data; callers using the
    public route continue to use :class:`CreateAuthRequest` during migration.
    """

    model_config = {"extra": "forbid"}

    project_id: str
    source_fact_ids: list[str] = Field(min_length=1)
    auth_ref: str

    @field_validator("project_id", "auth_ref")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("source_fact_ids")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("fact ids must not be empty")
        return cleaned


class AuthEventClaimRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dispatcher_id: str = "dispatcher"

    @field_validator("dispatcher_id")
    @classmethod
    def validate_dispatcher_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


AuthEventOperation = Literal[
    "bind_actor",
    "mark_waiting_user",
    "begin_verification",
    "mark_failed",
    "reject",
]
AuthEventOutcome = Literal[
    "verified",
    "verification_failed",
    "login_failed",
    "invalid_transition",
    "not_request_owner",
    "request_mismatch",
    "retry_exhausted",
    "expired",
    "store_unavailable",
    "capture_mismatch",
]


class AuthEventApplyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    event_id: str
    dispatcher_id: str = "dispatcher"
    operation: AuthEventOperation
    outcome_code: AuthEventOutcome | None = None

    @field_validator("event_id", "dispatcher_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class AuthEventReapRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dispatcher_id: str | None = None


AuthEventKind = Literal[
    "launch_requested",
    "browser_opened",
    "login_succeeded",
    "login_failed",
]
AuthEventState = Literal["queued", "claimed", "retryable", "applied", "rejected"]


class CreateAuthEvent(BaseModel):
    """The intentionally closed event ingress contract used by Helpers/CLI."""

    model_config = {"extra": "forbid"}

    project_id: str
    request_id: str
    auth_ref: str
    kind: AuthEventKind
    idempotency_key: UUID
    occurred_at: str
    capture_generation: int | None = Field(default=None, ge=0)

    @field_validator("project_id", "request_id", "auth_ref")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an ISO-8601 timestamp") from exc
        return text

    @model_validator(mode="after")
    def validate_capture_generation(self) -> "CreateAuthEvent":
        if self.kind == "login_succeeded" and self.capture_generation is None:
            raise ValueError("capture_generation is required for login_succeeded")
        if self.kind != "login_succeeded" and self.capture_generation is not None:
            raise ValueError("capture_generation is only valid for login_succeeded")
        return self


class AuthEvent(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    project_id: str
    request_id: str
    auth_ref: str
    kind: AuthEventKind
    actor_id: str
    idempotency_key: UUID
    occurred_at: str
    received_at: str
    state: AuthEventState
    attempt_count: int = 0
    next_attempt_at: str | None = None
    claimed_by: str | None = None
    claim_expires_at: str | None = None
    processed_at: str | None = None
    outcome_code: str | None = None
    capture_generation: int | None = None


class AuthHelperRequestView(BaseModel):
    """Strict, helper-scoped projection; never expose reason or failure details."""

    model_config = {"extra": "forbid"}

    id: str
    auth_ref: str
    login_url: str | None = None
    status: AuthRequestStatus
    # Returned only to the actor that owns a claimed request; omitted while null.
    helper_actor_id: str | None = None


class AuthCredential(BaseModel):
    model_config = {"extra": "forbid"}

    id: int
    token_digest: str
    actor_id: str
    scopes: list[str]
    project_allowlist: list[str]
    not_before: str
    expires_at: str | None = None
    replaced_by: str | None = None
    created_at: str


class AuthDeploymentSnapshot(BaseModel):
    """Dispatcher-owned deployment material used only by the internal bootstrap RPC."""

    model_config = {"extra": "forbid"}

    dispatcher_token: str | None = None
    helper_token: str | None = None
    helper_actor_id: str = "helper"
    helper_scopes: list[str] = Field(
        default_factory=lambda: ["helper.event.submit", "helper.request.read"]
    )
    helper_project_allowlist: list[str] = Field(default_factory=list)
    targets: dict[str, str] = Field(default_factory=dict)
    target_roles: dict[str, str] = Field(default_factory=dict)
    target_reasons: dict[str, str] = Field(default_factory=dict)

    @field_validator("dispatcher_token", "helper_token")
    @classmethod
    def validate_optional_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value if value else None

    @field_validator("helper_actor_id")
    @classmethod
    def validate_actor_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("helper_actor_id must not be empty")
        return value

    @field_validator("helper_scopes", "helper_project_allowlist")
    @classmethod
    def validate_string_lists(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("values must not be empty")
        return sorted(set(cleaned))

    @model_validator(mode="after")
    def validate_distinct_credentials(self) -> "AuthDeploymentSnapshot":
        if self.dispatcher_token is not None and self.dispatcher_token == self.helper_token:
            raise ValueError("dispatcher_token and helper_token must be different")
        if self.helper_token is not None and not self.helper_scopes:
            raise ValueError("helper_scopes must not be empty when helper_token is set")
        return self

    @field_validator("targets")
    @classmethod
    def validate_target_urls(cls, value: dict[str, str]) -> dict[str, str]:
        from urllib.parse import urlparse

        cleaned: dict[str, str] = {}
        for auth_ref, login_url in value.items():
            ref = auth_ref.strip()
            url = login_url.strip()
            parsed = urlparse(url)
            if not ref or parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("targets must contain non-empty auth refs and HTTPS login URLs")
            if ref in cleaned:
                raise ValueError("targets must not contain duplicate auth refs")
            cleaned[ref] = url
        return cleaned

    @field_validator("target_roles", "target_reasons")
    @classmethod
    def validate_target_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, item in value.items():
            key = key.strip()
            item = item.strip()
            if not key or not item:
                raise ValueError("target metadata must contain non-empty values")
            cleaned[key] = item
        return cleaned
