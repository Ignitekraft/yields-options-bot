from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class SignalResult:
    symbol: str
    signal: str
    confidence: float
    message: str
    direction: str


class YieldsScanner:
    """Independent scanner service responsible for converting market data into a signal."""

    def build_signal(self, previous: float, current: float, volatility: float, symbol: str) -> SignalResult:
        delta = float(current) - float(previous)
        if delta > 0:
            signal_label = "BUY"
            direction = "up"
        elif delta < 0:
            signal_label = "SELL"
            direction = "down"
        else:
            signal_label = "HOLD"
            direction = "flat"

        confidence = max(0.1, min(0.99, abs(delta) / max(abs(volatility) or 1.0, 1.0)))
        confidence = round(confidence, 2)
        message = f"{signal_label} {symbol} at {confidence:.2f} confidence"
        return SignalResult(
            symbol=symbol,
            signal=signal_label,
            confidence=confidence,
            message=message,
            direction=direction,
        )

    def analyze_history(self, history: List[Dict[str, Any]], symbol: Optional[str] = None) -> SignalResult:
        if not history:
            return SignalResult(
                symbol=symbol or "UNKNOWN",
                signal="HOLD",
                confidence=0.0,
                message="No market history available yet.",
                direction="flat",
            )

        previous = float(history[-2]["quote"]) if len(history) >= 2 else float(history[-1]["quote"])
        current = float(history[-1]["quote"])
        volatility = max(abs(current - previous), 1.0)
        target_symbol = symbol or history[-1].get("symbol", "UNKNOWN")
        return self.build_signal(previous, current, volatility, target_symbol)

    def analyze_live_tick(self, history: List[Dict[str, Any]], live_tick: Optional[Dict[str, Any]], symbol: Optional[str] = None) -> SignalResult:
        if live_tick is not None:
            analysis_history = list(history)
            analysis_history.append(live_tick)
            return self.analyze_history(analysis_history, symbol=symbol or live_tick.get("symbol"))
        return self.analyze_history(history, symbol=symbol)
