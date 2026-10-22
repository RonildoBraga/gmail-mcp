"""Per-account auth for both providers.

Google uses the installed-app OAuth loopback flow; Microsoft uses the MSAL
device-code flow (no client secret -- it is a public client). `resolve_provider`
returns (email, provider, auth_data); each provider's credential builder then
refreshes and re-persists as needed.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import msal
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import GOOGLE_SCOPES, MS_AUTHORITY, MS_CLIENT_ID, MS_SCOPES
from .store import CRED_DIR, AccountStore

log = logging.getLogger("gmail_mcp.auth")

GOOGLE_CLIENT_SECRET_PATH = CRED_DIR / "client_secret.json"

_store: AccountStore | None = None
_store_lock = threading.Lock()


def store() -> AccountStore:
    # Importing tools must not create a database/key or touch live credentials.
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = AccountStore()
    return _store


def resolve_provider(account: str | None) -> tuple[str, str, dict]:
    """Return (email, provider, auth_data) for `account`, or the default account."""
    s = store()
    email = account or s.get_default()
    if email is None:
        raise ValueError("No account specified and no default configured. Connect one first.")
    rec = s.get(email)
    if rec is None:
        raise ValueError(f"Unknown account {email!r}. Connected: {s.list_emails()}")
    return rec["email"], rec["provider"], rec["auth_data"]


# ============================================================ Google (OAuth)
def _expiry_to_epoch(expiry: datetime | None) -> int | None:
    return None if expiry is None else int(expiry.replace(tzinfo=timezone.utc).timestamp())


def _epoch_to_expiry(ts: int | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def add_google_account() -> str:
    """Run Google OAuth consent (opens a browser) and persist the account."""
    if not GOOGLE_CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError(
            f"Missing OAuth client secret at {GOOGLE_CLIENT_SECRET_PATH}. Create a Google Cloud "
            "'Desktop app' OAuth client and save the JSON there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CLIENT_SECRET_PATH), GOOGLE_SCOPES)
    creds = flow.run_local_server(
        port=0, open_browser=True, authorization_prompt_message="",
        access_type="offline", prompt="consent",
    )
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    email = svc.users().getProfile(userId="me").execute()["emailAddress"]
    store().upsert(email, "google", _google_auth_data(creds))
    log.info("Connected Google account %s", email)
    return email


def _google_auth_data(creds: Credentials) -> dict:
    # An absent scope list means the existing grant, not the current configured
    # consent scopes. Refreshing legacy credentials must never invent grants.
    scopes = creds.granted_scopes if creds.granted_scopes is not None else creds.scopes
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(scopes or []),
        "expiry": _expiry_to_epoch(creds.expiry),
    }


def google_credentials(email: str, auth: dict) -> Credentials:
    """Build live Google credentials from stored auth_data, refreshing if stale."""
    creds = Credentials(
        token=auth["access_token"],
        refresh_token=auth["refresh_token"],
        token_uri=auth["token_uri"],
        client_id=auth["client_id"],
        client_secret=auth["client_secret"],
        scopes=auth["scopes"],
        expiry=_epoch_to_expiry(auth["expiry"]),
    )
    if not creds.valid:
        if not creds.refresh_token:
            raise ValueError(f"Account {email!r} has no refresh token; re-connect it.")
        creds.refresh(Request())
        store().update_auth(email, _google_auth_data(creds))
    return creds


# ========================================================= Microsoft (MSAL)
def _msal_app(cache: msal.SerializableTokenCache | None = None) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(MS_CLIENT_ID, authority=MS_AUTHORITY, token_cache=cache)


def add_microsoft_account(on_prompt, key_email: str | None = None) -> str:
    """Run the MSAL device-code flow. `on_prompt(message)` shows the user the
    verification URL + code; this call then blocks until they finish signing in.
    `key_email` overrides the stored account key -- use it when the Microsoft
    account's primary alias collides with an existing Google account of the same
    address (store the Microsoft account under its Outlook.com address instead)."""
    cache = msal.SerializableTokenCache()
    app = _msal_app(cache)
    flow = app.initiate_device_flow(scopes=MS_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed to start: {flow.get('error_description', flow)}")
    on_prompt(flow["message"])
    result = app.acquire_token_by_device_flow(flow)  # blocks, polling
    if "access_token" not in result:
        raise RuntimeError(f"Sign-in failed: {result.get('error_description', result)}")
    accounts = app.get_accounts()
    preferred = result.get("id_token_claims", {}).get("preferred_username", "")
    selected = _select_microsoft_account(accounts, key_email or preferred, None)
    email = key_email or selected.get("username") or preferred
    if not email:
        raise RuntimeError("Signed in but could not determine the account email.")
    store().upsert(email, "microsoft", {
        "msal_cache": cache.serialize(), "scopes": list(MS_SCOPES),
        "home_account_id": selected["home_account_id"],
    })
    log.info("Connected Microsoft account %s", email)
    return email


def _select_microsoft_account(accounts: list[dict], email: str, home_account_id: str | None) -> dict:
    """Select a bound identity; preserve legacy aliases only for a sole cache account."""
    if home_account_id:
        matches = [a for a in accounts if a.get("home_account_id") == home_account_id]
    else:
        matches = [a for a in accounts if a.get("username", "").casefold() == email.casefold()]
        if not matches and len(accounts) == 1:
            matches = accounts  # Old key_email aliases did not record a stable ID.
    if len(matches) != 1 or not matches[0].get("home_account_id"):
        raise ValueError(
            f"No unambiguous cached Microsoft identity for {email!r}; re-connect this account. "
            "Refusing to use another cached account."
        )
    return matches[0]


def graph_token(email: str, auth: dict) -> str:
    """Return a live Graph access token, refreshing silently and re-persisting the cache."""
    cache = msal.SerializableTokenCache()
    if auth.get("msal_cache"):
        cache.deserialize(auth["msal_cache"])
    app = _msal_app(cache)
    account = _select_microsoft_account(app.get_accounts(), email, auth.get("home_account_id"))
    scopes = auth.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise ValueError(f"Missing stored Microsoft scopes for {email!r}; re-connect this account.")
    result = app.acquire_token_silent(scopes, account=account)
    if not result or "access_token" not in result:
        raise ValueError(f"Could not silently refresh {email!r}; re-connect it.")
    if cache.has_state_changed or auth.get("home_account_id") != account["home_account_id"]:
        store().update_auth(email, {
            **auth, "msal_cache": cache.serialize(), "home_account_id": account["home_account_id"],
        })
    return result["access_token"]
