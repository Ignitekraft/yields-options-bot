from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import inspect
import json
import logging
import random
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_RATE_LIMIT_RETRIES = 3
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
RATE_LIMIT_JITTER_SECONDS = 0.5
WS_CONNECTION_INTERVAL_SECONDS = 4.0
WEBSOCKET_REQUEST_LIMITS = {
    "trading": ((60.0, 360), (3600.0, 14400)),
    "account": ((60.0, 100), (3600.0, 2000)),
    "portfolio": ((60.0, 30), (3600.0, 1500)),
    "other": ((60.0, 220), (3600.0, 14400)),
}
REST_IP_REQUEST_LIMITS = ((60.0, 300), (600.0, 1000))
REST_ACCOUNT_REQUEST_LIMITS = ((60.0, 80),)


class _DerivRequestLimiter:
    """Coordinate connection pacing, request budgets, and 429 cooldowns."""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, interval_seconds)
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._request_times: dict[str, deque[float]] = {}

    def _delay_or_reserve(self) -> float:
        now = time.monotonic()
        with self._lock:
            next_slot = max(self._next_request_at, self._blocked_until)
            delay = next_slot - now
            if delay <= 0:
                self._next_request_at = (
                    now
                    + self.interval_seconds
                    + random.uniform(0.0, RATE_LIMIT_JITTER_SECONDS)
                )
                return 0.0
            return delay

    async def wait_async(self) -> None:
        while True:
            delay = self._delay_or_reserve()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    def wait_sync(self) -> None:
        while True:
            delay = self._delay_or_reserve()
            if delay <= 0:
                return
            time.sleep(delay)

    def apply_cooldown(self, delay_seconds: float) -> None:
        blocked_until = time.monotonic() + max(0.0, delay_seconds)
        with self._lock:
            self._blocked_until = max(self._blocked_until, blocked_until)

    def _reserve_budget(
        self,
        scopes: tuple[tuple[str, tuple[tuple[float, int], ...]], ...],
    ) -> float:
        now = time.monotonic()
        with self._lock:
            delays = [self._blocked_until - now]
            for scope, limits in scopes:
                timestamps = self._request_times.setdefault(scope, deque())
                longest_window = max(window for window, _ in limits)
                while timestamps and timestamps[0] <= now - longest_window:
                    timestamps.popleft()

                for window, limit in limits:
                    recent = [
                        timestamp
                        for timestamp in timestamps
                        if timestamp > now - window
                    ]
                    if len(recent) >= limit:
                        delays.append(recent[len(recent) - limit] + window - now)

            delay = max(delays, default=0.0)
            if delay > 0:
                return delay

            for scope, _ in scopes:
                self._request_times[scope].append(now)
            return 0.0

    async def wait_for_budget_async(
        self,
        scopes: tuple[tuple[str, tuple[tuple[float, int], ...]], ...],
    ) -> None:
        while True:
            delay = self._reserve_budget(scopes)
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    def wait_for_budget_sync(
        self,
        scopes: tuple[tuple[str, tuple[tuple[float, int], ...]], ...],
    ) -> None:
        while True:
            delay = self._reserve_budget(scopes)
            if delay <= 0:
                return
            time.sleep(delay)


_deriv_request_limiter = _DerivRequestLimiter(
    WS_CONNECTION_INTERVAL_SECONDS
)


def deriv_account_scope(identity: str | None) -> str | None:
    if not identity:
        return None
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


async def wait_for_deriv_request_for_account_async(
    account_scope: str | None,
) -> None:
    scopes = [("rest:ip", REST_IP_REQUEST_LIMITS)]
    if account_scope:
        scopes.append(
            (
                f"rest:account:{account_scope}",
                REST_ACCOUNT_REQUEST_LIMITS,
            )
        )
    await _deriv_request_limiter.wait_for_budget_async(tuple(scopes))


def wait_for_deriv_request_sync(account_scope: str | None = None) -> None:
    """Reserve slots within Deriv's documented IP and account REST budgets."""
    scopes = [("rest:ip", REST_IP_REQUEST_LIMITS)]
    if account_scope:
        scopes.append(
            (
                f"rest:account:{account_scope}",
                REST_ACCOUNT_REQUEST_LIMITS,
            )
        )
    _deriv_request_limiter.wait_for_budget_sync(tuple(scopes))


