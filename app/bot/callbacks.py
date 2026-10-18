from __future__ import annotations

import asyncio
import html
import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

try:
    from telegram import InputMediaPhoto
    from telegram.constants import ParseMode
except ImportError:  # pragma: no cover
    InputMediaPhoto = None
    ParseMode = None

from app.bot.handlers import (
    account_mode_service,
    account_service,
    auth_service,
    auto_trade_service,
    build_auto_trade_setup,
    build_connect_prompt,
    build_menu_message,
    build_start_message,
    handle_account_mode_settings,
    handle_auto_trade_start,
    handle_balance,
    handle_faq,
    handle_logout,
    handle_start,
    handle_stats,
    handle_status,
    TOKEN_PACKAGES,
    TOKEN_STORE_IMAGE_URL,
    build_token_purchase_message,
    build_token_store_message,
)
from app.bot.keyboards import TelegramKeyboard
from app.config import settings
from app.services.trade_execution_service import (
    TradeExecutionService,
    UnknownBuyOutcome,
)
from app.services.trade_history_service import TradeHistoryService

trade_execution_service = TradeExecutionService()
trade_history_service = TradeHistoryService()
logger = logging.getLogger(__name__)
LOW_TOKEN_WARNING_THRESHOLD = 50
TOKEN_EXHAUSTED_MESSAGE = (
    "💎 You’re out of Yields Tokens, so Auto Trade can’t start. "
    "No trade has been placed. Please tap on the \"💎 Get Tokens\" button "
    "or contact Sammie @samueludoh_ng to top up."
)
LOW_TOKEN_WARNING_IMAGE_URL = (
    "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
    "Bot%20images/Low%20Tokens%20Warning%20Alert.png"
)
VICTORY_SETUP_IMAGE_URLS = {
    "BULLISH": (
        "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
        "Bot%20images/Bullish%20and%20Bearish%20Market%20Setups.jpg"
    ),
    "BEARISH": (
        "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
        "Bot%20images/Bullish%20and%20Bearish%20Market%20Setups%202.jpg"
    ),
}
RELATIVE_STAKES = {
    "0_1": Decimal("0.001"),
    "0_5": Decimal("0.005"),
    "1": Decimal("0.01"),
}


def calculate_relative_stake(account_balance: object, percentage_key: str) -> float:
    percentage = RELATIVE_STAKES.get(percentage_key)
    if percentage is None:
        raise ValueError("UNSUPPORTED_STAKE_PERCENTAGE")
    try:
        balance = Decimal(str(account_balance))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("INVALID_ACCOUNT_BALANCE") from exc
    if not balance.is_finite() or balance <= 0:
        raise ValueError("INVALID_ACCOUNT_BALANCE")
    return float(
        (balance * percentage).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def parse_custom_stake(value: object) -> float:
    raw_value = str(value or "").strip()
    if len(raw_value) > 24 or not re.fullmatch(
        r"\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?",
        raw_value,
    ):
        raise ValueError("INVALID_CUSTOM_STAKE")
    try:
        amount = Decimal(raw_value.removeprefix("$").replace(",", ""))
        cents = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("INVALID_CUSTOM_STAKE") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("INVALID_CUSTOM_STAKE")
    if cents != amount:
        raise ValueError("CUSTOM_STAKE_MAX_TWO_DECIMAL_PLACES")
    return float(amount)


def _stake_selection_from_callback(
    callback_data: str,
) -> tuple[str | None, str | None, str | None]:
    suffix = callback_data.removeprefix("AUTO_TRADE_STAKE_")
    for percentage_key in RELATIVE_STAKES:
        if suffix == percentage_key:
            return percentage_key, None, None
        prefix = f"{percentage_key}_"
        if not suffix.startswith(prefix):
            continue
        selected_market_and_direction = suffix[len(prefix):]
        if "_" not in selected_market_and_direction:
            return None, None, None
        market_symbol, direction = selected_market_and_direction.rsplit("_", 1)
        if (
            market_symbol in settings.auto_trade_markets
            and direction in {"BULLISH", "BEARISH"}
        ):
            return percentage_key, market_symbol, direction
        return None, None, None
    return None, None, None


def _session_payload(state, session_status: str) -> dict:
    return {
        "session_id": state.session_id,
        "telegram_user_id": state.user_id,
        "session_status": session_status,
        "winning_cycles": state.wins,
        "trade_count": state.trade_count,
        "total_staked": state.total_staked,
        "net_profit": state.total_profit_loss,
    }


async def _save_session_summary(state, session_status: str | None = None) -> None:
    final_status = session_status or (
        "completed" if state.status == "completed" else "stopped"
    )
    try:
        await asyncio.to_thread(
            trade_history_service.update_session,
            _session_payload(state, final_status),
        )
    except Exception:
        logger.exception("Could not update Supabase Auto Trade session summary")


async def _session_end_message(
    user_id: str | int,
    state,
    reason: str | None = None,
) -> str:
    mode = str(state.account_mode or "demo").lower()
    balance_text = "Unavailable"
    currency = str(getattr(state, "currency", "USD") or "USD").upper()
    balances = None
    try:
        balances = await asyncio.wait_for(
            account_service.get_live_balances(user_id),
            timeout=15,
        )
        account_balance = (balances.get(mode) or {})
        if account_balance.get("available") and account_balance.get("balance") is not None:
            currency = str(account_balance.get("currency") or "USD").upper()
            balance_text = f"{float(account_balance['balance']):,.2f} {currency}"
    except Exception:
        logger.exception("Could not fetch final Deriv balance for session receipt")

    if balances is not None:
        try:
            await asyncio.to_thread(
                trade_history_service.update_balance_snapshots,
                user_id,
                balances,
            )
        except Exception:
            logger.exception("Could not save final Deriv balance snapshot")

    token_balance_text = "Unavailable"
    try:
        profile = await asyncio.to_thread(
            trade_history_service.get_user,
            user_id,
        )
        token_balance_text = str(int(profile.get("token_balance", 0)))
        if balance_text == "Unavailable":
            cached_balance = profile.get(f"{mode}_balance")
            if cached_balance is not None:
                currency = str(
                    profile.get(f"{mode}_currency") or currency
                ).upper()
                balance_text = (
                    f"Last known: {float(cached_balance):,.2f} {currency}"
                )
    except Exception:
        logger.exception("Could not fetch remaining tokens for session receipt")

    starting_balance = getattr(state, "starting_balance", None)
    if balance_text == "Unavailable" and starting_balance is not None:
        estimated_balance = starting_balance + float(
            state.total_profit_loss or 0.0
        )
        balance_text = f"Estimated: {estimated_balance:,.2f} {currency}"

    profit = float(state.total_profit_loss or 0.0)
    profit_text = f"{'+' if profit >= 0 else '-'}${abs(profit):,.2f} {currency}"
    market_label = friendly_market_name(state.market_symbol or state.symbol)

    if state.status == "completed":
        title = "🏆 Victory! Session completed\n✅"
        closing = "🔥 Momentum is on your side.\nKeep going? 💪"
    else:
        title = "🏁 Session ended"
        closing = reason or "Auto Trade stopped safely."

    return (
        f"{title}\n\n"
        f"📈 Market: {market_label}\n"
        f"🔴 Account: {mode.upper()}\n\n"
        f"🎯 Result: {profit_text}\n"
        f"💰 Balance: {balance_text}\n"
        f"💎 Tokens used: {state.tokens_spent}\n"
        f"💎 Tokens remaining: {token_balance_text}\n\n"
        f"{closing}"
    )


async def _safe_edit_message(query, text: str, **kwargs) -> None:
    try:
        await query.edit_message_text(text, **kwargs)
        return
    except Exception:
        pass

    if hasattr(query, "edit_message_caption"):
        try:
            await query.edit_message_caption(caption=text, **kwargs)
            return
        except Exception:
            pass

    try:
        await query.answer("The message updated, but Telegram could not refresh this view.")
    except Exception:
        pass


async def _send_session_end_message(
    query,
    user_id: str | int,
    state,
    reason: str | None = None,
) -> None:
    try:
        text = await _session_end_message(user_id, state, reason)
    except Exception:
        logger.exception("Could not build Auto Trade session-end receipt")
        text = (
            "🏁 Session ended\n\n"
            "Auto Trade has stopped safely. No further trades will be placed."
        )
    message = getattr(query, "message", None)
    reply_photo = getattr(message, "reply_photo", None)
    photo_url = VICTORY_SETUP_IMAGE_URLS.get(
        str(state.direction or "").upper()
    )
    if reply_photo is not None and photo_url is not None:
        try:
            await reply_photo(photo=photo_url, caption=text)
            return
        except Exception:
            logger.exception("Could not send Auto Trade session image")

    reply_text = getattr(message, "reply_text", None)
    if reply_text is not None:
        try:
            await reply_text(text)
            return
        except Exception:
            logger.exception("Could not send Auto Trade session-end message")
    logger.error("Could not send Auto Trade session-end message as a new reply")


async def _send_low_token_message(query, text: str, **kwargs) -> None:
    message = getattr(query, "message", None)
    reply_photo = getattr(message, "reply_photo", None)
    if reply_photo is not None:
        try:
            await reply_photo(
                photo=LOW_TOKEN_WARNING_IMAGE_URL,
                caption=text,
                **kwargs,
            )
            return
        except Exception:
            logger.exception("Could not send low-token warning image")

    reply_text = getattr(message, "reply_text", None)
    if reply_text is not None:
        try:
            await reply_text(text, **kwargs)
            return
        except Exception:
            logger.exception("Could not send low-token warning as a new message")
    logger.error("Could not send low-token warning as a new message")


async def _send_scan_message(query, text: str, **kwargs) -> None:
    message = getattr(query, "message", None)
    has_photo = bool(getattr(message, "photo", None))
    reply_text = getattr(message, "reply_text", None)

    if has_photo and reply_text is not None:
        try:
            await reply_text(text, **kwargs)
        except Exception:
            logger.exception("Could not send plain-text Auto Trade scan result")
            await _safe_edit_message(query, text, **kwargs)
            return

        try:
            await message.delete()
        except Exception:
            logger.exception(
                "Could not remove the previous Auto Trade scan photo"
            )
        return

    await _safe_edit_message(query, text, **kwargs)


async def _show_token_photo(query, caption: str, reply_markup) -> None:
    if InputMediaPhoto is not None and hasattr(query, "edit_message_media"):
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(
                    media=TOKEN_STORE_IMAGE_URL,
                    caption=caption,
                ),
                reply_markup=reply_markup,
            )
            return
        except Exception:
            logger.info(
                "Could not replace current message media with the token-store image",
                exc_info=True,
            )

    message = getattr(query, "message", None)
    reply_photo = getattr(message, "reply_photo", None)
    if reply_photo is not None:
        try:
            await reply_photo(
                photo=TOKEN_STORE_IMAGE_URL,
                caption=caption,
                reply_markup=reply_markup,
            )
            try:
                await message.delete()
            except Exception:
                logger.exception("Could not remove the previous token-store message")
            return
        except Exception:
            logger.exception("Could not send the token-store image")

    await _safe_edit_message(
        query,
        caption,
        reply_markup=reply_markup,
    )


