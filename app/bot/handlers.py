from __future__ import annotations

import asyncio

from app.config import settings
from app.services.account_service import AccountService
from app.services.account_mode_service import AccountModeService
from app.services.auth_service import AuthService
from app.services.auto_trade_service import AutoTradeService
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.scanner import YieldsScanner
from app.services.trade_history_service import TradeHistoryService

from .messages import (
    AUTO_TRADE_RUNNING_MESSAGE,
    AUTO_TRADE_UNAVAILABLE_MESSAGE,
    NO_CONNECTION_MESSAGE,
    SCAN_MESSAGE,
    START_MESSAGE,
)


auth_service = AuthService()
account_service = AccountService(auth_service)
account_mode_service = AccountModeService()
auto_trade_service = AutoTradeService()
trade_history_service = TradeHistoryService()
HELP_FOOTER = "\n\n💬 Need help with something? Ask Sammie @samueludoh_ng"


def _format_currency(
    value: float | int | str | None,
    currency: str = "USD",
) -> str:
    if value is None or value == "Ready":
        return f"$0.00 {currency}"

    try:
        return f"${float(value):,.2f} {currency}"
    except (TypeError, ValueError):
        return f"{value} {currency}"


def _mode_label(mode: str) -> str:
    return "REAL 🟢" if mode == "real" else "DEMO 🟡"


def _display_name(display_name: str | None = None) -> str:
    value = str(display_name or "").strip()
    if value:
        return value
    return "friend"


def _dashboard_text(
    mode: str,
    real_balance: Any,
    demo_balance: Any,
    tokens: int,
    display_name: str | None = None,
) -> str:
    name = _display_name(display_name)
    return (
        "🤖 YIELDS\n\n"
        f"Welcome back, {name}.\n\n"
        "Your Deriv account is connected.\n\n"
        f"Account Mode: {_mode_label(mode)}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💰 ACCOUNT\n\n"
        f"Real: {_format_currency(real_balance)}\n"
        f"Demo: {_format_currency(demo_balance)}\n\n"
        f"💎 Yields Tokens: {tokens}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 AUTO TRADE\n\n"
        "AI scans 5 Step Indices and selects the strongest current setup.\n\n"
        "Expiry: 30 seconds\n"
        "Cycles: 5"
    )


def handle_start(
    telegram_user_id: str | int | None = None,
    display_name: str | None = None,
) -> str:
    if telegram_user_id is None:
        return START_MESSAGE

    user_key = str(telegram_user_id)
    name = _display_name(display_name)
    if not auth_service.get_valid_access_token(user_key):
        return (
            "🤖 YIELDS\n\n"
            f"Hi {name},\n\n"
            "AI-powered trading intelligence for Deriv.\n\n"
            "Connect your Deriv account to unlock:\n"
            "• AI market scanning\n"
            "• Auto Trade\n"
            "• Real-time trade monitoring\n"
            "• Account balances\n"
            "• Yields Token features\n\n"
            "Your Deriv account is connected securely through OAuth."
        )

    mode = account_mode_service.get_mode(user_key)
    account = account_service.get_account_status(user_key)
    balance = account.get("balance")
    tokens = auth_service.get_user_tokens(user_key)

    return _dashboard_text(
        mode,
        balance,
        None,
        tokens,
        display_name=name,
    )


async def build_start_message(
    telegram_user_id: str | int,
    display_name: str | None = None,
) -> str:
    user_key = str(telegram_user_id)

    if not auth_service.get_valid_access_token(user_key):
        return handle_start(user_key, display_name=display_name)

    mode = account_mode_service.get_mode(user_key)
    balances = await account_service.get_live_balances(user_key)

    real_entry = balances.get("real", {})
    demo_entry = balances.get("demo", {})

    real_balance = (
        real_entry.get("balance")
        if real_entry.get("available")
        else None
    )
    demo_balance = (
        demo_entry.get("balance")
        if demo_entry.get("available")
        else None
    )
    tokens = balances.get(
        "tokens",
        auth_service.get_user_tokens(user_key),
    )

    return _dashboard_text(
        mode,
        real_balance,
        demo_balance,
        tokens,
        display_name=display_name,
    )


