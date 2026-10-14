from __future__ import annotations

import asyncio
import itertools
import json
import math
from typing import Any

import websockets

from app.config import settings


class UnknownBuyOutcome(RuntimeError):
    """The buy request was sent, but its final outcome could not be confirmed."""


class DerivRequestError(RuntimeError):
    """Deriv explicitly rejected an API request."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.response = response or {}

        if code and message:
            text = f"{code}:{message}"
        else:
            text = message or "DERIV_REQUEST_FAILED"

        super().__init__(text)


class TradeExecutionService:
    """Execute Deriv Rise/Fall contracts on a verified Options account."""

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

        upper = value.upper()

        if upper in cls.STEP_MARKET_MAP.values():
            return upper

        raise ValueError(f"UNSUPPORTED_STEP_MARKET:{symbol}")

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
    def validate_account_mode(account_mode: str | None) -> str:
        normalized = str(account_mode or "").strip().lower()

        if normalized not in {"demo", "real"}:
            raise ValueError("INVALID_ACCOUNT_MODE")

        return normalized

    @staticmethod
    def validate_stake(stake: float) -> float:
        try:
            value = float(stake)
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_STAKE") from exc

        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                "STAKE_MUST_BE_FINITE_AND_GREATER_THAN_ZERO"
            )

        return round(value, 2)

    @staticmethod
    def validate_expiry(expiry_seconds: int) -> int:
        try:
            value = int(expiry_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_EXPIRY") from exc

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
        return {
            "proposal": 1,
            "amount": cls.validate_stake(stake),
            "basis": "stake",
            "contract_type": cls.resolve_contract_type(direction),
            "currency": str(currency or "USD").upper(),
            "duration": cls.validate_expiry(expiry_seconds),
            "duration_unit": "s",
            "underlying_symbol": cls.normalize_step_symbol(symbol),
            "req_id": cls.next_req_id(),
        }

    @staticmethod
    def _error_details(
        response: dict[str, Any],
    ) -> tuple[str | None, str]:
        error = response.get("error")

        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")

            return (
                str(code) if code else None,
                str(message)
                if message
                else "DERIV_REQUEST_FAILED",
            )

        return None, "DERIV_REQUEST_FAILED"

    @classmethod
    def _error_message(cls, response: dict[str, Any]) -> str:
        code, message = cls._error_details(response)

        if code:
            return f"{code}:{message}"

        return message

    @classmethod
    async def _receive_response(
        cls,
        websocket: Any,
        expected_key: str,
        req_id: int,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        deadline = (
            asyncio.get_running_loop().time() + timeout
        )

        while True:
            remaining = (
                deadline
                - asyncio.get_running_loop().time()
            )

            if remaining <= 0:
                raise TimeoutError(
                    f"DERIV_{expected_key.upper()}_TIMEOUT"
                )

            raw = await asyncio.wait_for(
                websocket.recv(),
                timeout=remaining,
            )

            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")

            try:
                response = json.loads(raw)
            except (TypeError, ValueError):
                continue

            if not isinstance(response, dict):
                continue

            echo_req = response.get("echo_req")

            if isinstance(echo_req, dict):
                response_req_id = echo_req.get("req_id")

                if (
                    response_req_id is not None
                    and response_req_id != req_id
                ):
                    continue

            # IMPORTANT:
            # If Deriv explicitly returns an error, that means
            # the request was rejected. This is NOT an unknown
            # buy outcome.
            if "error" in response:
                code, message = cls._error_details(response)

                raise DerivRequestError(
                    message,
                    code=code,
                    response=response,
                )

            if expected_key in response:
                return response

            if response.get("msg_type") == expected_key:
                return response

    @classmethod
    async def _request(
        cls,
        websocket: Any,
        payload: dict[str, Any],
        expected_key: str,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        req_id = payload.get("req_id")

        if not isinstance(req_id, int):
            raise ValueError(
                "DERIV_REQUEST_ID_MISSING"
            )

        await asyncio.wait_for(
            websocket.send(json.dumps(payload)),
            timeout=15,
        )

        return await cls._receive_response(
            websocket,
            expected_key,
            req_id,
            timeout,
        )

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
            "proposal",
        )

        proposal = response.get("proposal")

        if not isinstance(proposal, dict):
            raise RuntimeError(
                "DERIV_PROPOSAL_RESPONSE_MALFORMED"
            )

        proposal_id = proposal.get("id")

        try:
            ask_price = float(
                proposal.get("ask_price")
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "DERIV_PROPOSAL_PRICE_MISSING"
            ) from exc

        if not proposal_id:
            raise RuntimeError(
                "DERIV_PROPOSAL_ID_MISSING"
            )

        if (
            not math.isfinite(ask_price)
            or ask_price <= 0
        ):
            raise RuntimeError(
                "DERIV_PROPOSAL_PRICE_INVALID"
            )

        return {
            "proposal_id": str(proposal_id),
            "ask_price": ask_price,
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

        # Once this send begins, NEVER automatically retry it.
        # A retry could create a duplicate purchase if Deriv
        # accepted the original request but the response was lost.
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
            response = await cls._receive_response(
                websocket,
                "buy",
                req_id,
                timeout=20,
            )

        except DerivRequestError:
            # Deriv explicitly rejected the purchase.
            # This is a definite rejection, NOT an unknown outcome.
            raise

        except (
            asyncio.TimeoutError,
            TimeoutError,
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.ConnectionClosedError,
            websockets.exceptions.ConnectionClosedOK,
            OSError,
        ) as exc:
            # We sent the buy request but cannot establish whether
            # Deriv accepted it. Do NOT retry automatically.
            raise UnknownBuyOutcome(
                f"BUY_OUTCOME_UNKNOWN:{type(exc).__name__}"
            ) from exc

        except Exception as exc:
            # Any unexpected transport/processing failure after
            # sending the buy is still treated conservatively.
            raise UnknownBuyOutcome(
                f"BUY_OUTCOME_UNKNOWN:{type(exc).__name__}"
            ) from exc

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
        }

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
        account_type: str | None = None,
    ) -> dict[str, Any]:
        """Place one contract on the verified, explicitly selected account."""

        del user_id
        del access_token

        proposal_id: str | None = None
        account_mode = str(
            account_type or ""
        ).strip().lower()

        symbol: str | None = None
        contract_type: str | None = None
        amount: float | None = None
        expiry: int | None = None

        try:
            account_mode = (
                self.validate_account_mode(
                    account_mode
                )
            )

            if not account_id:
                raise ValueError(
                    "MISSING_OPTIONS_ACCOUNT_ID"
                )

            if not ws_url:
                raise ValueError(
                    "MISSING_AUTHENTICATED_WS_URL"
                )

            symbol = self.normalize_step_symbol(
                market_symbol
            )

            contract_type = (
                self.resolve_contract_type(
                    direction
                )
            )

            amount = self.validate_stake(
                stake
            )

            expiry = self.validate_expiry(30)

        except ValueError as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
                "account_type": (
                    account_mode or None
                ),
                "account_id": (
                    account_id or None
                ),
            }

        try:
            async with websockets.connect(
                ws_url,
                open_timeout=15,
                ping_timeout=20,
                ping_interval=20,
            ) as websocket:

                proposal = (
                    await self._create_proposal_on_ws(
                        websocket,
                        symbol,
                        contract_type,
                        amount,
                        expiry,
                        currency,
                    )
                )

                proposal_id = proposal[
                    "proposal_id"
                ]

                buy = await self._buy_on_ws(
                    websocket,
                    proposal_id,
                    proposal["ask_price"],
                )

                return {
                    "status": "accepted",
                    "account_type": account_mode,
                    "account_id": account_id,
                    "symbol": symbol,
                    "underlying_symbol": symbol,
                    "direction": direction,
                    "contract_type": contract_type,
                    "stake": amount,
                    "expiry_seconds": expiry,
                    "proposal_id": proposal_id,
                    "contract_id": buy[
                        "contract_id"
                    ],
                    "buy_price": buy[
                        "buy_price"
                    ],
                    "balance_after": buy[
                        "balance_after"
                    ],
                    "shortcode": buy[
                        "shortcode"
                    ],
                }

        except DerivRequestError as exc:
            # DEFINITE rejection from Deriv.
            # The contract was NOT purchased.
            return {
                "status": "rejected",
                "reason": str(exc),
                "account_type": account_mode,
                "account_id": account_id,
                "symbol": symbol,
                "contract_type": contract_type,
                "proposal_id": proposal_id,
                "deriv_error_code": exc.code,
                "deriv_error": exc.response.get(
                    "error"
                ),
            }

        except UnknownBuyOutcome as exc:
            # Buy request was sent, but the response could not
            # establish whether Deriv accepted it.
            return {
                "status": "unknown_buy_outcome",
                "reason": str(exc),
                "account_type": account_mode,
                "account_id": account_id,
                "symbol": symbol,
                "contract_type": contract_type,
                "proposal_id": proposal_id,
            }

        except Exception as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
                "account_type": account_mode,
                "account_id": account_id,
                "symbol": symbol,
                "contract_type": contract_type,
                "proposal_id": proposal_id,
            }

    async def monitor_contract(
        self,
        contract_id: str,
        ws_url: str,
        timeout_seconds: int = 75,
    ) -> dict[str, Any]:
        """Monitor a contract until Deriv reports it has settled."""

        if not contract_id:
            raise ValueError(
                "MISSING_CONTRACT_ID"
            )

        if not ws_url:
            raise ValueError(
                "MISSING_AUTHENTICATED_WS_URL"
            )

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

            deadline = (
                asyncio.get_running_loop().time()
                + timeout_seconds
            )

            while True:
                remaining = (
                    deadline
                    - asyncio.get_running_loop().time()
                )

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

                try:
                    response = json.loads(raw)
                except (TypeError, ValueError):
                    continue

                if not isinstance(response, dict):
                    continue

                if "error" in response:
                    raise DerivRequestError(
                        self._error_message(
                            response
                        ),
                        code=(
                            response.get(
                                "error",
                                {},
                            ).get("code")
                            if isinstance(
                                response.get(
                                    "error"
                                ),
                                dict,
                            )
                            else None
                        ),
                        response=response,
                    )

                contract = response.get(
                    "proposal_open_contract"
                )

                if not isinstance(
                    contract,
                    dict,
                ):
                    continue

                if str(
                    contract.get(
                        "contract_id"
                    )
                ) != str(contract_id):
                    continue

                if bool(
                    contract.get(
                        "is_sold"
                    )
                ):
                    try:
                        profit = float(
                            contract.get(
                                "profit"
                            )
                            or 0.0
                        )
                    except (
                        TypeError,
                        ValueError,
                    ):
                        profit = 0.0

                    try:
                        payout = float(
                            contract.get(
                                "payout"
                            )
                            or 0.0
                        )
                    except (
                        TypeError,
                        ValueError,
                    ):
                        payout = 0.0

                    return {
                        "status": "settled",
                        "contract_id": str(
                            contract_id
                        ),
                        "is_sold": True,
                        "status_text": contract.get(
                            "status"
                        ),
                        "profit": profit,
                        "payout": payout,
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
        base = cls.validate_stake(
            base_stake
        )

        step = max(
            1,
            int(martingale_step),
        )

        if (
            not math.isfinite(
                float(multiplier)
            )
            or float(multiplier) <= 0
        ):
            raise ValueError(
                "INVALID_MARTINGALE_MULTIPLIER"
            )

        return round(
            base
            * (
                float(multiplier)
                ** (step - 1)
            ),
            2,
        )