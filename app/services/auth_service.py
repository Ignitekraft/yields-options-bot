from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.services.deriv.websocket_connection import (
    defer_deriv_requests_after_rate_limit,
    deriv_account_scope,
    wait_for_deriv_request_sync,
)
from app.services.oauth_credential_store import SupabaseOAuthCredentialStore


DERIV_OPTIONS_ACCOUNTS_URL = (
    "https://api.derivws.com/trading/v1/options/accounts"
)


def _resolve_state_dir() -> str:
    candidates = []

    for env_name in ("YIELDS_STATE_DIR", "APP_STATE_DIR", "STATE_DIR"):
        env_value = os.getenv(env_name)
        if env_value:
            candidates.append(env_value)

    candidates.extend(
        [
            "/data/yields_state",
            "/data",
            "/var/lib/render/project/.yields_state",
            "/mnt/data/yields_state",
            str(Path.cwd() / ".yields_state"),
            str(Path(__file__).resolve().parents[2] / ".yields_state"),
            str(Path("/tmp") / "yields_state"),
        ]
    )

    for candidate in candidates:
        try:
            path = Path(candidate).expanduser()
            path.mkdir(parents=True, exist_ok=True)

            probe = path / ".yields_state_write_test"
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")

            probe.unlink(missing_ok=True)
            return str(path)
        except Exception:
            continue

    fallback = Path.cwd() / ".yields_state"
    fallback.mkdir(parents=True, exist_ok=True)
    return str(fallback)


STATE_DIR = _resolve_state_dir()


def _ensure_state_dir() -> Path:
    path = Path(STATE_DIR)

    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        fallback = Path.cwd() / ".yields_state"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    return path


def _state_file(filename: str) -> str:
    return str(_ensure_state_dir() / filename)


STATE_FILE = _state_file("yields_oauth_states.json")
TOKEN_FILE = _state_file("yields_token_store.json")
TOKEN_COUNT_FILE = _state_file("yields_user_tokens.json")
logger = logging.getLogger(__name__)


