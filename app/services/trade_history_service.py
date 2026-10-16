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

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        with httpx.Client(transport=self.transport, timeout=8.0) as client:
            response = client.request(
                method,
                f"{self.supabase_url}/rest/v1/{path}",
                params=params,
                headers=self._headers(prefer),
                json=payload,
            )
            response.raise_for_status()
            return response

    def ensure_user(
        self,
        telegram_user_id: str | int,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
        account_mode: str = "demo",
    ) -> dict[str, Any]:
        mode = str(account_mode or "demo").lower()
        if mode not in {"demo", "real"}:
            mode = "demo"
        payload: dict[str, Any] = {
            "telegram_user_id": str(telegram_user_id),
            "current_account_mode": mode,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if first_name is not None:
            payload["telegram_first_name"] = first_name
        if last_name is not None:
            payload["telegram_last_name"] = last_name
        if username is not None:
            payload["telegram_username"] = username

        response = self._request(
            "POST",
            "users",
            params={"on_conflict": "telegram_user_id"},
            payload=payload,
            prefer="resolution=merge-duplicates,return=representation",
        )
        rows = response.json()
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        return self.get_user(telegram_user_id)

    def grant_first_auth_tokens(
        self,
        telegram_user_id: str | int,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "rpc/grant_first_auth_tokens",
            payload={"p_telegram_user_id": str(telegram_user_id)},
        ).json()
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("granted"), bool)
            or not isinstance(result.get("token_balance"), int)
        ):
            raise RuntimeError("SUPABASE_WELCOME_TOKEN_RESPONSE_INVALID")
        return result

    def get_user(self, telegram_user_id: str | int) -> dict[str, Any]:
        response = self._request(
            "GET",
            "users",
            params={
                "select": "telegram_user_id,current_account_mode,token_balance,"
                "telegram_first_name,telegram_last_name,telegram_username,"
                "demo_balance,demo_currency,real_balance,real_currency",
                "telegram_user_id": f"eq.{telegram_user_id}",
                "limit": "1",
            },
        )
        rows = response.json()
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        raise RuntimeError("SUPABASE_USER_NOT_FOUND")

    def update_user_mode(
        self,
        telegram_user_id: str | int,
        account_mode: str,
    ) -> None:
        mode = str(account_mode or "").lower()
        if mode not in {"demo", "real"}:
            raise ValueError("INVALID_ACCOUNT_MODE")
        self._request(
            "PATCH",
            "users",
            params={"telegram_user_id": f"eq.{telegram_user_id}"},
            payload={"current_account_mode": mode, "updated_at": datetime.now(timezone.utc).isoformat()},
            prefer="return=minimal",
        )

    def update_balance_snapshots(
        self,
        telegram_user_id: str | int,
        balances: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload: dict[str, Any] = {"updated_at": now}
        for mode in ("demo", "real"):
            entry = balances.get(mode) or {}
            if not entry.get("available") or entry.get("balance") is None:
                continue
            payload[f"{mode}_balance"] = float(entry["balance"])
            payload[f"{mode}_currency"] = str(entry.get("currency") or "USD").upper()
            payload[f"{mode}_balance_updated_at"] = now
        if len(payload) == 1:
            return
        self._request(
            "PATCH",
            "users",
            params={"telegram_user_id": f"eq.{telegram_user_id}"},
            payload=payload,
            prefer="return=minimal",
        )

    def create_session(self, session: dict[str, Any]) -> None:
        self._request(
            "POST",
            "auto_trade_sessions",
            payload={
                "id": str(session["session_id"]),
                "telegram_user_id": str(session["telegram_user_id"]),
                "account_mode": str(session["account_mode"]).lower(),
                "market_symbol": str(session["market_symbol"]),
                "direction": str(session["direction"]).upper(),
                "initial_stake": round(float(session["initial_stake"]), 2),
                "target_winning_cycles": int(session["target_winning_cycles"]),
                "session_status": "running",
            },
            prefer="return=minimal",
        )

    def update_session(self, session: dict[str, Any]) -> None:
        session_status = str(session["session_status"])
        payload = {
            "session_status": session_status,
            "winning_cycles": int(session.get("winning_cycles", 0)),
            "trade_count": int(session.get("trade_count", 0)),
            "total_staked": round(float(session.get("total_staked", 0)), 2),
            "net_profit": round(float(session.get("net_profit", 0)), 2),
        }
        if session_status != "running":
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        self._request(
            "PATCH",
            "auto_trade_sessions",
            params={
                "id": f"eq.{session['session_id']}",
                "telegram_user_id": f"eq.{session['telegram_user_id']}",
            },
            payload=payload,
            prefer="return=minimal",
        )

    def reserve_token(
        self,
        telegram_user_id: str | int,
        session_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "rpc/reserve_auto_trade_token",
            payload={
                "p_telegram_user_id": str(telegram_user_id),
                "p_session_id": str(session_id),
            },
        ).json()

    def refund_token(
        self,
        telegram_user_id: str | int,
        session_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "rpc/refund_auto_trade_token",
            payload={
                "p_telegram_user_id": str(telegram_user_id),
                "p_session_id": str(session_id),
            },
        ).json()

    def record_settled_trade(self, trade: dict[str, Any]) -> None:
        payload = {
            "telegram_user_id": str(trade["telegram_user_id"]),
            "contract_id": str(trade["contract_id"]),
            "session_id": str(trade["session_id"]),
            "account_mode": str(trade["account_mode"]).lower(),
            "market_symbol": str(trade["market_symbol"]),
            "direction": str(trade["direction"]).upper(),
            "stake": round(float(trade["stake"]), 2),
            "profit": round(float(trade["profit"]), 2),
            "outcome": "win" if float(trade["profit"]) > 0 else "loss",
            "direction_accurate": trade.get("direction_accurate"),
            "tokens_spent": max(0, int(trade.get("tokens_spent", 1))),
            "settled_at": trade.get("settled_at") or datetime.now(timezone.utc).isoformat(),
        }
        self._request(
            "POST",
            "trade_history",
            params={"on_conflict": "telegram_user_id,contract_id"},
            payload=payload,
            prefer="resolution=ignore-duplicates,return=minimal",
        )

    def get_user_stats(self, telegram_user_id: str | int) -> dict[str, Any]:
        result = self._request(
            "POST",
            "rpc/get_user_trade_stats",
            payload={"p_telegram_user_id": str(telegram_user_id)},
        ).json()

        if not isinstance(result, dict):
            raise RuntimeError("SUPABASE_TRADE_STATS_RESPONSE_INVALID")
        return result