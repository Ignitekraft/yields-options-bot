from __future__ import annotations

from app.services.auth_service import AuthService


class AccountService:
    """Authenticated account layer for Deriv account details and balance retrieval."""

    def __init__(self, auth_service: AuthService | None = None) -> None:
        self.auth_service = auth_service or AuthService()

    def get_balance(self, telegram_user_id: str) -> dict[str, object]:
        account = self.get_account_status(telegram_user_id)
        if not account["connected"]:
            return {"connected": False, "status": "not connected"}
        return {"connected": True, "currency": account["currency"], "balance": account["balance"]}

    def get_account_details(self, telegram_user_id: str) -> dict[str, object]:
        return self.get_account_status(telegram_user_id)

    def get_account_status(self, telegram_user_id: str) -> dict[str, object]:
        token_record = self.auth_service.get_connected_account(str(telegram_user_id))
        if not token_record:
            return {
                "connected": False,
                "account_id": None,
                "type": None,
                "currency": None,
                "balance": None,
                "status": "not connected",
            }

        return {
            "connected": True,
            "account_id": token_record.get("account_id", "demo-account-001"),
            "type": token_record.get("account_type", "Demo"),
            "currency": token_record.get("currency", "USD"),
            "balance": token_record.get("balance", 10000.00),
            "status": "ready",
        }
