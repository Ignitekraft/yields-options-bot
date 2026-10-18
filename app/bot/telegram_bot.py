from __future__ import annotations

import asyncio
import logging

from app.bot.callbacks import (
    _auto_trade_locked_seconds,
    dispatch_callback,
    handle_admin_mark_failed_command,
    handle_admin_mark_paid_command,
    handle_admin_verify_purchase_command,
    handle_admin_withdrawals_command,
    handle_custom_stake_input,
    handle_payout_details_command,
    handle_referrals_command,
    handle_withdraw_command,
)
from app.bot.handlers import (
    account_mode_service,
    auth_service,
    build_connect_prompt,
    build_menu_message,
    handle_balance,
    handle_faq,
    handle_logout,
    handle_scan,
    handle_stats,
    handle_status,
    build_token_store_message,
    TOKEN_STORE_IMAGE_URL,
    trade_history_service,
)
from app.bot.keyboards import TelegramKeyboard
from app.config import settings

try:
    from telegram import Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        TypeHandler,
        filters,
    )
except ImportError:  # pragma: no cover - optional dependency in local dev sandbox
    Update = object
    ContextTypes = object
    Application = None
    CallbackQueryHandler = None
    CommandHandler = None
    MessageHandler = None
    TypeHandler = None
    filters = None

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
        TelegramKeyboard.menu_keyboard(
            settings.community_url,
            locked_seconds=await _auto_trade_locked_seconds(user.id),
        )
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

    try:
        profile = await asyncio.to_thread(
            trade_history_service.get_user,
            user.id,
        )
        if not profile.get("welcome_gift_200_claimed", False):
            await update.message.reply_text(
                "🎁 FIRST-TIME GIFT\n\n"
                "You have a one-time gift of 200 Yields Tokens to use for "
                "trading. Accept it now and it will be added to your token balance.",
                reply_markup=TelegramKeyboard.welcome_gift_keyboard(),
            )
    except Exception:
        logger.exception("Could not check welcome gift eligibility")


async def _send_stats_message(update: Update) -> None:
    text = await handle_stats(update.effective_user.id)
    await update.message.reply_text(text)


async def _send_tokens_message(update: Update) -> None:
    text = build_token_store_message()
    keyboard = TelegramKeyboard.token_store_keyboard()
    try:
        await update.message.reply_photo(
            photo=TOKEN_STORE_IMAGE_URL,
            caption=text,
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Could not send the token-store image")
        await update.message.reply_text(text, reply_markup=keyboard)


async def _handle_unexpected_error(update, context) -> None:
    error = getattr(context, "error", None)
    if isinstance(error, BaseException):
        logger.error(
            "Unhandled Telegram update failure",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Unhandled Telegram update failure without exception details")

    query = getattr(update, "callback_query", None)
    answer = getattr(query, "answer", None)
    if answer is not None:
        try:
            data = str(getattr(query, "data", "") or "")
            message = (
                "Auto Trade couldn't finish that step. Check your Deriv "
                "account before trying again."
                if data == "START_AUTO_TRADE" or data.startswith("AUTO_TRADE")
                else "That button couldn't be completed. Please try again."
            )
            await answer(message, show_alert=True)
            return
        except Exception:
            logger.exception("Could not answer failed Telegram callback")
        return

    message = getattr(update, "effective_message", None)
    reply_text = getattr(message, "reply_text", None)
    if reply_text is not None:
        try:
            await reply_text(
                "⚠️ That message couldn't be processed. Please try again shortly."
            )
        except Exception:
            logger.exception("Could not send Telegram error notification")


def create_application():
    """Create the Telegram bot application if the library and token are available."""
    if Application is None:
        return None
    if not settings.telegram_bot_token:
        return None

    application = Application.builder().token(settings.telegram_bot_token).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = list(getattr(context, "args", []) or [])
        if args and args[0].startswith("ref_"):
            referrer_id = args[0].removeprefix("ref_")
            if referrer_id.isdigit():
                try:
                    await asyncio.to_thread(
                        trade_history_service.ensure_user,
                        referrer_id,
                        account_mode="demo",
                    )
                    await asyncio.to_thread(
                        trade_history_service.attach_referral,
                        update.effective_user.id,
                        referrer_id,
                    )
                except Exception:
                    logger.exception("Could not attach new user's referral")
        await _send_menu_message(update)

    async def sync_telegram_profile(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        user = getattr(update, "effective_user", None)
        if user is None:
            return
        try:
            await asyncio.to_thread(
                trade_history_service.ensure_user,
                user.id,
                first_name=getattr(user, "first_name", None),
                last_name=getattr(user, "last_name", None),
                username=getattr(user, "username", None),
                account_mode=account_mode_service.get_mode(str(user.id)),
                sync_telegram_profile=True,
            )
        except Exception:
            logger.exception(
                "Could not sync Telegram profile for user %s",
                user.id,
            )

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
        user_id = update.effective_user.id
        await update.message.reply_text(
            handle_faq(),
            reply_markup=TelegramKeyboard.dashboard_keyboard(
                locked_seconds=await _auto_trade_locked_seconds(user_id),
            ),
        )

    async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _send_stats_message(update)

    async def tokens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _send_tokens_message(update)

    async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(handle_logout(user_id), reply_markup=TelegramKeyboard.start_keyboard())

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
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
            reply_markup=TelegramKeyboard.dashboard_keyboard(
                locked_seconds=await _auto_trade_locked_seconds(user_id),
            ),
        )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("connect", connect))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("faq", faq))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("tokens", tokens))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("scan", scan))
    application.add_handler(CommandHandler("referrals", handle_referrals_command))
    application.add_handler(CommandHandler("payout", handle_withdraw_command))
    application.add_handler(
        CommandHandler("payout_details", handle_payout_details_command)
    )
    application.add_handler(
        CommandHandler("admin_verify_purchase", handle_admin_verify_purchase_command)
    )
    application.add_handler(
        CommandHandler("admin_withdrawals", handle_admin_withdrawals_command)
    )
    application.add_handler(
        CommandHandler("admin_mark_paid", handle_admin_mark_paid_command)
    )
    application.add_handler(
        CommandHandler("admin_mark_failed", handle_admin_mark_failed_command)
    )
    if TypeHandler is not None:
        application.add_handler(
            TypeHandler(Update, sync_telegram_profile),
            group=-1,
        )
    if CallbackQueryHandler is not None:
        application.add_handler(CallbackQueryHandler(dispatch_callback))
    if MessageHandler is not None and filters is not None:
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handle_custom_stake_input,
            )
        )
    application.add_error_handler(_handle_unexpected_error)
    return application
