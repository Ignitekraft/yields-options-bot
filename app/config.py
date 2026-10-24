from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_username: str = os.getenv("TELEGRAM_BOT_USERNAME", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    deriv_client_id: str = os.getenv("DERIV_CLIENT_ID", os.getenv("DERIV_APP_ID", ""))
    deriv_client_secret: str = os.getenv("DERIV_CLIENT_SECRET", "")
    deriv_redirect_uri: str = os.getenv("DERIV_REDIRECT_URI", "https://example.com/auth/deriv/callback")
    deriv_app_id: str = os.getenv("DERIV_APP_ID", os.getenv("DERIV_CLIENT_ID", ""))
    deriv_public_ws_url: str = os.getenv(
        "DERIV_PUBLIC_WS_URL",
        "wss://api.derivws.com/trading/v1/options/ws/public",
    )
    environment: str = os.getenv("APP_ENV", "development")
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_role_key: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    deriv_token_encryption_key: str = os.getenv("DERIV_TOKEN_ENCRYPTION_KEY", "")
    chart_analysis_provider: str = os.getenv(
        "CHART_ANALYSIS_PROVIDER",
        "gemini",
    )
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    whop_webhook_secret: str = os.getenv("WHOP_WEBHOOK_SECRET", "")
    whop_telegram_user_metadata_key: str = os.getenv(
        "WHOP_TELEGRAM_USER_METADATA_KEY",
        "telegram_user_id",
    )
    whop_token_amount_metadata_key: str = os.getenv(
        "WHOP_TOKEN_AMOUNT_METADATA_KEY",
        "token_amount",
    )
    menu_image_url: str = "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/Bot%20images/Yields%20Options%20Bot%20Neon%20Branding.png"
    community_url: str = "https://t.me/+N58js2a8NF1mMzY0"
    support_contact_url: str = "https://t.me/samueludoh_ng"
    bullish_setup_image_url: str = "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/Bot%20images/Bullish%20and%20Bearish%20Market%20Setups.jpg"
    bearish_setup_image_url: str = "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/Bot%20images/Bullish%20and%20Bearish%20Market%20Setups%202.jpg"
    auto_trade_markets: tuple[str, ...] = (
        "step_100",
        "step_200",
        "step_300",
        "step_400",
        "step_500",
    )
    default_expiry_duration: int = 3
    default_expiry_unit: str = "t"
    auto_trade_cycles: int = 3
    auto_trade_multiplier: float = 2.3
    martingale_multiplier: float = 2.3


settings = Settings()
