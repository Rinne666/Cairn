from __future__ import annotations

from pathlib import Path

from cairn.auth_helper.client import AuthHelperClient
from cairn.auth_helper.daemon import (
    AuthHelperConfig,
    AuthHelperDaemon,
    default_helper_id,
)
from cairn.auth_helper.desktop import DesktopNotifier
from cairn.server.models import AuthRequest


def _request(request_id: str = "auth_001") -> AuthRequest:
    return AuthRequest(
        id=request_id,
        project_id="proj_001",
        source_fact_ids=["f001"],
        auth_ref="target-user",
        role="user",
        reason="orders need auth",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
    )


class _FakeHelperClient:
    def __init__(self, pending: list[AuthRequest] | None = None) -> None:
        self.pending = pending or []
        self.claimed: list[tuple[str, str]] = []
        self.completed: list[str] = []
        self.failed: list[tuple[str, str | None]] = []
        self._claim_result = True

    def list_pending(self) -> list[AuthRequest]:
        return list(self.pending)

    def claim(self, request_id: str, helper_id: str) -> bool:
        self.claimed.append((request_id, helper_id))
        return self._claim_result

    def mark_waiting(self, request_id: str) -> bool:
        return True

    def mark_verifying(self, request_id: str) -> bool:
        return True

    def mark_complete(self, request_id: str) -> bool:
        self.completed.append(request_id)
        return True

    def mark_fail(self, request_id: str, reason: str | None = None) -> bool:
        self.failed.append((request_id, reason))
        return True


class _FakeNotifier(DesktopNotifier):
    def __init__(self) -> None:
        super().__init__(enabled=True)
        self.notified: list[AuthRequest] = []

    def notify_auth_required(self, request: AuthRequest) -> None:
        self.notified.append(request)


class _FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[AuthRequest] = []

    def launch(self, request: AuthRequest):
        self.launched.append(request)
        return _FakeProcess()


class _FakeProcess:
    pid = 1234


def _config(tmp_path: Path) -> AuthHelperConfig:
    return AuthHelperConfig(
        server="http://localhost:8000",
        config_path=tmp_path / "dispatch.yaml",
        helper_id="desktop-test",
        auto_launch=False,
    )


def test_default_helper_id_is_hostname_username() -> None:
    helper_id = default_helper_id()
    assert helper_id
    assert "unknown" not in helper_id or helper_id == "unknown-host"


def test_daemon_claims_and_notifies(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request()])
    notifier = _FakeNotifier()
    daemon = AuthHelperDaemon(
        _config(tmp_path),
        client=client,
        notifier=notifier,
        launcher=_FakeLauncher(),
    )

    daemon.run_once()

    assert client.claimed == [("auth_001", "desktop-test")]
    assert len(notifier.notified) == 1
    assert notifier.notified[0].auth_ref == "target-user"


def test_daemon_auto_launch(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request()])
    launcher = _FakeLauncher()
    config = _config(tmp_path)
    config.auto_launch = True
    daemon = AuthHelperDaemon(
        config,
        client=client,
        notifier=_FakeNotifier(),
        launcher=launcher,
    )

    daemon.run_once()

    assert len(launcher.launched) == 1
    assert launcher.launched[0].id == "auth_001"


def test_daemon_skips_when_claim_lost(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request()])
    client._claim_result = False
    launcher = _FakeLauncher()
    daemon = AuthHelperDaemon(
        _config(tmp_path),
        client=client,
        notifier=_FakeNotifier(),
        launcher=launcher,
    )

    daemon.run_once()

    # Claim failed -> no notification, no launch (avoids duplicate popups).
    assert client.claimed == [("auth_001", "desktop-test")]
    assert len(launcher.launched) == 0


def test_daemon_respects_max_parallel(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request("auth_001"), _request("auth_002")])
    launcher = _FakeLauncher()
    config = _config(tmp_path)
    config.auto_launch = True
    config.max_parallel_logins = 1
    daemon = AuthHelperDaemon(
        config,
        client=client,
        notifier=_FakeNotifier(),
        launcher=launcher,
    )

    daemon.run_once()

    # Only one request handled per tick when max_parallel_logins == 1.
    assert len(launcher.launched) == 1


def test_notifier_format_is_readable() -> None:
    message = DesktopNotifier._format(_request())
    assert "Cairn requires authentication" in message
    assert "Project: proj_001" in message
    assert "Target: target-user" in message
    assert "Role: user" in message
