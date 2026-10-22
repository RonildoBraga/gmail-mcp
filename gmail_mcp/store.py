"""SQLite-backed, per-account credential store (provider-agnostic).

One row per connected account, keyed by email. Each row carries a `provider`
('google' | 'microsoft') and a single Fernet-encrypted `auth_data` JSON blob
holding whatever that provider needs to mint live credentials:

    google    -> {access_token, refresh_token, token_uri, client_id,
                  client_secret, scopes, expiry}
    microsoft -> {msal_cache, scopes, home_account_id}

Encrypting the whole blob (not just the refresh token) keeps the schema tiny and
means no secret material ever sits in the clear. A one-time migration upgrades
the old Google-only schema in place, preserving connected accounts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from functools import wraps
from pathlib import Path

from cryptography.fernet import Fernet

CRED_DIR = Path(__file__).resolve().parent.parent / "credentials"
DB_PATH = CRED_DIR / "accounts.db"
KEY_PATH = CRED_DIR / "fernet.key"


def _load_fernet() -> Fernet:
    """Load (or first-time generate) the symmetric key that encrypts auth_data."""
    CRED_DIR.mkdir(exist_ok=True)
    if not KEY_PATH.exists():
        # Publish only a fully written key, and never replace another process's
        # key. Creating KEY_PATH directly exposes a partially written file.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=CRED_DIR, prefix=".fernet-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, KEY_PATH)
            except FileExistsError:
                pass  # Another process already published the shared key.
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return Fernet(KEY_PATH.read_bytes())


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


def _atomic(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            if self._db.in_transaction:
                return method(self, *args, **kwargs)
            # Lock before reading collision/default/identity state. SQLite's
            # writer lock also serializes separate MCP/CLI processes.
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                return method(self, *args, **kwargs)
    return call


class AccountStore:
    """Keyed credential store. Single connection; fine for a stdio MCP server."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
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
    @_synchronized
    def _columns(self, table: str) -> list[str]:
        return [r[1] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()]

    @_atomic
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
    @_atomic
    def upsert(self, email: str, provider: str, auth_data: dict) -> None:
        """Refresh a matching account; never overwrite a different provider/identity.

        Email selectors and the on-disk schema stay compatible. A Microsoft
        alias may be used as a distinct key when providers share an address.
        """
        if provider not in {"google", "microsoft"} or not email.strip():
            raise ValueError("An email selector and a supported provider are required.")
        matches = self._db.execute(
            "SELECT * FROM accounts WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchall()
        if any(row["provider"] != provider for row in matches):
            raise ValueError(
                f"Account {email!r} already belongs to another provider; refusing to overwrite it. "
                "Use a distinct Microsoft alias/account selector."
            )
        if len(matches) > 1:
            raise ValueError(f"Account {email!r} has ambiguous case variants; use distinct selectors.")
        existing = matches[0] if matches else None
        if existing:
            email = existing["email"]  # Keep the established selector and default.
            previous = json.loads(self._fernet.decrypt(existing["auth_data"]).decode())
            identity = previous.get("home_account_id")
            if provider == "microsoft" and identity and auth_data.get("home_account_id") != identity:
                raise ValueError(
                    f"Account {email!r} is bound to a different Microsoft identity; "
                    "refusing to replace its credentials. Use a distinct account selector."
                )
        blob = self._fernet.encrypt(json.dumps(auth_data).encode())
        is_default = existing["is_default"] if existing else (1 if self._count() == 0 else 0)
        self._db.execute(
            """
            INSERT INTO accounts (email, provider, auth_data, is_default)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET provider=excluded.provider,
                                             auth_data=excluded.auth_data
            """,
            (email, provider, blob, is_default),
        )

    @_atomic
    def update_auth(self, email: str, auth_data: dict, *, expected_provider: str | None = None,
                    expected_auth_data: dict | None = None) -> None:
        """Persist a refresh only if its original account snapshot still matches."""
        row = self._account_row(email)
        if row is None:
            if expected_provider is not None or expected_auth_data is not None:
                raise ValueError("Account credentials changed; retry with the current account.")
            raise KeyError(email)
        previous = json.loads(self._fernet.decrypt(row["auth_data"]).decode())
        if ((expected_provider is not None and row["provider"] != expected_provider)
                or (expected_auth_data is not None and previous != expected_auth_data)):
            raise ValueError("Account credentials changed; retry with the current account.")
        identity = previous.get("home_account_id")
        if row["provider"] == "microsoft" and identity and auth_data.get("home_account_id") != identity:
            raise ValueError("Account Microsoft identity changed; refusing to overwrite its credentials.")
        email = row["email"]
        blob = self._fernet.encrypt(json.dumps(auth_data).encode())
        self._db.execute("UPDATE accounts SET auth_data=? WHERE email=?", (blob, email))

    @_atomic
    def remove(self, email: str) -> bool:
        row = self._account_row(email)
        if row is None:
            return False
        email = row["email"]
        removed = self._db.execute("DELETE FROM accounts WHERE email=?", (email,)).rowcount > 0
        if removed and self.get_default() is None:
            remaining = self.list_emails()
            if remaining:
                self.set_default(remaining[0])
        return removed

    @_atomic
    def set_default(self, email: str) -> None:
        row = self._account_row(email)
        if row is None:
            raise KeyError(email)
        email = row["email"]
        self._db.execute("UPDATE accounts SET is_default=0")
        self._db.execute("UPDATE accounts SET is_default=1 WHERE email=?", (email,))

    # ------------------------------------------------------------------- reads
    @_synchronized
    def get(self, email: str) -> dict | None:
        row = self._account_row(email)
        if row is None:
            return None
        return {
            "email": row["email"],
            "provider": row["provider"],
            "auth_data": json.loads(self._fernet.decrypt(row["auth_data"]).decode()),
            "is_default": bool(row["is_default"]),
        }

    @_synchronized
    def provider_of(self, email: str) -> str | None:
        row = self._account_row(email)
        return row["provider"] if row else None

    @_synchronized
    def list_emails(self) -> list[str]:
        return [r["email"] for r in self._db.execute("SELECT email FROM accounts ORDER BY email")]

    @_synchronized
    def list_accounts(self) -> list[dict]:
        return [
            {"email": r["email"], "provider": r["provider"]}
            for r in self._db.execute("SELECT email, provider FROM accounts ORDER BY email")
        ]

    @_synchronized
    def get_default(self) -> str | None:
        row = self._db.execute("SELECT email FROM accounts WHERE is_default=1 LIMIT 1").fetchone()
        return row["email"] if row else None

    # --------------------------------------------------------------- internals
    @_synchronized
    def _account_row(self, email: str) -> sqlite3.Row | None:
        # Exact selectors preserve legacy rows; case-insensitive fallback is
        # accepted only if unique, so it can never silently switch accounts.
        row = self._db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
        if row is not None:
            return row
        rows = self._db.execute(
            "SELECT * FROM accounts WHERE email=? COLLATE NOCASE", (email,)
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Ambiguous account selector {email!r}; use its exact stored case.")
        return rows[0] if rows else None

    @_synchronized
    def _count(self) -> int:
        return self._db.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
