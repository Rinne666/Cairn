from __future__ import annotations

import json
import os
from pathlib import Path

from cairn.auth.models import AuthMeta


class PathTraversalError(ValueError):
    """Raised when a project id or auth ref attempts to escape the auth store root."""


class AuthStore:
    """Manages the on-disk layout of project-scoped auth profiles.

    Layout::

        <root>/
        └── <project_id>/
            └── <auth_ref>/
                ├── state.json   (Playwright storage state)
                └── meta.json    (non-secret profile metadata)

    ``project_id`` and ``auth_ref`` are sanitized so they can never escape the store
    root via ``../``, absolute paths or backslashes.
    """

    STATE_FILENAME = "state.json"
    META_FILENAME = "meta.json"

    def __init__(self, root: Path):
        self.root = Path(root)

    # -- path computation -------------------------------------------------
    def project_dir(self, project_id: str) -> Path:
        return self.root / self._sanitize_component(project_id)

    def profile_dir(self, project_id: str, auth_ref: str) -> Path:
        return self.project_dir(project_id) / self._sanitize_component(auth_ref)

    def state_file(self, project_id: str, auth_ref: str) -> Path:
        return self.profile_dir(project_id, auth_ref) / self.STATE_FILENAME

    def meta_file(self, project_id: str, auth_ref: str) -> Path:
        return self.profile_dir(project_id, auth_ref) / self.META_FILENAME

    # -- profile management ------------------------------------------------
    def profile_exists(self, project_id: str, auth_ref: str) -> bool:
        return self.state_file(project_id, auth_ref).is_file()

    def ensure_project_dir(self, project_id: str) -> Path:
        directory = self.project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def ensure_profile_dir(self, project_id: str, auth_ref: str) -> Path:
        directory = self.profile_dir(project_id, auth_ref)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def list_profiles(self, project_id: str) -> list[str]:
        """Return the sorted list of auth refs that have a saved ``state.json``."""
        directory = self.project_dir(project_id)
        if not directory.is_dir():
            return []
        refs: list[str] = []
        for child in directory.iterdir():
            if child.is_dir() and (child / self.STATE_FILENAME).is_file():
                refs.append(child.name)
        return sorted(refs)

    # -- persistence -------------------------------------------------------
    def write_state(self, project_id: str, auth_ref: str, storage_state: dict) -> Path:
        path = self.state_file(project_id, auth_ref)
        self.ensure_profile_dir(project_id, auth_ref)
        self._atomic_write(path, json.dumps(storage_state, ensure_ascii=False))
        self._restrict_permissions(path)
        return path

    def load_state(self, project_id: str, auth_ref: str) -> dict:
        path = self.state_file(project_id, auth_ref)
        if not path.is_file():
            raise FileNotFoundError(f"auth state not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def write_meta(self, project_id: str, auth_ref: str, meta: AuthMeta) -> Path:
        path = self.meta_file(project_id, auth_ref)
        self.ensure_profile_dir(project_id, auth_ref)
        self._atomic_write(path, meta.model_dump_json(indent=2))
        return path

    def load_meta(self, project_id: str, auth_ref: str) -> AuthMeta:
        path = self.meta_file(project_id, auth_ref)
        if not path.is_file():
            raise FileNotFoundError(f"auth meta not found: {path}")
        return AuthMeta.model_validate_json(path.read_text(encoding="utf-8"))

    def remove_profile(self, project_id: str, auth_ref: str) -> bool:
        directory = self.profile_dir(project_id, auth_ref)
        if not directory.is_dir():
            return False
        # Best-effort removal of the profile directory contents only (never parents).
        for child in sorted(directory.iterdir(), reverse=True):
            try:
                if child.is_dir():
                    child.rmdir()
                else:
                    child.unlink()
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass
        return not directory.exists()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _sanitize_component(value: str) -> str:
        """Reject path components that could escape the store root.

        Rules enforced by the plan: reject ``../``, absolute paths (``/`` prefix or
        Windows drive letter), and backslashes (which are separators on Windows and
        can smuggle traversal on POSIX).
        """
        if not value or not value.strip():
            raise PathTraversalError("project id / auth ref must not be empty")
        text = value.strip()
        if "/" in text or "\\" in text:
            raise PathTraversalError(f"invalid path component: {value!r}")
        if text in (".", ".."):
            raise PathTraversalError(f"invalid path component: {value!r}")
        if text.startswith(".") and ("/" in text or "\\" in text):
            raise PathTraversalError(f"invalid path component: {value!r}")
        # Reject anything that resolves to an absolute path.
        candidate = text.replace("\\", "/")
        if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
            raise PathTraversalError(f"invalid path component: {value!r}")
        return text

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        """Best-effort chmod 600 for the secret state file (no-op on Windows)."""
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
