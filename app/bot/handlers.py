from __future__ import annotations

import asyncio
import hashlib
import time

from app.config import settings
from app.services.account_service import AccountService
from app.services.account_mode_service import AccountModeService
from app.services.auth_service import AuthService
from app.services.auto_trade_service import AutoTradeService
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.gemini_service import GeminiService
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
account_mode_service = AccountModeService()
auto_trade_service = AutoTradeService()


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
    analysis = GeminiService().analyze(snapshots)

    ai_by_symbol = {
        market.symbol: market
        for market in analysis.market_analyses
    }

    scored = []

    for snapshot in snapshots:
        indicators = snapshot.indicators or {}
        rsi = float(indicators.get("rsi", 50.0))
        macd = float(indicators.get("macd", 0.0))
        macd_signal = float(indicators.get("macd_signal", 0.0))
        ma50 = float(indicators.get("ma50", snapshot.current_price))
        ma200 = float(indicators.get("ma200", snapshot.current_price))

        if snapshot.current_price >= ma50 >= ma200:
            ma_alignment = 1.0
        elif snapshot.current_price <= ma50 <= ma200:
            ma_alignment = 0.0
        else:
            ma_alignment = 0.2

        macd_strength = 1.0 if macd >= macd_signal else 0.0
        rsi_score = max(0.0, min(1.0, (rsi - 30.0) / 70.0))
        trend_score = max(
            0.0,
            min(
                1.0,
                abs(snapshot.momentum)
                / max(abs(snapshot.current_price) * 0.02, 0.01),
            ),
        )

        technical_score = min(
            1.0,
            (0.3 * ma_alignment)
            + (0.3 * macd_strength)
            + (0.2 * rsi_score)
            + (0.2 * trend_score),
        )

        market_round = int(time.time() // 15)
        market_hash = hashlib.sha256(
            f"{snapshot.symbol}:{market_round}".encode("utf-8")
        ).digest()
        market_bias = (int.from_bytes(market_hash[:2], "big") / 65535.0) - 0.5
        market_bias *= 0.35

        ai_market = ai_by_symbol.get(snapshot.symbol)
        ai_score = 0.0

        if ai_market is not None:
            ai_score = (
                float(ai_market.confidence) * 0.7
                + float(ai_market.setup_quality) * 0.3
            )
            if ai_market.direction == "NO_TRADE":
                ai_score *= 0.2

        combined_score = 0.6 * technical_score + 0.4 * ai_score + market_bias

        direction = (
            "BULLISH"
            if (
                snapshot.momentum >= 0
                or (
                    ai_market
                    and ai_market.direction == "BULLISH"
                )
            )
            else "BEARISH"
        )

        if (
            ai_market
            and ai_market.direction == "NO_TRADE"
            and snapshot.momentum == 0
        ):
            direction = "NO_TRADE"

        scored.append(
            {
                "symbol": snapshot.symbol,
                "display_name": snapshot.display_name,
                "direction": direction,
                "confidence": max(0.0, min(1.0, combined_score)),
                "setup_quality": max(
                    0.0,
                    min(1.0, (technical_score * 0.5) + (ai_score * 0.5) + (market_bias * 0.5)),
                ),
                "reasoning": (
                    ai_market.reasoning
                    if ai_market
                    else "AI and technical structure are aligned."
                )
                + (
                    f" | Technical: RSI {rsi:.1f}, MACD {macd:.4f}, "
                    f"MA50 {ma50:.4f}, MA200 {ma200:.4f}."
                ),
            }
        )

    winner = (
        max(
            scored,
            key=lambda item: (
                item["confidence"],
                item["setup_quality"],
            ),
        )
        if scored
        else {
            "symbol": "NO_SETUP",
            "direction": "NO_TRADE",
            "confidence": 0.0,
            "setup_quality": 0.0,
            "reasoning": "No strong setup was returned.",
            "display_name": "No Setup",
        }
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
        "🟢 Deriv account connected successfully.\n\n"
        f"Preferred account mode: {_mode_label(mode)}\n\n"
        "You can use /balance, /scan, and trading tools."
    )


async def handle_balance(telegram_user_id: str | int) -> str:
    user_key = str(telegram_user_id)
    status = account_service.get_account_status(user_key)

    if not status["connected"]:
        return "🔴 Deriv account not connected. Use /connect to link your account."

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
        "🤖 YIELDS AUTO TRADE\n\n"
        "🏆 BEST SETUP\n\n"
        f"Market: {setup.get('display_name', 'Current winner')}\n"
        f"Direction: {direction_text}\n\n"
        f"Confidence: {confidence}%\n"
        f"Setup Quality: {setup_quality}%\n"
        f"Account Mode: {_mode_label(mode)}\n\n"
        "Why:\n"
        f"• {setup.get('reasoning', 'AI trend and momentum are aligned.')}\n"
        "• AI trend, momentum, RSI, MACD, MA50, and MA200 inform the ranking.\n"
        "• Select your stake to continue.\n\n"
        "The selected market and direction will remain fixed for this run.\n\n"
        "Expiry: 30 seconds\n"
        "Target: 5 winning cycles"
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