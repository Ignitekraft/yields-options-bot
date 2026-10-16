from __future__ import annotations

import asyncio
import html
import logging

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
logger = logging.getLogger(__name__)


def _format_currency(
    value: float | int | str | None,
    currency: str = "USD",
) -> str:
    if value is None or value == "Ready":
        return f"0.00 {currency}"

    try:
        return f"{float(value):,.2f} {currency}"
    except (TypeError, ValueError):
        return f"{value} {currency}"


def _mode_label(mode: str) -> str:
    return "REAL 🟢" if mode == "real" else "DEMO 🟠"


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
    try:
        profile = await asyncio.to_thread(
            trade_history_service.ensure_user,
            user_key,
            account_mode=mode,
        )
        await asyncio.to_thread(
            trade_history_service.update_balance_snapshots,
            user_key,
            balances,
        )
        tokens = int(profile.get("token_balance", 50))
    except Exception:
        logger.exception("Could not sync dashboard profile with Supabase")
        tokens = 0

    return _dashboard_text(
        mode,
        real_balance,
        demo_balance,
        tokens,
        display_name=display_name,
    )


async def build_menu_message(
    telegram_user_id: str | int,
    display_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
) -> str:
    user_key = str(telegram_user_id)
    mode = account_mode_service.get_mode(user_key)
    connected = bool(auth_service.get_valid_access_token(user_key))
    real_text = "Connect Deriv to view"
    demo_text = "Connect Deriv to view"
    tokens_text = "Unavailable"

    try:
        profile = await asyncio.to_thread(
            trade_history_service.ensure_user,
            user_key,
            first_name=display_name,
            last_name=last_name,
            username=username,
            account_mode=mode,
        )
        tokens_text = str(int(profile.get("token_balance", 50)))
    except Exception:
        logger.exception("Could not load menu profile from Supabase")

    if connected:
        try:
            balances = await account_service.get_live_balances(user_key)
            for account_mode, label in (("real", "real_text"), ("demo", "demo_text")):
                account = balances.get(account_mode) or {}
                value = (
                    _format_currency(account.get("balance"), account.get("currency", "USD"))
                    if account.get("available") and account.get("balance") is not None
                    else "Unavailable"
                )
                if label == "real_text":
                    real_text = value
                else:
                    demo_text = value
            await asyncio.to_thread(
                trade_history_service.update_balance_snapshots,
                user_key,
                balances,
            )
        except Exception:
            logger.exception("Could not fetch live balances for menu")
            real_text = "Unavailable"
            demo_text = "Unavailable"

    name = _display_name(display_name)
    mode_label = "REAL 🟢" if mode == "real" else "DEMO 🟠"
    demo_invite = (
        "👀 Try Auto Trade on your Deriv Demo account.\n"
        "Choose 🚀 Auto Trade to view the current setup."
        if connected
        else "🔐 Connect your Deriv account to unlock Auto Trade."
    )
    return (
        f"Hi {name},\n\n"
        f"🟠 Trading mode: {mode_label}\n\n"
        f"💰 Real Balance: {real_text}\n"
        f"🎮 Demo Balance: {demo_text}\n"
        f"💎 Available Tokens: {tokens_text}\n\n"
        "💬 Need help? Ask Sammie @samueludoh_ng\n\n"
        f"{demo_invite}"
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
        "� Demo uses your Demo account.\n"
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
            "indicators": {},
            "current_price": 0.0,
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
                "indicators": {
                    "rsi": rsi,
                    "macd": macd,
                    "macd_signal": macd_signal,
                    "ma50": ma50,
                    "ma200": ma200,
                },
                "current_price": snapshot.current_price,
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
            "indicators": {},
            "current_price": 0.0,
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
        "indicators": winner["indicators"],
        "current_price": winner["current_price"],
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
    session_count = int(stats.get("session_count") or 0)
    wins = int(stats.get("wins") or 0)
    return (
        f"{icon} {label}\n"
        f"🏁 Win rate: {float(stats.get('win_rate') or 0):.1f}% "
        f"({wins}/{session_count} sessions)\n"
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
        profile = await asyncio.to_thread(
            trade_history_service.ensure_user,
            user_key,
            account_mode=account_mode_service.get_mode(user_key),
        )
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

    recent_sessions = stats.get("recent") or []
    if recent_sessions:
        recent_text = []
        for session in recent_sessions[:10]:
            profit = float(session.get("net_profit") or 0.0)
            result_icon = "✅" if profit > 0 else "❌" if profit < 0 else "➖"
            mode_label = str(session.get("account_mode") or "").upper()
            recent_text.append(
                f"🎮 {result_icon} "
                f"{_trade_market_name(str(session.get('market_symbol') or 'Market'))} "
                f"| {_format_pnl(profit)} | {mode_label}"
            )
        recent_section = "\n".join(recent_text)
    else:
        recent_section = "No completed sessions yet."

    return (
        "📊 YIELDS STATS | LAST 10 SESSIONS\n\n"
        f"{recent_section}\n\n"
        f"💎 Token balance: {int(stats.get('token_balance', profile.get('token_balance', 0)) or 0)}\n\n"
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
    mode = account_mode_service.get_mode(user_key)
    try:
        profile = await asyncio.to_thread(
            trade_history_service.ensure_user,
            user_key,
            account_mode=mode,
        )
        await asyncio.to_thread(
            trade_history_service.update_balance_snapshots,
            user_key,
            balances,
        )
        tokens = int(profile.get("token_balance", 0))
    except Exception:
        logger.exception("Could not sync account balances with Supabase")
        tokens = 0

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
    setup: dict | None = None,
) -> str:
    if not auth_service.get_valid_access_token(str(telegram_user_id)):
        return AUTO_TRADE_UNAVAILABLE_MESSAGE

    setup_data = setup if setup is not None else build_auto_trade_setup(telegram_user_id)
    direction = setup_data.get("direction", "NO_TRADE")

    indicators = setup_data.get("indicators") or {}
    current_price = setup_data.get("current_price")
    rsi = indicators.get("rsi")
    macd = indicators.get("macd")
    macd_signal = indicators.get("macd_signal")
    ma50 = indicators.get("ma50")
    ma200 = indicators.get("ma200")

    rsi_state = (
        "oversold" if rsi <= 30
        else "overbought" if rsi >= 70
        else "neutral"
    ) if rsi is not None else "unavailable"
    macd_state = (
        "bullish" if macd > macd_signal
        else "bearish" if macd < macd_signal
        else "flat"
    ) if macd is not None and macd_signal is not None else "unavailable"

    def moving_average_state(value: float | None) -> str:
        if current_price is None or value is None:
            return "unavailable"
        return (
            "price above" if current_price > value
            else "price below" if current_price < value
            else "at price"
        )

    direction_text = (
        "UP 🟢" if direction == "BULLISH"
        else "DOWN 🔴" if direction == "BEARISH"
        else "NO TRADE ⚪"
    )
    setup_title = (
        "🟢 Strong Bullish Setup Detected"
        if direction == "BULLISH"
        else "🔴 Strong Bearish Setup Detected"
        if direction == "BEARISH"
        else "⚪ No Strong Setup Detected"
    )
    indicator_report = "\n".join(
        [
            f"Trading pair: {setup_data.get('display_name', 'Current market')}",
            f"RSI: {rsi_state}",
            f"MACD: {macd_state}",
            f"MA50: {moving_average_state(ma50)}",
            f"MA200: {moving_average_state(ma200)}",
            f"Direction: {direction_text}",
        ]
    )

    return (
        f"{setup_title}\n\n"
        f"<pre>{html.escape(indicator_report)}</pre>\n\n"
        "Select trade amount 👇"
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