async def _get_trade_token_balance(update, query, account_mode: str) -> int | None:
    user = update.effective_user
    try:
        profile = await asyncio.to_thread(
            trade_history_service.ensure_user,
            user.id,
            first_name=getattr(user, "first_name", None),
            last_name=getattr(user, "last_name", None),
            username=getattr(user, "username", None),
            account_mode=account_mode,
        )
    except Exception:
        logger.exception("Could not verify token balance before Auto Trade")
        await _safe_edit_message(
            query,
            "⚠️ Auto Trade is temporarily unavailable because your token balance "
            "could not be verified. Please try again shortly.",
        )
        return None

    try:
        return max(0, int(profile.get("token_balance", 0)))
    except (TypeError, ValueError):
        await _safe_edit_message(
            query,
            "⚠️ Auto Trade is temporarily unavailable because your token balance "
            "could not be verified. Please try again shortly.",
        )
        return None


async def _get_auto_trade_limit_status() -> dict:
    return await asyncio.to_thread(
        trade_history_service.get_auto_trade_session_limit_status
    )


async def _auto_trade_locked_seconds() -> int | None:
    try:
        status = await _get_auto_trade_limit_status()
    except Exception:
        logger.exception("Could not load Auto Trade limit for menu")
        return None
    return status["retry_after_seconds"] if status["locked"] else None


async def _dashboard_keyboard_with_limit():
    return TelegramKeyboard.dashboard_keyboard(
        locked_seconds=await _auto_trade_locked_seconds(),
    )


async def _menu_keyboard_with_limit():
    return TelegramKeyboard.menu_keyboard(
        settings.community_url,
        locked_seconds=await _auto_trade_locked_seconds(),
    )


def _format_auto_trade_countdown(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds:02d}s"


def _locked_auto_trade_keyboard(query, seconds: int):
    message = getattr(query, "message", None)
    markup = getattr(message, "reply_markup", None)
    buttons = getattr(markup, "inline_keyboard", [])
    is_menu = any(
        getattr(button, "callback_data", None) == "MENU_GET_TOKENS"
        for row in buttons
        for button in row
    )
    if is_menu:
        return TelegramKeyboard.menu_keyboard(
            settings.community_url,
            locked_seconds=seconds,
        )
    return TelegramKeyboard.dashboard_keyboard(locked_seconds=seconds)


async def _show_auto_trade_limit_notice(
    query,
    seconds: int,
    *,
    callback_already_answered: bool = False,
) -> None:
    countdown = _format_auto_trade_countdown(seconds)
    notice = (
        "Auto Trade is temporarily paused: the bot has reached its global "
        "limit of 10 sessions in a rolling hour. "
        f"New sessions are available in {countdown}."
    )
    if callback_already_answered:
        reply_text = getattr(
            getattr(query, "message", None),
            "reply_text",
            None,
        )
        if reply_text is not None:
            try:
                await reply_text(notice)
            except Exception:
                logger.exception("Could not send Auto Trade session-limit notice")
    else:
        try:
            await query.answer(notice, show_alert=True)
        except Exception:
            logger.exception("Could not show Auto Trade session-limit notice")
    try:
        await query.edit_message_reply_markup(
            reply_markup=_locked_auto_trade_keyboard(query, seconds),
        )
    except Exception:
        logger.exception("Could not update locked Auto Trade button")


async def _prompt_connect(update, query) -> None:
    user_id = update.effective_user.id
    await _safe_edit_message(
        query,
        handle_start(
            user_id,
            display_name=getattr(update.effective_user, "first_name", None),
        ),
        reply_markup=TelegramKeyboard.start_keyboard(),
    )


async def _show_auto_trade_setup(update, query) -> None:
    user_id = update.effective_user.id
    connected = bool(auth_service.get_valid_access_token(str(user_id)))
    if not connected:
        await _prompt_connect(update, query)
        return

    try:
        setup = await asyncio.to_thread(build_auto_trade_setup, user_id)
    except Exception:
        logger.exception("Could not retrieve Deriv data for the Auto Trade scan")
        await _send_scan_message(
            query,
            "⚠️ Live Deriv market data is temporarily unavailable. "
            "No trade has been placed. Please try scanning again shortly.",
            reply_markup=(
                await _dashboard_keyboard_with_limit()
                if connected
                else TelegramKeyboard.start_keyboard()
            ),
        )
        return

    text = handle_auto_trade_start(user_id, setup=setup)
    photo_url = _setup_photo_url(setup.get("direction"))
    reply_markup = (
        TelegramKeyboard.stake_keyboard(
            setup.get("symbol"),
            setup.get("direction"),
        )
        if setup.get("direction") in {"BULLISH", "BEARISH"} and connected
        else (
            await _dashboard_keyboard_with_limit()
            if connected
            else TelegramKeyboard.start_keyboard()
        )
    )

    if InputMediaPhoto is not None and photo_url:
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(
                    media=photo_url,
                    caption=text,
                    parse_mode=ParseMode.HTML if ParseMode else "HTML",
                ),
                reply_markup=reply_markup,
            )
            return
        except Exception:
            logger.exception("Could not update the Auto Trade setup image")

    await _send_scan_message(
        query,
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML if ParseMode else "HTML",
    )


async def _handle_auto_trade_entry(update, query, *, allow_low_balance: bool = False) -> None:
    user_id = update.effective_user.id
    try:
        limit_status = await _get_auto_trade_limit_status()
    except Exception:
        logger.exception("Could not verify the global Auto Trade session limit")
        await query.answer(
            "Auto Trade is temporarily unavailable while we check session "
            "availability. Please try again shortly.",
            show_alert=True,
        )
        return
    if limit_status["locked"]:
        await _show_auto_trade_limit_notice(
            query,
            limit_status["retry_after_seconds"],
        )
        return

    answer = getattr(query, "answer", None)
    if answer is not None:
        await answer()

    if not auth_service.get_valid_access_token(str(user_id)):
        await _prompt_connect(update, query)
        return

    token_balance = await _get_trade_token_balance(
        update,
        query,
        account_mode_service.get_mode(user_id),
    )
    if token_balance is None:
        return

    if token_balance <= 0:
        await _send_low_token_message(
            query,
            TOKEN_EXHAUSTED_MESSAGE,
            reply_markup=TelegramKeyboard.exhausted_token_keyboard(),
        )
        return

    if token_balance <= LOW_TOKEN_WARNING_THRESHOLD and not allow_low_balance:
        await _send_low_token_message(
            query,
            "💎 LOW TOKEN BALANCE\n\n"
            f"You have {token_balance} token{'s' if token_balance != 1 else ''} available.\n\n"
            "Each contract uses 1 token. If your balance reaches zero, Auto Trade "
            "will stop before placing another contract. Any contract already "
            "placed will still be monitored until it settles.\n\n"
            "You can top up now or continue with your current balance.",
            reply_markup=TelegramKeyboard.low_token_keyboard(
                settings.support_contact_url,
            ),
        )
        return

    await _show_auto_trade_setup(update, query)

