"""Credential-store coverage stays in disposable databases with disposable keys."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        self.enterContext(patch.object(store, "_EXTERNAL_CREDENTIALS", False))
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

    def _assert_concurrent_collision(self, first_provider, first_auth, second_provider,
                                     second_auth, *, same_connection=False):
        other = self.s if same_connection else store.AccountStore()
        if other is not self.s:
            self.addCleanup(other._db.close)
        checked, release, second_started = threading.Event(), threading.Event(), threading.Event()
        encrypt = self.s._fernet.encrypt

        def pause_first(data):
            # Hold the first writer between validation and insertion, exactly
            # where an unprotected second writer used to bypass the guard.
            checked.set()
            if not release.wait(timeout=3):
                raise AssertionError("First writer was never released")
            return encrypt(data)

        def second_write():
            second_started.set()
            try:
                other.upsert("same@example.com", second_provider, second_auth)
            except ValueError:
                return "rejected"
            return "overwrote"

        with ThreadPoolExecutor(max_workers=2) as executor:
            with patch.object(self.s._fernet, "encrypt", side_effect=pause_first):
                first = executor.submit(self.s.upsert, "same@example.com", first_provider, first_auth)
                self.assertTrue(checked.wait(timeout=3))
                second = executor.submit(second_write)
                try:
                    self.assertTrue(second_started.wait(timeout=3))
                    # Give the competing writer a chance to exercise the old
                    # race; the new writer lock keeps it pending until commit.
                    threading.Event().wait(0.05)
                finally:
                    release.set()
                self.assertIsNone(first.result(timeout=3))
                self.assertEqual(second.result(timeout=3), "rejected")
        self.assertEqual(self.s.get("same@example.com")["provider"], first_provider)
        self.assertEqual(self.s.get("same@example.com")["auth_data"], first_auth)
        # A rejected transaction must release its SQLite writer lock.
        other.upsert("unrelated@example.com", "google", {})

    def test_concurrent_stores_cannot_bypass_provider_collision(self):
        self._assert_concurrent_collision("google", {"original": True}, "microsoft",
                                          {"home_account_id": "other"})

    def test_concurrent_stores_cannot_bypass_microsoft_identity_binding(self):
        self._assert_concurrent_collision("microsoft", {"home_account_id": "first"}, "microsoft",
                                          {"home_account_id": "second"})

    def test_shared_connection_serializes_collision_check_and_write(self):
        self._assert_concurrent_collision("google", {"original": True}, "microsoft",
                                          {"home_account_id": "other"}, same_connection=True)

    def test_stale_refresh_cannot_replace_reconnected_provider(self):
        previous = {"home_account_id": "first", "token": "old"}
        self.s.upsert("same@example.com", "microsoft", previous)
        other = store.AccountStore()
        self.addCleanup(other._db.close)
        other.remove("same@example.com")
        other.upsert("same@example.com", "google", {"token": "new-provider"})
        with self.assertRaisesRegex(ValueError, "credentials changed; retry"):
            self.s.update_auth("same@example.com", {**previous, "token": "refreshed"},
                               expected_provider="microsoft", expected_auth_data=previous)
        self.assertEqual(self.s.get("same@example.com")["auth_data"], {"token": "new-provider"})

    def test_stale_refresh_snapshot_is_rejected_and_matching_snapshot_succeeds(self):
        original = {"home_account_id": "first", "token": "original"}
        newer = {**original, "token": "newer"}
        self.s.upsert("same@example.com", "microsoft", original)
        self.s.update_auth("same@example.com", newer, expected_provider="microsoft",
                           expected_auth_data=original)
        with self.assertRaisesRegex(ValueError, "credentials changed; retry"):
            self.s.update_auth("same@example.com", original, expected_provider="microsoft",
                               expected_auth_data=original)
        self.assertEqual(self.s.get("same@example.com")["auth_data"], newer)

    def test_refresh_cannot_change_or_drop_bound_microsoft_identity(self):
        original = {"home_account_id": "first", "token": "original"}
        self.s.upsert("same@example.com", "microsoft", original)
        for replacement in ({"home_account_id": "second"}, {"token": "missing-id"}):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(ValueError, "Microsoft identity changed"):
                    self.s.update_auth("same@example.com", replacement)
                self.assertEqual(self.s.get("same@example.com")["auth_data"], original)

    def test_refresh_of_removed_account_requires_retry(self):
        with self.assertRaisesRegex(ValueError, "credentials changed; retry"):
            self.s.update_auth("removed@example.com", {}, expected_provider="google",
                               expected_auth_data={})
        with self.assertRaises(KeyError):
            self.s.update_auth("removed@example.com", {})

    def test_external_store_reuses_existing_files_without_generating_key(self):
        self.s.upsert("test@example.com", "google", {"test": "temporary-only"})
        original_key = store.KEY_PATH.read_bytes()
        with patch.object(store, "_EXTERNAL_CREDENTIALS", True), \
                patch.object(Fernet, "generate_key", side_effect=AssertionError("must reuse key")):
            other = store.AccountStore()
            self.addCleanup(other._db.close)
            self.assertEqual(other.get("test@example.com")["auth_data"], {"test": "temporary-only"})
            self.assertEqual(store.KEY_PATH.read_bytes(), original_key)

    def test_external_store_missing_directory_or_files_fails_before_opening(self):
        for present in ((), ("directory",), ("directory", "accounts.db"), ("directory", "fernet.key")):
            with self.subTest(present=present):
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder) / "external"
                    if "directory" in present:
                        root.mkdir()
                    for name in ("accounts.db", "fernet.key"):
                        if name in present:
                            (root / name).touch()
                    before = list(root.iterdir()) if root.exists() else None
                    with patch.object(store, "CRED_DIR", root), \
                            patch.object(store, "DB_PATH", root / "accounts.db"), \
                            patch.object(store, "KEY_PATH", root / "fernet.key"), \
                            patch.object(store, "_EXTERNAL_CREDENTIALS", True), \
                            patch.object(store, "_load_fernet") as loader, \
                            patch.object(store.sqlite3, "connect") as connect:
                        with self.assertRaisesRegex(FileNotFoundError, "no credentials were created"):
                            store.AccountStore()
                    loader.assert_not_called()
                    connect.assert_not_called()
                    self.assertEqual(list(root.iterdir()) if root.exists() else None, before)

    def test_external_db_disappearing_after_validation_is_not_recreated(self):
        missing = self.root / "missing.db"
        with patch.object(store, "DB_PATH", missing), patch.object(store, "_EXTERNAL_CREDENTIALS", True), \
                patch.object(store, "_require_existing_credentials"):
            with self.assertRaises(sqlite3.OperationalError):
                store.AccountStore()
        self.assertFalse(missing.exists())


class CredentialDirectoryConfigTests(unittest.TestCase):
    def test_unset_option_keeps_legacy_directory(self):
        with patch.dict(os.environ):
            os.environ.pop("GMAIL_MCP_CREDENTIALS_DIR", None)
            self.assertEqual(store._credential_dir(), Path(store.__file__).resolve().parent.parent / "credentials")

    def test_relative_or_empty_option_is_rejected(self):
        for value in ("", " ", "credentials", "../credentials"):
            with self.subTest(value=value), patch.dict(os.environ, {"GMAIL_MCP_CREDENTIALS_DIR": value}):
                with self.assertRaisesRegex(ValueError, "nonempty absolute"):
                    store._credential_dir()

    def test_absolute_option_accepts_spaces_without_accessing_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "external credentials"
            with patch.dict(os.environ, {"GMAIL_MCP_CREDENTIALS_DIR": str(target)}):
                self.assertEqual(store._credential_dir(), target)
                self.assertFalse(target.exists())

    def test_option_expands_current_user_home(self):
        with patch.dict(os.environ, {"GMAIL_MCP_CREDENTIALS_DIR": "~/mail-mcp-test-credentials"}):
            self.assertEqual(store._credential_dir(), Path("~/mail-mcp-test-credentials").expanduser())


class KeyPublicationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(store, "_EXTERNAL_CREDENTIALS", False))

    def test_concurrent_first_use_shares_one_complete_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            barrier = threading.Barrier(4)
            generate_key = Fernet.generate_key

            def concurrent_key():
                barrier.wait(timeout=3)
                return generate_key()

            with patch.object(store, "CRED_DIR", root), patch.object(store, "KEY_PATH", root / "fernet.key"):
                with patch.object(Fernet, "generate_key", side_effect=concurrent_key):
                    with ThreadPoolExecutor(max_workers=4) as executor:
                        keys = list(executor.map(lambda _: store._load_fernet(), range(4)))
                published = Fernet(store.KEY_PATH.read_bytes())
                for key in keys:
                    self.assertEqual(published.decrypt(key.encrypt(b"test-secret")), b"test-secret")
                self.assertEqual(list(root.iterdir()), [store.KEY_PATH])

    def test_failed_publication_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(store, "CRED_DIR", root), patch.object(store, "KEY_PATH", root / "fernet.key"):
                with patch.object(store.os, "link", side_effect=OSError("disk error")):
                    with self.assertRaisesRegex(OSError, "disk error"):
                        store._load_fernet()
                self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
