from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
    expiry_seconds: int = settings.default_expiry_seconds
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None
    total_staked: float = 0.0
    total_profit_loss: float = 0.0
    trade_count: int = 0


class AutoTradeService:
    """Deterministic Auto Trade state registry and safety checks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, TradeRunState] = {}

    def is_running(self, user_id: str | int) -> bool:
        with self._lock:
            state = self._runs.get(str(user_id))
            return bool(state and state.status in {"pending", "running", "awaiting_stake", "awaiting_confirmation"})

    def start_run(
        self,
        user_id: str | int,
        symbol: str,
        direction: str,
        base_stake: float,
        max_cycles: int = settings.auto_trade_cycles,
    ) -> TradeRunState:
        key = str(user_id)
        with self._lock:
            if self.is_running(key):
                raise RuntimeError("AUTO_TRADE_ALREADY_RUNNING")
            state = TradeRunState(
                user_id=key,
                symbol=symbol,
                direction=direction,
                base_stake=float(base_stake),
                current_stake=float(base_stake),
                max_cycles=max_cycles,
                status="running",
            )
            self._runs[key] = state
            return state

    def get_run(self, user_id: str | int) -> TradeRunState | None:
        return self._runs.get(str(user_id))

    def confirm_run(self, user_id: str | int) -> TradeRunState:
        state = self.get_run(user_id)
        if state is None:
            raise RuntimeError("AUTO_TRADE_NOT_FOUND")
        state.status = "running"
        return state

    def cancel_run(self, user_id: str | int) -> TradeRunState | None:
        key = str(user_id)
        state = self._runs.get(key)
        if state is None:
            return None
        state.status = "cancelled"
        state.completed_at = datetime.now(timezone.utc).isoformat()
        return state

    def finish_run(self, user_id: str | int, status: str = "completed") -> TradeRunState | None:
        key = str(user_id)
        state = self._runs.get(key)
        if state is None:
            return None
        state.status = status
        state.completed_at = datetime.now(timezone.utc).isoformat()
        return state

    def advance_after_result(self, user_id: str | int, won: bool) -> TradeRunState:
        key = str(user_id)
        state = self._runs.get(key)
        if state is None:
            raise RuntimeError("AUTO_TRADE_NOT_FOUND")

        if won:
            state.total_profit_loss += max(0.0, state.current_stake)
            state.trade_count += 1
            state.martingale_step = 1
            state.current_stake = state.base_stake
            if state.cycle >= state.max_cycles:
                state.status = "completed"
                state.completed_at = datetime.now(timezone.utc).isoformat()
                return state
            state.cycle += 1
            return state

        state.martingale_step += 1
        state.current_stake = round(state.base_stake * (settings.martingale_multiplier ** max(0, state.martingale_step - 1)), 2)
        state.total_staked += state.current_stake
        state.trade_count += 1
        if state.martingale_step > settings.max_martingale_steps:
            state.status = "safety_limit_reached"
            state.completed_at = datetime.now(timezone.utc).isoformat()
        return state

    def log_run(self, user_id: str | int, **kwargs: Any) -> dict[str, Any]:
        state = self.get_run(user_id)
        if state is None:
            return {"status": "not_found"}
        for key, value in kwargs.items():
            setattr(state, key, value)
        return {"status": state.status, "cycle": state.cycle}

    def clear_run(self, user_id: str | int) -> None:
        self._runs.pop(str(user_id), None)
