from __future__ import annotations

import os

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

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
    """Start FastAPI and the Telegram bot in the same asyncio event loop."""
    global bot_application

    application = get_bot_application()

    if application is None:
        return

    await application.initialize()

    if application.updater is None:
        raise RuntimeError("Telegram Application has no Updater configured.")

    await application.updater.start_polling(
        poll_interval=5,
        timeout=30,
    )

    await application.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Gracefully stop Telegram before FastAPI shuts down."""
    global bot_application

    application = bot_application

    if application is None:
        return

    if application.updater is not None:
        await application.updater.stop()

    await application.stop()
    await application.shutdown()

    bot_application = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/deriv/callback", response_class=HTMLResponse)
async def deriv_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """Callback from Deriv after OAuth login. Exchange the code for real tokens and complete the Telegram session."""

    if error:
        return f"<h3>OAuth Error: {error}</h3>"

    if not code or not state:
        return "<h3>Error: Missing code or state parameters.</h3>"

    telegram_user_id = auth_service.get_pending_user_for_state(state)

    if (
        telegram_user_id is None
        or not auth_service.validate_state(
            telegram_user_id,
            state,
        )
    ):
        return "<h3>Error: Invalid or expired OAuth state. Please run /connect again in Telegram.</h3>"

    client_id = settings.deriv_app_id or settings.deriv_client_id

    try:
        payload = auth_service.exchange_oauth_code(
            telegram_user_id,
            code,
            settings.deriv_redirect_uri,
            client_id,
            settings.deriv_client_secret,
        )
    except Exception as exc:
        return f"<h3>OAuth exchange failed: {exc}</h3>"

    application = get_bot_application()
    if application is not None:
        try:
            await application.bot.send_message(
                chat_id=int(telegram_user_id),
                text="✅ **Deriv account connected successfully!**\n\nYou can now use `/status`, `/scan`, and account tools.",
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Failed to send Telegram notification: {e}")

    return """
    <html>
        <body style="font-family: Arial, sans-serif; text-align: center; padding-top: 50px;">
            <h2 style="color: #2e7d32;">Deriv Account Connected Successfully!</h2>
            <p>You can close this window and return to Telegram.</p>
        </body>
    </html>
    """


@app.get("/")
def root() -> dict[str, str]:
    return {
        "app": "Yields",
        "mode": "telegram-first",
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)