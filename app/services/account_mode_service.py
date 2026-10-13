from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class AccountModeService:
    """Persist each Telegram user's preferred Deriv trading account mode."""

    VALID_MODES = {"demo", "real"}

    def __init__(self, storage_path: str | Path | None = None) -> None:
        if storage_path is not None:
            self._path = Path(storage_path)
        else:
            state_dir = (
                os.getenv("STATE_DIR")
                or os.getenv("YIELDS_STATE_DIR")
                or "data"
            )
            self._path = Path(state_dir) / "yields_account_modes.json"

        self._lock = threading.RLock()
        self._modes: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._modes = {}
                return
            except (OSError, json.JSONDecodeError):
                # Do not overwrite a damaged file automatically.
                raise RuntimeError("ACCOUNT_MODE_STORE_UNREADABLE")

            if not isinstance(payload, dict):
                raise RuntimeError("ACCOUNT_MODE_STORE_INVALID")

            modes: dict[str, str] = {}
            for user_id, mode in payload.items():
                normalized = str(mode).strip().lower()
                if normalized in self.VALID_MODES:
                    modes[str(user_id)] = normalized

            self._modes = modes

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(self._modes, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(self._path)

    def get_mode(self, user_id: str | int) -> str:
        """New users default to Demo without silently selecting a Real account."""
        with self._lock:
            return self._modes.get(str(user_id), "demo")

    def set_mode(self, user_id: str | int, mode: str) -> str:
        normalized = str(mode or "").strip().lower()
        if normalized not in self.VALID_MODES:
            raise ValueError("INVALID_ACCOUNT_MODE")

        key = str(user_id)
        with self._lock:
            previous = self._modes.get(key, "demo")
            self._modes[key] = normalized
            try:
                self._save()
            except Exception:
                if key in self._modes:
                    self._modes[key] = previous
                raise
            return normalized

    def clear_user(self, user_id: str | int) -> None:
        with self._lock:
            key = str(user_id)
            previous = self._modes.pop(key, None)
            try:
                self._save()
            except Exception:
                if previous is not None:
                    self._modes[key] = previous
                raise