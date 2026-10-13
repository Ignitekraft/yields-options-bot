from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import websockets

from app.config import settings


class UnknownBuyOutcome(RuntimeError):
    """
    Raised when the buy request may have reached Deriv but the client cannot
    determine whether the contract was purchased.

    IMPORTANT:
    Never automatically retry a buy after this exception.
    """

    pass


class TradeExecutionService:
    """
    Canonical real Deriv Rise/Fall execution service.

    Authentication is handled outside this class:
        OAuth token
            -> Options account resolution
            -> OTP request
            -> authenticated WebSocket URL

    This service receives the already-authenticated OTP WebSocket URL and
    performs proposal + buy over that connection.
    """

    STEP_MARKET_MAP = {
        "step_100": "R_100",
        "step_200": "R_200",
        "step_300": "R_300",
        "step_400": "R_400",
        "step_500": "R_500",
    }

    _req_counter = itertools.count(1)

    def __init__(self) -> None:
        self.martingale_multiplier = float(
            getattr(settings, "martingale_multiplier", 2.0)
        )

    @classmethod
    def next_req_id(cls) -> int:
        return next(cls._req_counter)

    @classmethod
    def normalize_step_symbol(cls, symbol: str) -> str:
        value = str(symbol or "").strip()

        if not value:
            raise ValueError("MISSING_MARKET_SYMBOL")

        lowered = value.lower()

        if lowered in cls.STEP_MARKET_MAP:
            return cls.STEP_MARKET_MAP[lowered]

        if lowered.startswith("step_"):
            suffix = lowered.split("_", 1)[1]

            if suffix.isdigit():
                return f"R_{suffix}"

        upper = value.upper()

        if upper in cls.STEP_MARKET_MAP.values():
            return upper

        if upper.startswith("R_"):
            return upper

        raise ValueError(
            f"UNSUPPORTED_STEP_MARKET:{symbol}"
        )

    @staticmethod
    def resolve_contract_type(direction: str) -> str:
        normalized = str(direction or "").strip().upper()

        if normalized in {"BULLISH", "RISE", "CALL"}:
            return "CALL"

        if normalized in {"BEARISH", "FALL", "PUT"}:
            return "PUT"

        raise ValueError(
            f"UNSUPPORTED_RISE_FALL_DIRECTION:{direction}"
        )

    @staticmethod
    def validate_stake(stake: float) -> float:
        try:
            value = float(stake)
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_STAKE") from exc

        if value <= 0:
            raise ValueError("STAKE_MUST_BE_GREATER_THAN_ZERO")

        return round(value, 2)

    @staticmethod
    def validate_expiry(expiry_seconds: int) -> int:
        value = int(expiry_seconds)

        if value != 30:
            raise ValueError(
                "YIELDS_EXPIRY_MUST_BE_30_SECONDS"
            )

        return 30

    @classmethod
    def build_proposal_payload(
        cls,
        symbol: str,
        direction: str,
        stake: float,
        expiry_seconds: int = 30,
        currency: str = "USD",
    ) -> dict[str, Any]:
        resolved_symbol = cls.normalize_step_symbol(symbol)
        contract_type = cls.resolve_contract_type(direction)
        amount = cls.validate_stake(stake)
        expiry = cls.validate_expiry(expiry_seconds)

        return {
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": str(currency or "USD").upper(),
            "duration": expiry,
            "duration_unit": "s",
            "underlying_symbol": resolved_symbol,
            "req_id": cls.next_req_id(),
        }

    @staticmethod
    def _error_message(response: dict[str, Any]) -> str:
        error = response.get("error")

        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")

            if code and message:
                return f"{code}:{message}"

            if message:
                return str(message)

        return "DERIV_REQUEST_FAILED"

    @classmethod
    async def _request(
        cls,
        websocket: Any,
        payload: dict[str, Any],
        expected_key: str,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        req_id = payload.get("req_id")

        try:
            await asyncio.wait_for(
                websocket.send(json.dumps(payload)),
                timeout=15,
            )
        except Exception as exc:
            raise RuntimeError(
                "DERIV_REQUEST_SEND_FAILED"
            ) from exc

        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"DERIV_{expected_key.upper()}_TIMEOUT"
                ) from exc

            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")

            try:
                response = json.loads(raw)
            except (TypeError, ValueError):
                continue

            if not isinstance(response, dict):
                continue

            if "error" in response:
                raise RuntimeError(
                    cls._error_message(response)
                )

            echo_req = response.get("echo_req")

            if (
                req_id is not None
                and isinstance(echo_req, dict)
                and echo_req.get("req_id") not in {None, req_id}
            ):
                continue

            if expected_key in response:
                return response

            if response.get("msg_type") == expected_key:
                return response

    @classmethod
    async def _create_proposal_on_ws(
        cls,
        websocket: Any,
        symbol: str,
        direction: str,
        stake: float,
        expiry_seconds: int,
        currency: str,
    ) -> dict[str, Any]:
        payload = cls.build_proposal_payload(
            symbol=symbol,
            direction=direction,
            stake=stake,
            expiry_seconds=expiry_seconds,
            currency=currency,
        )

        response = await cls._request(
            websocket,
            payload,
            expected_key="proposal",
        )

        proposal = response.get("proposal")

        if not isinstance(proposal, dict):
            raise RuntimeError(
                "DERIV_PROPOSAL_RESPONSE_MALFORMED"
            )

        proposal_id = proposal.get("id")
        ask_price = proposal.get("ask_price")

        if not proposal_id:
            raise RuntimeError(
                "DERIV_PROPOSAL_ID_MISSING"
            )

        try:
            ask_price = float(ask_price)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "DERIV_PROPOSAL_PRICE_MISSING"
            ) from exc

        if ask_price <= 0:
            raise RuntimeError(
                "DERIV_PROPOSAL_PRICE_INVALID"
            )

        return {
            "proposal_id": str(proposal_id),
            "ask_price": ask_price,
            "response": response,
        }

    @classmethod
    async def _buy_on_ws(
        cls,
        websocket: Any,
        proposal_id: str,
        price: float,
    ) -> dict[str, Any]:
        req_id = cls.next_req_id()

        payload = {
            "buy": str(proposal_id),
            "price": float(price),
            "req_id": req_id,
        }

        try:
            await asyncio.wait_for(
                websocket.send(json.dumps(payload)),
                timeout=15,
            )
        except Exception as exc:
            raise UnknownBuyOutcome(
                "BUY_OUTCOME_UNKNOWN_AFTER_SEND_FAILURE"
            ) from exc

        try:
            while True:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=20,
                )

                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")

                response = json.loads(raw)

                if not isinstance(response, dict):
                    continue

                if "error" in response:
                    raise RuntimeError(
                        cls._error_message(response)
                    )

                echo_req = response.get("echo_req")

                if (
                    isinstance(echo_req, dict)
                    and echo_req.get("req_id") not in {None, req_id}
                ):
                    continue

                if "buy" not in response:
                    continue

                buy = response.get("buy")

                if not isinstance(buy, dict):
                    raise RuntimeError(
                        "DERIV_BUY_RESPONSE_MALFORMED"
                    )

                contract_id = (
                    buy.get("contract_id")
                    or buy.get("id")
                )

                if not contract_id:
                    raise RuntimeError(
                        "DERIV_CONTRACT_ID_MISSING"
                    )

                return {
                    "contract_id": str(contract_id),
                    "buy_price": buy.get(
                        "buy_price",
                        price,
                    ),
                    "balance_after": buy.get(
                        "balance_after"
                    ),
                    "shortcode": buy.get(
                        "shortcode"
                    ),
                    "response": response,
                }

        except UnknownBuyOutcome:
            raise
        except asyncio.TimeoutError as exc:
            raise UnknownBuyOutcome(
                "BUY_OUTCOME_UNKNOWN_AFTER_TIMEOUT"
            ) from exc
        except websockets.exceptions.ConnectionClosed as exc:
            raise UnknownBuyOutcome(
                "BUY_OUTCOME_UNKNOWN_AFTER_CONNECTION_CLOSED"
            ) from exc

    async def execute_trade(
        self,
        user_id: str | int,
        market_symbol: str,
        direction: str,
        stake: float,
        account_id: str,
        access_token: str | None = None,
        currency: str = "USD",
        ws_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute exactly one Rise/Fall contract.

        `ws_url` must be the fresh OTP URL generated for `account_id`.

        access_token is retained in the signature for compatibility but is
        deliberately NOT sent over the WebSocket.
        """
        del user_id
        del access_token

        if not account_id:
            return {
                "status": "rejected",
                "reason": "MISSING_OPTIONS_ACCOUNT_ID",
            }

        if not ws_url:
            return {
                "status": "rejected",
                "reason": "MISSING_AUTHENTICATED_WS_URL",
            }

        resolved_symbol = self.normalize_step_symbol(
            market_symbol
        )
        resolved_contract_type = self.resolve_contract_type(
            direction
        )

        try:
            amount = self.validate_stake(stake)
            expiry = self.validate_expiry(30)
        except ValueError as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
            }

        try:
            async with websockets.connect(
                ws_url,
                open_timeout=15,
                ping_timeout=20,
                ping_interval=20,
            ) as websocket:
                proposal = await self._create_proposal_on_ws(
                    websocket=websocket,
                    symbol=resolved_symbol,
                    direction=resolved_contract_type,
                    stake=amount,
                    expiry_seconds=expiry,
                    currency=currency,
                )

                buy = await self._buy_on_ws(
                    websocket=websocket,
                    proposal_id=proposal["proposal_id"],
                    price=proposal["ask_price"],
                )

                return {
                    "status": "accepted",
                    "account_id": account_id,
                    "symbol": resolved_symbol,
                    "underlying_symbol": resolved_symbol,
                    "direction": direction,
                    "contract_type": resolved_contract_type,
                    "stake": amount,
                    "expiry_seconds": expiry,
                    "proposal_id": proposal["proposal_id"],
                    "contract_id": buy["contract_id"],
                    "buy_price": buy["buy_price"],
                    "balance_after": buy["balance_after"],
                    "shortcode": buy["shortcode"],
                }

        except UnknownBuyOutcome as exc:
            return {
                "status": "unknown_buy_outcome",
                "reason": str(exc),
                "account_id": account_id,
                "symbol": resolved_symbol,
                "contract_type": resolved_contract_type,
            }

        except Exception as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
                "account_id": account_id,
                "symbol": resolved_symbol,
                "contract_type": resolved_contract_type,
            }

    async def monitor_contract(
        self,
        contract_id: str,
        ws_url: str,
        timeout_seconds: int = 75,
    ) -> dict[str, Any]:
        """
        Monitor a purchased contract using a fresh authenticated OTP URL.

        A fresh OTP is required because each OTP URL is one-use.
        """
        if not contract_id:
            raise ValueError("MISSING_CONTRACT_ID")

        if not ws_url:
            raise ValueError("MISSING_AUTHENTICATED_WS_URL")

        req_id = self.next_req_id()

        payload = {
            "proposal_open_contract": 1,
            "contract_id": str(contract_id),
            "subscribe": 1,
            "req_id": req_id,
        }

        async with websockets.connect(
            ws_url,
            open_timeout=15,
            ping_timeout=20,
            ping_interval=20,
        ) as websocket:
            await websocket.send(
                json.dumps(payload)
            )

            deadline = asyncio.get_running_loop().time() + timeout_seconds

            while True:
                remaining = deadline - asyncio.get_running_loop().time()

                if remaining <= 0:
                    raise TimeoutError(
                        "CONTRACT_MONITOR_TIMEOUT"
                    )

                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=remaining,
                )

                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")

                response = json.loads(raw)

                if not isinstance(response, dict):
                    continue

                if "error" in response:
                    raise RuntimeError(
                        self._error_message(response)
                    )

                contract = response.get(
                    "proposal_open_contract"
                )

                if not isinstance(contract, dict):
                    continue

                if str(contract.get("contract_id")) != str(
                    contract_id
                ):
                    continue

                is_sold = bool(
                    contract.get("is_sold")
                )

                if is_sold:
                    return {
                        "status": "settled",
                        "contract_id": str(contract_id),
                        "is_sold": True,
                        "status_text": contract.get(
                            "status"
                        ),
                        "profit": contract.get(
                            "profit"
                        ),
                        "payout": contract.get(
                            "payout"
                        ),
                        "buy_price": contract.get(
                            "buy_price"
                        ),
                        "sell_price": contract.get(
                            "sell_price"
                        ),
                        "entry_tick": contract.get(
                            "entry_tick"
                        ),
                        "exit_tick": contract.get(
                            "exit_tick"
                        ),
                        "contract": contract,
                    }

    @classmethod
    def calculate_next_stake(
        cls,
        base_stake: float,
        martingale_step: int,
        multiplier: float = 2.0,
    ) -> float:
        base = cls.validate_stake(base_stake)

        step = max(
            1,
            int(martingale_step),
        )

        return round(
            base * (float(multiplier) ** (step - 1)),
            2,
        )