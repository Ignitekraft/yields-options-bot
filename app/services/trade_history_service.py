from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings


class TradeHistoryService:
    """Persist settled Yields contracts and load user-scoped statistics."""

    def __init__(
        self,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.supabase_url = (supabase_url or settings.supabase_url).rstrip("/")
        self.service_role_key = (
            service_role_key or settings.supabase_service_role_key
        )
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.supabase_url and self.service_role_key)

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        if not self.is_configured:
            raise RuntimeError("SUPABASE_TRADE_HISTORY_NOT_CONFIGURED")

        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def record_settled_trade(self, trade: dict[str, Any]) -> None:
        payload = {
            "telegram_user_id": str(trade["telegram_user_id"]),
            "contract_id": str(trade["contract_id"]),
            "account_mode": str(trade["account_mode"]).lower(),
            "market_symbol": str(trade["market_symbol"]),
            "direction": str(trade["direction"]).upper(),
            "stake": round(float(trade["stake"]), 2),
            "profit": round(float(trade["profit"]), 2),
            "outcome": "win" if float(trade["profit"]) > 0 else "loss",
            "direction_accurate": trade.get("direction_accurate"),
            "tokens_spent": max(0, int(trade.get("tokens_spent", 0))),
            "settled_at": trade.get("settled_at") or datetime.now(timezone.utc).isoformat(),
        }
        url = f"{self.supabase_url}/rest/v1/trade_history"

        with httpx.Client(transport=self.transport, timeout=8.0) as client:
            response = client.post(
                url,
                params={"on_conflict": "telegram_user_id,contract_id"},
                headers=self._headers("resolution=ignore-duplicates,return=minimal"),
                json=payload,
            )
            response.raise_for_status()

    def get_user_stats(self, telegram_user_id: str | int) -> dict[str, Any]:
        url = f"{self.supabase_url}/rest/v1/rpc/get_user_trade_stats"

        with httpx.Client(transport=self.transport, timeout=8.0) as client:
            response = client.post(
                url,
                headers=self._headers(),
                json={"p_telegram_user_id": str(telegram_user_id)},
            )
            response.raise_for_status()
            result = response.json()

        if not isinstance(result, dict):
            raise RuntimeError("SUPABASE_TRADE_STATS_RESPONSE_INVALID")
        return result