# Graph-native Authentication UI Design

**Goal:** Surface the existing authentication lifecycle inside Cairn’s Project graph, details panel, timeline, and project settings without exposing secrets or creating a separate authentication product.

## Scope and compatibility

This design supersedes the older deferral of authentication Web UI work to a later version. It retains the existing AuthRequest state machine, Auth Helper, interactive browser capture, AuthSession facts, and fixed three task types. AuthRequest remains coordination state, not a Fact or Intent; the UI renders it as a virtual graph node.

P0 excludes sessionStorage, persistent browser contexts, session freshness policies, WebAuthn, credential management, and remote browser control.

## Data and API design

Add a project-scoped metadata-only read endpoint, `GET /projects/{project_id}/auth-requests`, returning requests for that project after normal TTL reaping. It returns only existing AuthRequest fields: id, project_id, source_fact_ids, auth_ref, role, login_url, reason, status, timestamps, and safe failure reason. It never reads AuthStore files.

Add project-scoped actions only where the server can safely coordinate state:

- cancel an active request using its existing server-side state transition;
- create/reuse a reauthentication request only from configured, known auth metadata supplied by Dispatcher-owned configuration, not arbitrary browser input.

The initial UI action is deliberately not a Server "launch browser" endpoint. `Open Login` navigates to `cairn://auth/<request_id>`; a registered local Helper validates the request against the Server and launches its local Chromium flow. If the protocol is unavailable, the UI shows a non-secret fallback instruction instead of pretending that Server can control the operator’s device.

## UI design

The existing Alpine/Cytoscape single-page frontend remains unchanged in technology and visual language.

- Build Cytoscape elements from facts, intents, and active AuthRequests. An AuthRequest is a virtual `auth_required` node, connected from `source_fact_ids`, styled from existing amber/blue/red palettes according to `pending`, `claimed`, `waiting_user`, `verifying`, `failed`, `expired`, or `cancelled`.
- Render AuthSessionVerified and AuthSessionInvalid as semantic Fact labels/details while preserving their normal Fact storage and graph edges.
- Extend the existing Details tab with an Authentication section: target, role, source facts, safe reason, status, timestamps, verification methods, and status-appropriate Open Login / Retry / Cancel actions. No Cookie, token, storage path, raw state, or raw verifier error is rendered.
- Extend the existing Timeline projection with AuthRequest lifecycle events and add a compact project-level action-required indicator. Poll project plus project-scoped AuthRequests on the existing refresh cadence; completed virtual nodes disappear while the resulting Fact remains.
- Add a metadata-only Authentication section to project settings, summarizing the current graph-derived state for each known auth_ref.

## Runtime flow

```text
Reason → AuthRequest → virtual Auth Required node → cairn:// helper handoff
      → interactive login → AuthSessionVerified/Invalid Fact → graph refresh → explore continues
```

The existing Dispatcher creates requests and the existing Auth Helper completes login. The UI does not write secrets and does not replace Dispatcher scheduling.

## Error handling and security

The UI maps backend state directly and displays only the fixed safe failed message already used by AuthSessionInvalid. Unavailable deep-link handlers yield a local-helper-not-connected message. API serializers and frontend rendering must remain metadata-only; server-side validation rejects state transitions invalid for the current request status.

## Verification

- API tests: project isolation, TTL reaping, state transitions, and metadata-only serialization.
- Static UI tests: virtual node mapping, details actions, timeline events, deep-link fallback, and no secret-bearing fields.
- Existing full pytest suite remains green; manually inspect the Graph with fixture data for visual consistency.
