from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import inspect
import logging
import re
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_RATE_LIMIT_RETRIES = 3
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)


def is_http_429(error: Exception) -> bool:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "status", None)
    if status is None:
        status = getattr(error, "status_code", None)
    return status == 429 or bool(re.search(r"\bHTTP\s+429\b", str(error)))


def _retry_after_seconds(error: Exception, retry_index: int) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    retry_after = headers.get("Retry-After") if headers else None

    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(retry_after))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(
                    0.0,
                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                logger.warning("Ignoring invalid WebSocket Retry-After header")

    return RETRY_DELAYS_SECONDS[retry_index]


def _log_retry(retry_number: int, delay: float) -> None:
    logger.warning(
        "Deriv rate-limited a WebSocket handshake (HTTP 429); "
        "retry %d/%d in %.1f seconds",
        retry_number,
        MAX_RATE_LIMIT_RETRIES,
        delay,
    )


async def connect_async_with_retry(
    connect: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            pending_connection = connect(*args, **kwargs)
            if inspect.isawaitable(pending_connection):
                return await pending_connection
            return pending_connection
        except Exception as error:
            if not is_http_429(error) or attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            delay = _retry_after_seconds(error, attempt)
            _log_retry(attempt + 1, delay)
            await asyncio.sleep(delay)

    raise RuntimeError("WEBSOCKET_CONNECTION_RETRY_EXHAUSTED")


def connect_sync_with_retry(
    connect: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return connect(*args, **kwargs)
        except Exception as error:
            if not is_http_429(error) or attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            delay = _retry_after_seconds(error, attempt)
            _log_retry(attempt + 1, delay)
            time.sleep(delay)

    raise RuntimeError("WEBSOCKET_CONNECTION_RETRY_EXHAUSTED")
