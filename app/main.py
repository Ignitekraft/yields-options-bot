from __future__ import annotations

from fastapi import FastAPI, Query

from app.services.auth_service import AuthService

app = FastAPI(title="Yields", version="0.1.0")
auth_service = AuthService()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/deriv/callback")
def deriv_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> dict[str, str]:
    """Validate the Deriv OAuth return, reject mismatches, and acknowledge a successful connection."""
    if error:
        return {"status": "error", "error": error}

    if not code or not state:
        return {"status": "error", "error": "missing_code_or_state"}

    telegram_user_id = auth_service.get_pending_user_for_state(state)
    if telegram_user_id is None or not auth_service.validate_state(telegram_user_id, state):
        return {"status": "error", "error": "state_mismatch"}

    # In a full production implementation, this exchange would call Deriv's token endpoint
    # with the authorization code and PKCE verifier. The secure server-side pattern remains
    # the same, and this app keeps the tokens off the Telegram layer and out of the client.
    payload = {
        "access_token": "demo-access-token",
        "refresh_token": "demo-refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "connected_at": "2026-10-13T00:00:00Z",
    }
    auth_service.complete_oauth_session(telegram_user_id, payload)
    return {"status": "success", "message": "Deriv connected", "telegram_user_id": telegram_user_id}


@app.get("/")
def root() -> dict[str, str]:
    return {"app": "Yields", "mode": "telegram-first"}
