"""Multi-account, multi-provider mail MCP server.

Every mail tool takes an optional `account` (email); omit it to use the default.
Accounts can be Google (Gmail) or Microsoft (Outlook/Hotmail) -- the tools are
identical either way because they dispatch through providers.provider_for().
`search_all_threads` fans out across every connected account, mixing providers.

Add Google accounts with add_account (opens a browser); add Microsoft accounts
by running `python -m gmail_mcp.connect_microsoft` (device-code login).
Drive tools are Google-only.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import auth, drive, rules
from .providers import provider_for

logging.basicConfig(level=logging.INFO, stream=sys.stderr)

mcp = FastMCP("gmail-multi")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                          idempotentHint=False, openWorldHint=True)


# ============================================================ account management
@mcp.tool(annotations=MUTATING)
def add_account() -> dict:
    """Connect a new Google account via OAuth (opens a browser for consent).

    For Microsoft (Outlook/Hotmail), run `python -m gmail_mcp.connect_microsoft`.
    The first account connected becomes the default.
    """
    email = auth.add_google_account()
    return {"connected": email, "provider": "google", "is_default": auth.store().get_default() == email}


@mcp.tool(annotations=READ_ONLY)
def list_accounts() -> dict:
    """List connected accounts (with provider) and which one is the default."""
    s = auth.store()
    return {"accounts": s.list_accounts(), "default": s.get_default()}


@mcp.tool(annotations=READ_ONLY)
def get_account_capabilities(account: str | None = None) -> dict:
    """Describe supported operations and stored scopes without refreshing tokens.

    Capabilities describe this server's implementation, not a guarantee that
    the existing token or mailbox grants permission. No consent is initiated.
    """
    email, provider, auth_data = auth.resolve_provider(account)
    if provider not in {"google", "microsoft"}:
        raise ValueError(f"Unsupported provider {provider!r} for {email!r}.")
    google = provider == "google"
    return {
        "account": email,
        "provider": provider,
        "capabilities": {
            "threads": True, "drafts": True, "send": True, "folders": True,
            "labels": google, "drive": google,
            "gmail_filters": google, "outlook_inbox_rules": not google,
        },
        "stored_scopes": auth_data.get("scopes", []),
        "settings_permission": (
            "https://www.googleapis.com/auth/gmail.settings.basic" if google
            else "MailboxSettings.ReadWrite"
        ),
        "permission_note": "Existing grants may differ; missing permissions require a separate reconnect decision.",
    }


@mcp.tool(annotations=MUTATING)
def set_default_account(email: str) -> dict:
    """Set the account used when a tool's `account` argument is omitted."""
    auth.store().set_default(email)
    return {"default": email}


@mcp.tool(annotations=MUTATING)
def remove_account(email: str) -> dict:
    """Disconnect an account and delete its stored credentials."""
    s = auth.store()
    removed = s.remove(email)
    return {"removed": removed, "default": s.get_default()}


# ==================================================================== threads
@mcp.tool(annotations=READ_ONLY)
def search_threads(query: str, account: str | None = None, max_results: int = 20,
                   page_token: str | None = None) -> dict:
    """Search threads in one account. Gmail uses Gmail query syntax
    ('is:unread from:a@b.com'); Microsoft treats the query as free-text search."""
    return provider_for(account).search_threads(query, max_results, page_token)


@mcp.tool(annotations=READ_ONLY)
def get_thread(thread_id: str, account: str | None = None) -> dict:
    """Fetch a full thread (all messages, headers, and decoded bodies)."""
    return provider_for(account).get_thread(thread_id)


@mcp.tool(annotations=READ_ONLY)
def search_all_threads(query: str, max_results: int = 10) -> dict:
    """Search EVERY connected account at once (Gmail + Microsoft). Each result is tagged."""
    out, errors, continuations = [], {}, {}
    for email in auth.store().list_emails():
        try:
            res = provider_for(email).search_threads(query, max_results)
            if res.get("next_page_token"):
                continuations[email] = res["next_page_token"]
            for t in res["threads"]:
                t["account"] = email
                out.append(t)
        except Exception as exc:  # one bad account shouldn't sink the sweep
            errors[email] = str(exc)
    result = {"query": query, "threads": out}
    if errors:
        result["errors"] = errors
    if continuations:
        result["next_page_tokens"] = continuations
    return result


# ===================================================================== labels
@mcp.tool(annotations=READ_ONLY)
def list_labels(account: str | None = None) -> dict:
    """List all labels in an account (Gmail only)."""
    return provider_for(account).list_labels()


@mcp.tool(annotations=MUTATING)
def create_label(name: str, account: str | None = None) -> dict:
    """Create a new label (Gmail only)."""
    return provider_for(account).create_label(name)


@mcp.tool(annotations=MUTATING)
def label_thread(thread_id: str, label_ids: list[str], account: str | None = None) -> dict:
    """Add one or more labels to a thread (Gmail only)."""
    return provider_for(account).modify_labels(thread_id, add=label_ids, remove=[])


