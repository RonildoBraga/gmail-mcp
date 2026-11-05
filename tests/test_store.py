"""Credential-store coverage stays in disposable databases with disposable keys."""

import json
import sqlite3
import stat
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


class KeyPublicationTests(unittest.TestCase):
    def test_concurrent_first_use_shares_one_complete_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            barrier = threading.Barrier(4)
            generate_key = Fernet.generate_key

            def concurrent_key():
                barrier.wait(timeout=3)
                return generate_key()

            with patch.object(store, "CRED_DIR", root), patch.object(store, "KEY_PATH", root / "fernet.key"), patch.object(store, "DB_PATH", root / "accounts.db"):
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
            with patch.object(store, "CRED_DIR", root), patch.object(store, "KEY_PATH", root / "fernet.key"), patch.object(store, "DB_PATH", root / "accounts.db"):
                with patch.object(store.os, "link", side_effect=OSError("disk error")):
                    with self.assertRaisesRegex(OSError, "disk error"):
                        store._load_fernet()
                self.assertEqual(list(root.iterdir()), [])


class CredentialProtectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "credentials"
        self.key_path = self.root / "fernet.key"
        self.enterContext(patch.object(store, "CRED_DIR", self.root))
        self.enterContext(patch.object(store, "DB_PATH", self.root / "accounts.db"))
        self.enterContext(patch.object(store, "KEY_PATH", self.key_path))

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_new_store_is_private_to_the_user(self):
        store.AccountStore()
        self.assertEqual(self.mode(self.root), 0o700)
        self.assertEqual(self.mode(store.DB_PATH), 0o600)
        self.assertEqual(self.mode(self.key_path), 0o600)

    def test_existing_loose_permissions_are_tightened(self):
        store.AccountStore()
        self.root.chmod(0o755)
        store.DB_PATH.chmod(0o644)
        self.key_path.chmod(0o644)
        secret = self.root / "client_secret.json"
        secret.write_text("{}")
        secret.chmod(0o644)
        backups = self.root.parent / "backups"
        backups.mkdir()
        backup = backups / "accounts.db.old"
        backup.write_text("old")
        backup.chmod(0o644)
        store.harden_credentials()
        for path in (store.DB_PATH, self.key_path, secret, backup):
            self.assertEqual(self.mode(path), 0o600, path.name)
        self.assertEqual(self.mode(self.root), 0o700)
        self.assertEqual(self.mode(backups), 0o700)

    def test_tightening_never_widens_access(self):
        self.root.mkdir(mode=0o700)
        narrow = self.root / "narrow"
        narrow.write_text("x")
        narrow.chmod(0o400)
        store.harden_credentials()
        self.assertEqual(self.mode(narrow), 0o400)

    def test_missing_key_never_replaces_keys_for_stored_accounts(self):
        accounts = store.AccountStore()
        accounts.upsert("a@example.com", "google", {"refresh_token": "secret"})
        self.key_path.unlink()
        with self.assertRaises(store.MissingKeyError):
            store._load_fernet()
        self.assertFalse(self.key_path.exists())

    def test_store_without_accounts_can_start_again_after_losing_its_key(self):
        store.AccountStore()
        self.key_path.unlink()
        store.AccountStore()
        self.assertTrue(self.key_path.exists())


class KeychainSelectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.key_path = self.root / "fernet.key"
        self.enterContext(patch.object(store, "CRED_DIR", self.root))
        self.enterContext(patch.object(store, "DB_PATH", self.root / "accounts.db"))
        self.enterContext(patch.object(store, "KEY_PATH", self.key_path))
        self.enterContext(patch.object(store, "_DEFAULT_KEY_PATH", self.key_path))
        self.enterContext(patch.object(store.keystore, "available", return_value=True))
        self.vault = {}

    def fake_keychain(self):
        def add(key):
            return self.vault.setdefault("key", key) == key
        return (patch.object(store.keystore, "read", side_effect=lambda: self.vault.get("key")),
                patch.object(store.keystore, "add", side_effect=add))

    def test_keychain_key_wins_over_key_file(self):
        self.vault["key"] = Fernet.generate_key()
        self.key_path.write_bytes(Fernet.generate_key())
        reader, writer = self.fake_keychain()
        with reader, writer:
            loaded = store._load_fernet()
        self.assertEqual(Fernet(self.vault["key"]).decrypt(loaded.encrypt(b"x")), b"x")

    def test_new_store_creates_its_key_in_the_keychain_only(self):
        reader, writer = self.fake_keychain()
        with reader, writer:
            loaded = store._load_fernet()
        self.assertEqual(Fernet(self.vault["key"]).decrypt(loaded.encrypt(b"x")), b"x")
        self.assertFalse(self.key_path.exists())

    def test_key_file_keeps_working_until_migrated(self):
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        reader, writer = self.fake_keychain()
        with reader, writer:
            loaded = store._load_fernet()
        self.assertEqual(Fernet(key).decrypt(loaded.encrypt(b"x")), b"x")
        self.assertEqual(self.vault, {})

    def test_locked_keychain_falls_back_to_an_existing_key_file(self):
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        with patch.object(store.keystore, "read", side_effect=store.keystore.KeychainError("locked")):
            loaded = store._load_fernet()
        self.assertEqual(Fernet(key).decrypt(loaded.encrypt(b"x")), b"x")

    def test_locked_keychain_without_a_key_file_stops(self):
        with patch.object(store.keystore, "read", side_effect=store.keystore.KeychainError("locked")):
            with self.assertRaises(store.keystore.KeychainError):
                store._load_fernet()

    def test_other_store_locations_never_touch_the_keychain(self):
        with patch.object(store, "KEY_PATH", self.root / "other.key"):
            with patch.object(store.keystore, "read") as reader, patch.object(store.keystore, "add") as writer:
                store._load_fernet()
        reader.assert_not_called()
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
