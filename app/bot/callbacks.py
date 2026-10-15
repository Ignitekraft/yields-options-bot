from __future__ import annotations

import asyncio

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

trade_execution_service = TradeExecutionService()


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

        return result

    except UnknownBuyOutcome as exc:
        state.status = "unknown_buy_outcome"
        return {"status": "unknown_buy_outcome", "reason": str(exc)}

    except Exception as exc:
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
            await query.edit_message_text("⚠️ Auto Trade state was not found.")
            return

        if current.status == "cancelled":
            await query.edit_message_text("🛑 Auto Trade cancelled.")
            return

        if current.status == "completed":
            await query.edit_message_text(
                "🏁 AUTO TRADE COMPLETE\n\n"
                f"Winning cycles: {current.wins}/{current.max_cycles}\n"
                f"Settled contracts: {current.trade_count}\n"
                f"Total staked: ${current.total_staked:.2f}\n"
                f"Net profit/loss: ${current.total_profit_loss:.2f}\n"
                f"Account mode: {current.account_mode.upper()}"
            )
            return

        if current.status not in {"running", "monitoring"}:
            await query.edit_message_text(
                "⚠️ Auto Trade stopped.\n\n"
                f"State: {current.status}"
            )
            return

        if current.status == "monitoring":
            try:
                settlement = await _monitor_settlement(user_id, current)
            except Exception as exc:
                auto_trade_service.finish_run(user_id, "monitoring_error")
                await query.edit_message_text(
                    "⚠️ CONTRACT MONITORING STOPPED\n\n"
                    "The contract was purchased, but settlement could not be "
                    "verified. The bot will not place another trade.\n\n"
                    f"Details: {exc}"
                )
                return

            try:
                profit = float(settlement.get("profit") or 0.0)
            except (TypeError, ValueError):
                auto_trade_service.finish_run(user_id, "settlement_data_error")
                await query.edit_message_text(
                    "⚠️ Settlement returned an invalid profit value. "
                    "Auto Trade has stopped without placing another trade."
                )
                return

            won = profit > 0
            settled_contracts.append(settlement)
            current = auto_trade_service.advance_after_result(
                user_id,
                won=won,
                profit=profit,
            )

            market_label = friendly_market_name(current.market_symbol or current.symbol)
            await query.edit_message_text(
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
                "The selected market and direction remain fixed."
            )

            if current.status != "running":
                continue

            continue

        # Check the selected mode again before every purchase.
        if account_mode_service.get_mode(user_id) != current.account_mode:
            auto_trade_service.finish_run(user_id, "account_mode_changed")
            await query.edit_message_text(
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

            if status == "unknown_buy_outcome":
                message = (
                    "⚠️ PURCHASE OUTCOME UNKNOWN\n\n"
                    "The purchase may have succeeded, but confirmation was not "
                    "received. Auto Trade has stopped to avoid a duplicate buy.\n\n"
                )
            else:
                message = "⚠️ TRADE NOT PLACED\n\nAuto Trade has stopped.\n\n"

            await query.edit_message_text(message + f"Reason: {reason}")
            return

        market_label = friendly_market_name(current.market_symbol or current.symbol)
        await query.edit_message_text(
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
            handle_stats(user_id),
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
        await query.edit_message_text(
            "🟡 Demo Mode selected.\n\n"
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
        display_name = getattr(update.effective_user, "first_name", None) or getattr(update.effective_user, "username", None) or "friend"
        await query.edit_message_text(
            handle_auto_trade_start(user_id, display_name=display_name),
            reply_markup=TelegramKeyboard.stake_keyboard(),
        )
        return

    if data == "AUTO_TRADE_START_SCAN":
        display_name = getattr(update.effective_user, "first_name", None) or getattr(update.effective_user, "username", None) or "friend"
        await query.edit_message_text(
            handle_auto_trade_start(user_id, display_name=display_name),
            reply_markup=TelegramKeyboard.stake_keyboard(),
        )
        return

    if data == "AUTO_TRADE_CANCEL":
        state = auto_trade_service.cancel_run(user_id)
        if state is None:
            await query.edit_message_text(
                "🛑 Auto Trade cancelled. No trade has been placed."
            )
        else:
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
            await query.edit_message_text(
                "⚠️ No active Auto Trade session is available to confirm."
            )
            return

        try:
            auto_trade_service.confirm_run(user_id)
        except RuntimeError as exc:
            await query.edit_message_text(
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

        if stake_value not in {1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0, 100.0, 200.0, 300.0, 500.0}:
            await query.edit_message_text(
                "⚠️ Unsupported stake selection."
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

        mode = account_mode_service.get_mode(user_id)

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

        await query.edit_message_text(
            "⚡ TRADE CONFIRMATION\n\n"
            f"Market: {setup.get('display_name', setup['symbol'])}\n"
            f"Direction: {setup['direction']}\n"
            f"Account mode: {mode.upper()}\n"
            f"Initial stake: ${stake_value:.2f}\n"
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