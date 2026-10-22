"""Authentication regressions use fake caches/apps, never a real account."""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gmail_mcp import auth


class MicrosoftIdentityTests(unittest.TestCase):
    def setUp(self):
        self.cache = Mock(has_state_changed=False)
        self.cache.serialize.return_value = "updated-cache"
        self.app = Mock()
        self.app.acquire_token_silent.return_value = {"access_token": "test-token"}
        self.store = Mock()
        self.old = {"username": "other@example.com", "home_account_id": "other.tenant"}
        self.intended = {"username": "primary@example.com", "home_account_id": "intended.tenant"}
        self.app.get_accounts.return_value = [self.old, self.intended]
        self.auth_data = {
            "msal_cache": "test-cache", "scopes": ["Mail.Read"],
            "home_account_id": "intended.tenant",
        }
        self.enterContext(patch.object(auth.msal, "SerializableTokenCache", return_value=self.cache))
        self.enterContext(patch.object(auth, "_msal_app", return_value=self.app))
        self.enterContext(patch.object(auth, "_store", self.store))

    def test_bound_id_wins_over_username_or_cache_order(self):
        result = auth.graph_token("other@example.com", self.auth_data)
        self.assertEqual(result, "test-token")
        self.app.acquire_token_silent.assert_called_once_with(["Mail.Read"], account=self.intended)
        self.store.update_auth.assert_not_called()

    def test_missing_bound_account_never_falls_back(self):
        self.app.get_accounts.return_value = [self.old]
        with self.assertRaisesRegex(ValueError, "Refusing to use another"):
            auth.graph_token("primary@example.com", self.auth_data)
        self.app.acquire_token_silent.assert_not_called()
        self.store.update_auth.assert_not_called()

    def test_duplicate_bound_ids_are_ambiguous(self):
        self.app.get_accounts.return_value = [self.intended, dict(self.intended)]
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            auth.graph_token("primary@example.com", self.auth_data)
        self.app.acquire_token_silent.assert_not_called()

    def test_legacy_unique_username_is_bound_and_persisted(self):
        del self.auth_data["home_account_id"]
        self.assertEqual(auth.graph_token("PRIMARY@example.com", self.auth_data), "test-token")
        self.app.acquire_token_silent.assert_called_once_with(["Mail.Read"], account=self.intended)
        self.store.update_auth.assert_called_once_with("PRIMARY@example.com", {
            **self.auth_data, "home_account_id": "intended.tenant", "msal_cache": "updated-cache",
        }, expected_provider="microsoft", expected_auth_data=self.auth_data)
        self.assertIs(self.store.update_auth.call_args.kwargs["expected_auth_data"], self.auth_data)

    def test_legacy_alias_with_sole_cached_account_is_bound(self):
        del self.auth_data["home_account_id"]
        self.app.get_accounts.return_value = [self.intended]
        self.assertEqual(auth.graph_token("alias@outlook.com", self.auth_data), "test-token")
        self.assertEqual(self.store.update_auth.call_args.args[1]["home_account_id"], "intended.tenant")

    def test_legacy_alias_with_multiple_accounts_refuses_first_account(self):
        del self.auth_data["home_account_id"]
        with self.assertRaisesRegex(ValueError, "re-connect"):
            auth.graph_token("alias@outlook.com", self.auth_data)
        self.app.acquire_token_silent.assert_not_called()

    def test_duplicate_legacy_username_is_ambiguous(self):
        del self.auth_data["home_account_id"]
        self.app.get_accounts.return_value = [self.intended, {**self.old, "username": "primary@example.com"}]
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            auth.graph_token("primary@example.com", self.auth_data)
        self.app.acquire_token_silent.assert_not_called()

    def test_no_cache_accounts_or_stable_id_refuses_token(self):
        for accounts in ([], [{"username": "primary@example.com"}]):
            with self.subTest(accounts=accounts):
                self.app.get_accounts.return_value = accounts
                with self.assertRaisesRegex(ValueError, "re-connect"):
                    auth.graph_token("primary@example.com", self.auth_data)
        self.app.acquire_token_silent.assert_not_called()

    def test_missing_scopes_does_not_request_configured_grants(self):
        del self.auth_data["scopes"]
        with self.assertRaisesRegex(ValueError, "Missing stored Microsoft scopes"):
            auth.graph_token("primary@example.com", self.auth_data)
        self.app.acquire_token_silent.assert_not_called()

    def test_failed_silent_refresh_does_not_migrate_or_authenticate(self):
        del self.auth_data["home_account_id"]
        self.app.acquire_token_silent.return_value = {"error": "interaction_required"}
        with self.assertRaisesRegex(ValueError, "re-connect"):
            auth.graph_token("primary@example.com", self.auth_data)
        self.store.update_auth.assert_not_called()
        self.app.initiate_device_flow.assert_not_called()

    def test_refreshed_cache_preserves_scopes_and_identity(self):
        self.cache.has_state_changed = True
        auth.graph_token("primary@example.com", self.auth_data)
        self.store.update_auth.assert_called_once_with("primary@example.com", {
            **self.auth_data, "msal_cache": "updated-cache",
        }, expected_provider="microsoft", expected_auth_data=self.auth_data)

    def test_credential_change_during_refresh_propagates_without_returning_token(self):
        self.cache.has_state_changed = True
        conflict = RuntimeError("Credentials changed during refresh")
        self.store.update_auth.side_effect = conflict
        with self.assertRaises(RuntimeError) as caught:
            auth.graph_token("primary@example.com", self.auth_data)
        self.assertIs(caught.exception, conflict)
        self.assertEqual(self.store.update_auth.call_args.kwargs["expected_provider"], "microsoft")
        self.assertIs(self.store.update_auth.call_args.kwargs["expected_auth_data"], self.auth_data)

    def test_credential_change_during_legacy_binding_propagates(self):
        del self.auth_data["home_account_id"]
        conflict = RuntimeError("Credentials changed during identity binding")
        self.store.update_auth.side_effect = conflict
        with self.assertRaises(RuntimeError) as caught:
            auth.graph_token("primary@example.com", self.auth_data)
        self.assertIs(caught.exception, conflict)
        self.assertIs(self.store.update_auth.call_args.kwargs["expected_auth_data"], self.auth_data)

    def test_connect_records_stable_identity_even_for_alias(self):
        self.app.get_accounts.return_value = [self.intended]
        self.app.initiate_device_flow.return_value = {"user_code": "fake", "message": "mock prompt"}
        self.app.acquire_token_by_device_flow.return_value = {"access_token": "fake-token"}
        self.assertEqual(auth.add_microsoft_account(Mock(), "alias@outlook.com"), "alias@outlook.com")
        self.assertEqual(self.store.upsert.call_args.args[2]["home_account_id"], "intended.tenant")


