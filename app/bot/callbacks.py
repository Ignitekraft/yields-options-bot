from __future__ import annotations

from app.bot.handlers import (
    account_service,
    auto_trade_service,
    auth_service,
    build_auto_trade_setup,
    handle_auto_trade_start,
    handle_balance,
    handle_connect,
    handle_logout,
    handle_start,
    handle_status,
)
from app.bot.keyboards import TelegramKeyboard
from app.services.trade_execution_service import (
    TradeExecutionService,
    UnknownBuyOutcome,
)

trade_execution_service = TradeExecutionService()


async def execute_trade(user_id: str | int, state=None) -> dict:
    state = state or auto_trade_service.get_run(user_id)

    if state is None:
        return {
            "status": "rejected",
            "reason": "AUTO_TRADE_NOT_FOUND",
        }

    if (
        state.status == "executing"
        or not auto_trade_service.mark_executing(user_id)
    ):
        return {
            "status": "rejected",
            "reason": "DUPLICATE_EXECUTION",
        }

    state.status = "executing"

    try:
        user_key = str(user_id)

        access_token = auth_service.get_valid_access_token(
            user_key
        )

        if not access_token:
            state.status = "rejected"

            return {
                "status": "rejected",
                "reason": "DERIV_NOT_AUTHENTICATED",
            }

        # IMPORTANT:
        # Do not trust the stale account_id stored during OAuth.
        # Resolve the current Options account from Deriv.
        account = await account_service.get_trade_account(
            user_key
        )

        verified_account_id = account["account_id"]
        currency = account.get("currency") or "USD"

        # OTP is one-use and short-lived, so request it immediately
        # before establishing the trading WebSocket.
        ws_url = await account_service.request_account_otp_url(
            access_token=access_token,
            account_id=verified_account_id,
        )

        result = await trade_execution_service.execute_trade(
            user_id=user_id,
            market_symbol=state.symbol,
            direction=state.direction,
            stake=state.current_stake,
            account_id=verified_account_id,
            access_token=access_token,
            currency=currency,
            ws_url=ws_url,
        )

        if result.get("status") == "accepted":
            state.proposal_id = result.get(
                "proposal_id"
            )
            state.contract_id = result.get(
                "contract_id"
            )
            state.status = "running"

        elif result.get("status") == "unknown_buy_outcome":
            # NEVER automatically retry a purchase whose outcome is unknown.
            state.status = "rejected"

        else:
            state.status = "rejected"

        return result

    except UnknownBuyOutcome as exc:
        state.status = "rejected"

        return {
            "status": "unknown_buy_outcome",
            "reason": str(exc),
        }

    except Exception as exc:
        state.status = "rejected"

        return {
            "status": "rejected",
            "reason": str(exc),
        }

    finally:
        auto_trade_service.clear_execution_lock(
            user_id
        )


async def dispatch_callback(update, context) -> None:
    if update.callback_query is None:
        return

    query = update.callback_query
    await query.answer()

    data = query.data or ""
    user_id = update.effective_user.id

    if data == "START_CONNECT":
        await query.edit_message_text(
            handle_connect(user_id)
        )
        return

    if data == "START_INFO":
        await query.edit_message_text(
            "🤖 YIELDS\n\n"
            "AI-powered trading intelligence for Deriv.\n\n"
            "How it works:\n"
            "• AI ranks the 5 fixed Step Indices\n"
            "• You choose the stake\n"
            "• The app executes the deterministic Auto Trade cycle\n"
            "• Results are monitored in real time"
        )
        return

    if data == "START_BALANCE":
        await query.edit_message_text(
            await handle_balance(user_id)
        )
        return

    if data == "START_STATUS":
        await query.edit_message_text(
            handle_status(user_id)
        )
        return

    if data == "START_LOGOUT":
        await query.edit_message_text(
            handle_logout(user_id)
        )
        return

    if data == "START_AUTO_TRADE":
        await query.edit_message_text(
            handle_auto_trade_start(user_id),
            reply_markup=TelegramKeyboard.stake_keyboard(),
        )
        return

    if data == "AUTO_TRADE_START_SCAN":
        await query.edit_message_text(
            handle_auto_trade_start(user_id),
            reply_markup=TelegramKeyboard.stake_keyboard(),
        )
        return

    if data == "AUTO_TRADE_CANCEL":
        await query.edit_message_text(
            "🛑 Auto Trade cancelled. No trade has been placed."
        )
        return

    if data == "AUTO_TRADE_CONFIRM":
        state = auto_trade_service.get_run(user_id)

        if state is None:
            await query.edit_message_text(
                "⚠️ No active Auto Trade session is available to confirm."
            )
            return

        result = await execute_trade(
            user_id,
            state,
        )

        if result.get("status") == "accepted":
            await query.edit_message_text(
                "✅ Trade confirmed and submitted.\n\n"
                f"Market: {state.symbol}\n"
                f"Direction: {state.direction}\n"
                f"Stake: ${state.current_stake:.2f}\n"
                f"Contract ID: {result.get('contract_id', 'pending')}\n"
                f"Proposal ID: {result.get('proposal_id', 'pending')}"
            )
            return

        await query.edit_message_text(
            "⚠️ Trade was not placed.\n\n"
            f"Reason: {result.get('reason', 'Unknown error')}"
        )
        return

    if data.startswith("AUTO_TRADE_STAKE_"):
        try:
            stake_value = float(
                data.replace(
                    "AUTO_TRADE_STAKE_",
                    "",
                ).split("_")[0]
                or 1
            )
        except ValueError:
            stake_value = 1.0

        setup = build_auto_trade_setup(user_id)

        try:
            auto_trade_service.start_run(
                user_id,
                setup.get("symbol", "step_100"),
                setup.get("direction", "BULLISH"),
                stake_value,
            )

        except RuntimeError:
            existing = auto_trade_service.get_run(user_id)

            if existing is not None:
                existing.current_stake = stake_value
                existing.direction = setup.get(
                    "direction",
                    existing.direction,
                )
                existing.symbol = setup.get(
                    "symbol",
                    existing.symbol,
                )

        await query.edit_message_text(
            f"⚡ TRADE CONFIRMATION\n\n"
            f"Stake selected: ${stake_value:.2f}.\n\n"
            f"The cycle begins in the analyzed direction: "
            f"{setup.get('direction', 'BULLISH')}.\n"
            f"All 5 trades in this session will remain in that direction.\n\n"
            f"Expiry: 30 seconds\n"
            f"Cycles: 5",
            reply_markup=TelegramKeyboard.confirmation_keyboard(),
        )
        return

    await query.edit_message_text(
        handle_start(user_id)
    )