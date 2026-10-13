from __future__ import annotations

import json
from typing import Any

import websockets

from app.config import settings


class TradingService:
    """Real Deriv WebSocket execution layer for proposal, buy, and five-cycle trade sessions."""

    STEP_MARKET_MAP = {
        "step_100": "R_100",
        "step_200": "R_200",
        "step_300": "R_300",
        "step_400": "R_400",
        "step_500": "R_500",
    }

    def __init__(self, auth_service: Any | None = None) -> None:
        self.auth_service = auth_service
        self._demo_balance = 10000.0
        self._portfolio: list[dict[str, object]] = []

    @staticmethod
    def normalize_step_symbol(symbol: str) -> str:
        value = str(symbol or "").strip()
        if not value:
            raise ValueError("Missing market symbol.")
        lowered = value.lower()
        if lowered in TradingService.STEP_MARKET_MAP:
            return TradingService.STEP_MARKET_MAP[lowered]
        if lowered.startswith("step_"):
            suffix = lowered.split("_", 1)[1]
            if suffix.isdigit():
                return f"R_{suffix}"
        return value if value.upper().startswith("R_") else value

    @staticmethod
    def resolve_contract_type(contract_type: str) -> str:
        normalized = str(contract_type or "").upper()
        if normalized in {"BULLISH", "CALL", "RISE", "MULTUP", "UP"}:
            return "CALL"
        if normalized in {"BEARISH", "PUT", "FALL", "MULTDOWN", "DOWN", "SELL"}:
            return "PUT"
        raise ValueError(f"Unsupported trade direction: {contract_type}")

    @staticmethod
    def get_deriv_ws_url(app_id: str | int | None = None) -> str:
        app_id_value = str(app_id or settings.deriv_app_id or settings.deriv_client_id or "1089").strip()
        if not app_id_value:
            app_id_value = "1089"
        return f"wss://ws.derivws.com/websockets/v3?app_id={app_id_value}"

    def get_valid_access_token(self, telegram_user_id: str | int) -> str | None:
        if self.auth_service is None:
            return None
        if hasattr(self.auth_service, "get_valid_access_token"):
            return self.auth_service.get_valid_access_token(str(telegram_user_id))
        return None

    def get_balance(self) -> dict[str, object]:
        return {"account_type": "demo", "currency": "USD", "balance": round(self._demo_balance, 2)}

    def get_portfolio(self) -> dict[str, object]:
        return {"account_type": "demo", "positions": self._portfolio, "count": len(self._portfolio)}

    def create_proposal(
        self,
        symbol: str,
        amount: float = 10,
        contract_type: str = "CALL",
        currency: str = "USD",
        duration: int = 60,
        duration_unit: str = "s",
        multiplier: int = 10,
    ) -> dict[str, object]:
        resolved_symbol = self.normalize_step_symbol(symbol)
        resolved_contract_type = self.resolve_contract_type(contract_type)
        return {
            "proposal_id": f"prop-demo-{resolved_symbol.lower()}-{duration}",
            "symbol": resolved_symbol,
            "amount": float(amount),
            "contract_type": resolved_contract_type,
            "currency": currency,
            "duration": duration,
            "duration_unit": duration_unit,
            "multiplier": multiplier,
            "ask_price": round(float(amount) * 0.85, 2),
            "status": "valid",
        }

    def buy_contract(self, proposal_id: str, price: float, symbol: str) -> dict[str, object]:
        if price <= 0:
            return {"status": "rejected", "reason": "invalid_price"}

        contract_id = f"contract-demo-{symbol.lower()}-{len(self._portfolio) + 1}"
        position = {
            "contract_id": contract_id,
            "proposal_id": proposal_id,
            "symbol": symbol,
            "status": "open",
            "buy_price": price,
            "payout": round(price * 1.9, 2),
        }
        self._portfolio.append(position)
        self._demo_balance -= price
        return {"status": "accepted", "contract_id": contract_id, "symbol": symbol, "price": price}

    def monitor_contract(self, contract_id: str) -> dict[str, object]:
        for position in self._portfolio:
            if position["contract_id"] == contract_id:
                return {"contract_id": contract_id, "status": position["status"], "payout": position["payout"]}
        return {"contract_id": contract_id, "status": "not_found"}

    def sell_contract(self, contract_id: str, price: float | None = None) -> dict[str, object]:
        for position in self._portfolio:
            if position["contract_id"] == contract_id:
                position["status"] = "closed"
                settlement_price = price if price is not None else float(position["payout"])
                self._demo_balance += settlement_price
                return {"status": "closed", "contract_id": contract_id, "settlement": settlement_price}
        return {"status": "not_found", "contract_id": contract_id}

    async def execute_trade_cycles(
        self,
        telegram_user_id: str | int,
        symbol: str,
        contract_type: str,
        stake_amount: float,
        duration_seconds: int = 30,
        cycles: int = 5,
    ) -> list[dict[str, Any]]:
        token = self.get_valid_access_token(telegram_user_id)
        if not token:
            raise ValueError("User not authenticated with Deriv.")

        resolved_symbol = self.normalize_step_symbol(symbol)
        resolved_contract_type = self.resolve_contract_type(contract_type)
        executed_trades: list[dict[str, Any]] = []
        ws_url = self.get_deriv_ws_url()

        async with websockets.connect(ws_url) as websocket:
            await websocket.send(json.dumps({"authorize": token}))
            auth_response = json.loads(await websocket.recv())
            if isinstance(auth_response, dict) and "error" in auth_response:
                raise ValueError(f"Deriv Auth Error: {auth_response['error']['message']}")

            for cycle_index in range(max(1, int(cycles))):
                try:
                    proposal_req = {
                        "proposal": 1,
                        "amount": float(stake_amount),
                        "basis": "stake",
                        "contract_type": resolved_contract_type,
                        "currency": "USD",
                        "duration": int(duration_seconds),
                        "duration_unit": "s",
                        "symbol": resolved_symbol,
                        "underlying_symbol": resolved_symbol,
                        "subscribe": 1,
                    }
                    await websocket.send(json.dumps(proposal_req))
                    proposal_response = json.loads(await websocket.recv())
                    if isinstance(proposal_response, dict) and "error" in proposal_response:
                        print(f"Cycle {cycle_index + 1} Proposal Error: {proposal_response['error']['message']}")
                        continue

                    proposal = proposal_response.get("proposal") if isinstance(proposal_response, dict) else None
                    if not isinstance(proposal, dict):
                        proposal = proposal_response

                    proposal_id = proposal.get("id") or proposal.get("proposal_id")
                    ask_price = proposal.get("ask_price") or proposal.get("price")
                    if not proposal_id or float(ask_price or 0) <= 0:
                        print(f"Cycle {cycle_index + 1} invalid proposal response: {proposal_response}")
                        continue

                    buy_req = {"buy": proposal_id, "price": float(ask_price), "req_id": f"buy-{cycle_index + 1}"}
                    await websocket.send(json.dumps(buy_req))
                    buy_response = json.loads(await websocket.recv())
                    if isinstance(buy_response, dict) and "error" in buy_response:
                        print(f"Cycle {cycle_index + 1} Buy Error: {buy_response['error']['message']}")
                        continue

                    buy_payload = buy_response.get("buy") if isinstance(buy_response, dict) else None
                    if not isinstance(buy_payload, dict):
                        buy_payload = buy_response

                    contract_id = buy_payload.get("contract_id") or buy_payload.get("id")
                    if not contract_id:
                        print(f"Cycle {cycle_index + 1} missing contract_id in buy response: {buy_response}")
                        continue

                    executed_trades.append(
                        {
                            "cycle": cycle_index + 1,
                            "contract_id": contract_id,
                            "buy_price": buy_payload.get("buy_price", ask_price),
                            "balance_after": buy_payload.get("balance_after"),
                            "shortcode": buy_payload.get("shortcode"),
                            "symbol": resolved_symbol,
                            "contract_type": resolved_contract_type,
                        }
                    )
                except Exception as exc:  # pragma: no cover - websocket runtime guard
                    print(f"Error during trade cycle {cycle_index + 1}: {exc}")

        return executed_trades