def _setup_photo_url(direction: str | None) -> str | None:
    direction_value = str(direction or "").strip().upper()
    if direction_value == "BULLISH":
        return settings.bullish_setup_image_url
    if direction_value == "BEARISH":
        return settings.bearish_setup_image_url
    return None


def friendly_market_name(symbol: str | None) -> str:
    value = str(symbol or "").strip()
    if not value:
        return "Current market"

    mapping = {
        "STPRNG": "Step Index 100",
        "STPRNG2": "Step Index 200",
        "STPRNG3": "Step Index 300",
        "STPRNG4": "Step Index 400",
        "STPRNG5": "Step Index 500",
        "step_100": "Step Index 100",
        "step_200": "Step Index 200",
        "step_300": "Step Index 300",
        "step_400": "Step Index 400",
        "step_500": "Step Index 500",
    }
    return mapping.get(value, mapping.get(value.upper(), value))


async def execute_trade(user_id: str | int, state=None) -> dict:
    state = state or auto_trade_service.get_run(user_id)

    if state is None:
        return {"status": "rejected", "reason": "AUTO_TRADE_NOT_FOUND"}

    if state.status == "executing" or not auto_trade_service.mark_executing(user_id):
        return {"status": "rejected", "reason": "DUPLICATE_EXECUTION"}

    state.status = "executing"
    token_reserved = False
    purchase_confirmed = False

    try:
        user_key = str(user_id)
        access_token = auth_service.get_valid_access_token(user_key)

        if not access_token:
            state.status = "rejected"
            return {"status": "rejected", "reason": "DERIV_NOT_AUTHENTICATED"}

        selected_mode = account_mode_service.get_mode(user_key)

        # The preference must not change silently between setup and execution.
        if selected_mode != state.account_mode:
            state.status = "rejected"
            return {"status": "rejected", "reason": "ACCOUNT_MODE_CHANGED"}

        account = await account_service.get_trade_account(
            user_key,
            account_mode=state.account_mode,
        )

        verified_mode = str(account.get("account_type") or "").lower()
        if verified_mode != state.account_mode:
            state.status = "rejected"
            return {"status": "rejected", "reason": "ACCOUNT_MODE_VERIFICATION_FAILED"}

        account_id = account.get("account_id")
        currency = account.get("currency") or "USD"
        state.currency = str(currency).upper()

        if not account_id:
            state.status = "rejected"
            return {"status": "rejected", "reason": "MISSING_OPTIONS_ACCOUNT_ID"}

        ws_url = await account_service.request_account_otp_url(
            access_token=access_token,
            account_id=account_id,
        )

        try:
            reservation = await asyncio.to_thread(
                trade_history_service.reserve_token,
                user_id,
                state.session_id,
            )
        except Exception:
            state.status = "rejected"
            logger.exception("Could not reserve a Yields Token before purchase")
            return {
                "status": "token_service_error",
                "reason": "TOKEN_RESERVATION_FAILED",
            }

        if not reservation.get("ok"):
            state.status = "rejected"
            return {
                "status": "insufficient_tokens",
                "reason": "INSUFFICIENT_TOKENS",
                "token_balance": reservation.get("token_balance", 0),
            }
        token_reserved = True
        state.tokens_spent += 1

        result = await trade_execution_service.execute_trade(
            user_id=user_id,
            market_symbol=state.symbol,
            direction=state.direction,
            stake=state.current_stake,
            account_id=account_id,
            access_token=access_token,
            currency=currency,
            ws_url=ws_url,
            account_type=state.account_mode,
        )
        purchase_confirmed = result.get("status") == "accepted"

        if result.get("account_type") != state.account_mode:
            state.status = "rejected"
            return {
                "status": "rejected",
                "reason": "EXECUTION_ACCOUNT_MODE_MISMATCH",
            }

        if result.get("status") == "accepted":
            state.proposal_id = result.get("proposal_id")
            state.contract_id = result.get("contract_id")
            state.market_symbol = result.get("underlying_symbol")
            state.contract_type = result.get("contract_type")
            state.status = "monitoring"
            auto_trade_service.record_trade_stake(
                user_id,
                state.current_stake,
            )
        elif result.get("status") == "unknown_buy_outcome":
            state.status = "unknown_buy_outcome"
        else:
            state.status = "rejected"
            try:
                await asyncio.to_thread(
                    trade_history_service.refund_token,
                    user_id,
                    state.session_id,
                )
                state.tokens_spent = max(0, state.tokens_spent - 1)
            except Exception:
                logger.exception("Could not refund token for rejected purchase")

        return result

    except UnknownBuyOutcome as exc:
        state.status = "unknown_buy_outcome"
        return {"status": "unknown_buy_outcome", "reason": str(exc)}

    except Exception as exc:
        if token_reserved and not purchase_confirmed:
            try:
                await asyncio.to_thread(
                    trade_history_service.refund_token,
                    user_id,
                    state.session_id,
                )
                state.tokens_spent = max(0, state.tokens_spent - 1)
            except Exception:
                logger.exception("Could not refund token after pre-purchase failure")
        state.status = "rejected"
        return {"status": "rejected", "reason": str(exc)}

    finally:
        auto_trade_service.clear_execution_lock(user_id)


def _format_trade_tick(value) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "Unavailable"


async def _sync_profile(update) -> dict | None:
    user = getattr(update, "effective_user", None)
    if user is None:
        return None
    try:
        return await asyncio.to_thread(
            trade_history_service.ensure_user,
            user.id,
            first_name=getattr(user, "first_name", None),
            last_name=getattr(user, "last_name", None),
            username=getattr(user, "username", None),
            account_mode=account_mode_service.get_mode(str(user.id)),
            sync_telegram_profile=True,
        )
    except Exception:
        logger.exception("Could not sync Telegram profile with Supabase")
        return None


def _user_message(update):
    return (
        getattr(update, "effective_message", None)
        or getattr(update, "message", None)
    )


async def _start_payout_details(user_id, reply) -> None:
    await asyncio.to_thread(
        trade_history_service.set_payout_input_step,
        user_id,
        "country",
    )
    await reply(
        "🏦 Payout details setup\n\n"
        "First, send your country. Then send the bank or payout details that "
        "work best for receiving money there. Type them carefully; your details "
        "will be encrypted before storage. You can update them later."
    )


async def _send_referral_summary(update, *, edit_query=None) -> None:
    user = update.effective_user
    try:
        summary = await asyncio.to_thread(
            trade_history_service.get_referral_summary,
            user.id,
        )
    except Exception:
        logger.exception("Could not load referral summary")
        text = "Referral information is temporarily unavailable. Please try again."
        if edit_query is not None:
            await _safe_edit_message(edit_query, text)
        else:
            await _user_message(update).reply_text(text)
        return

    bot_username = str(settings.telegram_bot_username or "").strip().lstrip("@")
    referral_link = (
        f"https://t.me/{bot_username}?start=ref_{user.id}"
        if bot_username
        else "Referral link unavailable until TELEGRAM_BOT_USERNAME is configured."
    )
    text = (
        "🎁 YOUR REFERRALS\n\n"
        f"People referred: {int(summary.get('referral_count') or 0)}\n"
        f"Total earned: ${float(summary.get('total_earned') or 0):.2f} USD\n"
        f"Available to withdraw: "
        f"${float(summary.get('available_balance') or 0):.2f} USD\n\n"
        "Earn 5% of each verified token purchase made by your referrals. "
        "Withdrawals require at least $10 and are scheduled for the next "
        "15th or month-end payout date (UTC).\n\n"
        f"Your personal invite link:\n{referral_link}"
    )
    if edit_query is not None:
        await _safe_edit_message(
            edit_query,
            text,
            reply_markup=TelegramKeyboard.referral_keyboard(),
        )
    else:
        await _user_message(update).reply_text(
            text,
            reply_markup=TelegramKeyboard.referral_keyboard(),
        )


async def handle_referrals_command(update, context=None) -> None:
    del context
    await _sync_profile(update)
    await _send_referral_summary(update)


async def handle_payout_details_command(update, context=None) -> None:
    del context
    user = update.effective_user
    await _sync_profile(update)
    await _start_payout_details(user.id, _user_message(update).reply_text)


async def handle_withdraw_command(update, context=None) -> None:
    del context
    user = update.effective_user
    await _sync_profile(update)
    try:
        profile = await asyncio.to_thread(trade_history_service.get_user, user.id)
        if (
            not profile.get("payout_country")
            or not profile.get("payout_details_encrypted")
        ):
            await _start_payout_details(user.id, _user_message(update).reply_text)
            return
        withdrawal = await asyncio.to_thread(
            trade_history_service.create_referral_withdrawal,
            user.id,
            profile["payout_details_encrypted"],
        )
    except Exception as exc:
        reason = str(exc)
        if "REFERRAL_MINIMUM_WITHDRAWAL_NOT_MET" in reason:
            await _user_message(update).reply_text(
                "Withdrawals require at least $10. Your referral balance remains "
                "available until you reach the minimum."
            )
            return
        if "REFERRAL_WITHDRAWAL_ALREADY_PENDING" in reason:
            await _user_message(update).reply_text(
                "You already have a pending withdrawal request."
            )
            return
        logger.exception("Could not create referral withdrawal")
        await _user_message(update).reply_text(
            "The withdrawal request could not be created. Your earnings remain "
            "safe; please try again later."
        )
        return

    await _user_message(update).reply_text(
        f"✅ Withdrawal request received for "
        f"${float(withdrawal['amount']):.2f} USD.\n"
        f"Scheduled payout date: {withdrawal['scheduled_for']} (UTC).\n"
        "We’ll message you when it is paid or if any payout details need attention."
    )


