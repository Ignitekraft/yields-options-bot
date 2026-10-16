from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from statistics import median
import time
from typing import Any

from app.config import settings
from app.models.market import MarketSnapshot

BAR_SECONDS = 5
FRESH_TICK_SECONDS = 10
MIN_FIVE_SECOND_BARS = 100
MIN_ONE_MINUTE_BARS = 60
ATR_PERIOD = 14
ATR_BASELINE_BARS = 60
ATR_MIN_RATIO = 0.5
ATR_MAX_RATIO = 2.5
MOMENTUM_MIN_ATR_RATIO = 0.15
RSI_PERIOD = 7
RSI_LOWER_VETO = 20.0
RSI_UPPER_VETO = 80.0
logger = logging.getLogger(__name__)


def _ema_series(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    seed_index = period - 1
    current = sum(values[:period]) / period
    result[seed_index] = current
    multiplier = 2.0 / (period + 1)
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        result[index] = current
    return result


def _rsi_series(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    changes = [
        current - previous
        for previous, current in zip(values, values[1:])
    ]
    average_gain = sum(max(change, 0.0) for change in changes[:period]) / period
    average_loss = sum(max(-change, 0.0) for change in changes[:period]) / period

    def rsi(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        relative_strength = gain / loss
        return 100.0 - (100.0 / (1.0 + relative_strength))

    result[period] = rsi(average_gain, average_loss)
    for index in range(period + 1, len(values)):
        change = changes[index - 1]
        average_gain = (
            (average_gain * (period - 1) + max(change, 0.0)) / period
        )
        average_loss = (
            (average_loss * (period - 1) + max(-change, 0.0)) / period
        )
        result[index] = rsi(average_gain, average_loss)
    return result


def _aggregate_ticks(
    ticks: list[dict[str, Any]],
    interval_seconds: int,
) -> list[dict[str, float | int]]:
    buckets: dict[int, dict[str, float | int]] = {}
    for tick in ticks:
        epoch = int(tick["epoch"])
        quote = float(tick["quote"])
        bucket_epoch = epoch - epoch % interval_seconds
        bar = buckets.get(bucket_epoch)
        if bar is None:
            buckets[bucket_epoch] = {
                "epoch": bucket_epoch,
                "open": quote,
                "high": quote,
                "low": quote,
                "close": quote,
            }
        else:
            bar["high"] = max(float(bar["high"]), quote)
            bar["low"] = min(float(bar["low"]), quote)
            bar["close"] = quote

    if not buckets:
        return []

    result: list[dict[str, float | int]] = []
    epochs = sorted(buckets)
    previous_close: float | None = None
    for epoch in range(epochs[0], epochs[-1] + interval_seconds, interval_seconds):
        bar = buckets.get(epoch)
        if bar is None:
            if previous_close is None:
                continue
            bar = {
                "epoch": epoch,
                "open": previous_close,
                "high": previous_close,
                "low": previous_close,
                "close": previous_close,
            }
        previous_close = float(bar["close"])
        result.append(bar)
    return result


def _atr_series(bars: list[dict[str, float | int]], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) <= period:
        return result

    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        high = float(bar["high"])
        low = float(bar["low"])
        previous_close = float(bars[index - 1]["close"]) if index else float(bar["close"])
        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )

    current = sum(true_ranges[1 : period + 1]) / period
    result[period] = current
    for index in range(period + 1, len(bars)):
        current = ((current * (period - 1)) + true_ranges[index]) / period
        result[index] = current
    return result


def _empty_snapshot(
    symbol: str,
    display_name: str,
    data_quality: str,
    reason: str,
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        display_name=display_name,
        current_price=0.0,
        trend="neutral",
        data_quality=data_quality,
        indicators={"signal_reason": reason},
    )


@dataclass
class SignalResult:
    symbol: str
    signal: str
    signal_strength: float
    message: str
    direction: str


class YieldsScanner:
    """Analyze Deriv ticks using short-term trend, momentum, and volatility."""

    def normalize_step_market(
        self,
        symbol: str,
        display_name: str,
        history: list[dict[str, Any]],
    ) -> MarketSnapshot:
        if not history:
            return _empty_snapshot(
                symbol,
                display_name,
                "insufficient",
                "No Deriv tick history was returned.",
            )

        ticks = []
        for item in history:
            epoch = int(item["epoch"])
            quote = float(item["quote"])
            if epoch <= 0 or not math.isfinite(quote) or quote <= 0:
                raise ValueError("INVALID_DERIV_TICK_HISTORY")
            ticks.append({"epoch": epoch, "quote": quote})
        ticks.sort(key=lambda item: item["epoch"])
        if not ticks:
            raise ValueError("INVALID_DERIV_TICK_HISTORY")

        latest_epoch = int(ticks[-1]["epoch"])
        current_price = float(ticks[-1]["quote"])
        five_second_bars = _aggregate_ticks(ticks, BAR_SECONDS)
        one_minute_bars = _aggregate_ticks(ticks, 60)
        if (
            len(five_second_bars) < MIN_FIVE_SECOND_BARS
            or len(one_minute_bars) < MIN_ONE_MINUTE_BARS
        ):
            return _empty_snapshot(
                symbol,
                display_name,
                "insufficient",
                "Not enough live history for the 5-second and 1-minute filters.",
            )

        age_seconds = time.time() - latest_epoch
        if age_seconds < -5:
            return _empty_snapshot(
                symbol,
                display_name,
                "invalid",
                "Latest Deriv tick is unexpectedly in the future.",
            )
        if age_seconds > FRESH_TICK_SECONDS:
            return _empty_snapshot(
                symbol,
                display_name,
                "stale",
                f"Latest Deriv tick is {age_seconds} seconds old.",
            )

        five_second_closes = [float(bar["close"]) for bar in five_second_bars]
        one_minute_closes = [float(bar["close"]) for bar in one_minute_bars]
        ema8 = _ema_series(five_second_closes, 8)
        ema21 = _ema_series(five_second_closes, 21)
        ema20_1m = _ema_series(one_minute_closes, 20)
        ema50_1m = _ema_series(one_minute_closes, 50)
        rsi_values = _rsi_series(five_second_closes, RSI_PERIOD)
        atr_values = _atr_series(five_second_bars, ATR_PERIOD)

        fast_ema = ema8[-1]
        slow_ema = ema21[-1]
        slow_ema_prior = ema21[-4]
        trend_ema = ema20_1m[-1]
        context_ema = ema50_1m[-1]
        rsi = rsi_values[-1]
        atr = atr_values[-1]
        if any(
            value is None
            for value in (
                fast_ema,
                slow_ema,
                slow_ema_prior,
                trend_ema,
                context_ema,
                rsi,
                atr,
            )
        ):
            return _empty_snapshot(
                symbol,
                display_name,
                "insufficient",
                "Indicator warm-up history is incomplete.",
            )

        usable_atr = [
            value for value in atr_values[-ATR_BASELINE_BARS:]
            if value is not None and value > 0
        ]
        if not usable_atr:
            return _empty_snapshot(
                symbol,
                display_name,
                "insufficient",
                "Volatility baseline could not be calculated.",
            )
        atr_baseline = median(usable_atr)
        atr_ratio = atr / atr_baseline if atr_baseline > 0 else 0.0
        momentum_move = five_second_closes[-1] - five_second_closes[-4]
        roc3 = momentum_move / five_second_closes[-4] * 100.0
        momentum_atr_ratio = abs(momentum_move) / atr if atr > 0 else 0.0
        momentum_ok = momentum_atr_ratio >= MOMENTUM_MIN_ATR_RATIO
        ema21_slope = slow_ema - slow_ema_prior
        volatility_ok = ATR_MIN_RATIO <= atr_ratio <= ATR_MAX_RATIO
        long_conditions = (
            fast_ema > slow_ema,
            ema21_slope > 0,
            roc3 > 0 and momentum_ok,
            current_price > slow_ema and current_price > trend_ema,
            trend_ema > context_ema,
            rsi < RSI_UPPER_VETO,
        )
        short_conditions = (
            fast_ema < slow_ema,
            ema21_slope < 0,
            roc3 < 0 and momentum_ok,
            current_price < slow_ema and current_price < trend_ema,
            trend_ema < context_ema,
            rsi > RSI_LOWER_VETO,
        )

        direction = "neutral"
        signal_reason = "Short-term trend and momentum are not aligned."
        if not volatility_ok:
            signal_reason = (
                "Current volatility is outside the normal range "
                f"({atr_ratio:.2f}x its recent baseline)."
            )
        elif not momentum_ok:
            signal_reason = (
                "3-bar momentum is too small relative to recent volatility."
            )
        elif all(long_conditions):
            direction = "bullish"
            signal_reason = "5-second trend and momentum align with 1-minute context."
        elif all(short_conditions):
            direction = "bearish"
            signal_reason = "5-second trend and momentum align with 1-minute context."
        elif (
            fast_ema > slow_ema
            and ema21_slope > 0
            and roc3 > 0
            and rsi >= RSI_UPPER_VETO
        ):
            signal_reason = "Upward move is stretched; RSI vetoed a new entry."
        elif (
            fast_ema < slow_ema
            and ema21_slope < 0
            and roc3 < 0
            and rsi <= RSI_LOWER_VETO
        ):
            signal_reason = "Downward move is stretched; RSI vetoed a new entry."

        signal_strength = (
            abs(fast_ema - slow_ema) + abs(roc3) / 100.0 * current_price
        ) / atr if atr > 0 else 0.0
        indicators: dict[str, Any] = {
            "rsi7": round(rsi, 2),
            "ema8_5s": round(fast_ema, 6),
            "ema21_5s": round(slow_ema, 6),
            "ema21_slope_3bars": round(ema21_slope, 6),
            "roc3_pct": round(roc3, 5),
            "momentum_atr_ratio": round(momentum_atr_ratio, 3),
            "atr14_5s": round(atr, 6),
            "atr_baseline": round(atr_baseline, 6),
            "atr_ratio": round(atr_ratio, 3),
            "ema20_1m": round(trend_ema, 6),
            "ema50_1m": round(context_ema, 6),
            "signal_strength": round(signal_strength, 4),
            "signal_reason": signal_reason,
            "bars_5s": len(five_second_bars),
            "bars_1m": len(one_minute_bars),
        }

        return MarketSnapshot(
            symbol=symbol,
            display_name=display_name,
            current_price=current_price,
            prices=five_second_closes,
            timestamps=[
                str(bar["epoch"])
                for bar in five_second_bars
            ],
            trend=direction,
            momentum=roc3,
            volatility=atr,
            recent_high=max(float(bar["high"]) for bar in five_second_bars[-12:]),
            recent_low=min(float(bar["low"]) for bar in five_second_bars[-12:]),
            indicators=indicators,
            data_quality="good",
        )

    def collect_step_market_snapshots(self, market_data_service) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        for symbol in settings.auto_trade_markets:
            display_name = symbol.replace("step_", "Step ").replace("_", " ").title()
            try:
                history = market_data_service.get_historical_ticks(
                    symbol,
                    count=5000,
                )
                snapshots.append(
                    self.normalize_step_market(
                        symbol,
                        display_name,
                        history,
                    )
                )
            except Exception:
                logger.exception(
                    "Could not scan Deriv market %s",
                    symbol,
                )
                snapshots.append(
                    MarketSnapshot(
                        symbol=symbol,
                        display_name=display_name,
                        current_price=0.0,
                        trend="neutral",
                        indicators={
                            "signal_reason": (
                                f"Live market data for {display_name} "
                                "was unavailable."
                            )
                        },
                        data_quality="unavailable",
                    )
                )
        return snapshots

    def analyze_history(
        self,
        history: list[dict[str, Any]],
        symbol: str | None = None,
    ) -> SignalResult:
        target_symbol = symbol or (
            str(history[-1].get("symbol", "UNKNOWN")) if history else "UNKNOWN"
        )
        snapshot = self.normalize_step_market(
            target_symbol,
            target_symbol,
            history,
        )
        signal = (
            "BUY" if snapshot.trend == "bullish"
            else "SELL" if snapshot.trend == "bearish"
            else "HOLD"
        )
        strength = float(snapshot.indicators.get("signal_strength", 0.0))
        return SignalResult(
            symbol=target_symbol,
            signal=signal,
            signal_strength=strength if signal != "HOLD" else 0.0,
            message=snapshot.indicators.get(
                "signal_reason",
                "No reliable live setup.",
            ),
            direction=snapshot.trend,
        )

    def analyze_live_tick(
        self,
        history: list[dict[str, Any]],
        live_tick: dict[str, Any] | None,
        symbol: str | None = None,
    ) -> SignalResult:
        analysis_history = list(history)
        if live_tick is not None:
            analysis_history.append(live_tick)
        return self.analyze_history(analysis_history, symbol=symbol)

    def build_signal(
        self,
        previous: float,
        current: float,
        volatility: float,
        symbol: str,
    ) -> SignalResult:
        return SignalResult(
            symbol=symbol,
            signal="HOLD",
            signal_strength=0.0,
            message="A two-point price change is not sufficient for a trade signal.",
            direction="flat",
        )
