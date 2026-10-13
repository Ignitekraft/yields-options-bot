from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MarketAnalysis(BaseModel):
    symbol: str
    direction: Literal["BULLISH", "BEARISH", "NO_TRADE"]
    confidence: float = Field(ge=0.0, le=1.0)
    setup_quality: float = Field(ge=0.0, le=1.0)
    reasoning: str
    supporting_factors: list[str]
    risks: list[str]
    data_quality: Literal["good", "insufficient"] = "good"


class AutoTradeAnalysis(BaseModel):
    winner_symbol: str
    winner_direction: Literal["BULLISH", "BEARISH", "NO_TRADE"]
    winner_confidence: float = Field(ge=0.0, le=1.0)
    winner_setup_quality: float = Field(ge=0.0, le=1.0)
    market_analyses: list[MarketAnalysis]
    overall_reasoning: str
