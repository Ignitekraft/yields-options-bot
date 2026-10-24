from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from app.config import settings
from app.services.account_service import AccountService
from app.services.account_mode_service import AccountModeService
from app.services.auth_service import AuthService
from app.services.auto_trade_service import (
    AutoTradeService,
    MAX_CONSECUTIVE_LOSSES,
)
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.scanner import YieldsScanner
from app.services.trade_execution_service import TradeExecutionService
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
TOKEN_STORE_IMAGE_URL = (
    "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
    "Bot%20images/fb421bed-8e95-4895-b92f-9e277693b83f.png"
)
TOKEN_PACKAGES = {
    50: ("5.50", "https://whop.com/ignitekraft-limited/get-50-tokens/"),
    100: ("8.50", "https://whop.com/ignitekraft-limited/get-100-tokens/"),
    200: ("14.50", "https://whop.com/ignitekraft-limited/get-200-tokens/"),
    500: ("35.00", "https://whop.com/ignitekraft-limited/get-500-tokens/"),
}


def _friendly_market_issue(reason: object) -> str:
    message = str(reason or "").strip()
    lowered = message.lower()
    if any(
        marker in lowered
        for marker in ("rate limit", "too many requests", "http 429", "http_429")
    ):
        return (
            "Deriv is temporarily limiting market-data connections. "
            "Please wait a little and try again."
        )
    if "stale" in lowered or "old." in lowered or "future" in lowered:
        return (
            "Market prices are out of date. "
            "Please wait for fresh data and try again."
        )
    if "not enough live history" in lowered:
        return (
            "There isn't enough recent market history to analyze this market yet."
        )
    if "no deriv tick history" in lowered:
        return "No recent live prices were available for this market."
    if "indicator warm-up" in lowered:
        return "More recent market history is needed to analyze this market."
    if "volatility baseline" in lowered:
        return "Market conditions could not be assessed yet."
    return "Live market data could not be retrieved. Please try again shortly."


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


def build_token_store_message() -> str:
    return (
        "💎 YIELDS TOKENS\n\n"
        "Tokens are used when Auto Trade places contracts. "
        "One token is used for each contract.\n\n"
        "Choose a package below to continue to secure payment with Whop."
    )


def build_token_purchase_message(token_amount: int) -> str:
    package = TOKEN_PACKAGES.get(token_amount)
    if package is None:
        raise ValueError("UNSUPPORTED_TOKEN_PACKAGE")
    price, _payment_url = package
    return (
        f"You're purchasing: {token_amount} Tokens 💎 - {price} USD.\n"
        "Proceed to payment to complete your purchase."
    )


