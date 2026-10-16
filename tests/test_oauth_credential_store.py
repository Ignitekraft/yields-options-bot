from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet

from app.services.auth_service import AuthService, TOKEN_FILE
from app.services.oauth_credential_store import SupabaseOAuthCredentialStore


class OAuthCredentialStoreTests(unittest.TestCase):
    def test_supabase_store_encrypts_and_restores_credentials(self):
        encryption_key = Fernet.generate_key().decode("ascii")
        rows: dict[str, dict[str, str]] = {}
        captured_requests: list[httpx.Request] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            if request.url.path.endswith("/users"):
                return httpx.Response(201, json=[])
            if request.method == "POST":
                payload = json.loads(request.content)
                rows[payload["telegram_user_id"]] = payload
                return httpx.Response(201, json=[])
            if request.method == "GET":
                row = rows.get(request.url.params["telegram_user_id"][3:])
                return httpx.Response(200, json=[row] if row else [])
            if request.method == "DELETE":
                rows.pop(request.url.params["telegram_user_id"][3:], None)
                return httpx.Response(204)
            return httpx.Response(405)

        store = SupabaseOAuthCredentialStore(
            supabase_url="https://supabase.example",
            service_role_key="backend-service-key",
            encryption_key=encryption_key,
            transport=httpx.MockTransport(handle_request),
        )
        credentials = {
            "access_token": "deriv-access-secret",
            "refresh_token": "deriv-refresh-secret",
            "account_id": "demo-account",
        }

        store.save("telegram-user-1", credentials)

        encrypted = rows["telegram-user-1"]["encrypted_credentials"]
        self.assertNotIn("deriv-access-secret", encrypted)
        self.assertNotIn("deriv-refresh-secret", encrypted)
        self.assertEqual(store.load("telegram-user-1"), credentials)
        self.assertTrue(
            all(
                request.headers["Authorization"]
                == "Bearer backend-service-key"
                for request in captured_requests
            )
        )

    def test_auth_service_restores_credentials_from_durable_store(self):
        class MemoryCredentialStore:
            is_enabled = True
            is_supabase_configured = True

            def __init__(self):
                self.records: dict[str, dict[str, object]] = {}

            def save(self, user_id, credentials):
                self.records[str(user_id)] = dict(credentials)

            def load(self, user_id):
                return self.records.get(str(user_id))

            def delete(self, user_id):
                self.records.pop(str(user_id), None)

        store = MemoryCredentialStore()
        first_process = AuthService(store)
        with (
            patch.object(
                first_process,
                "_hydrate_account_metadata",
                side_effect=lambda _token, existing: existing,
            ),
            patch.object(first_process, "_load_json", return_value={}),
            patch.object(first_process, "_save_json") as save_json,
        ):
            first_process.complete_oauth_session(
                "telegram-user-2",
                {
                    "access_token": "persistent-access-token",
                    "refresh_token": "persistent-refresh-token",
                    "expires_in": 3600,
                },
            )

        self.assertFalse(
            any(
                call.args[0] == TOKEN_FILE
                for call in save_json.call_args_list
            )
        )
        restarted_process = AuthService(store)
        self.assertEqual(
            restarted_process.get_valid_access_token("telegram-user-2"),
            "persistent-access-token",
        )

    def test_legacy_local_credentials_are_migrated_and_removed(self):
        class MemoryCredentialStore:
            is_enabled = True
            is_supabase_configured = True

            def __init__(self):
                self.records: dict[str, dict[str, object]] = {}

            def save(self, user_id, credentials):
                self.records[str(user_id)] = dict(credentials)

            def load(self, user_id):
                return self.records.get(str(user_id))

            def delete(self, user_id):
                self.records.pop(str(user_id), None)

        legacy_record = {
            "access_token": "legacy-access-token",
            "refresh_token": "legacy-refresh-token",
        }
        local_tokens = {"telegram-user-legacy": legacy_record.copy()}
        store = MemoryCredentialStore()
        auth = AuthService(store)

        def load_json(path):
            return local_tokens.copy() if path == TOKEN_FILE else {}

        def save_json(path, payload):
            if path == TOKEN_FILE:
                local_tokens.clear()
                local_tokens.update(payload)

        with (
            patch.object(auth, "_load_json", side_effect=load_json),
            patch.object(auth, "_save_json", side_effect=save_json),
        ):
            restored = auth._load_credentials("telegram-user-legacy")

        self.assertEqual(restored, legacy_record)
        self.assertEqual(
            store.records["telegram-user-legacy"],
            legacy_record,
        )
        self.assertNotIn("telegram-user-legacy", local_tokens)

    def test_expired_access_token_is_refreshed_and_persisted(self):
        class MemoryCredentialStore:
            is_enabled = True
            is_supabase_configured = True

            def __init__(self):
                self.record = {
                    "access_token": "expired-access-token",
                    "refresh_token": "usable-refresh-token",
                    "expires_at": (
                        datetime.now(timezone.utc) - timedelta(minutes=1)
                    ).isoformat(),
                }

            def save(self, _user_id, credentials):
                self.record = dict(credentials)

            def load(self, _user_id):
                return self.record

            def delete(self, _user_id):
                self.record = {}

        store = MemoryCredentialStore()
        auth = AuthService(store)
        response = httpx.Response(
            200,
            json={
                "access_token": "refreshed-access-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
            request=httpx.Request("POST", "https://auth.deriv.com/oauth2/token"),
        )
        with (
            patch(
                "app.services.auth_service.settings",
                type(
                    "SettingsStub",
                    (),
                    {
                        "deriv_app_id": "test-app",
                        "deriv_client_id": "",
                        "deriv_client_secret": "",
                    },
                )(),
            ),
            patch("app.services.auth_service.httpx.post", return_value=response) as post,
        ):
            token = auth.get_valid_access_token("telegram-user-3")

        self.assertEqual(token, "refreshed-access-token")
        self.assertEqual(
            post.call_args.kwargs["data"]["grant_type"],
            "refresh_token",
        )
        self.assertEqual(
            store.record["refresh_token"],
            "usable-refresh-token",
        )
        self.assertGreater(
            datetime.fromisoformat(store.record["expires_at"]),
            datetime.now(timezone.utc),
        )
