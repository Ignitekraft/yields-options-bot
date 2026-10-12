from __future__ import annotations


class TradingService:
    """Demo-only trading layer for proposal, buy, portfolio, and contract monitoring flows."""

    def __init__(self) -> None:
        self._demo_balance = 10000.0
        self._portfolio: list[dict[str, object]] = []

    def get_balance(self) -> dict[str, object]:
        return {"account_type": "demo", "currency": "USD", "balance": round(self._demo_balance, 2)}

    def get_portfolio(self) -> dict[str, object]:
        return {"account_type": "demo", "positions": self._portfolio, "count": len(self._portfolio)}

    def create_proposal(
        self,
        symbol: str,
        amount: float = 10,
        contract_type: str = "MULTDOWN",
        currency: str = "USD",
        duration: int = 60,
        duration_unit: str = "s",
        multiplier: int = 10,
    ) -> dict[str, object]:
        return {
            "proposal_id": f"prop-demo-{symbol.lower()}-{duration}",
            "symbol": symbol,
            "amount": amount,
            "contract_type": contract_type,
            "currency": currency,
            "duration": duration,
            "duration_unit": duration_unit,
            "multiplier": multiplier,
            "ask_price": round(amount * 0.85, 2),
            "status": "valid",
        }

    def buy_contract(self, proposal_id: str, price: float, symbol: str) -> dict[str, object]:
        if price <= 0:
            return {"status": "rejected", "reason": "invalid_price"}

        contract_id = f"contract-demo-{symbol.lower()}-{len(self._portfolio) + 1}"
        position = {
            "contract_id": contract_id,
            "proposal_id": proposal_id,
            "symbol": symbol,
            "status": "open",
            "buy_price": price,
            "payout": round(price * 1.9, 2),
        }
        self._portfolio.append(position)
        self._demo_balance -= price
        return {"status": "accepted", "contract_id": contract_id, "symbol": symbol, "price": price}

    def monitor_contract(self, contract_id: str) -> dict[str, object]:
        for position in self._portfolio:
            if position["contract_id"] == contract_id:
                return {"contract_id": contract_id, "status": position["status"], "payout": position["payout"]}
        return {"contract_id": contract_id, "status": "not_found"}

    def sell_contract(self, contract_id: str, price: float | None = None) -> dict[str, object]:
        for position in self._portfolio:
            if position["contract_id"] == contract_id:
                position["status"] = "closed"
                settlement_price = price if price is not None else float(position["payout"])
                self._demo_balance += settlement_price
                return {"status": "closed", "contract_id": contract_id, "settlement": settlement_price}
        return {"status": "not_found", "contract_id": contract_id}