def _websocket_request_group(payload: dict[str, Any]) -> str:
    action = next(
        (
            key
            for key in (
                "proposal",
                "proposal_open_contract",
                "buy",
                "sell",
                "balance",
                "statement",
                "portfolio",
                "profit_table",
            )
            if key in payload
        ),
        "other",
    )
    if action in {"proposal", "proposal_open_contract", "buy", "sell"}:
        return "trading"
    if action in {"balance", "statement"}:
        return "account"
    if action in {"portfolio", "profit_table"}:
        return "portfolio"
    return "other"


async def send_deriv_websocket_request(
    websocket: Any,
    payload: dict[str, Any],
) -> None:
    """Apply the documented shared WebSocket budget before sending a call."""
    group = _websocket_request_group(payload)
    await _deriv_request_limiter.wait_for_budget_async(
        ((f"websocket:{group}", WEBSOCKET_REQUEST_LIMITS[group]),)
    )
    await asyncio.wait_for(websocket.send(json.dumps(payload)), timeout=15)


def send_deriv_websocket_request_sync(
    websocket: Any,
    payload: dict[str, Any],
) -> None:
    """Synchronous variant of the documented WebSocket request budget."""
    group = _websocket_request_group(payload)
    _deriv_request_limiter.wait_for_budget_sync(
        ((f"websocket:{group}", WEBSOCKET_REQUEST_LIMITS[group]),)
    )
    websocket.send(json.dumps(payload))


def is_http_429(error: Exception) -> bool:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "status", None)
    if status is None:
        status = getattr(error, "status_code", None)
    return status == 429 or bool(re.search(r"\bHTTP\s+429\b", str(error)))


def is_deriv_rate_limit_error(error: Exception) -> bool:
    code = str(getattr(error, "code", "") or "")
    message = str(error)
    return bool(
        re.search(r"rate.?limit|too many requests", f"{code} {message}", re.I)
    )


def _retry_after_seconds(error: Exception, retry_index: int) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    retry_after = headers.get("Retry-After") if headers else None

    if retry_after:
        try:
            delay = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(retry_after))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = max(
                    0.0,
                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                logger.warning("Ignoring invalid WebSocket Retry-After header")
                delay = RETRY_DELAYS_SECONDS[retry_index]
        return delay + random.uniform(0.0, RATE_LIMIT_JITTER_SECONDS)

    return (
        RETRY_DELAYS_SECONDS[retry_index]
        + random.uniform(0.0, RATE_LIMIT_JITTER_SECONDS)
    )


def _log_retry(retry_number: int, delay: float) -> None:
    logger.warning(
        "Deriv rate limit received; outbound requests paused for %.1f seconds "
        "before retry %d/%d",
        delay,
        retry_number,
        MAX_RATE_LIMIT_RETRIES,
    )


def defer_deriv_requests_after_rate_limit(error: Exception) -> float:
    """Pause all outbound Deriv requests after a rate-limit response."""
    delay = _retry_after_seconds(error, 0)
    _deriv_request_limiter.apply_cooldown(delay)
    logger.warning(
        "Deriv REST request was rate-limited; pausing outbound requests "
        "for %.1f seconds",
        delay,
    )
    return delay


async def connect_async_with_retry(
    connect: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        await _deriv_request_limiter.wait_async()
        try:
            pending_connection = connect(*args, **kwargs)
            if inspect.isawaitable(pending_connection):
                return await pending_connection
            return pending_connection
        except Exception as error:
            if not is_http_429(error):
                raise
            delay = _retry_after_seconds(
                error,
                min(attempt, len(RETRY_DELAYS_SECONDS) - 1),
            )
            _deriv_request_limiter.apply_cooldown(delay)
            if attempt == MAX_RATE_LIMIT_RETRIES:
                logger.error(
                    "Deriv WebSocket connection retries exhausted; "
                    "all outbound requests paused for %.1f seconds",
                    delay,
                )
                raise
            _log_retry(attempt + 1, delay)

    raise RuntimeError("WEBSOCKET_CONNECTION_RETRY_EXHAUSTED")


def connect_sync_with_retry(
    connect: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        _deriv_request_limiter.wait_sync()
        try:
            return connect(*args, **kwargs)
        except Exception as error:
            if not is_http_429(error):
                raise
            delay = _retry_after_seconds(
                error,
                min(attempt, len(RETRY_DELAYS_SECONDS) - 1),
            )
            _deriv_request_limiter.apply_cooldown(delay)
            if attempt == MAX_RATE_LIMIT_RETRIES:
                logger.error(
                    "Deriv WebSocket connection retries exhausted; "
                    "all outbound requests paused for %.1f seconds",
                    delay,
                )
                raise
            _log_retry(attempt + 1, delay)

    raise RuntimeError("WEBSOCKET_CONNECTION_RETRY_EXHAUSTED")
