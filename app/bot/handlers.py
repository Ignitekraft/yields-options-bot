from __future__ import annotations

from app.config import settings
from app.services.account_service import AccountService
from app.services.auth_service import AuthService
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.scanner import YieldsScanner

from .messages import NO_CONNECTION_MESSAGE, SCAN_MESSAGE, START_MESSAGE


auth_service = AuthService()
account_service = AccountService(auth_service)


def handle_start() -> str:
    return START_MESSAGE


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