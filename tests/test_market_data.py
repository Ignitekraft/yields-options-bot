import asyncio
import base64
import hashlib
import hmac
import importlib
import json
import math
import os
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from cryptography.fernet import Fernet

from app.bot.callbacks import (
    _auto_trade_failure_message,
    _auto_trade_start_failure_message,
    _payout_failure_reason_for_user,
    _handle_auto_trade_entry,
    _run_auto_trade,
    _run_auto_trade_loop,
    handle_custom_stake_input,
    parse_custom_stake,
    _send_low_token_message,
    _send_session_end_message,
    _show_auto_trade_setup,
    _setup_photo_url,
    _session_end_message,
    handle_chart_screenshot,
    calculate_relative_stake,
    dispatch_callback,
    execute_trade,
)
from app.bot.handlers import (
    _friendly_market_issue,
    build_menu_message,
    build_auto_trade_setup,
    build_start_message,
    build_token_purchase_message,
    build_token_store_message,
    TOKEN_PACKAGES,
    TOKEN_STORE_IMAGE_URL,
    handle_auto_trade_start,
    handle_start,
)
from app.bot.keyboards import TelegramKeyboard
from app.bot.telegram_bot import (
    _handle_unexpected_error,
    _send_menu_message,
    _send_tokens_message,
)
from app.config import settings
from app.main import app, deriv_oauth_callback, verify_whop_webhook_signature
from app.models.market import MarketSnapshot
from app.services.account_service import AccountService
from app.services.auth_service import AuthService
from app.services.auto_trade_service import AutoTradeService
from app.services.chart_analysis import (
    ChartAnalysis,
    ChartAnalysisError,
    GeminiChartAnalysisProvider,
)
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.deriv.websocket_connection import (
    connect_async_with_retry,
    connect_sync_with_retry,
)
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


