from __future__ import annotations

from app.config import settings


class SettingsService:
    """Minimal settings layer for user-facing configuration display."""

    def get_settings_summary(self) -> dict[str, object]:
        return {
            "martingale_enabled": True,
            "multiplier": settings.martingale_multiplier,
            "max_cycles": settings.auto_trade_cycles,
            "expiry_duration": settings.default_expiry_duration,
            "expiry_unit": settings.default_expiry_unit,
        }
