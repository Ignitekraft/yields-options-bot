from __future__ import annotations

from app.config import settings
from app.services.deriv.market_data import MarketDataService
from app.services.scanner import YieldsScanner
from app.services.trading_service import TradingService


class YieldsOrchestrator:
    """Coordinates market scanning and Rise/Fall trade planning."""

    def __init__(
        self,
        market_data_service: MarketDataService | None = None,
        scanner: YieldsScanner | None = None,
    ) -> None:
        self.market_data_service = (
            market_data_service
            or MarketDataService()
        )
        self.scanner = scanner or YieldsScanner()
        self.trading_service = TradingService()

    def scan_symbol(
        self,
        symbol: str | None = None,
    ) -> dict[str, object]:
        target_symbol = (
            symbol
            or settings.auto_trade_markets[0]
        )

        history = (
            self.market_data_service.get_historical_ticks(
                target_symbol,
                count=5000,
            )
        )

        live_tick = (
            self.market_data_service.subscribe_ticks(
                target_symbol
            )
        )

        signal = self.scanner.analyze_live_tick(
            history,
            live_tick,
            symbol=target_symbol,
        )

        return {
            "symbol": target_symbol,
            "signal": signal.signal,
            "signal_strength": signal.signal_strength,
            "message": signal.message,
            "direction": signal.direction,
        }

    def build_trade_plan(
        self,
        symbol: str,
        amount: float = 10.0,
    ) -> dict[str, object]:
        signal = self.scan_symbol(symbol)

        if signal["signal"] not in {"BUY", "SELL"}:
            return {
                "signal": signal,
                "proposal": None,
                "action": "hold",
            }

        contract_type = "PUT" if signal["signal"] == "SELL" else "CALL"

        proposal = self.trading_service.create_proposal(
            symbol=symbol,
            amount=amount,
            contract_type=contract_type,
            duration=30,
            duration_unit="s",
        )

        return {
            "signal": signal,
            "proposal": proposal,
            "action": (
                "buy"
                if signal["signal"] in {"BUY", "SELL"}
                else "hold"
            ),
        }