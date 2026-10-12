from __future__ import annotations


class AuthenticatedDerivWebSocket:
    """Future authenticated Deriv WebSocket flow for account and trading operations."""

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    def get_connection_url(self) -> str:
        return "https://developers.deriv.com/"  # placeholder to remind implementation must come from OTP flow
