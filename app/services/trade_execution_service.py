from __future__ import annotations

from app.config import settings


class TradeExecutionService:
    """Deterministic trade execution helpers and safety validation."""

    @staticmethod
    def calculate_next_stake(base_stake: float, martingale_step: int, multiplier: float | None = None) -> float:
        multiplier = float(multiplier if multiplier is not None else settings.martingale_multiplier)
        step_index = max(0, int(martingale_step) - 1)
        next_stake = float(base_stake) * (multiplier ** step_index)
        return round(next_stake, 2)

    @staticmethod
    def validate_stake(stake: float, account_balance: float | None = None) -> tuple[bool, str | None]:
        if stake is None or not isinstance(stake, (int, float)):
            return False, "invalid_stake"
        amount = float(stake)
        if amount <= 0:
            return False, "invalid_stake"
        if amount > 1000000:
            return False, "stake_too_large"
        if account_balance is not None and amount > float(account_balance):
            return False, "insufficient_balance"
        return True, None

    @staticmethod
    def resolve_contract_direction(symbol: str, direction: str) -> str:
        normalized = str(direction).upper()
        if normalized in {"BULLISH", "CALL"}:
            return "CALL"
        if normalized in {"BEARISH", "PUT"}:
            return "PUT"
        return normalized

    @staticmethod
    def build_proposal_payload(symbol: str, direction: str, stake: float, expiry_seconds: int = settings.default_expiry_seconds) -> dict[str, object]:
        contract_type = TradeExecutionService.resolve_contract_direction(symbol, direction)
        return {
            "symbol": symbol,
            "contract_type": contract_type,
            "amount": float(stake),
            "currency": "USD",
            "duration": int(expiry_seconds),
            "duration_unit": "s",
            "multiplier": 1,
        }
