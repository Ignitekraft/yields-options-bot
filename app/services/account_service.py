def exchange_oauth_code(
        self,
        telegram_user_id: str,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str = "",
    ) -> Dict[str, Any]:
        """Exchange a Deriv authorization code for access tokens using the official auth endpoint."""
        session = self._oauth_state_store.get(telegram_user_id, {})
        code_verifier = session.get("code_verifier")
        if not code_verifier:
            raise ValueError("missing_code_verifier")

        payload = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
        }
        if client_secret:
            payload["client_secret"] = client_secret

        response = httpx.post("https://auth.deriv.com/oauth2/token", data=payload, timeout=20.0)
        response.raise_for_status()
        data = response.json()
        return self.complete_oauth_session(telegram_user_id, data)