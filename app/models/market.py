from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketSnapshot:
    symbol: str
    display_name: str
    current_price: float
    prices: list[float] = field(default_factory=list)
    timestamps: list[str] = field(default_factory=list)
    trend: str = "neutral"
    momentum: float = 0.0
    volatility: float = 0.0
    recent_high: float = 0.0
    recent_low: float = 0.0
    indicators: dict[str, Any] = field(default_factory=dict)
    data_quality: str = "good"

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "display_name": self.display_name,
            "current_price": self.current_price,
            "prices": self.prices,
            "timestamps": self.timestamps,
            "trend": self.trend,
            "momentum": self.momentum,
            "volatility": self.volatility,
            "recent_high": self.recent_high,
            "recent_low": self.recent_low,
            "indicators": self.indicators,
            "data_quality": self.data_quality,
        }
