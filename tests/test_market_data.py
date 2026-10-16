import asyncio
import importlib
import json
import math
import os
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.bot.callbacks import (
    _handle_auto_trade_entry,
    _run_auto_trade,
    _send_low_token_message,
    _show_auto_trade_setup,
    _setup_photo_url,
    _session_end_message,
    calculate_relative_stake,
    dispatch_callback,
    execute_trade,
)
from app.bot.handlers import (
    build_menu_message,
    build_auto_trade_setup,
    build_start_message,
    handle_auto_trade_start,
    handle_start,
)
from app.bot.keyboards import TelegramKeyboard
from app.bot.telegram_bot import _send_menu_message
from app.config import settings
from app.main import app, deriv_oauth_callback
from app.models.market import MarketSnapshot
from app.services.account_service import AccountService
from app.services.auth_service import AuthService
from app.services.auto_trade_service import AutoTradeService
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.scanner import SignalResult, YieldsScanner
from app.services.trade_history_service import TradeHistoryService
from app.services.trade_execution_service import TradeExecutionService
from app.services.trading_service import TradingService
from app.services.yields_orchestrator import YieldsOrchestrator


def _sample_deriv_ticks(direction: int = 1, count: int = 800) -> list[dict]:
    current_epoch = int(time.time())
    base_index = count - 9
    base_price = (
        100
        + direction
        * (
            0.008 * base_index
            + 0.12 * math.sin(base_index * 0.39)
        )
    )
    final_changes = [-0.08, -0.08, -0.08, -0.08, 0.10, 0.10, 0.10, 0.10]
    ticks = []
    for index in range(count):
        if index < base_index + 1:
            quote = 100 + direction * (
                0.008 * index + 0.12 * math.sin(index * 0.39)
            )
        else:
            change_count = index - base_index
            quote = base_price + direction * sum(
                final_changes[:change_count]
            )
        epoch = (current_epoch // 5 - count + 1 + index) * 5
        ticks.append(
            {
                "symbol": "STPRNG",
                "quote": quote,
                "epoch": epoch,
            }
        )
    return ticks


class MarketDataTests(unittest.TestCase):
    def test_market_data_normalizes_tick_payload(self):
        service = MarketDataService()

        normalized = service.normalize_tick(
            {
                "tick": {
                    "symbol": "STPRNG",
                    "quote": 123.45,
                    "epoch": 1710000000,
                },
                "echo_req": {"ticks": "STPRNG"},
            }
        )

        self.assertEqual(normalized["symbol"], "STPRNG")
        self.assertEqual(normalized["quote"], 123.45)
        self.assertEqual(normalized["epoch"], 1710000000)
        self.assertIn("timestamp", normalized)

    def test_scanner_builds_signal_result(self):
        scanner = YieldsScanner()
        signal = scanner.build_signal(1.0, 1.2, 0.85, "STPRNG")

        self.assertIsInstance(signal, SignalResult)
        self.assertEqual(signal.symbol, "STPRNG")
        self.assertEqual(signal.signal, "HOLD")
        self.assertEqual(signal.signal_strength, 0)

    def test_step_index_symbols_use_deriv_step_market_mapping(self):
        expected = {
            "step_100": "STPRNG",
            "step_200": "STPRNG2",
            "step_300": "STPRNG3",
            "step_400": "STPRNG4",
            "step_500": "STPRNG5",
        }

        for key, value in expected.items():
            self.assertEqual(TradeExecutionService.normalize_step_symbol(key), value)
            self.assertEqual(TradeExecutionService.normalize_step_symbol(value), value)

    def test_scanner_confirms_short_term_momentum_with_higher_timeframe_trend(self):
        scanner = YieldsScanner()
        bullish = scanner.normalize_step_market(
            "step_100",
            "Step Index 100",
            _sample_deriv_ticks(1),
        )
        bearish = scanner.normalize_step_market(
            "step_200",
            "Step Index 200",
            _sample_deriv_ticks(-1),
        )

        self.assertEqual(bullish.trend, "bullish")
        self.assertEqual(bearish.trend, "bearish")
        self.assertEqual(bullish.data_quality, "good")
        self.assertGreater(bullish.indicators["bars_1m"], 60)
        self.assertGreater(bullish.indicators["atr_ratio"], 0.5)
        self.assertLess(bullish.indicators["atr_ratio"], 2.5)
        self.assertIn("1-minute context", bullish.indicators["signal_reason"])

    def test_scanner_refuses_short_or_stale_deriv_history(self):
        scanner = YieldsScanner()
        insufficient = scanner.normalize_step_market(
            "step_100",
            "Step Index 100",
            _sample_deriv_ticks(count=20),
        )
        stale_ticks = _sample_deriv_ticks()
        for tick in stale_ticks:
            tick["epoch"] -= 60
        stale = scanner.normalize_step_market(
            "step_100",
            "Step Index 100",
            stale_ticks,
        )

        self.assertEqual(insufficient.trend, "neutral")
        self.assertEqual(insufficient.data_quality, "insufficient")
        self.assertEqual(stale.trend, "neutral")
        self.assertEqual(stale.data_quality, "stale")

    def test_orchestrator_does_not_create_proposal_for_hold_signal(self):
        orchestrator = YieldsOrchestrator()
        hold_signal = {
            "symbol": "step_100",
            "signal": "HOLD",
            "signal_strength": 0.0,
            "direction": "neutral",
        }
        with patch.object(
            orchestrator,
            "scan_symbol",
            return_value=hold_signal,
        ), patch.object(
            orchestrator.trading_service,
            "create_proposal",
        ) as create_proposal:
            plan = orchestrator.build_trade_plan("step_100")

        self.assertEqual(plan["action"], "hold")
        self.assertIsNone(plan["proposal"])
        create_proposal.assert_not_called()

    def test_historical_ticks_are_read_from_deriv_and_step_symbols_are_mapped(self):
        now = int(time.time())

        class FakeSocket:
            def __init__(self):
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def send(self, payload):
                self.sent.append(json.loads(payload))

            def recv(self, timeout=None):
                return json.dumps(
                    {
                        "history": {
                            "prices": ["100.1", "100.2"],
                            "times": [now - 1, now],
                        }
                    }
                )

        socket = FakeSocket()
        with patch(
            "app.services.deriv.market_data.websocket_client.connect",
            return_value=socket,
        ) as connect_mock:
            history = MarketDataService(
                "wss://ws.derivws.com/websockets/v3",
                app_id="test-app",
            ).get_historical_ticks("step_100", count=5000)

        connect_mock.assert_called_once_with(
            "wss://ws.derivws.com/websockets/v3?app_id=test-app",
            open_timeout=10,
            close_timeout=2,
        )
        self.assertEqual([tick["quote"] for tick in history], [100.1, 100.2])
        self.assertEqual([tick["epoch"] for tick in history], [now - 1, now])
        self.assertEqual(socket.sent[0]["ticks_history"], "STPRNG")
        self.assertEqual(socket.sent[0]["count"], 5000)
        self.assertEqual(socket.sent[0]["style"], "ticks")

    def test_monitor_contract_uses_numeric_contract_id(self):
        service = TradeExecutionService()

        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.responses = iter(
                    [
                        json.dumps(
                            {
                                "proposal_open_contract": {
                                    "contract_id": 123456789,
                                    "is_sold": True,
                                    "profit": 1.0,
                                    "payout": 2.0,
                                    "buy_price": 1.0,
                                    "sell_price": 1.0,
                                    "entry_tick": 100,
                                    "exit_tick": 101,
                                }
                            }
                        )
                    ]
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def recv(self):
                return next(self.responses)

        with patch(
            "app.services.trade_execution_service.websockets.connect",
            return_value=FakeWebSocket(),
        ):
            result = asyncio.run(
                service.monitor_contract(
                    "123456789",
                    "wss://example.com",
                    timeout_seconds=1,
                )
            )

        self.assertEqual(result["status"], "settled")
        self.assertIsInstance(result["contract_id"], str)
        self.assertEqual(result["contract_id"], "123456789")

    def test_execute_trade_retries_invalid_asset_candidates(self):
        class FakeWebSocket:
            def __init__(self):
                self.responses = iter(
                    [
                        json.dumps({
                            "error": {
                                "code": "InvalidInputAsset",
                                "message": "Trading is not offered for this asset",
                            }
                        }),
                        json.dumps({
                            "proposal": {
                                "id": "proposal-456",
                                "ask_price": 1.25,
                            }
                        }),
                        json.dumps({
                            "buy": {
                                "contract_id": 987654321,
                                "buy_price": 1.25,
                                "balance_after": 1000.0,
                                "shortcode": "CALL",
                            }
                        }),
                    ]
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def send(self, payload):
                return None

            async def recv(self):
                return next(self.responses)

        service = TradeExecutionService()
        with patch(
            "app.services.trade_execution_service.websockets.connect",
            return_value=FakeWebSocket(),
        ):
            result = asyncio.run(
                service.execute_trade(
                    user_id="retry-user",
                    market_symbol="step_100",
                    direction="BULLISH",
                    stake=10.0,
                    account_id="demo-123",
                    access_token="token",
                    currency="USD",
                    ws_url="wss://api.derivws.com/trading/v1/options/ws/public",
                    account_type="demo",
                )
            )

        self.assertEqual(result["status"], "accepted")
        self.assertIn(result["symbol"], {"STPRNG", "STPRNG2", "STPRNG3", "STPRNG4", "STPRNG5"})
        self.assertIn("contract_id", result)

    def test_stake_keyboard_excludes_blocked_values(self):
        keyboard = TelegramKeyboard.stake_keyboard()
        rows = keyboard.inline_keyboard

        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [["🟢 0.1%", "🟢 0.5%", "🟠 1%"]],
        )
        self.assertEqual([button.callback_data for button in rows[0]], [
            "AUTO_TRADE_STAKE_0_1",
            "AUTO_TRADE_STAKE_0_5",
            "AUTO_TRADE_STAKE_1",
        ])
        self.assertEqual([button.style for button in rows[0]], ["success", "success", None])

    def test_relative_stakes_use_half_up_whole_number_rounding(self):
        self.assertEqual(calculate_relative_stake(5000, "0_1"), 5)
        self.assertEqual(calculate_relative_stake(5000, "0_5"), 25)
        self.assertEqual(calculate_relative_stake(5000, "1"), 50)
        self.assertEqual(calculate_relative_stake(250.5, "0_5"), 1)
        self.assertEqual(calculate_relative_stake(400, "0_1"), 0)

    def test_state_dir_falls_back_when_render_path_is_not_writable(self):
        import app.services.auth_service as auth_service

        original_env = os.environ.get("YIELDS_STATE_DIR")
        original_state_dir = auth_service.STATE_DIR
        original_mkdir = Path.mkdir

        def fake_mkdir(*args, **kwargs):
            path = args[0] if args else Path.cwd()
            if str(path).startswith("/data"):
                raise PermissionError("denied")
            return original_mkdir(path, *args[1:], **kwargs)

        try:
            with patch.dict(
                os.environ,
                {"YIELDS_STATE_DIR": "/data/yields_state"},
                clear=False,
            ):
                with patch.object(Path, "mkdir", side_effect=fake_mkdir):
                    reloaded = importlib.reload(auth_service)
                    self.assertIn(".yields_state", reloaded.STATE_DIR)
                    self.assertTrue(Path(reloaded.STATE_DIR).exists())
        finally:
            if original_env is None:
                os.environ.pop("YIELDS_STATE_DIR", None)
            else:
                os.environ["YIELDS_STATE_DIR"] = original_env

            auth_service.STATE_DIR = original_state_dir
            importlib.reload(auth_service)

    def test_stale_running_auto_trade_is_treated_as_inactive(self):
        service = AutoTradeService()
        state = service.start_run(
            "stale-user",
            "step_100",
            "BULLISH",
            2.0,
            account_mode="demo",
        )
        state.status = "running"
        state.started_at = "2020-01-01T00:00:00+00:00"

        self.assertFalse(service.is_running("stale-user"))

        replacement = service.start_run(
            "stale-user",
            "step_300",
            "BEARISH",
            5.0,
            account_mode="demo",
        )
        self.assertEqual(replacement.symbol, "step_300")
        self.assertEqual(replacement.status, "awaiting_confirmation")

    def test_auto_trade_keeps_martingale_without_safety_limit(self):
        service = AutoTradeService()
        state = service.start_run(
            "martingale-user",
            "step_100",
            "BULLISH",
            2.0,
            account_mode="demo",
        )

        state = service.advance_after_result("martingale-user", won=False, profit=-2.0)
        self.assertEqual(state.status, "running")
        self.assertEqual(state.current_stake, 4.6)
        self.assertEqual(state.martingale_step, 2)

        for _ in range(9):
            state = service.advance_after_result("martingale-user", won=False, profit=-2.0)

        self.assertEqual(state.status, "running")
        self.assertGreater(state.current_stake, 2.0)
        self.assertEqual(state.martingale_step, 11)

    def test_auto_trade_starts_next_cycle_without_delay_after_settlement(self):
        service = AutoTradeService()
        service.start_run(
            "immediate-user",
            "step_300",
            "BULLISH",
            2.0,
            account_mode="demo",
        )
        service.confirm_run("immediate-user")

        class FakeQuery:
            def __init__(self):
                self.edited = None

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "immediate-user"})()

        update = FakeUpdate()

        with patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(return_value={"profit": 1.0, "status": "settled"}),
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(return_value={"status": "rejected", "reason": "stop"}),
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                reserve_token=lambda *_: {"ok": True, "token_balance": 10},
                refund_token=lambda *_: {"ok": True, "token_balance": 10},
                update_session=lambda *_: None,
            ),
        ), patch(
            "app.bot.callbacks.asyncio.sleep",
            side_effect=AssertionError("post-settlement sleep should not delay the next trade"),
        ):
            asyncio.run(_run_auto_trade(update, service.get_run("immediate-user")))

        self.assertTrue(True)

    def test_start_screen_uses_live_balance_values(self):
        auth = AuthService()
        auth.connect_user(
            "live-start-user",
            {
                "access_token": "live-token",
                "refresh_token": "live-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        balances = {
            "real": {
                "available": True,
                "balance": 1250.5,
                "currency": "USD",
            },
            "demo": {
                "available": True,
                "balance": 750.25,
                "currency": "USD",
            },
            "tokens": 7,
        }

        with patch(
            "app.bot.handlers.account_service.get_live_balances",
            new=AsyncMock(return_value=balances),
        ), patch(
            "app.bot.handlers.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 7},
                update_balance_snapshots=lambda *_args: None,
            ),
        ):
            text = asyncio.run(
                build_start_message("live-start-user", display_name="Sam")
            )

        self.assertIn("Welcome back, Sam.", text)
        self.assertIn("1,250.50 USD", text)
        self.assertIn("750.25 USD", text)
        self.assertNotIn("AUTO TRADE", text)
        self.assertNotIn("Gemini", text)

    def test_menu_message_shows_user_name_mode_balances_and_tokens(self):
        with patch(
            "app.bot.handlers.auth_service.get_valid_access_token",
            return_value=None,
        ), patch(
            "app.bot.handlers.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.handlers.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 26},
            ),
        ):
            text = asyncio.run(
                build_menu_message(
                    "menu-user",
                    display_name="Sam",
                    last_name="User",
                    username="sam_user",
                )
            )

        self.assertIn("Hi Sam", text)
        self.assertIn("Trading mode: DEMO", text)
        self.assertIn("Real Balance: Connect Deriv to view", text)
        self.assertIn("Demo Balance: Connect Deriv to view", text)
        self.assertIn("Available Tokens: 26", text)
        self.assertIn("Need help? Ask Sammie", text)

    def test_menu_keyboard_has_requested_rows_and_community_url(self):
        keyboard = TelegramKeyboard.menu_keyboard(
            "https://t.me/+N58js2a8NF1mMzY0"
        )
        rows = keyboard.inline_keyboard

        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [
                ["🚀 Auto Trade"],
                ["💎 Get Tokens", "🌍 Community"],
                ["❓ Faq"],
                ["🎁 Invite"],
            ],
        )
        self.assertEqual(rows[1][1].url, "https://t.me/+N58js2a8NF1mMzY0")

    def test_start_menu_sender_sends_brand_image_and_menu_keyboard(self):
        class FakeMessage:
            def __init__(self):
                self.photo = None
                self.text = None

            async def reply_photo(self, **kwargs):
                self.photo = kwargs

            async def reply_text(self, text, **kwargs):
                self.text = (text, kwargs)

        class FakeUpdate:
            def __init__(self):
                self.effective_user = type(
                    "User",
                    (),
                    {
                        "id": "start-menu-user",
                        "first_name": "Sam",
                        "last_name": "User",
                        "username": "sam_user",
                    },
                )()
                self.message = FakeMessage()

        update = FakeUpdate()
        with patch(
            "app.bot.telegram_bot.auth_service.get_valid_access_token",
            return_value=None,
        ), patch(
            "app.bot.telegram_bot.build_menu_message",
            new=AsyncMock(return_value="Welcome to Yields"),
        ):
            asyncio.run(_send_menu_message(update))

        self.assertIsNotNone(update.message.photo)
        self.assertEqual(update.message.photo["photo"], settings.menu_image_url)
        self.assertEqual(update.message.photo["caption"], "Welcome to Yields")
        rows = update.message.photo["reply_markup"].inline_keyboard
        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [
                ["🔐 Connect Deriv"],
                ["ℹ️ How Yields Works"],
            ],
        )

    def test_stats_command_sends_message_without_buttons(self):
        from app.bot.telegram_bot import _send_stats_message

        class FakeMessage:
            def __init__(self):
                self.text = None

            async def reply_text(self, text, **kwargs):
                self.text = (text, kwargs)

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id="stats-user"),
            message=FakeMessage(),
        )

        with patch(
            "app.bot.telegram_bot.handle_stats",
            new=AsyncMock(return_value="Yields stats"),
        ):
            asyncio.run(_send_stats_message(update))

        self.assertEqual(update.message.text, ("Yields stats", {}))

    def test_auto_trade_setup_images_use_supabase_urls_by_direction(self):
        self.assertEqual(
            _setup_photo_url("BULLISH"),
            settings.bullish_setup_image_url,
        )
        self.assertEqual(
            _setup_photo_url("BEARISH"),
            settings.bearish_setup_image_url,
        )

    def test_auto_trade_start_matches_indicator_setup_format(self):
        auth = AuthService()
        auth.connect_user(
            "trade-user",
            {
                "access_token": "trade-token",
                "refresh_token": "trade-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        with patch(
            "app.bot.handlers.build_auto_trade_setup",
            return_value={
                "symbol": "STPRNG",
                "display_name": "Step Index 100",
                "direction": "BULLISH",
                "signal_strength": 0.88,
                "current_price": 120.0,
                "indicators": {
                    "rsi7": 25.0,
                    "ema8_5s": 121.0,
                    "ema21_5s": 120.0,
                    "roc3_pct": 0.12,
                    "momentum_atr_ratio": 0.8,
                    "atr14_5s": 0.035,
                    "atr_ratio": 1.1,
                    "ema20_1m": 119.0,
                    "ema50_1m": 118.0,
                },
                "reasoning": "Strong upward trend and trend alignment.",
            },
        ):
            text = handle_auto_trade_start("trade-user")

        self.assertEqual(
            text,
            "🟢 Strong Bullish Setup Detected\n\n"
            "<pre>Trading pair: Step Index 100\n"
            "RSI(7): 25.00 (within range)\n"
            "5s EMA(8/21): bullish (121.00000/120.00000)\n"
            "3-bar momentum: +0.120% (0.80x ATR)\n"
            "5s ATR(14): 0.03500 (1.10x baseline)\n"
            "1m EMA(20/50): bullish (119.00000/118.00000)\n"
            "Direction: UP 🟢</pre>\n\n"
            "Select trade amount 👇",
        )

    def test_auto_trade_start_message_is_clear_and_meaningful(self):
        auth = AuthService()
        auth.connect_user(
            "readable-user",
            {
                "access_token": "readable-token",
                "refresh_token": "readable-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        with patch(
            "app.bot.handlers.build_auto_trade_setup",
            return_value={
                "symbol": "STPRNG",
                "display_name": "Step 400",
                "direction": "BULLISH",
                "signal_strength": 0.76,
                "current_price": 120.0,
                "indicators": {
                    "rsi7": 65.0,
                    "ema8_5s": 121.0,
                    "ema21_5s": 120.0,
                    "roc3_pct": 0.12,
                    "momentum_atr_ratio": 0.8,
                    "atr14_5s": 0.035,
                    "atr_ratio": 1.1,
                    "ema20_1m": 119.0,
                    "ema50_1m": 118.0,
                },
                "reasoning": "The Step 400 shows bullish pressure with MA50/MA200 alignment and RSI confirmation.",
            },
        ):
            text = handle_auto_trade_start("readable-user")

        self.assertIn("Strong Bullish Setup Detected", text)
        self.assertIn("Trading pair: Step 400", text)
        self.assertIn("Direction: UP 🟢", text)
        self.assertIn("<pre>", text)
        self.assertIn("Select trade amount", text)

    def test_auto_trade_setup_has_copyable_analysis_block(self):
        auth = AuthService()
        auth.connect_user(
            "premium-user",
            {
                "access_token": "premium-token",
                "refresh_token": "premium-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        with patch(
            "app.bot.handlers.build_auto_trade_setup",
            return_value={
                "symbol": "STPRNG",
                "display_name": "Step 400",
                "direction": "BULLISH",
                "signal_strength": 0.76,
                "current_price": 120.0,
                "indicators": {
                    "rsi7": 65.0,
                    "ema8_5s": 121.0,
                    "ema21_5s": 120.0,
                    "roc3_pct": 0.12,
                    "momentum_atr_ratio": 0.8,
                    "atr14_5s": 0.035,
                    "atr_ratio": 1.1,
                    "ema20_1m": 119.0,
                    "ema50_1m": 118.0,
                },
                "reasoning": "The Step 400 shows bullish pressure with MA50/MA200 alignment and RSI confirmation.",
            },
        ):
            text = handle_auto_trade_start("premium-user")

        self.assertIn("<pre>", text)
        self.assertIn("Select trade amount 👇", text)

    def test_auto_trade_setup_ranks_markets_from_indicators_deterministically(self):
        auth = AuthService()
        auth.connect_user(
            "indicator-user",
            {
                "access_token": "indicator-token",
                "refresh_token": "indicator-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        original_markets = settings.auto_trade_markets
        snapshots = [
            MarketSnapshot(
                symbol="step_300",
                display_name="Step 300",
                current_price=90.0,
                trend="neutral",
                indicators={
                    "rsi7": 50.0,
                    "signal_strength": 0.1,
                    "signal_reason": "Short-term trend and momentum are not aligned.",
                },
            ),
            MarketSnapshot(
                symbol="step_500",
                display_name="Step 500",
                current_price=120.0,
                trend="bullish",
                indicators={
                    "rsi7": 65.0,
                    "signal_strength": 1.2,
                    "signal_reason": "5-second trend and momentum align with 1-minute context.",
                },
            ),
        ]

        with patch("app.bot.handlers.YieldsScanner.collect_step_market_snapshots", return_value=snapshots):
            object.__setattr__(
                settings,
                "auto_trade_markets",
                ("step_300", "step_500"),
            )

            try:
                setup = build_auto_trade_setup("indicator-user")
                repeated_setup = build_auto_trade_setup("indicator-user")
            finally:
                object.__setattr__(
                    settings,
                    "auto_trade_markets",
                    original_markets,
                )

        self.assertEqual(setup["symbol"], "step_500")
        self.assertEqual(setup["direction"], "BULLISH")
        self.assertEqual(setup, repeated_setup)
        self.assertIn("5-second trend", setup["reasoning"])
        self.assertGreater(setup["signal_strength"], 0)

    def test_auto_trade_setup_rejects_conflicting_indicator_votes(self):
        auth = AuthService()
        auth.connect_user(
            "conflicting-indicators-user",
            {
                "access_token": "indicator-token",
                "refresh_token": "indicator-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
        snapshots = [
            MarketSnapshot(
                symbol="step_300",
                display_name="Step 300",
                current_price=90.0,
                trend="neutral",
                indicators={
                    "rsi7": 65.0,
                    "signal_strength": 1.0,
                    "signal_reason": "Short-term trend and momentum are not aligned.",
                },
            ),
        ]

        with patch(
            "app.bot.handlers.YieldsScanner.collect_step_market_snapshots",
            return_value=snapshots,
        ):
            setup = build_auto_trade_setup("conflicting-indicators-user")

        self.assertEqual(setup["symbol"], "NO_SETUP")
        self.assertEqual(setup["direction"], "NO_TRADE")

    def test_auto_trade_setup_explains_stale_deriv_market_data(self):
        auth = AuthService()
        auth.connect_user(
            "stale-data-user",
            {
                "access_token": "stale-data-token",
                "refresh_token": "stale-data-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
        stale_snapshot = MarketSnapshot(
            symbol="step_100",
            display_name="Step 100",
            current_price=0.0,
            data_quality="stale",
            indicators={"signal_reason": "Latest Deriv tick is 42 seconds old."},
        )

        with patch(
            "app.bot.handlers.YieldsScanner.collect_step_market_snapshots",
            return_value=[stale_snapshot],
        ):
            setup = build_auto_trade_setup("stale-data-user")

        self.assertEqual(setup["symbol"], "NO_SETUP")
        self.assertIn("42 seconds old", setup["reasoning"])

    def test_confirmation_callback_executes_trade(self):
        class FakeQuery:
            def __init__(self):
                self.data = "AUTO_TRADE_CONFIRM"
                self.edited = None

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type(
                    "User",
                    (),
                    {"id": "confirm-user"},
                )()

        run_state = AutoTradeService()
        run_state.start_run(
            "confirm-user",
            "step_300",
            "BULLISH",
            1.0,
        )

        with patch(
            "app.bot.callbacks.auto_trade_service",
            run_state,
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 5},
                create_session=lambda *_args: None,
            ),
        ), patch(
            "app.bot.callbacks._run_auto_trade",
            new=AsyncMock(),
        ) as run_auto_trade_mock:
            update = FakeUpdate()
            asyncio.run(dispatch_callback(update, None))

        run_auto_trade_mock.assert_awaited_once()

    def test_confirm_callback_blocks_when_token_balance_is_zero(self):
        class FakeQuery:
            def __init__(self):
                self.data = "AUTO_TRADE_CONFIRM"
                self.edited = None
                self.photo = None
                self.message = SimpleNamespace(reply_photo=self.reply_photo)

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text

            async def reply_photo(self, **kwargs):
                self.photo = kwargs

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "empty-token-user"})()

        run_state = AutoTradeService()
        run_state.start_run("empty-token-user", "step_300", "BULLISH", 5.0)
        create_session = unittest.mock.Mock()
        update = FakeUpdate()

        with patch("app.bot.callbacks.auto_trade_service", run_state), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 0},
                create_session=create_session,
            ),
        ), patch(
            "app.bot.callbacks._run_auto_trade",
            new=AsyncMock(),
        ) as run_auto_trade_mock:
            asyncio.run(dispatch_callback(update, None))

        self.assertIn("out of Yields Tokens", update.callback_query.photo["caption"])
        self.assertIn("No trade has been placed", update.callback_query.photo["caption"])
        self.assertIn("Low%20Tokens%20Warning%20Alert.png", update.callback_query.photo["photo"])
        create_session.assert_not_called()
        run_auto_trade_mock.assert_not_awaited()

    def test_auto_trade_warns_at_fifty_tokens_before_scanning(self):
        class FakeQuery:
            def __init__(self):
                self.data = "START_AUTO_TRADE"
                self.edited = None
                self.reply_markup = None
                self.photo = None
                self.message = SimpleNamespace(reply_photo=self.reply_photo)

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text
                self.reply_markup = kwargs.get("reply_markup")

            async def reply_photo(self, **kwargs):
                self.photo = kwargs

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "low-token-user"})()

        update = FakeUpdate()

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 50},
            ),
        ), patch(
            "app.bot.callbacks.build_auto_trade_setup",
        ) as build_setup:
            asyncio.run(dispatch_callback(update, None))

        self.assertIn("50 tokens", update.callback_query.photo["caption"])
        self.assertIn("LOW TOKEN BALANCE", update.callback_query.photo["caption"])
        self.assertEqual(
            update.callback_query.photo["photo"],
            "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
            "Bot%20images/Low%20Tokens%20Warning%20Alert.png",
        )
        build_setup.assert_not_called()
        rows = update.callback_query.photo["reply_markup"].inline_keyboard
        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [
                ["🚀 Trade Anyway", "💎 Get Tokens"],
                ["🏠 Main Menu", "💬 Message Sammie"],
            ],
        )
        self.assertEqual(
            rows[1][1].url,
            "https://t.me/samueludoh_ng",
        )

    def test_unauthenticated_auto_trade_entry_offers_oauth_connect_button(self):
        class FakeQuery:
            def __init__(self):
                self.edited = None
                self.reply_markup = None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text
                self.reply_markup = kwargs.get("reply_markup")

        class FakeUpdate:
            effective_user = SimpleNamespace(id="unauthenticated-user", first_name="Sam")

        query = FakeQuery()
        update = FakeUpdate()
        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value=None,
        ), patch(
            "app.bot.callbacks.trade_history_service.ensure_user",
        ) as ensure_user:
            asyncio.run(_handle_auto_trade_entry(update, query))

        self.assertIn("Connect your Deriv account", query.edited)
        self.assertEqual(
            query.reply_markup.inline_keyboard[0][0].callback_data,
            "START_CONNECT",
        )
        ensure_user.assert_not_called()

    def test_start_menu_offers_connect_button_for_unauthenticated_user(self):
        class FakeMessage:
            def __init__(self):
                self.reply_markup = None

            async def reply_photo(self, **kwargs):
                self.reply_markup = kwargs["reply_markup"]

        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id="unauthenticated-start-user",
                first_name="Sam",
                last_name=None,
                username=None,
            ),
            message=FakeMessage(),
        )
        with patch(
            "app.bot.telegram_bot.auth_service.get_valid_access_token",
            return_value=None,
        ), patch(
            "app.bot.telegram_bot.build_menu_message",
            new=AsyncMock(return_value="Connect your Deriv account"),
        ):
            asyncio.run(_send_menu_message(update))

        self.assertEqual(
            update.message.reply_markup.inline_keyboard[0][0].callback_data,
            "START_CONNECT",
        )

    def test_start_connect_button_edits_photo_caption_and_opens_oauth_url(self):
        class FakeQuery:
            data = "START_CONNECT"

            def __init__(self):
                self.caption = None
                self.reply_markup = None

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                raise RuntimeError("message has a caption")

            async def edit_message_caption(self, caption, **kwargs):
                self.caption = caption
                self.reply_markup = kwargs.get("reply_markup")

        query = FakeQuery()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id="connect-user"),
        )
        oauth_url = "https://oauth.deriv.com/oauth2/authorize?state=test-state"

        with patch(
            "app.bot.callbacks.build_connect_prompt",
            return_value=("Secure connection instructions", oauth_url),
        ):
            asyncio.run(dispatch_callback(update, None))

        self.assertEqual(query.caption, "Secure connection instructions")
        self.assertEqual(
            query.reply_markup.inline_keyboard[0][0].url,
            oauth_url,
        )

    def test_start_info_button_edits_photo_caption(self):
        class FakeQuery:
            data = "START_INFO"

            def __init__(self):
                self.caption = None

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                raise RuntimeError("message has a caption")

            async def edit_message_caption(self, caption, **kwargs):
                self.caption = caption

        query = FakeQuery()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id="info-user"),
        )
        asyncio.run(dispatch_callback(update, None))

        self.assertIn("How it works:", query.caption)
        self.assertIn("AI ranks the 5 fixed Step Indices", query.caption)

    def test_trade_anyway_rechecks_balance_before_continuing(self):
        class FakeQuery:
            def __init__(self):
                self.data = "AUTO_TRADE_TRADE_ANYWAY"
                self.edited = None

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "trade-anyway-user"})()

        update = FakeUpdate()
        show_setup = AsyncMock()

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 1},
            ),
        ), patch(
            "app.bot.callbacks._show_auto_trade_setup",
            new=show_setup,
        ):
            asyncio.run(dispatch_callback(update, None))

        show_setup.assert_awaited_once()

    def test_balance_above_fifty_skips_low_token_warning(self):
        class FakeQuery:
            def __init__(self):
                self.data = "START_AUTO_TRADE"

            async def answer(self):
                return None

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "above-threshold-user"})()

        show_setup = AsyncMock()

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 51},
            ),
        ), patch(
            "app.bot.callbacks._show_auto_trade_setup",
            new=show_setup,
        ):
            asyncio.run(dispatch_callback(FakeUpdate(), None))

        show_setup.assert_awaited_once()

    def test_trade_anyway_stops_if_balance_has_reached_zero(self):
        class FakeQuery:
            def __init__(self):
                self.data = "AUTO_TRADE_TRADE_ANYWAY"
                self.edited = None
                self.photo = None
                self.message = SimpleNamespace(reply_photo=self.reply_photo)

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text

            async def reply_photo(self, **kwargs):
                self.photo = kwargs

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "now-empty-user"})()

        update = FakeUpdate()
        show_setup = AsyncMock()

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 0},
            ),
        ), patch(
            "app.bot.callbacks._show_auto_trade_setup",
            new=show_setup,
        ):
            asyncio.run(dispatch_callback(update, None))

        self.assertIn("out of Yields Tokens", update.callback_query.photo["caption"])
        self.assertIn("Low%20Tokens%20Warning%20Alert.png", update.callback_query.photo["photo"])
        show_setup.assert_not_awaited()

    def test_auto_trade_setup_message_uses_a_single_scan_result(self):
        class FakeQuery:
            def __init__(self):
                self.edited = None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "single-scan-user"})()

        setup = {
            "symbol": "step_100",
            "display_name": "Step Index 100",
            "direction": "BULLISH",
            "current_price": 120.0,
            "indicators": {
                "rsi7": 25.0,
                "ema8_5s": 121.0,
                "ema21_5s": 120.0,
                "roc3_pct": 0.12,
                "momentum_atr_ratio": 0.8,
                "atr14_5s": 0.035,
                "atr_ratio": 1.1,
                "ema20_1m": 119.0,
                "ema50_1m": 118.0,
            },
        }
        update = FakeUpdate()

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.build_auto_trade_setup",
            return_value=setup,
        ) as build_setup, patch(
            "app.bot.callbacks._setup_photo_url",
            return_value=None,
        ):
            asyncio.run(_show_auto_trade_setup(update, update.callback_query))

        build_setup.assert_called_once_with("single-scan-user")
        self.assertIn("Trading pair: Step Index 100", update.callback_query.edited)
        self.assertIn("<pre>", update.callback_query.edited)

    def test_no_trade_setup_hides_stake_buttons_and_explains_filter(self):
        class FakeQuery:
            def __init__(self):
                self.edited = None
                self.reply_markup = None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text
                self.reply_markup = kwargs.get("reply_markup")

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id="no-trade-setup-user"),
            callback_query=FakeQuery(),
        )
        setup = {
            "symbol": "NO_SETUP",
            "display_name": "Step Index 100",
            "direction": "NO_TRADE",
            "indicators": {
                "signal_reason": "Current volatility is outside the normal range.",
            },
            "reasoning": "Current volatility is outside the normal range.",
        }

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.build_auto_trade_setup",
            return_value=setup,
        ), patch(
            "app.bot.callbacks._setup_photo_url",
            return_value=None,
        ):
            asyncio.run(_show_auto_trade_setup(update, update.callback_query))

        self.assertIn("No trade:", update.callback_query.edited)
        self.assertIn("volatility is outside", update.callback_query.edited)
        self.assertEqual(
            update.callback_query.reply_markup.inline_keyboard[0][0].text,
            "🤖 Auto Trade",
        )
        self.assertNotIn(
            "🟢 0.1%",
            [
                button.text
                for row in update.callback_query.reply_markup.inline_keyboard
                for button in row
            ],
        )

    def test_stake_callback_falls_back_to_caption_edit_when_message_is_media(self):
        class FakeQuery:
            def __init__(self):
                self.data = "AUTO_TRADE_STAKE_0_1"
                self.caption_text = None
                self.text_text = None
                self.answer_calls = 0

            async def answer(self):
                self.answer_calls += 1

            async def edit_message_text(self, text, **kwargs):
                self.text_text = text
                raise RuntimeError("message is a media caption")

            async def edit_message_caption(self, caption, **kwargs):
                self.caption_text = caption

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "stake-media-user"})()

        update = FakeUpdate()

        with patch(
            "app.bot.callbacks.build_auto_trade_setup",
            return_value={
                "symbol": "step_500",
                "display_name": "Step Index 500",
                "direction": "BULLISH",
                "signal_strength": 0.92,
            },
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.account_service.get_trade_account",
            new=AsyncMock(
                return_value={
                    "account_type": "demo",
                    "account_id": "demo-1",
                    "balance": 5000,
                    "currency": "USD",
                }
            ),
        ), patch(
            "app.bot.callbacks.auto_trade_service.is_running",
            return_value=False,
        ), patch(
            "app.bot.callbacks.auto_trade_service.start_run",
            return_value=object(),
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 50},
            ),
        ):
            asyncio.run(dispatch_callback(update, None))

        query = update.callback_query
        self.assertIsNotNone(query.text_text)
        self.assertIsNotNone(query.caption_text)
        self.assertIn("TRADE CONFIRMATION", query.caption_text)
        self.assertIn(
            "Initial stake: 5 USD (0.1% of 5,000.00 USD balance)",
            query.caption_text,
        )

    def test_confirm_start_callback_falls_back_to_caption_edit_when_message_is_media(self):
        class FakeQuery:
            def __init__(self):
                self.caption_text = None
                self.text_text = None
                self.caption_history = []

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.text_text = text
                raise RuntimeError("message is a media caption")

            async def edit_message_caption(self, caption, **kwargs):
                self.caption_text = caption
                self.caption_history.append(caption)

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "confirm-media-user"})()

        run_state = AutoTradeService()
        state = run_state.start_run("confirm-media-user", "step_300", "BULLISH", 5.0)
        run_state.confirm_run("confirm-media-user")

        update = FakeUpdate()

        async def fake_execute_trade(_user_id, trade_state):
            trade_state.contract_id = "con-123"
            trade_state.market_symbol = "STPRNG3"
            trade_state.status = "monitoring"
            return {"status": "accepted"}

        with patch(
            "app.bot.callbacks.auto_trade_service",
            run_state,
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=fake_execute_trade),
        ), patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(
                return_value={
                    "status": "settled",
                    "contract_id": "con-123",
                    "profit": 1.0,
                    "entry_tick": 100.0,
                    "exit_tick": 101.0,
                }
            ),
        ), patch(
            "app.bot.callbacks.friendly_market_name",
            return_value="Step Index 300",
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                reserve_token=lambda *_args: {"ok": True, "token_balance": 49},
                record_settled_trade=lambda *_args: None,
                update_session=lambda *_args: None,
            ),
        ):
            run_state.advance_after_result = lambda user_id, won, profit: setattr(
                state,
                "status",
                "completed",
            ) or state
            asyncio.run(_run_auto_trade(update, state))

        query = update.callback_query
        self.assertIsNotNone(query.text_text)
        self.assertIsNotNone(query.caption_text)
        self.assertTrue(
            any("AUTO TRADE RUNNING" in caption for caption in query.caption_history)
        )

    def test_settled_contract_is_persisted_with_direction_accuracy(self):
        class FakeQuery:
            async def edit_message_text(self, text, **kwargs):
                self.text = text

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "history-user"})()

        class FakeTradeHistory:
            def __init__(self):
                self.trades = []
                self.sessions = []
                self.reservations = []

            def reserve_token(self, user_id, session_id):
                self.reservations.append((user_id, session_id))
                return {"ok": True, "token_balance": 49}

            def record_settled_trade(self, trade):
                self.trades.append(trade)

            def update_session(self, session):
                self.sessions.append(session)

        run_service = AutoTradeService()
        state = run_service.start_run(
            "history-user",
            "step_500",
            "BULLISH",
            5.0,
            max_cycles=1,
            account_mode="demo",
        )
        run_service.confirm_run("history-user")
        update = FakeUpdate()
        trade_history = FakeTradeHistory()

        async def fake_execute_trade(_user_id, trade_state):
            trade_state.contract_id = "contract-history-1"
            trade_state.market_symbol = "STPRNG5"
            trade_state.status = "monitoring"
            return {"status": "accepted"}

        with patch("app.bot.callbacks.auto_trade_service", run_service), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=fake_execute_trade),
        ), patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(
                return_value={
                    "status": "settled",
                    "contract_id": "contract-history-1",
                    "profit": 2.5,
                    "entry_tick": 100.0,
                    "exit_tick": 101.0,
                }
            ),
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            trade_history,
        ):
            asyncio.run(_run_auto_trade(update, state))

        self.assertEqual(len(trade_history.trades), 1)
        self.assertEqual(trade_history.trades[0]["telegram_user_id"], "history-user")
        self.assertEqual(trade_history.trades[0]["account_mode"], "demo")
        self.assertEqual(trade_history.trades[0]["market_symbol"], "STPRNG5")
        self.assertEqual(trade_history.trades[0]["session_id"], state.session_id)
        self.assertEqual(trade_history.trades[0]["stake"], 5.0)
        self.assertEqual(trade_history.trades[0]["tokens_spent"], 1)
        self.assertTrue(trade_history.trades[0]["direction_accurate"])
        self.assertEqual(trade_history.sessions[-1]["session_status"], "completed")

    def test_zero_tokens_mid_session_stops_before_another_trade(self):
        class FakeQuery:
            def __init__(self):
                self.edited = None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text

        class FakeUpdate:
            def __init__(self):
                self.callback_query = FakeQuery()
                self.effective_user = type("User", (), {"id": "depleted-user"})()

        class FakeTradeHistory:
            def __init__(self):
                self.sessions = []

            def reserve_token(self, *_args):
                return {
                    "ok": False,
                    "reason": "INSUFFICIENT_TOKENS",
                    "token_balance": 0,
                }

            def update_session(self, session):
                self.sessions.append(session)

        run_service = AutoTradeService()
        state = run_service.start_run(
            "depleted-user",
            "step_300",
            "BULLISH",
            1.0,
            account_mode="demo",
        )
        run_service.confirm_run("depleted-user")
        update = FakeUpdate()
        trade_history = FakeTradeHistory()

        with patch("app.bot.callbacks.auto_trade_service", run_service), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            trade_history,
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(
                return_value={
                    "status": "insufficient_tokens",
                    "reason": "INSUFFICIENT_TOKENS",
                }
            ),
        ) as execute_trade_mock:
            asyncio.run(_run_auto_trade(update, state))

        self.assertIn("out of Yields Tokens", update.callback_query.edited)
        execute_trade_mock.assert_awaited_once()
        self.assertEqual(trade_history.sessions[-1]["session_status"], "stopped")

    def test_execute_trade_reserves_token_before_deriv_purchase(self):
        class FakeTradeHistory:
            def __init__(self):
                self.reservations = []

            def reserve_token(self, user_id, session_id):
                self.reservations.append((user_id, session_id))
                return {"ok": True, "token_balance": 4}

        run_service = AutoTradeService()
        state = run_service.start_run(
            "reserve-before-buy-user",
            "step_300",
            "BULLISH",
            5.0,
            account_mode="demo",
        )
        run_service.confirm_run("reserve-before-buy-user")
        trade_history = FakeTradeHistory()

        with patch("app.bot.callbacks.auto_trade_service", run_service), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.account_service.get_trade_account",
            new=AsyncMock(
                return_value={"account_type": "demo", "account_id": "demo-1", "currency": "USD"}
            ),
        ), patch(
            "app.bot.callbacks.account_service.request_account_otp_url",
            new=AsyncMock(return_value="wss://example.test"),
        ), patch(
            "app.bot.callbacks.trade_history_service",
            trade_history,
        ), patch(
            "app.bot.callbacks.trade_execution_service.execute_trade",
            new=AsyncMock(
                return_value={
                    "status": "accepted",
                    "account_type": "demo",
                    "contract_id": "contract-reserved",
                    "proposal_id": "proposal-reserved",
                    "underlying_symbol": "STPRNG3",
                    "contract_type": "CALL",
                }
            ),
        ) as deriv_trade:
            result = asyncio.run(execute_trade("reserve-before-buy-user", state))

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(
            trade_history.reservations,
            [("reserve-before-buy-user", state.session_id)],
        )
        deriv_trade.assert_awaited_once()

    def test_trade_history_service_posts_idempotent_record_and_user_scoped_stats(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path.endswith("get_user_trade_stats"):
                return httpx.Response(
                    200,
                    json={"recent": [], "real": {}, "demo": {}},
                    request=request,
                )
            return httpx.Response(201, request=request)

        service = TradeHistoryService(
            supabase_url="https://project.supabase.co",
            service_role_key="test-service-role-key",
            transport=httpx.MockTransport(respond),
        )
        service.record_settled_trade(
            {
                "telegram_user_id": "12345",
                "contract_id": "contract-abc",
                "session_id": "11111111-1111-4111-8111-111111111111",
                "account_mode": "DEMO",
                "market_symbol": "STPRNG5",
                "direction": "BULLISH",
                "stake": 5,
                "profit": 2.5,
                "direction_accurate": True,
            }
        )
        result = service.get_user_stats("12345")

        self.assertEqual(requests[0].url.path, "/rest/v1/trade_history")
        self.assertEqual(
            requests[0].url.params.get("on_conflict"),
            "telegram_user_id,contract_id",
        )
        self.assertIn("resolution=ignore-duplicates", requests[0].headers["Prefer"])
        inserted = json.loads(requests[0].content)
        self.assertEqual(inserted["outcome"], "win")
        self.assertEqual(inserted["account_mode"], "demo")
        self.assertEqual(result["recent"], [])
        self.assertEqual(
            json.loads(requests[1].content),
            {"p_telegram_user_id": "12345"},
        )

    def test_stats_message_renders_recent_trades_and_account_summaries(self):
        from app.bot.handlers import handle_stats

        stats = {
            "recent": [
                {
                    "account_mode": "demo",
                    "market_symbol": "STPRNG5",
                    "direction": "BULLISH",
                    "net_profit": 5.19,
                    "session_status": "completed",
                    "trade_count": 7,
                    "winning_cycles": 5,
                },
            ],
            "token_balance": 43,
            "real": {
                "session_count": 0,
                "wins": 0,
                "win_rate": 0,
                "accuracy": 0,
                "net_pnl": 0,
                "tokens_spent": 0,
            },
            "demo": {
                "session_count": 13,
                "wins": 5,
                "win_rate": 100,
                "accuracy": 52,
                "net_pnl": 166.22,
                "tokens_spent": 0,
            },
        }

        with patch(
            "app.bot.handlers.trade_history_service",
            SimpleNamespace(
                is_configured=True,
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 43},
                get_user_stats=lambda _user_id: stats,
            ),
        ):
            text = asyncio.run(handle_stats("stats-user"))

        self.assertIn("LAST 10 SESSIONS", text)
        self.assertIn("Step Index 500", text)
        self.assertIn("+$5.19", text)
        self.assertIn("Token balance: 43", text)
        self.assertIn("5/13 sessions", text)
        self.assertIn("Win rate: 100.0%", text)
        self.assertIn("Direction accuracy: 52.0%", text)
        self.assertIn("Net PnL: +$166.22", text)

    def test_session_end_receipt_includes_balance_result_market_and_tokens(self):
        state = AutoTradeService().start_run(
            "receipt-user",
            "step_500",
            "BULLISH",
            5.0,
            max_cycles=5,
            account_mode="demo",
        )
        state.status = "completed"
        state.market_symbol = "STPRNG5"
        state.total_profit_loss = 6.68
        state.tokens_spent = 10

        with patch(
            "app.bot.callbacks.account_service.get_live_balances",
            new=AsyncMock(
                return_value={
                    "demo": {
                        "available": True,
                        "balance": 172.09,
                        "currency": "USD",
                    },
                }
            ),
        ), patch(
            "app.bot.callbacks.trade_history_service.get_user",
            return_value={"token_balance": 40},
        ):
            text = asyncio.run(_session_end_message("receipt-user", state))

        self.assertIn("Victory! Session completed", text)
        self.assertIn("Market: Step Index 500", text)
        self.assertIn("Account: DEMO", text)
        self.assertIn("Result: +$6.68 USD", text)
        self.assertIn("Balance: 172.09 USD", text)
        self.assertIn("Tokens used: 10", text)
        self.assertIn("Tokens remaining: 40", text)

    def test_deriv_oauth_success_message_includes_main_menu_button(self):
        class FakeBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)

        class FakeApp:
            def __init__(self):
                self.bot = FakeBot()

        fake_app = FakeApp()

        with patch("app.main.auth_service.validate_state", return_value=True), \
             patch("app.main.auth_service.get_pending_user_for_state", return_value="telegram-user-42"), \
             patch("app.main.auth_service.exchange_oauth_code", return_value={"access_token": "token"}), \
             patch("app.main.get_bot_application", return_value=fake_app):
            response = asyncio.run(
                deriv_oauth_callback(
                    code="auth-code",
                    state="state-value",
                    error=None,
                )
            )

        self.assertIn("Deriv Account Connected Successfully", response)
        self.assertIn("Open Dashboard", response)

        sent_message = fake_app.bot.sent[0]
        self.assertIn("reply_markup", sent_message)
        self.assertIsNotNone(sent_message["reply_markup"])

    def test_user_messages_avoid_slash_commands_and_expose_faq_stats(self):
        from app.bot.handlers import handle_faq, handle_logout, handle_stats, handle_status

        connected_text = handle_status("status-user")
        disconnected_text = handle_logout("logout-user")
        faq_text = handle_faq()
        with patch(
            "app.bot.handlers.trade_history_service",
            SimpleNamespace(
                is_configured=True,
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 50},
                get_user_stats=lambda _user_id: {
                    "token_balance": 50,
                    "recent": [],
                    "real": {},
                    "demo": {},
                },
            ),
        ):
            stats_text = asyncio.run(handle_stats("status-user"))

        self.assertNotIn("/connect", connected_text)
        self.assertNotIn("/status", connected_text)
        self.assertNotIn("/connect", disconnected_text)
        self.assertIn("FAQ", faq_text.upper())
        self.assertIn("STATS", stats_text.upper())

    def test_oauth_state_and_pkce_flow(self):
        service = AuthService()
        session = service.begin_oauth_session("telegram-user-42")

        self.assertIn("state", session)
        self.assertIn("code_verifier", session)
        self.assertIn("code_challenge", session)
        self.assertTrue(
            service.validate_state(
                "telegram-user-42",
                session["state"],
            )
        )

        url = build_deriv_oauth_url(
            "my-client-id",
            "https://example.com/auth/deriv/callback",
            "trade account_manage",
            session["state"],
            session["code_challenge"],
        )

        self.assertIn("response_type=code", url)
        self.assertIn("state=" + session["state"], url)
        self.assertIn("code_challenge=" + session["code_challenge"], url)

    def test_oauth_code_exchange_uses_pkce_verifier(self):
        service = AuthService()
        session = service.begin_oauth_session("telegram-user-99")

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "access_token": "real-access-token",
                    "refresh_token": "real-refresh-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }

        with patch(
            "app.services.auth_service.httpx.post",
            return_value=FakeResponse(),
        ) as post_mock:
            tokens = service.exchange_oauth_code(
                "telegram-user-99",
                "auth-code-123",
                "https://example.com/auth/deriv/callback",
                "my-client-id",
            )

        self.assertEqual(tokens["access_token"], "real-access-token")
        self.assertEqual(tokens["refresh_token"], "real-refresh-token")
        self.assertEqual(
            post_mock.call_args.kwargs["data"]["code"],
            "auth-code-123",
        )
        self.assertEqual(
            post_mock.call_args.kwargs["data"]["code_verifier"],
            session["code_verifier"],
        )

    def test_oauth_completion_resolves_account_id_from_deriv_accounts(self):
        service = AuthService()

        class FakeAccountsResponse:
            status_code = 200

            def json(self):
                return {
                    "data": [
                        {
                            "account_id": "real-123",
                            "account_type": "real",
                            "currency": "USD",
                            "balance": 250.0,
                        },
                        {
                            "account_id": "demo-456",
                            "account_type": "demo",
                            "currency": "USD",
                            "balance": 500.0,
                        },
                    ]
                }

        with patch(
            "app.services.auth_service.httpx.get",
            return_value=FakeAccountsResponse(),
        ):
            tokens = service.complete_oauth_session(
                "telegram-user-real",
                {
                    "access_token": "oauth-token",
                    "refresh_token": "oauth-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )

        self.assertEqual(tokens["account_id"], "real-123")
        self.assertEqual(tokens["account_type"], "real")
        self.assertEqual(tokens["currency"], "USD")

    def test_account_status_and_demo_trading_flow(self):
        auth = AuthService()
        auth.connect_user(
            "telegram-user-99",
            {
                "access_token": "abc",
                "refresh_token": "def",
                "expires_in": 3600,
                "account_id": "demo-account-001",
                "account_type": "Demo",
                "currency": "USD",
                "balance": 10000.00,
            },
        )

        account_service = AccountService(auth)
        account = account_service.get_account_status("telegram-user-99")

        self.assertTrue(account["connected"])
        self.assertEqual(account["type"], "Demo")
        self.assertEqual(account["currency"], "USD")

        trading = TradingService()
        proposal = trading.create_proposal("STPRNG", amount=10.0)

        self.assertIn("proposal_id", proposal)
        self.assertEqual(proposal["currency"], "USD")

        buy = trading.buy_contract(
            proposal["proposal_id"],
            float(proposal["ask_price"]),
            "STPRNG",
        )

        self.assertEqual(buy["status"], "accepted")
        self.assertIn("contract_id", buy)

    def test_trading_service_uses_deriv_websocket_protocol(self):
        class MockAuthService:
            def get_valid_access_token(self, user_id):
                return "mock-access-token"

        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.responses = iter(
                    [
                        json.dumps(
                            {
                                "echo_req": {
                                    "authorize": "mock-access-token",
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "proposal": {
                                    "id": "proposal-123",
                                    "ask_price": 1.25,
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "buy": {
                                    "contract_id": "contract-123",
                                    "buy_price": 1.25,
                                    "balance_after": 1000.0,
                                    "shortcode": "CALL",
                                }
                            }
                        ),
                    ]
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def recv(self):
                return next(self.responses)

        trading = TradingService(MockAuthService())

        with patch(
            "app.services.trading_service.websockets.connect",
            return_value=FakeWebSocket(),
        ) as connect_mock:
            result = asyncio.run(
                trading.execute_trade_cycles(
                    "telegram-user-200",
                    "step_100",
                    "CALL",
                    10.0,
                    30,
                    1,
                )
            )

        self.assertEqual(
            connect_mock.call_args.args[0],
            trading.get_deriv_ws_url(),
        )
        self.assertEqual(result[0]["contract_id"], "contract-123")
        self.assertEqual(result[0]["symbol"], "STPRNG")
        self.assertEqual(result[0]["contract_type"], "CALL")
        self.assertEqual(trading.resolve_contract_type("CALL"), "CALL")

    def test_live_balance_fetch_uses_deriv_otp_websocket_flow(self):
        async def run_test():
            auth = AuthService()
            auth.connect_user(
                "telegram-user-live-balance",
                {
                    "access_token": "abc",
                    "refresh_token": "def",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "account_id": "demo-account-001",
                    "account_type": "Demo",
                    "currency": "USD",
                    "balance": 1234.56,
                },
            )

            class FakeAccountsResponse:
                status_code = 200

                def json(self):
                    return {
                        "data": [
                            {
                                "account_id": "demo-001",
                                "account_type": "demo",
                                "currency": "USD",
                                "balance": 10000.0,
                            },
                            {
                                "account_id": "real-001",
                                "account_type": "real",
                                "currency": "USD",
                                "balance": 245.75,
                            },
                        ]
                    }

            class FakeOtpResponse:
                def __init__(self, url):
                    self.status_code = 200
                    self._url = url
                    self.text = ""

                def json(self):
                    return {"data": {"url": self._url}}

            class FakeAsyncClient:
                def __init__(self):
                    self.calls = []

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return None

                async def get(self, url, headers=None):
                    self.calls.append(("GET", url, headers))
                    return FakeAccountsResponse()

                async def post(self, url, headers=None):
                    self.calls.append(("POST", url, headers))
                    if url.endswith("/demo-001/otp"):
                        return FakeOtpResponse(
                            "wss://demo.example/ws?otp=demo"
                        )
                    if url.endswith("/real-001/otp"):
                        return FakeOtpResponse(
                            "wss://real.example/ws?otp=real"
                        )
                    return FakeOtpResponse(
                        "wss://demo.example/ws?otp=demo"
                    )

            class FakeSocket:
                def __init__(self, url):
                    self.url = url
                    self.sent = []

                    if "demo" in url:
                        balance = 10000.0
                    else:
                        balance = 245.75

                    self._recv = [
                        json.dumps(
                            {
                                "msg_type": "balance",
                                "balance": {
                                    "currency": "USD",
                                    "balance": balance,
                                },
                            }
                        )
                    ]

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return None

                async def send(self, payload):
                    self.sent.append(json.loads(payload))

                async def recv(self):
                    return self._recv.pop(0)

            fake_demo_socket = FakeSocket(
                "wss://demo.example/ws?otp=demo"
            )
            fake_real_socket = FakeSocket(
                "wss://real.example/ws?otp=real"
            )

            with patch(
                "app.services.account_service.httpx.AsyncClient",
                return_value=FakeAsyncClient(),
            ), patch(
                "app.services.account_service.websockets.connect",
                side_effect=[
                    fake_demo_socket,
                    fake_real_socket,
                ],
            ):
                balances = await AccountService(auth).get_live_balances(
                    "telegram-user-live-balance"
                )

            self.assertEqual(
                fake_demo_socket.sent[0],
                {
                    "balance": 1,
                    "subscribe": 1,
                    "req_id": 1,
                },
            )
            self.assertEqual(
                fake_real_socket.sent[0],
                {
                    "balance": 1,
                    "subscribe": 1,
                    "req_id": 1,
                },
            )
            self.assertEqual(balances["demo"]["balance"], 10000.0)
            self.assertEqual(balances["real"]["balance"], 245.75)
            self.assertEqual(balances["tokens"], 50)

        asyncio.run(run_test())

    def test_fastapi_app_registers_startup_hook(self):
        self.assertTrue(app.router.on_startup)

    def test_unauthenticated_user_sees_onboarding_screen(self):
        message = handle_start("missing-user")

        self.assertIn("🤖 YIELDS OPTIONS BOT", message)
        self.assertIn("Connect your Deriv account", message)
        self.assertIn("Connect securely through Deriv OAuth", message)

    def test_authenticated_user_sees_dashboard(self):
        auth = AuthService()
        auth.connect_user(
            "dashboard-user",
            {
                "access_token": "abc123",
                "refresh_token": "def456",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        message = handle_start("dashboard-user")

        self.assertIn("Welcome back", message)
        self.assertNotIn("AI scans 5 Step Indices", message)
        self.assertNotIn("Expiry: 30 seconds", message)
        self.assertNotIn("AUTO TRADE", message)

    def test_exactly_five_step_markets_are_normalized(self):
        scanner = YieldsScanner()
        with patch.object(
            MarketDataService,
            "get_historical_ticks",
            return_value=_sample_deriv_ticks(),
        ):
            snapshots = scanner.collect_step_market_snapshots(MarketDataService())

        self.assertEqual(len(snapshots), 5)
        self.assertEqual(
            [snapshot.symbol for snapshot in snapshots],
            list(settings.auto_trade_markets),
        )

    def test_fixed_expiry_is_always_thirty_seconds(self):
        payload = TradeExecutionService.build_proposal_payload(
            "step_300",
            "BULLISH",
            10.0,
            expiry_seconds=30,
        )

        self.assertEqual(payload["duration"], 30)
        self.assertEqual(payload["duration_unit"], "s")

    def test_proposal_payload_uses_underlying_symbol(self):
        payload = TradeExecutionService.build_proposal_payload(
            "step_300",
            "BULLISH",
            10.0,
            expiry_seconds=30,
        )

        self.assertEqual(payload["underlying_symbol"], "STPRNG3")
        self.assertNotIn("symbol", payload)
        self.assertEqual(payload["contract_type"], "CALL")
        self.assertEqual(payload["duration"], 30)
        self.assertEqual(payload["duration_unit"], "s")

    def test_bearish_direction_maps_to_put(self):
        payload = TradeExecutionService.build_proposal_payload(
            "step_300",
            "BEARISH",
            10.0,
            expiry_seconds=30,
        )

        self.assertEqual(payload["underlying_symbol"], "STPRNG3")
        self.assertEqual(payload["contract_type"], "PUT")

    def test_martingale_state_machine(self):
        service = AutoTradeService()
        relative_stake = calculate_relative_stake(10_000, "0_1")
        run = service.start_run(
            "martingale-user",
            "step_300",
            "BULLISH",
            relative_stake,
        )

        self.assertEqual(run.current_stake, relative_stake)

        service.advance_after_result("martingale-user", False)
        self.assertEqual(
            service.get_run("martingale-user").current_stake,
            23.0,
        )

        service.advance_after_result("martingale-user", False)
        self.assertEqual(
            service.get_run("martingale-user").current_stake,
            52.9,
        )

        service.advance_after_result("martingale-user", True)
        self.assertEqual(
            service.get_run("martingale-user").cycle,
            2,
        )
        self.assertEqual(
            service.get_run("martingale-user").current_stake,
            relative_stake,
        )

    def test_duplicate_auto_trade_start_is_rejected(self):
        service = AutoTradeService()
        service.start_run(
            "dup-user",
            "step_200",
            "BEARISH",
            5.0,
        )

        with self.assertRaises(RuntimeError):
            service.start_run(
                "dup-user",
                "step_200",
                "BEARISH",
                5.0,
            )

    def test_auto_trade_run_can_only_be_confirmed_once(self):
        service = AutoTradeService()
        run = service.start_run("single-confirm-user", "step_100", "BULLISH", 1.0)

        self.assertEqual(run.status, "awaiting_confirmation")
        service.confirm_run("single-confirm-user")
        self.assertEqual(run.status, "running")
        with self.assertRaises(RuntimeError):
            service.confirm_run("single-confirm-user")


if __name__ == "__main__":
    unittest.main()