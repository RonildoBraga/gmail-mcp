import contextlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from gmail_mcp import keystore, store


def completed(returncode=0, stdout=b""):
    return subprocess.CompletedProcess([], returncode, stdout, b"")


class SecurityCommandTests(unittest.TestCase):
    def test_read_returns_the_key_without_its_newline(self):
        with patch.object(keystore.subprocess, "run", return_value=completed(stdout=b"abc=\n")):
            self.assertEqual(keystore.read(), b"abc=")

    def test_read_reports_a_missing_item_as_none(self):
        with patch.object(keystore.subprocess, "run", return_value=completed(44)):
            self.assertIsNone(keystore.read())

    def test_read_raises_for_other_failures(self):
        with patch.object(keystore.subprocess, "run", return_value=completed(51)):
            with self.assertRaises(keystore.KeychainError):
                keystore.read()

    def test_add_sends_the_key_on_stdin_and_never_in_process_arguments(self):
        key = Fernet.generate_key()
        with patch.object(keystore.subprocess, "run", return_value=completed()) as run:
            self.assertTrue(keystore.add(key))
        self.assertEqual(run.call_args.args[0], [keystore.SECURITY, "-i"])
        self.assertIn(key, run.call_args.kwargs["input"])

    def test_add_reports_an_existing_item(self):
        with patch.object(keystore.subprocess, "run", return_value=completed(45)):
            self.assertFalse(keystore.add(Fernet.generate_key()))

    def test_add_rejects_text_that_could_change_the_command(self):
        with patch.object(keystore.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                keystore.add(b"abc; delete-keychain login.keychain")
        run.assert_not_called()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.key_path = self.root / "fernet.key"
        self.db_path = self.root / "accounts.db"
        self.enterContext(patch.object(store, "KEY_PATH", self.key_path))
        self.enterContext(patch.object(store, "DB_PATH", self.db_path))
        self.enterContext(patch.object(keystore, "available", return_value=True))
        self.key = Fernet.generate_key()
        self.key_path.write_bytes(self.key + b"\n")
        db = sqlite3.connect(self.db_path)
        db.execute("CREATE TABLE accounts (auth_data BLOB)")
        db.execute("INSERT INTO accounts VALUES (?)", (Fernet(self.key).encrypt(b"{}"),))
        db.commit()
        db.close()
        self.vault = {}
        self.enterContext(patch.object(keystore, "read", side_effect=lambda: self.vault.get("key")))
        self.enterContext(patch.object(keystore, "add", side_effect=self.add))

    def add(self, key):
        self.vault["key"] = key
        return True

    def test_migrate_copies_the_key_and_keeps_the_file_by_default(self):
        result = keystore.migrate()
        self.assertEqual(self.vault["key"], self.key)
        self.assertTrue(self.key_path.exists())
        self.assertEqual(result, {"status": "migrated", "accounts_verified": 1, "file_removed": False})

    def test_migrate_can_remove_the_file_once_verified(self):
        result = keystore.migrate(remove_file=True)
        self.assertFalse(self.key_path.exists())
        self.assertTrue(result["file_removed"])

    def test_wrong_key_is_never_stored_or_removed(self):
        self.key_path.write_bytes(Fernet.generate_key())
        with self.assertRaisesRegex(keystore.KeychainError, "does not decrypt"):
            keystore.migrate(remove_file=True)
        self.assertEqual(self.vault, {})
        self.assertTrue(self.key_path.exists())

    def test_conflicting_keychain_key_keeps_the_file(self):
        self.vault["key"] = Fernet.generate_key()
        with self.assertRaisesRegex(keystore.KeychainError, "different key"):
            keystore.migrate(remove_file=True)
        self.assertTrue(self.key_path.exists())

    def test_failed_keychain_write_keeps_the_file(self):
        with patch.object(keystore, "add", return_value=True):
            with self.assertRaisesRegex(keystore.KeychainError, "different key"):
                keystore.migrate(remove_file=True)
        self.assertTrue(self.key_path.exists())

    def test_rerun_after_removal_reports_already_migrated(self):
        keystore.migrate(remove_file=True)
        self.assertEqual(keystore.migrate()["status"], "already_migrated")

    def test_output_never_contains_the_key(self):
        output = io.StringIO()
        with patch("sys.argv", ["keystore", "migrate"]), contextlib.redirect_stdout(output):
            keystore.main()
        self.assertEqual(json.loads(output.getvalue())["status"], "migrated")
        self.assertNotIn(self.key.decode(), output.getvalue())

    def test_status_reports_where_the_key_is(self):
        self.assertEqual(keystore.status(), {"keychain_available": True, "key_in_keychain": False,
                                             "key_file_present": True})


if __name__ == "__main__":
    unittest.main()