class LazyStoreTests(unittest.TestCase):
    def test_external_directory_import_selects_all_paths_without_initializing_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "external credentials"
            code = """
import json
from gmail_mcp import auth, store
print(json.dumps({
    "directory": str(store.CRED_DIR), "database": str(store.DB_PATH),
    "key": str(store.KEY_PATH), "client_secret": str(auth.GOOGLE_CLIENT_SECRET_PATH),
    "store_uninitialized": auth._store is None,
}))
"""
            completed = subprocess.run(
                [sys.executable, "-c", code], check=True, text=True, capture_output=True,
                env={**os.environ, "GMAIL_MCP_CREDENTIALS_DIR": str(target)},
            )
            selected = json.loads(completed.stdout)
            self.assertEqual(selected, {
                "directory": str(target), "database": str(target / "accounts.db"),
                "key": str(target / "fernet.key"), "client_secret": str(target / "client_secret.json"),
                "store_uninitialized": True,
            })
            self.assertFalse(target.exists())

    def test_import_does_not_initialize_credential_store(self):
        with patch("gmail_mcp.store.AccountStore") as factory:
            importlib.reload(auth)
            factory.assert_not_called()
            self.assertIsNone(auth._store)
        # Restore the actual factory after the patched reload.
        importlib.reload(auth)

    def test_store_constructed_once_on_first_use(self):
        with patch.object(auth, "_store", None), patch.object(auth, "AccountStore") as factory:
            self.assertIs(auth.store(), factory.return_value)
            self.assertIs(auth.store(), factory.return_value)
            factory.assert_called_once_with()

    def test_resolver_returns_established_email_selector(self):
        fake = Mock()
        fake.get.return_value = {"email": "Stored@example.com", "provider": "microsoft", "auth_data": {}}
        with patch.object(auth, "_store", fake):
            self.assertEqual(auth.resolve_provider("stored@example.com"), ("Stored@example.com", "microsoft", {}))


