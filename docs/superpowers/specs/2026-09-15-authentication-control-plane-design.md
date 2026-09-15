# Authentication Control Plane Design

**Hard boundary:** Server stores protocol records; Dispatcher is the only actor allowed to advance authentication business state or write graph facts/intents; Helper/CLI submit authenticated events only; Web UI reads a safe projection and submits constrained commands only.

## Authority and deployment boundary

- **Server** persists requests, queues, lifecycle records and read projections. Its only state mutation outside Dispatcher is atomic lease recovery which returns an abandoned queue claim to `retryable`; it never changes `AuthRequest.status`, reads AuthStore, or writes graph data.
- **Dispatcher** owns the transaction that applies an event/command: it validates identity and state, advances `AuthRequest`, appends lifecycle, marks queue result, then writes idempotent graph facts/intents. It is the sole TTL reaper.
- **Helper/CLI** owns interactive browser capture and is the only component writing a local `AuthStore`. It never imports `CairnClient` or `AuthGraphAdapter`; it submits an event after browser progress/capture and does not complete a request.
- **Dispatcher and Helper share the configured AuthStore namespace** per target. Local mode uses one absolute `auth.store_root`; container/distributed mode uses a deployment-mounted, access-controlled shared volume. Dispatcher is read-only and Helper read/write. If this cannot be guaranteed, `login_succeeded` is rejected as `store_unavailable`.
- **Web UI** has no direct transition route. It sees `AuthRequestView`; browser commands enqueue work and never contain a URL, role, verification input or secret.

`AuthTargetConfig` addressed by `auth_ref` is the only authority for role, login URL, profile, verification policy, TTL and graph descriptions. Event/command attempts to supply any of those values are `schema_rejected`.

## Canonical durable schema

`auth_events` is append-only: `id` (server-generated immutable opaque id), `project_id`, `request_id`, `auth_ref`, `kind`, `actor_id`, `idempotency_key`, `occurred_at`, `received_at`, `state`, `attempt_count`, `next_attempt_at`, `claimed_by`, `claim_expires_at`, `processed_at`, `outcome_code`. `(actor_id, idempotency_key)` is unique; the project/request/auth_ref relation is validated. `kind` is exactly `launch_requested|browser_opened|login_succeeded|login_failed`; state is exactly `queued|claimed|retryable|applied|rejected`.

The accepted event JSON is exactly `{project_id, request_id, auth_ref, kind, idempotency_key, occurred_at}`. Unknown fields cause 422. There is no payload or free-text/URL/cookie/credential/browser-error/profile-path/role metadata column.

`auth_commands` has the same queue, lease and idempotency columns. Its `kind` is `cancel|reauthenticate`; it has exactly one selector: `request_id` for cancel, or `auth_ref` for reauthenticate. Both/neither is 422. Success returns only `{command_id, state: "queued"}`.

`auth_lifecycle_events` is append-only: `request_id`, `event_id` (nullable only for `created`), `sequence`, `kind`, `recorded_at`, enum-only `outcome_code`. Its canonical replay guard is unique `(request_id, event_id, kind)`. TTL has a generated `event_id` `ttl:<request_id>:<expiry_generation>`, never a Helper event.

## Consumption, transitions, and expiry

In a `BEGIN IMMEDIATE` transaction Dispatcher selects the oldest eligible queued/retryable record and updates it to `claimed`, `claimed_by=dispatcher_instance_id`, `claim_expires_at=server_now+30s`, incrementing `attempt_count`. Only its unexpired claim can apply. A reclaimed lease becomes `retryable` with exponential server-clock backoff; three attempts ends `rejected/retry_exhausted`. Applied/rejected records are replay no-ops. Reuse of an idempotency key with different immutable data returns 409; identical data returns the original record.

| Input | Required status | Dispatcher result |
|---|---|---|
| `launch_requested` | `pending` | `claimed` |
| `browser_opened` | `claimed` | `waiting_user` |
| `login_succeeded` | `waiting_user` | `verifying`; read configured store, verify target, then `completed` + `AuthSessionVerified`, or `failed` + `AuthSessionInvalid` |
| `login_failed` | `claimed`, `waiting_user`, `verifying` | `failed` + `AuthSessionInvalid` |
| `cancel` | `pending`, `claimed`, `waiting_user`, `verifying` | `cancelled` |
| `reauthenticate(auth_ref)` | configured target | create/reuse one nonterminal target request |