def _dashboard_text(
    mode: str,
    real_balance: Any,
    demo_balance: Any,
    tokens: int,
    display_name: str | None = None,
) -> str:
    name = _display_name(display_name)
    return (
        "🤖 YIELDS OPTIONS BOT\n\n"
        f"Welcome back, {name}.\n\n"
        "Your Deriv account is connected.\n\n"
        f"Account Mode: {_mode_label(mode)}\n\n"
        "--------\n\n"
        "💰 ACCOUNT\n\n"
        f"Real: {_format_currency(real_balance)}\n"
        f"Demo: {_format_currency(demo_balance)}\n\n"
        f"💎 Yields Tokens: {tokens}"
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
            "🤖 YIELDS OPTIONS BOT\n\n"
            f"Hi {name},\n\n"
            "Indicator-based trading tools for Deriv.\n\n"
            "Connect your Deriv account to unlock:\n"
            "• Live indicator market scanning\n"
            "• Auto Trade\n"
            "• Real-time trade monitoring\n"
            "• Account balances\n"
            "• Yields Token features\n\n"
            "Connect securely through Deriv OAuth using the button below."
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
        tokens = int(profile.get("token_balance", 0))
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
        tokens_text = str(int(profile.get("token_balance", 0)))
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
        else (
            "📈 View the public market setup with Auto Trade.\n"
            "🔐 Connect Deriv when you’re ready to place trades."
        )
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
    excluded_market_symbol: str | None = None,
) -> dict:
    user_key = str(telegram_user_id)
    if not auth_service.get_valid_access_token(user_key):
        return {
            "symbol": "NO_SETUP",
            "direction": "NO_TRADE",
            "indicators": {},
            "current_price": 0.0,
            "signal_strength": 0.0,
            "reasoning": "Account not connected.",
        }

    service = MarketDataService()
    scanner = YieldsScanner()
    scanned_snapshots = scanner.collect_step_market_snapshots(service)
    excluded_market = None
    if excluded_market_symbol:
        excluded_market = TradeExecutionService.normalize_step_symbol(
            excluded_market_symbol
        )
    snapshots = [
        snapshot
        for snapshot in scanned_snapshots
        if excluded_market is None
        or _market_identity(snapshot.symbol) != excluded_market
    ]
    candidates = [
        snapshot
        for snapshot in snapshots
        if snapshot.data_quality == "good"
    ]
    directional_candidates = [
        snapshot
        for snapshot in candidates
        if snapshot.trend in {"bullish", "bearish"}
    ]
    market_issues = [
        {
            "display_name": snapshot.display_name,
            "reason": _friendly_market_issue(
                (snapshot.indicators or {}).get("signal_reason")
                or "Market data could not be analyzed."
            ),
        }
        for snapshot in snapshots
        if snapshot.data_quality != "good"
    ]
    scan_summary = {
        "markets_scanned": len(scanned_snapshots),
        "markets_available": len(candidates),
        "market_issues": market_issues,
    }
    winner = max(
        directional_candidates or candidates,
        key=lambda snapshot: (
            abs(
                float(
                    snapshot.indicators.get(
                        "directional_score",
                        snapshot.indicators.get("signal_strength", 0.0),
                    )
                )
            ),
            float(snapshot.indicators.get("signal_strength", 0.0)),
        ),
        default=None,
    )

    if winner is None:
        distinct_reasons = {
            item["reason"] for item in market_issues
        }
        if len(distinct_reasons) == 1:
            shared_reason = next(iter(distinct_reasons))
            affected_markets = ", ".join(
                item["display_name"] for item in market_issues
            )
            issue_details = (
                f"{shared_reason} Affected markets: {affected_markets}."
            )
        else:
            issue_details = "; ".join(
                f"{item['display_name']}: {item['reason']}"
                for item in market_issues
            )
        if excluded_market is not None and not market_issues:
            reasoning = (
                "No directional setup was available in another market."
            )
        else:
            reasoning = (
                "No usable live market data was available."
                + (f" {issue_details}" if issue_details else "")
            )
        if excluded_market is not None:
            reasoning += (
                f" Previous market "
                f"({_trade_market_name(excluded_market_symbol or '')}) was "
                "excluded to alternate markets."
            )
        return {
            "symbol": "NO_SETUP",
            "display_name": "Market Scan",
            "direction": "NO_TRADE",
            "scan_status": (
                "no_setup" if excluded_market is not None else "unavailable"
            ),
            "indicators": {"signal_reason": reasoning},
            "current_price": 0.0,
            "signal_strength": 0.0,
            "reasoning": reasoning,
            **scan_summary,
        }

    direction = (
        "BULLISH" if winner.trend == "bullish"
        else "BEARISH" if winner.trend == "bearish"
        else "NO_TRADE"
    )
    signal_strength = float(
        winner.indicators.get("signal_strength", 0.0)
    )
    is_directional = direction in {"BULLISH", "BEARISH"}
    reasoning = str(
        winner.indicators.get(
            "signal_reason",
            "The entry filters are not aligned.",
        )
    )

    return {
        "symbol": winner.symbol if is_directional else "NO_SETUP",
        "display_name": winner.display_name,
        "direction": direction,
        "indicators": winner.indicators,
        "current_price": winner.current_price,
        "signal_strength": signal_strength,
        "reasoning": reasoning,
        "scan_status": "ready" if is_directional else "no_setup",
        **scan_summary,
    }


