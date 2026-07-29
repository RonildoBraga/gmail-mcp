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

from . import auth, drive
from .providers import provider_for

logging.basicConfig(level=logging.INFO, stream=sys.stderr)

mcp = FastMCP("gmail-multi")


# ============================================================ account management
@mcp.tool()
def add_account() -> dict:
    """Connect a new Google account via OAuth (opens a browser for consent).

    For Microsoft (Outlook/Hotmail), run `python -m gmail_mcp.connect_microsoft`.
    The first account connected becomes the default.
    """
    email = auth.add_google_account()
    return {"connected": email, "provider": "google", "is_default": auth.store().get_default() == email}


@mcp.tool()
def list_accounts() -> dict:
    """List connected accounts (with provider) and which one is the default."""
    s = auth.store()
    return {"accounts": s.list_accounts(), "default": s.get_default()}


@mcp.tool()
def set_default_account(email: str) -> dict:
    """Set the account used when a tool's `account` argument is omitted."""
    auth.store().set_default(email)
    return {"default": email}


@mcp.tool()
def remove_account(email: str) -> dict:
    """Disconnect an account and delete its stored credentials."""
    s = auth.store()
    removed = s.remove(email)
    return {"removed": removed, "default": s.get_default()}


# ==================================================================== threads
@mcp.tool()
def search_threads(query: str, account: str | None = None, max_results: int = 20) -> dict:
    """Search threads in one account. Gmail uses Gmail query syntax
    ('is:unread from:a@b.com'); Microsoft treats the query as free-text search."""
    return provider_for(account).search_threads(query, max_results)


@mcp.tool()
def get_thread(thread_id: str, account: str | None = None) -> dict:
    """Fetch a full thread (all messages, headers, and decoded bodies)."""
    return provider_for(account).get_thread(thread_id)


@mcp.tool()
def search_all_threads(query: str, max_results: int = 10) -> dict:
    """Search EVERY connected account at once (Gmail + Microsoft). Each result is tagged."""
    out, errors = [], {}
    for email in auth.store().list_emails():
        try:
            res = provider_for(email).search_threads(query, max_results)
            for t in res["threads"]:
                t["account"] = email
                out.append(t)
        except Exception as exc:  # one bad account shouldn't sink the sweep
            errors[email] = str(exc)
    result = {"query": query, "threads": out}
    if errors:
        result["errors"] = errors
    return result


# ===================================================================== labels
@mcp.tool()
def list_labels(account: str | None = None) -> dict:
    """List all labels in an account (Gmail only)."""
    return provider_for(account).list_labels()


@mcp.tool()
def create_label(name: str, account: str | None = None) -> dict:
    """Create a new label (Gmail only)."""
    return provider_for(account).create_label(name)


@mcp.tool()
def label_thread(thread_id: str, label_ids: list[str], account: str | None = None) -> dict:
    """Add one or more labels to a thread (Gmail only)."""
    return provider_for(account).modify_labels(thread_id, add=label_ids, remove=[])


@mcp.tool()
def unlabel_thread(thread_id: str, label_ids: list[str], account: str | None = None) -> dict:
    """Remove one or more labels from a thread (Gmail only)."""
    return provider_for(account).modify_labels(thread_id, add=[], remove=label_ids)


# ===================================================================== drafts
@mcp.tool()
def create_draft(to: str, subject: str, body: str, cc: str | None = None,
                 bcc: str | None = None, account: str | None = None) -> dict:
    """Create a draft email in an account."""
    return provider_for(account).create_draft(to, subject, body, cc, bcc)


@mcp.tool()
def list_drafts(account: str | None = None, max_results: int = 20) -> dict:
    """List drafts in an account."""
    return provider_for(account).list_drafts(max_results)


# ======================================================================= send
@mcp.tool()
def send_message(to: str, subject: str, body: str, cc: str | None = None,
                 bcc: str | None = None, account: str | None = None) -> dict:
    """Send an email immediately from an account (the From is the account itself)."""
    return provider_for(account).send_message(to, subject, body, cc, bcc)


# ====================================================================== drive
@mcp.tool()
def drive_list(folder_id: str = "root", account: str | None = None, max_results: int = 50) -> dict:
    """List files and folders inside a Drive folder (default: root). Google only."""
    email, svc = drive.service_for(account)
    resp = svc.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields=f"files({drive.FIELDS})", pageSize=max_results,
        orderBy="folder,modifiedTime desc",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    return {"account": email, "folder_id": folder_id, "files": resp.get("files", [])}


@mcp.tool()
def drive_search(query: str, account: str | None = None, max_results: int = 25) -> dict:
    """Search Drive using Drive query syntax, e.g. "name contains 'tax'". Google only."""
    email, svc = drive.service_for(account)
    resp = svc.files().list(
        q=query, fields=f"files({drive.FIELDS})", pageSize=max_results,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    return {"account": email, "query": query, "files": resp.get("files", [])}


@mcp.tool()
def drive_get(file_id: str, account: str | None = None) -> dict:
    """Get a Drive file's full metadata by id. Google only."""
    email, svc = drive.service_for(account)
    return {"account": email, "file": drive.file_meta(svc, file_id)}


@mcp.tool()
def drive_read(file_id: str, account: str | None = None) -> dict:
    """Read a Drive file's text content (Docs/Sheets/Slides exported; binaries return binary=true). Google only."""
    email, svc = drive.service_for(account)
    meta, text = drive.read_content(svc, file_id)
    return {"account": email,
            "file": {"id": meta["id"], "name": meta["name"], "mimeType": meta["mimeType"]},
            "content": text, "binary": text is None}


@mcp.tool()
def drive_create_folder(name: str, parent_id: str = "root", account: str | None = None) -> dict:
    """Create a new folder in Drive under parent_id (default: root). Google only."""
    email, svc = drive.service_for(account)
    f = svc.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id, name, webViewLink", supportsAllDrives=True,
    ).execute()
    return {"account": email, "created": f}


@mcp.tool()
def drive_move(file_id: str, new_parent_id: str, account: str | None = None) -> dict:
    """Move a file or folder into a different parent folder. Google only."""
    email, svc = drive.service_for(account)
    cur = svc.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    prev = ",".join(cur.get("parents", []))
    f = svc.files().update(fileId=file_id, addParents=new_parent_id, removeParents=prev,
                           fields="id, name, parents", supportsAllDrives=True).execute()
    return {"account": email, "moved": f}


@mcp.tool()
def drive_rename(file_id: str, new_name: str, account: str | None = None) -> dict:
    """Rename a Drive file or folder. Google only."""
    email, svc = drive.service_for(account)
    f = svc.files().update(fileId=file_id, body={"name": new_name}, fields="id, name", supportsAllDrives=True).execute()
    return {"account": email, "renamed": f}


@mcp.tool()
def drive_trash(file_id: str, account: str | None = None) -> dict:
    """Move a Drive file or folder to Trash (reversible). Google only."""
    email, svc = drive.service_for(account)
    f = svc.files().update(fileId=file_id, body={"trashed": True}, fields="id, name, trashed", supportsAllDrives=True).execute()
    return {"account": email, "trashed": f}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