Invalid ordering is terminal `rejected/invalid_transition` and leaves the request untouched. Dispatcher calculates `expires_at = created_at + target.ttl_seconds`; each loop atomically changes due nonterminal requests to `expired`, writes deterministic lifecycle, and produces `AuthSessionInvalid` only when invalidating a previously verified session. All clocks are server/Dispatcher UTC.

Graph effects use `auth-event:<event_id>` or `auth-ttl:<request_id>:<expiry_generation>` as stable source keys, checked before fact/intent creation. A retry cannot duplicate graph records.

## Endpoint and credential contract

Bearer credentials are deployment configuration, never graph/projection data. Server stores only SHA-256 token digest, actor id, scopes, project allowlist, `not_before`, `expires_at`, and `replaced_by`. Rotation adds a bounded-overlap digest; revocation sets expiry to server-now. HTTPS is required except loopback. Every endpoint requires scope and project authorization.

| Caller | Endpoint | Scope | Permission |
|---|---|---|---|
| Helper/CLI | `GET /projects/{project_id}/auth-requests/{request_id}/helper-view` | `helper.request.read` | id, auth_ref, configured login URL, status only |
| Helper/CLI | `POST /auth-events` | `helper.event.submit` | enqueue its project event |
| Dispatcher | internal claim/ack/reap | `dispatcher.auth.consume` | consume queues and mutate auth state |
| Browser UI | `GET /projects/{project_id}/auth-requests` | `ui.auth.read` | safe project projection |
| Browser UI | `POST /auth-commands` | `ui.command.submit` | queue permitted command |

Browser identity is an operator session from the deployment reverse proxy/session layer, mapped to explicit project `read`, `cancel`, `reauthenticate` permissions. Without an identity provider UI command ingress is disabled (503); no token is embedded in HTML. Cookie deployments require same-origin + CSRF defense; bearer clients use UUID idempotency keys. Authorization failures are opaque 403/404.

`AuthRequestView` is a strict allowlist: `id`, `project_id`, `source_fact_ids`, `auth_ref`, `role`, `status`, `created_at`, `claimed_at`, `completed_at`, `terminal_summary`. It excludes `login_url`, `reason`, `failure_reason`, `helper_id`, `actor_id`, `event_id`, `state_path`, and all secret-shaped values. Serializer tests assert allowlist equality and adversarial redaction.

## Exact legacy migration

`auth_control_plane_mode` is a Pydantic Server/Dispatcher setting: `legacy|dual_write|enforced`. Upgrades default legacy; fresh installs may default enforced only after parity tests pass.

| Current raw route/caller | Replacement | Enforced behavior |
|---|---|---|
| `POST /projects/{project_id}/auth-requests` from reason task | Dispatcher internal create operation | external route unavailable |
| `GET /auth-requests` from Helper | scoped helper view/query | unavailable |
| `POST /auth-requests/{id}/claim|waiting|verifying|complete|fail` from Helper/CLI | `POST /auth-events`; Dispatcher consumes | unavailable |
| `CairnClient`/`AuthGraphAdapter` in `cli.py` | store capture then success/fail event | imports/calls removed |

In legacy mode current paths remain. In dual-write, migrated actors are event-only and legacy direct calls from them are rejected; legacy actors stay legacy-only, preventing races. One lifecycle application maps to one stable graph key. Existing rows receive one created lifecycle row and terminal rows one terminal lifecycle. Rollback stops consumption but deletes no append-only data or completed state. Enforced raw mutations return 410 migration code; routes are physically removed next major version.

## Delivery and verification

1. Guarded DB migrations, schemas, scoped ingress, queue claim/ack/reap, tests.
2. Dispatcher internal service + CLI/Helper migration away from direct `CairnClient`/`AuthGraphAdapter` writes.
3. Dual-write parity proof, then enforcement.
4. Safe projection, operator command endpoint, Timeline/graph rendering, then local `cairn://` bridge.

Tests cover migrations; scope/project isolation; idempotency collision/replay; claim crash/reclaim; retry exhaustion; every transition/TTL edge; absent shared store; target-field rejection; graph deduplication; legacy races; forbidden projection keys.