def _sample_one_minute_candles(count: int = 60) -> list[dict]:
    current_epoch = int(time.time())
    start_epoch = current_epoch - current_epoch % 60 - (count - 1) * 60
    candles = []
    for index in range(count):
        close = 100 + index * 0.01
        candles.append(
            {
                "epoch": start_epoch + index * 60,
                "open": close - 0.005,
                "high": close + 0.01,
                "low": close - 0.01,
                "close": close,
            }
        )
    return candles


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        from app.services.deriv.websocket_connection import (
            _DerivRequestLimiter,
        )

        limiter = _DerivRequestLimiter(4)
        limiter.interval_seconds = 0
        self.rate_limiter_patch = patch(
            "app.services.deriv.websocket_connection._deriv_request_limiter",
            limiter,
        )
        self.rate_limiter_patch.start()
        self.addCleanup(self.rate_limiter_patch.stop)

    def test_auto_trade_failure_messages_are_user_friendly(self):
        rate_limit_message = _auto_trade_failure_message(
            "rejected",
            "RateLimit: Too many requests",
        )
        payout_message = _auto_trade_failure_message(
            "rejected",
            "ContractBuyValidationError: maximum payout exceeded",
            "ContractBuyValidationError",
        )
        generic_message = _auto_trade_failure_message(
            "rejected",
            "INVALID_CONTRACT_SPECIFICATION",
        )

        self.assertIn("temporarily limiting requests", rate_limit_message)
        self.assertNotIn("RateLimit", rate_limit_message)
        self.assertIn("maximum payout", payout_message)
        self.assertNotIn("ContractBuyValidationError", payout_message)
        self.assertNotIn("INVALID_CONTRACT_SPECIFICATION", generic_message)
        self.assertIn("stopped safely", generic_message)

    def test_auto_trade_start_failure_messages_hide_internal_codes(self):
        duplicate = _auto_trade_start_failure_message(
            RuntimeError("AUTO_TRADE_ALREADY_RUNNING")
        )
        unavailable = _auto_trade_start_failure_message(
            RuntimeError("AUTO_TRADE_NOT_CONFIRMABLE")
        )

        self.assertIn("already active", duplicate)
        self.assertNotIn("AUTO_TRADE_ALREADY_RUNNING", duplicate)
        self.assertIn("could not start", unavailable)
        self.assertNotIn("AUTO_TRADE_NOT_CONFIRMABLE", unavailable)

    def test_payout_failure_messages_hide_technical_details(self):
        technical = _payout_failure_reason_for_user(
            "HTTP 502: PaymentProviderError at https://example.test/payout"
        )
        readable = _payout_failure_reason_for_user(
            "The account name does not match the bank records."
        )

        self.assertNotIn("HTTP 502", technical)
        self.assertNotIn("PaymentProviderError", technical)
        self.assertIn("payment partner could not complete", technical)
        self.assertEqual(readable, "The account name does not match the bank records.")

    def test_market_scan_failure_reasons_are_user_friendly(self):
        limited = _friendly_market_issue(
            "Market data request failed: WebSocket handshake rejected (HTTP 429)"
        )
        unavailable = _friendly_market_issue(
            "Market data request failed: TimeoutError"
        )

        self.assertIn("temporarily limiting", limited)
        self.assertNotIn("HTTP 429", limited)
        self.assertNotIn("TimeoutError", unavailable)
        self.assertIn("try again shortly", unavailable)

    def test_auto_trade_keyboard_displays_locked_countdown(self):
        dashboard = TelegramKeyboard.dashboard_keyboard(125)
        menu = TelegramKeyboard.menu_keyboard("https://t.me/community", 125)

        self.assertEqual(
            dashboard.inline_keyboard[0][0].text,
            "🔒 Auto Trade · 2m 05s",
        )
        self.assertEqual(
            dashboard.inline_keyboard[0][0].callback_data,
            "AUTO_TRADE_LIMIT_INFO",
        )
        self.assertEqual(menu.inline_keyboard[0][0].text, "🔒 Auto Trade · 2m 05s")
        self.assertEqual(
            menu.inline_keyboard[0][0].callback_data,
            "AUTO_TRADE_LIMIT_INFO",
        )

    def test_auto_trade_entry_shows_global_limit_and_does_not_scan(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id="limited-user")
        )

        class FakeQuery:
            def __init__(self):
                self.message = SimpleNamespace(
                    reply_markup=TelegramKeyboard.menu_keyboard(
                        "https://t.me/community"
                    )
                )
                self.alert = None
                self.updated_markup = None

            async def answer(self, text, **_kwargs):
                self.alert = text

            async def edit_message_reply_markup(self, **kwargs):
                self.updated_markup = kwargs["reply_markup"]

        query = FakeQuery()
        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": True,
                    "retry_after_seconds": 125,
                }
            ),
        ), patch(
            "app.bot.callbacks._get_trade_token_balance",
            new=AsyncMock(),
        ) as token_balance, patch(
            "app.bot.callbacks._show_auto_trade_setup",
            new=AsyncMock(),
        ) as scan_setup:
            asyncio.run(_handle_auto_trade_entry(update, query))

        self.assertIn("10 sessions in a rolling hour", query.alert)
        self.assertIn("2m 05s", query.alert)
        self.assertEqual(
            query.updated_markup.inline_keyboard[0][0].text,
            "🔒 Auto Trade · 2m 05s",
        )
        token_balance.assert_not_awaited()
        scan_setup.assert_not_awaited()

    def test_unhandled_telegram_errors_hide_technical_details(self):
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text),
            callback_query=None,
        )
        context = SimpleNamespace(
            error=RuntimeError("DATABASE_PASSWORD=private"),
        )

        asyncio.run(_handle_unexpected_error(update, context))

        shown_message = reply_text.await_args.args[0]
        self.assertIn("couldn't be processed", shown_message)
        self.assertNotIn("DATABASE_PASSWORD", shown_message)
        self.assertNotIn("private", shown_message)

    def test_unhandled_callback_errors_show_alert_without_chat_message(self):
        answer = AsyncMock()
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text),
            callback_query=SimpleNamespace(
                data="AUTO_TRADE_CONFIRM",
                answer=answer,
            ),
        )
        context = SimpleNamespace(error=RuntimeError("private technical detail"))

        asyncio.run(_handle_unexpected_error(update, context))

        answer.assert_awaited_once_with(
            "Auto Trade couldn't finish that step. Check your Deriv account "
            "before trying again.",
            show_alert=True,
        )
        reply_text.assert_not_awaited()

    def test_failed_callback_alert_does_not_post_a_separate_chat_message(self):
        answer = AsyncMock(side_effect=RuntimeError("query is no longer valid"))
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text),
            callback_query=SimpleNamespace(data="AUTO_TRADE_CONFIRM", answer=answer),
        )

        asyncio.run(
            _handle_unexpected_error(
                update,
                SimpleNamespace(error=RuntimeError("trade callback failed")),
            )
        )

        answer.assert_awaited_once()
        reply_text.assert_not_awaited()

    def test_auto_trade_callback_failure_is_contained_with_a_friendly_alert(self):
        answer = AsyncMock()
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                data="AUTO_TRADE_CONFIRM",
                answer=answer,
            ),
            effective_user=SimpleNamespace(id="callback-error-user"),
        )

        with patch(
            "app.bot.callbacks._dispatch_callback",
            new=AsyncMock(side_effect=RuntimeError("private technical detail")),
        ):
            asyncio.run(dispatch_callback(update, SimpleNamespace()))

        answer.assert_awaited_once_with(
            "Auto Trade couldn't finish that step. Check your Deriv account "
            "before trying again.",
            show_alert=True,
        )

    def test_oauth_errors_are_friendly_and_not_reflected_as_html(self):
        denied = asyncio.run(
            deriv_oauth_callback(
                code=None,
                state=None,
                error='<script>alert("x")</script>',
            )
        )
        self.assertIn("connection was not completed", denied)
        self.assertNotIn("<script>", denied)

        with (
            patch(
                "app.main.auth_service.get_pending_user_for_state",
                return_value="oauth-failure-user",
            ),
            patch("app.main.auth_service.validate_state", return_value=True),
            patch(
                "app.main.auth_service.exchange_oauth_code",
                side_effect=RuntimeError("private provider response"),
            ),
        ):
            failed_exchange = asyncio.run(
                deriv_oauth_callback(
                    code="valid-code",
                    state="valid-state",
                    error=None,
                )
            )

        self.assertIn("could not complete the connection", failed_exchange)
        self.assertNotIn("private provider response", failed_exchange)
        self.jitter_patch = patch(
            "app.services.deriv.websocket_connection.random.uniform",
            return_value=0.0,
        )
        self.jitter_patch.start()
        self.addCleanup(self.jitter_patch.stop)

    def test_websocket_handshake_retries_http_429_with_retry_after(self):
        class RateLimited(Exception):
            def __init__(self):
                self.response = SimpleNamespace(
                    status_code=429,
                    headers={"Retry-After": "0"},
                )

        async def check_async_retry():
            websocket = object()
            connect = AsyncMock(side_effect=[RateLimited(), websocket])
            sleep = AsyncMock()
            with patch(
                "app.services.deriv.websocket_connection.asyncio.sleep",
                sleep,
            ), patch(
                "app.services.deriv.websocket_connection.random.uniform",
                return_value=0.0,
            ):
                result = await connect_async_with_retry(connect, "wss://example.com")
            self.assertIs(result, websocket)
            self.assertEqual(connect.await_count, 2)
            sleep.assert_not_awaited()

        async def check_sync_retry():
            connection = object()
            connect = Mock(side_effect=[RateLimited(), connection])
            with patch(
                "app.services.deriv.websocket_connection.random.uniform",
                return_value=0.0,
            ), patch(
                "app.services.deriv.websocket_connection.time.sleep"
            ) as sleep:
                result = connect_sync_with_retry(connect, "wss://example.com")
            self.assertIs(result, connection)
            self.assertEqual(connect.call_count, 2)
            sleep.assert_not_called()

        asyncio.run(check_async_retry())
        asyncio.run(check_sync_retry())

    def test_deriv_connection_starts_are_paced_across_concurrent_calls(self):
        from app.services.deriv import websocket_connection

        async def check_pacing():
            timestamps = []

            async def connect(_url):
                timestamps.append(time.monotonic())
                return object()

            limiter = websocket_connection._DerivRequestLimiter(4)
            limiter.interval_seconds = 0.02
            with patch.object(
                websocket_connection,
                "_deriv_request_limiter",
                limiter,
            ), patch(
                "app.services.deriv.websocket_connection.random.uniform",
                return_value=0.0,
            ):
                await asyncio.gather(
                    connect_async_with_retry(connect, "wss://example.com/one"),
                    connect_async_with_retry(connect, "wss://example.com/two"),
                )

            self.assertEqual(len(timestamps), 2)
            self.assertGreaterEqual(timestamps[1] - timestamps[0], 0.015)

        asyncio.run(check_pacing())

    def test_deriv_request_limiter_enforces_documented_shared_budgets(self):
        from app.services.deriv.websocket_connection import (
            REST_ACCOUNT_REQUEST_LIMITS,
            REST_IP_REQUEST_LIMITS,
            WEBSOCKET_REQUEST_LIMITS,
            _DerivRequestLimiter,
            _websocket_request_group,
        )

        limiter = _DerivRequestLimiter(0)
        scopes = (("websocket:trading", ((60.0, 2),)),)
        with patch(
            "app.services.deriv.websocket_connection.time.monotonic",
            return_value=100.0,
        ):
            self.assertEqual(limiter._reserve_budget(scopes), 0.0)
            self.assertEqual(limiter._reserve_budget(scopes), 0.0)
            self.assertEqual(limiter._reserve_budget(scopes), 60.0)

        self.assertEqual(WEBSOCKET_REQUEST_LIMITS["trading"], ((60.0, 360), (3600.0, 14400)))
        self.assertEqual(WEBSOCKET_REQUEST_LIMITS["account"], ((60.0, 100), (3600.0, 2000)))
        self.assertEqual(WEBSOCKET_REQUEST_LIMITS["portfolio"], ((60.0, 30), (3600.0, 1500)))
        self.assertEqual(WEBSOCKET_REQUEST_LIMITS["other"], ((60.0, 220), (3600.0, 14400)))
        self.assertEqual(REST_IP_REQUEST_LIMITS, ((60.0, 300), (600.0, 1000)))
        self.assertEqual(REST_ACCOUNT_REQUEST_LIMITS, ((60.0, 80),))
        self.assertEqual(_websocket_request_group({"proposal": 1}), "trading")
        self.assertEqual(
            _websocket_request_group({"proposal_open_contract": 1}),
            "trading",
        )
        self.assertEqual(_websocket_request_group({"balance": 1}), "account")
        self.assertEqual(_websocket_request_group({"portfolio": 1}), "portfolio")
        self.assertEqual(_websocket_request_group({"ticks": "STPRNG"}), "other")

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
        self.assertGreater(bullish.indicators["directional_score"], 0)
        self.assertLess(bearish.indicators["directional_score"], 0)
        self.assertIn("directional bias", bullish.indicators["signal_reason"])

    def test_scanner_reports_weak_momentum_and_abnormal_volatility_as_context(self):
        scanner = YieldsScanner()

        weak_momentum_ticks = _sample_deriv_ticks()
        for tick in weak_momentum_ticks[-4:]:
            tick["quote"] = weak_momentum_ticks[-5]["quote"]
        weak_momentum = scanner.normalize_step_market(
            "step_100",
            "Step Index 100",
            weak_momentum_ticks,
        )
        self.assertNotEqual(weak_momentum.trend, "neutral")
        self.assertFalse(weak_momentum.indicators["momentum_confirmed"])
        self.assertNotIn(
            "recent momentum is too weak",
            weak_momentum.indicators["signal_reason"],
        )

        volatility_spike_ticks = _sample_deriv_ticks()
        volatility_spike_ticks[-1]["quote"] += 1.0
        volatility_spike = scanner.normalize_step_market(
            "step_100",
            "Step Index 100",
            volatility_spike_ticks,
        )
        self.assertNotEqual(volatility_spike.trend, "neutral")
        self.assertFalse(volatility_spike.indicators["volatility_in_range"])
        self.assertNotIn(
            "volatility outside the accepted range",
            volatility_spike.indicators["signal_reason"],
        )

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
                "wss://api.derivws.com/trading/v1/options/ws/public",
            ).get_historical_ticks("step_100", count=5000)

        connect_mock.assert_called_once_with(
            "wss://api.derivws.com/trading/v1/options/ws/public",
            open_timeout=10,
            close_timeout=2,
        )
        self.assertEqual([tick["quote"] for tick in history], [100.1, 100.2])
        self.assertEqual([tick["epoch"] for tick in history], [now - 1, now])
        self.assertEqual(socket.sent[0]["ticks_history"], "STPRNG")
        self.assertEqual(socket.sent[0]["count"], 5000)
        self.assertEqual(socket.sent[0]["style"], "ticks")
        self.assertEqual(socket.sent[0]["end"], "latest")

    def test_legacy_market_data_endpoint_is_upgraded_to_public_gateway(self):
        service = MarketDataService(
            "wss://ws.binaryws.com/websockets/v3?app_id=old-app-id",
        )

        self.assertEqual(
            service._connection_url(),
            "wss://api.derivws.com/trading/v1/options/ws/public",
        )

    def test_historical_candles_use_documented_granularity_request(self):
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
                        "candles": [
                            {
                                "epoch": now - 60,
                                "open": "100.0",
                                "high": "101.0",
                                "low": "99.0",
                                "close": "100.5",
                            }
                        ]
                    }
                )

        socket = FakeSocket()
        with patch(
            "app.services.deriv.market_data.websocket_client.connect",
            return_value=socket,
        ) as connect_mock:
            candles = MarketDataService().get_historical_candles(
                "step_100",
                count=60,
                granularity=60,
            )

        connect_mock.assert_called_once_with(
            "wss://api.derivws.com/trading/v1/options/ws/public",
            open_timeout=10,
            close_timeout=2,
        )
        self.assertEqual(
            socket.sent[0],
            {
                "ticks_history": "STPRNG",
                "count": 60,
                "end": "latest",
                "style": "candles",
                "granularity": 60,
                "req_id": 1,
            },
        )
        self.assertEqual(candles[0]["close"], 100.5)

    def test_batch_market_history_reuses_one_public_connection(self):
        now = int(time.time())

        class FakeSocket:
            def __init__(self):
                self.sent = []
                self.responses = iter(
                    [
                        {"history": {"prices": ["100.1"], "times": [now]}},
                        {
                            "candles": [
                                {
                                    "epoch": now - 60,
                                    "open": "100",
                                    "high": "101",
                                    "low": "99",
                                    "close": "100.5",
                                }
                            ]
                        },
                        {"history": {"prices": ["200.1"], "times": [now]}},
                        {
                            "candles": [
                                {
                                    "epoch": now - 60,
                                    "open": "200",
                                    "high": "201",
                                    "low": "199",
                                    "close": "200.5",
                                }
                            ]
                        },
                    ]
                )

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def send(self, payload):
                self.sent.append(json.loads(payload))

            def recv(self, timeout=None):
                return json.dumps(next(self.responses))

        socket = FakeSocket()
        with patch(
            "app.services.deriv.market_data.websocket_client.connect",
            return_value=socket,
        ) as connect_mock:
            results = MarketDataService().get_step_market_data_batch(
                ["step_100", "step_200"],
                tick_count=1000,
                candle_count=60,
            )

        connect_mock.assert_called_once()
        self.assertEqual(len(socket.sent), 4)
        self.assertEqual(
            [request["req_id"] for request in socket.sent],
            [1, 2, 3, 4],
        )
        self.assertEqual(results["step_100"][0][0]["quote"], 100.1)
        self.assertEqual(results["step_100"][1][0]["close"], 100.5)
        self.assertEqual(results["step_200"][0][0]["quote"], 200.1)
        self.assertEqual(results["step_200"][1][0]["close"], 200.5)

    def test_active_symbols_uses_current_underlying_symbol_fields(self):
        service = MarketDataService()
        with patch.object(
            service,
            "_request_public",
            return_value={
                "active_symbols": [
                    {
                        "underlying_symbol": "STPRNG",
                        "underlying_symbol_name": "Step Index 100",
                    },
                    {"underlying_symbol": "STPRNG2"},
                    {"display_name": "Missing symbol"},
                ]
            },
        ) as request_public:
            symbols = service.get_active_symbols()

        request_public.assert_called_once_with({"active_symbols": "brief"})
        self.assertEqual(
            symbols,
            [
                {
                    "symbol": "STPRNG",
                    "display_name": "Step Index 100",
                },
                {
                    "symbol": "STPRNG2",
                    "display_name": "STPRNG2",
                },
            ],
        )

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
        self.assertEqual(result["entry_tick"], 100)
        self.assertEqual(result["exit_tick"], 101)

    def test_monitor_contract_publishes_final_expiry_countdown(self):
        service = TradeExecutionService()
        countdowns = []

        class FakeWebSocket:
            def __init__(self):
                self.responses = iter(
                    [
                        json.dumps(
                            {
                                "proposal_open_contract": {
                                    "contract_id": 123456789,
                                    "is_sold": False,
                                    "date_expiry": 1003,
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "proposal_open_contract": {
                                    "contract_id": 123456789,
                                    "is_sold": True,
                                    "profit": 1.0,
                                    "payout": 2.0,
                                    "buy_price": 1.0,
                                    "sell_price": 2.0,
                                    "entry_tick": 100,
                                    "exit_tick": 101,
                                }
                            }
                        ),
                    ]
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def send(self, _payload):
                return None

            async def recv(self):
                response = next(self.responses)
                if response.find('"is_sold": true') >= 0:
                    await asyncio.sleep(0)
                return response

        async def on_countdown(seconds):
            countdowns.append(seconds)

        with patch(
            "app.services.trade_execution_service.websockets.connect",
            return_value=FakeWebSocket(),
        ), patch(
            "app.services.trade_execution_service.time.time",
            return_value=1000,
        ):
            asyncio.run(
                service.monitor_contract(
                    "123456789",
                    "wss://example.com",
                    timeout_seconds=1,
                    on_countdown=on_countdown,
                )
            )

        self.assertEqual(countdowns, [3])

    def test_monitor_contract_runs_settlement_handoff_before_returning(self):
        service = TradeExecutionService()
        handoff_calls = []

        class FakeWebSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def send(self, _payload):
                return None

            async def recv(self):
                return json.dumps(
                    {
                        "proposal_open_contract": {
                            "contract_id": 123456789,
                            "is_sold": True,
                            "profit": 1.0,
                            "entry_tick": 100,
                            "exit_tick": 101,
                        }
                    }
                )

        websocket = FakeWebSocket()

        async def on_settlement(settlement, connected_websocket):
            handoff_calls.append((settlement["contract_id"], connected_websocket))
            return {"status": "accepted"}

        with patch(
            "app.services.trade_execution_service.websockets.connect",
            return_value=websocket,
        ):
            result = asyncio.run(
                service.monitor_contract(
                    "123456789",
                    "wss://example.com",
                    timeout_seconds=1,
                    on_settlement=on_settlement,
                )
            )

        self.assertEqual(handoff_calls, [("123456789", websocket)])
        self.assertEqual(
            result["next_trade_result"],
            {"status": "accepted"},
        )

    def test_monitor_contract_uses_live_ticks_when_contract_ticks_are_missing(self):
        service = TradeExecutionService()

        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.responses = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def send(self, payload):
                self.sent.append(json.loads(payload))
                if len(self.sent) == 2:
                    contract_req_id = self.sent[0]["req_id"]
                    tick_req_id = self.sent[1]["req_id"]
                    self.responses = iter(
                        [
                            json.dumps(
                                {
                                    "echo_req": {"req_id": contract_req_id},
                                    "proposal_open_contract": {
                                        "contract_id": 123456789,
                                        "is_sold": False,
                                        "current_spot": 100.25,
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "echo_req": {"req_id": tick_req_id},
                                    "tick": {"quote": 100.75},
                                }
                            ),
                            json.dumps(
                                {
                                    "echo_req": {"req_id": contract_req_id},
                                    "proposal_open_contract": {
                                        "contract_id": 123456789,
                                        "is_sold": True,
                                        "profit": 1.0,
                                        "payout": 2.0,
                                    },
                                }
                            ),
                        ]
                    )

            async def recv(self):
                return next(self.responses)

        fake_websocket = FakeWebSocket()
        with patch(
            "app.services.trade_execution_service.websockets.connect",
            return_value=fake_websocket,
        ):
            result = asyncio.run(
                service.monitor_contract(
                    "123456789",
                    "wss://example.com",
                    timeout_seconds=1,
                    market_symbol="STPRNG",
                )
            )

        self.assertEqual(
            fake_websocket.sent[1]["ticks"],
            "STPRNG",
        )
        self.assertEqual(result["entry_tick"], 100.25)
        self.assertEqual(result["exit_tick"], 100.75)

    def test_monitor_contract_continues_after_live_tick_subscription_error(self):
        service = TradeExecutionService()

        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.responses = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def send(self, payload):
                self.sent.append(json.loads(payload))
                if len(self.sent) == 2:
                    contract_req_id = self.sent[0]["req_id"]
                    tick_req_id = self.sent[1]["req_id"]
                    self.responses = iter(
                        [
                            json.dumps(
                                {
                                    "echo_req": {"req_id": tick_req_id},
                                    "error": {
                                        "code": "AuthorizationRequired",
                                        "message": "Ticks are not available here",
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "echo_req": {"req_id": contract_req_id},
                                    "proposal_open_contract": {
                                        "contract_id": 123456789,
                                        "is_sold": True,
                                        "profit": 1.0,
                                        "entry_tick": 100.25,
                                        "exit_tick": 100.75,
                                    },
                                }
                            ),
                        ]
                    )

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
                    market_symbol="STPRNG",
                )
            )

        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["entry_tick"], 100.25)
        self.assertEqual(result["exit_tick"], 100.75)

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

    def test_execute_trade_reuses_an_existing_authenticated_websocket(self):
        service = TradeExecutionService()
        websocket = object()
        with patch.object(
            service,
            "_create_proposal_on_ws",
            new=AsyncMock(return_value={
                "proposal_id": "proposal-reused",
                "ask_price": 1.0,
            }),
        ) as create_proposal, patch.object(
            service,
            "_buy_on_ws",
            new=AsyncMock(return_value={
                "contract_id": "contract-reused",
                "buy_price": 1.0,
                "balance_after": 99.0,
                "shortcode": "CALL",
            }),
        ) as buy_contract, patch(
            "app.services.trade_execution_service.websockets.connect",
        ) as connect:
            result = asyncio.run(
                service.execute_trade(
                    user_id="fast-user",
                    market_symbol="step_100",
                    direction="BULLISH",
                    stake=1.0,
                    account_id="demo-123",
                    currency="USD",
                    ws_url="wss://example.com",
                    account_type="demo",
                    websocket=websocket,
                )
            )

        self.assertEqual(result["status"], "accepted")
        connect.assert_not_called()
        create_proposal.assert_awaited_once()
        self.assertIs(create_proposal.await_args.kwargs["websocket"], websocket)
        self.assertFalse(create_proposal.await_args.kwargs["subscribe"])
        buy_contract.assert_awaited_once()
        self.assertIs(buy_contract.await_args.kwargs["websocket"], websocket)

    def test_contract_buy_validation_error_is_human_readable(self):
        message = TradeExecutionService._error_message(
            {
                "error": {
                    "code": "ContractBuyValidationError",
                    "message": (
                        "Minimum stake of 0.35 and maximum payout of 10000.00. "
                        "Current payout is 12994.55."
                    ),
                }
            }
        )

        self.assertEqual(
            message,
            "The proposed payout exceeds Deriv's maximum. Minimum stake of "
            "0.35 and maximum payout of 10000.00. Current payout is 12994.55.",
        )
        self.assertNotIn("ContractBuyValidationError", message)

    def test_stake_keyboard_excludes_blocked_values(self):
        keyboard = TelegramKeyboard.stake_keyboard()
        rows = keyboard.inline_keyboard

        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [["🟢 0.1%", "🟢 0.5%", "🟠 1%"], ["✏️ Custom amount"]],
        )
        self.assertEqual([button.callback_data for button in rows[0]], [
            "AUTO_TRADE_STAKE_0_1",
            "AUTO_TRADE_STAKE_0_5",
            "AUTO_TRADE_STAKE_1",
        ])
        self.assertEqual([button.style for button in rows[0]], ["success", "success", None])
        self.assertEqual(
            rows[1][0].callback_data,
            "AUTO_TRADE_CUSTOM_STAKE",
        )

    def test_stake_keyboard_carries_the_analyzed_market_and_direction(self):
        keyboard = TelegramKeyboard.stake_keyboard("step_100", "BULLISH")

        self.assertEqual(
            [button.callback_data for button in keyboard.inline_keyboard[0]],
            [
                "AUTO_TRADE_STAKE_0_1_step_100_BULLISH",
                "AUTO_TRADE_STAKE_0_5_step_100_BULLISH",
                "AUTO_TRADE_STAKE_1_step_100_BULLISH",
            ],
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            "AUTO_TRADE_CUSTOM_STAKE_step_100_BULLISH",
        )

    def test_custom_stake_parser_accepts_cents_and_rejects_invalid_amounts(self):
        self.assertEqual(parse_custom_stake("$1,250.35"), 1250.35)
        self.assertEqual(parse_custom_stake("0.01"), 0.01)
        for invalid in ("", "0", "-1", "1.234", "NaN", "abc"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_custom_stake(invalid)

    def test_custom_stake_button_prompts_for_amount_and_remembers_setup(self):
        service = AutoTradeService()

        class FakeQuery:
            data = "AUTO_TRADE_CUSTOM_STAKE_step_100_BULLISH"

            def __init__(self):
                self.edited = None

            async def answer(self):
                return None

            async def edit_message_text(self, text, **_kwargs):
                self.edited = text

        query = FakeQuery()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id="custom-stake-prompt-user"),
        )
        context = SimpleNamespace(
            user_data={
                "auto_trade_chart_setup": {
                    "symbol": "step_100",
                    "direction": "BULLISH",
                },
                "auto_trade_chart_setup_expires_at": time.monotonic() + 300,
            }
        )
        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ):
            asyncio.run(dispatch_callback(update, context))

        self.assertIn("Send your preferred stake amount", query.edited)
        self.assertEqual(
            service.get_custom_stake_request("custom-stake-prompt-user"),
            {
                "symbol": "step_100",
                "direction": "BULLISH",
                "account_mode": "demo",
            },
        )

    def test_custom_stake_input_creates_confirmation_for_selected_setup(self):
        service = AutoTradeService()
        service.set_custom_stake_request(
            "custom-stake-user",
            "step_300",
            "BEARISH",
            "demo",
        )

        class FakeMessage:
            def __init__(self):
                self.text = "$12.34"
                self.replies = []

            async def reply_text(self, text, **kwargs):
                self.replies.append((text, kwargs))

        message = FakeMessage()
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(
                id="custom-stake-user",
                first_name="Test",
                last_name="User",
                username="test_user",
            ),
        )

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service.ensure_user",
            return_value={"token_balance": 12},
        ), patch(
            "app.bot.callbacks.account_service.get_trade_account",
            new=AsyncMock(
                return_value={
                    "account_type": "demo",
                    "balance": 50.0,
                    "currency": "USD",
                }
            ),
        ):
            asyncio.run(handle_custom_stake_input(update))

        state = service.get_run("custom-stake-user")
        self.assertEqual(state.base_stake, 12.34)
        self.assertEqual(state.symbol, "step_300")
        self.assertEqual(state.direction, "BEARISH")
        self.assertEqual(state.status, "awaiting_confirmation")
        self.assertIn("Initial stake: 12.34 USD (custom amount)", message.replies[0][0])
        self.assertIsNotNone(message.replies[0][1]["reply_markup"])
        self.assertIsNone(
            service.get_custom_stake_request("custom-stake-user")
        )

    def test_custom_stake_input_rejects_amount_above_account_balance(self):
        service = AutoTradeService()
        service.set_custom_stake_request(
            "custom-stake-too-large-user",
            "step_300",
            "BEARISH",
            "demo",
        )

        class FakeMessage:
            text = "20.00"

            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **_kwargs):
                self.replies.append(text)

        message = FakeMessage()
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(
                id="custom-stake-too-large-user",
                first_name="Test",
                last_name="User",
                username="test_user",
            ),
        )

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service.ensure_user",
            return_value={"token_balance": 12},
        ), patch(
            "app.bot.callbacks.account_service.get_trade_account",
            new=AsyncMock(
                return_value={
                    "account_type": "demo",
                    "balance": 10.0,
                    "currency": "USD",
                }
            ),
        ):
            asyncio.run(handle_custom_stake_input(update))

        self.assertIn("exceeds the available DEMO balance", message.replies[0])
        self.assertIsNone(service.get_run("custom-stake-too-large-user"))
        self.assertIsNotNone(
            service.get_custom_stake_request("custom-stake-too-large-user")
        )

    def test_relative_stakes_preserve_cents(self):
        self.assertEqual(calculate_relative_stake(5000, "0_1"), 5)
        self.assertEqual(calculate_relative_stake(5000, "0_5"), 25)
        self.assertEqual(calculate_relative_stake(5000, "1"), 50)
        self.assertEqual(calculate_relative_stake(250.5, "0_5"), 1.25)
        stake = calculate_relative_stake(220_800, "0_1")
        self.assertEqual(stake, 220.8)
        proposal = TradeExecutionService.build_proposal_payload(
            "step_100",
            "BULLISH",
            stake,
        )
        self.assertEqual(proposal["amount"], 220.8)
        self.assertEqual(calculate_relative_stake(400, "0_1"), 0.4)

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

    def test_auto_trade_continues_smart_scaling_after_five_consecutive_losses(self):
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
        self.assertEqual(state.consecutive_losses, 1)

        state = service.advance_after_result("martingale-user", won=False, profit=-4.6)
        self.assertEqual(state.status, "running")
        self.assertEqual(state.current_stake, 10.58)
        self.assertEqual(state.consecutive_losses, 2)

        state = service.advance_after_result("martingale-user", won=False, profit=-10.58)
        self.assertEqual(state.status, "running")
        self.assertEqual(state.consecutive_losses, 3)
        self.assertEqual(state.current_stake, 24.33)

        state = service.advance_after_result("martingale-user", won=False, profit=-24.33)
        self.assertEqual(state.status, "running")
        self.assertEqual(state.consecutive_losses, 4)
        self.assertEqual(state.current_stake, 55.96)

        state = service.advance_after_result("martingale-user", won=False, profit=-55.96)
        self.assertEqual(state.status, "running")
        self.assertEqual(state.current_stake, 128.71)
        self.assertEqual(state.losses, 5)
        self.assertEqual(state.consecutive_losses, 5)

    def test_auto_trade_multiplies_each_losing_stake_and_resets_after_win(self):
        service = AutoTradeService()
        state = service.start_run(
            "martingale-progression-user",
            "step_100",
            "BULLISH",
            10.0,
            max_cycles=1,
            account_mode="demo",
        )
        service.confirm_run("martingale-progression-user")
        executed_stakes = []
        settlements = iter(
            [
                {"status": "settled", "profit": -10.0},
                {"status": "settled", "profit": -23.0},
                {"status": "settled", "profit": 5.0},
            ]
        )

        class FakeQuery:
            async def edit_message_text(self, _text, **_kwargs):
                return None

        update = SimpleNamespace(
            callback_query=FakeQuery(),
            effective_user=SimpleNamespace(id="martingale-progression-user"),
        )

        async def fake_execute_trade(_user_id, trade_state):
            executed_stakes.append(trade_state.current_stake)
            service.record_trade_stake(
                trade_state.user_id,
                trade_state.current_stake,
            )
            trade_state.contract_id = f"contract-{len(executed_stakes)}"
            trade_state.market_symbol = "STPRNG"
            trade_state.status = "monitoring"
            return {"status": "accepted"}

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=fake_execute_trade),
        ), patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(side_effect=lambda *_args: next(settlements)),
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(record_settled_trade=lambda *_args: None),
        ), patch(
            "app.bot.callbacks._save_session_summary",
            new=AsyncMock(),
        ), patch(
            "app.bot.callbacks._session_end_message",
            new=AsyncMock(return_value="Session completed."),
        ), patch(
            "app.bot.callbacks._safe_edit_message",
            new=AsyncMock(),
        ):
            asyncio.run(_run_auto_trade(update, state))

        self.assertEqual(executed_stakes, [10.0, 23.0, 52.9])
        self.assertEqual(state.losses, 2)
        self.assertEqual(state.wins, 1)
        self.assertEqual(state.consecutive_losses, 0)
        self.assertEqual(state.current_stake, 10.0)
        self.assertEqual(state.status, "completed")

    def test_auto_trade_keeps_scaling_after_five_losses_until_cycle_win(self):
        service = AutoTradeService()
        state = service.start_run(
            "five-loss-report-user",
            "step_100",
            "BEARISH",
            10.0,
            max_cycles=1,
            account_mode="demo",
        )
        service.confirm_run("five-loss-report-user")
        executed_stakes = []
        cycle_reports = []
        end_photos = []

        class FakeCycleMessage:
            def __init__(self, text, parse_mode=None):
                self.text = text
                self.edits = [text]
                self.parse_modes = [parse_mode]

            async def edit_text(self, text, **kwargs):
                self.text = text
                self.edits.append(text)
                self.parse_modes.append(kwargs.get("parse_mode"))

        class FakeMessage:
            async def reply_text(self, text, **kwargs):
                report = FakeCycleMessage(text, kwargs.get("parse_mode"))
                cycle_reports.append(report)
                return report

            async def reply_photo(self, **kwargs):
                end_photos.append(kwargs)

        class FakeQuery:
            def __init__(self):
                self.message = FakeMessage()

            async def edit_message_text(self, _text, **_kwargs):
                return None

        update = SimpleNamespace(
            callback_query=FakeQuery(),
            effective_user=SimpleNamespace(id="five-loss-report-user"),
        )

        async def fake_execute_trade(_user_id, trade_state):
            executed_stakes.append(trade_state.current_stake)
            service.record_trade_stake(
                trade_state.user_id,
                trade_state.current_stake,
            )
            trade_state.contract_id = f"contract-{len(executed_stakes)}"
            trade_state.market_symbol = "STPRNG"
            trade_state.status = "monitoring"
            return {"status": "accepted"}

        settlements = iter(
            [
                {"status": "settled", "profit": -10.0, "entry_tick": 100.0, "exit_tick": 99.0},
                {"status": "settled", "profit": -23.0, "entry_tick": 100.0, "exit_tick": 99.0},
                {"status": "settled", "profit": -52.9, "entry_tick": 100.0, "exit_tick": 99.0},
                {"status": "settled", "profit": -121.67, "entry_tick": 100.0, "exit_tick": 99.0},
                {"status": "settled", "profit": -279.84, "entry_tick": 100.0, "exit_tick": 99.0},
                {"status": "settled", "profit": 5.0, "entry_tick": 99.0, "exit_tick": 100.0},
            ]
        )

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=fake_execute_trade),
        ), patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(side_effect=lambda *_args: next(settlements)),
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(record_settled_trade=lambda *_args: None),
        ), patch(
            "app.bot.callbacks._save_session_summary",
            new=AsyncMock(),
        ), patch(
            "app.bot.callbacks._session_end_message",
            new=AsyncMock(return_value="🏆 Victory! Session completed."),
        ), patch(
            "app.bot.callbacks._safe_edit_message",
            new=AsyncMock(),
        ):
            asyncio.run(_run_auto_trade(update, state))

        self.assertEqual(
            executed_stakes,
            [10.0, 23.0, 52.9, 121.67, 279.84, 643.63],
        )
        self.assertEqual(len(cycle_reports), 1)
        self.assertTrue(
            all(parse_mode == "HTML" for report in cycle_reports for parse_mode in report.parse_modes)
        )
        self.assertIn("Cycle 1", cycle_reports[0].text)
        self.assertEqual(cycle_reports[0].text.count("Step "), 6)
        self.assertIn("⚡ Cycle 1 Step 6", cycle_reports[0].text)
        self.assertIn(
            "<pre>🔴 Invest: $10.00 → -$10.00\n"
            "📊 Price: 100.0000 → 99.0000</pre>",
            cycle_reports[0].text,
        )
        self.assertEqual(
            cycle_reports[0].text.count("Smart Scaling activated"),
            5,
        )
        self.assertNotIn("Smart Scaling activated</pre>", cycle_reports[0].text)
        self.assertNotIn("stopped after 5 consecutive losses", cycle_reports[0].text)
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.consecutive_losses, 0)
        self.assertEqual(len(end_photos), 1)
        self.assertEqual(
            end_photos[0]["photo"],
            "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
            "Bot%20images/Bullish%20and%20Bearish%20Market%20Setups%202.jpg",
        )
        self.assertEqual(
            end_photos[0]["caption"],
            "🏆 Victory! Session completed.",
        )

    def test_unexpected_auto_trade_error_is_reported_without_bubbling(self):
        service = AutoTradeService()
        state = service.start_run(
            "auto-trade-error-user",
            "step_100",
            "BULLISH",
            10.0,
            account_mode="demo",
        )
        service.confirm_run("auto-trade-error-user")

        class FakeMessage:
            def __init__(self):
                self.photos = []
                self.replies = []

            async def reply_photo(self, **kwargs):
                self.photos.append(kwargs)

            async def reply_text(self, text, **_kwargs):
                self.replies.append(text)

        query = SimpleNamespace(message=FakeMessage())
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id="auto-trade-error-user"),
        )
        end_message = AsyncMock(
            return_value="A temporary problem interrupted Auto Trade."
        )

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=RuntimeError("internal failure")),
        ), patch(
            "app.bot.callbacks._save_session_summary",
            new=AsyncMock(),
        ), patch(
            "app.bot.callbacks._session_end_message",
            new=end_message,
        ), patch(
            "app.bot.callbacks.logger.exception",
        ):
            asyncio.run(_run_auto_trade(update, state))

        self.assertEqual(state.status, "unknown_buy_outcome")
        self.assertEqual(len(query.message.photos), 1)
        self.assertEqual(
            query.message.photos[0]["caption"],
            "A temporary problem interrupted Auto Trade.",
        )
        self.assertEqual(query.message.replies, [])
        end_message.assert_awaited_once_with(
            "auto-trade-error-user",
            state,
            "The purchase outcome is uncertain. Auto Trade stopped to avoid "
            "placing a duplicate trade. Please check your Deriv account before "
            "trying again.",
        )

    def test_completed_auto_trade_sends_victory_receipt_after_cycle_report(self):
        service = AutoTradeService()
        state = service.start_run(
            "victory-receipt-user",
            "step_500",
            "BEARISH",
            10.0,
            max_cycles=1,
            account_mode="demo",
        )
        service.confirm_run("victory-receipt-user")

        class FakeMessage:
            def __init__(self):
                self.replies = []
                self.photos = []

            async def reply_text(self, text, **_kwargs):
                self.replies.append(text)
                return SimpleNamespace(edit_text=AsyncMock())

            async def reply_photo(self, **kwargs):
                self.photos.append(kwargs)

        class FakeQuery:
            def __init__(self):
                self.message = FakeMessage()

            async def edit_message_text(self, _text, **_kwargs):
                return None

        update = SimpleNamespace(
            callback_query=FakeQuery(),
            effective_user=SimpleNamespace(id="victory-receipt-user"),
        )

        async def fake_execute_trade(_user_id, trade_state):
            trade_state.contract_id = "victory-contract"
            trade_state.market_symbol = "STPRNG5"
            trade_state.status = "monitoring"
            return {"status": "accepted"}

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=fake_execute_trade),
        ), patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(return_value={"profit": 958.88, "status": "settled"}),
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(record_settled_trade=lambda *_args: None),
        ), patch(
            "app.bot.callbacks._save_session_summary",
            new=AsyncMock(),
        ), patch(
            "app.bot.callbacks._session_end_message",
            new=AsyncMock(return_value="🏆 Victory! Session completed."),
        ), patch(
            "app.bot.callbacks._safe_edit_message",
            new=AsyncMock(),
        ):
            asyncio.run(_run_auto_trade(update, state))

        self.assertEqual(state.status, "completed")
        self.assertIn("Cycle 1", update.callback_query.message.replies[0])
        self.assertEqual(len(update.callback_query.message.replies), 1)
        self.assertEqual(len(update.callback_query.message.photos), 1)
        self.assertEqual(
            update.callback_query.message.photos[0]["photo"],
            "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
            "Bot%20images/Bullish%20and%20Bearish%20Market%20Setups%202.jpg",
        )
        self.assertEqual(
            update.callback_query.message.photos[0]["caption"],
            "🏆 Victory! Session completed.",
        )

    def test_session_end_image_matches_direction_and_falls_back_to_new_text(self):
        class FakeMessage:
            def __init__(self, fail_photo=False):
                self.photos = []
                self.replies = []
                self.fail_photo = fail_photo
                self.edits = []

            async def reply_photo(self, **kwargs):
                if self.fail_photo:
                    raise RuntimeError("photo send failed")
                self.photos.append(kwargs)

            async def reply_text(self, text):
                self.replies.append(text)

            async def edit_text(self, text):
                self.edits.append(text)

        async def run_case(direction, status, fail_photo=False):
            message = FakeMessage(fail_photo=fail_photo)
            state = SimpleNamespace(status=status, direction=direction)
            query = SimpleNamespace(message=message)
            with patch(
                "app.bot.callbacks._session_end_message",
                new=AsyncMock(return_value="Victory!"),
            ), patch(
                "app.bot.callbacks.logger.exception",
            ):
                await _send_session_end_message(query, "user", state)
            return message

        bullish = asyncio.run(run_case("BULLISH", "stopped"))
        self.assertEqual(
            bullish.photos[0]["photo"],
            "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
            "Bot%20images/Bullish%20and%20Bearish%20Market%20Setups.jpg",
        )
        self.assertEqual(bullish.edits, [])

        bearish = asyncio.run(run_case("BEARISH", "completed"))
        self.assertEqual(
            bearish.photos[0]["photo"],
            "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
            "Bot%20images/Bullish%20and%20Bearish%20Market%20Setups%202.jpg",
        )

        fallback = asyncio.run(run_case("BEARISH", "stopped", fail_photo=True))
        self.assertEqual(fallback.replies, ["Victory!"])
        self.assertEqual(fallback.edits, [])

    def test_settlement_failure_updates_pending_report_and_ends_run(self):
        service = AutoTradeService()
        state = service.start_run(
            "failed-settlement-report-user",
            "step_300",
            "BULLISH",
            10.0,
            account_mode="demo",
        )
        service.confirm_run("failed-settlement-report-user")

        class FakeCycleMessage:
            def __init__(self, text):
                self.text = text

            async def edit_text(self, text, **_kwargs):
                self.text = text

        class FakeMessage:
            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **_kwargs):
                if text.startswith("⚡ Cycle"):
                    message = FakeCycleMessage(text)
                    self.replies.append(message)
                    return message
                self.replies.append(text)
                return None

        class FakeQuery:
            def __init__(self):
                self.message = FakeMessage()

            async def edit_message_text(self, _text, **_kwargs):
                return None

        update = SimpleNamespace(
            callback_query=FakeQuery(),
            effective_user=SimpleNamespace(id="failed-settlement-report-user"),
        )

        async def fake_execute_trade(_user_id, trade_state):
            trade_state.contract_id = "unverified-contract"
            trade_state.market_symbol = "STPRNG3"
            trade_state.status = "monitoring"
            return {"status": "accepted"}

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=fake_execute_trade),
        ), patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(side_effect=TimeoutError("CONTRACT_MONITOR_TIMEOUT")),
        ), patch(
            "app.bot.callbacks._save_session_summary",
            new=AsyncMock(),
        ), patch(
            "app.bot.callbacks._session_end_message",
            new=AsyncMock(return_value="Settlement could not be verified."),
        ), patch(
            "app.bot.callbacks._safe_edit_message",
            new=AsyncMock(),
        ):
            asyncio.run(_run_auto_trade(update, state))

        self.assertEqual(state.status, "monitoring_error")
        report = update.callback_query.message.replies[0]
        self.assertIn("Contract settlement could not be verified", report.text)
        self.assertNotIn("Waiting for the 30-second contract to settle", report.text)
        self.assertEqual(
            update.callback_query.message.replies[-1],
            "Settlement could not be verified.",
        )

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
        events = []

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

        async def fake_execute_trade(_user_id, trade_state):
            events.append("purchase_attempt")
            if events.count("purchase_attempt") == 1:
                trade_state.contract_id = "first-contract"
                trade_state.status = "monitoring"
                return {"status": "accepted"}
            return {"status": "rejected", "reason": "stop"}

        def record_settled_trade(trade):
            events.append(("settled_trade", trade))

        async def save_session_summary(*_args, **_kwargs):
            events.append("session_summary")

        async def safe_edit_message(*_args, **_kwargs):
            events.append("report")

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(
                return_value={
                    "profit": -2.0,
                    "status": "settled",
                    "contract_id": "first-contract",
                    "entry_tick": 100,
                    "exit_tick": 99,
                }
            ),
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=fake_execute_trade),
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                reserve_token=lambda *_: {"ok": True, "token_balance": 10},
                refund_token=lambda *_: {"ok": True, "token_balance": 10},
                update_session=lambda *_: None,
                record_settled_trade=record_settled_trade,
            ),
        ), patch(
            "app.bot.callbacks.asyncio.sleep",
            side_effect=AssertionError("post-settlement sleep should not delay the next trade"),
        ), patch(
            "app.bot.callbacks._save_session_summary",
            new=AsyncMock(side_effect=save_session_summary),
        ), patch(
            "app.bot.callbacks._safe_edit_message",
            new=AsyncMock(side_effect=safe_edit_message),
        ), patch(
            "app.bot.callbacks._send_session_end_message",
            new=AsyncMock(),
        ):
            asyncio.run(_run_auto_trade(update, service.get_run("immediate-user")))

        purchase_attempts = [
            index for index, event in enumerate(events) if event == "purchase_attempt"
        ]
        self.assertGreaterEqual(len(purchase_attempts), 2, events)
        second_purchase = purchase_attempts[1]
        persisted_trade_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, tuple) and event[0] == "settled_trade"
        )
        self.assertLess(second_purchase, persisted_trade_index)
        persisted_trade = events[persisted_trade_index][1]
        self.assertEqual(persisted_trade["contract_id"], "first-contract")
        self.assertEqual(persisted_trade["stake"], 2.0)

    def test_auto_trade_buys_on_settlement_websocket_before_monitor_returns(self):
        service = AutoTradeService()
        state = service.start_run(
            "same-socket-user",
            "step_300",
            "BULLISH",
            2.0,
            account_mode="demo",
        )
        service.confirm_run("same-socket-user")
        events = []
        websocket = object()
        prepared_account = {
            "account_id": "demo-123",
            "account_type": "demo",
            "currency": "USD",
            "ws_url": "wss://example.com",
        }
        settlement = {
            "status": "settled",
            "contract_id": "first-contract",
            "profit": -2.0,
            "entry_tick": 100,
            "exit_tick": 99,
        }

        class FakeQuery:
            async def edit_message_text(self, *_args, **_kwargs):
                return None

        update = SimpleNamespace(
            callback_query=FakeQuery(),
            effective_user=SimpleNamespace(id="same-socket-user"),
        )

        async def fake_execute_trade(_user_id, trade_state, **kwargs):
            events.append(("purchase", kwargs))
            if len(events) == 1:
                trade_state.contract_id = "first-contract"
                trade_state.status = "monitoring"
                return {"status": "accepted"}
            trade_state.status = "rejected"
            return {"status": "rejected", "reason": "test stop"}

        async def fake_monitor(_user_id, _trade_state, **kwargs):
            events.append("settlement_confirmed")
            result = await kwargs["on_settlement"](
                settlement,
                websocket,
                prepared_account,
            )
            events.append("monitor_returned")
            return {**settlement, "next_trade_result": result}

        with patch(
            "app.bot.callbacks.auto_trade_service",
            service,
        ), patch(
            "app.bot.callbacks._monitor_settlement",
            new=AsyncMock(side_effect=fake_monitor),
        ), patch(
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(side_effect=fake_execute_trade),
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(record_settled_trade=lambda *_args: None),
        ), patch(
            "app.bot.callbacks._save_session_summary",
            new=AsyncMock(),
        ), patch(
            "app.bot.callbacks._update_cycle_report",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.bot.callbacks._safe_edit_message",
            new=AsyncMock(),
        ), patch(
            "app.bot.callbacks._send_session_end_message",
            new=AsyncMock(),
        ):
            asyncio.run(_run_auto_trade_loop(update, state))

        purchase_events = [event for event in events if isinstance(event, tuple)]
        self.assertEqual(len(purchase_events), 2)
        self.assertEqual(events[1], "settlement_confirmed")
        self.assertEqual(events[3], "monitor_returned")
        self.assertIs(purchase_events[1][1]["websocket"], websocket)
        self.assertIs(
            purchase_events[1][1]["prepared_account"],
            prepared_account,
        )

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

    def test_token_store_keyboard_has_four_packages_and_main_menu(self):
        keyboard = TelegramKeyboard.token_store_keyboard()

        self.assertEqual(
            [[button.text for button in row] for row in keyboard.inline_keyboard],
            [
                ["50 Tokens"],
                ["100 Tokens"],
                ["200 Tokens"],
                ["500 Tokens"],
                ["🏠 Main Menu"],
            ],
        )
        self.assertEqual(
            [
                row[0].callback_data
                for row in keyboard.inline_keyboard[:-1]
            ],
            [
                "TOKEN_PACKAGE_50",
                "TOKEN_PACKAGE_100",
                "TOKEN_PACKAGE_200",
                "TOKEN_PACKAGE_500",
            ],
        )
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].callback_data,
            "MENU_MAIN",
        )

    def test_token_purchase_messages_match_package_amounts_and_prices(self):
        self.assertEqual(
            TOKEN_PACKAGES,
            {
                50: (
                    "5.50",
                    "https://whop.com/ignitekraft-limited/get-50-tokens/",
                ),
                100: (
                    "8.50",
                    "https://whop.com/ignitekraft-limited/get-100-tokens/",
                ),
                200: (
                    "14.50",
                    "https://whop.com/ignitekraft-limited/get-200-tokens/",
                ),
                500: (
                    "35.00",
                    "https://whop.com/ignitekraft-limited/get-500-tokens/",
                ),
            },
        )
        for amount, (price, _url) in TOKEN_PACKAGES.items():
            self.assertEqual(
                build_token_purchase_message(amount),
                f"You're purchasing: {amount} Tokens 💎 - {price} USD.\n"
                "Proceed to payment to complete your purchase.",
            )
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_TOKEN_PACKAGE"):
            build_token_purchase_message(25)

    def test_token_purchase_keyboard_links_to_whop_and_back_to_store(self):
        payment_url = TOKEN_PACKAGES[100][1]
        keyboard = TelegramKeyboard.token_purchase_keyboard(payment_url)

        self.assertEqual(
            keyboard.inline_keyboard[0][0].text,
            "💳 Pay in Whop",
        )
        self.assertEqual(keyboard.inline_keyboard[0][0].url, payment_url)
        self.assertEqual(
            keyboard.inline_keyboard[1][0].text,
            "↩️ Back to Tokens",
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            "MENU_GET_TOKENS",
        )

    def test_get_tokens_command_sends_store_image_and_five_buttons(self):
        class FakeMessage:
            photo_args = None

            async def reply_photo(self, **kwargs):
                self.photo_args = kwargs

        update = SimpleNamespace(message=FakeMessage())

        asyncio.run(_send_tokens_message(update))

        self.assertEqual(update.message.photo_args["photo"], TOKEN_STORE_IMAGE_URL)
        self.assertEqual(
            update.message.photo_args["caption"],
            build_token_store_message(),
        )
        self.assertEqual(
            len(update.message.photo_args["reply_markup"].inline_keyboard),
            5,
        )

    def test_get_tokens_callback_shows_image_and_package_buttons(self):
        class FakeQuery:
            data = "MENU_GET_TOKENS"

            def __init__(self):
                self.media_args = None

            async def answer(self):
                return None

            async def edit_message_media(self, **kwargs):
                self.media_args = kwargs

        query = FakeQuery()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id="tokens-callback-user"),
        )

        asyncio.run(dispatch_callback(update, None))

        self.assertEqual(query.media_args["media"].media, TOKEN_STORE_IMAGE_URL)
        self.assertEqual(
            query.media_args["media"].caption,
            build_token_store_message(),
        )
        self.assertEqual(
            len(query.media_args["reply_markup"].inline_keyboard),
            5,
        )

    def test_token_package_callback_shows_correct_payment_and_back_buttons(self):
        class FakeQuery:
            data = "TOKEN_PACKAGE_100"

            def __init__(self):
                self.media_args = None

            async def answer(self):
                return None

            async def edit_message_media(self, **kwargs):
                self.media_args = kwargs

        query = FakeQuery()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id="tokens-purchase-user"),
        )

        asyncio.run(dispatch_callback(update, None))

        self.assertEqual(query.media_args["media"].media, TOKEN_STORE_IMAGE_URL)
        self.assertEqual(
            query.media_args["media"].caption,
            "You're purchasing: 100 Tokens 💎 - 8.50 USD.\n"
            "Proceed to payment to complete your purchase.",
        )
        purchase_keyboard = query.media_args["reply_markup"].inline_keyboard
        self.assertEqual(
            purchase_keyboard[0][0].url,
            TOKEN_PACKAGES[100][1],
        )
        self.assertEqual(
            purchase_keyboard[1][0].callback_data,
            "MENU_GET_TOKENS",
        )

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
        ), patch(
            "app.bot.telegram_bot._auto_trade_locked_seconds",
            new=AsyncMock(return_value=None),
        ):
            asyncio.run(_send_menu_message(update))

        self.assertIsNotNone(update.message.photo)
        self.assertEqual(update.message.photo["photo"], settings.menu_image_url)
        self.assertEqual(update.message.photo["caption"], "Welcome to Yields")
        rows = update.message.photo["reply_markup"].inline_keyboard
        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [
                ["📈 Scan Market"],
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
            "🟢 UP SETUP\n\n"
            "<pre>Trading pair: Step Index 100\n"
            "RSI(7): 25.00 (within range)\n"
            "5s EMA(8/21): bullish (121.00000/120.00000)\n"
            "3-bar momentum: +0.120% (0.80x ATR)\n"
            "5s ATR(14): 0.03500 (1.10x baseline)\n"
            "1m EMA(20/50): bullish (119.00000/118.00000)\n"
            "Indicator direction score: unavailable\n"
            "Direction: UP 🟢 (directional bias, not a guarantee)</pre>\n\n"
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

        self.assertIn("UP SETUP", text)
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

    def test_auto_trade_setup_requires_authentication_before_public_scan(self):
        snapshot = MarketSnapshot(
            symbol="step_300",
            display_name="Step Index 300",
            current_price=13500.0,
            trend="bullish",
            indicators={
                "signal_strength": 0.9,
                "signal_reason": "Trend and momentum align.",
            },
            data_quality="good",
        )
        with patch(
            "app.bot.handlers.auth_service.get_valid_access_token",
            return_value=None,
        ), patch(
            "app.bot.handlers.YieldsScanner.collect_step_market_snapshots",
            return_value=[snapshot],
        ) as collect_snapshots:
            setup = build_auto_trade_setup("public-scan-user")

        self.assertEqual(setup["symbol"], "NO_SETUP")
        self.assertEqual(setup["direction"], "NO_TRADE")
        self.assertEqual(setup["reasoning"], "Account not connected.")
        collect_snapshots.assert_not_called()

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

    def test_auto_trade_setup_selects_strongest_directional_score(self):
        auth = AuthService()
        auth.connect_user(
            "direction-score-user",
            {
                "access_token": "direction-score-token",
                "refresh_token": "direction-score-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
        snapshots = [
            MarketSnapshot(
                symbol="step_100",
                display_name="Step 100",
                current_price=100.0,
                trend="bullish",
                indicators={
                    "directional_score": 31.0,
                    "signal_strength": 4.0,
                    "signal_reason": "Indicator balance leans BULLISH.",
                },
                data_quality="good",
            ),
            MarketSnapshot(
                symbol="step_200",
                display_name="Step 200",
                current_price=200.0,
                trend="bearish",
                indicators={
                    "directional_score": -68.0,
                    "signal_strength": 1.0,
                    "signal_reason": "Indicator balance leans BEARISH.",
                },
                data_quality="good",
            ),
            MarketSnapshot(
                symbol="step_300",
                display_name="Step 300",
                current_price=300.0,
                trend="neutral",
                indicators={
                    "directional_score": 3.0,
                    "signal_strength": 12.0,
                    "signal_reason": "Indicators are balanced.",
                },
                data_quality="good",
            ),
        ]

        with patch(
            "app.bot.handlers.YieldsScanner.collect_step_market_snapshots",
            return_value=snapshots,
        ):
            setup = build_auto_trade_setup("direction-score-user")

        self.assertEqual(setup["symbol"], "step_200")
        self.assertEqual(setup["direction"], "BEARISH")
        self.assertEqual(setup["scan_status"], "ready")
        self.assertEqual(setup["indicators"]["directional_score"], -68.0)

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
        self.assertIn("prices are out of date", setup["reasoning"])
        self.assertNotIn("42 seconds old", setup["reasoning"])

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
                start_auto_trade_session=lambda *_args: {
                    "started": True,
                    "retry_after_seconds": 0,
                },
            ),
        ), patch(
            "app.bot.callbacks._run_auto_trade",
            new=AsyncMock(),
        ) as run_auto_trade_mock:
            update = FakeUpdate()
            asyncio.run(dispatch_callback(update, None))

        run_auto_trade_mock.assert_awaited_once()

    def test_confirmation_rechecks_global_limit_before_starting_session(self):
        class FakeQuery:
            data = "AUTO_TRADE_CONFIRM"

            def __init__(self):
                self.markup = None
                self.message = SimpleNamespace(
                    reply_markup=None,
                    reply_text=AsyncMock(),
                )

            async def answer(self, text="", **_kwargs):
                self.alert = text

            async def edit_message_reply_markup(self, **kwargs):
                self.markup = kwargs["reply_markup"]

            async def edit_message_text(self, *_args, **_kwargs):
                return None

        update = SimpleNamespace(
            callback_query=FakeQuery(),
            effective_user=SimpleNamespace(
                id="limit-race-user",
                first_name="Test",
                last_name=None,
                username=None,
            ),
        )
        run_service = AutoTradeService()
        state = run_service.start_run(
            "limit-race-user",
            "step_100",
            "BULLISH",
            10.0,
            account_mode="demo",
        )
        start_session = Mock(
            return_value={
                "started": False,
                "retry_after_seconds": 321,
            }
        )
        with patch(
            "app.bot.callbacks.auto_trade_service",
            run_service,
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 10},
                start_auto_trade_session=start_session,
            ),
        ), patch(
            "app.bot.callbacks._run_auto_trade",
            new=AsyncMock(),
        ) as run_auto_trade:
            asyncio.run(dispatch_callback(update, None))

        self.assertEqual(state.status, "cancelled")
        limit_notice = update.callback_query.message.reply_text.await_args.args[0]
        self.assertIn("10 sessions in a rolling hour", limit_notice)
        self.assertIn("5m 21s", limit_notice)
        self.assertEqual(
            update.callback_query.markup.inline_keyboard[0][0].callback_data,
            "AUTO_TRADE_LIMIT_INFO",
        )
        start_session.assert_called_once()
        run_auto_trade.assert_not_awaited()

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
        self.assertEqual(
            [
                [button.text for button in row]
                for row in update.callback_query.photo["reply_markup"].inline_keyboard
            ],
            [["💎 Get Tokens"], ["🏠 Main Menu"]],
        )
        create_session.assert_not_called()
        run_auto_trade_mock.assert_not_awaited()

    def test_exhausted_token_message_matches_requested_copy(self):
        from app.bot.callbacks import TOKEN_EXHAUSTED_MESSAGE

        self.assertEqual(
            TOKEN_EXHAUSTED_MESSAGE,
            "💎 You’re out of Yields Tokens, so Auto Trade can’t start. "
            "No trade has been placed. Please tap on the \"💎 Get Tokens\" "
            "button or contact Sammie @samueludoh_ng to top up.",
        )

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
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": False,
                    "retry_after_seconds": 0,
                }
            ),
        ):
            asyncio.run(dispatch_callback(update, None))

        self.assertIn("50 tokens", update.callback_query.photo["caption"])
        self.assertIn("LOW TOKEN BALANCE", update.callback_query.photo["caption"])
        self.assertEqual(
            update.callback_query.photo["photo"],
            "https://hvvwkijquaoqsbzhpyim.supabase.co/storage/v1/object/public/"
            "Bot%20images/Low%20Tokens%20Warning%20Alert.png",
        )
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

    def test_unauthenticated_auto_trade_entry_prompts_to_connect(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id="unauthenticated-user",
                first_name="Sam",
            )
        )
        query = SimpleNamespace()
        with patch(
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": False,
                    "retry_after_seconds": 0,
                }
            ),
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value=None,
        ), patch(
            "app.bot.callbacks.trade_history_service.ensure_user",
        ) as ensure_user:
            with patch(
                "app.bot.callbacks._show_auto_trade_setup",
                new=AsyncMock(),
            ) as show_setup, patch(
                "app.bot.callbacks._prompt_connect",
                new=AsyncMock(),
            ) as prompt_connect:
                asyncio.run(_handle_auto_trade_entry(update, query))

        prompt_connect.assert_awaited_once_with(update, query)
        show_setup.assert_not_awaited()
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
        ), patch(
            "app.bot.telegram_bot._auto_trade_locked_seconds",
            new=AsyncMock(return_value=None),
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
        self.assertEqual(
            query.reply_markup.inline_keyboard[1][0].callback_data,
            "MENU_MAIN",
        )

    def test_main_menu_callback_uses_start_menu_for_disconnected_user(self):
        class FakeQuery:
            data = "MENU_MAIN"

            def __init__(self):
                self.media = None

            async def answer(self):
                return None

            async def edit_message_media(self, **kwargs):
                self.media = kwargs

        query = FakeQuery()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(
                id="main-menu-user",
                first_name="Sam",
                last_name="User",
                username="sam_user",
            ),
        )

        with patch(
            "app.bot.callbacks.build_menu_message",
            new=AsyncMock(return_value="Start menu"),
        ) as build_menu, patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value=None,
        ):
            asyncio.run(dispatch_callback(update, None))

        build_menu.assert_awaited_once_with(
            "main-menu-user",
            display_name="Sam",
            last_name="User",
            username="sam_user",
        )
        self.assertEqual(query.media["media"].caption, "Start menu")
        self.assertEqual(
            [[button.text for button in row] for row in query.media["reply_markup"].inline_keyboard],
            [
                ["📈 Scan Market"],
                ["🔐 Connect Deriv"],
                ["ℹ️ How Yields Works"],
            ],
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
        self.assertIn("send its chart screenshot", query.caption)
        self.assertIn("suggests Rise, Fall, or no trade", query.caption)

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
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": False,
                    "retry_after_seconds": 0,
                }
            ),
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
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": False,
                    "retry_after_seconds": 0,
                }
            ),
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
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": False,
                    "retry_after_seconds": 0,
                }
            ),
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

    def test_auto_trade_setup_requests_a_chart_market_without_scanning(self):
        class FakeQuery:
            def __init__(self):
                self.edited = None
                self.reply_markup = None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text
                self.reply_markup = kwargs.get("reply_markup")

        update = SimpleNamespace(
            callback_query=FakeQuery(),
            effective_user=SimpleNamespace(id="chart-market-user"),
        )

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ):
            asyncio.run(_show_auto_trade_setup(update, update.callback_query))

        self.assertIn("Choose one of the five Step Indices", update.callback_query.edited)
        choices = [
            button.callback_data
            for row in update.callback_query.reply_markup.inline_keyboard
            for button in row
        ]
        self.assertEqual(
            choices[:5],
            [
                "AUTO_TRADE_CHART_step_100",
                "AUTO_TRADE_CHART_step_200",
                "AUTO_TRADE_CHART_step_300",
                "AUTO_TRADE_CHART_step_400",
                "AUTO_TRADE_CHART_step_500",
            ],
        )

    def test_gemini_chart_provider_sends_image_and_parses_typed_setup(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "direction": "BULLISH",
                                                "confidence": 82,
                                                "reasoning": "Price is making higher lows.",
                                                "market_mismatch": False,
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }

        class FakeClient:
            request = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                self.request = (url, kwargs)
                return FakeResponse()

        client = FakeClient()
        with patch(
            "app.services.chart_analysis.httpx.AsyncClient",
            return_value=client,
        ):
            result = asyncio.run(
                GeminiChartAnalysisProvider("test-key", "test-model").analyze_chart(
                    b"chart-image",
                    "Step Index 100",
                )
            )

        self.assertEqual(result.direction, "BULLISH")
        self.assertEqual(result.confidence, 82.0)
        self.assertEqual(result.reasoning, "Price is making higher lows.")
        url, kwargs = client.request
        self.assertTrue(url.endswith("/test-model:generateContent"))
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-key")
        request_parts = kwargs["json"]["contents"][0]["parts"]
        self.assertEqual(
            request_parts[1]["inline_data"]["mime_type"],
            "image/jpeg",
        )
        instructions = kwargs["json"]["systemInstruction"]["parts"][0]["text"]
        self.assertIn(
            "market_mismatch",
            instructions,
        )
        self.assertIn("30 seconds after entry", instructions)
        self.assertIn("at least 150 seconds", instructions)
        self.assertIn("most recent visible price action", instructions)
        self.assertIn("not a calibrated win probability", instructions)
        self.assertIn(
            "Return NO_TRADE for a genuinely choppy/ranging market",
            instructions,
        )
        self.assertIn("not forced to alternate", instructions)
        self.assertIn("Do not require unusually strong", instructions)

    def test_gemini_chart_provider_rejects_mismatched_market_and_bad_output(self):
        from app.services.chart_analysis import _parse_analysis_result

        mismatch = _parse_analysis_result(
            {
                "direction": "BEARISH",
                "confidence": 91,
                "reasoning": "The chart shows a downtrend.",
                "market_mismatch": True,
            }
        )
        self.assertEqual(mismatch.direction, "NO_TRADE")
        self.assertTrue(mismatch.market_mismatch)

        with self.assertRaises(ValueError):
            _parse_analysis_result(
                {
                    "direction": "BUY",
                    "confidence": 90,
                    "reasoning": "Direction unclear.",
                    "market_mismatch": False,
                }
            )

    def test_chart_market_selection_waits_for_photo_and_sets_expiry(self):
        context = SimpleNamespace(user_data={})

        class FakeQuery:
            data = "AUTO_TRADE_CHART_step_300"

            def __init__(self):
                self.text = None
                self.markup = None

            async def answer(self):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.text = text
                self.markup = kwargs.get("reply_markup")

        query = FakeQuery()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id="chart-selection-user"),
        )
        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ):
            asyncio.run(dispatch_callback(update, context))

        self.assertEqual(
            context.user_data["pending_chart_market_symbol"],
            "step_300",
        )
        self.assertGreater(
            context.user_data["pending_chart_market_expires_at"],
            0,
        )
        self.assertIn("Step Index 300 chart", query.text)
        self.assertIn("AI provider", query.text)

    def test_expired_chart_setup_cannot_continue_to_stake_selection(self):
        class FakeQuery:
            data = "AUTO_TRADE_STAKE_0_1_step_100_BULLISH"

            def __init__(self):
                self.text = None

            async def answer(self):
                return None

            async def edit_message_text(self, text, **_kwargs):
                self.text = text

        query = FakeQuery()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id="expired-chart-setup-user"),
        )
        context = SimpleNamespace(
            user_data={
                "auto_trade_chart_setup": {
                    "symbol": "step_100",
                    "direction": "BULLISH",
                },
                "auto_trade_chart_setup_expires_at": time.monotonic() - 1,
            }
        )
        ensure_user = Mock(return_value={"token_balance": 10})

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service.ensure_user",
            ensure_user,
        ):
            asyncio.run(dispatch_callback(update, context))

        self.assertIn("chart setup has expired", query.text)
        ensure_user.assert_not_called()

    def test_chart_screenshot_analysis_offers_stake_buttons(self):
        class FakeFile:
            async def download_as_bytearray(self):
                return bytearray(b"chart-image")

        class FakePhoto:
            file_size = 11

            async def get_file(self):
                return FakeFile()

        class FakeMessage:
            photo = [FakePhoto()]

            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **kwargs):
                self.replies.append((text, kwargs))

        async def run_test():
            message = FakeMessage()
            user_data = {
                "pending_chart_market_symbol": "step_100",
                "pending_chart_market_expires_at": (
                    asyncio.get_running_loop().time() + 300
                ),
            }
            provider = SimpleNamespace(
                analyze_chart=AsyncMock(
                    return_value=ChartAnalysis(
                        direction="BULLISH",
                        confidence=78,
                        reasoning="The visible chart forms higher lows.",
                        market_mismatch=False,
                    )
                )
            )
            with patch(
                "app.bot.callbacks.auth_service.get_valid_access_token",
                return_value="access-token",
            ), patch(
                "app.bot.callbacks.create_chart_analysis_provider",
                return_value=provider,
            ):
                await handle_chart_screenshot(
                    SimpleNamespace(
                        message=message,
                        effective_user=SimpleNamespace(id="chart-photo-user"),
                    ),
                    SimpleNamespace(user_data=user_data),
                )
            return message, user_data, provider

        message, user_data, provider = asyncio.run(run_test())

        provider.analyze_chart.assert_awaited_once_with(
            b"chart-image",
            "Step Index 100",
        )
        self.assertNotIn("pending_chart_market_symbol", user_data)
        self.assertEqual(
            user_data["auto_trade_chart_setup"]["direction"],
            "BULLISH",
        )
        text, kwargs = message.replies[0]
        self.assertIn("AI evidence confidence: 78%", text)
        self.assertIn("first 30-second contract only", text)
        self.assertIn("Choose your stake", text)
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn(
            "AUTO_TRADE_STAKE_0_1_step_100_BULLISH",
            callbacks,
        )

    def test_chart_no_trade_result_does_not_offer_stake_buttons(self):
        class FakePhoto:
            file_size = 10

            async def get_file(self):
                return SimpleNamespace(
                    download_as_bytearray=AsyncMock(return_value=b"chart")
                )

        class FakeMessage:
            photo = [FakePhoto()]

            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **kwargs):
                self.replies.append((text, kwargs))

        async def run_test():
            message = FakeMessage()
            user_data = {
                "pending_chart_market_symbol": "step_200",
                "pending_chart_market_expires_at": (
                    asyncio.get_running_loop().time() + 300
                ),
            }
            provider = SimpleNamespace(
                analyze_chart=AsyncMock(
                    return_value=ChartAnalysis(
                        direction="NO_TRADE",
                        confidence=44,
                        reasoning="The chart is too unclear.",
                        market_mismatch=False,
                    )
                )
            )
            with patch(
                "app.bot.callbacks.auth_service.get_valid_access_token",
                return_value="access-token",
            ), patch(
                "app.bot.callbacks.create_chart_analysis_provider",
                return_value=provider,
            ):
                await handle_chart_screenshot(
                    SimpleNamespace(
                        message=message,
                        effective_user=SimpleNamespace(id="unclear-chart-user"),
                    ),
                    SimpleNamespace(user_data=user_data),
                )
            return message, user_data

        message, user_data = asyncio.run(run_test())

        text, kwargs = message.replies[0]
        self.assertIn("NO CLEAR SETUP", text)
        self.assertNotIn("reply_markup", kwargs)
        self.assertEqual(
            user_data["pending_chart_market_symbol"],
            "step_200",
        )

    def test_oversized_chart_screenshot_is_rejected_before_download(self):
        class FakePhoto:
            file_size = 9 * 1024 * 1024
            get_file = AsyncMock()

        class FakeMessage:
            photo = [FakePhoto()]

            def __init__(self):
                self.reply_text = AsyncMock()

        async def run_test():
            message = FakeMessage()
            user_data = {
                "pending_chart_market_symbol": "step_100",
                "pending_chart_market_expires_at": (
                    asyncio.get_running_loop().time() + 300
                ),
            }
            provider_factory = Mock()
            with patch(
                "app.bot.callbacks.auth_service.get_valid_access_token",
                return_value="access-token",
            ), patch(
                "app.bot.callbacks.create_chart_analysis_provider",
                provider_factory,
            ):
                await handle_chart_screenshot(
                    SimpleNamespace(
                        message=message,
                        effective_user=SimpleNamespace(id="large-chart-user"),
                    ),
                    SimpleNamespace(user_data=user_data),
                )
            return message, provider_factory

        message, provider_factory = asyncio.run(run_test())

        self.assertIn("too large", message.reply_text.await_args.args[0])
        FakePhoto.get_file.assert_not_awaited()
        provider_factory.assert_not_called()

    def test_unauthenticated_user_is_prompted_to_connect_before_scanning(self):
        class FakeQuery:
            def __init__(self):
                self.edited = None
                self.reply_markup = None

            async def edit_message_text(self, text, **kwargs):
                self.edited = text
                self.reply_markup = kwargs.get("reply_markup")

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id="public-scan-user"),
            callback_query=FakeQuery(),
        )
        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value=None,
        ):
            asyncio.run(
                _show_auto_trade_setup(update, update.callback_query)
            )

        self.assertIn("Connect your Deriv account", update.callback_query.edited)
        self.assertEqual(
            update.callback_query.reply_markup.inline_keyboard[0][0].text,
            "🔐 Connect Deriv",
        )

    def test_auto_trade_entry_requires_authentication_before_scanning(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id="public-entry-user")
        )
        query = SimpleNamespace()
        with patch(
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": False,
                    "retry_after_seconds": 0,
                }
            ),
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value=None,
        ), patch(
            "app.bot.callbacks._show_auto_trade_setup",
            new=AsyncMock(),
        ) as show_setup:
            asyncio.run(_handle_auto_trade_entry(update, query))

        show_setup.assert_not_awaited()

    def test_auto_trade_entry_exhaustion_shows_get_tokens_and_main_menu(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id="exhausted-entry-user")
        )
        query = SimpleNamespace()

        with patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": False,
                    "retry_after_seconds": 0,
                }
            ),
        ), patch(
            "app.bot.callbacks._get_trade_token_balance",
            new=AsyncMock(return_value=0),
        ), patch(
            "app.bot.callbacks._send_low_token_message",
            new=AsyncMock(),
        ) as send_low_token:
            asyncio.run(_handle_auto_trade_entry(update, query))

        args, kwargs = send_low_token.await_args
        from app.bot.callbacks import TOKEN_EXHAUSTED_MESSAGE

        self.assertIs(args[0], query)
        self.assertEqual(args[1], TOKEN_EXHAUSTED_MESSAGE)
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [["💎 Get Tokens"], ["🏠 Main Menu"]],
        )

    def test_stake_callback_falls_back_to_caption_edit_when_message_is_media(self):
        class FakeQuery:
            def __init__(self):
                self.data = "AUTO_TRADE_STAKE_0_1_step_100_BULLISH"
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
        context = SimpleNamespace(
            user_data={
                "auto_trade_chart_setup": {
                    "symbol": "step_100",
                    "display_name": "Step Index 100",
                    "direction": "BULLISH",
                    "confidence": 81,
                    "reasoning": "The chart shows higher lows.",
                    "analysis_provider": "GEMINI",
                },
                "auto_trade_chart_setup_expires_at": time.monotonic() + 300,
            }
        )

        with patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.account_service.get_trade_account",
            new=AsyncMock(
                return_value={
                    "account_type": "demo",
                    "account_id": "demo-1",
                    "balance": 220800,
                    "currency": "USD",
                }
            ),
        ), patch(
            "app.bot.callbacks.auto_trade_service.is_running",
            return_value=False,
        ), patch(
            "app.bot.callbacks.auto_trade_service.start_run",
            return_value=object(),
        ) as start_run, patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 50},
            ),
        ):
            asyncio.run(dispatch_callback(update, context))

        query = update.callback_query
        self.assertIsNotNone(query.text_text)
        self.assertIsNotNone(query.caption_text)
        self.assertIn("TRADE CONFIRMATION", query.caption_text)
        self.assertIn("Market: Step Index 100", query.caption_text)
        self.assertIn(
            "Initial stake: 220.80 USD (0.1% of 220,800.00 USD balance)",
            query.caption_text,
        )
        start_run.assert_called_once()
        self.assertEqual(start_run.call_args.kwargs["symbol"], "step_100")
        self.assertEqual(start_run.call_args.kwargs["direction"], "BULLISH")
        self.assertEqual(start_run.call_args.kwargs["base_stake"], 220.8)

    def test_confirm_refreshes_relative_stake_without_whole_number_rounding(self):
        class FakeQuery:
            data = "AUTO_TRADE_CONFIRM"

            async def answer(self):
                return None

            async def edit_message_text(self, _text, **_kwargs):
                return None

        update = SimpleNamespace(
            callback_query=FakeQuery(),
            effective_user=SimpleNamespace(
                id="confirm-relative-stake-user",
                first_name="Test",
                last_name="User",
                username="test_user",
            ),
        )
        run_service = AutoTradeService()
        state = run_service.start_run(
            "confirm-relative-stake-user",
            "step_100",
            "BULLISH",
            220.8,
            account_mode="demo",
        )
        state.stake_percentage_key = "0_1"

        with patch(
            "app.bot.callbacks.auto_trade_service",
            run_service,
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.account_service.get_trade_account",
            new=AsyncMock(
                return_value={
                    "balance": 220_800,
                    "currency": "USD",
                }
            ),
        ), patch(
            "app.bot.callbacks.trade_history_service",
            SimpleNamespace(
                ensure_user=lambda *_args, **_kwargs: {"token_balance": 50},
                start_auto_trade_session=lambda *_args, **_kwargs: {
                    "started": True,
                    "retry_after_seconds": 0,
                },
            ),
        ), patch(
            "app.bot.callbacks._run_auto_trade",
            new=AsyncMock(),
        ) as run_auto_trade:
            asyncio.run(dispatch_callback(update, None))

        self.assertEqual(state.base_stake, 220.8)
        self.assertEqual(state.current_stake, 220.8)
        run_auto_trade.assert_awaited_once_with(update, state)

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
            "app.bot.callbacks._get_auto_trade_limit_status",
            new=AsyncMock(
                return_value={
                    "locked": False,
                    "retry_after_seconds": 0,
                }
            ),
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

    def test_execute_trade_uses_preverified_account_and_existing_websocket(self):
        class FakeTradeHistory:
            def reserve_token(self, *_args):
                return {"ok": True, "token_balance": 4}

        run_service = AutoTradeService()
        state = run_service.start_run(
            "reuse-account-user",
            "step_300",
            "BULLISH",
            5.0,
            account_mode="demo",
        )
        run_service.confirm_run("reuse-account-user")
        prepared_account = {
            "account_type": "demo",
            "account_id": "demo-1",
            "currency": "USD",
            "ws_url": "wss://example.test",
        }
        websocket = object()

        with patch(
            "app.bot.callbacks.auto_trade_service",
            run_service,
        ), patch(
            "app.bot.callbacks.auth_service.get_valid_access_token",
            return_value="access-token",
        ), patch(
            "app.bot.callbacks.account_mode_service.get_mode",
            return_value="demo",
        ), patch(
            "app.bot.callbacks.account_service.get_trade_account",
            new=AsyncMock(side_effect=AssertionError("must reuse verified account")),
        ) as resolve_account, patch(
            "app.bot.callbacks.account_service.request_account_otp_url",
            new=AsyncMock(side_effect=AssertionError("must reuse active socket")),
        ) as request_otp, patch(
            "app.bot.callbacks.trade_history_service",
            FakeTradeHistory(),
        ), patch(
            "app.bot.callbacks.trade_execution_service.execute_trade",
            new=AsyncMock(
                return_value={
                    "status": "accepted",
                    "account_type": "demo",
                    "contract_id": "fast-contract",
                    "proposal_id": "fast-proposal",
                    "underlying_symbol": "STPRNG3",
                    "contract_type": "CALL",
                }
            ),
        ) as deriv_trade:
            result = asyncio.run(
                execute_trade(
                    "reuse-account-user",
                    state,
                    prepared_account=prepared_account,
                    websocket=websocket,
                )
            )

        self.assertEqual(result["status"], "accepted")
        resolve_account.assert_not_awaited()
        request_otp.assert_not_awaited()
        self.assertIs(deriv_trade.await_args.kwargs["websocket"], websocket)
        self.assertEqual(
            deriv_trade.await_args.kwargs["ws_url"],
            "wss://example.test",
        )

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

    def test_global_auto_trade_session_limit_uses_shared_supabase_rpcs(self):
        requests = []
        responses = iter(
            [
                {
                    "sessions_started": 10,
                    "limit": 10,
                    "locked": True,
                    "retry_after_seconds": 125,
                },
                {
                    "started": True,
                    "sessions_started": 10,
                    "limit": 10,
                    "retry_after_seconds": 0,
                },
            ]
        )

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                json=next(responses),
                request=request,
            )

        service = TradeHistoryService(
            supabase_url="https://project.supabase.co",
            service_role_key="test-service-role-key",
            transport=httpx.MockTransport(respond),
        )
        status = service.get_auto_trade_session_limit_status()
        started = service.start_auto_trade_session(
            {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "telegram_user_id": "12345",
                "account_mode": "DEMO",
                "market_symbol": "step_100",
                "direction": "bullish",
                "initial_stake": 12.5,
                "target_winning_cycles": 5,
            }
        )

        self.assertTrue(status["locked"])
        self.assertEqual(status["retry_after_seconds"], 125)
        self.assertTrue(started["started"])
        self.assertTrue(
            requests[0].url.path.endswith(
                "/rpc/get_auto_trade_session_limit_status"
            )
        )
        self.assertEqual(json.loads(requests[0].content), {})
        self.assertTrue(
            requests[1].url.path.endswith("/rpc/start_auto_trade_session")
        )
        self.assertEqual(
            json.loads(requests[1].content),
            {
                "p_session_id": "11111111-1111-4111-8111-111111111111",
                "p_telegram_user_id": "12345",
                "p_account_mode": "demo",
                "p_market_symbol": "step_100",
                "p_direction": "BULLISH",
                "p_initial_stake": 12.5,
                "p_target_winning_cycles": 5,
            },
        )

    def test_telegram_profile_sync_clears_removed_fields_and_encrypts_payout_details(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                json=[{"telegram_user_id": "12345"}],
                request=request,
            )

        service = TradeHistoryService(
            supabase_url="https://project.supabase.co",
            service_role_key="test-service-role-key",
            transport=httpx.MockTransport(respond),
            encryption_key=Fernet.generate_key().decode("ascii"),
        )
        service.ensure_user(
            "12345",
            first_name="Sam",
            last_name=None,
            username=None,
            sync_telegram_profile=True,
        )
        payout_details = "Account number: 1234567890"
        encrypted = service.encrypt_payout_details(payout_details)

        payload = json.loads(requests[0].content)
        self.assertEqual(payload["telegram_first_name"], "Sam")
        self.assertIsNone(payload["telegram_last_name"])
        self.assertIsNone(payload["telegram_username"])
        self.assertNotEqual(encrypted, payout_details)
        self.assertEqual(service.decrypt_payout_details(encrypted), payout_details)

    def test_welcome_gift_claim_calls_idempotent_supabase_rpc(self):
        requests = []
        rpc_responses = iter(
            [
                {"granted": True, "tokens_granted": 200, "token_balance": 200},
                {"granted": False, "tokens_granted": 0, "token_balance": 200},
            ]
        )

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                json=next(rpc_responses),
                request=request,
            )

        service = TradeHistoryService(
            supabase_url="https://project.supabase.co",
            service_role_key="test-service-role-key",
            transport=httpx.MockTransport(respond),
        )

        first_grant = service.claim_welcome_gift_200("telegram-user-200")
        reconnect = service.claim_welcome_gift_200("telegram-user-200")

        self.assertTrue(first_grant["granted"])
        self.assertEqual(first_grant["tokens_granted"], 200)
        self.assertEqual(first_grant["token_balance"], 200)
        self.assertFalse(reconnect["granted"])
        self.assertEqual(reconnect["token_balance"], 200)
        self.assertEqual(len(requests), 2)
        self.assertTrue(
            all(
                request.url.path.endswith("/rpc/claim_welcome_gift_200")
                for request in requests
            )
        )
        self.assertEqual(
            json.loads(requests[0].content),
            {"p_telegram_user_id": "telegram-user-200"},
        )

    def test_whop_webhook_signature_requires_fresh_matching_signature(self):
        payload = b'{"type":"payment.succeeded"}'
        secret = "ws_test_secret"
        webhook_id = "evt_test_123"
        timestamp = "1700000000"
        signed_content = f"{webhook_id}.{timestamp}.".encode() + payload
        signature = base64.b64encode(
            hmac.new(
                secret.encode(),
                signed_content,
                hashlib.sha256,
            ).digest()
        ).decode()
        headers = {
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,{signature}",
        }

        self.assertTrue(
            verify_whop_webhook_signature(
                payload,
                headers,
                secret,
                now=1700000000,
            )
        )
        self.assertFalse(
            verify_whop_webhook_signature(
                payload,
                headers,
                secret,
                now=1700000301,
            )
        )
        self.assertFalse(
            verify_whop_webhook_signature(
                payload,
                {**headers, "webhook-signature": "v1,invalid"},
                secret,
                now=1700000000,
            )
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
        self.assertIn("Session win rate: 100.0%", text)
        self.assertIn("Direction accuracy (contracts): 52.0%", text)
        self.assertIn("Net PnL: +$166.22", text)
        self.assertIn("Net PnL shows overall profit or loss.", text)

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
            "app.bot.callbacks.account_service.get_current_live_balance",
            new=AsyncMock(
                return_value={
                    "available": True,
                    "balance": 172.09,
                    "currency": "USD",
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

    def test_session_end_keeps_live_balance_when_snapshot_save_fails(self):
        state = AutoTradeService().start_run(
            "receipt-snapshot-failure-user",
            "step_500",
            "BULLISH",
            5.0,
            account_mode="demo",
        )
        state.status = "completed"
        balances = {
            "available": True,
            "balance": 321.45,
            "currency": "USD",
        }
        history = SimpleNamespace(
            update_balance_snapshots=Mock(
                side_effect=RuntimeError("storage unavailable"),
            ),
            get_user=Mock(return_value={"token_balance": 12}),
        )

        with patch(
            "app.bot.callbacks.account_service.get_current_live_balance",
            new=AsyncMock(return_value=balances),
        ), patch(
            "app.bot.callbacks.trade_history_service",
            history,
        ):
            text = asyncio.run(
                _session_end_message("receipt-snapshot-failure-user", state)
            )

        self.assertIn("Balance: 321.45 USD", text)
        self.assertNotIn("Live balance temporarily unavailable", text)

    def test_session_end_never_substitutes_cached_or_estimated_balance(self):
        service = AutoTradeService()
        cached_state = service.start_run(
            "receipt-cached-balance-user",
            "step_500",
            "BEARISH",
            5.0,
            account_mode="demo",
        )
        cached_state.status = "stopped"
        estimated_state = service.start_run(
            "receipt-estimated-balance-user",
            "step_500",
            "BEARISH",
            5.0,
            account_mode="demo",
        )
        estimated_state.status = "stopped"
        estimated_state.starting_balance = 100.0
        estimated_state.total_profit_loss = -12.5

        async def make_text(user_id, state, profile):
            with patch(
                "app.bot.callbacks.account_service.get_current_live_balance",
                new=AsyncMock(side_effect=RuntimeError("Deriv unavailable")),
            ), patch(
                "app.bot.callbacks.trade_history_service",
                SimpleNamespace(get_user=Mock(return_value=profile)),
            ), patch(
                "app.bot.callbacks.logger.exception",
            ):
                return await _session_end_message(user_id, state)

        cached_text = asyncio.run(
            make_text(
                "receipt-cached-balance-user",
                cached_state,
                {
                    "token_balance": 12,
                    "demo_balance": 210.0,
                    "demo_currency": "USD",
                },
            )
        )
        estimated_text = asyncio.run(
            make_text(
                "receipt-estimated-balance-user",
                estimated_state,
                {"token_balance": 12},
            )
        )

        for text in (cached_text, estimated_text):
            self.assertIn("Balance: Live balance temporarily unavailable", text)
            self.assertNotIn("Last known:", text)
            self.assertNotIn("Estimated:", text)

    def test_deriv_oauth_success_message_includes_welcome_gift_offer(self):
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
             patch(
                 "app.main.trade_history_service.ensure_user",
                 return_value={
                     "telegram_user_id": "telegram-user-42",
                     "welcome_gift_200_claimed": False,
                 },
             ) as ensure_user, \
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
        ensure_user.assert_called_once_with("telegram-user-42")

        sent_message = fake_app.bot.sent[0]
        self.assertIn("reply_markup", sent_message)
        self.assertIn("one-time gift of 200 Yields Tokens", sent_message["text"])
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

    def test_current_live_balance_uses_selected_account_and_live_value(self):
        auth = Mock()
        auth.get_valid_access_token.return_value = "access-token"
        service = AccountService(auth)
        account = {
            "account_id": "demo-123",
            "account_type": "demo",
            "currency": "USD",
            "balance": 999.0,
        }
        service.get_trade_account = AsyncMock(return_value=account)
        service._read_live_balance = AsyncMock(return_value=123.45)

        result = asyncio.run(
            service.get_current_live_balance("live-user", "demo")
        )

        service.get_trade_account.assert_awaited_once_with("live-user", "demo")
        service._read_live_balance.assert_awaited_once_with(
            access_token="access-token",
            account_id="demo-123",
        )
        self.assertEqual(result["balance"], 123.45)
        self.assertEqual(result["currency"], "USD")

    def test_current_live_balance_does_not_fall_back_to_account_list_value(self):
        auth = Mock()
        auth.get_valid_access_token.return_value = "access-token"
        service = AccountService(auth)
        service.get_trade_account = AsyncMock(
            return_value={
                "account_id": "demo-123",
                "account_type": "demo",
                "currency": "USD",
                "balance": 999.0,
            }
        )
        service._read_live_balance = AsyncMock(return_value=None)

        with self.assertRaisesRegex(ValueError, "LIVE_BALANCE_UNAVAILABLE"):
            asyncio.run(
                service.get_current_live_balance("live-user", "demo")
            )

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
                            "wss://api.derivws.com/ws?otp=demo"
                        )
                    if url.endswith("/real-001/otp"):
                        return FakeOtpResponse(
                            "wss://api.derivws.com/ws?otp=real"
                        )
                    return FakeOtpResponse(
                        "wss://api.derivws.com/ws?otp=demo"
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
                "wss://api.derivws.com/ws?otp=demo"
            )
            fake_real_socket = FakeSocket(
                "wss://api.derivws.com/ws?otp=real"
            )

            with patch(
                "app.services.account_service.httpx.AsyncClient",
                return_value=FakeAsyncClient(),
            ), patch(
                "app.services.account_service.websockets.connect",
                side_effect=[
                    fake_real_socket,
                    fake_demo_socket,
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
        ), patch.object(
            MarketDataService,
            "get_historical_candles",
            return_value=_sample_one_minute_candles(),
        ):
            snapshots = scanner.collect_step_market_snapshots(MarketDataService())

        self.assertEqual(len(snapshots), 5)
        self.assertEqual(
            [snapshot.symbol for snapshot in snapshots],
            list(settings.auto_trade_markets),
        )

    def test_one_failed_market_does_not_abort_other_market_scans(self):
        scanner = YieldsScanner()

        class PartiallyUnavailableMarketData:
            def __init__(self):
                self.requested_counts = []

            def get_historical_ticks(self, symbol, count):
                self.requested_counts.append(count)
                if symbol == "step_100":
                    raise TimeoutError("Deriv request timed out")
                return _sample_deriv_ticks()

            def get_historical_candles(self, symbol, count, granularity):
                return _sample_one_minute_candles(count)

        market_data = PartiallyUnavailableMarketData()
        snapshots = scanner.collect_step_market_snapshots(market_data)

        self.assertEqual(len(snapshots), len(settings.auto_trade_markets))
        self.assertEqual(market_data.requested_counts, [1000] * len(snapshots))
        self.assertEqual(snapshots[0].data_quality, "unavailable")
        self.assertIn(
            "Live market data could not be retrieved",
            snapshots[0].indicators["signal_reason"],
        )
        self.assertTrue(
            all(snapshot.data_quality == "good" for snapshot in snapshots[1:])
        )
        self.assertIn("TimeoutError", snapshots[0].indicators["data_error"])
        self.assertNotIn("TimeoutError", snapshots[0].indicators["signal_reason"])

    def test_all_market_data_failures_are_reported_as_one_scan_failure(self):
        failed_snapshots = [
            MarketSnapshot(
                symbol=symbol,
                display_name=f"Step {index * 100}",
                current_price=0.0,
                trend="neutral",
                indicators={
                    "signal_reason": "Market data request failed: "
                    "WebSocket handshake rejected (HTTP 520)",
                    "data_error": "WebSocket handshake rejected (HTTP 520)",
                },
                data_quality="unavailable",
            )
            for index, symbol in enumerate(settings.auto_trade_markets, 1)
        ]

        with (
            patch(
                "app.bot.handlers.auth_service.get_valid_access_token",
                return_value="test-token",
            ),
            patch(
                "app.bot.handlers.YieldsScanner.collect_step_market_snapshots",
                return_value=failed_snapshots,
            ),
        ):
            setup = build_auto_trade_setup("scan-user")
            message = handle_auto_trade_start("scan-user", setup)

        self.assertEqual(setup["scan_status"], "unavailable")
        self.assertEqual(setup["markets_scanned"], 5)
        self.assertEqual(setup["markets_available"], 0)
        self.assertEqual(len(setup["market_issues"]), 5)
        self.assertIn("Live market data could not be retrieved", setup["reasoning"])
        self.assertIn("any of the 5 configured markets", message)
        self.assertIn("Step 100", message)
        self.assertIn("Step 500", message)
        self.assertIn("Live market data could not be retrieved", message)
        self.assertNotIn("HTTP 520", message)
        self.assertNotIn("Shared failure", message)
        self.assertNotIn("Step 100: Step 100", message)
        self.assertNotIn("RSI(7): unavailable", message)

    def test_partial_market_failures_are_disclosed_with_available_count(self):
        snapshots = [
            MarketSnapshot(
                symbol=settings.auto_trade_markets[0],
                display_name="Step 100",
                current_price=100.0,
                trend="neutral",
                indicators={"signal_strength": 0.0},
                data_quality="good",
            ),
            *[
                MarketSnapshot(
                    symbol=symbol,
                    display_name=f"Step {index * 100}",
                    current_price=0.0,
                    trend="neutral",
                    indicators={
                        "signal_reason": "Market data request failed: "
                        "TimeoutError"
                    },
                    data_quality="unavailable",
                )
                for index, symbol in enumerate(
                    settings.auto_trade_markets[1:], 2
                )
            ],
        ]

        with (
            patch(
                "app.bot.handlers.auth_service.get_valid_access_token",
                return_value="test-token",
            ),
            patch(
                "app.bot.handlers.YieldsScanner.collect_step_market_snapshots",
                return_value=snapshots,
            ),
        ):
            setup = build_auto_trade_setup("scan-user")
            message = handle_auto_trade_start("scan-user", setup)

        self.assertEqual(setup["scan_status"], "no_setup")
        self.assertEqual(setup["markets_available"], 1)
        self.assertIn("Data available for 1/5 markets", message)
        self.assertIn("Step 500", message)

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