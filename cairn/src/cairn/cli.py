from pathlib import Path

import click
import uvicorn

from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.group()
def auth():
    """Manage real-environment login sessions (login / verify / list / remove)."""


@auth.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--project", "project_id", required=True, help="Project id (e.g. proj_001)")
@click.option("--target", "target_name", required=True, help="Auth target name (auth_ref)")
@click.option("--request", "request_id", required=False, help="Auth request id to drive (auth_007)")
def login(config_path: Path, project_id: str, target_name: str, request_id: str | None):
    """Open a headed browser for the operator to log in, then save the session state."""
    from cairn.auth.graph import AuthGraphAdapter
    from cairn.auth.manager import AuthManager
    from cairn.auth.models import AuthMeta, utcnow
    from cairn.auth.store import AuthStore
    from cairn.auth.verifier import AuthVerifier
    from cairn.dispatcher.config import DispatchConfig
    from cairn.dispatcher.protocol.client import CairnClient

    config = DispatchConfig.load(config_path)
    if config.auth is None:
        raise click.ClickException("dispatch config has no 'auth' section")
    try:
        target = config.auth.target(target_name)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    client = CairnClient(config.server)

    def _drive_request(method: str) -> None:
        """Advance the auth-request state machine (best effort)."""
        if request_id is None:
            return
        try:
            getattr(client, method)(request_id)
        finally:
            return

    try:
        # Claimed -> waiting_user as the operator is about to see the login window.
        if request_id is not None:
            _drive_request("auth_request_waiting")

        store = AuthStore(Path(config.auth.store_root))
        manager = AuthManager()
        result = manager.capture_interactive(
            target,
            login_timeout_seconds=config.auth.login_timeout,
            verify_timeout_seconds=config.auth.verify_timeout,
            on_status=lambda msg: click.echo(f"[auth] {msg}"),
        )
        if result.storage_state is None:
            if request_id is not None:
                _drive_request("auth_request_fail")
            raise click.ClickException("login did not produce a verified session")

        # Re-verify the freshly saved state independently before trusting it.
        state_path = store.write_state(project_id, target.name, result.storage_state)
        click.echo(f"[auth] saved storage state: {state_path}")

        if request_id is not None:
            _drive_request("auth_request_verifying")

        verifier = AuthVerifier(timeout_ms=config.auth.verify_timeout * 1000)
        verification = verifier.verify_storage_state(target, result.storage_state)
        if not verification.valid:
            if request_id is not None:
                _drive_request("auth_request_fail")
            raise click.ClickException(f"re-verification failed: {verification.reason}")

        store.write_meta(
            project_id,
            target.name,
            AuthMeta(
                target=target.name,
                role=target.role,
                base_url=target.base_url,
                created_at=utcnow(),
                verified_at=utcnow(),
                verification={
                    "page": verification.page_ok,
                    "selector": verification.selector_ok,
                    "api": verification.api_ok,
                },
            ),
        )

        # Publish the verified-session Fact via the normal Intent -> conclude protocol.
        adapter = AuthGraphAdapter(client)
        intent_id = adapter.verified(project_id, target, methods=verification.methods())
        if request_id is not None:
            _drive_request("auth_request_complete")
        click.echo(f"[auth] AuthSessionVerified fact recorded (intent={intent_id})")
        click.echo(f"[auth] target={target.name} role={target.role} verification={'+'.join(verification.methods())}")
    finally:
        client.close()


