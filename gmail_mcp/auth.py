"""OAuth consent flow and per-account credential resolution.

`add_account()` runs Google's installed-app (loopback) OAuth flow, reads the
account's email from its Gmail profile, and stores the tokens. `resolve()` takes
an optional account email, falls back to the default, refreshes the access token
if stale, and returns live credentials.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .store import CRED_DIR, AccountStore

log = logging.getLogger("gmail_mcp.auth")

# gmail.modify covers read + labels + threads + drafts (everything the original
# single-account connector did). openid + userinfo.email make the consent return
# the account identity. Add gmail.send here only if you implement a send tool.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

CLIENT_SECRET_PATH = CRED_DIR / "client_secret.json"

_store = AccountStore()


def store() -> AccountStore:
    return _store


def _expiry_to_epoch(expiry: datetime | None) -> int | None:
    """google-auth gives Credentials.expiry as a NAIVE UTC datetime. Tag it UTC
    before converting so the epoch isn't offset by the local timezone (a naive
    .timestamp() would assume local time)."""
    if expiry is None:
        return None
    return int(expiry.replace(tzinfo=timezone.utc).timestamp())


def _epoch_to_expiry(ts: int | None) -> datetime | None:
    """Inverse of _expiry_to_epoch -> NAIVE UTC datetime, which is what
    Credentials.expiry must be (it compares against a naive utcnow internally)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def add_account() -> str:
    """Run OAuth consent (opens a browser) and persist the account. Returns its email."""
    if not CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError(
            f"Missing OAuth client secret at {CLIENT_SECRET_PATH}. Create an OAuth "
            "'Desktop app' client in Google Cloud Console and save the JSON there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    # access_type=offline + prompt=consent => we reliably get a refresh token.
    # authorization_prompt_message="" suppresses the stdout print that would
    # otherwise corrupt the MCP stdio channel.
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        authorization_prompt_message="",
        access_type="offline",
        prompt="consent",
    )
    email = _email_for(creds)
    _persist(email, creds)
    log.info("Connected Gmail account %s", email)
    return email


def resolve(account: str | None) -> tuple[str, Credentials]:
    """Return (email, live credentials) for `account`, or the default if None."""
    email = account or _store.get_default()
    if email is None:
        raise ValueError("No account specified and no default configured. Call add_account first.")
    rec = _store.get(email)
    if rec is None:
        raise ValueError(f"Unknown account {email!r}. Connected: {_store.list_emails()}")

    creds = Credentials(
        token=rec["access_token"],
        refresh_token=rec["refresh_token"],
        token_uri=rec["token_uri"],
        client_id=rec["client_id"],
        client_secret=rec["client_secret"],
        scopes=rec["scopes"],
        expiry=_epoch_to_expiry(rec["expiry"]),
    )
    if not creds.valid:
        if not creds.refresh_token:
            raise ValueError(f"Account {email!r} has no refresh token; re-run add_account.")
        creds.refresh(Request())
        _store.update_tokens(
            email,
            access_token=creds.token,
            expiry=_expiry_to_epoch(creds.expiry),
            refresh_token=creds.refresh_token,
        )
    return email, creds


def _email_for(creds: Credentials) -> str:
    profile = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return profile.users().getProfile(userId="me").execute()["emailAddress"]


def _persist(email: str, creds: Credentials) -> None:
    _store.upsert(
        email,
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        token_uri=creds.token_uri,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        scopes=list(creds.scopes or SCOPES),
        expiry=_expiry_to_epoch(creds.expiry),
    )
