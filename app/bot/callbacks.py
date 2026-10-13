from __future__ import annotations

from app.bot.handlers import (
    handle_auto_trade_start,
    handle_balance,
    handle_connect,
    handle_logout,
    handle_start,
    handle_status,
)


async def dispatch_callback(update, context) -> None:
    if update.callback_query is None:
        return

    query = update.callback_query
    await query.answer()
    data = query.data or ""

    user_id = update.effective_user.id
    if data == "START_CONNECT":
        await query.edit_message_text(handle_connect(user_id))
        return
    if data == "START_INFO":
        await query.edit_message_text(
            "🤖 YIELDS\n\nAI-powered trading intelligence for Deriv.\n\nHow it works:\n• Gemini ranks the 5 fixed Step Indices\n• You choose the stake\n• The app executes the deterministic Auto Trade cycle\n• Results are monitored in real time"
        )
        return
    if data == "START_BALANCE":
        await query.edit_message_text(await handle_balance(user_id))
        return
    if data == "START_STATUS":
        await query.edit_message_text(handle_status(user_id))
        return
    if data == "START_LOGOUT":
        await query.edit_message_text(handle_logout(user_id))
        return
    if data == "START_AUTO_TRADE":
        await query.edit_message_text(handle_auto_trade_start(user_id))
        return
    if data == "AUTO_TRADE_START_SCAN":
        await query.edit_message_text(handle_auto_trade_start(user_id))
        return
    if data == "AUTO_TRADE_CANCEL":
        await query.edit_message_text("🛑 Auto Trade cancelled. No trade has been placed.")
        return
    if data.startswith("AUTO_TRADE_STAKE_"):
        await query.edit_message_text(
            "⚡ TRADE CONFIRMATION\n\nStake selected. Please confirm to start the five-cycle run.\n\nExpiry: 30 seconds\nCycles: 5"
        )
        return
    await query.edit_message_text(handle_start(user_id))
