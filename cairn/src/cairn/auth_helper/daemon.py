from __future__ import annotations

import logging
import platform
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.auth_helper.client import AuthHelperClient
from cairn.auth_helper.desktop import DesktopNotifier
from cairn.auth_helper.launcher import AuthLoginLauncher
from cairn.dispatcher.protocol.client import CairnClient
from cairn.server.models import AuthRequest

LOG = logging.getLogger(__name__)


def default_helper_id() -> str:
    """A stable identifier for this desktop helper: ``hostname-username``."""
    host = socket.gethostname() or "unknown-host"
    user = ""
    try:
        import getpass

        user = getpass.getuser()
    except Exception:
        user = ""
    return f"{host}-{user}" if user else host


@dataclass(slots=True)
class AuthHelperConfig:
    server: str
    config_path: Path
    helper_id: str
    poll_interval: float = 2.0
    notification: bool = True
    auto_launch: bool = True
    max_parallel_logins: int = 1


@dataclass(slots=True)
class _ActiveLogin:
    request: AuthRequest
    process: Any | None = None


class AuthHelperDaemon:
    """Poll loop for the desktop auth helper.

    Each tick it lists ``pending`` auth requests, attempts an atomic claim (so multiple
    helpers/dispatchers never pop up duplicate browsers), then notifies the operator and
    — if enabled — launches the headed login flow. ``max_parallel_logins`` caps how many
    login browsers may be open at once.
    """

    def __init__(
        self,
        config: AuthHelperConfig,
        client: AuthHelperClient | None = None,
        notifier: DesktopNotifier | None = None,
        launcher: AuthLoginLauncher | None = None,
    ):
        self.config = config
        self._client = client or AuthHelperClient(CairnClient(config.server))
        self._notifier = notifier or DesktopNotifier(enabled=config.notification)
        self._launcher = launcher
        self._running = True
        self._active: dict[str, _ActiveLogin] = {}

    def _launcher_for(self, request: AuthRequest) -> AuthLoginLauncher:
        if self._launcher is not None:
            return self._launcher
        return AuthLoginLauncher(
            config_path=self.config.config_path,
            project_id=request.project_id,
            target=request.auth_ref,
        )

    def run_once(self) -> None:
        if not self._running:
            return
        self._reap_finished()
        if self._active_process_count() >= self.config.max_parallel_logins:
            return
        requests = self._client.list_pending()
        for request in requests:
            if not self._running:
                return
            if self._active_process_count() >= self.config.max_parallel_logins:
                return
            self._handle(request)
            self._reap_finished()

    def _active_process_count(self) -> int:
        return sum(entry.process is not None for entry in self._active.values())

    def _reap_finished(self) -> None:
        for request_id, entry in list(self._active.items()):
            if entry.process is None:
                continue
            try:
                returncode = entry.process.poll()
            except Exception as exc:
                LOG.warning("failed to poll auth login request=%s error=%s", request_id, exc)
                continue
            if returncode is not None:
                self._active.pop(request_id, None)

    def _handle(self, request: AuthRequest) -> None:
        if not self._client.claim(request.id, self.config.helper_id):
            # Another helper won the atomic claim; do not pop up a duplicate browser.
            LOG.info("auth request already claimed by another helper request=%s", request.id)
            return
        LOG.info("auth request claimed request=%s target=%s", request.id, request.auth_ref)
        self._active[request.id] = _ActiveLogin(request=request)
        self._notifier.notify_auth_required(request)
        if self.config.auto_launch:
            launcher = self._launcher_for(request)
            try:
                process = launcher.launch(request)
                self._active[request.id].process = process
                # We do not wait here: the login flow runs in its own process and will
                # drive the request to waiting_user / verifying / completed / failed via
                # the --request flag. The poll loop keeps running for other requests.
                LOG.info("auth login launched request=%s pid=%s", request.id, process.pid)
            except Exception as exc:
                LOG.warning("failed to launch auth login request=%s error=%s", request.id, exc)
                self._client.mark_fail(request.id, f"launcher error: {exc}")
                self._active.pop(request.id, None)

    def run_forever(self) -> None:
        LOG.info("auth helper starting server=%s helper=%s", self.config.server, self.config.helper_id)
        while self._running:
            try:
                self.run_once()
            except Exception as exc:
                LOG.warning("auth helper tick failed error=%s", exc)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._running = False
