from __future__ import annotations

import json
from typing import Any


class AuthenticatedDerivWebSocket:
    """Authenticated Deriv websocket client wrapper for OTP-backed trading sessions."""

    def __init__(self, access_token: str, otp_url: str | None = None) -> None:
        self.access_token = access_token
        self.otp_url = otp_url

    def get_connection_url(self) -> str:
        if self.otp_url:
            return self.otp_url
        return "wss://ws.derivws.com/websockets/v3"

    def build_message(self, action: str, **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"req_id": kwargs.get("req_id", 1)}
        if action == "ticks":
            payload["ticks"] = kwargs.get("symbol")
            payload["subscribe"] = 1
        elif action == "proposal":
            payload.update({
                "proposal": 1,
                "amount": float(kwargs.get("amount", 0.0)),
                "basis": "payout",
                "contract_type": kwargs.get("contract_type", "CALL"),
                "currency": kwargs.get("currency", "USD"),
                "duration": int(kwargs.get("duration", 5)),
                "duration_unit": kwargs.get("duration_unit", "t"),
                "symbol": kwargs.get("symbol"),
            })
        elif action == "buy":
            payload.update({
                "buy": kwargs.get("proposal_id"),
                "price": float(kwargs.get("price", 0.0)),
            })
        return payload

    def serialize(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload)
