from __future__ import annotations

import asyncio
import logging
from pathlib import Path

try:
    from telegram import InputMediaPhoto
except ImportError:  # pragma: no cover
    InputMediaPhoto = None

from app.bot.handlers import (
    account_mode_service,
    account_service,
    auth_service,
    auto_trade_service,
    build_auto_trade_setup,
    build_start_message,
    handle_account_mode_settings,
    handle_auto_trade_start,
    handle_balance,
    handle_connect,
    handle_faq,
    handle_logout,
    handle_start,
    handle_stats,
    handle_status,
)
from app.bot.keyboards import TelegramKeyboard
from app.services.trade_execution_service import (
    TradeExecutionService,
    UnknownBuyOutcome,
)
from app.services.trade_history_service import TradeHistoryService

trade_execution_service = TradeExecutionService()
trade_history_service = TradeHistoryService()
logger = logging.getLogger(__name__)


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


async def _require_trade_tokens(update, query, account_mode: str) -> bool:
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
        return False

    if int(profile.get("token_balance", 0)) <= 0:
        await _safe_edit_message(
            query,
            "💎 You’re out of Yields Tokens, so Auto Trade can’t start. "
            "No trade has been placed. Please contact support to top up.",
        )
        return False
    return True

def _setup_photo_path(direction: str | None) -> str | None:
    direction_value = str(direction or "").strip().upper()
    asset_dir = Path(__file__).resolve().parent.parent / "assets" / "trade_signals"

    if direction_value == "BULLISH":
        path = asset_dir / "bullish_setup.png"
    elif direction_value == "BEARISH":
        path = asset_dir / "bearish_setup.png"
    else:
        path = None

    if path is not None and path.exists():
        return str(path)
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
            await _safe_edit_message(query, "🛑 Auto Trade cancelled.")
            return

        if current.status == "completed":
            await _save_session_summary(current, "completed")
            await _safe_edit_message(
                query,
                "🏁 AUTO TRADE COMPLETE\n\n"
                f"Winning cycles: {current.wins}/{current.max_cycles}\n"
                f"Settled contracts: {current.trade_count}\n"
                f"Total staked: ${current.total_staked:.2f}\n"
                f"Net profit/loss: ${current.total_profit_loss:.2f}\n"
                f"Account mode: {current.account_mode.upper()}"
            )
            return

        if current.status not in {"running", "monitoring"}:
            await _save_session_summary(current, "failed")
            await _safe_edit_message(
                query,
                "⚠️ Auto Trade stopped.\n\n"
                f"State: {current.status}"
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
                    "⚠️ CONTRACT MONITORING STOPPED\n\n"
                    "The contract was purchased, but settlement could not be "
                    "verified. The bot will not place another trade.\n\n"
                    f"Details: {exc}"
                )
                return

            try:
                profit = float(settlement.get("profit") or 0.0)
            except (TypeError, ValueError):
                current = auto_trade_service.finish_run(user_id, "settlement_data_error") or current
                await _save_session_summary(current, "failed")
                await _safe_edit_message(
                    query,
                    "⚠️ Settlement returned an invalid profit value. "
                    "Auto Trade has stopped without placing another trade."
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
                "🛑 Account mode changed during Auto Trade. "
                "No further purchase will be attempted."
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
            await _save_session_summary(
                current,
                "failed" if status == "unknown_buy_outcome" else "stopped",
            )

            if status == "insufficient_tokens":
                message = (
                    "💎 You’re out of Yields Tokens, so Auto Trade has stopped "
                    "before placing another trade. No token was charged for this attempt.\n\n"
                )
            elif status == "token_service_error":
                message = (
                    "⚠️ Auto Trade is temporarily unavailable because your token balance "
                    "could not be verified. No trade was attempted. Please try again shortly.\n\n"
                )
            elif status == "unknown_buy_outcome":
                message = (
                    "⚠️ PURCHASE OUTCOME UNKNOWN\n\n"
                    "The purchase may have succeeded, but confirmation was not "
                    "received. Auto Trade has stopped to avoid a duplicate buy.\n\n"
                )
            else:
                message = "⚠️ TRADE NOT PLACED\n\nAuto Trade has stopped.\n\n"

            await _safe_edit_message(query, message + f"Reason: {reason}")
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
        await query.edit_message_text(
            handle_connect(user_id),
            reply_markup=TelegramKeyboard.connect_keyboard(),
        )
        return

    if data == "START_INFO":
        await query.edit_message_text(
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
        if not await _require_trade_tokens(
            update,
            query,
            account_mode_service.get_mode(user_id),
        ):
            return
        display_name = getattr(update.effective_user, "first_name", None) or getattr(update.effective_user, "username", None) or "friend"
        text = handle_auto_trade_start(user_id, display_name=display_name)
        setup = build_auto_trade_setup(user_id)
        photo_path = _setup_photo_path(setup.get("direction"))

        if InputMediaPhoto is not None and photo_path:
            try:
                with open(photo_path, "rb") as photo_file:
                    await query.edit_message_media(
                        media=InputMediaPhoto(media=photo_file, caption=text),
                        reply_markup=TelegramKeyboard.stake_keyboard(),
                    )
                return
            except Exception:
                pass

        await query.edit_message_text(text, reply_markup=TelegramKeyboard.stake_keyboard())
        return

    if data == "AUTO_TRADE_START_SCAN":
        if not await _require_trade_tokens(
            update,
            query,
            account_mode_service.get_mode(user_id),
        ):
            return
        display_name = getattr(update.effective_user, "first_name", None) or getattr(update.effective_user, "username", None) or "friend"
        text = handle_auto_trade_start(user_id, display_name=display_name)
        setup = build_auto_trade_setup(user_id)
        photo_path = _setup_photo_path(setup.get("direction"))

        if InputMediaPhoto is not None and photo_path:
            try:
                with open(photo_path, "rb") as photo_file:
                    await query.edit_message_media(
                        media=InputMediaPhoto(media=photo_file, caption=text),
                        reply_markup=TelegramKeyboard.stake_keyboard(),
                    )
                return
            except Exception:
                pass

        await query.edit_message_text(text, reply_markup=TelegramKeyboard.stake_keyboard())
        return

    if data == "AUTO_TRADE_CANCEL":
        state = auto_trade_service.cancel_run(user_id)
        if state is None:
            await query.edit_message_text(
                "🛑 Auto Trade cancelled. No trade has been placed."
            )
        else:
            await _save_session_summary(state, "cancelled")
            await query.edit_message_text(
                "🛑 Auto Trade cancellation requested.\n\n"
                "If a contract has already been purchased, it cannot be "
                "cancelled by this button; the bot will not initiate another "
                "purchase after the current monitoring step."
            )
        return

    if data == "AUTO_TRADE_CONFIRM":
        state = auto_trade_service.get_run(user_id)

        if state is None:
            await _safe_edit_message(
                query,
                "⚠️ No active Auto Trade session is available to confirm."
            )
            return

        effective_user = update.effective_user
        try:
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
                await _safe_edit_message(
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
        raw_stake = data.removeprefix("AUTO_TRADE_STAKE_")

        try:
            stake_value = float(raw_stake)
        except (TypeError, ValueError):
            await query.edit_message_text(
                "⚠️ Invalid stake selection. Choose one of the available stake buttons."
            )
            return

        if stake_value not in {1.0, 3.0, 5.0, 10.0, 30.0, 100.0, 300.0, 500.0}:
            await query.edit_message_text(
                "⚠️ Unsupported stake selection."
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
            await _safe_edit_message(
                query,
                "💎 You’re out of Yields Tokens, so Auto Trade can’t start. "
                "No trade has been placed. Please contact support to top up.",
            )
            return

        setup = build_auto_trade_setup(user_id)

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
            f"Initial stake: ${stake_value:.2f}\n"
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