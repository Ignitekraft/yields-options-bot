from __future__ import annotations

import json
import logging
from typing import Any

import websockets

from app.config import settings

logger = logging.getLogger(__name__)


class TradeExecutionService:
    """Authenticated trade execution helpers for Deriv Rise/Fall contracts only."""

    STEP_MARKET_MAP = {
        "step_100": "R_100",
        "step_200": "R_200",
        "step_300": "R_300",
        "step_400": "R_400",
        "step_500": "R_500",
    }

    @staticmethod
    def calculate_next_stake(base_stake: float, martingale_step: int, multiplier: float | None = None) -> float:
        multiplier = float(multiplier if multiplier is not None else settings.martingale_multiplier)
        step_index = max(0, int(martingale_step) - 1)
        next_stake = float(base_stake) * (multiplier ** step_index)
        return round(next_stake, 2)

    @staticmethod
    def validate_stake(stake: float, account_balance: float | None = None) -> tuple[bool, str | None]:
        if stake is None or not isinstance(stake, (int, float)):
            return False, "invalid_stake"
        amount = float(stake)
        if amount <= 0:
            return False, "invalid_stake"
        if amount > 1000000:
            return False, "stake_too_large"
        if account_balance is not None and amount > float(account_balance):
            return False, "insufficient_balance"
        return True, None

    @staticmethod
    def resolve_contract_direction(direction: str) -> str:
        normalized = str(direction).upper()
        if normalized in {"BULLISH", "CALL"}:
            return "CALL"
        if normalized in {"BEARISH", "PUT"}:
            return "PUT"
        raise ValueError(f"Unsupported Auto Trade direction: {direction}")

    @staticmethod
    def resolve_market_symbol(step_market: str, available_symbols: list[dict[str, Any]] | None = None) -> str:
        configured = str(step_market).lower()
        mapped = TradeExecutionService.STEP_MARKET_MAP.get(configured)
        if mapped:
            return mapped
        if available_symbols:
            for item in available_symbols:
                symbol = str(item.get("symbol") or "").strip()
                display_name = str(item.get("display_name") or "").lower()
                if symbol.lower() == configured:
                    return symbol
                if configured.replace("step_", "volatility ") in display_name:
                    return symbol
        raise ValueError(f"Market {step_market} could not be resolved to a supported Deriv underlying symbol.")

    @staticmethod
    def get_ws_url(app_id: str | int | None = None) -> str:
        app_id_value = str(app_id or settings.deriv_app_id or settings.deriv_client_id or "1089").strip()
        if not app_id_value:
            app_id_value = "1089"
        return f"wss://ws.derivws.com/websockets/v3?app_id={app_id_value}"

    @staticmethod
    async def _send_ws_request(access_token: str, payload: dict[str, Any], app_id: str | int | None = None) -> dict[str, Any]:
        ws_url = TradeExecutionService.get_ws_url(app_id)
        async with websockets.connect(ws_url) as websocket:
            await websocket.send(json.dumps({"authorize": access_token}))
            auth_response = json.loads(await websocket.recv())
            if isinstance(auth_response, dict) and auth_response.get("error"):
                raise ValueError(str(auth_response.get("error")).replace("\n", " "))
            await websocket.send(json.dumps(payload))
            response_text = await websocket.recv()
        response = json.loads(response_text) if isinstance(response_text, str) else response_text
        if isinstance(response, dict) and response.get("error"):
            raise ValueError(str(response.get("error")))
        return response if isinstance(response, dict) else {"status": "unknown", "payload": response}

    async def create_proposal(
        self,
        user_id: str | int,
        access_token: str,
        account_id: str,
        market_symbol: str,
        direction: str,
        stake: float,
        currency: str = "USD",
        expiry_seconds: int = 30,
    ) -> dict[str, Any]:
        contract_type = self.resolve_contract_direction(direction)
        resolved_symbol = self.resolve_market_symbol(market_symbol)
        valid, reason = self.validate_stake(stake)
        if not valid:
            raise ValueError(reason or "invalid_stake")

        payload = {
            "proposal": 1,
            "amount": float(stake),
            "basis": "stake",
            "contract_type": contract_type,
            "currency": str(currency or "USD"),
            "duration": int(expiry_seconds),
            "duration_unit": "s",
            "underlying_symbol": resolved_symbol,
            "req_id": f"proposal-{user_id}-{int(stake)}-{expiry_seconds}",
            "subscribe": 1,
        }
        response = await self._send_ws_request(access_token, payload, app_id=settings.deriv_app_id or settings.deriv_client_id)
        proposal = response.get("proposal") if isinstance(response, dict) else None
        if proposal is None and isinstance(response, dict) and response.get("proposal_id"):
            proposal = response
        if not isinstance(proposal, dict):
            raise ValueError("Proposal request rejected by Deriv.")

        proposal_id = proposal.get("proposal_id") or proposal.get("id")
        ask_price = proposal.get("ask_price") or proposal.get("price")
        if not proposal_id or float(ask_price or 0) <= 0:
            raise ValueError("Proposal response missing proposal_id or usable price.")

        return {
            "status": "valid",
            "proposal_id": str(proposal_id),
            "contract_type": contract_type,
            "market_symbol": resolved_symbol,
            "underlying_symbol": resolved_symbol,
            "amount": float(stake),
            "currency": str(currency or "USD"),
            "duration": int(expiry_seconds),
            "duration_unit": "s",
            "ask_price": float(ask_price),
            "raw": proposal,
        }

    async def buy_contract(
        self,
        user_id: str | int,
        access_token: str,
        account_id: str,
        proposal_id: str,
        ask_price: float,
        market_symbol: str,
        direction: str,
    ) -> dict[str, Any]:
        contract_type = self.resolve_contract_direction(direction)
        payload = {
            "buy": proposal_id,
            "price": float(ask_price),
            "req_id": f"buy-{user_id}-{proposal_id}",
            "contract_type": contract_type,
            "symbol": market_symbol,
        }
        response = await self._send_ws_request(access_token, payload, app_id=settings.deriv_app_id or settings.deriv_client_id)
        if isinstance(response, dict) and response.get("error"):
            raise ValueError(str(response.get("error")))

        buy_payload = response.get("buy") if isinstance(response, dict) else None
        if not isinstance(buy_payload, dict):
            raise ValueError("Buy request was not acknowledged by Deriv.")

        contract_id = buy_payload.get("contract_id") or buy_payload.get("id")
        if not contract_id:
            raise ValueError("Buy response missing contract_id.")

        return {
            "status": "accepted",
            "contract_id": str(contract_id),
            "proposal_id": str(proposal_id),
            "market_symbol": market_symbol,
            "direction": direction,
            "contract_type": contract_type,
            "buy_price": float(ask_price),
            "raw": buy_payload,
        }

    async def monitor_contract(
        self,
        user_id: str | int,
        access_token: str,
        account_id: str,
        contract_id: str,
    ) -> dict[str, Any]:
        payload = {
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 1,
            "req_id": f"monitor-{user_id}-{contract_id}",
        }
        response = await self._send_ws_request(access_token, payload, app_id=settings.deriv_app_id or settings.deriv_client_id)
        contract = response.get("proposal_open_contract") if isinstance(response, dict) else None
        if not isinstance(contract, dict):
            contract = response if isinstance(response, dict) else {}
        if not contract:
            raise ValueError("Contract status fetch returned no data.")
        return {
            "status": contract.get("status") or "unknown",
            "contract_id": contract_id,
            "profit": contract.get("profit"),
            "payout": contract.get("payout"),
            "raw": contract,
        }

    async def execute_trade(
        self,
        user_id: str | int,
        market_symbol: str,
        direction: str,
        stake: float,
        account_id: str | None = None,
        access_token: str | None = None,
        currency: str = "USD",
    ) -> dict[str, Any]:
        if not market_symbol:
            raise ValueError("Missing trade market selection.")
        if not access_token:
            raise ValueError("Missing Deriv access token.")
        if not account_id:
            raise ValueError("No Deriv account is available for this user.")

        proposal = await self.create_proposal(
            user_id=user_id,
            access_token=access_token,
            account_id=account_id,
            market_symbol=market_symbol,
            direction=direction,
            stake=stake,
            currency=currency,
            expiry_seconds=30,
        )

        buy = await self.buy_contract(
            user_id=user_id,
            access_token=access_token,
            account_id=account_id,
            proposal_id=proposal["proposal_id"],
            ask_price=proposal["ask_price"],
            market_symbol=proposal["market_symbol"],
            direction=direction,
        )

        return {
            "status": "accepted",
            "proposal_id": buy["proposal_id"],
            "contract_id": buy["contract_id"],
            "buy_price": buy["buy_price"],
            "direction": direction,
            "market_symbol": proposal["market_symbol"],
            "currency": currency,
            "contract_type": self.resolve_contract_direction(direction),
        }

    @staticmethod
    def build_proposal_payload(symbol: str, direction: str, stake: float, expiry_seconds: int = settings.default_expiry_seconds) -> dict[str, Any]:
        contract_type = TradeExecutionService.resolve_contract_direction(direction)
        return {
            "proposal": 1,
            "amount": float(stake),
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": int(expiry_seconds),
            "duration_unit": "s",
            "underlying_symbol": TradeExecutionService.resolve_market_symbol(symbol),
            "req_id": f"proposal-{symbol}-{expiry_seconds}",
        }
