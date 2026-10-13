from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Tuple
from urllib.parse import urlencode


def generate_state() -> str:
    """Generate a cryptographically secure OAuth state value."""
    return secrets.token_urlsafe(32)


def generate_pkce_pair() -> Tuple[str, str]:
    """Return a PKCE verifier and matching S256 code challenge."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).rstrip(b"=").decode("utf-8")
    return verifier, challenge


def build_deriv_oauth_url(client_id: str, redirect_uri: str, scope: str, state: str, code_challenge: str) -> str:
    """Build a Deriv OAuth authorization URL with PKCE, app_id, and state validation requirements."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "app_id": client_id,  # Required by Deriv OAuth to prevent silent redirect drop
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return "https://oauth.deriv.com/oauth2/authorize?" + urlencode(params)


def create_deriv_oauth_link(client_id: str, redirect_uri: str, scope: str) -> Tuple[str, str, str]:
    """Generate a state, verifier and challenge and return the authorization URL."""
    state = generate_state()
    verifier, challenge = generate_pkce_pair()
    url = build_deriv_oauth_url(client_id, redirect_uri, scope, state, challenge)
    return state, verifier, url