def handle_account_mode_settings(
    telegram_user_id: str | int,
) -> str:
    user_key = str(telegram_user_id)

    if not auth_service.get_valid_access_token(user_key):
        return "🔴 Connect your Deriv account before choosing an account mode."

    mode = account_mode_service.get_mode(user_key)

    return (
        "⚙️ ACCOUNT MODE\n\n"
        f"Selected mode: {_mode_label(mode)}\n\n"
        "Choose which Deriv Options account Yields should use for Auto Trade.\n\n"
        "🟡 Demo uses your Demo account.\n"
        "🟢 Real uses your Real account and can risk real funds.\n\n"
        "Switching to Real requires a separate confirmation. "
        "If the selected account cannot be verified, trading will stop "
        "instead of switching to another account."
    )


def build_auto_trade_setup(
    telegram_user_id: str | int,
) -> dict:
    user_key = str(telegram_user_id)

    if not auth_service.get_valid_access_token(user_key):
        return {
            "symbol": "NO_SETUP",
            "direction": "NO_TRADE",
            "confidence": 0.0,
            "setup_quality": 0.0,
            "reasoning": "Account not connected.",
        }

    service = MarketDataService()
    scanner = YieldsScanner()
    snapshots = scanner.collect_step_market_snapshots(service)
    scored = []

    for snapshot in snapshots:
        indicators = snapshot.indicators or {}
        rsi = float(indicators.get("rsi", 50.0))
        macd = float(indicators.get("macd", 0.0))
        macd_signal = float(indicators.get("macd_signal", 0.0))
        ma50 = float(indicators.get("ma50", snapshot.current_price))
        ma200 = float(indicators.get("ma200", snapshot.current_price))

        votes = (
            1 if macd > macd_signal else -1 if macd < macd_signal else 0,
            1 if rsi > 55.0 else -1 if rsi < 45.0 else 0,
            1 if snapshot.current_price > ma50 else -1 if snapshot.current_price < ma50 else 0,
            1 if snapshot.current_price > ma200 else -1 if snapshot.current_price < ma200 else 0,
        )
        directional_score = sum(votes)
        direction = (
            "BULLISH"
            if directional_score > 0
            else "BEARISH"
            if directional_score < 0
            else "NO_TRADE"
        )
        confidence = abs(directional_score) / len(votes)
        aligned_votes = sum(
            vote > 0 if direction == "BULLISH" else vote < 0
            for vote in votes
        ) if direction != "NO_TRADE" else 0
        setup_quality = aligned_votes / len(votes)

        if (
            snapshot.data_quality != "good"
            or confidence < settings.min_indicator_confidence
            or setup_quality < settings.min_setup_quality
        ):
            direction = "NO_TRADE"

        scored.append(
            {
                "symbol": snapshot.symbol,
                "display_name": snapshot.display_name,
                "direction": direction,
                "confidence": confidence,
                "setup_quality": setup_quality,
                "reasoning": (
                    f"Indicator alignment: MACD {macd:.4f} vs signal "
                    f"{macd_signal:.4f}, RSI {rsi:.1f}, current price "
                    f"{snapshot.current_price:.4f}, MA50 {ma50:.4f}, "
                    f"MA200 {ma200:.4f}."
                ),
            }
        )

    valid_setups = [item for item in scored if item["direction"] != "NO_TRADE"]
    winner = max(
        valid_setups,
        key=lambda item: (item["confidence"], item["setup_quality"]),
        default={
            "symbol": "NO_SETUP",
            "direction": "NO_TRADE",
            "confidence": 0.0,
            "setup_quality": 0.0,
            "reasoning": "No market met the minimum indicator-alignment thresholds.",
            "display_name": "No Setup",
        },
    )

    return {
        "symbol": winner["symbol"],
        "display_name": winner["display_name"],
        "direction": winner["direction"],
        "confidence": float(winner["confidence"]),
        "setup_quality": float(winner["setup_quality"]),
        "reasoning": winner["reasoning"],
    }


def handle_status(telegram_user_id: str | int) -> str:
    account = account_service.get_account_status(str(telegram_user_id))

    if not account["connected"]:
        return NO_CONNECTION_MESSAGE

    mode = account_mode_service.get_mode(telegram_user_id)

    return (
        "✅ Deriv account connected\n\n"
        "Status: active and ready\n"
        f"Preferred account mode: {_mode_label(mode)}\n\n"
        "Your balance, market scan, and trading tools are ready to use."
        f"{HELP_FOOTER}"
    )