def _market_identity(symbol: str) -> str:
    try:
        return TradeExecutionService.normalize_step_symbol(symbol)
    except ValueError:
        return str(symbol or "").strip().upper()


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
        "• How are Auto Trade setups selected?\n"
        "  It uses live Step Index prices and checks 5-second EMA(8/21), EMA(21) slope, 3-bar momentum, "
        "ATR(14), RSI(7), and 1-minute EMA(20/50) alignment.\n\n"
        "• Which markets are used?\n"
        "  Only the fixed Step Index family: 100, 200, 300, 400, and 500.\n\n"
        "• What is Auto Trade?\n"
        "  It picks the strongest current setup, confirms your stake, and monitors each contract until settlement.\n\n"
        "• How long is each Auto Trade contract?\n"
        "  Each contract expires after five market ticks. The elapsed time varies with how quickly the market updates.\n\n"
        "• Does Auto Trade stop after consecutive losses?\n"
        f"  Yes. Auto Trade stops after {MAX_CONSECUTIVE_LOSSES} consecutive "
        f"losses. Smart Scaling uses a {settings.martingale_multiplier:g}x "
        "factor; a session also ends after the configured winning-cycle "
        "target, when funds/tokens "
        "run out, or if Deriv rejects a trade.\n\n"
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
        f"🏁 Session win rate: {float(stats.get('win_rate') or 0):.1f}% "
        f"({wins}/{session_count} sessions)\n"
        f"🎯 Direction accuracy (contracts): "
        f"{float(stats.get('accuracy') or 0):.1f}%\n"
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
        "----\n\n"
        f"{_stats_mode_text('Real', '💰', stats.get('real') or {})}\n\n"
        f"{_stats_mode_text('Demo', '🎮', stats.get('demo') or {})}"
        "\n\nSession win rate counts profitable sessions; Net PnL shows "
        "overall profit or loss."
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


def build_connect_prompt(telegram_user_id: str | int) -> tuple[str, str]:
    session = auth_service.begin_oauth_session(str(telegram_user_id))
    client_id = settings.deriv_app_id or settings.deriv_client_id

    url = build_deriv_oauth_url(
        client_id,
        settings.deriv_redirect_uri,
        "trade account_manage",
        session["state"],
        session["code_challenge"],
    )

    message = (
        "🔐 Secure Deriv connection\n\n"
        "Use the link below to continue safely and return to your dashboard.\n\n"
        f"{url}"
        f"{HELP_FOOTER}"
    )
    return message, url


def handle_connect(telegram_user_id: str | int) -> str:
    message, _url = build_connect_prompt(telegram_user_id)
    return message


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

    symbol = settings.auto_trade_markets[0]
    history = market_service.get_historical_ticks(symbol, count=5000)
    live_tick = market_service.subscribe_ticks(symbol)

    result = scanner.analyze_live_tick(history, live_tick, symbol=symbol)

    return "\n".join(
        [
            SCAN_MESSAGE,
            f"Symbol: {result.symbol}",
            f"Signal: {result.signal}",
            f"Signal strength: {result.signal_strength:.2f}",
            f"Message: {result.message}",
        ]
    )


