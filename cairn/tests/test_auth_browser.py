from __future__ import annotations

import sys
import types

from playwright.sync_api import Error as PlaywrightError

from cairn.auth.manager import AuthManager
from cairn.auth.models import AuthVerificationResult
from cairn.auth.verifier import AuthVerifier
from cairn.dispatcher.config import AuthTargetConfig


def _target(*, indexed_db: bool = True) -> AuthTargetConfig:
    return AuthTargetConfig.model_validate(
        {
            "name": "target",
            "base_url": "https://example.test",
            "login_url": "https://example.test/login",
            "role": "user",
            "indexed_db": indexed_db,
            "verify": {"url": "https://example.test/home"},
        }
    )


class _FakeContext:
    def __init__(self, verification: AuthVerificationResult | None = None):
        self.verification = verification
        self.storage_state_calls: list[dict] = []

    def new_page(self):
        return types.SimpleNamespace()

    def storage_state(self, **kwargs):
        self.storage_state_calls.append(kwargs)
        return {"cookies": []}

    def close(self):
        pass


def test_capture_passes_indexed_db_to_storage_state(monkeypatch) -> None:
    context = _FakeContext()
    page = types.SimpleNamespace(
        goto=lambda *args, **kwargs: types.SimpleNamespace(status=200),
        wait_for_selector=lambda *args, **kwargs: None,
        close=lambda: None,
    )
    context.new_page = lambda: page
    browser = types.SimpleNamespace(
        new_context=lambda: context,
        close=lambda: None,
    )
    playwright = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(Error=PlaywrightError, sync_playwright=lambda: _Playwright(playwright)))
    manager = AuthManager()
    manager._wait_for_verified_session = lambda *args, **kwargs: AuthVerificationResult(True, True, True, True)

    result = manager.capture_interactive(_target(indexed_db=False), login_timeout_seconds=1)

    assert result.storage_state == {"cookies": []}
    assert context.storage_state_calls == [{"indexed_db": False}]


def test_verify_storage_state_returns_invalid_on_playwright_error(monkeypatch) -> None:
    class _BrokenPlaywright:
        def __enter__(self):
            raise PlaywrightError("browser failed")

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(Error=PlaywrightError, sync_playwright=lambda: _BrokenPlaywright()))

    result = AuthVerifier().verify_storage_state(_target(), {"cookies": []})

    assert result == AuthVerificationResult(False, False, False, False, "browser failed")


def test_capture_returns_invalid_on_authoritative_playwright_error(monkeypatch) -> None:
    context = _FakeContext()
    page = types.SimpleNamespace(
        goto=lambda *args, **kwargs: types.SimpleNamespace(status=200),
        wait_for_selector=lambda *args, **kwargs: None,
        close=lambda: None,
    )
    context.new_page = lambda: page
    browser = types.SimpleNamespace(new_context=lambda: context, close=lambda: None)
    playwright = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(Error=PlaywrightError, sync_playwright=lambda: _Playwright(playwright)))

    monkeypatch.setattr(
        AuthVerifier,
        "_verify_context",
        lambda *args: (_ for _ in ()).throw(PlaywrightError("network failed")),
    )
    manager = AuthManager()

    result = manager.capture_interactive(_target(), login_timeout_seconds=1)

    assert result.verification == AuthVerificationResult(False, False, False, False, "network failed")


class _Playwright:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False