@mcp.tool(annotations=MUTATING)
def unlabel_thread(thread_id: str, label_ids: list[str], account: str | None = None) -> dict:
    """Remove one or more labels from a thread (Gmail only)."""
    return provider_for(account).modify_labels(thread_id, add=[], remove=label_ids)


# =================================================================== organise
@mcp.tool(annotations=MUTATING)
def archive_thread(thread_id: str, account: str | None = None) -> dict:
    """Take a thread out of the inbox but keep it. Works for both providers:
    Gmail removes the INBOX label (stays in All Mail); Outlook moves it to the
    Archive folder. Nothing is deleted."""
    return provider_for(account).archive_thread(thread_id)


@mcp.tool(annotations=MUTATING)
def move_thread(thread_id: str, folder: str, account: str | None = None) -> dict:
    """File a thread into `folder`, out of the inbox. Works for both providers.
    Outlook: a well-known folder (Archive, Junk Email, Deleted Items) or a custom
    folder, created on demand -- prefer a custom folder (e.g. "Junk", "Finance")
    over the built-in "Junk Email"/"Deleted Items", which auto-purge. Gmail: a
    label of that name (created if missing), and INBOX is removed."""
    return provider_for(account).move_thread(thread_id, folder)


@mcp.tool(annotations=READ_ONLY)
def list_folders(account: str | None = None) -> dict:
    """List folders (Outlook) or labels (Gmail) in an account."""
    return provider_for(account).list_folders()


# ===================================================================== drafts
@mcp.tool(annotations=MUTATING)
def create_draft(to: str, subject: str, body: str, cc: str | None = None,
                 bcc: str | None = None, account: str | None = None) -> dict:
    """Create a draft email in an account."""
    return provider_for(account).create_draft(to, subject, body, cc, bcc)


@mcp.tool(annotations=READ_ONLY)
def list_drafts(account: str | None = None, max_results: int = 20,
                page_token: str | None = None) -> dict:
    """List drafts in an account."""
    return provider_for(account).list_drafts(max_results, page_token)


# =========================================================== provider settings
def _settings_provider(account: str | None, expected_provider: str, dry_run: bool = True):
    # Resolve once, before token acquisition, and always dispatch with that email.
    # Applying a preview must never re-resolve a potentially changed default.
    if not dry_run and not account:
        raise ValueError("Applying settings changes requires the explicit account from the preview.")
    email, provider, _ = auth.resolve_provider(account)
    if provider != expected_provider:
        feature = "Gmail filters" if expected_provider == "google" else "Outlook Inbox rules"
        raise ValueError(f"{feature} are unsupported for {provider} account {email!r}.")
    return provider_for(email)


@mcp.tool(annotations=READ_ONLY)
def list_filters(account: str | None = None) -> dict:
    """List native Gmail filters. Unsupported on Microsoft accounts."""
    return rules.list_filters(_settings_provider(account, "google"))


@mcp.tool(annotations=READ_ONLY)
def get_filter(filter_id: str, account: str | None = None) -> dict:
    """Read a native Gmail filter and its version for a later preview/apply."""
    return rules.get_filter(_settings_provider(account, "google"), filter_id)


@mcp.tool(annotations=MUTATING)
def create_filter(filter: dict, account: str | None = None, dry_run: bool = True) -> dict:
    """Preview/create a native Gmail filter with criteria/action objects.

    Future mail routing may change. Apply with dry_run=false and the explicit
    account from the preview. No automatic permission expansion or retry.
    """
    return rules.create_filter(_settings_provider(account, "google", dry_run), filter, dry_run)


@mcp.tool(annotations=MUTATING)
def delete_filter(filter_id: str, account: str | None = None, dry_run: bool = True,
                  expected_version: str | None = None) -> dict:
    """Preview/delete a Gmail filter permanently. Save the returned recovery body.

    Apply needs explicit account and expected_version from the preview.
    """
    return rules.delete_filter(_settings_provider(account, "google", dry_run), filter_id,
                               dry_run, expected_version)


@mcp.tool(annotations=MUTATING)
def replace_filter(filter_id: str, filter: dict, account: str | None = None,
                   dry_run: bool = True, expected_version: str | None = None) -> dict:
    """Preview/replace a Gmail filter: create new, then delete old (no update API).

    Apply needs explicit account and expected_version. Replacement changes ID.
    Partial or uncertain results require inspection; do not blindly retry.
    """
    return rules.replace_filter(_settings_provider(account, "google", dry_run), filter_id,
                                filter, dry_run, expected_version)


@mcp.tool(annotations=READ_ONLY)
def list_inbox_rules(account: str | None = None) -> dict:
    """List all native Outlook Inbox rules, including read-only ones."""
    return rules.list_inbox_rules(_settings_provider(account, "microsoft"))


@mcp.tool(annotations=READ_ONLY)
def get_inbox_rule(rule_id: str, account: str | None = None) -> dict:
    """Read an Outlook Inbox rule and version. Unsupported on Gmail accounts."""
    return rules.get_inbox_rule(_settings_provider(account, "microsoft"), rule_id)