@auth.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--project", "project_id", required=True, help="Project id (e.g. proj_001)")
@click.option("--target", "target_name", required=True, help="Auth target name (auth_ref)")
def verify(config_path: Path, project_id: str, target_name: str):
    """Load an existing saved session state and re-verify it is still valid."""
    from cairn.auth.graph import AuthGraphAdapter, INVALID_REASON
    from cairn.auth.models import AuthMeta, utcnow
    from cairn.auth.store import AuthStore
    from cairn.auth.verifier import AuthVerifier
    from cairn.dispatcher.config import DispatchConfig
    from cairn.dispatcher.protocol.client import CairnClient

    config = DispatchConfig.load(config_path)
    if config.auth is None:
        raise click.ClickException("dispatch config has no 'auth' section")
    try:
        target = config.auth.target(target_name)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    store = AuthStore(Path(config.auth.store_root))
    try:
        storage_state = store.load_state(project_id, target.name)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    verifier = AuthVerifier(timeout_ms=config.auth.verify_timeout * 1000)
    verification = verifier.verify_storage_state(target, storage_state)

    client = CairnClient(config.server)
    try:
        adapter = AuthGraphAdapter(client)
        if verification.valid:
            intent_id = adapter.verified(project_id, target, methods=verification.methods())
            click.echo(f"[auth] AuthSessionVerified fact recorded (intent={intent_id})")
            click.echo(f"[auth] session valid target={target.name} role={target.role} verification={'+'.join(verification.methods())}")
        else:
            evidence = INVALID_REASON
            intent_id = adapter.invalid(project_id, target, evidence=evidence)
            click.echo(f"[auth] AuthSessionInvalid fact recorded (intent={intent_id})")
            click.echo(f"[auth] session invalid target={target.name} evidence={evidence}")
    finally:
        client.close()

    # Refresh meta with the latest verification outcome.
    try:
        meta = store.load_meta(project_id, target.name)
    except FileNotFoundError:
        meta = AuthMeta(
            target=target.name,
            role=target.role,
            base_url=target.base_url,
            created_at=utcnow(),
        )
    meta.verified_at = utcnow()
    meta.verification = {
        "page": verification.page_ok,
        "selector": verification.selector_ok,
        "api": verification.api_ok,
    }
    store.write_meta(project_id, target.name, meta)


@auth.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--project", "project_id", required=True, help="Project id (e.g. proj_001)")
def list(config_path: Path, project_id: str):
    """List saved auth profiles for a project."""
    from cairn.auth.store import AuthStore
    from cairn.dispatcher.config import DispatchConfig

    config = DispatchConfig.load(config_path)
    if config.auth is None:
        raise click.ClickException("dispatch config has no 'auth' section")
    store = AuthStore(Path(config.auth.store_root))
    refs = store.list_profiles(project_id)
    if not refs:
        click.echo("[auth] no saved auth profiles")
        return
    for ref in refs:
        try:
            meta = store.load_meta(project_id, ref)
            click.echo(f"{ref}\trole={meta.role}\tverified_at={meta.verified_at}")
        except FileNotFoundError:
            click.echo(f"{ref}\t(no meta)")


@auth.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--project", "project_id", required=True, help="Project id (e.g. proj_001)")
@click.option("--target", "target_name", required=True, help="Auth target name (auth_ref)")
def remove(config_path: Path, project_id: str, target_name: str):
    """Remove a saved auth profile for a project."""
    from cairn.auth.store import AuthStore
    from cairn.dispatcher.config import DispatchConfig

    config = DispatchConfig.load(config_path)
    if config.auth is None:
        raise click.ClickException("dispatch config has no 'auth' section")
    store = AuthStore(Path(config.auth.store_root))
    removed = store.remove_profile(project_id, target_name)
    click.echo(f"[auth] {'removed' if removed else 'not found'}: {target_name}")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    db.configure(Path(db_path))
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option(
    "--server",
    default="http://localhost:8000",
    show_default=True,
    help="Cairn server base URL",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path (contains the auth targets)",
)
@click.option("--helper-name", "helper_id", default=None, help="Helper identifier (default hostname-username)")
@click.option("--poll-interval", type=float, default=2.0, show_default=True, help="Poll interval in seconds")
@click.option("--notification/--no-notification", default=True, show_default=True, help="Enable desktop notifications")
@click.option("--auto-launch/--no-auto-launch", default=True, show_default=True, help="Auto-launch the headed login browser")
@click.option("--max-parallel", "max_parallel", type=int, default=1, show_default=True, help="Max concurrent login browsers")
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def auth_helper(
    server: str,
    config_path: Path,
    helper_id: str | None,
    poll_interval: float,
    notification: bool,
    auto_launch: bool,
    max_parallel: int,
    log_level: str,
):
    """Run the desktop auth helper (polls pending auth requests and launches login)."""
    from cairn.auth_helper.daemon import (
        AuthHelperConfig,
        AuthHelperDaemon,
        default_helper_id,
    )

    configure_logging(log_level)
    config = AuthHelperConfig(
        server=server,
        config_path=config_path,
        helper_id=helper_id or default_helper_id(),
        poll_interval=poll_interval,
        notification=notification,
        auto_launch=auto_launch,
        max_parallel_logins=max_parallel,
    )
    daemon = AuthHelperDaemon(config)
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        daemon.stop()
