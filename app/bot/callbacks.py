from __future__ import annotations

import asyncio
import logging
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
LOW_TOKEN_WARNING_IMAGE_URL = (
    "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
    "Bot%20images/Low%20Tokens%20Warning%20Alert.png"
)
RELATIVE_STAKES = {
    "0_1": Decimal("0.001"),
    "0_5": Decimal("0.005"),
    "1": Decimal("0.01"),
}


def calculate_relative_stake(account_balance: object, percentage_key: str) -> int:
    percentage = RELATIVE_STAKES.get(percentage_key)
    if percentage is None:
        raise ValueError("UNSUPPORTED_STAKE_PERCENTAGE")
    try:
        balance = Decimal(str(account_balance))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("INVALID_ACCOUNT_BALANCE") from exc
    if not balance.is_finite() or balance <= 0:
        raise ValueError("INVALID_ACCOUNT_BALANCE")
    return int((balance * percentage).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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
    try:
        balances = await asyncio.wait_for(
            account_service.get_live_balances(user_id),
            timeout=15,
        )
        await asyncio.to_thread(
            trade_history_service.update_balance_snapshots,
            user_id,
            balances,
        )
        account_balance = (balances.get(mode) or {})
        if account_balance.get("available") and account_balance.get("balance") is not None:
            currency = str(account_balance.get("currency") or "USD").upper()
            balance_text = f"{float(account_balance['balance']):,.2f} {currency}"
    except Exception:
        logger.warning("Could not fetch final Deriv balance for session receipt")

    token_balance_text = "Unavailable"
    try:
        profile = await asyncio.to_thread(
            trade_history_service.get_user,
            user_id,
        )
        token_balance_text = str(int(profile.get("token_balance", 0)))
    except Exception:
        logger.warning("Could not fetch remaining tokens for session receipt")

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

    await _safe_edit_message(query, text, **kwargs)


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
                TelegramKeyboard.dashboard_keyboard()
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
            TelegramKeyboard.dashboard_keyboard()
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
            "💎 You’re out of Yields Tokens, so Auto Trade can’t start. "
            "No trade has been placed. Please contact support to top up.",
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
        except Exception as exc:
            state.status = "rejected"
            logger.exception("Could not reserve a Yields Token before purchase")
            return {"status": "token_service_error", "reason": str(exc)}

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
    )