async def _is_admin(user_id) -> bool:
    try:
        profile = await asyncio.to_thread(
            trade_history_service.get_user,
            user_id,
        )
        return bool(profile.get("is_admin"))
    except Exception:
        logger.exception("Could not verify referral admin access")
        return False


async def handle_admin_verify_purchase_command(update, context) -> None:
    admin_id = update.effective_user.id
    message = _user_message(update)
    if not await _is_admin(admin_id):
        await message.reply_text("This command is available to bot admins only.")
        return
    args = list(getattr(context, "args", []) or [])
    if len(args) not in {4, 5}:
        await message.reply_text(
            "Usage: /admin_verify_purchase <buyer_telegram_id> "
            "<whop_payment_id> <token_amount> <amount_usd> [currency]"
        )
        return
    buyer_id, payment_id, token_text, amount_text = args[:4]
    currency = args[4].upper() if len(args) == 5 else "USD"
    try:
        tokens = int(token_text)
        amount = Decimal(amount_text).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        if tokens <= 0 or amount <= 0 or currency != "USD":
            raise ValueError
    except (InvalidOperation, ValueError):
        await message.reply_text(
            "Purchase values must be positive; this referral ledger currently "
            "accepts USD purchases only."
        )
        return

    try:
        result = await asyncio.to_thread(
            trade_history_service.record_verified_token_purchase,
            provider="manual",
            provider_purchase_id=payment_id,
            provider_event_id=None,
            telegram_user_id=buyer_id,
            tokens=tokens,
            amount_paid=str(amount),
            currency=currency,
            verified_by_telegram_user_id=admin_id,
        )
    except Exception:
        logger.exception("Admin could not verify token purchase")
        await message.reply_text(
            "Purchase verification failed. Check the buyer, order ID, and "
            "database connection; no token credit was confirmed."
        )
        return
    if result.get("duplicate"):
        await message.reply_text(
            "That payment/order ID was already processed; no duplicate credit was made."
        )
        return

    await message.reply_text(
        f"✅ Verified {tokens} tokens for Telegram user {buyer_id}. "
        f"Referral commission credited: "
        f"${float(result.get('commission') or 0):.2f} USD."
    )
    application = getattr(context, "application", None)
    if application is not None:
        try:
            await application.bot.send_message(
                chat_id=int(buyer_id),
                text=f"✅ Your payment was verified. {tokens} Yields Tokens "
                "have been added to your balance.",
            )
        except Exception:
            logger.exception("Could not notify buyer of verified token purchase")
        referrer_id = result.get("referrer_telegram_user_id")
        commission = float(result.get("commission") or 0)
        if referrer_id and commission > 0:
            try:
                await application.bot.send_message(
                    chat_id=int(referrer_id),
                    text=(
                        "🎉 Your referral made a token purchase. "
                        f"${commission:.2f} USD (5%) has been added to your "
                        "referral earnings."
                    ),
                )
            except Exception:
                logger.exception("Could not notify referrer of manual commission")


async def handle_admin_withdrawals_command(update, context) -> None:
    admin_id = update.effective_user.id
    message = _user_message(update)
    if not await _is_admin(admin_id):
        await message.reply_text("This command is available to bot admins only.")
        return
    try:
        withdrawals = await asyncio.to_thread(
            trade_history_service.get_pending_referral_withdrawals,
            admin_id,
        )
    except Exception:
        logger.exception("Could not list due referral withdrawals")
        await message.reply_text("Pending withdrawals are temporarily unavailable.")
        return
    if not withdrawals:
        await message.reply_text("There are no due referral withdrawals.")
        return
    for withdrawal in withdrawals:
        try:
            payout_details = trade_history_service.decrypt_payout_details(
                withdrawal["payout_details_encrypted"]
            )
        except Exception:
            logger.exception(
                "Could not decrypt payout details for withdrawal %s",
                withdrawal.get("id"),
            )
            payout_details = "Unable to decrypt. Check payout encryption configuration."
        await message.reply_text(
            f"Withdrawal: {withdrawal['id']}\n"
            f"User: {withdrawal['telegram_user_id']}\n"
            f"Amount: ${float(withdrawal['amount']):.2f} USD\n"
            f"Country: {withdrawal.get('payout_country') or 'Not provided'}\n"
            f"Payout details:\n{payout_details}\n\n"
            f"Mark paid: /admin_mark_paid {withdrawal['id']}\n"
            f"Mark failed: /admin_mark_failed {withdrawal['id']} <reason>"
        )


