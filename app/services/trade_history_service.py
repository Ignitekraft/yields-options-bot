from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class TradeHistoryService:
    """Persist settled Yields contracts and load user-scoped statistics."""

    def __init__(
        self,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        encryption_key: str | None = None,
    ) -> None:
        self.supabase_url = (supabase_url or settings.supabase_url).rstrip("/")
        self.service_role_key = (
            service_role_key or settings.supabase_service_role_key
        )
        self.transport = transport
        self.encryption_key = (
            encryption_key or settings.deriv_token_encryption_key
        )

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

    def _payout_cipher(self) -> Fernet:
        if not self.encryption_key:
            raise RuntimeError("PAYOUT_DETAILS_ENCRYPTION_KEY_NOT_CONFIGURED")
        try:
            return Fernet(self.encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("INVALID_PAYOUT_DETAILS_ENCRYPTION_KEY") from exc

    def encrypt_payout_details(self, details: str) -> str:
        return self._payout_cipher().encrypt(
            details.encode("utf-8")
        ).decode("ascii")

    def decrypt_payout_details(self, encrypted: str) -> str:
        try:
            return self._payout_cipher().decrypt(
                encrypted.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("PAYOUT_DETAILS_DECRYPTION_FAILED") from exc

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
        sync_telegram_profile: bool = False,
    ) -> dict[str, Any]:
        mode = str(account_mode or "demo").lower()
        if mode not in {"demo", "real"}:
            mode = "demo"
        payload: dict[str, Any] = {
            "telegram_user_id": str(telegram_user_id),
            "current_account_mode": mode,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if sync_telegram_profile or first_name is not None:
            payload["telegram_first_name"] = first_name
        if sync_telegram_profile or last_name is not None:
            payload["telegram_last_name"] = last_name
        if sync_telegram_profile or username is not None:
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

    def claim_welcome_gift_200(
        self,
        telegram_user_id: str | int,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "rpc/claim_welcome_gift_200",
            payload={"p_telegram_user_id": str(telegram_user_id)},
        ).json()
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("granted"), bool)
            or not isinstance(result.get("token_balance"), int)
        ):
            raise RuntimeError("SUPABASE_WELCOME_GIFT_RESPONSE_INVALID")
        return result

    def attach_referral(
        self,
        telegram_user_id: str | int,
        referrer_telegram_user_id: str | int,
    ) -> bool:
        if str(telegram_user_id) == str(referrer_telegram_user_id):
            return False
        result = self._request(
            "POST",
            "rpc/attach_referral",
            payload={
                "p_telegram_user_id": str(telegram_user_id),
                "p_referrer_telegram_user_id": str(referrer_telegram_user_id),
            },
        ).json()
        if not isinstance(result, bool):
            raise RuntimeError("SUPABASE_REFERRAL_LINK_RESPONSE_INVALID")
        return result

    def get_user(self, telegram_user_id: str | int) -> dict[str, Any]:
        response = self._request(
            "GET",
            "users",
            params={
                "select": "telegram_user_id,current_account_mode,token_balance,"
                "telegram_first_name,telegram_last_name,telegram_username,"
                "demo_balance,demo_currency,real_balance,real_currency,"
                "welcome_gift_200_claimed,is_admin,referred_by_telegram_user_id,"
                "referral_balance,payout_country,payout_details_encrypted,"
                "payout_input_step",
                "telegram_user_id": f"eq.{telegram_user_id}",
                "limit": "1",
            },
        )
        rows = response.json()
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        raise RuntimeError("SUPABASE_USER_NOT_FOUND")

    def set_payout_input_step(
        self,
        telegram_user_id: str | int,
        step: str | None,
    ) -> None:
        if step not in {None, "country", "details"}:
            raise ValueError("INVALID_PAYOUT_INPUT_STEP")
        self._request(
            "PATCH",
            "users",
            params={"telegram_user_id": f"eq.{telegram_user_id}"},
            payload={
                "payout_input_step": step,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )

    def save_payout_country(
        self,
        telegram_user_id: str | int,
        country: str,
    ) -> None:
        clean_country = " ".join(country.strip().split())
        if not clean_country or len(clean_country) > 100:
            raise ValueError("PAYOUT_COUNTRY_INVALID")
        self._request(
            "PATCH",
            "users",
            params={"telegram_user_id": f"eq.{telegram_user_id}"},
            payload={
                "payout_country": clean_country,
                "payout_input_step": "details",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )

    def save_payout_details(
        self,
        telegram_user_id: str | int,
        details: str,
    ) -> None:
        clean_details = details.strip()
        if not clean_details or len(clean_details) > 2000:
            raise ValueError("PAYOUT_DETAILS_INVALID")
        self._request(
            "PATCH",
            "users",
            params={"telegram_user_id": f"eq.{telegram_user_id}"},
            payload={
                "payout_details_encrypted": self.encrypt_payout_details(
                    clean_details
                ),
                "payout_input_step": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )

    def get_referral_summary(
        self,
        telegram_user_id: str | int,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "rpc/get_referral_summary",
            payload={"p_telegram_user_id": str(telegram_user_id)},
        ).json()
        if not isinstance(result, dict):
            raise RuntimeError("SUPABASE_REFERRAL_SUMMARY_RESPONSE_INVALID")
        return result

    def record_verified_token_purchase(
        self,
        *,
        provider: str,
        provider_purchase_id: str,
        provider_event_id: str | None,
        telegram_user_id: str | int,
        tokens: int,
        amount_paid: str | int | float,
        currency: str = "USD",
        verified_by_telegram_user_id: str | int | None = None,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "rpc/record_verified_token_purchase",
            payload={
                "p_provider": provider,
                "p_provider_purchase_id": provider_purchase_id,
                "p_provider_event_id": provider_event_id,
                "p_telegram_user_id": str(telegram_user_id),
                "p_tokens": int(tokens),
                "p_amount_paid": str(amount_paid),
                "p_currency": currency.upper(),
                "p_verified_by_telegram_user_id": (
                    str(verified_by_telegram_user_id)
                    if verified_by_telegram_user_id is not None
                    else None
                ),
            },
        ).json()
        if not isinstance(result, dict):
            raise RuntimeError("SUPABASE_TOKEN_PURCHASE_RESPONSE_INVALID")
        return result

    def create_referral_withdrawal(
        self,
        telegram_user_id: str | int,
        payout_details_encrypted: str,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "rpc/create_referral_withdrawal",
            payload={
                "p_telegram_user_id": str(telegram_user_id),
                "p_payout_details_snapshot_encrypted": payout_details_encrypted,
            },
        ).json()
        if not isinstance(result, dict) or not result.get("withdrawal_id"):
            raise RuntimeError("SUPABASE_WITHDRAWAL_RESPONSE_INVALID")
        return result

    def get_pending_referral_withdrawals(
        self,
        admin_telegram_user_id: str | int,
    ) -> list[dict[str, Any]]:
        result = self._request(
            "POST",
            "rpc/get_pending_referral_withdrawals",
            payload={"p_admin_telegram_user_id": str(admin_telegram_user_id)},
        ).json()
        if not isinstance(result, list) or any(
            not isinstance(row, dict) for row in result
        ):
            raise RuntimeError("SUPABASE_WITHDRAWALS_RESPONSE_INVALID")
        return result

    def update_referral_withdrawal(
        self,
        *,
        admin_telegram_user_id: str | int,
        withdrawal_id: str,
        status: str,
        failure_reason: str | None = None,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "rpc/admin_update_referral_withdrawal",
            payload={
                "p_admin_telegram_user_id": str(admin_telegram_user_id),
                "p_withdrawal_id": withdrawal_id,
                "p_status": status,
                "p_failure_reason": failure_reason,
            },
        ).json()
        if not isinstance(result, dict):
            raise RuntimeError("SUPABASE_WITHDRAWAL_UPDATE_RESPONSE_INVALID")
        return result

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