from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

import httpx
import websockets

from app.config import settings
from app.services.auth_service import AuthService

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
DERIV_ACCOUNT_API_URL = "https://api.derivws.com/trading/v1/options/accounts"


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

            response = httpx.get(DERIV_ACCOUNT_API_URL, headers=headers, timeout=20.0)
            response.raise_for_status()
            payload = response.json()

            account_list = payload.get("data", payload).get("account_list", payload.get("account_list", []))
            if account_list:
                _apply_account_list(account_list)
                return {
                    "connected": True,
                    "real": real_balance,
                    "demo": demo_balance,
                    "tokens": tokens_count,
                }
        except Exception:
            pass

        try:
            async with websockets.connect(DERIV_WS_URL) as websocket:
                await websocket.send(json.dumps({"authorize": token}))
                auth_resp = json.loads(await websocket.recv())

                if "error" not in auth_resp:
                    account_list = auth_resp.get("authorize", {}).get("account_list", [])
                    if account_list:
                        _apply_account_list(account_list)

                await websocket.send(json.dumps({"balance": 1, "subscribe": 1}))
                balance_resp = json.loads(await websocket.recv())

                if isinstance(balance_resp, dict):
                    balance_entry = balance_resp.get("balance")
                    if isinstance(balance_entry, dict):
                        currency = balance_entry.get("currency", "USD")
                        if balance_entry.get("demo") is not None:
                            demo_balance = _format_balance(balance_entry.get("demo"), currency)
                        elif balance_entry.get("amount") is not None:
                            real_balance = _format_balance(balance_entry.get("amount"), currency)

                    if isinstance(balance_resp.get("authorize"), dict):
                        account_list = balance_resp.get("authorize", {}).get("account_list", [])
                        if account_list:
                            _apply_account_list(account_list)
        except Exception:
            pass

        return {
            "connected": True,
            "real": real_balance,
            "demo": demo_balance,
            "tokens": tokens_count,
        }