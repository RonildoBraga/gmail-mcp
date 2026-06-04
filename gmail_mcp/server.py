"""Multi-account Gmail MCP server.

Every Gmail tool takes an optional `account` (email address). Omit it to use the
default account. Manage the account set with add_account / list_accounts /
set_default_account / remove_account. `search_all_threads` fans out across every
connected account at once — the thing a single-account connector can't do.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from . import auth, gmail

# Logs MUST go to stderr — stdout is the MCP stdio protocol channel.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

mcp = FastMCP("gmail-multi")


# ============================================================ account management
@mcp.tool()
def add_account() -> dict:
    """Connect a new Google account via OAuth (opens a browser window for consent).

    The first account connected becomes the default. Returns the account's email.
    """
    email = auth.add_account()
    return {"connected": email, "is_default": auth.store().get_default() == email}


@mcp.tool()
def list_accounts() -> dict:
    """List connected Gmail accounts and which one is the default."""
    s = auth.store()
    return {"accounts": s.list_emails(), "default": s.get_default()}


@mcp.tool()
def set_default_account(email: str) -> dict:
    """Set the account used when a tool's `account` argument is omitted."""
    auth.store().set_default(email)
    return {"default": email}


@mcp.tool()
def remove_account(email: str) -> dict:
    """Disconnect an account and delete its stored tokens."""
    s = auth.store()
    removed = s.remove(email)
    return {"removed": removed, "default": s.get_default()}


# ====================================================================== threads
@mcp.tool()
def search_threads(query: str, account: str | None = None, max_results: int = 20) -> dict:
    """Search threads in one account using Gmail query syntax (e.g. 'is:unread from:a@b.com')."""
    email, svc = gmail.service_for(account)
    resp = svc.users().threads().list(userId="me", q=query, maxResults=max_results).execute()
    threads = [gmail.thread_summary(svc, t["id"]) for t in resp.get("threads", [])]
    return {"account": email, "query": query, "threads": threads}


@mcp.tool()
def get_thread(thread_id: str, account: str | None = None) -> dict:
    """Fetch a full thread (all messages, headers, and decoded bodies)."""
    email, svc = gmail.service_for(account)
    t = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    return {"account": email, "thread": gmail.render_thread(t)}


@mcp.tool()
def search_all_threads(query: str, max_results: int = 10) -> dict:
    """Search EVERY connected account at once. Each result is tagged with its account."""
    out = []
    errors = {}
    for email in auth.store().list_emails():
        try:
            _, svc = gmail.service_for(email)
            resp = svc.users().threads().list(
                userId="me", q=query, maxResults=max_results
            ).execute()
            for t in resp.get("threads", []):
                summary = gmail.thread_summary(svc, t["id"])
                summary["account"] = email
                out.append(summary)
        except Exception as exc:  # one bad account shouldn't sink the whole sweep
            errors[email] = str(exc)
    result = {"query": query, "threads": out}
    if errors:
        result["errors"] = errors
    return result


# ======================================================================= labels
@mcp.tool()
def list_labels(account: str | None = None) -> dict:
    """List all labels in an account."""
    email, svc = gmail.service_for(account)
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    return {"account": email, "labels": [{"id": l["id"], "name": l["name"]} for l in labels]}


@mcp.tool()
def create_label(name: str, account: str | None = None) -> dict:
    """Create a new label."""
    email, svc = gmail.service_for(account)
    label = svc.users().labels().create(userId="me", body={"name": name}).execute()
    return {"account": email, "id": label["id"], "name": label["name"]}


@mcp.tool()
def label_thread(thread_id: str, label_ids: list[str], account: str | None = None) -> dict:
    """Add one or more labels to a thread."""
    email, svc = gmail.service_for(account)
    svc.users().threads().modify(
        userId="me", id=thread_id, body={"addLabelIds": label_ids}
    ).execute()
    return {"account": email, "thread_id": thread_id, "added": label_ids}


@mcp.tool()
def unlabel_thread(thread_id: str, label_ids: list[str], account: str | None = None) -> dict:
    """Remove one or more labels from a thread."""
    email, svc = gmail.service_for(account)
    svc.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": label_ids}
    ).execute()
    return {"account": email, "thread_id": thread_id, "removed": label_ids}


# ======================================================================= drafts
@mcp.tool()
def create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    account: str | None = None,
) -> dict:
    """Create a draft email in an account."""
    email, svc = gmail.service_for(account)
    raw = gmail.build_raw_message(to=to, subject=subject, body=body, cc=cc)
    draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return {"account": email, "draft_id": draft["id"]}


@mcp.tool()
def list_drafts(account: str | None = None, max_results: int = 20) -> dict:
    """List drafts in an account."""
    email, svc = gmail.service_for(account)
    resp = svc.users().drafts().list(userId="me", maxResults=max_results).execute()
    return {"account": email, "drafts": resp.get("drafts", [])}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
