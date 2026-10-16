from __future__ import annotations

import asyncio
import logging

from app.bot.callbacks import dispatch_callback
from app.bot.handlers import (
    auth_service,
    build_connect_prompt,
    build_menu_message,
    handle_balance,
    handle_faq,
    handle_logout,
    handle_scan,
    handle_stats,
    handle_status,
    trade_history_service,
)
from app.bot.keyboards import TelegramKeyboard
from app.config import settings

try:
    from telegram import Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
except ImportError:  # pragma: no cover - optional dependency in local dev sandbox
    Update = object
    ContextTypes = object
    Application = None
    CallbackQueryHandler = None
    CommandHandler = None

logger = logging.getLogger(__name__)


async def _send_menu_message(update: Update) -> None:
    user = update.effective_user
    text = await build_menu_message(
        user.id,
        display_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        username=getattr(user, "username", None),
    )
    keyboard = (
        TelegramKeyboard.menu_keyboard(settings.community_url)
        if auth_service.get_valid_access_token(str(user.id))
        else TelegramKeyboard.start_keyboard()
    )
    try:
        await update.message.reply_photo(
            photo=settings.menu_image_url,
            caption=text,
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Could not send the Yields start-menu image")
        await update.message.reply_text(text, reply_markup=keyboard)


async def _send_stats_message(update: Update) -> None:
    text = await handle_stats(update.effective_user.id)
    await update.message.reply_text(text)


def create_application():
    """Create the Telegram bot application if the library and token are available."""
    if Application is None:
        return None
    if not settings.telegram_bot_token:
        return None

    application = Application.builder().token(settings.telegram_bot_token).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _send_menu_message(update)

    async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        message, connect_url = build_connect_prompt(user_id)
        await update.message.reply_text(
            message,
            reply_markup=TelegramKeyboard.connect_keyboard(connect_url),
        )

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(handle_status(user_id), reply_markup=TelegramKeyboard.status_keyboard())

    async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(await handle_balance(user_id), reply_markup=TelegramKeyboard.status_keyboard())

    async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(handle_faq(), reply_markup=TelegramKeyboard.dashboard_keyboard())

    async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _send_stats_message(update)

    async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(handle_logout(user_id), reply_markup=TelegramKeyboard.start_keyboard())

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            result = await asyncio.to_thread(handle_scan)
        except Exception:
            logger.exception("Could not retrieve live Deriv data for /scan")
            result = (
                "⚠️ Live Deriv market data is temporarily unavailable. "
                "No trade signal was generated. Please try again shortly."
            )
        await update.message.reply_text(
            result,
            reply_markup=TelegramKeyboard.dashboard_keyboard(),
        )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("connect", connect))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("faq", faq))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("scan", scan))
    if CallbackQueryHandler is not None:
        application.add_handler(CallbackQueryHandler(dispatch_callback))
    return application
