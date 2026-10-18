from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import settings


@dataclass
class TradeRunState:
    user_id: str
    symbol: str
    direction: str
    base_stake: float
    current_stake: float
    cycle: int = 1
    martingale_step: int = 1
    max_cycles: int = settings.auto_trade_cycles
    status: str = "pending"
    expiry_duration: int = settings.default_expiry_duration
    expiry_unit: str = settings.default_expiry_unit
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None
    total_staked: float = 0.0
    total_profit_loss: float = 0.0
    trade_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "base_stake": self.base_stake,
            "current_stake": self.current_stake,
            "cycle": self.cycle,
            "martingale_step": self.martingale_step,
            "max_cycles": self.max_cycles,
            "status": self.status,
            "expiry_duration": self.expiry_duration,
            "expiry_unit": self.expiry_unit,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_staked": self.total_staked,
            "total_profit_loss": self.total_profit_loss,
            "trade_count": self.trade_count,
        }
