from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import httpx
import websockets

from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)

DERIV_ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"
DERIV_OTP_URL = "https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp"


class AccountService:
    """Service that manages authenticated user account details and fetches real-time balances from Deriv."""

    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    def get_account_status(self, telegram_user_id: str | int) -> Dict[str, Any]:
        user_key = str(telegram_user_id)
        account = self.auth_service.get_connected_account(user_key)
        if not account or not account.get("access_token"):
            return {"connected": False}

        return {
            "connected": True,
            "account_id": account.get("account_id", "OAuth Active"),
            "type": account.get("account_type", "Demo / Real"),
            "currency": account.get("currency", "USD"),
            "balance": account.get("balance", "Ready"),
        }

    @staticmethod
    def _default_account_result(account_id: Optional[str] = None, currency: str = "USD") -> Dict[str, Any]:
        return {
            "available": False,
            "balance": None,
            "currency": currency,
            "account_id": account_id,
            "status": "unavailable",
        }

    @staticmethod
    async def request_account_otp_url(access_token: str, account_id: str) -> str:
        if not access_token:
            raise ValueError("Missing Deriv access token.")
        if not account_id:
            raise ValueError("Missing Deriv account_id.")

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                DERIV_OTP_URL.format(account_id=account_id),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code != 200:
            details = (response.text or "")[:200].replace("\n", " ")
            raise ValueError(f"OTP request rejected: HTTP {response.status_code}. {details}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("OTP response was not valid JSON.") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError("OTP response was malformed: missing data object.")

        ws_url = data.get("url")
        if not isinstance(ws_url, str) or not ws_url.strip():
            raise ValueError("OTP response missing a valid data.url.")

        parsed = urlparse(ws_url)
        if parsed.scheme != "wss":
            raise ValueError("OTP returned a non-secure WebSocket URL.")
        if not parsed.netloc:
            raise ValueError("OTP returned an invalid WebSocket URL.")
        normalized_path = (parsed.path or "").lower()
        normalized_query = (parsed.query or "").lower()
        if "websocket" not in normalized_path and "websocket" not in normalized_query and "ws" not in normalized_path and "ws" not in normalized_query:
            raise ValueError("OTP URL is not a valid WebSocket endpoint.")

        return ws_url.strip()

    @staticmethod
    def _normalize_account_list(payload: Any) -> List[Dict[str, Any]]:
        if payload is None:
            return []

        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            for key in ("data", "account_list", "accounts"):
                value = payload.get(key)
                if value is None:
                    continue
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    return [value]

            if any(key in payload for key in ("account_id", "account_type", "loginid", "balance")):
                return [payload]

        return []

    async def get_live_balances(self, telegram_user_id: str | int) -> Dict[str, Any]:
        user_key = str(telegram_user_id)
        token = self.auth_service.get_valid_access_token(user_key)
        tokens_count = self.auth_service.get_user_tokens(user_key)

        if not token:
            return {"connected": False}

        result: Dict[str, Any] = {
            "connected": True,
            "real": self._default_account_result(),
            "demo": self._default_account_result(),
            "tokens": tokens_count,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                accounts_response = await client.get(
                    DERIV_ACCOUNTS_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                logger.info("Deriv accounts GET status=%s for user=%s", accounts_response.status_code, user_key)

                if accounts_response.status_code != 200:
                    logger.warning(
                        "Deriv accounts request failed for user=%s status=%s body=%s",
                        user_key,
                        accounts_response.status_code,
                        accounts_response.text[:500],
                    )
                    result["real"]["status"] = "error"
                    result["demo"]["status"] = "error"
                    return result

                accounts_payload = accounts_response.json()
                normalized_accounts = self._normalize_account_list(accounts_payload)
                logger.info(
                    "Normalized Deriv account list for user=%s: %s",
                    user_key,
                    [
                        {"account_id": account.get("account_id"), "account_type": account.get("account_type")}
                        for account in normalized_accounts
                    ],
                )

                if not normalized_accounts:
                    logger.warning("No Deriv accounts returned for user=%s", user_key)
                    result["real"]["status"] = "error"
                    result["demo"]["status"] = "error"
                    return result

                for account in normalized_accounts:
                    account_id = account.get("account_id") or account.get("loginid") or account.get("id")
                    account_type = str(account.get("account_type") or account.get("type") or "").lower()

                    if not account_id:
                        logger.warning("Skipping Deriv account entry without account_id for user=%s: %s", user_key, account)
                        continue

                    result_key = None
                    if account_type in {"demo", "virtual"}:
                        result_key = "demo"
                    elif account_type in {"real", "live", "standard"}:
                        result_key = "real"
                    else:
                        logger.info("Skipping unsupported Deriv account type for user=%s account_id=%s account_type=%s", user_key, account_id, account_type)
                        continue

                    account_entry = {
                        "available": False,
                        "balance": None,
                        "currency": account.get("currency", "USD"),
                        "account_id": account_id,
                        "status": "unavailable",
                    }

                    try:
                        ws_url = await AccountService.request_account_otp_url(token, account_id)
                        parsed = urlparse(ws_url)
                        logger.info(
                            "Deriv OTP ready for user=%s account_id=%s account_type=%s host=%s path=%s",
                            user_key,
                            account_id,
                            account_type,
                            parsed.netloc,
                            parsed.path,
                        )

                        try:
                            async with websockets.connect(ws_url, open_timeout=15, ping_timeout=20, ping_interval=20) as websocket:
                                await websocket.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": 1}))
                                raw_message = await websocket.recv()
                        except Exception as exc:  # websocket connection/recv failure
                            logger.exception(
                                "Deriv websocket failure for user=%s account_id=%s account_type=%s",
                                user_key,
                                account_id,
                                account_type,
                            )
                            account_entry["status"] = "error"
                            result[result_key] = account_entry
                            continue

                        message = json.loads(raw_message) if isinstance(raw_message, str) else raw_message
                        if isinstance(message, dict) and message.get("error"):
                            logger.warning(
                                "Deriv balance error for user=%s account_id=%s: %s",
                                user_key,
                                account_id,
                                message.get("error"),
                            )
                            account_entry["status"] = "error"
                            result[result_key] = account_entry
                            continue

                        balance_payload = message.get("balance") if isinstance(message, dict) else None
                        if not isinstance(balance_payload, dict):
                            logger.warning(
                                "Deriv balance response missing balance payload for user=%s account_id=%s message=%s",
                                user_key,
                                account_id,
                                message,
                            )
                            account_entry["status"] = "error"
                            result[result_key] = account_entry
                            continue

                        balance_value = balance_payload.get("balance")
                        if balance_value is None:
                            logger.warning(
                                "Deriv balance value missing for user=%s account_id=%s payload=%s",
                                user_key,
                                account_id,
                                balance_payload,
                            )
                            account_entry["status"] = "error"
                            result[result_key] = account_entry
                            continue

                        currency = balance_payload.get("currency", "USD")
                        account_entry = {
                            "available": True,
                            "balance": float(balance_value),
                            "currency": currency,
                            "account_id": account_id,
                            "status": "ok",
                        }
                        result[result_key] = account_entry

                    except ValueError as exc:
                        logger.exception(
                            "Invalid Deriv balance payload for user=%s account_id=%s account_type=%s",
                            user_key,
                            account_id,
                            account_type,
                        )
                        result[result_key] = {
                            **account_entry,
                            "status": "error",
                        }
                    except httpx.HTTPError as exc:
                        logger.exception(
                            "HTTP error while fetching Deriv OTP or balance for user=%s account_id=%s account_type=%s",
                            user_key,
                            account_id,
                            account_type,
                        )
                        result[result_key] = {
                            **account_entry,
                            "status": "error",
                        }

            except httpx.HTTPError as exc:
                logger.exception("Deriv accounts request failed for user=%s", user_key)
                result["real"] = {**result["real"], "status": "error"}
                result["demo"] = {**result["demo"], "status": "error"}
                return result
            except ValueError as exc:
                logger.exception("Invalid Deriv accounts payload for user=%s", user_key)
                result["real"] = {**result["real"], "status": "error"}
                result["demo"] = {**result["demo"], "status": "error"}
                return result

        return result
