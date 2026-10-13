from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_username: str = os.getenv("TELEGRAM_BOT_USERNAME", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    deriv_client_id: str = os.getenv("DERIV_CLIENT_ID", os.getenv("DERIV_APP_ID", ""))
    deriv_client_secret: str = os.getenv("DERIV_CLIENT_SECRET", "")
    deriv_redirect_uri: str = os.getenv("DERIV_REDIRECT_URI", "https://example.com/auth/deriv/callback")
    deriv_app_id: str = os.getenv("DERIV_APP_ID", os.getenv("DERIV_CLIENT_ID", ""))
    deriv_public_ws_url: str = os.getenv("DERIV_PUBLIC_WS_URL", "wss://ws.derivws.com/websockets/v3")
    environment: str = os.getenv("APP_ENV", "development")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    auto_trade_markets: tuple[str, ...] = (
        "step_100",
        "step_200",
        "step_300",
        "step_400",
        "step_500",
    )
    default_expiry_seconds: int = int(os.getenv("AUTO_TRADE_EXPIRY_SECONDS", "30"))
    auto_trade_cycles: int = int(os.getenv("AUTO_TRADE_CYCLES", "5"))
    auto_trade_multiplier: float = float(os.getenv("AUTO_TRADE_MULTIPLIER", "2.0"))
    martingale_multiplier: float = float(os.getenv("MARTINGALE_MULTIPLIER", "2.0"))
    max_martingale_steps: int = int(os.getenv("MAX_MARTINGALE_STEPS", "3"))
    min_ai_confidence: float = float(os.getenv("MIN_AI_CONFIDENCE", "0.70"))
    min_setup_quality: float = float(os.getenv("MIN_SETUP_QUALITY", "0.70"))


settings = Settings()