def handle_auto_trade_start(
    telegram_user_id: str | int,
    setup: dict | None = None,
) -> str:
    if not auth_service.get_valid_access_token(str(telegram_user_id)):
        return AUTO_TRADE_UNAVAILABLE_MESSAGE

    setup_data = (
        setup
        if setup is not None
        else build_auto_trade_setup(telegram_user_id)
    )
    if setup_data.get("scan_status") == "unavailable":
        markets_scanned = int(setup_data.get("markets_scanned") or 0)
        issues = setup_data.get("market_issues") or []
        distinct_reasons = {
            str(item["reason"]) for item in issues
        }
        if len(distinct_reasons) == 1:
            shared_reason = html.escape(
                _friendly_market_issue(next(iter(distinct_reasons)))
            )
            affected_markets = ", ".join(
                html.escape(str(item["display_name"]))
                for item in issues
            )
            details = f"{shared_reason}\nAffected markets: {affected_markets}"
        else:
            issue_lines = [
                f"• {html.escape(str(item['display_name']))}: "
                f"{html.escape(_friendly_market_issue(item['reason']))}"
                for item in issues
            ]
            details = "\n".join(issue_lines)
        return (
            "⚠️ LIVE MARKET DATA UNAVAILABLE\n\n"
            f"Could not retrieve usable live data for any of the "
            f"{markets_scanned} configured markets.\n"
            "No signal was generated and no trade was placed."
            + (f"\n\n{details}" if details else "")
        )

    direction = setup_data.get("direction", "NO_TRADE")

    indicators = setup_data.get("indicators") or {}
    rsi = indicators.get("rsi7")
    roc3 = indicators.get("roc3_pct")
    momentum_atr_ratio = indicators.get("momentum_atr_ratio")
    atr = indicators.get("atr14_5s")
    atr_ratio = indicators.get("atr_ratio")
    ema8 = indicators.get("ema8_5s")
    ema21 = indicators.get("ema21_5s")
    ema20_1m = indicators.get("ema20_1m")
    ema50_1m = indicators.get("ema50_1m")

    rsi_state = (
        "stretched high" if rsi >= 80
        else "stretched low" if rsi <= 20
        else "within range"
    ) if rsi is not None else "unavailable"
    short_trend = (
        "bullish" if ema8 > ema21
        else "bearish" if ema8 < ema21
        else "flat"
    ) if ema8 is not None and ema21 is not None else "unavailable"
    context_trend = (
        "bullish" if ema20_1m > ema50_1m
        else "bearish" if ema20_1m < ema50_1m
        else "flat"
    ) if ema20_1m is not None and ema50_1m is not None else "unavailable"
    momentum_text = (
        (
            f"{roc3:+.3f}% ({momentum_atr_ratio:.2f}x ATR)"
            if momentum_atr_ratio is not None
            else f"{roc3:+.3f}%"
        )
        if roc3 is not None
        else "unavailable"
    )
    volatility_text = (
        f"{atr:.5f} "
        f"({atr_ratio:.2f}x baseline)"
        if atr is not None and atr_ratio is not None
        else "unavailable"
    )
    rsi_text = (
        f"{rsi:.2f} ({rsi_state})"
        if rsi is not None
        else "unavailable"
    )
    short_trend_text = (
        f"{short_trend} ({ema8:.5f}/{ema21:.5f})"
        if ema8 is not None and ema21 is not None
        else "unavailable"
    )
    context_trend_text = (
        f"{context_trend} ({ema20_1m:.5f}/{ema50_1m:.5f})"
        if ema20_1m is not None and ema50_1m is not None
        else "unavailable"
    )

    direction_text = (
        "UP 🟢" if direction == "BULLISH"
        else "DOWN 🔴" if direction == "BEARISH"
        else "NO TRADE ⚪"
    )
    directional_score = indicators.get("directional_score")
    directional_score_text = (
        f"{float(directional_score):+.1f}/100"
        if directional_score is not None
        else "unavailable"
    )
    setup_title = (
        "🟢 UP SETUP"
        if direction == "BULLISH"
        else "🔴 DOWN SETUP"
        if direction == "BEARISH"
        else "⚪ No Directional Setup"
    )
    indicator_report = "\n".join(
        [
            f"Trading pair: {setup_data.get('display_name', 'Current market')}",
            f"RSI(7): {rsi_text}",
            f"5s EMA(8/21): {short_trend_text}",
            f"3-bar momentum: {momentum_text}",
            f"5s ATR(14): {volatility_text}",
            f"1m EMA(20/50): {context_trend_text}",
            f"Indicator direction score: {directional_score_text}",
            f"Direction: {direction_text} (directional bias, not a guarantee)",
        ]
    )

    if direction in {"BULLISH", "BEARISH"}:
        next_step = "Select trade amount 👇"
    else:
        next_step = (
            "No trade: "
            + html.escape(
                str(
                    setup_data.get("reasoning")
                    or indicators.get("signal_reason")
                    or "The entry filters are not aligned."
                )
            )
        )
    markets_scanned = int(setup_data.get("markets_scanned") or 0)
    markets_available = int(setup_data.get("markets_available") or 0)
    market_issues = setup_data.get("market_issues") or []
    availability_note = ""
    if markets_scanned and market_issues:
        issue_lines = [
            f"• {html.escape(str(item['display_name']))}: "
            f"{html.escape(_friendly_market_issue(item['reason']))}"
            for item in market_issues
        ]
        availability_note = (
            f"\n\nData available for {markets_available}/{markets_scanned} "
            "markets. Could not scan:\n"
            + "\n".join(issue_lines)
        )
    return (
        f"{setup_title}\n\n"
        f"<pre>{html.escape(indicator_report)}</pre>\n\n"
        f"{next_step}"
        f"{availability_note}"
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