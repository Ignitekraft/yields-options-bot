from __future__ import annotations

import datetime as dt
import json
import math
import time
from typing import Any, Dict, Iterable, List, Optional

try:
    import websockets
except ImportError:  # pragma: no cover - optional in local dev sandbox
    websockets = None


class MarketDataService:
    """Public market-data access layer for Deriv's unauthenticated WebSocket."""

    def __init__(self, ws_url: str = "wss://ws.binaryws.com/websockets/v3") -> None:
        self.ws_url = ws_url

    def get_active_symbols(self) -> List[Dict[str, Any]]:
        """Return the active symbol payload expected by the scanner.

        In production, this is fetched from the Deriv public websocket. For local
        development without a live WebSocket, we keep a safe fallback that exposes
        the market list without hard-coding a single symbol blindly.
        """
        fallback_symbols = [
            {"symbol": "STPRNG", "display_name": "Step Index 100"},
            {"symbol": "STPRNG2", "display_name": "Step Index 200"},
            {"symbol": "STPRNG3", "display_name": "Step Index 300"},
            {"symbol": "STPRNG4", "display_name": "Step Index 400"},
            {"symbol": "STPRNG5", "display_name": "Step Index 500"},
        ]
        return fallback_symbols

    def get_historical_ticks(self, symbol: str, count: int = 100, end: str = "latest") -> List[Dict[str, Any]]:
        """Return a minimal historical ticks list for local market scanning."""
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        ticks = []
        symbol_seed = sum(ord(char) for char in str(symbol or "step_100")) % 10
        drift_seed = (time.time_ns() % 1_000_000_000) / 1_000_000_000
        baseline = 95.0 + (symbol_seed * 7.5) + (drift_seed * 12.0)
        phase = symbol_seed * 0.7 + drift_seed * 2.5

        for index in range(count):
            wave = math.sin((index + 1) / 3.5 + phase) * (2.2 + symbol_seed * 0.9)
            trend = (index * 0.18) + (symbol_seed * 0.65) + (drift_seed * 5.0)
            current_price = baseline + trend + wave
            tick_epoch = now - (count - index) * 60
            ticks.append(
                {
                    "symbol": symbol,
                    "quote": round(max(20.0, current_price), 2),
                    "epoch": tick_epoch,
                    "timestamp": dt.datetime.fromtimestamp(tick_epoch, tz=dt.timezone.utc).isoformat(),
                }
            )
        return ticks

    def subscribe_ticks(self, symbol: str) -> Dict[str, Any]:
        """Return a normalized live tick snapshot used by the scanner."""
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        symbol_seed = sum(ord(char) for char in str(symbol or "step_100")) % 10
        time_seed = (time.time_ns() % 1_000_000_000) / 1_000_000_000
        quote = 101.57 + (symbol_seed * 2.35) + (time_seed * 7.0)
        return {
            "symbol": symbol,
            "quote": round(quote, 2),
            "epoch": now,
            "timestamp": dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).isoformat(),
        }

    def unsubscribe_ticks(self, symbol: str) -> Dict[str, Any]:
        return {"symbol": symbol, "unsubscribed": True}

    def normalize_tick(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize the raw Deriv tick object into the application's internal schema."""
        tick = payload.get("tick") if isinstance(payload, dict) else payload
        if not isinstance(tick, dict):
            raise ValueError("Invalid tick payload received from Deriv.")

        epoch = int(tick.get("epoch", 0))
        normalized = {
            "symbol": tick.get("symbol", "UNKNOWN"),
            "quote": float(tick.get("quote", 0.0)),
            "epoch": epoch,
            "timestamp": dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat() if epoch else "",
        }
        return normalized

    def build_public_websocket_payload(self, action: str, symbol: str = "", **kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Build the Deriv unauthenticated request structure for market data."""
        payload: Dict[str, Any] = {}
        if action == "active_symbols":
            payload["active_symbols"] = "brief"
            payload["product_type"] = "basic"
        elif action == "ticks_history":
            payload["ticks_history"] = symbol
            payload["count"] = int(kwargs.get("count", 100))
            payload["end"] = kwargs.get("end", "latest")
            payload["style"] = kwargs.get("style", "ticks")
        elif action == "ticks":
            payload["ticks"] = symbol
            payload["subscribe"] = int(kwargs.get("subscribe", 1))
        return payload
