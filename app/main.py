from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.telegram_bot import create_application
from app.config import settings
from app.services.auth_service import AuthService
from app.services.trade_history_service import TradeHistoryService

app = FastAPI(title="Yields", version="0.1.0")

auth_service = AuthService()
trade_history_service = TradeHistoryService()
logger = logging.getLogger(__name__)
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

    profile = None
    try:
        profile = await asyncio.to_thread(
            trade_history_service.ensure_user,
            telegram_user_id,
        )
    except Exception:
        logger.exception(
            "Could not sync OAuth Telegram profile for user %s",
            telegram_user_id,
        )

    application = get_bot_application()
    if application is not None:
        try:
            try:
                chat_id = int(telegram_user_id)
            except (TypeError, ValueError):
                chat_id = telegram_user_id

            main_menu_button = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="START_DASHBOARD")],
                    *(
                        [[InlineKeyboardButton(
                            "🎁 Accept 200 Tokens",
                            callback_data="WELCOME_GIFT_ACCEPT_200",
                        )]]
                        if not (profile or {}).get("welcome_gift_200_claimed", False)
                        else []
                    ),
                ]
            )

            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Deriv account connected successfully!\n\n"
                    "Your account is active and ready.\n\n"
                    + (
                        "🎁 You have a one-time gift of 200 Yields Tokens "
                        "available to accept.\n\n"
                        if not (profile or {}).get("welcome_gift_200_claimed", False)
                        else ""
                    )
                    + "Tap below to return to the main menu."
                ),
                reply_markup=main_menu_button,
            )
        except Exception as e:
            logger.exception("Failed to send Telegram OAuth success notification")

    if profile is None:
        return """
        <html>
            <body style="font-family: Arial, sans-serif; text-align: center; padding-top: 50px; background: #0f1a24; color: #f5f7fa;">
                <h2>Deriv Account Connected</h2>
                <p>Your account is active, but your Telegram profile could not be synced.</p>
                <p>Please return to Telegram and try /start again.</p>
            </body>
        </html>
        """

    return """
    <html>
        <body style="font-family: Arial, sans-serif; text-align: center; padding-top: 50px; background: #0f1a24; color: #f5f7fa;">
            <div style="max-width: 560px; margin: 0 auto; padding: 32px 20px;">
                <h2 style="color: #3ddc97; margin-bottom: 12px;">Deriv Account Connected Successfully!</h2>
                <p style="font-size: 16px; line-height: 1.6; margin-bottom: 18px;">You can close this window and return to Telegram.</p>
                <div style="display: inline-block; padding: 12px 20px; border-radius: 10px; background: #2f9e74; color: white; font-weight: 600;">Open Dashboard</div>
            </div>
        </body>
    </html>
    """


def verify_whop_webhook_signature(
    payload: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    now: float | None = None,
) -> bool:
    webhook_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signature_header = headers.get("webhook-signature")
    if not secret or not webhook_id or not timestamp or not signature_header:
        return False
    try:
        timestamp_seconds = int(timestamp)
    except (TypeError, ValueError):
        return False
    current_time = time.time() if now is None else now
    if abs(current_time - timestamp_seconds) > 300:
        return False

    signed_content = f"{webhook_id}.{timestamp}.".encode("utf-8") + payload
    expected = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            signed_content,
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    return any(
        hmac.compare_digest(item.removeprefix("v1,"), expected)
        for item in signature_header.split()
        if item.startswith("v1,")
    )


@app.post("/webhooks/whop")
async def whop_webhook(request: Request):
    if not settings.whop_webhook_secret:
        raise HTTPException(status_code=503, detail="WHOP_WEBHOOK_NOT_CONFIGURED")
    payload = await request.body()
    if not verify_whop_webhook_signature(
        payload,
        request.headers,
        settings.whop_webhook_secret,
    ):
        raise HTTPException(status_code=401, detail="INVALID_WEBHOOK_SIGNATURE")

    try:
        event = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="INVALID_WEBHOOK_JSON") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="INVALID_WEBHOOK_EVENT")
    if event.get("type") != "payment.succeeded":
        return JSONResponse({"received": True, "ignored": True})

    payment = event.get("data")
    if not isinstance(payment, dict):
        raise HTTPException(status_code=400, detail="INVALID_PAYMENT_EVENT")
    metadata = payment.get("metadata")
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=422, detail="PAYMENT_METADATA_REQUIRED")

    telegram_user_id = metadata.get(settings.whop_telegram_user_metadata_key)
    token_amount = metadata.get(settings.whop_token_amount_metadata_key)
    payment_id = payment.get("id")
    event_id = request.headers.get("webhook-id")
    amount_paid = payment.get("total")
    currency = str(payment.get("currency") or "").upper()
    try:
        if isinstance(token_amount, bool):
            raise ValueError
        token_value = Decimal(str(token_amount))
        amount = Decimal(str(amount_paid))
        if (
            not token_value.is_finite()
            or token_value != token_value.to_integral_value()
        ):
            raise ValueError
        tokens = int(token_value)
        if (
            not str(telegram_user_id).isdigit()
            or int(telegram_user_id) <= 0
            or payment_id is None
            or isinstance(payment_id, bool)
            or not str(payment_id).strip()
            or not str(event_id).strip()
        ):
            raise ValueError
        if (
            tokens <= 0
            or not amount.is_finite()
            or amount <= 0
            or currency != "USD"
        ):
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=(
                "PAYMENT_METADATA_INVALID: expected Telegram user ID, positive "
                "token_amount, USD total, payment ID, and webhook-id"
            ),
        )

    try:
        purchase = await asyncio.to_thread(
            trade_history_service.record_verified_token_purchase,
            provider="whop",
            provider_purchase_id=str(payment_id),
            provider_event_id=str(event_id),
            telegram_user_id=str(telegram_user_id),
            tokens=tokens,
            amount_paid=str(amount),
            currency=currency,
        )
    except Exception as exc:
        logger.exception("Could not process verified Whop payment")
        raise HTTPException(status_code=503, detail="PURCHASE_CREDIT_FAILED") from exc

    if purchase.get("credited"):
        application = get_bot_application()
        if application is not None:
            try:
                await application.bot.send_message(
                    chat_id=int(telegram_user_id),
                    text=f"✅ Your Whop payment was verified. {tokens} Yields "
                    "Tokens have been added to your balance.",
                )
            except Exception:
                logger.exception("Could not notify buyer of Whop token credit")
            referrer_id = purchase.get("referrer_telegram_user_id")
            if referrer_id and float(purchase.get("commission") or 0) > 0:
                try:
                    await application.bot.send_message(
                        chat_id=int(referrer_id),
                        text=(
                            "🎉 Your referral made a token purchase. "
                            f"${float(purchase['commission']):.2f} USD (5%) "
                            "has been added to your referral earnings."
                        ),
                    )
                except Exception:
                    logger.exception("Could not notify referrer of commission")

    return JSONResponse(
        {
            "received": True,
            "credited": bool(purchase.get("credited")),
            "duplicate": bool(purchase.get("duplicate")),
        }
    )


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