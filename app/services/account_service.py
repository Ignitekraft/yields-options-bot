from __future__ import annotations

import json
import websockets
from typing import Any, Dict
from app.services.auth_service import AuthService

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"


class AccountService:
    """Service that manages authenticated user account details and fetches real-time balances from Deriv."""

    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    def get_account_status(self, telegram_user_id: str | int) -> Dict[str, Any]:
        user_key = str(telegram_user_id)
        account = self.auth_service.get_connected_account(user_key)
        if not account or not account.get("access_token"):
            return {"connected": False}

        return {
            "connected": True,
            "account_id": "OAuth Active",
            "type": "Demo / Real",
            "currency": "USD",
            "balance": "Ready",
        }

    async def get_live_balances(self, telegram_user_id: str | int) -> Dict[str, Any]:
        user_key = str(telegram_user_id)
        token = self.auth_service.get_valid_access_token(user_key)
        tokens_count = self.auth_service.get_user_tokens(user_key)

        if not token:
            return {"connected": False}

        real_balance = "0.00 USD"
        demo_balance = "0.00 USD"

        try:
            async with websockets.connect(DERIV_WS_URL) as websocket:
                # 1. Authorize with OAuth token to retrieve account list
                await websocket.send(json.dumps({"authorize": token}))
                auth_response = json.loads(await websocket.recv())

                if "error" in auth_response:
                    return {"connected": True, "real": real_balance, "demo": demo_balance, "tokens": tokens_count}

                authorize_data = auth_response.get("authorize", {})
                account_list = authorize_data.get("account_list", [])

                # 2. Iterate through accounts (Demo vs Real) and fetch balances
                for acc in account_list:
                    loginid = acc.get("loginid")
                    is_virtual = acc.get("is_virtual", 0)
                    
                    if not loginid:
                        continue

                    # Open a separate auth call or socket request for each specific account loginid
                    async with websockets.connect(DERIV_WS_URL) as sub_ws:
                        await sub_ws.send(json.dumps({"authorize": token}))
                        _ = await sub_ws.recv()  # consume auth response

                        # Request balance for this specific loginid
                        await sub_ws.send(json.dumps({"balance": 1, "loginid": loginid}))
                        bal_resp = json.loads(await sub_ws.recv())

                        if "balance" in bal_resp:
                            bal_data = bal_resp["balance"]
                            amount = bal_data.get("balance", 0.0)
                            curr = bal_data.get("currency", "USD")
                            formatted = f"{amount:.2f} {curr}"

                            if is_virtual or "VRTC" in loginid:
                                demo_balance = formatted
                            else:
                                real_balance = formatted

        except Exception as e:
            print(f"Error fetching Deriv live balances: {e}")

        return {
            "connected": True,
            "real": real_balance,
            "demo": demo_balance,
            "tokens": tokens_count,
        }