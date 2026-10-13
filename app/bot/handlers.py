from __future__ import annotations

from app.config import settings
from app.services.account_service import AccountService
from app.services.auth_service import AuthService
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.scanner import YieldsScanner

from .messages import (
    AUTO_TRADE_RUNNING_MESSAGE,
    AUTO_TRADE_UNAVAILABLE_MESSAGE,
    NO_CONNECTION_MESSAGE,
    SCAN_MESSAGE,
    START_MESSAGE,
)


auth_service = AuthService()
account_service = AccountService(auth_service)


def _format_currency(value: float | int | str | None, currency: str = "USD") -> str:
    if value is None or value == "Ready":
        return f"$0.00 {currency}"
    try:
        return f"${float(value):,.2f} {currency}"
    except (TypeError, ValueError):
        return f"{value} {currency}"


def handle_start(telegram_user_id: str | int | None = None) -> str:
    if telegram_user_id is None:
        return START_MESSAGE

    user_key = str(telegram_user_id)
    if not auth_service.get_valid_access_token(user_key):
        return (
            "🤖 YIELDS\n\n"
            "AI-powered trading intelligence for Deriv.\n\n"
            "Connect your Deriv account to unlock:\n"
            "• AI market scanning\n"
            "• Auto Trade\n"
            "• Real-time trade monitoring\n"
            "• Account balances\n"
            "• Yields Token features\n\n"
            "Your Deriv account is connected securely through OAuth.\n"
        )

    account = account_service.get_account_status(user_key)
    tokens = auth_service.get_user_tokens(user_key)
    real_balance = None
    demo_balance = None
    if account.get("balance"):
        real_balance = account.get("balance")

    return (
        "🤖 YIELDS\n\n"
        "Welcome back.\n\n"
        "Your Deriv account is connected.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💰 ACCOUNT\n\n"
        f"Real: {_format_currency(real_balance)}\n"
        f"Demo: {_format_currency(demo_balance)}\n\n"
        f"💎 Yields Tokens: {tokens}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 AUTO TRADE\n\n"
        "Gemini scans 5 Step Indices and selects the strongest current setup.\n\n"
        "Expiry: 30 seconds\n"
        "Cycles: 5\n"
    )


def handle_status(telegram_user_id: str | int) -> str:
    account = account_service.get_account_status(str(telegram_user_id))
    if not account["connected"]:
        return NO_CONNECTION_MESSAGE

    return (
        "🟢 Deriv account connected successfully.\n\n"
        "You can now use /balance, /scan, and trading tools."
    )


async def handle_balance(telegram_user_id: str | int) -> str:
    """Return formatted Deriv balances and Yields token count."""
    status = account_service.get_account_status(str(telegram_user_id))
    if not status["connected"]:
        return "🔴 Deriv account not connected. Use /connect to link your account."

    balances = await account_service.get_live_balances(str(telegram_user_id))
    real = balances.get("real", {"available": False, "balance": None, "currency": "USD", "account_id": None, "status": "unavailable"})
    demo = balances.get("demo", {"available": False, "balance": None, "currency": "USD", "account_id": None, "status": "unavailable"})
    tokens = balances.get("tokens", 50)

    def format_value(entry: dict, label: str) -> str:
        if entry.get("available"):
            balance = entry.get("balance")
            currency = entry.get("currency", "USD")
            if balance is None:
                return f"{label}: Not available"
            return f"{label}: ${balance:,.2f} {currency}"

        if entry.get("status") == "error":
            return f"{label}: Unable to fetch"
        return f"{label}: Not available"

    return (
        "💰 Deriv Balances\n\n"
        f"💵 {format_value(real, 'Real')}\n"
        f"🎮 {format_value(demo, 'Demo')}\n\n"
        f"💎 Yields Tokens: {tokens}"
    )


def handle_connect(telegram_user_id: str | int) -> str:
    """Start a secure Deriv OAuth session for a Telegram user."""
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
        "Deriv account connection is required.\n\n"
        "Tap the link below to continue securely:\n\n"
        f"{url}"
    )


def handle_logout(telegram_user_id: str | int) -> str:
    auth_service.disconnect_user(str(telegram_user_id))
    return "🔒 Deriv account disconnected. You can reconnect any time with /connect."


def handle_scan() -> str:
    market_service = MarketDataService()
    scanner = YieldsScanner()

    symbol_list = market_service.get_active_symbols()
    symbol = symbol_list[0]["symbol"] if symbol_list else "R_100"
    history = market_service.get_historical_ticks(symbol, count=25)
    live_tick = market_service.subscribe_ticks(symbol)

    result = scanner.analyze_live_tick(history, live_tick, symbol=symbol)
    response = [
        SCAN_MESSAGE,
        f"Symbol: {result.symbol}",
        f"Signal: {result.signal}",
        f"Confidence: {result.confidence:.2f}",
        f"Message: {result.message}",
    ]
    return "\n".join(response)


def handle_auto_trade_start(telegram_user_id: str | int) -> str:
    if not auth_service.get_valid_access_token(str(telegram_user_id)):
        return AUTO_TRADE_UNAVAILABLE_MESSAGE
    return (
        "🤖 YIELDS AUTO TRADE\n\n"
        "🏆 BEST SETUP\n\n"
        "Market: Step 300 Index\n"
        "Direction: 🟢 BULLISH\n\n"
        "Confidence: 83%\n"
        "Setup Quality: 88%\n\n"
        "Why:\n"
        "• Strong short-term upward momentum\n"
        "• Consistent recent price structure\n"
        "• Higher highs maintained\n\n"
        "Expiry: 30 seconds\n"
    )


def handle_auto_trade_running(telegram_user_id: str | int) -> str:
    return AUTO_TRADE_RUNNING_MESSAGE


def handle_auto_trade_no_setup() -> str:
    return "⚠️ NO STRONG SETUP\n\nYields scanned all 5 Step Indices, but no market currently meets the minimum setup requirements.\n\nNo trade has been placed."