async def _notify_withdrawal_user(context, withdrawal: dict) -> None:
    application = getattr(context, "application", None)
    if application is None:
        return
    text = (
        f"✅ Your referral payout of ${float(withdrawal['amount']):.2f} USD "
        "has been marked as paid."
        if withdrawal["status"] == "paid"
        else (
            "⚠️ Your referral payout could not be completed.\n\n"
            f"Reason: "
            f"{_payout_failure_reason_for_user(withdrawal.get('failure_reason'))}\n\n"
            "Your earnings have been returned to your available balance. "
            "Update your payout details and submit a new request when ready."
        )
    )
    keyboard = (
        None
        if withdrawal["status"] == "paid"
        else TelegramKeyboard.referral_keyboard()
    )
    try:
        await application.bot.send_message(
            chat_id=int(withdrawal["telegram_user_id"]),
            text=text,
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception(
            "Could not notify user %s about referral payout",
            withdrawal.get("telegram_user_id"),
        )


async def handle_admin_mark_paid_command(update, context) -> None:
    await _handle_admin_withdrawal_update(update, context, "paid")


async def handle_admin_mark_failed_command(update, context) -> None:
    await _handle_admin_withdrawal_update(update, context, "failed")


async def _handle_admin_withdrawal_update(update, context, status: str) -> None:
    admin_id = update.effective_user.id
    message = _user_message(update)
    if not await _is_admin(admin_id):
        await message.reply_text("This command is available to bot admins only.")
        return
    args = list(getattr(context, "args", []) or [])
    minimum = 2 if status == "failed" else 1
    if len(args) < minimum:
        usage = (
            "/admin_mark_failed <withdrawal_id> <reason>"
            if status == "failed"
            else "/admin_mark_paid <withdrawal_id>"
        )
        await message.reply_text(f"Usage: {usage}")
        return
    withdrawal_id = args[0]
    reason = " ".join(args[1:]).strip() if status == "failed" else None
    try:
        withdrawal = await asyncio.to_thread(
            trade_history_service.update_referral_withdrawal,
            admin_telegram_user_id=admin_id,
            withdrawal_id=withdrawal_id,
            status=status,
            failure_reason=reason,
        )
    except Exception:
        logger.exception("Could not update referral withdrawal")
        await message.reply_text(
            "The withdrawal could not be updated. It may already have been processed."
        )
        return
    await message.reply_text(
        f"Withdrawal {withdrawal_id} marked {status}."
    )
    await _notify_withdrawal_user(context, withdrawal)


def _cycle_report_text(
    cycle: int,
    completed_steps: list[str],
    pending_step: str | None = None,
) -> str:
    sections = list(completed_steps)
    if pending_step:
        sections.append(f"⚡ Cycle {cycle} {pending_step}")
    return "\n\n".join(sections)


def _auto_trade_failure_message(
    status: str | None,
    reason: str,
    deriv_error_code: str | None = None,
) -> str:
    if status == "insufficient_tokens":
        return TOKEN_EXHAUSTED_MESSAGE
    if status == "token_service_error":
        return "Your token balance could not be verified. No trade was attempted."
    if status == "unknown_buy_outcome":
        return (
            "The purchase outcome is uncertain. Auto Trade stopped to avoid "
            "placing a duplicate trade. Please check your Deriv account before "
            "trying again."
        )

    error_details = f"{deriv_error_code or ''} {reason}".lower()
    if "contractbuyvalidationerror" in error_details and "maximum payout" in error_details:
        return (
            "Deriv's maximum payout would be exceeded for this trade. "
            "Please choose a lower stake or a different setup."
        )
    if re.search(r"rate.?limit|too many requests|http\s*429", error_details):
        return (
            "Deriv is temporarily limiting requests. Auto Trade stopped safely. "
            "Please wait a little before starting another session."
        )
    return (
        "Deriv couldn't place the next trade, so Auto Trade stopped safely. "
        "Please try again shortly. If the issue continues, contact support."
    )


def _auto_trade_start_failure_message(error: Exception) -> str:
    if str(error) == "AUTO_TRADE_ALREADY_RUNNING":
        return "An Auto Trade session is already active. Finish or cancel it before starting another."
    if str(error) == "INVALID_STAKE":
        return "That stake amount is invalid. Please choose a valid amount and try again."
    return "Auto Trade could not start right now. Please try again shortly."


def _payout_failure_reason_for_user(reason: object) -> str:
    message = str(reason or "").strip()
    technical_detail = re.search(
        r"https?://|(?i:\b(?:traceback|exception|stack trace|http|sql|database|"
        r"endpoint|api|status code)\b)|[A-Z][A-Za-z]+Error\b|"
        r"\b[A-Z][A-Z0-9_]{5,}\b",
        message,
    )
    if not message or len(message) > 240 or technical_detail:
        return (
            "Our payment partner could not complete the transfer. "
            "Please review your payout details and contact support if needed."
        )
    return message


async def _update_cycle_report(query, text: str, message=None):
    if message is not None:
        edit_text = getattr(message, "edit_text", None)
        if edit_text is not None:
            try:
                await edit_text(text, parse_mode="HTML")
                return message
            except Exception:
                logger.exception("Could not update Auto Trade cycle report")

    parent_message = getattr(query, "message", None)
    reply_text = getattr(parent_message, "reply_text", None)
    if reply_text is not None:
        try:
            return await reply_text(text, parse_mode="HTML")
        except Exception:
            logger.exception("Could not send Auto Trade cycle report")

    await _safe_edit_message(query, text, parse_mode="HTML")
    return None


async def _monitor_settlement(user_id: str | int, state) -> dict:
    user_key = str(user_id)
    access_token = auth_service.get_valid_access_token(user_key)

    if not access_token:
        raise RuntimeError("DERIV_NOT_AUTHENTICATED_DURING_MONITORING")

    account = await account_service.get_trade_account(
        user_key,
        account_mode=state.account_mode,
    )

    if str(account.get("account_type") or "").lower() != state.account_mode:
        raise RuntimeError("ACCOUNT_MODE_VERIFICATION_FAILED_DURING_MONITORING")

    ws_url = await account_service.request_account_otp_url(
        access_token=access_token,
        account_id=account["account_id"],
    )

    return await trade_execution_service.monitor_contract(
        contract_id=state.contract_id,
        ws_url=ws_url,
        timeout_seconds=75,
        market_symbol=state.market_symbol or state.symbol,
    )


async def _run_auto_trade_loop(update, state) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    settled_contracts: list[dict] = []
    report_cycle: int | None = None
    cycle_report_message = None
    cycle_steps: list[str] = []
    session_status_message_updated = False

    while True:
        current = auto_trade_service.get_run(user_id)

        if current is None:
            await _safe_edit_message(query, "⚠️ Auto Trade state was not found.")
            return

        if current.status == "cancelled":
            await _save_session_summary(current, "cancelled")
            await _send_session_end_message(
                query,
                user_id,
                current,
                "Cancelled by you.",
            )
            return

        if current.status == "completed":
            await _save_session_summary(current, "completed")
            await _send_session_end_message(
                query,
                user_id,
                current,
            )
            return

        if current.status not in {"running", "monitoring"}:
            await _save_session_summary(current, "failed")
            await _send_session_end_message(
                query,
                user_id,
                current,
                f"Auto Trade stopped safely ({current.status.replace('_', ' ')}).",
            )
            return

        if current.status == "monitoring":
            try:
                settlement = await _monitor_settlement(user_id, current)
            except Exception:
                logger.exception(
                    "Could not verify settlement for contract %s",
                    current.contract_id,
                )
                current = auto_trade_service.finish_run(user_id, "monitoring_error") or current
                if report_cycle == current.cycle:
                    cycle_steps.append(
                        "⚠️ Contract settlement could not be verified. "
                        "No further trade was placed."
                    )
                    cycle_report_message = await _update_cycle_report(
                        query,
                        _cycle_report_text(current.cycle, cycle_steps),
                        cycle_report_message,
                    )
                await _save_session_summary(current, "failed")
                await _send_session_end_message(
                    query,
                    user_id,
                    current,
                    "Settlement could not be verified. No further trade was placed.",
                )
                return

            try:
                profit = float(settlement.get("profit") or 0.0)
            except (TypeError, ValueError):
                current = auto_trade_service.finish_run(user_id, "settlement_data_error") or current
                await _save_session_summary(current, "failed")
                await _send_session_end_message(
                    query,
                    user_id,
                    current,
                    "Settlement data could not be validated. No further trade was placed.",
                )
                return

            won = profit > 0
            entry_tick = settlement.get("entry_tick")
            exit_tick = settlement.get("exit_tick")
            direction_accurate = None
            try:
                entry_value = float(entry_tick)
                exit_value = float(exit_tick)
                if current.direction == "BULLISH":
                    direction_accurate = exit_value > entry_value
                elif current.direction == "BEARISH":
                    direction_accurate = exit_value < entry_value
            except (TypeError, ValueError):
                pass

            history_saved = False
            try:
                await asyncio.to_thread(
                    trade_history_service.record_settled_trade,
                    {
                        "telegram_user_id": user_id,
                        "contract_id": settlement.get("contract_id") or current.contract_id,
                        "session_id": current.session_id,
                        "account_mode": current.account_mode,
                        "market_symbol": current.market_symbol or current.symbol,
                        "direction": current.direction,
                        "stake": current.current_stake,
                        "profit": profit,
                        "direction_accurate": direction_accurate,
                        "tokens_spent": 1,
                    },
                )
                history_saved = True
            except Exception:
                logger.exception("Could not save settled trade to Supabase trade history")

            trade_cycle = current.cycle
            trade_step = current.martingale_step
            trade_stake = current.current_stake
            settled_contracts.append(settlement)
            current = auto_trade_service.advance_after_result(
                user_id,
                won=won,
                profit=profit,
            )
            await _save_session_summary(
                current,
                (
                    "completed"
                    if current.status == "completed"
                    else "running"
                ),
            )

            if report_cycle != trade_cycle:
                report_cycle = trade_cycle
                cycle_report_message = None
                cycle_steps = []

            result_emoji = "🟢" if won else "🔴"
            profit_text = (
                f"+${profit:.2f}" if profit > 0 else f"-${abs(profit):.2f}"
            )
            step_details = (
                f"{result_emoji} Invest: ${trade_stake:.2f} → {profit_text}\n"
                f"📊 Price: {_format_trade_tick(entry_tick)} → "
                f"{_format_trade_tick(exit_tick)}"
            )
            result_text = (
                f"⚡ Cycle {trade_cycle} Step {trade_step}\n"
                f"<pre>{html.escape(step_details)}</pre>"
            )
            if not won and current.status == "running":
                result_text += (
                    "\n\n🔄 Smart Scaling activated\n"
                    f"Next stake: ${current.current_stake:.2f}"
                )
            elif won:
                result_text += "\n\n✅ Cycle completed."

            if not history_saved:
                result_text += "\n\n⚠️ This settled trade could not be saved to Stats."
            cycle_steps.append(result_text)
            cycle_report_message = await _update_cycle_report(
                query,
                _cycle_report_text(trade_cycle, cycle_steps),
                cycle_report_message,
            )

            if current.status != "running":
                continue

            continue

        # Check the selected mode again before every purchase.
        if account_mode_service.get_mode(user_id) != current.account_mode:
            current = auto_trade_service.finish_run(user_id, "account_mode_changed") or current
            await _save_session_summary(current, "stopped")
            await _send_session_end_message(
                query,
                user_id,
                current,
                "Account mode changed. No further purchase was attempted.",
            )
            return

        result = await execute_trade(user_id, current)

        if result.get("status") != "accepted":
            status = result.get("status")
            reason = result.get("reason", "Unknown error")

            logger.warning(
                "Auto Trade stopped before placing the next trade "
                "(status=%s, deriv_error_code=%s, reason=%s)",
                status,
                result.get("deriv_error_code"),
                reason,
            )
            auto_trade_service.finish_run(
                user_id,
                "unknown_buy_outcome" if status == "unknown_buy_outcome" else "rejected",
            )

            current = auto_trade_service.get_run(user_id) or current
            status_reason = _auto_trade_failure_message(
                status,
                str(reason),
                str(result.get("deriv_error_code") or ""),
            )
            await _save_session_summary(
                current,
                "failed" if status in {"unknown_buy_outcome", "token_service_error"} else "stopped",
            )
            if status == "insufficient_tokens":
                await _send_low_token_message(
                    query,
                    TOKEN_EXHAUSTED_MESSAGE,
                    reply_markup=TelegramKeyboard.exhausted_token_keyboard(),
                )
            else:
                await _send_session_end_message(
                    query,
                    user_id,
                    current,
                    status_reason,
                )
            return

        market_label = friendly_market_name(current.market_symbol or current.symbol)
        if not session_status_message_updated:
            await _safe_edit_message(
                query,
                "🤖 AUTO TRADE RUNNING\n\n"
                f"Account mode: {current.account_mode.upper()}\n"
                f"Market: {market_label}\n"
                f"Direction: {current.direction}\n"
                f"Target: {current.max_cycles} winning cycles\n"
                "Smart Scaling continues after losses until the target wins, "
                "available balance/tokens, or a Deriv rejection stops the run.",
            )
            session_status_message_updated = True

        if report_cycle != current.cycle:
            report_cycle = current.cycle
            cycle_report_message = None
            cycle_steps = []
        pending_step = (
            f"Step {current.martingale_step}\n⏳ Purchased for "
            f"${current.current_stake:.2f}. Waiting for the 30-second "
            "contract to settle."
        )
        cycle_report_message = await _update_cycle_report(
            query,
            _cycle_report_text(current.cycle, cycle_steps, pending_step),
            cycle_report_message,
        )


async def _run_auto_trade(update, state) -> None:
    try:
        await _run_auto_trade_loop(update, state)
    except Exception:
        logger.exception("Unexpected failure during Auto Trade session")
        user_id = update.effective_user.id
        current = auto_trade_service.get_run(user_id) or state
        current = auto_trade_service.finish_run(user_id, "failed") or current
        await _save_session_summary(current, "failed")
        await _send_session_end_message(
            update.callback_query,
            user_id,
            current,
            "A temporary problem interrupted Auto Trade. No further trades "
            "were placed. Please check your Deriv account before starting "
            "another session.",
        )


async def _prepare_trade_confirmation(
    update,
    query,
    *,
    setup: dict,
    stake_value: float,
    currency: str,
    account_mode: str,
    token_balance: int,
    stake_description: str,
    account_balance: object | None = None,
    stake_percentage_key: str | None = None,
) -> bool:
    user_id = update.effective_user.id

    async def report(text: str) -> None:
        if hasattr(query, "edit_message_text"):
            await _safe_edit_message(query, text)
        else:
            await query.reply_text(text)

    if auto_trade_service.is_running(user_id):
        await report("⚠️ An Auto Trade run is already active.")
        return False

    try:
        auto_trade_service.start_run(
            user_id=user_id,
            symbol=setup["symbol"],
            direction=setup["direction"],
            base_stake=stake_value,
            account_mode=account_mode,
        )
        state = auto_trade_service.get_run(user_id)
        if state is not None:
            state.currency = currency
            state.stake_percentage_key = stake_percentage_key
            try:
                starting_balance = float(account_balance)
                if starting_balance >= 0:
                    state.starting_balance = starting_balance
            except (TypeError, ValueError):
                state.starting_balance = None
    except (RuntimeError, ValueError) as exc:
        logger.warning("Auto Trade preparation was rejected: %s", exc)
        await report(
            "⚠️ " + _auto_trade_start_failure_message(exc)
        )
        return False

    confirmation = (
        "⚡ TRADE CONFIRMATION\n\n"
        f"Market: {setup.get('display_name', setup['symbol'])}\n"
        f"Direction: {setup['direction']}\n"
        f"Account mode: {account_mode.upper()}\n"
        f"Initial stake: {stake_value:,.2f} {currency}"
        f"{stake_description}\n"
        f"Available tokens: {token_balance}\n"
        "Expiry: 30 seconds\n"
        "Target: 5 winning cycles\n"
        "There is no consecutive-loss stop right now. Smart Scaling raises "
        "the next stake after each loss, so it can grow quickly and may exceed "
        "your available balance.\n\n"
        "A loss stays in the current cycle and increases the next stake "
        "through Smart Scaling to help recover. "
        "A settled win advances the cycle and resets the stake.\n\n"
        "Confirm to begin."
    )
    keyboard = TelegramKeyboard.confirmation_keyboard()
    if hasattr(query, "edit_message_text"):
        await _safe_edit_message(
            query,
            confirmation,
            reply_markup=keyboard,
        )
    else:
        await query.reply_text(confirmation, reply_markup=keyboard)
    return True


async def handle_custom_stake_input(update, context=None) -> None:
    del context
    message = update.message
    user = update.effective_user
    profile = await _sync_profile(update)
    payout_step = profile.get("payout_input_step") if profile else None
    if payout_step == "country":
        try:
            await asyncio.to_thread(
                trade_history_service.save_payout_country,
                user.id,
                message.text or "",
            )
        except ValueError:
            await message.reply_text(
                "Please send a valid country name (up to 100 characters)."
            )
            return
        except Exception:
            logger.exception("Could not save user's payout country")
            await message.reply_text(
                "Could not save your country right now. Please try again."
            )
            return
        await message.reply_text(
            "Now send your bank or payout details for receiving money in that "
            "country. Include the information carefully; it will be encrypted "
            "before storage. You can update it later."
        )
        return
    if payout_step == "details":
        try:
            await asyncio.to_thread(
                trade_history_service.save_payout_details,
                user.id,
                message.text or "",
            )
        except ValueError:
            await message.reply_text(
                "Please send payout details as text (up to 2,000 characters)."
            )
            return
        except Exception:
            logger.exception("Could not save user's encrypted payout details")
            await message.reply_text(
                "Could not securely save those details. Please try again later."
            )
            return
        await message.reply_text(
            "✅ Your payout details were saved securely. You can change them "
            "any time from 🎁 Invite."
        )
        return

    request = auto_trade_service.get_custom_stake_request(user.id)
    if request is None:
        return

    def reply(text: str, **kwargs):
        return message.reply_text(text, **kwargs)

    try:
        stake_value = parse_custom_stake(message.text)
    except ValueError:
        await reply(
            "⚠️ Enter a valid stake greater than zero, with no more than "
            "two decimal places (for example, 12.50)."
        )
        return

    account_mode = str(request["account_mode"])
    if account_mode_service.get_mode(user.id) != account_mode:
        auto_trade_service.clear_custom_stake_request(user.id)
        await reply(
            "⚠️ Your account mode changed. Please select a stake again "
            "from the current Auto Trade setup."
        )
        return
    if not auth_service.get_valid_access_token(str(user.id)):
        auto_trade_service.clear_custom_stake_request(user.id)
        await reply("⚠️ Connect your Deriv account before setting a stake.")
        return

    try:
        profile = await asyncio.to_thread(
            trade_history_service.ensure_user,
            user.id,
            first_name=getattr(user, "first_name", None),
            last_name=getattr(user, "last_name", None),
            username=getattr(user, "username", None),
            account_mode=account_mode,
        )
        token_balance = max(0, int(profile.get("token_balance", 0)))
    except Exception:
        logger.exception("Could not verify tokens for custom-stake Auto Trade")
        await reply(
            "⚠️ Auto Trade is temporarily unavailable because your token "
            "balance could not be verified. Please try again shortly."
        )
        return
    if token_balance <= 0:
        auto_trade_service.clear_custom_stake_request(user.id)
        await reply(TOKEN_EXHAUSTED_MESSAGE)
        return

    try:
        selected_account = await account_service.get_trade_account(
            user.id,
            account_mode=account_mode,
        )
        balance = Decimal(str(selected_account.get("balance")))
        if not balance.is_finite() or balance <= 0:
            raise ValueError("INVALID_ACCOUNT_BALANCE")
    except Exception:
        logger.exception("Could not verify account balance for custom stake")
        await reply(
            "⚠️ The selected account balance could not be verified, so no "
            "trade was prepared. Please try again."
        )
        return

    if Decimal(str(stake_value)) > balance:
        await reply(
            f"⚠️ Your stake (${stake_value:,.2f}) exceeds the available "
            f"{account_mode.upper()} balance of "
            f"${balance:,.2f} {selected_account.get('currency') or 'USD'}. "
            "Enter a smaller amount."
        )
        return

    symbol = request.get("symbol")
    direction = request.get("direction")
    if symbol and direction:
        setup = {
            "symbol": symbol,
            "display_name": friendly_market_name(symbol),
            "direction": direction,
        }
    else:
        try:
            setup = await asyncio.to_thread(build_auto_trade_setup, user.id)
        except Exception:
            logger.exception("Could not retrieve setup for custom-stake Auto Trade")
            await reply(
                "⚠️ Live market data is temporarily unavailable. "
                "No trade was placed. Please try again shortly."
            )
            return
    if (
        setup.get("symbol") == "NO_SETUP"
        or setup.get("direction") not in {"BULLISH", "BEARISH"}
    ):
        auto_trade_service.clear_custom_stake_request(user.id)
        await reply(
            "⚠️ No tradable setup is currently available. No trade has been placed."
        )
        return

    confirmed = await _prepare_trade_confirmation(
        update,
        message,
        setup=setup,
        stake_value=stake_value,
        currency=str(selected_account.get("currency") or "USD").upper(),
        account_mode=account_mode,
        token_balance=token_balance,
        stake_description=" (custom amount)",
        account_balance=balance,
    )
    if confirmed:
        auto_trade_service.clear_custom_stake_request(user.id)


async def dispatch_callback(update, context) -> None:
    if update.callback_query is None:
        return

    query = update.callback_query
    data = query.data or ""
    if data not in {
        "START_AUTO_TRADE",
        "AUTO_TRADE_LIMIT_INFO",
        "AUTO_TRADE_START_SCAN",
        "AUTO_TRADE_TRADE_ANYWAY",
    }:
        await query.answer()

    user_id = update.effective_user.id
    user_key = str(user_id)

    if data == "WELCOME_GIFT_ACCEPT_200":
        try:
            result = await asyncio.to_thread(
                trade_history_service.claim_welcome_gift_200,
                user_id,
            )
        except Exception:
            logger.exception("Could not grant user's 200-token welcome gift")
            await _safe_edit_message(
                query,
                "The welcome gift could not be verified right now. Your balance "
                "has not changed. Please try again shortly.",
            )
            return
        if result.get("granted"):
            await _safe_edit_message(
                query,
                "🎉 Your 200-token first-time gift has been added to your "
                f"balance! You now have {result['token_balance']} Yields Tokens "
                "to use for trading.",
            )
        else:
            await _safe_edit_message(
                query,
                "This first-time 200-token gift has already been claimed. "
                f"Your current balance is {result['token_balance']} Yields Tokens.",
            )
        return

    if data == "REFERRAL_PAYOUT_DETAILS":
        await _start_payout_details(
            user_id,
            query.message.reply_text,
        )
        return

    if data == "REFERRAL_WITHDRAW":
        try:
            profile = await asyncio.to_thread(
                trade_history_service.get_user,
                user_id,
            )
        except Exception:
            logger.exception("Could not load payout profile")
            await _safe_edit_message(
                query,
                "Payout information is temporarily unavailable. Please try again.",
            )
            return
        if not profile.get("payout_country") or not profile.get("payout_details_encrypted"):
            await _start_payout_details(
                user_id,
                query.message.reply_text,
            )
            return
        try:
            withdrawal = await asyncio.to_thread(
                trade_history_service.create_referral_withdrawal,
                user_id,
                profile["payout_details_encrypted"],
            )
        except Exception as exc:
            reason = str(exc)
            if "REFERRAL_MINIMUM_WITHDRAWAL_NOT_MET" in reason:
                await query.message.reply_text(
                    "Withdrawals require at least $10. Your referral earnings "
                    "remain available until you reach the minimum."
                )
                return
            if "REFERRAL_WITHDRAWAL_ALREADY_PENDING" in reason:
                await query.message.reply_text(
                    "You already have a pending withdrawal request."
                )
                return
            logger.exception("Could not create referral withdrawal")
            await query.message.reply_text(
                "The withdrawal could not be created. Your earnings remain "
                "available; please try again later."
            )
            return
        await query.message.reply_text(
            f"✅ Withdrawal request received for "
            f"${float(withdrawal['amount']):.2f} USD.\n"
            f"Scheduled payout: {withdrawal['scheduled_for']} (UTC). "
            "We’ll notify you when it is processed."
        )
        return

    if data == "START_CONNECT":
        message, connect_url = build_connect_prompt(user_id)
        await _safe_edit_message(
            query,
            message,
            reply_markup=TelegramKeyboard.connect_keyboard(connect_url),
        )
        return

    if data == "START_INFO":
        await _safe_edit_message(
            query,
            "🤖 YIELDS\n\n"
            "AI-powered trading intelligence for Deriv.\n\n"
            "How it works:\n"
            "• AI ranks the 5 fixed Step Indices\n"
            "• You choose the stake\n"
            "• Auto Trade executes the selected Rise/Fall setup\n"
            "• Each cycle reports its trade steps and results\n"
            "• Smart Scaling increases the next stake after a loss\n"
            "• There is currently no consecutive-loss stop; higher stakes can "
            "grow quickly\n"
            "• The run stops after five winning cycles, when tokens or balance "
            "run out, or if Deriv rejects a trade"
        )
        return

    if data == "START_DASHBOARD":
        display_name = getattr(update.effective_user, "first_name", None) or getattr(update.effective_user, "username", None) or "friend"
        if not auth_service.get_valid_access_token(user_key):
            await query.edit_message_text(
                handle_start(user_id, display_name=display_name),
                reply_markup=TelegramKeyboard.start_keyboard(),
            )
            return

        await query.edit_message_text(
            await build_start_message(user_id, display_name=display_name),
            reply_markup=await _dashboard_keyboard_with_limit(),
        )
        return

    if data == "START_BALANCE":
        await query.edit_message_text(
            await handle_balance(user_id),
            reply_markup=TelegramKeyboard.status_keyboard(),
        )
        return

    if data == "START_STATUS":
        await query.edit_message_text(
            handle_status(user_id),
            reply_markup=TelegramKeyboard.status_keyboard(),
        )
        return

    if data == "START_FAQ":
        await query.edit_message_text(
            handle_faq(),
            reply_markup=await _dashboard_keyboard_with_limit(),
        )
        return

    if data == "MENU_FAQ":
        await _safe_edit_message(
            query,
            handle_faq(),
            reply_markup=await _menu_keyboard_with_limit(),
        )
        return

    if data == "MENU_MAIN":
        display_name = getattr(update.effective_user, "first_name", None)
        text = await build_menu_message(
            user_id,
            display_name=display_name,
            last_name=getattr(update.effective_user, "last_name", None),
            username=getattr(update.effective_user, "username", None),
        )
        keyboard = (
            await _menu_keyboard_with_limit()
            if auth_service.get_valid_access_token(user_key)
            else TelegramKeyboard.start_keyboard()
        )
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(media=settings.menu_image_url, caption=text),
                reply_markup=keyboard,
            )
        except Exception:
            await _safe_edit_message(
                query,
                text,
                reply_markup=keyboard,
            )
        return

    if data == "MENU_GET_TOKENS":
        await _show_token_photo(
            query,
            build_token_store_message(),
            TelegramKeyboard.token_store_keyboard(),
        )
        return

    if data.startswith("TOKEN_PACKAGE_"):
        try:
            token_amount = int(data.removeprefix("TOKEN_PACKAGE_"))
            _price, payment_url = TOKEN_PACKAGES[token_amount]
        except (KeyError, ValueError):
            await _safe_edit_message(
                query,
                "⚠️ That token package is unavailable. Please choose a package again.",
                reply_markup=TelegramKeyboard.token_store_keyboard(),
            )
            return

        await _show_token_photo(
            query,
            build_token_purchase_message(token_amount),
            TelegramKeyboard.token_purchase_keyboard(payment_url),
        )
        return

    if data == "MENU_INVITE":
        await _send_referral_summary(update, edit_query=query)
        return

    if data == "START_STATS":
        await query.edit_message_text(
            await handle_stats(user_id),
            reply_markup=await _dashboard_keyboard_with_limit(),
        )
        return

    if data == "START_SETTINGS":
        await query.edit_message_text(
            handle_account_mode_settings(user_id),
            reply_markup=TelegramKeyboard.account_mode_keyboard(
                account_mode_service.get_mode(user_id)
            ),
        )
        return

    if data == "ACCOUNT_MODE_DEMO":
        if not auth_service.get_valid_access_token(user_key):
            await query.edit_message_text(
                "🔴 Connect your Deriv account before changing account mode."
            )
            return

        account_mode_service.set_mode(user_id, "demo")
        try:
            await asyncio.to_thread(
                trade_history_service.update_user_mode,
                user_id,
                "demo",
            )
        except Exception:
            logger.exception("Could not sync Demo mode to Supabase user profile")
        await query.edit_message_text(
            "🟠 Demo Mode selected.\n\n"
            "Auto Trade will use a verified Demo Options account. "
            "It will not fall back to Real.",
            reply_markup=TelegramKeyboard.account_mode_keyboard("demo"),
        )
        return

    if data == "ACCOUNT_MODE_REAL":
        if not auth_service.get_valid_access_token(user_key):
            await query.edit_message_text(
                "🔴 Connect your Deriv account before changing account mode."
            )
            return

        await query.edit_message_text(
            "⚠️ CONFIRM REAL MODE\n\n"
            "Real Mode can place trades using real funds. Losses and "
            "martingale stake increases can affect your actual balance.\n\n"
            "Confirm only if you intend to trade your Real account.",
            reply_markup=TelegramKeyboard.real_mode_confirmation_keyboard(),
        )
        return

    if data == "ACCOUNT_MODE_REAL_CONFIRM":
        if not auth_service.get_valid_access_token(user_key):
            await query.edit_message_text(
                "🔴 Connect your Deriv account before changing account mode."
            )
            return

        account_mode_service.set_mode(user_id, "real")
        try:
            await asyncio.to_thread(
                trade_history_service.update_user_mode,
                user_id,
                "real",
            )
        except Exception:
            logger.exception("Could not sync Real mode to Supabase user profile")
        await query.edit_message_text(
            "🟢 Real Mode selected.\n\n"
            "Before trading, Yields will verify that a Real Options account "
            "is available. If verification fails, the trade will stop rather "
            "than falling back to Demo.",
            reply_markup=TelegramKeyboard.account_mode_keyboard("real"),
        )
        return

    if data == "START_LOGOUT":
        await query.edit_message_text(handle_logout(user_id))
        return

    if data == "START_AUTO_TRADE":
        await _handle_auto_trade_entry(update, query)
        return

    if data == "AUTO_TRADE_LIMIT_INFO":
        try:
            limit_status = await _get_auto_trade_limit_status()
        except Exception:
            logger.exception("Could not refresh global Auto Trade session limit")
            await query.answer(
                "Auto Trade is temporarily unavailable while we check session "
                "availability. Please try again shortly.",
                show_alert=True,
            )
            return
        if limit_status["locked"]:
            await _show_auto_trade_limit_notice(
                query,
                limit_status["retry_after_seconds"],
            )
        else:
            await _handle_auto_trade_entry(update, query)
        return

    if data == "AUTO_TRADE_START_SCAN":
        await _handle_auto_trade_entry(update, query)
        return

    if data == "AUTO_TRADE_TRADE_ANYWAY":
        await _handle_auto_trade_entry(update, query, allow_low_balance=True)
        return

    if data == "AUTO_TRADE_CANCEL":
        auto_trade_service.clear_custom_stake_request(user_id)
        state = auto_trade_service.cancel_run(user_id)
        if state is None:
            await _safe_edit_message(
                query,
                "🛑 Auto Trade cancelled. No trade has been placed."
            )
        else:
            await _save_session_summary(state, "cancelled")
            await _send_session_end_message(
                query,
                user_id,
                state,
                "Cancelled by you. An already purchased contract was allowed to settle.",
            )
        return

    if data == "AUTO_TRADE_CONFIRM":
        if not auth_service.get_valid_access_token(user_key):
            await _prompt_connect(update, query)
            return

        state = auto_trade_service.get_run(user_id)

        if state is None:
            await _safe_edit_message(
                query,
                "⚠️ No active Auto Trade session is available to confirm."
            )
            return

        effective_user = update.effective_user
        try:
            if state.stake_percentage_key is not None:
                selected_account = await account_service.get_trade_account(
                    user_id,
                    account_mode=state.account_mode,
                )
                state.starting_balance = float(selected_account.get("balance"))
                refreshed_stake = calculate_relative_stake(
                    selected_account.get("balance"),
                    state.stake_percentage_key,
                )
                if refreshed_stake <= 0:
                    await _safe_edit_message(
                        query,
                        "⚠️ This percentage is below $0.01 for your current account "
                        "balance, so no trade can be placed. Increase your balance "
                        "or choose a higher percentage.",
                    )
                    return
                state.base_stake = float(refreshed_stake)
                state.current_stake = float(refreshed_stake)
                state.currency = str(selected_account.get("currency") or "USD").upper()

            profile = await asyncio.to_thread(
                trade_history_service.ensure_user,
                user_id,
                first_name=getattr(effective_user, "first_name", None),
                last_name=getattr(effective_user, "last_name", None),
                username=getattr(effective_user, "username", None),
                account_mode=state.account_mode,
            )
            token_balance = int(profile.get("token_balance", 0))
            if token_balance <= 0:
                await _send_low_token_message(
                    query,
                    TOKEN_EXHAUSTED_MESSAGE,
                    reply_markup=TelegramKeyboard.exhausted_token_keyboard(),
                )
                return

            session_start = await asyncio.to_thread(
                trade_history_service.start_auto_trade_session,
                {
                    "session_id": state.session_id,
                    "telegram_user_id": user_id,
                    "account_mode": state.account_mode,
                    "market_symbol": state.symbol,
                    "direction": state.direction,
                    "initial_stake": state.base_stake,
                    "target_winning_cycles": state.max_cycles,
                },
            )
            if not session_start["started"]:
                auto_trade_service.cancel_run(user_id)
                await _show_auto_trade_limit_notice(
                    query,
                    session_start["retry_after_seconds"],
                    callback_already_answered=True,
                )
                return
        except Exception:
            logger.exception("Could not create Supabase Auto Trade session")
            await _safe_edit_message(
                query,
                "⚠️ Auto Trade is temporarily unavailable because your account "
                "could not be prepared. No trade has been placed. Please try again shortly.",
            )
            return

        try:
            auto_trade_service.confirm_run(user_id)
        except RuntimeError as exc:
            logger.warning("Auto Trade confirmation failed: %s", exc)
            failed_state = auto_trade_service.finish_run(user_id, "failed")
            if failed_state is not None:
                await _save_session_summary(failed_state, "failed")
            await _safe_edit_message(
                query,
                "⚠️ Auto Trade could not start because the session is no longer "
                "available. Please start a new session and try again."
            )
            return

        await _run_auto_trade(update, state)
        return

    if data.startswith("AUTO_TRADE_CUSTOM_STAKE"):
        if not auth_service.get_valid_access_token(user_key):
            await _prompt_connect(update, query)
            return
        if auto_trade_service.is_running(user_id):
            await _safe_edit_message(
                query,
                "⚠️ An Auto Trade run is already active.",
            )
            return

        selection = data.removeprefix("AUTO_TRADE_CUSTOM_STAKE")
        selected_symbol = None
        selected_direction = None
        if selection:
            if not selection.startswith("_"):
                await _safe_edit_message(
                    query,
                    "⚠️ Invalid custom-stake selection. Please restart Auto Trade.",
                )
                return
            market_and_direction = selection[1:]
            if "_" not in market_and_direction:
                await _safe_edit_message(
                    query,
                    "⚠️ Invalid custom-stake selection. Please restart Auto Trade.",
                )
                return
            selected_symbol, selected_direction = market_and_direction.rsplit("_", 1)
            if (
                selected_symbol not in settings.auto_trade_markets
                or selected_direction not in {"BULLISH", "BEARISH"}
            ):
                await _safe_edit_message(
                    query,
                    "⚠️ Invalid custom-stake selection. Please restart Auto Trade.",
                )
                return

        mode = account_mode_service.get_mode(user_id)
        auto_trade_service.set_custom_stake_request(
            user_id,
            selected_symbol,
            selected_direction,
            mode,
        )
        await _safe_edit_message(
            query,
            "✏️ Send your preferred stake amount as a number, for example "
            "12.50. The amount must be greater than zero, may have up to "
            "two decimal places, and cannot exceed your available account balance.",
            reply_markup=TelegramKeyboard.custom_stake_prompt_keyboard(),
        )
        return

    if data.startswith("AUTO_TRADE_STAKE_"):
        if not auth_service.get_valid_access_token(user_key):
            await _prompt_connect(update, query)
            return

        percentage_key, selected_symbol, selected_direction = (
            _stake_selection_from_callback(data)
        )
        if percentage_key is None:
            await query.edit_message_text(
                "⚠️ Invalid stake selection. Choose one of the available stake buttons."
            )
            return

        mode = account_mode_service.get_mode(user_id)
        effective_user = update.effective_user
        try:
            profile = await asyncio.to_thread(
                trade_history_service.ensure_user,
                user_id,
                first_name=getattr(effective_user, "first_name", None),
                last_name=getattr(effective_user, "last_name", None),
                username=getattr(effective_user, "username", None),
                account_mode=mode,
            )
        except Exception:
            logger.exception("Could not check token balance for Auto Trade")
            await _safe_edit_message(
                query,
                "⚠️ Auto Trade is temporarily unavailable because your token "
                "balance could not be verified. No trade has been placed.",
            )
            return

        token_balance = int(profile.get("token_balance", 0))
        if token_balance <= 0:
            await _send_low_token_message(
                query,
                TOKEN_EXHAUSTED_MESSAGE,
                reply_markup=TelegramKeyboard.exhausted_token_keyboard(),
            )
            return

        try:
            selected_account = await account_service.get_trade_account(
                user_id,
                account_mode=mode,
            )
            account_balance = selected_account.get("balance")
            stake_value = calculate_relative_stake(account_balance, percentage_key)
        except Exception:
            logger.exception("Could not calculate percentage stake from selected account")
            await _safe_edit_message(
                query,
                "⚠️ The selected account balance could not be verified, so no "
                "stake was calculated. Please refresh your balance and try again.",
            )
            return

        if stake_value <= 0:
            await _safe_edit_message(
                query,
                "⚠️ This percentage is below $0.01 in your current account balance, "
                "so no trade can be placed. Increase your balance or choose a "
                "higher percentage.",
            )
            return

        currency = str(selected_account.get("currency") or "USD").upper()
        if selected_symbol and selected_direction:
            setup = {
                "symbol": selected_symbol,
                "display_name": friendly_market_name(selected_symbol),
                "direction": selected_direction,
            }
        else:
            try:
                setup = await asyncio.to_thread(
                    build_auto_trade_setup,
                    user_id,
                )
            except Exception:
                logger.exception(
                    "Could not retrieve Deriv data for stake selection"
                )
                await _safe_edit_message(
                    query,
                    "⚠️ Live Deriv market data is temporarily unavailable. "
                    "No trade has been placed. Please try again shortly.",
                )
                return

            if (
                setup.get("symbol") == "NO_SETUP"
                or setup.get("direction") not in {"BULLISH", "BEARISH"}
            ):
                await query.edit_message_text(
                    "⚠️ No tradable setup is currently available.\n\n"
                    "No trade has been placed."
                )
                return

        await _prepare_trade_confirmation(
            update,
            query,
            setup=setup,
            stake_value=stake_value,
            currency=currency,
            account_mode=mode,
            token_balance=token_balance,
            stake_description=(
                f" ({float(RELATIVE_STAKES[percentage_key] * 100):g}% of "
                f"{float(account_balance):,.2f} {currency} balance)"
            ),
            stake_percentage_key=percentage_key,
            account_balance=account_balance,
        )
        return

    await query.edit_message_text(
        handle_start(user_id),
        reply_markup=await _dashboard_keyboard_with_limit(),
    )