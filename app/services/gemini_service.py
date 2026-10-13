from __future__ import annotations

import json
import logging
from typing import Iterable

from app.config import settings
from app.models.ai_analysis import AutoTradeAnalysis, MarketAnalysis
from app.models.market import MarketSnapshot

logger = logging.getLogger(__name__)

try:
    from google import genai
except Exception:  # pragma: no cover - optional dependency in local dev sandbox
    genai = None


class GeminiService:
    """Pure analysis service for market-scanning and ranking. It never touches trading or OAuth state."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model

    def _build_prompt(self, snapshots: Iterable[MarketSnapshot]) -> str:
        snapshot_list = list(snapshots)
        market_lines = []
        for snapshot in snapshot_list:
            market_lines.append(
                "\n".join(
                    [
                        f"symbol={snapshot.symbol}",
                        f"display_name={snapshot.display_name}",
                        f"current_price={snapshot.current_price}",
                        f"trend={snapshot.trend}",
                        f"momentum={snapshot.momentum}",
                        f"volatility={snapshot.volatility}",
                        f"recent_high={snapshot.recent_high}",
                        f"recent_low={snapshot.recent_low}",
                        f"prices={snapshot.prices}",
                        f"indicators={json.dumps(snapshot.indicators, sort_keys=True)}",
                        f"data_quality={snapshot.data_quality}",
                    ]
                )
            )
        payload = "\n---\n".join(market_lines)
        return (
            "You are the market-analysis engine for Yields. "
            "You analyze only the real-time market data supplied by the application. "
            "Do not invent prices, indicators, candles, market conditions, or events. "
            "Base your analysis only on the supplied data.\n\n"
            "Your job is to compare the five configured Step Indices and identify the strongest current directional setup. "
            "Workflow: analyze every market independently, compare trend, momentum, RSI, MACD, MA50, MA200, volatility, and data quality, and select one winner only if the setup meets the minimum threshold.\n\n"
            "You MUST NOT: invent market data, invent prices, invent indicators, claim certainty, decide stake size, decide martingale size, decide expiry, execute trades, issue Deriv API commands, or guarantee profit.\n\n"
            "You MUST: identify BULLISH, BEARISH, or NO_TRADE bias for each market; score each market from 0.0 to 1.0 confidence and setup quality; rank all five markets; explain why the winner was selected; list risks and supporting factors; and return structured JSON.\n\n"
            "If none of the five markets has a sufficiently strong setup, return NO_TRADE rather than forcing a winner.\n\n"
            + payload
        )

    def _fallback_analysis(self, snapshots: list[MarketSnapshot]) -> AutoTradeAnalysis:
        market_analyses: list[MarketAnalysis] = []
        for snapshot in snapshots:
            indicators = snapshot.indicators or {}
            rsi = float(indicators.get("rsi", 50.0))
            macd = float(indicators.get("macd", 0.0))
            macd_signal = float(indicators.get("macd_signal", 0.0))
            ma50 = float(indicators.get("ma50", snapshot.current_price))
            ma200 = float(indicators.get("ma200", snapshot.current_price))

            if snapshot.data_quality != "good":
                market_analyses.append(
                    MarketAnalysis(
                        symbol=snapshot.symbol,
                        direction="NO_TRADE",
                        confidence=0.0,
                        setup_quality=0.0,
                        reasoning="Insufficient market data quality to support an analysis.",
                        supporting_factors=[],
                        risks=["Insufficient data quality."],
                        data_quality="insufficient",
                    )
                )
                continue

            ma_alignment = 1 if snapshot.current_price >= ma50 >= ma200 else -1 if snapshot.current_price <= ma50 <= ma200 else 0
            momentum_signal = 1 if macd >= macd_signal else -1 if macd < macd_signal else 0
            rsi_bias = 1 if rsi >= 55 else -1 if rsi <= 45 else 0
            combined_score = ma_alignment + momentum_signal + rsi_bias + (1 if snapshot.momentum >= 0 else -1)
            direction = "BULLISH" if combined_score >= 1 else "BEARISH" if combined_score <= -1 else "NO_TRADE"
            base_confidence = 0.45 + min(0.35, max(0.0, abs(snapshot.momentum) / 100.0)) + max(0.0, (abs(rsi - 50) / 100.0))
            setup_quality = 0.45 + max(0.0, (abs(macd - macd_signal) / max(abs(snapshot.current_price) * 0.01, 0.01)))
            if ma_alignment == 0:
                setup_quality -= 0.1
            market_analyses.append(
                MarketAnalysis(
                    symbol=snapshot.symbol,
                    direction=direction,
                    confidence=max(0.0, min(1.0, round(base_confidence, 2))),
                    setup_quality=max(0.0, min(1.0, round(setup_quality, 2))),
                    reasoning=f"The {snapshot.display_name} shows {snapshot.trend} pressure with MA50/MA200 alignment, RSI {rsi:.1f}, and MACD confirmation.",
                    supporting_factors=[
                        f"Trend is {snapshot.trend}",
                        f"RSI is {rsi:.1f}",
                        f"MACD is {macd:.4f} with signal {macd_signal:.4f}",
                        f"MA50/MA200 are {ma50:.4f}/{ma200:.4f}",
                        f"Recent price range is {snapshot.recent_low} to {snapshot.recent_high}",
                    ],
                    risks=["Volatility and short-term reversal risk persist."],
                    data_quality="good",
                )
            )

        winner = max(market_analyses, key=lambda item: (item.confidence, item.setup_quality), default=None)
        if winner is None:
            raise ValueError("No market snapshots were provided for analysis.")

        if winner.confidence < settings.min_ai_confidence or winner.setup_quality < settings.min_setup_quality:
            winner = MarketAnalysis(
                symbol=winner.symbol,
                direction="NO_TRADE",
                confidence=winner.confidence,
                setup_quality=winner.setup_quality,
                reasoning="The strongest available setup does not meet the minimum quality threshold.",
                supporting_factors=[],
                risks=["Minimum confidence threshold not met."],
                data_quality="good",
            )

        return AutoTradeAnalysis(
            winner_symbol=winner.symbol,
            winner_direction=winner.direction,
            winner_confidence=winner.confidence,
            winner_setup_quality=winner.setup_quality,
            market_analyses=market_analyses,
            overall_reasoning=(
                f"The strongest active setup is {winner.symbol} with {winner.direction.lower()} bias and a confidence score of {winner.confidence:.2f}."
                if winner.direction != "NO_TRADE"
                else "No market currently meets the minimum setup requirements for Auto Trade."
            ),
        )

    def analyze(self, snapshots: list[MarketSnapshot]) -> AutoTradeAnalysis:
        if not snapshots:
            raise ValueError("No market snapshots available for Gemini analysis.")

        if not self.api_key:
            logger.warning("GEMINI_API_KEY is missing; falling back to deterministic local analysis.")
            return self._fallback_analysis(snapshots)

        if genai is None:
            logger.warning("google-genai package not installed; using deterministic fallback analysis.")
            return self._fallback_analysis(snapshots)

        try:
            client = genai.Client(api_key=self.api_key)
            prompt = self._build_prompt(snapshots)
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": AutoTradeAnalysis,
                },
            )

            if hasattr(response, "text") and response.text:
                parsed = json.loads(response.text)
                return AutoTradeAnalysis.model_validate(parsed)

            if hasattr(response, "parsed") and response.parsed is not None:
                return AutoTradeAnalysis.model_validate(response.parsed)

            if hasattr(response, "model_dump"):
                return AutoTradeAnalysis.model_validate(response.model_dump())

            return self._fallback_analysis(snapshots)
        except Exception as exc:  # pragma: no cover - depends on external API runtime
            logger.exception("Gemini analysis failed: %s", exc)
            return self._fallback_analysis(snapshots)
