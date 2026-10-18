from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets

from app.services.auth_service import AuthService
from app.services.deriv.websocket_connection import (
    connect_async_with_retry,
    defer_deriv_requests_after_rate_limit,
    deriv_account_scope,
    send_deriv_websocket_request,
    wait_for_deriv_request_for_account_async,
)


OPTIONS_ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"
logger = logging.getLogger(__name__)


class AccountService:
    """Discover Deriv Options accounts and create account-scoped OTP sessions."""

    def __init__(self, auth_service: AuthService | None = None) -> None:
        self.auth_service = auth_service or AuthService()

    @staticmethod
    def _placeholder_account_id(value: Any) -> bool:
        if value is None:
            return True

        return str(value).strip().lower() in {
            "",
            "oauth active",
            "oauth_active",
            "active",
            "ready",
            "demo / real",
            "demo/real",
            "connected",
        }

    @staticmethod
    def _normalize_account_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()

        if normalized in {"real", "live", "standard"}:
            return "real"

        if normalized in {"demo", "virtual", "practice"}:
            return "demo"

        return normalized

    @classmethod
    def _normalize_account(
        cls,
        record: dict[str, Any],
    ) -> dict[str, Any] | None:
        account_id = record.get("account_id")

        if cls._placeholder_account_id(account_id):
            return None

        return {
            "account_id": str(account_id).strip(),
            "account_type": cls._normalize_account_type(
                record.get("account_type") or record.get("type")
            ),
            "currency": str(record.get("currency") or "USD").upper(),
            "balance": record.get("balance"),
            "status": record.get("status"),
            "group": record.get("group"),
            "raw": record,
        }

    @classmethod
    def _extract_accounts(cls, payload: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return

            if not isinstance(value, dict):
                return

            if "account_id" in value:
                candidates.append(value)
                return

            for key in ("data", "accounts", "account_list"):
                if key in value:
                    visit(value[key])

        visit(payload)

        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for candidate in candidates:
            account = cls._normalize_account(candidate)
            if account is None:
                continue

            account_id = account["account_id"]
            if account_id in seen_ids:
                continue

            seen_ids.add(account_id)
            normalized.append(account)

        return normalized

    async def _fetch_accounts(
        self,
        access_token: str,
    ) -> list[dict[str, Any]]:
        if not access_token:
            raise ValueError("DERIV_NOT_AUTHENTICATED")

        await wait_for_deriv_request_for_account_async(
            deriv_account_scope(access_token)
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                OPTIONS_ACCOUNTS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )

        if response.status_code != 200:
            if response.status_code == 429:
                rate_limit_error = httpx.HTTPStatusError(
                    "Deriv account-list request was rate-limited",
                    request=response.request,
                    response=response,
                )
                defer_deriv_requests_after_rate_limit(rate_limit_error)
            details = (response.text or "")[:300].replace("\n", " ")
            raise ValueError(
                f"OPTIONS_ACCOUNTS_REQUEST_FAILED:"
                f"HTTP_{response.status_code}:{details}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("OPTIONS_ACCOUNTS_INVALID_JSON") from exc

        accounts = self._extract_accounts(payload)
        if not accounts:
            raise ValueError("NO_OPTIONS_ACCOUNTS_FOUND")

        return accounts

    def get_account_status(
        self,
        user_id: str | int,
    ) -> dict[str, Any]:
        user_key = str(user_id)
        record = self.auth_service.get_connected_account(user_key) or {}

        if not isinstance(record, dict):
            record = {}

        account_id = record.get("account_id")
        if self._placeholder_account_id(account_id):
            account_id = None

        return {
            "connected": bool(
                self.auth_service.get_valid_access_token(user_key)
            ),
            "account_id": account_id,
            "type": record.get("account_type") or record.get("type"),
            "currency": record.get("currency") or "USD",
            "balance": record.get("balance"),
        }

    async def get_trade_account(
        self,
        user_id: str | int,
        account_mode: str = "demo",
    ) -> dict[str, Any]:
        """Resolve exactly one live Options account of the requested mode.

        Never falls back to the opposite account mode. If the account list
        contains multiple equally eligible accounts, refuse to guess.
        """
        mode = self._normalize_account_type(account_mode)
        if mode not in {"demo", "real"}:
            raise ValueError("INVALID_ACCOUNT_MODE")

        user_key = str(user_id)
        access_token = self.auth_service.get_valid_access_token(user_key)
        if not access_token:
            raise ValueError("DERIV_NOT_AUTHENTICATED")

        live_accounts = await self._fetch_accounts(access_token)

        eligible = [
            account
            for account in live_accounts
            if account["account_type"] == mode
        ]

        if not eligible:
            raise ValueError(
                f"NO_{mode.upper()}_OPTIONS_ACCOUNT_FOUND:"
                "Trading stopped; no account-mode fallback is permitted."
            )

        connected = self.auth_service.get_connected_account(user_key) or {}
        if not isinstance(connected, dict):
            connected = {}

        preferred_currency = str(
            connected.get("currency") or "USD"
        ).upper()

        currency_matches = [
            account
            for account in eligible
            if account["currency"] == preferred_currency
        ]

        if currency_matches:
            eligible = currency_matches

        if len(eligible) != 1:
            raise ValueError(
                f"{mode.upper()}_OPTIONS_ACCOUNT_SELECTION_AMBIGUOUS:"
                "More than one eligible account remains; refusing to guess."
            )

        selected = eligible[0]

        if selected["account_type"] != mode:
            raise ValueError("ACCOUNT_MODE_VERIFICATION_FAILED")

        return selected

    @staticmethod
    async def request_account_otp_url(
        access_token: str,
        account_id: str,
    ) -> str:
        """Request a fresh account-scoped WebSocket URL."""
        if not access_token:
            raise ValueError("DERIV_NOT_AUTHENTICATED")

        if not account_id:
            raise ValueError("MISSING_OPTIONS_ACCOUNT_ID")

        endpoint = f"{OPTIONS_ACCOUNTS_URL}/{account_id}/otp"

        await wait_for_deriv_request_for_account_async(
            deriv_account_scope(access_token)
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code != 200:
            details = (response.text or "")[:300].replace("\n", " ")
            if response.status_code == 429:
                rate_limit_error = httpx.HTTPStatusError(
                    "Deriv account OTP request was rate-limited",
                    request=response.request,
                    response=response,
                )
                defer_deriv_requests_after_rate_limit(rate_limit_error)
            raise ValueError(
                f"OTP_REQUEST_REJECTED:"
                f"HTTP_{response.status_code}:{details}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("OTP_RESPONSE_INVALID_JSON") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError("OTP_RESPONSE_MISSING_DATA")

        ws_url = data.get("url")
        if not isinstance(ws_url, str) or not ws_url.strip():
            raise ValueError("OTP_RESPONSE_MISSING_WS_URL")

        ws_url = ws_url.strip()
        parsed = urlparse(ws_url)

        if parsed.scheme != "wss":
            raise ValueError("OTP_WS_URL_NOT_SECURE")

        if not parsed.netloc:
            raise ValueError("OTP_WS_URL_INVALID")

        if parsed.hostname != "api.derivws.com":
            raise ValueError("OTP_WS_URL_UNEXPECTED_HOST")

        return ws_url

    async def _read_live_balance(
        self,
        access_token: str,
        account_id: str,
    ) -> float | None:
        ws_url = await self.request_account_otp_url(
            access_token=access_token,
            account_id=account_id,
        )

        connection = await connect_async_with_retry(
            websockets.connect,
            ws_url,
            open_timeout=15,
            ping_timeout=20,
            ping_interval=20,
        )
        async with connection as websocket:
            await send_deriv_websocket_request(
                websocket,
                {
                    "balance": 1,
                    "subscribe": 1,
                    "req_id": 1,
                },
            )

            deadline = asyncio.get_running_loop().time() + 20

            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("BALANCE_REQUEST_TIMEOUT")

                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=remaining,
                )

                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")

                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue

                if not isinstance(message, dict):
                    continue

                if "error" in message:
                    error = message.get("error") or {}
                    raise ValueError(
                        str(error.get("message") or "BALANCE_REQUEST_FAILED")
                    )

                balance_data = message.get("balance")
                if not isinstance(balance_data, dict):
                    continue

                value = balance_data.get("balance")
                return None if value is None else float(value)

    async def get_current_live_balance(
        self,
        user_id: str | int,
        account_mode: str,
    ) -> dict[str, Any]:
        """Fetch a fresh balance from exactly the selected Deriv account."""
        account = await self.get_trade_account(user_id, account_mode)
        access_token = self.auth_service.get_valid_access_token(str(user_id))
        if not access_token:
            raise ValueError("DERIV_NOT_AUTHENTICATED")

        balance = await self._read_live_balance(
            access_token=access_token,
            account_id=account["account_id"],
        )
        if balance is None:
            raise ValueError("LIVE_BALANCE_UNAVAILABLE")

        return {
            "available": True,
            "balance": balance,
            "currency": account["currency"],
            "account_id": account["account_id"],
            "status": "available",
        }

    async def get_live_balances(
        self,
        user_id: str | int,
    ) -> dict[str, Any]:
        user_key = str(user_id)
        access_token = self.auth_service.get_valid_access_token(user_key)

        result: dict[str, Any] = {
            "real": {
                "available": False,
                "balance": None,
                "currency": "USD",
                "status": "unavailable",
            },
            "demo": {
                "available": False,
                "balance": None,
                "currency": "USD",
                "status": "unavailable",
            },
            "tokens": self.auth_service.get_user_tokens(user_key),
        }

        if not access_token:
            return result

        accounts = await self._fetch_accounts(access_token)

        # Only expose one unambiguous balance for each mode. Never display
        # an arbitrary account's balance if multiple accounts share a mode.
        for mode in ("real", "demo"):
            mode_accounts = [
                account
                for account in accounts
                if account["account_type"] == mode
            ]

            connected = self.auth_service.get_connected_account(user_key) or {}
            if not isinstance(connected, dict):
                connected = {}

            currency = str(connected.get("currency") or "USD").upper()
            matches = [
                account
                for account in mode_accounts
                if account["currency"] == currency
            ]

            if matches:
                mode_accounts = matches

            if len(mode_accounts) != 1:
                result[mode] = {
                    "available": False,
                    "balance": None,
                    "currency": currency,
                    "status": (
                        "unavailable"
                        if not mode_accounts
                        else "ambiguous"
                    ),
                }
                continue

            account = mode_accounts[0]
            balance: float | None = None

            try:
                raw_balance = account.get("balance")
                if raw_balance is not None:
                    balance = float(raw_balance)
            except (TypeError, ValueError):
                balance = None

            entry: dict[str, Any] = {
                "available": True,
                "balance": balance,
                "currency": account["currency"],
                "account_id": account["account_id"],
                "status": "available",
            }

            try:
                live_balance = await self._read_live_balance(
                    access_token=access_token,
                    account_id=account["account_id"],
                )
                if live_balance is not None:
                    entry["balance"] = live_balance
            except Exception:
                # Preserve the account-list balance if the optional refresh
                # fails; do not change the account's identity or mode.
                logger.exception(
                    "Could not refresh live Deriv balance for account %s",
                    account["account_id"],
                )

            result[mode] = entry

        return result