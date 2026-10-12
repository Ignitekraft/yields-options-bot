from __future__ import annotations

from app.services.deriv.market_data import MarketDataService
from app.services.scanner import YieldsScanner
from app.services.trading_service import TradingService


class YieldsOrchestrator:
    """Coordinates the scanner and the demo trading flow with a clean service boundary."""

    def __init__(self, market_data_service: MarketDataService | None = None, scanner: YieldsScanner | None = None) -> None:
        self.market_data_service = market_data_service or MarketDataService()
        self.scanner = scanner or YieldsScanner()
        self.trading_service = TradingService()

    def scan_symbol(self, symbol: str | None = None) -> dict[str, object]:
        target_symbol = symbol or self.market_data_service.get_active_symbols()[0]["symbol"]
        history = self.market_data_service.get_historical_ticks(target_symbol, count=25)
        live_tick = self.market_data_service.subscribe_ticks(target_symbol)
        signal = self.scanner.analyze_live_tick(history, live_tick, symbol=target_symbol)
        return {
            "symbol": target_symbol,
            "signal": signal.signal,
            "confidence": signal.confidence,
            "message": signal.message,
            "direction": signal.direction,
        }

    def build_trade_plan(self, symbol: str, amount: float = 10.0) -> dict[str, object]:
        signal = self.scan_symbol(symbol)
        proposal = self.trading_service.create_proposal(
            symbol=symbol,
            amount=amount,
            contract_type="MULTDOWN" if signal["signal"] == "SELL" else "MULTUP",
        )
        return {
            "signal": signal,
            "proposal": proposal,
            "action": "buy" if signal["signal"] in {"BUY", "SELL"} else "hold",
        }
