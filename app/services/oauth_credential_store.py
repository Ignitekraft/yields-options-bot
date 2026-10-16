from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SupabaseOAuthCredentialStore:
    """Persist encrypted Deriv OAuth credentials in backend-only Supabase storage."""

    TABLE = "deriv_oauth_credentials"

    def __init__(
        self,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        encryption_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.supabase_url = (supabase_url or settings.supabase_url).rstrip("/")
        self.service_role_key = (
            service_role_key or settings.supabase_service_role_key
        )
        self.encryption_key = (
            encryption_key or settings.deriv_token_encryption_key
        )
        self.transport = transport

    @property
    def is_enabled(self) -> bool:
        return bool(
            self.supabase_url
            and self.service_role_key
            and self.encryption_key
        )

    @property
    def is_supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.service_role_key)

    def _cipher(self) -> Fernet:
        if not self.encryption_key:
            raise RuntimeError("DERIV_TOKEN_ENCRYPTION_KEY_NOT_CONFIGURED")
        try:
            return Fernet(self.encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("INVALID_DERIV_TOKEN_ENCRYPTION_KEY") from exc

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        if not self.is_supabase_configured:
            raise RuntimeError("SUPABASE_CREDENTIAL_STORE_NOT_CONFIGURED")
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        with httpx.Client(transport=self.transport, timeout=8.0) as client:
            response = client.request(
                method,
                f"{self.supabase_url}/rest/v1/{path}",
                params=params,
                headers=self._headers(prefer),
                json=payload,
            )
            response.raise_for_status()
            return response

    def save(
        self,
        telegram_user_id: str | int,
        credentials: dict[str, Any],
    ) -> None:
        cipher = self._cipher()
        user_id = str(telegram_user_id)
        encrypted = cipher.encrypt(
            json.dumps(
                {"telegram_user_id": user_id, "credentials": credentials},
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        now = datetime.now(timezone.utc).isoformat()

        self._request(
            "POST",
            "users",
            params={"on_conflict": "telegram_user_id"},
            payload={"telegram_user_id": user_id, "updated_at": now},
            prefer="resolution=merge-duplicates,return=minimal",
        )
        self._request(
            "POST",
            self.TABLE,
            params={"on_conflict": "telegram_user_id"},
            payload={
                "telegram_user_id": user_id,
                "encrypted_credentials": encrypted,
                "updated_at": now,
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def load(self, telegram_user_id: str | int) -> dict[str, Any] | None:
        if not self.is_enabled:
            return None
        cipher = self._cipher()
        user_id = str(telegram_user_id)
        response = self._request(
            "GET",
            self.TABLE,
            params={
                "select": "encrypted_credentials",
                "telegram_user_id": f"eq.{user_id}",
                "limit": "1",
            },
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return None
        if not isinstance(rows[0], dict):
            raise RuntimeError("INVALID_STORED_DERIV_CREDENTIALS")
        encrypted = rows[0].get("encrypted_credentials")
        if not isinstance(encrypted, str) or not encrypted:
            raise RuntimeError("INVALID_STORED_DERIV_CREDENTIALS")
        try:
            payload = json.loads(cipher.decrypt(encrypted.encode("ascii")))
        except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("DERIV_CREDENTIAL_DECRYPTION_FAILED") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("telegram_user_id") != user_id
            or not isinstance(payload.get("credentials"), dict)
        ):
            raise RuntimeError("INVALID_STORED_DERIV_CREDENTIALS")
        return payload["credentials"]

    def delete(self, telegram_user_id: str | int) -> None:
        if not self.is_supabase_configured:
            return
        self._request(
            "DELETE",
            self.TABLE,
            params={"telegram_user_id": f"eq.{telegram_user_id}"},
            prefer="return=minimal",
        )
