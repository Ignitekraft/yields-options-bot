from __future__ import annotations

import json
from typing import Any, Dict, Iterable

import httpx
import websockets

from app.config import settings
from app.services.auth_service import AuthService

DERIV_ACCOUNT_API_URL = "https://api.derivws.com/trading/v1/options/accounts"
DERIV_WS_ROOT = "https://api.derivws.com/trading/v1/options/ws"


class AccountService:
    """Service that manages authenticated user account details using stored OAuth tokens."""

    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    def get_account_status(self, telegram_user_id: str | int) -> Dict[str, Any]:
        user_key = str(telegram_user_id)
        account = self.auth_service.get_connected_account(user_key)
        if not account or not account.get("access_token"):
            return {"connected": False}

        return {
            "connected": True,
            "account_id": account.get("account_id", "OAuth Active"),
            "type": account.get("account_type", "Demo / Real"),
            "currency": account.get("currency", "USD"),
            "balance": account.get("balance", "Ready"),
        }

    async def get_live_balances(self, telegram_user_id: str | int) -> Dict[str, Any]:
        user_key = str(telegram_user_id)
        token = self.auth_service.get_valid_access_token(user_key)
        tokens_count = self.auth_service.get_user_tokens(user_key)

        if not token:
            return {"connected": False}

        real_balance = "0.00 USD"
        demo_balance = "0.00 USD"

        def _format_balance(value: Any, currency: str = "USD") -> str:
            if value is None:
                value = 0.0
            return f"{value} {currency}"

        def _apply_account_list(account_list: Iterable[Dict[str, Any]]) -> None:
            nonlocal real_balance, demo_balance
            for acc in account_list:
                if not isinstance(acc, dict):
                    continue
                curr = acc.get("currency", "USD")
                bal = acc.get("balance", acc.get("amount", 0.0))
                if acc.get("is_virtual"):
                    demo_balance = _format_balance(bal, curr)
                else:
                    real_balance = _format_balance(bal, curr)

        try:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            app_id = (settings.deriv_app_id or settings.deriv_client_id or "").strip()
            if app_id:
                headers["Deriv-App-ID"] = app_id

            account_response = httpx.get(DERIV_ACCOUNT_API_URL, headers=headers, timeout=20.0)
            account_response.raise_for_status()
            payload = account_response.json()
            account_data = payload.get("data", payload)
            account_list = account_data.get("account_list", account_data.get("accounts", []))
            if account_list:
                _apply_account_list(account_list)

            selected_account = None
            if isinstance(account_list, list) and account_list:
                selected_account = account_list[0]
                if isinstance(selected_account, dict):
                    selected_account = selected_account.get("account_id") or selected_account.get("id")

            if not selected_account:
                selected_account = self.auth_service.get_connected_account(user_key).get("account_id") if self.auth_service.get_connected_account(user_key) else ""

            if selected_account:
                otp_response = httpx.post(
                    f"{DERIV_ACCOUNT_API_URL}/{selected_account}/otp",
                    headers={
                        "Authorization": f"Bearer {token}",
                        **({"Deriv-App-ID": app_id} if app_id else {}),
                    },
                    timeout=20.0,
                )
                otp_response.raise_for_status()
                ws_url = otp_response.json().get("data", {}).get("url")
                if ws_url:
                    async with websockets.connect(ws_url) as websocket:
                        await websocket.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": 1}))
                        balance_message = json.loads(await websocket.recv())
                        balance_data = balance_message.get("balance") if isinstance(balance_message, dict) else None
                        if isinstance(balance_data, dict):
                            currency = balance_data.get("currency", "USD")
                            if balance_data.get("balance") is not None:
                                real_balance = _format_balance(balance_data.get("balance"), currency)
                            if balance_data.get("demo") is not None:
                                demo_balance = _format_balance(balance_data.get("demo"), currency)
                        if isinstance(balance_message, dict) and balance_message.get("msg_type") == "balance":
                            balance_data = balance_message.get("balance")
                            if isinstance(balance_data, dict):
                                currency = balance_data.get("currency", "USD")
                                if balance_data.get("balance") is not None:
                                    real_balance = _format_balance(balance_data.get("balance"), currency)
                                if balance_data.get("demo") is not None:
                                    demo_balance = _format_balance(balance_data.get("demo"), currency)
        except Exception:
            pass

        return {
            "connected": True,
            "real": real_balance,
            "demo": demo_balance,
            "tokens": tokens_count,
        }