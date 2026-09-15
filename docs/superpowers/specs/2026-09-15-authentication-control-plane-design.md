# Authentication Control Plane Design

**Hard boundary:** Server stores protocol state, Dispatcher is the only business writer, Helper/CLI submit authentication events only, and Web UI reads safe state plus submits constrained commands.

## Roles

- **Server:** durable storage, additive migrations, authenticated control-plane ingress, read-only projections; never reads AuthStore or session material.
- **Dispatcher:** sole actor that creates/changes AuthRequests, creates/concludes auth Intents/Facts, validates target metadata from `DispatchConfig`, and consumes queued events/commands.
- **Helper/CLI:** local browser execution and AuthStore access only. They emit authenticated, non-secret events and never instantiate `CairnClient` protocol writes.
- **Web UI:** read-only metadata projection and constrained command submission. It cannot transition AuthRequests or choose target role/URL.

## Durable records

`auth_events` is append-only. Columns: id, project_id, request_id, kind, actor_id, idempotency_key, occurred_at, received_at, payload_version, safe_detail, processing_status (`pending|claimed|applied|rejected|retryable`), processed_at, outcome_code. A unique `(actor_id, idempotency_key)` provides deduplication. `auth_lifecycle_events` records the Dispatcher-applied timeline event: request_id, sequence, kind, occurred_at, safe_detail; uniqueness is `(request_id, kind, occurred_at)` plus Dispatcher idempotency checks.

Both tables are added in `server/db.py` with guarded additive migrations, indexed by project/request and processing state. AuthEvent payload accepts no free text, URL, role, credential, cookie, token, state path, or browser error.

### Exact queue schema and consumption

`auth_events`: `id`, `project_id`, `request_id`, `kind`, `actor_id`, `idempotency_key`, `state`, `attempt_count`, `next_attempt_at`, `claimed_by`, `claim_expires_at`, `received_at`, `processed_at`, `outcome_code`. `kind` is the closed enum above; `state` is `queued|claimed|applied|rejected|retryable`; unique `(actor_id,idempotency_key)` and foreign-key-equivalent validation binds request to project. There is no arbitrary payload column: event data is only the enum plus identifiers.

Dispatcher atomically claims the oldest eligible queued/retryable event by setting `claimed_by`, `claim_expires_at`, and incrementing attempts. A 30-second lease expiry returns it to `retryable`; three attempts terminally produce `rejected` with an allowlisted outcome code. Reprocessing any event already `applied|rejected` is a no-op. Every AuthRequest transition writes a lifecycle row keyed by `(request_id,event_id,kind)`, which—not client time—provides exactly-once projection. Server TTL reaping remains authoritative for expiry: it changes AuthRequest status once and inserts `expired` lifecycle row with deterministic `ttl:<request_id>` event key.

`auth_commands` has the same queue mechanics and columns, with closed `kind` enum `cancel|reauthenticate`. Browser requests may submit only `{idempotency_key, kind, project_id, request_id? | auth_ref?}`; server validates structure, stores a command, and returns an opaque command id. Dispatcher alone validates command actor scope, target config, deduplication and state change. Commands never write AuthRequest directly.

## Event and command matrix

| Source | Input | Dispatcher result |
|---|---|---|
| Helper | `launch_requested` | claim pending request; emit `claimed` |
| Helper | `browser_opened` | claimed → waiting_user; emit `browser_opened`, `waiting_user` |
| Helper | `login_succeeded` | waiting_user → verifying; independently verify AuthStore, then complete + AuthSessionVerified or fail + AuthSessionInvalid |
| Helper | `login_failed` | claimed/waiting/verifying → failed with fixed safe summary |
| UI command | `cancel` | Dispatcher cancels only pending/claimed/waiting_user request |
| UI command | `reauthenticate(auth_ref)` | Dispatcher validates configured target and creates/reuses request |

Invalid ordering is rejected with an allowlisted outcome code. Dispatcher claims pending events, retries `retryable` events with bounded attempts, and only marks `applied` after its state transition succeeds. TTL reaping appends `expired` lifecycle events exactly once.

## Authentication and trust

Helpers use a per-helper opaque bearer credential configured outside graph/prompt data. The credential has `auth_event.submit` scope and project allowlist. Dispatcher uses a separate internal credential for command consumption. New config fields require Pydantic validation, example entries, and tests; rotations accept current plus previous credential for a bounded overlap. Server routes are split into UI projection, Helper event ingress, and Dispatcher internal command/consume endpoints; legacy raw `auth-requests` read/write routes are deprecated, internally scoped during migration, then removed.

Concrete scopes are `helper.event.submit`, `helper.request.read`, `ui.auth.read`, `ui.command.submit`, and `dispatcher.auth.consume`. Server stores only SHA-256 token digests with actor id, scope set, project allowlist, `not_before`, `expires_at`, and optional `replaced_by`; plaintext arrives only from deployment configuration. Bearer access requires HTTPS except loopback. Rotating creates a second bounded-valid digest; revocation sets expiry to now. Every endpoint rejects actors lacking both scope and project authorization.

Helper reads only `GET /projects/{project_id}/auth-requests/{request_id}/helper-view`, a dedicated safe view containing id, configured auth_ref, configured login URL, and state; it is unavailable to UI scopes. UI reads `GET /projects/{project_id}/auth-requests` using `AuthRequestView`; required keys are `id, project_id, source_fact_ids, auth_ref, role, status, created_at, claimed_at, completed_at, terminal_summary`. Forbidden keys are `login_url, reason, failure_reason, helper_id, actor_id, event_id, state_path`, and any secret-shaped value. Serializer tests assert both key absence and redaction resistance.

## Migration

1. Add events, lifecycle records, safe projections, and scoped credentials while retaining legacy behavior behind a compatibility flag.
2. Move CLI and Helper to event emission; Dispatcher consumes events and is the only protocol writer. Block legacy direct writes by default once parity tests pass.
3. Add UI commands through Dispatcher queue, then Graph/Timeline/UI rendering. Deep-link registration and local bridge follow only after event flow is stable.

## Safety and verification

All browser-facing views use a dedicated allowlisted schema: identifiers, auth_ref, configured role, status, timestamps, and fixed summaries only. Tests cover migration upgrades, actor/project isolation, idempotency, transition order, retry/TTL behavior, credential scope/rotation, ignored LLM URL/role values, no helper protocol writes, and absence of forbidden values/keys in every UI serializer.

Migration flag `auth_control_plane_mode` is `legacy|dual_write|enforced` (default `legacy` for upgraded deployments, `enforced` for new installs after parity). In `dual_write`, Dispatcher consumes events while legacy writes remain observable for rollback; no duplicate Fact is produced because graph idempotency uses `auth-event:<event_id>` as the AuthGraph source key. Backfill creates lifecycle `created` rows only for nonterminal existing requests. Rollback changes the mode to `legacy` but never deletes append-only queue/lifecycle rows. Enforced mode rejects legacy write endpoints with a migration error and removes them in the next major protocol version.
