"""SQLite-backed, per-account token store.

The whole point of multi-account is here: one row per Google account, keyed by
email address, instead of a single global token. Refresh tokens are the
long-lived credential, so they're encrypted at rest with Fernet; access tokens
are short-lived and stored in the clear.
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
    """Load (or first-time generate) the symmetric key used to encrypt refresh tokens."""
    CRED_DIR.mkdir(exist_ok=True)
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
    return Fernet(KEY_PATH.read_bytes())


class AccountStore:
    """Keyed token store. Thread-safe enough for a stdio MCP server (single conn)."""

    def __init__(self) -> None:
        CRED_DIR.mkdir(exist_ok=True)
        self._fernet = _load_fernet()
        self._db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                email          TEXT PRIMARY KEY,
                access_token   TEXT,
                refresh_token  BLOB,          -- Fernet-encrypted
                token_uri      TEXT,
                client_id      TEXT,
                client_secret  TEXT,
                scopes         TEXT,          -- JSON array
                expiry         INTEGER,       -- epoch seconds, nullable
                is_default     INTEGER DEFAULT 0
            )
            """
        )
        self._db.commit()

    # ---------------------------------------------------------------- writes
    def upsert(
        self,
        email: str,
        *,
        access_token: str | None,
        refresh_token: str | None,
        token_uri: str | None,
        client_id: str | None,
        client_secret: str | None,
        scopes: list[str],
        expiry: int | None,
    ) -> None:
        existing = self._db.execute(
            "SELECT refresh_token, is_default FROM accounts WHERE email = ?", (email,)
        ).fetchone()

        if refresh_token:
            enc_refresh: bytes | None = self._fernet.encrypt(refresh_token.encode())
        elif existing is not None:
            # Google omits the refresh token on re-consent — keep the one we have.
            enc_refresh = existing["refresh_token"]
        else:
            enc_refresh = None

        # First account ever added becomes the default; otherwise preserve state.
        if existing is not None:
            is_default = existing["is_default"]
        else:
            is_default = 1 if self._count() == 0 else 0

        with self._db:
            self._db.execute(
                """
                INSERT INTO accounts (email, access_token, refresh_token, token_uri,
                                      client_id, client_secret, scopes, expiry, is_default)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    access_token  = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    token_uri     = excluded.token_uri,
                    client_id     = excluded.client_id,
                    client_secret = excluded.client_secret,
                    scopes        = excluded.scopes,
                    expiry        = excluded.expiry
                """,
                (
                    email,
                    access_token,
                    enc_refresh,
                    token_uri,
                    client_id,
                    client_secret,
                    json.dumps(scopes),
                    expiry,
                    is_default,
                ),
            )

    def update_tokens(
        self, email: str, *, access_token: str, expiry: int | None, refresh_token: str | None = None
    ) -> None:
        """Persist a rotated access token (and refresh token, if Google rotated it)."""
        with self._db:
            if refresh_token:
                enc = self._fernet.encrypt(refresh_token.encode())
                self._db.execute(
                    "UPDATE accounts SET access_token=?, expiry=?, refresh_token=? WHERE email=?",
                    (access_token, expiry, enc, email),
                )
            else:
                self._db.execute(
                    "UPDATE accounts SET access_token=?, expiry=? WHERE email=?",
                    (access_token, expiry, email),
                )

    def remove(self, email: str) -> bool:
        with self._db:
            removed = self._db.execute("DELETE FROM accounts WHERE email=?", (email,)).rowcount > 0
        # If we deleted the default, promote whatever's left.
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

    # ---------------------------------------------------------------- reads
    def get(self, email: str) -> dict | None:
        row = self._db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_emails(self) -> list[str]:
        return [r["email"] for r in self._db.execute("SELECT email FROM accounts ORDER BY email")]

    def get_default(self) -> str | None:
        row = self._db.execute("SELECT email FROM accounts WHERE is_default=1 LIMIT 1").fetchone()
        return row["email"] if row else None

    # ---------------------------------------------------------------- internals
    def _count(self) -> int:
        return self._db.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        refresh = self._fernet.decrypt(row["refresh_token"]).decode() if row["refresh_token"] else None
        return {
            "email": row["email"],
            "access_token": row["access_token"],
            "refresh_token": refresh,
            "token_uri": row["token_uri"],
            "client_id": row["client_id"],
            "client_secret": row["client_secret"],
            "scopes": json.loads(row["scopes"]) if row["scopes"] else [],
            "expiry": row["expiry"],
            "is_default": bool(row["is_default"]),
        }
