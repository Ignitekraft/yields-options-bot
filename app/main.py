from __future__ import annotations

import threading

from fastapi import FastAPI, Query

from app.bot.telegram_bot import create_application
from app.config import settings
from app.services.auth_service import AuthService

app = FastAPI(title="Yields", version="0.1.0")
auth_service = AuthService()
bot_application = None


def get_bot_application():
    global bot_application
    if bot_application is None and settings.telegram_bot_token:
        bot_application = create_application()
    return bot_application


@app.on_event("startup")
async def startup_event() -> None:
    """Ensure the Telegram bot is booted when the FastAPI app starts in production."""
    application = get_bot_application()
    if application is None:
        return

    await application.initialize()
    thread = threading.Thread(
        target=application.run_polling,
        kwargs={"poll_interval": 5, "timeout": 30},
        daemon=True,
    )
    thread.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    application = get_bot_application()
    if application is not None:
        await application.shutdown()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/deriv/callback")
def deriv_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> dict[str, str]:
    """Validate the Deriv OAuth return, reject mismatches, and acknowledge a successful connection."""
    if error:
        return {"status": "error", "error": error}

    if not code or not state:
        return {"status": "error", "error": "missing_code_or_state"}

    telegram_user_id = auth_service.get_pending_user_for_state(state)
    if telegram_user_id is None or not auth_service.validate_state(telegram_user_id, state):
        return {"status": "error", "error": "state_mismatch"}

    payload = {
        "access_token": "demo-access-token",
        "refresh_token": "demo-refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "connected_at": "2026-10-13T00:00:00Z",
    }
    auth_service.complete_oauth_session(telegram_user_id, payload)
    return {"status": "success", "message": "Deriv connected", "telegram_user_id": telegram_user_id}


@app.get("/")
def root() -> dict[str, str]:
    return {"app": "Yields", "mode": "telegram-first"}
