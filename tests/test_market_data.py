import unittest

from app.main import app
from app.services.account_service import AccountService
from app.services.auth_service import AuthService
from app.services.deriv.auth import build_deriv_oauth_url
from app.services.deriv.market_data import MarketDataService
from app.services.scanner import SignalResult, YieldsScanner
from app.services.trading_service import TradingService


class MarketDataTests(unittest.TestCase):
    def test_market_data_normalizes_tick_payload(self):
        service = MarketDataService()

        normalized = service.normalize_tick(
            {
                "tick": {"symbol": "R_100", "quote": 123.45, "epoch": 1710000000},
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

    def test_oauth_state_and_pkce_flow(self):
        service = AuthService()
        session = service.begin_oauth_session("telegram-user-42")

        self.assertIn("state", session)
        self.assertIn("code_verifier", session)
        self.assertIn("code_challenge", session)
        self.assertTrue(service.validate_state("telegram-user-42", session["state"]))

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

        buy = trading.buy_contract(proposal["proposal_id"], float(proposal["ask_price"]), "R_100")
        self.assertEqual(buy["status"], "accepted")
        self.assertIn("contract_id", buy)

    def test_fastapi_app_registers_startup_hook(self):
        self.assertTrue(app.router.on_startup)


if __name__ == "__main__":
    unittest.main()
