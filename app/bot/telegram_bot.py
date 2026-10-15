from __future__ import annotations

import asyncio
import logging

from app.bot.callbacks import dispatch_callback
from app.bot.handlers import (
    account_mode_service,
    auth_service,
    build_start_message,
    handle_balance,
    handle_connect,
    handle_faq,
    handle_logout,
    handle_scan,
    handle_start,
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


def create_application():
    """Create the Telegram bot application if the library and token are available."""
    if Application is None:
        return None
    if not settings.telegram_bot_token:
        return None

    application = Application.builder().token(settings.telegram_bot_token).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        display_name = getattr(update.effective_user, "first_name", None) or getattr(update.effective_user, "username", None) or "friend"
        try:
            await asyncio.to_thread(
                trade_history_service.ensure_user,
                user_id,
                first_name=getattr(update.effective_user, "first_name", None),
                last_name=getattr(update.effective_user, "last_name", None),
                username=getattr(update.effective_user, "username", None),
                account_mode=account_mode_service.get_mode(user_id),
            )
        except Exception:
            logger.exception("Could not register Telegram user in Supabase")
        connected = bool(auth_service.get_valid_access_token(str(user_id)))
        keypad = TelegramKeyboard.dashboard_keyboard() if connected else TelegramKeyboard.start_keyboard()
        text = await build_start_message(user_id, display_name=display_name) if connected else handle_start(user_id, display_name=display_name)
        await update.message.reply_text(text, reply_markup=keypad)

    async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(handle_connect(user_id), reply_markup=TelegramKeyboard.start_keyboard())

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(handle_status(user_id), reply_markup=TelegramKeyboard.status_keyboard())

    async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(await handle_balance(user_id), reply_markup=TelegramKeyboard.status_keyboard())

    async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(handle_faq(), reply_markup=TelegramKeyboard.dashboard_keyboard())

    async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(await handle_stats(user_id), reply_markup=TelegramKeyboard.dashboard_keyboard())

    async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(handle_logout(user_id), reply_markup=TelegramKeyboard.start_keyboard())

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(handle_scan(), reply_markup=TelegramKeyboard.dashboard_keyboard())

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
