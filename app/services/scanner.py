from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import settings
from app.models.market import MarketSnapshot


def _moving_average(values: List[float], window: int) -> float:
    if not values:
        return 0.0
    recent = values[-window:] if len(values) >= window else values
    return sum(recent) / max(len(recent), 1)


def _calculate_rsi(values: List[float], period: int = 14) -> float:
    if len(values) < 2:
        return 50.0
    gains: List[float] = []
    losses: List[float] = []
    for previous, current in zip(values, values[1:]):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    if len(gains) < period:
        avg_gain = sum(gains) / max(len(gains), 1)
        avg_loss = sum(losses) / max(len(losses), 1)
    else:
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calculate_macd(values: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> tuple[float, float]:
    if len(values) < slow:
        return 0.0, 0.0
    fast_ema = _moving_average(values[-fast:], fast)
    slow_ema = _moving_average(values[-slow:], slow)
    macd = fast_ema - slow_ema
    if len(values) < slow + signal_period:
        signal = macd
    else:
        signal = _moving_average([macd] * max(1, signal_period), signal_period)
    return macd, signal


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

    def normalize_step_market(self, symbol: str, display_name: str, history: List[Dict[str, Any]]) -> MarketSnapshot:
        if not history:
            return MarketSnapshot(
                symbol=symbol,
                display_name=display_name,
                current_price=0.0,
                prices=[],
                timestamps=[],
                trend="neutral",
                momentum=0.0,
                volatility=0.0,
                recent_high=0.0,
                recent_low=0.0,
                indicators={},
                data_quality="insufficient",
            )

        prices = [float(item.get("quote", 0.0)) for item in history]
        recent_high = max(prices)
        recent_low = min(prices)
        current_price = prices[-1]
        previous_price = prices[-2] if len(prices) > 1 else current_price
        momentum = round(current_price - previous_price, 4)
        volatility = round((recent_high - recent_low) / max(len(prices), 1), 4)
        trend = "bullish" if momentum >= 0 else "bearish"
        ma50 = _moving_average(prices, 50)
        ma200 = _moving_average(prices, 200)
        rsi = _calculate_rsi(prices)
        macd, macd_signal = _calculate_macd(prices)

        return MarketSnapshot(
            symbol=symbol,
            display_name=display_name,
            current_price=current_price,
            prices=prices,
            timestamps=[str(item.get("timestamp", "")) for item in history],
            trend=trend,
            momentum=momentum,
            volatility=volatility,
            recent_high=recent_high,
            recent_low=recent_low,
            indicators={
                "price_change": momentum,
                "range": round(recent_high - recent_low, 4),
                "trend_strength": round(abs(momentum) / max(volatility, 0.01), 4),
                "rsi": round(rsi, 2),
                "macd": round(macd, 4),
                "macd_signal": round(macd_signal, 4),
                "ma50": round(ma50, 4),
                "ma200": round(ma200, 4),
            },
            data_quality="good",
        )

    def collect_step_market_snapshots(self, market_data_service) -> List[MarketSnapshot]:
        snapshots: List[MarketSnapshot] = []
        for symbol in settings.auto_trade_markets:
            display_name = symbol.replace("step_", "Step ").replace("_", " ").title()
            history = market_data_service.get_historical_ticks(symbol, count=25)
            snapshots.append(self.normalize_step_market(symbol, display_name, history))
        return snapshots
