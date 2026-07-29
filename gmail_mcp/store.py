"""SQLite-backed, per-account credential store (provider-agnostic).

One row per connected account, keyed by email. Each row carries a `provider`
('google' | 'microsoft') and a single Fernet-encrypted `auth_data` JSON blob
holding whatever that provider needs to mint live credentials:

    google    -> {access_token, refresh_token, token_uri, client_id,
                  client_secret, scopes, expiry}
    microsoft -> {msal_cache, scopes}   # msal_cache is MSAL's serialized cache

Encrypting the whole blob (not just the refresh token) keeps the schema tiny and
means no secret material ever sits in the clear. A one-time migration upgrades
the old Google-only schema in place, preserving connected accounts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet

CRED_DIR = Path(__file__).resolve().parent.parent / "credentials"
DB_PATH = CRED_DIR / "accounts.db"
KEY_PATH = CRED_DIR / "fernet.key"


def _load_fernet() -> Fernet:
    """Load (or first-time generate) the symmetric key that encrypts auth_data."""
    CRED_DIR.mkdir(exist_ok=True)
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
    return Fernet(KEY_PATH.read_bytes())


class AccountStore:
    """Keyed credential store. Single connection; fine for a stdio MCP server."""

    def __init__(self) -> None:
        CRED_DIR.mkdir(exist_ok=True)
        self._fernet = _load_fernet()
        self._db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                email      TEXT PRIMARY KEY,
                provider   TEXT NOT NULL DEFAULT 'google',
                auth_data  BLOB,          -- Fernet-encrypted JSON
                is_default INTEGER DEFAULT 0
            )
            """
        )
        self._db.commit()
        self._migrate_legacy()

    # ------------------------------------------------------------- migration
    def _columns(self, table: str) -> list[str]:
        return [r[1] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()]

    def _migrate_legacy(self) -> None:
        """Upgrade the old Google-only schema (token_uri column, plaintext access
        token, separately-encrypted refresh token) to the unified blob schema."""
        cols = self._columns("accounts")
        if "token_uri" not in cols:
            return  # already unified (or fresh)

        legacy = self._db.execute("SELECT * FROM accounts").fetchall()
        migrated: list[tuple[str, str, bytes, int]] = []
        for row in legacy:
            refresh = (
                self._fernet.decrypt(row["refresh_token"]).decode()
                if row["refresh_token"]
                else None
            )
            auth = {
                "access_token": row["access_token"],
                "refresh_token": refresh,
                "token_uri": row["token_uri"],
                "client_id": row["client_id"],
                "client_secret": row["client_secret"],
                "scopes": json.loads(row["scopes"]) if row["scopes"] else [],
                "expiry": row["expiry"],
            }
            blob = self._fernet.encrypt(json.dumps(auth).encode())
            migrated.append((row["email"], "google", blob, row["is_default"]))

        with self._db:
            self._db.execute("ALTER TABLE accounts RENAME TO accounts_legacy")
            self._db.execute(
                """
                CREATE TABLE accounts (
                    email      TEXT PRIMARY KEY,
                    provider   TEXT NOT NULL DEFAULT 'google',
                    auth_data  BLOB,
                    is_default INTEGER DEFAULT 0
                )
                """
            )
            self._db.executemany(
                "INSERT INTO accounts (email, provider, auth_data, is_default) VALUES (?, ?, ?, ?)",
                migrated,
            )
            self._db.execute("DROP TABLE accounts_legacy")

    # ------------------------------------------------------------------ writes
    def upsert(self, email: str, provider: str, auth_data: dict) -> None:
        """Insert or replace an account's credentials. First account added becomes default."""
        blob = self._fernet.encrypt(json.dumps(auth_data).encode())
        existing = self._db.execute(
            "SELECT is_default FROM accounts WHERE email = ?", (email,)
        ).fetchone()
        is_default = existing["is_default"] if existing else (1 if self._count() == 0 else 0)
        with self._db:
            self._db.execute(
                """
                INSERT INTO accounts (email, provider, auth_data, is_default)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET provider=excluded.provider,
                                                 auth_data=excluded.auth_data
                """,
                (email, provider, blob, is_default),
            )

    def update_auth(self, email: str, auth_data: dict) -> None:
        """Persist refreshed credentials (rotated token, updated MSAL cache, ...)."""
        blob = self._fernet.encrypt(json.dumps(auth_data).encode())
        with self._db:
            self._db.execute("UPDATE accounts SET auth_data=? WHERE email=?", (blob, email))

    def remove(self, email: str) -> bool:
        with self._db:
            removed = self._db.execute("DELETE FROM accounts WHERE email=?", (email,)).rowcount > 0
        if removed and self.get_default() is None:
            remaining = self.list_emails()
            if remaining:
                self.set_default(remaining[0])
        return removed

    def set_default(self, email: str) -> None:
        if self._db.execute("SELECT 1 FROM accounts WHERE email=?", (email,)).fetchone() is None:
            raise KeyError(email)
        with self._db:
            self._db.execute("UPDATE accounts SET is_default=0")
            self._db.execute("UPDATE accounts SET is_default=1 WHERE email=?", (email,))

    # ------------------------------------------------------------------- reads
    def get(self, email: str) -> dict | None:
        row = self._db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
        if row is None:
            return None
        return {
            "email": row["email"],
            "provider": row["provider"],
            "auth_data": json.loads(self._fernet.decrypt(row["auth_data"]).decode()),
            "is_default": bool(row["is_default"]),
        }

    def provider_of(self, email: str) -> str | None:
        row = self._db.execute("SELECT provider FROM accounts WHERE email=?", (email,)).fetchone()
        return row["provider"] if row else None

    def list_emails(self) -> list[str]:
        return [r["email"] for r in self._db.execute("SELECT email FROM accounts ORDER BY email")]

    def list_accounts(self) -> list[dict]:
        return [
            {"email": r["email"], "provider": r["provider"]}
            for r in self._db.execute("SELECT email, provider FROM accounts ORDER BY email")
        ]

    def get_default(self) -> str | None:
        row = self._db.execute("SELECT email FROM accounts WHERE is_default=1 LIMIT 1").fetchone()
        return row["email"] if row else None

    # --------------------------------------------------------------- internals
    def _count(self) -> int:
        return self._db.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
