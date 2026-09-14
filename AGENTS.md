# AGENTS.md

## What this is

Cairn is a blackboard-architecture problem-solving engine (validated on AI penetration testing). A shared Fact/Intent/Hint graph grows from `origin` toward `goal`; a dispatcher schedules tasks to agent workers that coordinate only through the graph. Two deployable components:

- **Cairn Server** (`cairn/src/cairn/server/`) — FastAPI + SQLite. Protocol source of truth: projects, facts, intents, hints, settings. No reasoning logic.
- **Cairn Dispatcher** (`cairn/src/cairn/dispatcher/`) — reads the graph, decides which task to schedule, manages worker runtimes, and is the **sole writer** to the server protocol.

## Layout

- `cairn/` — the only Python package (uv project). `src/cairn/server/`, `src/cairn/dispatcher/`, `src/cairn/auth/` + `src/cairn/auth_helper/` (login-session capture/verification), `src/cairn/cli.py` (entrypoint: `cairn serve|dispatch|auth|auth-helper`).
- `cairn/tests/` — pytest suite (160 tests, fast, no Docker or live LLMs needed).
- `cairn/src/cairn/dispatcher/prompts/{default,mock}/` — task prompts as markdown; selected via `runtime.prompt_group`. The `mock` group is JSON-driven and used by the test suite — keep it working when changing prompt/task contracts.
- `container/` — Kali worker image (Dockerfile). Its `AGENTS.md` is **baked into the image** as the worker agents' environment instructions — do not confuse it with this file. CI (`.github/workflows/build-container-ghcr.yml`) rebuilds the GHCR image on any push touching `container/**`.
- `docs/specs/` — design docs (Chinese): `server-protocol.md` and `dispatcher-design.md`. **Read both before changing the protocol, scheduler, or task lifecycle.**
- Root-level Chinese markdown docs describe the auth feature design and compliance reports — read them before touching `auth/`/`auth_helper/`.
- `dispatch.yaml` is the live dispatcher config (gitignored, contains real API keys). Edit `dispatch.example.yaml` / `dispatch.local.example.yaml` instead and note that `dispatch.yaml` must be recreated locally from them.

## Commands

Run from the repo root (Windows Git Bash works; deployment targets macOS/Linux):

```bash
# Fast regression suite (no Docker, no live endpoints)
uv run --project cairn --group dev pytest

# Single file / test
uv run --project cairn --group dev pytest cairn/tests/test_scheduler_logic.py -k name

# Server (default SQLite: ~/.local/share/cairn/cairn.db)
uv run --project cairn cairn serve

# Dispatcher (container mode by default; local mode via runtime.execution: local)
uv run --project cairn cairn dispatch --config dispatch.yaml

# Startup healthchecks only
uv run --project cairn cairn dispatch --config dispatch.yaml --startup-healthcheck-only
```

No lint/typecheck tooling is configured — don't invent config, just keep the existing style.

## Architecture rules

- **Dispatcher writes, agents don't.** Agent workers only receive a prompt and return structured output. Never let agents claim intents, heartbeat, or call the Cairn API directly; all protocol calls go through `dispatcher/protocol/client.py` from dispatcher code.
- **Task types are fixed at three**: `bootstrap` (initial direct solve attempt), `reason` (is the goal met / propose intents; single-flight per project via a server-side lease), `explore` (execute one claimed intent, produce one Fact). `bootstrap` and `explore` are two-phase: main execution, then a `*_conclude` fallback on the same session after timeout/parse failure.
- **Server keeps graph consistency only.** Facts are append-only; state changes are new facts, never edits. Project-level coordination state (`Project.reason` lease) is not part of the graph.
- **Config is pydantic-validated** (`dispatcher/config.py`). New YAML knobs need a model field, an example-config entry, and test coverage (`test_config_and_adapters.py`).
- **DB changes**: schema + migrations all live in `server/db.py` as additive, `PRAGMA table_info`-guarded `ALTER TABLE` steps. Add a migration there, not a fresh schema — `test_db_migrations.py` verifies upgrades from old databases.

## Conventions

- Python ≥ 3.12, pydantic v2, `from __future__ import annotations` at top of every module, modern unions (`str | None`).
- uv-managed with the Aliyun PyPI mirror (`[[tool.uv.index]]` in `cairn/pyproject.toml`) — expect region-specific package resolution.
- Worker backends are adapters under `dispatcher/workers/adapters/` (`claudecode`, `codex`, `pi`, `mock`); a new backend means a new adapter + `WorkerType` literal + env-key entry in `config.py`.
- Design docs, specs, and prompt prose are largely Chinese; code, identifiers, and commit messages are English (conventional-commit style: `feat:`, `fix:`, `chore:`, `test:`, `docs:`).

## Security constraint (auth feature)

Login credentials and session secrets live only in the execution environment's auth store. **Secrets must never be written into facts, intents, hints, or agent prompts** — only `AuthSessionVerified` / `AuthSessionInvalid` facts enter the graph. Auth targets are configured under `auth:` in the dispatch config (see `dispatch.example.yaml`).
