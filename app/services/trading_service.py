from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.trade_execution_service import TradeExecutionService


class TradingService:
    """
    Compatibility facade for older Yields orchestration code.

    IMPORTANT:
    This service is Rise/Fall only.

    Actual authenticated trading is performed by TradeExecutionService.
    This class does not maintain fake balances or fake portfolio positions.
    """

    STEP_MARKET_MAP = TradeExecutionService.STEP_MARKET_MAP

    def __init__(
        self,
        auth_service: Any | None = None,
        account_service: Any | None = None,
    ) -> None:
        self.auth_service = auth_service
        self.account_service = account_service
        self.execution_service = TradeExecutionService()

    @staticmethod
    def normalize_step_symbol(symbol: str) -> str:
        return TradeExecutionService.normalize_step_symbol(
            symbol
        )

    @staticmethod
    def resolve_contract_type(contract_type: str) -> str:
        return TradeExecutionService.resolve_contract_type(
            contract_type
        )

    def get_valid_access_token(
        self,
        telegram_user_id: str | int,
    ) -> str | None:
        if self.auth_service is None:
            return None

        getter = getattr(
            self.auth_service,
            "get_valid_access_token",
            None,
        )

        if getter is None:
            return None

        return getter(
            str(telegram_user_id)
        )

    @staticmethod
    def get_deriv_ws_url(
        app_id: str | int | None = None,
    ) -> str:
        """
        Retained only for compatibility with old callers.

        It MUST NOT be used for authenticated trading.
        """
        del app_id

        return (
            "wss://api.derivws.com/"
            "trading/v1/options/ws/public"
        )

    def create_proposal(
        self,
        symbol: str,
        amount: float = 10.0,
        contract_type: str = "CALL",
        currency: str = "USD",
        duration: int = settings.default_expiry_seconds,
        duration_unit: str = "s",
        multiplier: int | None = None,
    ) -> dict[str, object]:
        """
        Build a real Rise/Fall proposal request.

        This method intentionally does not invent a proposal ID or ask price.
        A real proposal ID/price can only come from Deriv's WebSocket.
        """
        del multiplier

        if duration != settings.default_expiry_seconds:
            raise ValueError(
                f"YIELDS_EXPIRY_MUST_BE_{settings.default_expiry_seconds}_SECONDS"
            )

        if duration_unit != "s":
            raise ValueError(
                "YIELDS_EXPIRY_UNIT_MUST_BE_SECONDS"
            )

        resolved_symbol = self.normalize_step_symbol(
            symbol
        )

        resolved_contract_type = self.resolve_contract_type(
            contract_type
        )

        stake = TradeExecutionService.validate_stake(
            amount
        )

        payload = (
            TradeExecutionService.build_proposal_payload(
                symbol=resolved_symbol,
                direction=resolved_contract_type,
                stake=stake,
                expiry_seconds=settings.default_expiry_seconds,
                currency=currency,
            )
        )

        return {
            "status": "ready",
            "symbol": resolved_symbol,
            "underlying_symbol": resolved_symbol,
            "contract_type": resolved_contract_type,
            "amount": stake,
            "currency": str(currency).upper(),
            "duration": settings.default_expiry_seconds,
            "duration_unit": "s",
            "request": payload,
        }

    async def execute_trade(
        self,
        telegram_user_id: str | int,
        symbol: str,
        direction: str,
        stake_amount: float,
        account_id: str,
        ws_url: str,
        currency: str = "USD",
    ) -> dict[str, Any]:
        """
        Real Rise/Fall execution facade.

        Normally Auto Trade should call TradeExecutionService directly.
        This exists for older service consumers.
        """
        token = self.get_valid_access_token(
            telegram_user_id
        )

        return await self.execution_service.execute_trade(
            user_id=telegram_user_id,
            market_symbol=symbol,
            direction=direction,
            stake=stake_amount,
            account_id=account_id,
            access_token=token,
            currency=currency,
            ws_url=ws_url,
        )

    async def execute_trade_cycles(
        self,
        telegram_user_id: str | int,
        symbol: str,
        contract_type: str,
        stake_amount: float,
        duration_seconds: int = settings.default_expiry_seconds,
        cycles: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Legacy compatibility method.

        This method deliberately does NOT implement the Yields Auto Trade
        five-winning-cycle state machine.

        The proper Auto Trade controller must decide:
            win  -> next cycle + reset stake
            loss -> same cycle + martingale
        and then call execute_trade() for the next contract.

        Keeping that state machine outside this compatibility facade prevents
        two different parts of the application from independently controlling
        the same Auto Trade session.
        """
        del telegram_user_id
        del symbol
        del contract_type
        del stake_amount
        del duration_seconds
        del cycles

        raise RuntimeError(
            "LEGACY_EXECUTE_TRADE_CYCLES_DISABLED:"
            "USE_AUTO_TRADE_STATE_MACHINE"
        )

    def get_balance(self) -> dict[str, object]:
        raise RuntimeError(
            "TradingService does not maintain simulated balances. "
            "Use AccountService.get_live_balances()."
        )

    def get_portfolio(self) -> dict[str, object]:
        raise RuntimeError(
            "TradingService does not maintain a simulated portfolio. "
            "Use authenticated Deriv portfolio monitoring."
        )

    def buy_contract(
        self,
        proposal_id: str,
        price: float,
        symbol: str,
    ) -> dict[str, object]:
        raise RuntimeError(
            "Direct synchronous buy_contract() is disabled. "
            "Use TradeExecutionService.execute_trade()."
        )

    def monitor_contract(
        self,
        contract_id: str,
    ) -> dict[str, object]:
        raise RuntimeError(
            "Synchronous monitor_contract() is disabled. "
            "Use TradeExecutionService.monitor_contract()."
        )

    def sell_contract(
        self,
        contract_id: str,
        price: float | None = None,
    ) -> dict[str, object]:
        del price

        raise RuntimeError(
            "Synchronous sell_contract() is disabled. "
            "Rise/Fall Auto Trade settlement is monitored through "
            "proposal_open_contract."
        )