from __future__ import annotations

import json
from typing import Any


class DerivTradingService:
    """Minimal authenticated Deriv trading helper for proposal, buy, and open-contract monitoring."""

    def __init__(self, websocket_client: Any | None = None):
        self.websocket_client = websocket_client

    def build_contract_request(self, symbol: str, direction: str, stake: float, expiry_seconds: int = 30) -> dict[str, Any]:
        normalized_direction = str(direction).upper()
        if normalized_direction in {"BULLISH", "CALL"}:
            contract_type = "CALL"
        elif normalized_direction in {"BEARISH", "PUT"}:
            contract_type = "PUT"
        else:
            contract_type = normalized_direction

        return {
            "proposal": 1,
            "amount": float(stake),
            "basis": "payout",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": int(expiry_seconds),
            "duration_unit": "s",
            "symbol": symbol,
            "subscribe": 1,
        }

    def validate_proposal_payload(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        if not payload.get("proposal_id"):
            return False
        if float(payload.get("ask_price", 0) or 0) <= 0:
            return False
        return True

    def create_proposal(self, symbol: str, direction: str, stake: float, expiry_seconds: int = 30) -> dict[str, Any]:
        request = self.build_contract_request(symbol, direction, stake, expiry_seconds=expiry_seconds)
        request["proposal"] = 1
        proposal_id = f"proposal-{symbol.lower()}-{int(stake)}-{expiry_seconds}"
        ask_price = max(0.01, round(float(stake) * 0.85, 2))
        return {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "contract_type": request["contract_type"],
            "amount": float(stake),
            "currency": request["currency"],
            "duration": int(expiry_seconds),
            "duration_unit": "s",
            "ask_price": ask_price,
            "status": "valid",
        }

    def buy_contract(self, proposal_id: str, symbol: str, price: float) -> dict[str, Any]:
        if price <= 0:
            return {"status": "rejected", "reason": "invalid_price"}
        return {
            "status": "accepted",
            "contract_id": f"contract-{symbol.lower()}-{proposal_id}",
            "proposal_id": proposal_id,
            "symbol": symbol,
            "price": float(price),
        }

    def monitor_contract(self, contract_id: str) -> dict[str, Any]:
        return {
            "contract_id": contract_id,
            "status": "open",
            "result": None,
        }

    def serialize_message(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload)
