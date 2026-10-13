from __future__ import annotations

from app.config import settings


class SettingsService:
    """Minimal settings layer for user-facing configuration display."""

    def get_settings_summary(self) -> dict[str, object]:
        return {
            "martingale_enabled": True,
            "multiplier": settings.martingale_multiplier,
            "max_martingale_steps": settings.max_martingale_steps,
            "max_cycles": settings.auto_trade_cycles,
            "expiry_seconds": settings.default_expiry_seconds,
        }