class AuthService:
    """Manage Deriv OAuth, persisted tokens, and account metadata."""

    def __init__(
        self,
        credential_store: SupabaseOAuthCredentialStore | None = None,
    ) -> None:
        self.credential_store = (
            credential_store or SupabaseOAuthCredentialStore()
        )
        if (
            self.credential_store.is_supabase_configured
            and not self.credential_store.is_enabled
        ):
            logger.warning(
                "Encrypted Supabase OAuth credential persistence is unavailable; "
                "set DERIV_TOKEN_ENCRYPTION_KEY to enable durable token storage."
            )

    @staticmethod
    def _expiry_time(expires_in: Any) -> str | None:
        try:
            seconds = float(expires_in)
        except (TypeError, ValueError):
            return None
        if seconds <= 0:
            return None
        return (
            datetime.now(timezone.utc) + timedelta(seconds=seconds)
        ).isoformat()

    def _remove_local_token_record(self, user_key: str) -> None:
        tokens = self._load_json(TOKEN_FILE)
        if user_key in tokens:
            del tokens[user_key]
            self._save_json(TOKEN_FILE, tokens)
            if user_key in self._load_json(TOKEN_FILE):
                raise RuntimeError("LEGACY_TOKEN_CLEANUP_FAILED")

    def _save_credentials(
        self,
        user_key: str,
        credentials: Dict[str, Any],
    ) -> None:
        if self.credential_store.is_enabled:
            self.credential_store.save(user_key, credentials)
            self._remove_local_token_record(user_key)
            return

        tokens = self._load_json(TOKEN_FILE)
        tokens[user_key] = credentials
        self._save_json(TOKEN_FILE, tokens)

    def _load_credentials(self, user_key: str) -> Dict[str, Any] | None:
        if self.credential_store.is_enabled:
            credentials = self.credential_store.load(user_key)
            if credentials is not None:
                return credentials

            # Migrate credentials from older releases that stored them locally.
            legacy_tokens = self._load_json(TOKEN_FILE)
            legacy_record = legacy_tokens.get(user_key)
            if isinstance(legacy_record, dict):
                self.credential_store.save(user_key, legacy_record)
                self._remove_local_token_record(user_key)
                return legacy_record
            return None

        tokens = self._load_json(TOKEN_FILE)
        record = tokens.get(user_key)
        return record if isinstance(record, dict) else None

    def _refresh_access_token(
        self,
        user_key: str,
        credentials: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        refresh_token = credentials.get("refresh_token")
        client_id = settings.deriv_app_id or settings.deriv_client_id
        if not isinstance(refresh_token, str) or not refresh_token or not client_id:
            return None

        request_data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if settings.deriv_client_secret:
            request_data["client_secret"] = settings.deriv_client_secret

        try:
            wait_for_deriv_request_sync(deriv_account_scope(refresh_token))
            response = httpx.post(
                "https://auth.deriv.com/oauth2/token",
                data=request_data,
                timeout=20.0,
            )
            response.raise_for_status()
            refreshed_payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 429
            ):
                defer_deriv_requests_after_rate_limit(exc)
            raise RuntimeError("DERIV_TOKEN_REFRESH_FAILED") from exc

        refreshed_access_token = (
            refreshed_payload.get("access_token")
            if isinstance(refreshed_payload, dict)
            else None
        )
        if not isinstance(refreshed_access_token, str) or not refreshed_access_token:
            raise RuntimeError("INVALID_DERIV_TOKEN_REFRESH_RESPONSE")

        updated = dict(credentials)
        updated.update(refreshed_payload)
        updated["refresh_token"] = (
            refreshed_payload.get("refresh_token") or refresh_token
        )
        updated["expires_at"] = self._expiry_time(
            refreshed_payload.get("expires_in")
        )
        self._save_credentials(user_key, updated)
        return updated

    def _load_json(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}

        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_json(self, path: str, data: dict) -> None:
        try:
            _ensure_state_dir()
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
        except OSError:
            # Do not expose tokens or credential contents in logs.
            return

    def begin_oauth_session(
        self,
        telegram_user_id: str | int,
    ) -> Dict[str, str]:
        user_key = str(telegram_user_id)

        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)

        code_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(
                    code_verifier.encode("utf-8")
                ).digest()
            )
            .rstrip(b"=")
            .decode("utf-8")
        )

        states = self._load_json(STATE_FILE)
        states[user_key] = {
            "state": state,
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
        }

        self._save_json(STATE_FILE, states)

        return {
            "state": state,
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
        }

    def validate_state(
        self,
        telegram_user_id: str | int,
        state: str,
    ) -> bool:
        states = self._load_json(STATE_FILE)
        session = states.get(str(telegram_user_id), {})

        return (
            bool(session.get("state"))
            and secrets.compare_digest(session["state"], state)
        )

    def get_pending_user_for_state(
        self,
        state: str,
    ) -> Optional[str]:
        states = self._load_json(STATE_FILE)

        for telegram_user_id, session in states.items():
            stored_state = session.get("state")

            if (
                isinstance(stored_state, str)
                and secrets.compare_digest(stored_state, state)
            ):
                return telegram_user_id

        return None

    @staticmethod
    def _normalize_accounts(payload: Any) -> list[dict[str, Any]]:
        """Normalize the common account-list response shapes."""
        if isinstance(payload, list):
            return [
                item for item in payload
                if isinstance(item, dict)
            ]

        if not isinstance(payload, dict):
            return []

        for key in ("data", "accounts", "account_list"):
            value = payload.get(key)

            if isinstance(value, list):
                return [
                    item for item in value
                    if isinstance(item, dict)
                ]

            if isinstance(value, dict):
                # Some APIs wrap the list inside another object.
                for nested_key in ("data", "accounts", "account_list"):
                    nested = value.get(nested_key)

                    if isinstance(nested, list):
                        return [
                            item for item in nested
                            if isinstance(item, dict)
                        ]

                if any(
                    value.get(key)
                    for key in ("account_id", "loginid", "id")
                ):
                    return [value]

        return []

    @staticmethod
    def _account_id(account: dict[str, Any]) -> Optional[str]:
        value = (
            account.get("account_id")
            or account.get("loginid")
            or account.get("id")
        )

        return str(value) if value else None

    @staticmethod
    def _account_type(account: dict[str, Any]) -> str:
        value = (
            account.get("account_type")
            or account.get("type")
            or ""
        )

        return str(value).strip().lower()

    @classmethod
    def _select_demo_account(
        cls,
        accounts: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """
        Select Demo explicitly.

        Never silently fall back to Real for trading-related metadata.
        """
        for account in accounts:
            if not cls._account_id(account):
                continue

            account_type = cls._account_type(account)

            if account_type in {"demo", "virtual", "practice"}:
                return account

        return None

    def _hydrate_account_metadata(
        self,
        access_token: str,
        existing: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch account metadata and prefer Demo.

        If Deriv cannot provide a verified Demo account, retain the
        existing metadata for compatibility, but callers must not
        assume it represents a Demo trading account.
        """
        if not access_token:
            return existing

        try:
            wait_for_deriv_request_sync(deriv_account_scope(access_token))
            response = httpx.get(
                DERIV_OPTIONS_ACCOUNTS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=20.0,
            )

            if response.status_code != 200:
                if response.status_code == 429:
                    rate_limit_error = httpx.HTTPStatusError(
                        "Deriv OAuth account lookup was rate-limited",
                        request=response.request,
                        response=response,
                    )
                    defer_deriv_requests_after_rate_limit(rate_limit_error)
                return existing

            accounts = self._normalize_accounts(response.json())
            selected = self._select_demo_account(accounts)

            if selected is None:
                return existing

            account_id = self._account_id(selected)

            if not account_id:
                return existing

            resolved: Dict[str, Any] = {
                "account_id": account_id,
                "account_type": "demo",
                "currency": selected.get("currency") or "USD",
            }

            if selected.get("balance") is not None:
                resolved["balance"] = selected["balance"]

            result = dict(existing or {})
            result.update(resolved)

            return result

        except (httpx.HTTPError, ValueError, TypeError) as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 429
            ):
                defer_deriv_requests_after_rate_limit(exc)
            return existing

    def complete_oauth_session(
        self,
        telegram_user_id: str | int,
        token_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        user_key = str(telegram_user_id)

        token_data = {
            "access_token": token_payload.get("access_token"),
            "refresh_token": token_payload.get("refresh_token"),
            "expires_in": token_payload.get("expires_in"),
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": self._expiry_time(token_payload.get("expires_in")),
        }

        # Preserve additional OAuth fields, if present.
        for key, value in token_payload.items():
            if key not in {
                "access_token",
                "refresh_token",
                "expires_in",
                "token_type",
            }:
                token_data[key] = value

        token_data = self._hydrate_account_metadata(
            token_data.get("access_token"),
            token_data,
        ) or token_data

        self._save_credentials(user_key, token_data)

        user_tokens = self._load_json(TOKEN_COUNT_FILE)

        if user_key not in user_tokens:
            user_tokens[user_key] = 50
            self._save_json(TOKEN_COUNT_FILE, user_tokens)

        states = self._load_json(STATE_FILE)
        states.pop(user_key, None)
        self._save_json(STATE_FILE, states)

        return token_data

    def exchange_oauth_code(
        self,
        telegram_user_id: str | int,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str = "",
    ) -> Dict[str, Any]:
        user_key = str(telegram_user_id)
        states = self._load_json(STATE_FILE)
        session = states.get(user_key, {})
        code_verifier = session.get("code_verifier")

        if not code_verifier:
            raise ValueError("missing_code_verifier")

        payload = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
        }

        if client_secret:
            payload["client_secret"] = client_secret

        wait_for_deriv_request_sync()
        response = httpx.post(
            "https://auth.deriv.com/oauth2/token",
            data=payload,
            timeout=20.0,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                defer_deriv_requests_after_rate_limit(exc)
            raise

        return self.complete_oauth_session(
            telegram_user_id,
            response.json(),
        )

    def connect_user(
        self,
        telegram_user_id: str | int,
        token_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Backward-compatible alias for the OAuth completion flow."""
        return self.complete_oauth_session(
            telegram_user_id,
            token_payload,
        )

    def get_valid_access_token(
        self,
        telegram_user_id: str | int,
    ) -> Optional[str]:
        user_key = str(telegram_user_id)
        record = self._load_credentials(user_key)
        if record is None:
            return None

        expires_at = record.get("expires_at")
        if isinstance(expires_at, str):
            try:
                expiry = datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                )
            except ValueError:
                expiry = None
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry is not None and expiry <= datetime.now(timezone.utc):
                record = self._refresh_access_token(user_key, record)
                if record is None:
                    return None

        token = record.get("access_token")
        return token if isinstance(token, str) and token else None

    def get_connected_account(
        self,
        telegram_user_id: str | int,
    ) -> Optional[Dict[str, Any]]:
        """
        Refresh account metadata from Deriv rather than trusting a
        previously stored account ID.

        This method prefers Demo when metadata can be resolved. The
        trading account service must still verify the account returned
        by the live Deriv account-list endpoint before requesting OTP.
        """
        user_key = str(telegram_user_id)
        record = self._load_credentials(user_key)
        if record is None:
            return None

        access_token = record.get("access_token")

        if not access_token:
            return record

        refreshed = self._hydrate_account_metadata(
            access_token,
            record,
        )

        if refreshed is not None:
            self._save_credentials(user_key, refreshed)
            return refreshed

        return record

    def disconnect_user(
        self,
        telegram_user_id: str | int,
    ) -> bool:
        user_key = str(telegram_user_id)
        self.credential_store.delete(user_key)
        self._remove_local_token_record(user_key)
        return True

    def get_user_tokens(
        self,
        telegram_user_id: str | int,
    ) -> int:
        user_tokens = self._load_json(TOKEN_COUNT_FILE)
        return user_tokens.get(str(telegram_user_id), 50)