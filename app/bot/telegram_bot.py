from __future__ import annotations

from app.config import settings
from app.bot.handlers import handle_connect, handle_logout, handle_scan, handle_start, handle_status

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:  # pragma: no cover - optional dependency in local dev sandbox
    Update = object
    ContextTypes = object
    Application = None
    CommandHandler = None


def create_application():
    """Create the Telegram bot application if the library and token are available."""
    if Application is None:
        return None
    if not settings.telegram_bot_token:
        return None

    application = Application.builder().token(settings.telegram_bot_token).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(handle_start())

    async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(handle_connect(user_id))

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(handle_status(False))

    async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(handle_logout(user_id))

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(handle_scan())

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("connect", connect))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("scan", scan))
    return application
