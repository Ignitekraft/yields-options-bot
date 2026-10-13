from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
import websockets

from app.config import settings
from app.services.auth_service import AuthService


OPTIONS_ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"


class AccountService:
    """Handles live Deriv Options-account discovery, balances, and OTP sessions."""

    def __init__(self, auth_service: AuthService | None = None) -> None:
        self.auth_service = auth_service or AuthService()

    @staticmethod
    def _placeholder_account_id(value: Any) -> bool:
        if value is None:
            return True

        normalized = str(value).strip().lower()

        return normalized in {
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

        if normalized in {"demo", "virtual"}:
            return "demo"

        return normalized

    @staticmethod
    def _normalize_account(record: dict[str, Any]) -> dict[str, Any] | None:
        account_id = record.get("account_id")

        if AccountService._placeholder_account_id(account_id):
            return None

        return {
            "account_id": str(account_id),
            "account_type": AccountService._normalize_account_type(
                record.get("account_type")
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

        for candidate in candidates:
            account = cls._normalize_account(candidate)
            if account is not None:
                normalized.append(account)

        return normalized

    async def _fetch_accounts(self, access_token: str) -> list[dict[str, Any]]:
        if not access_token:
            raise ValueError("DERIV_NOT_AUTHENTICATED")

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                OPTIONS_ACCOUNTS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code != 200:
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

    def get_account_status(self, user_id: str | int) -> dict[str, Any]:
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

    async def get_trade_account(self, user_id: str | int) -> dict[str, Any]:
        """
        Resolve the actual current Options account associated with the OAuth
        credentials.

        We do NOT blindly trust the account_id stored during OAuth completion,
        because an old/stale/non-Options identifier can cause AccountNotFound
        when requesting an OTP.
        """
        user_key = str(user_id)

        access_token = self.auth_service.get_valid_access_token(user_key)

        if not access_token:
            raise ValueError("DERIV_NOT_AUTHENTICATED")

        connected = self.auth_service.get_connected_account(user_key) or {}

        if not isinstance(connected, dict):
            connected = {}

        live_accounts = await self._fetch_accounts(access_token)

        stored_account_id = connected.get("account_id")
        stored_loginid = connected.get("loginid")
        stored_id = connected.get("id")

        stored_identifiers = {
            str(value).strip()
            for value in (
                stored_account_id,
                stored_loginid,
                stored_id,
            )
            if not self._placeholder_account_id(value)
        }

        # 1. Exact account-id match is always preferred.
        for account in live_accounts:
            if account["account_id"] in stored_identifiers:
                return account

        preferred_type = self._normalize_account_type(
            connected.get("account_type") or connected.get("type")
        )

        preferred_currency = str(
            connected.get("currency") or "USD"
        ).upper()

        candidates = live_accounts

        # 2. Match the account type if OAuth state contains it.
        if preferred_type:
            typed = [
                account
                for account in candidates
                if account["account_type"] == preferred_type
            ]

            if typed:
                candidates = typed

        # 3. Match currency where possible.
        currency_matched = [
            account
            for account in candidates
            if account["currency"] == preferred_currency
        ]

        if currency_matched:
            candidates = currency_matched

        # 4. If there is exactly one remaining account, it is safe to use.
        if len(candidates) == 1:
            return candidates[0]

        # 5. If there is only one Options account at all, use it.
        if len(live_accounts) == 1:
            return live_accounts[0]

        raise ValueError(
            "OPTIONS_ACCOUNT_SELECTION_AMBIGUOUS"
        )

    @staticmethod
    async def request_account_otp_url(
        access_token: str,
        account_id: str,
    ) -> str:
        if not access_token:
            raise ValueError("DERIV_NOT_AUTHENTICATED")

        if not account_id:
            raise ValueError("MISSING_OPTIONS_ACCOUNT_ID")

        endpoint = (
            f"{OPTIONS_ACCOUNTS_URL}/"
            f"{account_id}/otp"
        )

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code != 200:
            details = (response.text or "")[:300].replace("\n", " ")

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

        if "api.derivws.com" not in parsed.netloc.lower():
            raise ValueError("OTP_WS_URL_UNEXPECTED_HOST")

        return ws_url

    async def get_live_balances(
        self,
        user_id: str | int,
    ) -> dict[str, Any]:
        user_key = str(user_id)

        access_token = self.auth_service.get_valid_access_token(user_key)

        if not access_token:
            return {
                "real": {
                    "available": False,
                    "balance": None,
                    "currency": "USD",
                },
                "demo": {
                    "available": False,
                    "balance": None,
                    "currency": "USD",
                },
                "tokens": 0,
            }

        accounts = await self._fetch_accounts(access_token)

        result: dict[str, Any] = {
            "real": {
                "available": False,
                "balance": None,
                "currency": "USD",
            },
            "demo": {
                "available": False,
                "balance": None,
                "currency": "USD",
            },
            "tokens": 0,
        }

        for account in accounts:
            account_type = account["account_type"]

            if account_type not in {"real", "demo"}:
                continue

            balance = account.get("balance")

            if balance is not None:
                try:
                    balance = float(balance)
                except (TypeError, ValueError):
                    balance = None

            result[account_type] = {
                "available": True,
                "balance": balance,
                "currency": account["currency"],
                "account_id": account["account_id"],
            }

            # Preserve the existing "tokens" behavior where possible.
            if balance is not None:
                result["tokens"] += 1

            # Refresh the live balance through the account-scoped WS.
            try:
                ws_url = await self.request_account_otp_url(
                    access_token,
                    account["account_id"],
                )

                async with websockets.connect(
                    ws_url,
                    open_timeout=15,
                    ping_timeout=20,
                    ping_interval=20,
                ) as websocket:
                    await websocket.send(
                        '{"balance":1,"subscribe":1,"req_id":1}'
                    )

                    raw = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=20,
                    )

                    message = raw
                    if isinstance(raw, str):
                        import json

                        message = json.loads(raw)

                    if (
                        isinstance(message, dict)
                        and isinstance(message.get("balance"), dict)
                    ):
                        live_balance = message["balance"].get("balance")

                        if live_balance is not None:
                            result[account_type]["balance"] = float(
                                live_balance
                            )

            except Exception:
                # The REST account balance remains usable if the optional
                # live balance socket fails.
                continue

        return result