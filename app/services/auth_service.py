from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from typing import Any, Dict, Optional
import httpx

STATE_FILE = "/tmp/yields_oauth_states.json"
TOKEN_FILE = "/tmp/yields_token_store.json"
TOKEN_COUNT_FILE = "/tmp/yields_user_tokens.json"


class AuthService:
    """Secure backend service managing Deriv OAuth states, tokens, and bot usage tokens with disk persistence."""

    def __init__(self) -> None:
        pass

    def _load_json(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_json(self, path: str, data: dict) -> None:
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def begin_oauth_session(self, telegram_user_id: str) -> Dict[str, str]:
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("utf-8")).digest()
        ).rstrip(b"=").decode("utf-8")

        states = self._load_json(STATE_FILE)
        states[str(telegram_user_id)] = {
            "state": state,
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
        }
        self._save_json(STATE_FILE, states)
        return {"state": state, "code_verifier": code_verifier, "code_challenge": code_challenge}

    def validate_state(self, telegram_user_id: str, state: str) -> bool:
        states = self._load_json(STATE_FILE)
        session = states.get(str(telegram_user_id), {})
        return bool(session.get("state")) and session["state"] == state

    def get_pending_user_for_state(self, state: str) -> Optional[str]:
        states = self._load_json(STATE_FILE)
        for telegram_user_id, session in states.items():
            if session.get("state") == state:
                return telegram_user_id
        return None

    def complete_oauth_session(self, telegram_user_id: str, token_payload: Dict[str, Any]) -> Dict[str, Any]:
        tokens = self._load_json(TOKEN_FILE)
        token_data = {
            "access_token": token_payload.get("access_token"),
            "refresh_token": token_payload.get("refresh_token"),
            "expires_in": token_payload.get("expires_in"),
            "token_type": token_payload.get("token_type", "Bearer"),
        }
        tokens[str(telegram_user_id)] = token_data
        self._save_json(TOKEN_FILE, tokens)

        user_tokens = self._load_json(TOKEN_COUNT_FILE)
        if str(telegram_user_id) not in user_tokens:
            user_tokens[str(telegram_user_id)] = 50
            self._save_json(TOKEN_COUNT_FILE, user_tokens)

        states = self._load_json(STATE_FILE)
        states.pop(str(telegram_user_id), None)
        self._save_json(STATE_FILE, states)
        return token_data

    def exchange_oauth_code(
        self,
        telegram_user_id: str,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str = "",
    ) -> Dict[str, Any]:
        states = self._load_json(STATE_FILE)
        session = states.get(str(telegram_user_id), {})
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

        response = httpx.post("https://auth.deriv.com/oauth2/token", data=payload, timeout=20.0)
        response.raise_for_status()
        data = response.json()
        return self.complete_oauth_session(telegram_user_id, data)

    def get_valid_access_token(self, telegram_user_id: str) -> Optional[str]:
        tokens = self._load_json(TOKEN_FILE)
        record = tokens.get(str(telegram_user_id))
        if not record:
            return None
        return record.get("access_token")

    def get_connected_account(self, telegram_user_id: str) -> Optional[Dict[str, Any]]:
        tokens = self._load_json(TOKEN_FILE)
        return tokens.get(str(telegram_user_id))

    def disconnect_user(self, telegram_user_id: str) -> bool:
        tokens = self._load_json(TOKEN_FILE)
        if str(telegram_user_id) in tokens:
            del tokens[str(telegram_user_id)]
            self._save_json(TOKEN_FILE, tokens)
        return True

    def get_user_tokens(self, telegram_user_id: str) -> int:
        user_tokens = self._load_json(TOKEN_COUNT_FILE)
        return user_tokens.get(str(telegram_user_id), 50)