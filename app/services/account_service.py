from __future__ import annotations

import json
import websockets
from typing import Any, Dict
from app.services.auth_service import AuthService

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"


class AccountService:
    """Service that manages authenticated user account details using stored OAuth tokens."""

    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    def get_account_status(self, telegram_user_id: str) -> Dict[str, Any]:
        account = self.auth_service.get_connected_account(str(telegram_user_id))
        if not account or not account.get("access_token"):
            return {"connected": False}

        return {
            "connected": True,
            "account_id": "OAuth Active",
            "type": "Demo / Real",
            "currency": "USD",
            "balance": "Ready",
        }

    async def get_live_balances(self, telegram_user_id: str) -> Dict[str, Any]:
        token = self.auth_service.get_valid_access_token(str(telegram_user_id))
        tokens_count = self.auth_service.get_user_tokens(str(telegram_user_id))

        if not token:
            return {"connected": False}

        real_balance = "0.00 USD"
        demo_balance = "0.00 USD"

        try:
            async with websockets.connect(DERIV_WS_URL) as websocket:
                await websocket.send(json.dumps({"authorize": token}))
                auth_resp = json.loads(await websocket.recv())
                
                if "error" not in auth_resp:
                    account_list = auth_resp.get("authorize", {}).get("account_list", [])
                    for acc in account_list:
                        curr = acc.get("currency", "USD")
                        bal = acc.get("balance", 0.0)
                        if acc.get("is_virtual"):
                            demo_balance = f"{bal} {curr}"
                        else:
                            real_balance = f"{bal} {curr}"
        except Exception:
            pass

        return {
            "connected": True,
            "real": real_balance,
            "demo": demo_balance,
            "tokens": tokens_count,
        }