async def _run_auto_trade(update, state) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    settled_contracts: list[dict] = []

    while True:
        current = auto_trade_service.get_run(user_id)

        if current is None:
            await _safe_edit_message(query, "⚠️ Auto Trade state was not found.")
            return

        if current.status == "cancelled":
            await _save_session_summary(current, "cancelled")
            await _safe_edit_message(
                query,
                await _session_end_message(user_id, current, "Cancelled by you."),
            )
            return

        if current.status == "completed":
            await _save_session_summary(current, "completed")
            await _safe_edit_message(
                query,
                await _session_end_message(user_id, current),
            )
            return

        if current.status not in {"running", "monitoring"}:
            await _save_session_summary(current, "failed")
            await _safe_edit_message(
                query,
                await _session_end_message(
                    user_id,
                    current,
                    f"Auto Trade stopped safely ({current.status.replace('_', ' ')}).",
                ),
            )
            return

        if current.status == "monitoring":
            try:
                settlement = await _monitor_settlement(user_id, current)
            except Exception as exc:
                current = auto_trade_service.finish_run(user_id, "monitoring_error") or current
                await _save_session_summary(current, "failed")
                await _safe_edit_message(
                    query,
                    await _session_end_message(
                        user_id,
                        current,
                        "Settlement could not be verified. No further trade was placed.",
                    ),
                )
                return

            try:
                profit = float(settlement.get("profit") or 0.0)
            except (TypeError, ValueError):
                current = auto_trade_service.finish_run(user_id, "settlement_data_error") or current
                await _save_session_summary(current, "failed")
                await _safe_edit_message(
                    query,
                    await _session_end_message(
                        user_id,
                        current,
                        "Settlement data could not be validated. No further trade was placed.",
                    ),
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

            settled_contracts.append(settlement)
            current = auto_trade_service.advance_after_result(
                user_id,
                won=won,
                profit=profit,
            )
            await _save_session_summary(
                current,
                "completed" if current.status == "completed" else "running",
            )

            market_label = friendly_market_name(current.market_symbol or current.symbol)
            history_warning = (
                "⚠️ This settled trade could not be saved to Stats.\n\n"
                if not history_saved
                else ""
            )
            await _safe_edit_message(
                query,
                "🤖 AUTO TRADE RUNNING\n\n"
                f"Account mode: {current.account_mode.upper()}\n"
                f"Market: {market_label}\n"
                f"Direction: {current.direction}\n"
                f"Last result: {'WIN' if won else 'LOSS'}\n"
                f"Last contract P/L: ${profit:.2f}\n\n"
                f"Winning cycles: {current.wins}/{current.max_cycles}\n"
                f"Current cycle: {current.cycle}/{current.max_cycles}\n"
                f"Next stake: ${current.current_stake:.2f}\n"
                f"Settled contracts: {current.trade_count}\n"
                f"Total staked: ${current.total_staked:.2f}\n"
                f"Net profit/loss: ${current.total_profit_loss:.2f}\n\n"
                f"{history_warning}"
                "The selected market and direction remain fixed."
            )

            if current.status != "running":
                continue

            continue

        # Check the selected mode again before every purchase.
        if account_mode_service.get_mode(user_id) != current.account_mode:
            current = auto_trade_service.finish_run(user_id, "account_mode_changed") or current
            await _save_session_summary(current, "stopped")
            await _safe_edit_message(
                query,
                await _session_end_message(
                    user_id,
                    current,
                    "Account mode changed. No further purchase was attempted.",
                ),
            )
            return

        result = await execute_trade(user_id, current)

        if result.get("status") != "accepted":
            status = result.get("status")
            reason = result.get("reason", "Unknown error")

            auto_trade_service.finish_run(
                user_id,
                "unknown_buy_outcome" if status == "unknown_buy_outcome" else "rejected",
            )

            current = auto_trade_service.get_run(user_id) or current
            status_reason = {
                "insufficient_tokens": (
                    "You’re out of Yields Tokens. No token was charged for this attempt."
                ),
                "token_service_error": (
                    "Your token balance could not be verified. No trade was attempted."
                ),
                "unknown_buy_outcome": (
                    "The purchase outcome is uncertain. Auto Trade stopped to avoid a duplicate trade."
                ),
            }.get(status, "A trade could not be placed, so Auto Trade stopped safely.")
            await _save_session_summary(
                current,
                "failed" if status in {"unknown_buy_outcome", "token_service_error"} else "stopped",
            )
            session_message = await _session_end_message(user_id, current, status_reason)
            if status == "insufficient_tokens":
                await _send_low_token_message(query, session_message)
            else:
                await _safe_edit_message(query, session_message)
            return

        market_label = friendly_market_name(current.market_symbol or current.symbol)
        await _safe_edit_message(
            query,
            "⏳ CONTRACT PURCHASED\n\n"
            f"Account mode: {current.account_mode.upper()}\n"
            f"Market: {market_label}\n"
            f"Direction: {current.direction}\n"
            f"Stake: ${current.current_stake:.2f}\n"
            f"Contract ID: {current.contract_id}\n\n"
            f"Waiting for the 30-second contract to settle.\n"
            f"Winning cycles: {current.wins}/{current.max_cycles}"
        )


async def dispatch_callback(update, context) -> None:
    if update.callback_query is None:
        return

    query = update.callback_query
    await query.answer()

    data = query.data or ""
    user_id = update.effective_user.id
    user_key = str(user_id)

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
            "• Contract results are monitored before the next trade\n"
            "• The run stops after five winning cycles or a safety stop"
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
            reply_markup=TelegramKeyboard.dashboard_keyboard(),
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
            reply_markup=TelegramKeyboard.dashboard_keyboard(),
        )
        return

    if data == "MENU_FAQ":
        await _safe_edit_message(
            query,
            handle_faq(),
            reply_markup=TelegramKeyboard.menu_keyboard(settings.community_url),
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
            TelegramKeyboard.menu_keyboard(settings.community_url)
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
        try:
            profile = await asyncio.to_thread(
                trade_history_service.get_user,
                user_id,
            )
            token_balance = int(profile.get("token_balance", 0))
        except Exception:
            token_balance = None
        balance_text = str(token_balance) if token_balance is not None else "Unavailable"
        await _safe_edit_message(
            query,
            "💎 YIELDS TOKENS\n\n"
            f"Available balance: {balance_text}\n\n"
            "One token is used for each contract placed by Auto Trade.\n"
            "For a token top-up, contact Sammie @samueludoh_ng.",
            reply_markup=TelegramKeyboard.menu_keyboard(settings.community_url),
        )
        return

    if data == "MENU_INVITE":
        bot_username = str(settings.telegram_bot_username or "").strip().lstrip("@")
        invite_text = (
            f"Invite a friend to Yields: https://t.me/{bot_username}"
            if bot_username
            else "Open Yields in Telegram, tap the bot profile, and share it with a friend."
        )
        await _safe_edit_message(
            query,
            f"🎁 INVITE FRIENDS\n\n{invite_text}",
            reply_markup=TelegramKeyboard.menu_keyboard(settings.community_url),
        )
        return

    if data == "START_STATS":
        await query.edit_message_text(
            await handle_stats(user_id),
            reply_markup=TelegramKeyboard.dashboard_keyboard(),
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

    if data == "AUTO_TRADE_START_SCAN":
        await _handle_auto_trade_entry(update, query)
        return

    if data == "AUTO_TRADE_TRADE_ANYWAY":
        await _handle_auto_trade_entry(update, query, allow_low_balance=True)
        return

    if data == "AUTO_TRADE_CANCEL":
        state = auto_trade_service.cancel_run(user_id)
        if state is None:
            await _safe_edit_message(
                query,
                "🛑 Auto Trade cancelled. No trade has been placed."
            )
        else:
            await _save_session_summary(state, "cancelled")
            await _safe_edit_message(
                query,
                await _session_end_message(
                    user_id,
                    state,
                    "Cancelled by you. An already purchased contract was allowed to settle.",
                )
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
                refreshed_stake = calculate_relative_stake(
                    selected_account.get("balance"),
                    state.stake_percentage_key,
                )
                if refreshed_stake < 1:
                    await _safe_edit_message(
                        query,
                        "⚠️ This percentage rounds to 0 in your current account "
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
                    "💎 You’re out of Yields Tokens, so Auto Trade can’t start. "
                    "No trade has been placed. Please contact support to top up.",
                )
                return

            await asyncio.to_thread(
                trade_history_service.create_session,
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
            await _safe_edit_message(
                query,
                f"⚠️ Auto Trade could not start: {exc}"
            )
            return

        await _run_auto_trade(update, state)
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
                "💎 You’re out of Yields Tokens, so Auto Trade can’t start. "
                "No trade has been placed. Please contact support to top up.",
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

        if stake_value < 1:
            await _safe_edit_message(
                query,
                "⚠️ This percentage rounds to 0 in your current account balance, "
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

        if auto_trade_service.is_running(user_id):
            await query.edit_message_text(
                "⚠️ An Auto Trade run is already active."
            )
            return

        try:
            auto_trade_service.start_run(
                user_id=user_id,
                symbol=setup["symbol"],
                direction=setup["direction"],
                base_stake=stake_value,
                account_mode=mode,
            )
            state = auto_trade_service.get_run(user_id)
            if state is not None:
                state.stake_percentage_key = percentage_key
                state.currency = currency
        except (RuntimeError, ValueError) as exc:
            await query.edit_message_text(
                f"⚠️ Unable to prepare Auto Trade: {exc}"
            )
            return

        await _safe_edit_message(
            query,
            "⚡ TRADE CONFIRMATION\n\n"
            f"Market: {setup.get('display_name', setup['symbol'])}\n"
            f"Direction: {setup['direction']}\n"
            f"Account mode: {mode.upper()}\n"
            f"Initial stake: {stake_value:,} {currency} "
            f"({float(RELATIVE_STAKES[percentage_key] * 100):g}% of "
            f"{float(account_balance):,.2f} {currency} balance)\n"
            f"Available tokens: {token_balance}\n"
            "Expiry: 30 seconds\n"
            "Target: 5 winning cycles\n\n"
            "A loss stays in the current cycle and increases the next stake "
            "according to the configured martingale progression. "
            "A settled win advances the cycle and resets the stake.\n\n"
            "Confirm to begin.",
            reply_markup=TelegramKeyboard.confirmation_keyboard(),
        )
        return

    await query.edit_message_text(
        handle_start(user_id),
        reply_markup=TelegramKeyboard.dashboard_keyboard(),
    )