@mcp.tool(annotations=MUTATING)
def create_inbox_rule(rule: dict, account: str | None = None, dry_run: bool = True) -> dict:
    """Preview/create a native Outlook Inbox rule. Future mail routing may change.

    Supply Graph fields, including displayName, sequence, conditions and actions.
    Apply with explicit account and dry_run=false; save the recovery information.
    """
    return rules.create_inbox_rule(_settings_provider(account, "microsoft", dry_run), rule, dry_run)


@mcp.tool(annotations=MUTATING)
def update_inbox_rule(rule_id: str, patch: dict, account: str | None = None,
                      dry_run: bool = True, expected_version: str | None = None) -> dict:
    """Preview/PATCH only supplied writable fields of an Outlook Inbox rule.

    Omitted fields, including sequence, are preserved. Read-only rules reject
    changes. Apply needs explicit account and expected_version from the preview.
    """
    return rules.update_inbox_rule(_settings_provider(account, "microsoft", dry_run), rule_id,
                                   patch, dry_run, expected_version)


@mcp.tool(annotations=MUTATING)
def delete_inbox_rule(rule_id: str, account: str | None = None, dry_run: bool = True,
                      expected_version: str | None = None) -> dict:
    """Preview/delete an Outlook Inbox rule, retaining its native recovery body.

    Read-only rules reject deletion. Apply needs explicit account and version.
    """
    return rules.delete_inbox_rule(_settings_provider(account, "microsoft", dry_run), rule_id,
                                   dry_run, expected_version)


# ======================================================================= send
@mcp.tool(annotations=MUTATING)
def send_message(to: str, subject: str, body: str, cc: str | None = None,
                 bcc: str | None = None, account: str | None = None) -> dict:
    """Send an email immediately from an account (the From is the account itself)."""
    return provider_for(account).send_message(to, subject, body, cc, bcc)


# ====================================================================== drive
@mcp.tool(annotations=READ_ONLY)
def drive_list(folder_id: str = "root", account: str | None = None, max_results: int = 50,
                page_token: str | None = None) -> dict:
    """List files and folders inside a Drive folder (default: root). Google only."""
    email, svc = drive.service_for(account)
    escaped = folder_id.replace("\\", "\\\\").replace("'", "\\'")
    resp = drive.list_files(
        svc, max_results, page_token, q=f"'{escaped}' in parents and trashed=false",
        orderBy="folder,modifiedTime desc",
    )
    return {"account": email, "folder_id": folder_id, **resp}


@mcp.tool(annotations=READ_ONLY)
def drive_search(query: str, account: str | None = None, max_results: int = 25,
                  page_token: str | None = None) -> dict:
    """Search Drive using Drive query syntax, e.g. "name contains 'tax'". Google only."""
    email, svc = drive.service_for(account)
    return {"account": email, "query": query,
            **drive.list_files(svc, max_results, page_token, q=query)}


@mcp.tool(annotations=READ_ONLY)
def drive_get(file_id: str, account: str | None = None) -> dict:
    """Get a Drive file's full metadata by id. Google only."""
    email, svc = drive.service_for(account)
    return {"account": email, "file": drive.file_meta(svc, file_id)}


@mcp.tool(annotations=READ_ONLY)
def drive_read(file_id: str, account: str | None = None) -> dict:
    """Read a Drive file's text content (Docs/Sheets/Slides exported; binaries return binary=true). Google only."""
    email, svc = drive.service_for(account)
    meta, text = drive.read_content(svc, file_id)
    return {"account": email,
            "file": {"id": meta["id"], "name": meta["name"], "mimeType": meta["mimeType"]},
            "content": text, "binary": text is None}


@mcp.tool(annotations=MUTATING)
def drive_create_folder(name: str, parent_id: str = "root", account: str | None = None) -> dict:
    """Create a new folder in Drive under parent_id (default: root). Google only."""
    email, svc = drive.service_for(account)
    f = svc.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id, name, webViewLink", supportsAllDrives=True,
    ).execute()
    return {"account": email, "created": f}


@mcp.tool(annotations=MUTATING)
def drive_move(file_id: str, new_parent_id: str, account: str | None = None) -> dict:
    """Move a file or folder into a different parent folder. Google only."""
    email, svc = drive.service_for(account)
    cur = svc.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    prev = ",".join(cur.get("parents", []))
    f = svc.files().update(fileId=file_id, addParents=new_parent_id, removeParents=prev,
                           fields="id, name, parents", supportsAllDrives=True).execute()
    return {"account": email, "moved": f}


@mcp.tool(annotations=MUTATING)
def drive_rename(file_id: str, new_name: str, account: str | None = None) -> dict:
    """Rename a Drive file or folder. Google only."""
    email, svc = drive.service_for(account)
    f = svc.files().update(fileId=file_id, body={"name": new_name}, fields="id, name", supportsAllDrives=True).execute()
    return {"account": email, "renamed": f}


@mcp.tool(annotations=MUTATING)
def drive_trash(file_id: str, account: str | None = None) -> dict:
    """Move a Drive file or folder to Trash (reversible). Google only."""
    email, svc = drive.service_for(account)
    f = svc.files().update(fileId=file_id, body={"trashed": True}, fields="id, name, trashed", supportsAllDrives=True).execute()
    return {"account": email, "trashed": f}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
