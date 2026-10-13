from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any, Dict, Optional, Tuple

import httpx


class AuthService:
    """Secure backend service that owns Deriv OAuth tokens per Telegram user."""

    def __init__(self) -> None:
        self._token_store: Dict[str, Dict[str, Any]] = {}
        self._oauth_state_store: Dict[str, Dict[str, str]] = {}

    def begin_oauth_session(self, telegram_user_id: str) -> Dict[str, str]:
        """Create a pending OAuth session with PKCE and state validation data."""
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("utf-8")).digest()
        ).rstrip(b"=").decode("utf-8")

        self._oauth_state_store[telegram_user_id] = {
            "state": state,
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
        }
        return {
            "state": state,
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
        }

    def validate_state(self, telegram_user_id: str, state: str) -> bool:
        session = self._oauth_state_store.get(telegram_user_id, {})
        return bool(session.get("state")) and session["state"] == state

    def get_pending_oauth_state(self, telegram_user_id: str) -> Optional[str]:
        session = self._oauth_state_store.get(telegram_user_id, {})
        return session.get("state")

    def get_pending_user_for_state(self, state: str) -> Optional[str]:
        for telegram_user_id, session in self._oauth_state_store.items():
            if session.get("state") == state:
                return telegram_user_id
        return None

    def complete_oauth_session(self, telegram_user_id: str, token_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Finalize the OAuth exchange and persist the access token securely server-side."""
        self._token_store[telegram_user_id] = {
            "access_token": token_payload.get("access_token"),
            "refresh_token": token_payload.get("refresh_token"),
            "expires_in": token_payload.get("expires_in"),
            "token_type": token_payload.get("token_type", "Bearer"),
            "connected_at": token_payload.get("connected_at"),
        }
        self._oauth_state_store.pop(telegram_user_id, None)
        return self._token_store[telegram_user_id]

    def exchange_oauth_code(
        self,
        telegram_user_id: str,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str = "",
    ) -> Dict[str, Any]:
        """Exchange a Deriv authorization code for access tokens using the stored PKCE verifier."""
        session = self._oauth_state_store.get(telegram_user_id, {})
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

        response = httpx.post("https://oauth.deriv.com/oauth2/token", data=payload, timeout=20.0)
        response.raise_for_status()
        data = response.json()
        return self.complete_oauth_session(telegram_user_id, data)

    def connect_user(self, telegram_user_id: str, token_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Persist tokens server-side for the Telegram user."""
        return self.complete_oauth_session(telegram_user_id, token_payload)

    def refresh_user_token(self, telegram_user_id: str, refresh_token: str) -> Dict[str, Any]:
        """Placeholder for Deriv refresh-token exchange implementation."""
        record = self._token_store.get(telegram_user_id, {})
        record["refresh_token"] = refresh_token
        return record

    def get_valid_access_token(self, telegram_user_id: str) -> Optional[str]:
        """Return a valid access token or None if the user is not connected."""
        record = self._token_store.get(telegram_user_id)
        if not record:
            return None
        return record.get("access_token")

    def disconnect_user(self, telegram_user_id: str) -> bool:
        """Remove a user's token record."""
        if telegram_user_id in self._token_store:
            del self._token_store[telegram_user_id]
        if telegram_user_id in self._oauth_state_store:
            del self._oauth_state_store[telegram_user_id]
        return True

    def get_connected_account(self, telegram_user_id: str) -> Optional[Dict[str, Any]]:
        """Return a minimal account record for the connected Telegram user."""
        return self._token_store.get(telegram_user_id)
