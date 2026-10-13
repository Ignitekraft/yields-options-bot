from __future__ import annotations

from typing import Any, Dict
from app.services.auth_service import AuthService


class AccountService:
    """Service that manages authenticated user account details using stored OAuth tokens."""

    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    def get_account_status(self, telegram_user_id: str) -> Dict[str, Any]:
        """Retrieve connection status for a Telegram user from the global token store."""
        account = self.auth_service.get_connected_account(str(telegram_user_id))
        if not account or not account.get("access_token"):
            return {"connected": False}

        return {
            "connected": True,
            "account_id": "OAuth Connected",
            "type": "Demo/Real",
            "currency": "USD",
            "balance": "Ready",
        }