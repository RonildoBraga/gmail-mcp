"""macOS Keychain storage for the credential store's Fernet key.

Keeping the key out of the credentials folder means a copy of that folder is not
enough to read the stored tokens. Access goes through `/usr/bin/security`, so the
Keychain item keeps trusting one stable, signed binary across Python upgrades.

The key is handed to `security` on stdin, never on its command line, because
process arguments are visible to every user on the machine. Nothing here prints
or logs the key.

    python -m gmail_mcp.keystore status
    python -m gmail_mcp.keystore migrate [--remove-file]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SECURITY = "/usr/bin/security"
SERVICE = "gmail-mcp"
ACCOUNT = "fernet-key"
_ITEM_NOT_FOUND = 44
_ITEM_ALREADY_EXISTS = 45
_TIMEOUT_SECONDS = 30


class KeychainError(RuntimeError):
    """The Keychain could not be used, or disagrees with the key file."""


def available() -> bool:
    return sys.platform == "darwin" and Path(SECURITY).exists()


def read() -> bytes | None:
    """Return the stored key, or None when no Keychain item exists."""
    result = subprocess.run(
        [SECURITY, "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
        capture_output=True, timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == _ITEM_NOT_FOUND:
        return None
    raise KeychainError(f"Reading the Keychain failed (status {result.returncode}).")


def add(key: bytes) -> bool:
    """Store the key once. False means another process stored one first."""
    text = key.decode("ascii")
    if not re.fullmatch(r"[A-Za-z0-9_=-]+", text):
        raise ValueError("Fernet keys are URL-safe base64")
    command = f'add-generic-password -s {SERVICE} -a {ACCOUNT} -l "gmail-mcp Fernet key" -w {text}\n'
    result = subprocess.run(
        [SECURITY, "-i"], input=command.encode("ascii"),
        capture_output=True, timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True
    if result.returncode == _ITEM_ALREADY_EXISTS:
        return False
    raise KeychainError(f"Writing to the Keychain failed (status {result.returncode}).")


def _stored_blobs(db_path: Path) -> list[bytes]:
    if not db_path.exists():
        return []
    db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        return [row[0] for row in db.execute("SELECT auth_data FROM accounts")]
    finally:
        db.close()


def _verify(key: bytes, blobs: list[bytes]) -> None:
    fernet = Fernet(key)
    try:
        for blob in blobs:
            fernet.decrypt(blob)
    except InvalidToken:
        raise KeychainError("This key does not decrypt the stored accounts.") from None


def status() -> dict:
    from . import store

    in_keychain = read() is not None if available() else False
    return {"keychain_available": available(), "key_in_keychain": in_keychain,
            "key_file_present": store.KEY_PATH.exists()}


def migrate(*, remove_file: bool = False) -> dict:
    """Copy the key file into the Keychain, verified, and optionally delete the file."""
    from . import store

    if not available():
        raise KeychainError("The macOS Keychain is not available on this machine.")
    stored = read()
    blobs = _stored_blobs(store.DB_PATH)
    if not store.KEY_PATH.exists():
        if stored is None:
            raise KeychainError("No key file and no Keychain item were found.")
        _verify(stored, blobs)
        return {"status": "already_migrated", "accounts_verified": len(blobs), "file_removed": False}
    key = store.KEY_PATH.read_bytes().strip()
    _verify(key, blobs)
    if stored is None:
        add(key)
        stored = read()
    if stored != key:
        raise KeychainError("The Keychain holds a different key; the key file was left in place.")
    if remove_file:
        store.KEY_PATH.unlink()
    return {"status": "migrated", "accounts_verified": len(blobs), "file_removed": remove_file}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    move = commands.add_parser("migrate")
    move.add_argument("--remove-file", action="store_true",
                      help="delete fernet.key after the Keychain copy is verified")
    args = parser.parse_args()
    result = status() if args.command == "status" else migrate(remove_file=args.remove_file)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