class GoogleScopeTests(unittest.TestCase):
    def test_refresh_does_not_invent_configured_scopes(self):
        stored = {
            "access_token": "expired", "refresh_token": "existing-refresh",
            "token_uri": "https://oauth.example/token", "client_id": "client",
            "client_secret": "secret", "scopes": [], "expiry": None,
        }
        creds = Mock(
            valid=False, token="refreshed", refresh_token="existing-refresh",
            token_uri=stored["token_uri"], client_id="client", client_secret="secret",
            scopes=None, granted_scopes=None, expiry=None,
        )
        fake_store = Mock()
        with patch.object(auth, "Credentials", return_value=creds) as factory, \
                patch.object(auth, "Request") as request, patch.object(auth, "_store", fake_store):
            self.assertIs(auth.google_credentials("legacy@example.com", stored), creds)
        self.assertEqual(factory.call_args.kwargs["scopes"], [])
        creds.refresh.assert_called_once_with(request.return_value)
        persisted = fake_store.update_auth.call_args.args[1]
        self.assertEqual(persisted["scopes"], [])
        self.assertTrue(set(auth.GOOGLE_SCOPES).isdisjoint(persisted["scopes"]))
        self.assertEqual(fake_store.update_auth.call_args.kwargs["expected_provider"], "google")
        self.assertIs(fake_store.update_auth.call_args.kwargs["expected_auth_data"], stored)

    def test_credential_change_during_google_refresh_propagates(self):
        stored = {
            "access_token": "expired", "refresh_token": "existing-refresh",
            "token_uri": "https://oauth.example/token", "client_id": "client",
            "client_secret": "secret", "scopes": ["stored-scope"], "expiry": None,
        }
        creds = Mock(
            valid=False, token="refreshed", refresh_token="existing-refresh",
            token_uri=stored["token_uri"], client_id="client", client_secret="secret",
            scopes=["stored-scope"], granted_scopes=None, expiry=None,
        )
        conflict = RuntimeError("Credentials changed during refresh")
        fake_store = Mock()
        fake_store.update_auth.side_effect = conflict
        with patch.object(auth, "Credentials", return_value=creds), \
                patch.object(auth, "Request"), patch.object(auth, "_store", fake_store):
            with self.assertRaises(RuntimeError) as caught:
                auth.google_credentials("legacy@example.com", stored)
        self.assertIs(caught.exception, conflict)
        self.assertEqual(fake_store.update_auth.call_args.kwargs["expected_provider"], "google")
        self.assertIs(fake_store.update_auth.call_args.kwargs["expected_auth_data"], stored)

    def test_persistence_uses_explicit_granted_scopes(self):
        creds = Mock(
            token="token", refresh_token="refresh", token_uri="https://oauth.example/token",
            client_id="client", client_secret="secret", scopes=["requested-scope"],
            granted_scopes=["granted-scope"], expiry=None,
        )
        self.assertEqual(auth._google_auth_data(creds)["scopes"], ["granted-scope"])


if __name__ == "__main__":
    unittest.main()
