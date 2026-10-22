"""Credential-store coverage stays in disposable databases with disposable keys."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from gmail_mcp import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(store, "CRED_DIR", self.root))
        self.enterContext(patch.object(store, "DB_PATH", self.root / "accounts.db"))
        self.enterContext(patch.object(store, "KEY_PATH", self.root / "fernet.key"))
        self.s = store.AccountStore()
        self.addCleanup(self.s._db.close)

    def test_cross_provider_collision_is_symmetric_and_keeps_credentials(self):
        for first, second in (("google", "microsoft"), ("microsoft", "google")):
            with self.subTest(first=first):
                self.s.upsert("same@example.com", first, {"secret": "original"})
                with self.assertRaisesRegex(ValueError, "another provider"):
                    self.s.upsert("same@example.com", second, {"secret": "replacement"})
                self.assertEqual(self.s.get("same@example.com")["provider"], first)
                self.assertEqual(self.s.get("same@example.com")["auth_data"], {"secret": "original"})
                self.s.remove("same@example.com")

    def test_case_variant_provider_collision_is_rejected(self):
        self.s.upsert("Account@example.com", "google", {"secret": "original"})
        with self.assertRaisesRegex(ValueError, "another provider"):
            self.s.upsert("account@EXAMPLE.com", "microsoft", {})
        self.assertEqual(self.s.list_emails(), ["Account@example.com"])

    def test_same_provider_refresh_keeps_selector_and_default(self):
        self.s.upsert("Account@example.com", "google", {"version": 1})
        self.s.upsert("other@example.com", "microsoft", {})
        self.s.upsert("account@example.com", "google", {"version": 2})
        self.assertEqual(self.s.get_default(), "Account@example.com")
        self.assertEqual(len(self.s.list_accounts()), 2)
        self.assertEqual(self.s.get("account@example.com")["auth_data"], {"version": 2})

    def test_different_microsoft_identity_cannot_replace_bound_alias(self):
        self.s.upsert("alias@outlook.com", "microsoft", {"home_account_id": "one", "secret": "old"})
        for identity in ("two", None):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(ValueError, "different Microsoft identity"):
                    self.s.upsert("alias@outlook.com", "microsoft", {"home_account_id": identity})
        self.assertEqual(self.s.get("alias@outlook.com")["auth_data"]["secret"], "old")

    def test_distinct_alias_supports_both_providers_without_schema_rebuild(self):
        self.s.upsert("same@example.com", "google", {})
        self.s.upsert("alias@outlook.com", "microsoft", {"home_account_id": "one"})
        self.s.set_default("ALIAS@outlook.com")
        self.s.update_auth("ALIAS@outlook.com", {"home_account_id": "one", "token": "updated"})
        self.assertEqual(self.s.get_default(), "alias@outlook.com")
        self.assertEqual(self.s.provider_of("ALIAS@outlook.com"), "microsoft")
        self.assertTrue(self.s.remove("ALIAS@outlook.com"))
        self.assertEqual(self.s.get_default(), "same@example.com")

    def test_whole_auth_blob_is_encrypted(self):
        auth_data = {"msal_cache": "private-cache-value", "scopes": ["Mail.Read"], "home_account_id": "private-id"}
        self.s.upsert("alias@outlook.com", "microsoft", auth_data)
        blob = self.s._db.execute("SELECT auth_data FROM accounts").fetchone()[0]
        self.assertNotIn(b"private", blob)
        self.assertEqual(self.s.get("alias@outlook.com")["auth_data"], auth_data)

    def test_legacy_migration_preserves_default_and_refresh_token(self):
        self.s._db.close()
        connection = sqlite3.connect(store.DB_PATH)
        connection.execute("DROP TABLE accounts")
        connection.execute("""CREATE TABLE accounts (
            email TEXT PRIMARY KEY, access_token TEXT, refresh_token BLOB,
            token_uri TEXT, client_id TEXT, client_secret TEXT, scopes TEXT,
            expiry INTEGER, is_default INTEGER)""")
        key = Fernet(store.KEY_PATH.read_bytes())
        connection.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            "legacy@example.com", "old-access", key.encrypt(b"old-refresh"), "https://oauth.example/token",
            "client", "secret", json.dumps(["stored-scope"]), 123, 1,
        ))
        connection.commit()
        connection.close()
        self.s = store.AccountStore()
        self.addCleanup(self.s._db.close)
        self.assertEqual(self.s.get_default(), "legacy@example.com")
        rec = self.s.get("legacy@example.com")
        self.assertEqual(rec["provider"], "google")
        self.assertEqual(rec["auth_data"]["refresh_token"], "old-refresh")
        self.assertEqual(rec["auth_data"]["scopes"], ["stored-scope"])
        self.assertNotIn("token_uri", self.s._columns("accounts"))

    def test_legacy_case_variants_remain_exactly_selectable(self):
        self.s.upsert("Account@example.com", "google", {"version": 1})
        self.s._db.execute("INSERT INTO accounts (email, provider, auth_data, is_default) VALUES (?, ?, ?, 0)", (
            "account@example.com", "google", self.s._fernet.encrypt(b'{"version":2}'),
        ))
        self.s._db.commit()
        self.assertEqual(self.s.get("Account@example.com")["auth_data"]["version"], 1)
        self.assertEqual(self.s.get("account@example.com")["auth_data"]["version"], 2)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.s.get("ACCOUNT@example.com")
        with self.assertRaisesRegex(ValueError, "ambiguous case variants"):
            self.s.upsert("Account@example.com", "google", {})


if __name__ == "__main__":
    unittest.main()
