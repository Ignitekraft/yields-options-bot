from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_username: str = os.getenv("TELEGRAM_BOT_USERNAME", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    deriv_client_id: str = os.getenv("DERIV_CLIENT_ID", "")
    deriv_client_secret: str = os.getenv("DERIV_CLIENT_SECRET", "")
    deriv_redirect_uri: str = os.getenv("DERIV_REDIRECT_URI", "https://example.com/auth/deriv/callback")
    deriv_app_id: str = os.getenv("DERIV_APP_ID", "")
    deriv_public_ws_url: str = os.getenv("DERIV_PUBLIC_WS_URL", "wss://ws.binaryws.com/websockets/v3")
    environment: str = os.getenv("APP_ENV", "development")


settings = Settings()
