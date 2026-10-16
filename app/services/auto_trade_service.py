from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import settings

AUTO_TRADE_STALE_SECONDS = int(getattr(settings, "auto_trade_stale_seconds", 1800))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TradeRunState:
    user_id: str
    symbol: str
    direction: str
    base_stake: float
    current_stake: float
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stake_percentage_key: str | None = None
    cycle: int = 1
    martingale_step: int = 1
    max_cycles: int = settings.auto_trade_cycles
    status: str = "pending"
    expiry_seconds: int = settings.default_expiry_seconds
    account_mode: str = "demo"
    currency: str = "USD"
    started_at: str = field(default_factory=_now)
    completed_at: str | None = None
    total_staked: float = 0.0
    total_profit_loss: float = 0.0
    tokens_spent: int = 0
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    proposal_id: str | None = None
    contract_id: str | None = None
    market_symbol: str | None = None
    contract_type: str | None = None
    last_result: str | None = None
    last_profit: float = 0.0


class AutoTradeService:
    """Thread-safe in-memory Auto Trade state registry."""

    ACTIVE_STATUSES = {
        "pending",
        "running",
        "executing",
        "awaiting_stake",
        "awaiting_confirmation",
        "monitoring",
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, TradeRunState] = {}
        self._executing: set[str] = set()

    @staticmethod
    def _parse_started_at(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _prune_stale_runs_locked(self) -> None:
        stale_cutoff = datetime.now(timezone.utc).timestamp() - AUTO_TRADE_STALE_SECONDS
        for key, state in list(self._runs.items()):
            if state.status not in self.ACTIVE_STATUSES:
                continue
            started_at = self._parse_started_at(state.started_at)
            if started_at is None:
                continue
            if started_at.timestamp() < stale_cutoff:
                self._runs.pop(key, None)
                self._executing.discard(key)

    def is_running(self, user_id: str | int) -> bool:
        with self._lock:
            self._prune_stale_runs_locked()
            state = self._runs.get(str(user_id))
            return bool(state and state.status in self.ACTIVE_STATUSES)

    def start_run(
        self,
        user_id: str | int,
        symbol: str,
        direction: str,
        base_stake: float,
        max_cycles: int = settings.auto_trade_cycles,
        account_mode: str = "demo",
    ) -> TradeRunState:
        key = str(user_id)
        mode = str(account_mode or "").strip().lower()

        if mode not in {"demo", "real"}:
            raise ValueError("INVALID_ACCOUNT_MODE")
        if not symbol or str(symbol).upper() == "NO_SETUP":
            raise ValueError("NO_VALID_MARKET_SETUP")
        if direction not in {"BULLISH", "BEARISH", "RISE", "FALL", "CALL", "PUT"}:
            raise ValueError("NO_VALID_TRADE_DIRECTION")

        try:
            stake = float(base_stake)
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_STAKE") from exc

        if stake <= 0 or stake == float("inf") or stake != stake:
            raise ValueError("INVALID_STAKE")
        if int(max_cycles) < 1:
            raise ValueError("INVALID_CYCLE_TARGET")

        with self._lock:
            self._prune_stale_runs_locked()
            existing = self._runs.get(key)
            if existing and existing.status in self.ACTIVE_STATUSES:
                raise RuntimeError("AUTO_TRADE_ALREADY_RUNNING")

            state = TradeRunState(
                user_id=key,
                symbol=str(symbol),
                direction=str(direction).upper(),
                base_stake=round(stake, 2),
                current_stake=round(stake, 2),
                max_cycles=int(max_cycles),
                status="awaiting_confirmation",
                account_mode=mode,
            )
            self._runs[key] = state
            return state

    def get_run(self, user_id: str | int) -> TradeRunState | None:
        with self._lock:
            return self._runs.get(str(user_id))

    def confirm_run(self, user_id: str | int) -> TradeRunState:
        with self._lock:
            state = self._runs.get(str(user_id))
            if state is None:
                raise RuntimeError("AUTO_TRADE_NOT_FOUND")
            if state.status not in {"pending", "awaiting_confirmation"}:
                raise RuntimeError("AUTO_TRADE_NOT_CONFIRMABLE")
            state.status = "running"
            return state

    def cancel_run(self, user_id: str | int) -> TradeRunState | None:
        key = str(user_id)
        with self._lock:
            state = self._runs.get(key)
            if state is None:
                return None
            if state.status not in self.ACTIVE_STATUSES:
                return state
            state.status = "cancelled"
            state.completed_at = _now()
            self._executing.discard(key)
            return state

    def finish_run(
        self,
        user_id: str | int,
        status: str = "completed",
    ) -> TradeRunState | None:
        key = str(user_id)
        with self._lock:
            state = self._runs.get(key)
            if state is None:
                return None
            state.status = status
            state.completed_at = _now()
            self._executing.discard(key)
            return state

    def record_trade_stake(
        self,
        user_id: str | int,
        stake: float,
    ) -> TradeRunState:
        with self._lock:
            state = self._runs.get(str(user_id))
            if state is None:
                raise RuntimeError("AUTO_TRADE_NOT_FOUND")
            state.total_staked += round(float(stake), 2)
            return state

    def advance_after_result(
        self,
        user_id: str | int,
        won: bool,
        profit: float | None = None,
    ) -> TradeRunState:
        key = str(user_id)

        with self._lock:
            state = self._runs.get(key)
            if state is None:
                raise RuntimeError("AUTO_TRADE_NOT_FOUND")

            settled_profit = float(profit or 0.0)
            state.total_profit_loss += settled_profit
            state.trade_count += 1
            state.last_profit = settled_profit
            state.last_result = "won" if won else "lost"

            if won:
                state.wins += 1
                state.martingale_step = 1
                state.current_stake = state.base_stake

                if state.cycle >= state.max_cycles:
                    state.status = "completed"
                    state.completed_at = _now()
                    self._executing.discard(key)
                    return state

                state.cycle += 1
                state.status = "running"
                self._executing.discard(key)
                return state

            state.losses += 1
            state.martingale_step += 1

            multiplier = float(getattr(settings, "martingale_multiplier", 2.25))
            state.current_stake = round(
                state.base_stake * (multiplier ** (state.martingale_step - 1)),
                2,
            )
            state.status = "running"
            self._executing.discard(key)
            return state

    def mark_executing(self, user_id: str | int) -> bool:
        key = str(user_id)
        with self._lock:
            state = self._runs.get(key)
            if state is None or state.status not in {"running", "monitoring"}:
                return False
            if key in self._executing:
                return False
            self._executing.add(key)
            return True

    def clear_execution_lock(self, user_id: str | int) -> None:
        with self._lock:
            self._executing.discard(str(user_id))

    def log_run(self, user_id: str | int, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            state = self._runs.get(str(user_id))
            if state is None:
                return {"status": "not_found"}
            for key, value in kwargs.items():
                if hasattr(state, key):
                    setattr(state, key, value)
            return {
                "status": state.status,
                "cycle": state.cycle,
                "wins": state.wins,
                "losses": state.losses,
            }

    def clear_run(self, user_id: str | int) -> None:
        with self._lock:
            key = str(user_id)
            self._runs.pop(key, None)
            self._executing.discard(key)