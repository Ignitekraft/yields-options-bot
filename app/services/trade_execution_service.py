from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import itertools
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable

import websockets

from app.config import settings
from app.services.deriv.websocket_connection import (
    connect_async_with_retry,
    defer_deriv_requests_after_rate_limit,
    is_deriv_rate_limit_error,
    send_deriv_websocket_request,
)

logger = logging.getLogger(__name__)


class UnknownBuyOutcome(RuntimeError):
    """The purchase may have succeeded; never automatically retry it."""


class DerivRequestError(RuntimeError):
    """A definite rejection returned by Deriv."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class TradeExecutionService:
    """Execute Deriv Rise/Fall contracts on a verified Options account."""

    STEP_MARKET_MAP = {
        "step_100": "STPRNG",
        "step_200": "STPRNG2",
        "step_300": "STPRNG3",
        "step_400": "STPRNG4",
        "step_500": "STPRNG5",
    }

    _req_counter = itertools.count(1)

    def __init__(self) -> None:
        self.martingale_multiplier = float(
            getattr(settings, "martingale_multiplier", 2.3)
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
    def normalize_contract_id(contract_id: Any) -> int | str:
        if contract_id is None:
            raise ValueError("MISSING_CONTRACT_ID")

        if isinstance(contract_id, int):
            return contract_id

        if isinstance(contract_id, float):
            if math.isfinite(contract_id):
                return int(contract_id)
            raise ValueError("INVALID_CONTRACT_ID")

        if isinstance(contract_id, str):
            stripped = contract_id.strip()
            if not stripped:
                raise ValueError("MISSING_CONTRACT_ID")
            try:
                return int(stripped)
            except ValueError:
                return stripped

        try:
            return int(contract_id)
        except (TypeError, ValueError):
            return str(contract_id)

    @classmethod
    def asset_candidates(cls, symbol: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def add(value: str | None) -> None:
            if not value:
                return
            normalized = str(value).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                candidates.append(normalized)

        try:
            add(cls.normalize_step_symbol(symbol))
        except ValueError:
            add(str(symbol or "").strip())

        for value in (
            "STPRNG",
            "STPRNG2",
            "STPRNG3",
            "STPRNG4",
            "STPRNG5",
        ):
            add(value)

        return candidates

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
        subscribe: bool = True,
    ) -> dict[str, Any]:
        payload = {
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
        if subscribe:
            payload["subscribe"] = 1
        return payload

    @staticmethod
    def _error_message(response: dict[str, Any]) -> str:
        error = response.get("error")

        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")

            if (
                code == "ContractBuyValidationError"
                and isinstance(message, str)
                and "maximum payout" in message.lower()
            ):
                return f"The proposed payout exceeds Deriv's maximum. {message}"

            if code and message:
                return f"{code}:{message}"

            if message:
                return str(message)

        return "DERIV_REQUEST_FAILED"

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
                deadline - asyncio.get_running_loop().time()
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

            if "error" in response:
                error = response.get("error")

                if isinstance(error, dict):
                    code = error.get("code")
                    deriv_error = DerivRequestError(
                        cls._error_message(response),
                        code=str(code) if code else None,
                    )
                    if is_deriv_rate_limit_error(deriv_error):
                        defer_deriv_requests_after_rate_limit(deriv_error)
                    raise deriv_error

                raise DerivRequestError(
                    cls._error_message(response)
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
            raise ValueError("DERIV_REQUEST_ID_MISSING")

        await send_deriv_websocket_request(websocket, payload)

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
        subscribe: bool = True,
    ) -> dict[str, Any]:
        payload = cls.build_proposal_payload(
            symbol=symbol,
            direction=direction,
            stake=stake,
            expiry_seconds=expiry_seconds,
            currency=currency,
            subscribe=subscribe,
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

        # Once the buy request has been sent, never automatically
        # retry it unless Deriv has explicitly rejected it.
        try:
            await send_deriv_websocket_request(websocket, payload)
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
            # This is a definite rejection. No contract was bought.
            raise
        except Exception as exc:
            # Timeout / connection problem after sending the buy:
            # purchase status is uncertain, so DO NOT retry.
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

        if contract_id is None:
            raise RuntimeError(
                "DERIV_CONTRACT_ID_MISSING"
            )

        normalized_contract_id = cls.normalize_contract_id(contract_id)

        return {
            "contract_id": normalized_contract_id,
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

    @asynccontextmanager
    async def _websocket_session(self, ws_url: str, websocket: Any | None):
        if websocket is not None:
            yield websocket
            return

        connection = await connect_async_with_retry(
            websockets.connect,
            ws_url,
            open_timeout=15,
            ping_timeout=20,
            ping_interval=20,
        )
        async with connection as connected_websocket:
            yield connected_websocket

    @classmethod
    @classmethod
    def _is_unknown_contract_proposal_error(
        cls,
        error: Exception,
    ) -> bool:
        message = str(error).strip().lower()

        return (
            "invalidcontractproposal" in message
            and "unknown contract proposal" in message
        )

    @classmethod
    def _is_invalid_asset_error(
        cls,
        error: Exception,
    ) -> bool:
        message = str(error).strip().lower()

        return (
            "trading is not offered for this asset" in message
            or "not offered for this asset" in message
            or "invalidinputasset" in message
            or "invalid asset" in message
        )

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
        websocket: Any | None = None,
        prepared_proposals: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Place one Rise/Fall contract on the selected account."""

        del user_id
        del access_token

        proposal_id: str | None = None

        account_mode = (
            str(account_type or "")
            .strip()
            .lower()
        )

        try:
            account_mode = self.validate_account_mode(
                account_mode
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

            contract_type = self.resolve_contract_type(
                direction
            )

            amount = self.validate_stake(stake)
            expiry = self.validate_expiry(30)

        except ValueError as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
                "account_type": (
                    account_mode or None
                ),
                "account_id": account_id or None,
            }

        last_error: Exception | None = None
        reuse_websocket = websocket is not None
        try:
            async with self._websocket_session(ws_url, websocket) as websocket:
                candidates = self.asset_candidates(symbol)
                for candidate_symbol in candidates:
                    try:
                        proposal_key = (
                            f"{candidate_symbol}:{amount:.2f}"
                        )
                        proposal = (
                            (prepared_proposals or {}).get(proposal_key)
                        )
                        if proposal is None:
                            proposal = await self._create_proposal_on_ws(
                                websocket=websocket,
                                symbol=candidate_symbol,
                                direction=contract_type,
                                stake=amount,
                                expiry_seconds=expiry,
                                currency=currency,
                                subscribe=not reuse_websocket,
                            )
                        else:
                            logger.info(
                                "Using a preloaded Deriv proposal for the next "
                                "Auto Trade contract"
                            )

                        proposal_id = proposal["proposal_id"]

                        try:
                            buy = await self._buy_on_ws(
                                websocket=websocket,
                                proposal_id=proposal_id,
                                price=proposal["ask_price"],
                            )
                        except DerivRequestError as exc:
                            if not self._is_unknown_contract_proposal_error(exc):
                                if self._is_invalid_asset_error(exc) and candidate_symbol != candidates[-1]:
                                    last_error = exc
                                    continue
                                raise

                            proposal = (
                                await self._create_proposal_on_ws(
                                    websocket=websocket,
                                    symbol=candidate_symbol,
                                    direction=contract_type,
                                    stake=amount,
                                    expiry_seconds=expiry,
                                    currency=currency,
                                    subscribe=not reuse_websocket,
                                )
                            )

                            proposal_id = proposal["proposal_id"]
                            buy = await self._buy_on_ws(
                                websocket=websocket,
                                proposal_id=proposal_id,
                                price=proposal["ask_price"],
                            )

                        return {
                            "status": "accepted",
                            "account_type": account_mode,
                            "account_id": account_id,
                            "symbol": candidate_symbol,
                            "underlying_symbol": candidate_symbol,
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
                        if self._is_invalid_asset_error(exc) and candidate_symbol != candidates[-1]:
                            last_error = exc
                            continue
                        raise

                if last_error is not None:
                    raise last_error
                raise RuntimeError("DERIV_NO_VALID_ASSET_CANDIDATES")

        except UnknownBuyOutcome as exc:
            return {
                "status": "unknown_buy_outcome",
                "reason": str(exc),
                "account_type": account_mode,
                "account_id": account_id,
                "symbol": (
                    locals().get("symbol")
                ),
                "contract_type": (
                    locals().get("contract_type")
                ),
                "proposal_id": proposal_id,
            }

        except DerivRequestError as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
                "deriv_error_code": exc.code,
                "account_type": account_mode,
                "account_id": account_id,
                "symbol": (
                    locals().get("symbol")
                ),
                "contract_type": (
                    locals().get("contract_type")
                ),
                "proposal_id": proposal_id,
            }

        except Exception as exc:
            return {
                "status": "rejected",
                "reason": str(exc),
                "account_type": account_mode,
                "account_id": account_id,
                "symbol": (
                    locals().get("symbol")
                ),
                "contract_type": (
                    locals().get("contract_type")
                ),
                "proposal_id": proposal_id,
            }

    async def monitor_contract(
        self,
        contract_id: str,
        ws_url: str,
        timeout_seconds: int = 75,
        market_symbol: str | None = None,
        on_countdown: Callable[[int], Awaitable[None]] | None = None,
        on_settlement: Callable[
            [dict[str, Any], Any, dict[str, dict[str, Any]]],
            Awaitable[Any],
        ] | None = None,
        preload_proposals: list[dict[str, Any]] | None = None,
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
        normalized_contract_id = self.normalize_contract_id(contract_id)
        tick_req_id = self.next_req_id() if market_symbol else None

        payload = {
            "proposal_open_contract": 1,
            "contract_id": normalized_contract_id,
            "subscribe": 1,
            "req_id": req_id,
        }

        connection = await connect_async_with_retry(
            websockets.connect,
            ws_url,
            open_timeout=15,
            ping_timeout=20,
            ping_interval=20,
        )
        async with connection as websocket:
            countdown_tasks: set[asyncio.Task[None]] = set()
            proposal_request_keys: dict[int, str] = {}
            prepared_proposals: dict[str, dict[str, Any]] = {}

            await send_deriv_websocket_request(websocket, payload)
            if market_symbol and tick_req_id is not None:
                try:
                    await send_deriv_websocket_request(
                        websocket,
                        {
                            "ticks": self.normalize_step_symbol(market_symbol),
                            "subscribe": 1,
                            "req_id": tick_req_id,
                        },
                    )
                except Exception:
                    logger.exception(
                        "Could not subscribe to live ticks while monitoring "
                        "contract %s; continuing settlement monitoring",
                        contract_id,
                    )
                    tick_req_id = None

            for preload in preload_proposals or []:
                try:
                    proposal_payload = self.build_proposal_payload(
                        symbol=str(preload["symbol"]),
                        direction=str(preload["direction"]),
                        stake=float(preload["stake"]),
                        expiry_seconds=30,
                        currency=str(preload.get("currency") or "USD"),
                        subscribe=True,
                    )
                    proposal_request_keys[proposal_payload["req_id"]] = (
                        f"{self.normalize_step_symbol(str(preload['symbol']))}:"
                        f"{float(preload['stake']):.2f}"
                    )
                    await send_deriv_websocket_request(
                        websocket,
                        proposal_payload,
                    )
                except Exception:
                    logger.exception(
                        "Could not preload a proposal for the next Auto Trade"
                    )

            deadline = (
                asyncio.get_running_loop().time()
                + timeout_seconds
            )
            entry_price = None
            latest_tick = None
            last_countdown: int | None = None

            def schedule_countdown(seconds_remaining: int) -> None:
                if on_countdown is None:
                    return

                async def notify() -> None:
                    try:
                        await on_countdown(seconds_remaining)
                    except Exception:
                        logger.exception(
                            "Could not publish settlement countdown for "
                            "contract %s",
                            contract_id,
                        )

                task = asyncio.create_task(notify())
                countdown_tasks.add(task)
                task.add_done_callback(countdown_tasks.discard)

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

                echo_req = response.get(
                    "echo_req"
                )

                if isinstance(echo_req, dict):
                    proposal_req_id = echo_req.get("req_id")
                    proposal_key = proposal_request_keys.get(proposal_req_id)
                    if proposal_key is not None:
                        if "error" in response:
                            logger.warning(
                                "Deriv rejected a preloaded proposal: %s",
                                self._error_message(response),
                            )
                            proposal_request_keys.pop(proposal_req_id, None)
                            continue
                        proposal = response.get("proposal")
                        if isinstance(proposal, dict):
                            proposal_id = proposal.get("id")
                            try:
                                ask_price = float(proposal.get("ask_price"))
                            except (TypeError, ValueError):
                                ask_price = 0.0
                            if (
                                proposal_id
                                and math.isfinite(ask_price)
                                and ask_price > 0
                            ):
                                prepared_proposals[proposal_key] = {
                                    "proposal_id": str(proposal_id),
                                    "ask_price": ask_price,
                                }
                        continue

                tick = response.get("tick")
                if isinstance(echo_req, dict):
                    response_req_id = (
                        echo_req.get("req_id")
                    )
                    if (
                        response_req_id == tick_req_id
                        and "error" in response
                    ):
                        logger.warning(
                            "Deriv live tick subscription failed while monitoring "
                            "contract %s: %s; continuing settlement monitoring",
                            contract_id,
                            self._error_message(response),
                        )
                        tick_req_id = None
                        continue
                    if (
                        tick_req_id is not None
                        and response_req_id == tick_req_id
                    ):
                        if isinstance(tick, dict):
                            try:
                                quote = float(tick.get("quote"))
                            except (TypeError, ValueError):
                                continue
                            if math.isfinite(quote) and quote > 0:
                                latest_tick = quote
                                if entry_price is None:
                                    entry_price = quote
                        continue
                    if response_req_id is not None and response_req_id != req_id:
                        continue
                elif isinstance(tick, dict):
                    try:
                        quote = float(tick.get("quote"))
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(quote) and quote > 0:
                        latest_tick = quote
                        if entry_price is None:
                            entry_price = quote
                    continue

                if "error" in response:
                    raise DerivRequestError(
                        self._error_message(
                            response
                        )
                    )

                contract = response.get(
                    "proposal_open_contract"
                )

                if not isinstance(contract, dict):
                    continue

                if (
                    str(
                        contract.get(
                            "contract_id"
                        )
                    )
                    != str(normalized_contract_id)
                ):
                    continue

                if not bool(contract.get("is_sold")) and on_countdown is not None:
                    expiry_value = contract.get("date_expiry")
                    try:
                        expiry_epoch = float(expiry_value)
                    except (TypeError, ValueError):
                        expiry_epoch = 0.0
                    if math.isfinite(expiry_epoch) and expiry_epoch > 0:
                        seconds_remaining = math.ceil(expiry_epoch - time.time())
                        if (
                            1 <= seconds_remaining <= 30
                            and seconds_remaining != last_countdown
                        ):
                            schedule_countdown(seconds_remaining)
                            last_countdown = seconds_remaining

                if entry_price is None:
                    entry_price = self._contract_price(
                        contract,
                        "entry_tick",
                        "entry_spot",
                        "entry_tick_display_value",
                        "entry_spot_display_value",
                        "current_spot",
                        "spot",
                    )

                if bool(
                    contract.get("is_sold")
                ):
                    for task in countdown_tasks:
                        task.cancel()
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

                    settlement = {
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
                        "entry_tick": (
                            self._contract_price(
                                contract,
                                "entry_tick",
                                "entry_spot",
                                "entry_tick_display_value",
                                "entry_spot_display_value",
                            )
                            or entry_price
                        ),
                        "exit_tick": (
                            self._contract_price(
                                contract,
                                "exit_tick",
                                "exit_spot",
                                "exit_tick_display_value",
                                "exit_spot_display_value",
                                "current_spot",
                                "spot",
                            )
                            or latest_tick
                        ),
                        "contract": contract,
                    }
                    if on_settlement is not None:
                        try:
                            settlement["next_trade_result"] = await on_settlement(
                                settlement,
                                websocket,
                                prepared_proposals,
                            )
                        except Exception:
                            logger.exception(
                                "Could not start the next trade on the settled "
                                "contract connection"
                            )
                            settlement["next_trade_result"] = {
                                "status": "unknown_buy_outcome",
                                "reason": "AUTO_TRADE_EXECUTION_INTERRUPTED",
                            }
                    return settlement

    @staticmethod
    def _contract_price(contract: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = contract.get(key)
            if isinstance(value, dict):
                value = value.get("display_value", value.get("value"))
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(price) and price > 0:
                return price
        return None

    @classmethod
    def calculate_next_stake(
        cls,
        base_stake: float,
        martingale_step: int,
        multiplier: float = 2.3,
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