def handle_faq() -> str:
    return (
        "❓ YIELDS FAQ\n\n"
        "• How does the AI rank the markets?\n"
        "  It scores the 5 Step Indices using trend, momentum, RSI, MACD, MA50, and MA200.\n\n"
        "• Which markets are used?\n"
        "  Only the fixed Step Index family: 100, 200, 300, 400, and 500.\n\n"
        "• What is Auto Trade?\n"
        "  It picks the strongest current setup, confirms your stake, and monitors each contract until settlement.\n\n"
        "• Can I switch between Demo and Real?\n"
        "  Yes. Use the Account Mode panel and confirm before switching.\n\n"
        "• How do I reconnect?\n"
        "  Tap the secure Connect action from the dashboard or use the connection flow again."
        f"{HELP_FOOTER}"
    )


def _format_pnl(value: float | int | str | None) -> str:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):,.2f}"


def _trade_market_name(symbol: str) -> str:
    names = {
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
    return names.get(symbol, symbol)


def _stats_mode_text(label: str, icon: str, stats: dict) -> str:
    return (
        f"{icon} {label}\n"
        f"🏁 Win rate: {float(stats.get('win_rate') or 0):.1f}%\n"
        f"🎯 Direction accuracy: {float(stats.get('accuracy') or 0):.1f}%\n"
        f"💵 Net PnL: {_format_pnl(stats.get('net_pnl'))}\n"
        f"💎 Tokens spent: {int(stats.get('tokens_spent') or 0)}"
    )


async def handle_stats(telegram_user_id: str | int) -> str:
    user_key = str(telegram_user_id)
    if not trade_history_service.is_configured:
        return (
            "📊 YIELDS TRADE STATS | LAST 10\n\n"
            "Trade history storage is not configured yet."
            f"{HELP_FOOTER}"
        )

    try:
        stats = await asyncio.to_thread(
            trade_history_service.get_user_stats,
            user_key,
        )
    except Exception:
        return (
            "📊 YIELDS TRADE STATS | LAST 10\n\n"
            "Trade history is temporarily unavailable. Please try again shortly."
            f"{HELP_FOOTER}"
        )

    recent_trades = stats.get("recent") or []
    if recent_trades:
        recent_text = []
        for trade in recent_trades[:10]:
            is_win = trade.get("outcome") == "win"
            result_icon = "✅" if is_win else "❌"
            mode_label = str(trade.get("account_mode") or "").title()
            direction_icon = "🟢" if trade.get("direction") == "BULLISH" else "🔴"
            direction_accuracy = trade.get("direction_accurate")
            accuracy_icon = (
                "✓" if direction_accuracy is True
                else "✗" if direction_accuracy is False
                else "·"
            )
            recent_text.append(
                f"🎮 {result_icon} {_trade_market_name(str(trade.get('market_symbol') or 'Market'))} "
                f"| {_format_pnl(trade.get('profit'))} | "
                f"{direction_icon} {mode_label} {accuracy_icon}"
            )
        recent_section = "\n".join(recent_text)
    else:
        recent_section = "No settled trades yet."

    return (
        "📊 YIELDS STATS | LAST 10 TRADES\n\n"
        f"{recent_section}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{_stats_mode_text('Real', '💰', stats.get('real') or {})}\n\n"
        f"{_stats_mode_text('Demo', '🎮', stats.get('demo') or {})}"
        f"{HELP_FOOTER}"
    )


async def handle_balance(telegram_user_id: str | int) -> str:
    user_key = str(telegram_user_id)
    status = account_service.get_account_status(user_key)

    if not status["connected"]:
        return (
            "🔴 Deriv account not connected\n\n"
            "Connect your account to unlock your balance and trading tools."
        )

    balances = await account_service.get_live_balances(user_key)
    real = balances.get("real", {})
    demo = balances.get("demo", {})
    tokens = balances.get("tokens", 50)

    def format_value(entry: dict, label: str) -> str:
        if entry.get("available"):
            balance = entry.get("balance")
            currency = entry.get("currency", "USD")
            if balance is None:
                return f"{label}: Balance unavailable"
            return f"{label}: {_format_currency(balance, currency)}"

        if entry.get("status") == "ambiguous":
            return f"{label}: Multiple accounts found; unable to select safely"

        if entry.get("status") == "error":
            return f"{label}: Unable to fetch"

        return f"{label}: Not available"

    mode = account_mode_service.get_mode(user_key)

    return (
        "💰 DERIV BALANCES\n\n"
        f"Account Mode: {_mode_label(mode)}\n\n"
        f"💵 {format_value(real, 'Real')}\n"
        f"🎮 {format_value(demo, 'Demo')}\n\n"
        f"💎 Yields Tokens: {tokens}"
    )


def handle_connect(telegram_user_id: str | int) -> str:
    session = auth_service.begin_oauth_session(str(telegram_user_id))
    client_id = settings.deriv_app_id or settings.deriv_client_id

    url = build_deriv_oauth_url(
        client_id,
        settings.deriv_redirect_uri,
        "trade account_manage",
        session["state"],
        session["code_challenge"],
    )

    return (
        "🔐 Secure Deriv connection\n\n"
        "Use the link below to continue safely and return to your dashboard.\n\n"
        f"{url}"
        f"{HELP_FOOTER}"
    )


def handle_logout(telegram_user_id: str | int) -> str:
    auth_service.disconnect_user(str(telegram_user_id))
    return (
        "🔒 Deriv account disconnected\n\n"
        "You can reconnect at any time from the dashboard or the Connect flow."
        f"{HELP_FOOTER}"
    )


def handle_scan() -> str:
    market_service = MarketDataService()
    scanner = YieldsScanner()

    symbol_list = market_service.get_active_symbols()
    symbol = symbol_list[0]["symbol"] if symbol_list else "STPRNG"
    history = market_service.get_historical_ticks(symbol, count=25)
    live_tick = market_service.subscribe_ticks(symbol)

    result = scanner.analyze_live_tick(history, live_tick, symbol=symbol)

    return "\n".join(
        [
            SCAN_MESSAGE,
            f"Symbol: {result.symbol}",
            f"Signal: {result.signal}",
            f"Confidence: {result.confidence:.2f}",
            f"Message: {result.message}",
        ]
    )


def handle_auto_trade_start(
    telegram_user_id: str | int,
    display_name: str | None = None,
) -> str:
    if not auth_service.get_valid_access_token(str(telegram_user_id)):
        return AUTO_TRADE_UNAVAILABLE_MESSAGE

    setup = build_auto_trade_setup(telegram_user_id)
    direction = setup.get("direction", "NO_TRADE")
    name = _display_name(display_name)

    direction_text = (
        "🟢 BULLISH"
        if direction == "BULLISH"
        else "🔴 BEARISH"
        if direction == "BEARISH"
        else "⚪ NO_TRADE"
    )

    confidence = int(float(setup.get("confidence", 0.0)) * 100)
    setup_quality = int(float(setup.get("setup_quality", 0.0)) * 100)
    mode = account_mode_service.get_mode(telegram_user_id)

    return (
        f"Hi {name},\n\n"
        "⚡ AI Auto Trade\n\n"
        "🏆 Best setup\n\n"
        f"Market: {setup.get('display_name', 'Current winner')}\n"
        f"Direction: {direction_text}\n"
        f"Confidence: {confidence}%\n"
        f"Setup quality: {setup_quality}%\n"
        f"Account mode: {_mode_label(mode)}\n\n"
        "Why this setup works:\n"
        f"• {setup.get('reasoning', 'AI trend and momentum are aligned.')}\n"
        "• AI trend, momentum, RSI, MACD, MA50, and MA200 inform the ranking.\n"
        "• Select your stake to continue.\n\n"
        "The selected market and direction will remain fixed for this run.\n\n"
        "Expiry: 30 seconds\n"
        "Target: 5 winning cycles"
        f"{HELP_FOOTER}"
    )


def handle_auto_trade_running(telegram_user_id: str | int) -> str:
    return AUTO_TRADE_RUNNING_MESSAGE


def handle_auto_trade_no_setup() -> str:
    return (
        "⚠️ NO STRONG SETUP\n\n"
        "Yields scanned the Step Indices, but no market currently meets "
        "the minimum setup requirements.\n\n"
        "No trade has been placed."
    )