from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from websockets.sync import client as websocket_client
except ImportError:  # pragma: no cover - optional in local dev sandbox
    websocket_client = None

from app.config import settings


class MarketDataService:
    """Public market-data access through Deriv's WebSocket API."""

    STEP_SYMBOLS = {
        "step_100": "STPRNG",
        "step_200": "STPRNG2",
        "step_300": "STPRNG3",
        "step_400": "STPRNG4",
        "step_500": "STPRNG5",
    }

    def __init__(
        self,
        ws_url: str | None = None,
        app_id: str | None = None,
    ) -> None:
        self.ws_url = ws_url or settings.deriv_public_ws_url
        self.app_id = (
            settings.deriv_app_id
            if app_id is None
            else str(app_id)
        )

    def _connection_url(self) -> str:
        parts = urlsplit(self.ws_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if self.app_id and not query.get("app_id"):
            query["app_id"] = self.app_id
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )

    def _request_public(self, payload: dict[str, Any]) -> dict[str, Any]:
        if websocket_client is None:
            raise RuntimeError("DERIV_WEBSOCKETS_CLIENT_UNAVAILABLE")

        request = {**payload, "req_id": 1}
        with websocket_client.connect(
            self._connection_url(),
            open_timeout=10,
            close_timeout=2,
        ) as connection:
            connection.send(json.dumps(request))
            response = json.loads(connection.recv(timeout=10))

        if not isinstance(response, dict):
            raise ValueError("DERIV_MARKET_DATA_INVALID_RESPONSE")

        error = response.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "UNKNOWN")
            message = str(error.get("message") or "Unknown Deriv API error")
            raise ValueError(f"DERIV_MARKET_DATA_ERROR:{code}:{message}")

        return response

    @classmethod
    def _api_symbol(cls, symbol: str) -> str:
        value = str(symbol or "").strip()
        return cls.STEP_SYMBOLS.get(value.lower(), value)

    def get_active_symbols(self) -> list[dict[str, Any]]:
        response = self._request_public(
            self.build_public_websocket_payload("active_symbols")
        )
        symbols = response.get("active_symbols")
        if not isinstance(symbols, list):
            raise ValueError("DERIV_ACTIVE_SYMBOLS_MISSING")

        return [
            {
                "symbol": str(item["symbol"]),
                "display_name": str(
                    item.get("display_name")
                    or item.get("underlying_symbol")
                    or item["symbol"]
                ),
            }
            for item in symbols
            if isinstance(item, dict) and item.get("symbol")
        ]

    def get_historical_ticks(
        self,
        symbol: str,
        count: int = 5000,
        end: str = "latest",
    ) -> list[dict[str, Any]]:
        if not symbol:
            raise ValueError("DERIV_TICK_HISTORY_SYMBOL_REQUIRED")
        if not 1 <= int(count) <= 5000:
            raise ValueError("DERIV_TICK_HISTORY_COUNT_OUT_OF_RANGE")

        response = self._request_public(
            self.build_public_websocket_payload(
                "ticks_history",
                self._api_symbol(symbol),
                count=count,
                end=end,
                style="ticks",
            )
        )
        history = response.get("history")
        if not isinstance(history, dict):
            raise ValueError("DERIV_TICK_HISTORY_MISSING")

        prices = history.get("prices")
        times = history.get("times")
        if not isinstance(prices, list) or not isinstance(times, list):
            raise ValueError("DERIV_TICK_HISTORY_INVALID")
        if len(prices) != len(times):
            raise ValueError("DERIV_TICK_HISTORY_LENGTH_MISMATCH")

        ticks: list[dict[str, Any]] = []
        for price, epoch_value in zip(prices, times):
            epoch = int(epoch_value)
            quote = float(price)
            if epoch <= 0 or not math.isfinite(quote) or quote <= 0:
                raise ValueError("DERIV_TICK_HISTORY_INVALID_TICK")
            ticks.append(
                {
                    "symbol": symbol,
                    "quote": quote,
                    "epoch": epoch,
                    "timestamp": dt.datetime.fromtimestamp(
                        epoch,
                        tz=dt.timezone.utc,
                    ).isoformat(),
                }
            )

        return ticks

    def subscribe_ticks(self, symbol: str) -> dict[str, Any]:
        if not symbol:
            raise ValueError("DERIV_TICK_SYMBOL_REQUIRED")
        response = self._request_public(
            self.build_public_websocket_payload(
                "ticks",
                self._api_symbol(symbol),
                subscribe=0,
            )
        )
        return self.normalize_tick(response)

    def unsubscribe_ticks(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "unsubscribed": True}

    def normalize_tick(self, payload: dict[str, Any]) -> dict[str, Any]:
        tick = payload.get("tick") if isinstance(payload, dict) else None
        if not isinstance(tick, dict):
            raise ValueError("DERIV_TICK_RESPONSE_MISSING")

        epoch = int(tick.get("epoch", 0))
        quote = float(tick.get("quote", 0.0))
        if epoch <= 0 or not math.isfinite(quote) or quote <= 0:
            raise ValueError("DERIV_TICK_RESPONSE_INVALID")

        return {
            "symbol": tick.get("symbol", "UNKNOWN"),
            "quote": quote,
            "epoch": epoch,
            "timestamp": dt.datetime.fromtimestamp(
                epoch,
                tz=dt.timezone.utc,
            ).isoformat(),
        }

    def build_public_websocket_payload(
        self,
        action: str,
        symbol: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if action == "active_symbols":
            payload["active_symbols"] = "brief"
            payload["product_type"] = "basic"
        elif action == "ticks_history":
            payload["ticks_history"] = symbol
            payload["count"] = int(kwargs.get("count", 5000))
            payload["end"] = kwargs.get("end", "latest")
            payload["style"] = kwargs.get("style", "ticks")
        elif action == "ticks":
            payload["ticks"] = symbol
            if int(kwargs.get("subscribe", 0)):
                payload["subscribe"] = 1
        else:
            raise ValueError(f"UNSUPPORTED_DERIV_MARKET_DATA_ACTION:{action}")
        return payload
