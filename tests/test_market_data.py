import asyncio
import importlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.bot.callbacks import dispatch_callback
from app.bot.handlers import (
    build_auto_trade_setup,
    handle_auto_trade_start,
    handle_start,
)
from app.config import settings
from app.main import app
from app.models.ai_analysis import AutoTradeAnalysis
from app.models.market import MarketSnapshot
from app.services.account_service import AccountService
from app.services.auth_service import AuthService
from app.services.auto_trade_service import AutoTradeService
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.gemini_service import GeminiService
from app.services.scanner import SignalResult, YieldsScanner
from app.services.trade_execution_service import TradeExecutionService
from app.services.trading_service import TradingService


class MarketDataTests(unittest.TestCase):
    def test_market_data_normalizes_tick_payload(self):
        service = MarketDataService()

        normalized = service.normalize_tick(
            {
                "tick": {
                    "symbol": "R_100",
                    "quote": 123.45,
                    "epoch": 1710000000,
                },
                "echo_req": {"ticks": "R_100"},
            }
        )

        self.assertEqual(normalized["symbol"], "R_100")
        self.assertEqual(normalized["quote"], 123.45)
        self.assertEqual(normalized["epoch"], 1710000000)
        self.assertIn("timestamp", normalized)

    def test_scanner_builds_signal_result(self):
        scanner = YieldsScanner()
        signal = scanner.build_signal(1.0, 1.2, 0.85, "R_100")

        self.assertIsInstance(signal, SignalResult)
        self.assertEqual(signal.symbol, "R_100")
        self.assertGreater(signal.confidence, 0)

    def test_market_history_varies_by_symbol_to_avoid_duplicate_winners(self):
        service = MarketDataService()

        history_100 = service.get_historical_ticks("step_100", count=12)
        history_300 = service.get_historical_ticks("step_300", count=12)

        self.assertNotEqual(history_100[0]["quote"], history_300[0]["quote"])
        self.assertNotEqual(history_100[-1]["quote"], history_300[-1]["quote"])

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
        self.assertIn(result["symbol"], {"R_100", "R_200", "R_300", "R_400", "R_500"})
        self.assertIn("contract_id", result)

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
        self.assertEqual(replacement.status, "running")

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
        ):
            text = handle_start("live-start-user")

        self.assertIn("$1,250.50 USD", text)
        self.assertIn("$750.25 USD", text)
        self.assertIn("AI", text)
        self.assertNotIn("Gemini", text)

    def test_auto_trade_start_prompts_for_stake_and_uses_ai_language(self):
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
                "symbol": "R_100",
                "direction": "BULLISH",
                "confidence": 0.83,
                "setup_quality": 0.88,
                "reasoning": "Strong upward trend and trend alignment.",
            },
        ):
            text = handle_auto_trade_start("trade-user")

        self.assertIn("AI", text)
        self.assertNotIn("Gemini", text)
        self.assertIn("Select your stake", text)
        self.assertIn("All 5 trades", text)

    def test_auto_trade_setup_blends_ai_and_technical_signals(self):
        auth = AuthService()
        auth.connect_user(
            "blend-user",
            {
                "access_token": "blend-token",
                "refresh_token": "blend-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        original_markets = settings.auto_trade_markets

        with patch("app.bot.handlers.MarketDataService") as market_service_cls, \
             patch("app.bot.handlers.GeminiService") as gemini_cls:

            market_service = market_service_cls.return_value
            market_service.get_historical_ticks.return_value = [
                {"quote": 100.0, "timestamp": "1"},
                {"quote": 101.0, "timestamp": "2"},
                {"quote": 103.5, "timestamp": "3"},
                {"quote": 104.6, "timestamp": "4"},
                {"quote": 106.0, "timestamp": "5"},
            ]

            gemini_cls.return_value.analyze.return_value = type(
                "Analysis",
                (),
                {
                    "market_analyses": [
                        type(
                            "MarketAnalysis",
                            (),
                            {
                                "symbol": "step_300",
                                "direction": "BULLISH",
                                "confidence": 0.85,
                                "setup_quality": 0.9,
                                "reasoning": "AI says bullish.",
                            },
                        )(),
                        type(
                            "MarketAnalysis",
                            (),
                            {
                                "symbol": "step_500",
                                "direction": "BEARISH",
                                "confidence": 0.2,
                                "setup_quality": 0.3,
                                "reasoning": "AI says bearish.",
                            },
                        )(),
                    ],
                    "winner_symbol": "step_300",
                    "winner_direction": "BULLISH",
                    "winner_confidence": 0.85,
                    "winner_setup_quality": 0.9,
                    "overall_reasoning": "AI-led bullish setup.",
                },
            )()

            object.__setattr__(
                settings,
                "auto_trade_markets",
                ["step_300", "step_500"],
            )

            try:
                setup = build_auto_trade_setup("blend-user")
            finally:
                object.__setattr__(
                    settings,
                    "auto_trade_markets",
                    original_markets,
                )

        self.assertEqual(setup["symbol"], "step_300")
        self.assertGreater(setup["confidence"], 0.0)
        self.assertIn("AI", setup["reasoning"])
        self.assertIn("Technical", setup["reasoning"])

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
            "app.bot.callbacks.execute_trade",
            new=AsyncMock(
                return_value={
                    "status": "accepted",
                    "contract_id": "con-123",
                    "proposal_id": "prop-123",
                    "buy_price": 1.0,
                }
            ),
        ) as execute_trade_mock:
            asyncio.run(dispatch_callback(FakeUpdate(), None))

        execute_trade_mock.assert_called_once()

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
        proposal = trading.create_proposal("R_100", amount=10.0)

        self.assertIn("proposal_id", proposal)
        self.assertEqual(proposal["currency"], "USD")

        buy = trading.buy_contract(
            proposal["proposal_id"],
            float(proposal["ask_price"]),
            "R_100",
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
        self.assertEqual(result[0]["symbol"], "R_100")
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

        self.assertIn("🤖 YIELDS", message)
        self.assertIn("Connect your Deriv account", message)
        self.assertIn("OAuth", message)

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
        self.assertIn("AI scans 5 Step Indices", message)
        self.assertIn("Expiry: 30 seconds", message)

    def test_exactly_five_step_markets_are_normalized(self):
        scanner = YieldsScanner()
        snapshots = scanner.collect_step_market_snapshots(
            MarketDataService()
        )

        self.assertEqual(len(snapshots), 5)
        self.assertEqual(
            [snapshot.symbol for snapshot in snapshots],
            list(settings.auto_trade_markets),
        )

    def test_gemini_service_returns_valid_structured_analysis(self):
        scanner = YieldsScanner()
        snapshots = scanner.collect_step_market_snapshots(
            MarketDataService()
        )
        analysis = GeminiService(
            api_key="fake-key",
            model="gemini-2.0-flash",
        ).analyze(snapshots)

        self.assertIsInstance(analysis, AutoTradeAnalysis)
        self.assertIn(
            analysis.winner_symbol,
            settings.auto_trade_markets,
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

        self.assertEqual(payload["underlying_symbol"], "R_300")
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

        self.assertEqual(payload["underlying_symbol"], "R_300")
        self.assertEqual(payload["contract_type"], "PUT")

    def test_martingale_state_machine(self):
        service = AutoTradeService()
        run = service.start_run(
            "martingale-user",
            "step_300",
            "BULLISH",
            10.0,
        )

        self.assertEqual(run.current_stake, 10.0)

        service.advance_after_result("martingale-user", False)
        self.assertEqual(
            service.get_run("martingale-user").current_stake,
            20.0,
        )

        service.advance_after_result("martingale-user", False)
        self.assertEqual(
            service.get_run("martingale-user").current_stake,
            40.0,
        )

        service.advance_after_result("martingale-user", True)
        self.assertEqual(
            service.get_run("martingale-user").cycle,
            2,
        )
        self.assertEqual(
            service.get_run("martingale-user").current_stake,
            10.0,
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


if __name__ == "__main__":
    unittest.main()