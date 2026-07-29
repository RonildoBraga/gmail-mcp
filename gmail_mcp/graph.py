"""Microsoft Graph mail helpers (delegated, on a bearer access token).

Kept dependency-free (stdlib urllib). Graph's shape is mapped onto the same
thread/message vocabulary the Gmail side uses, so the MCP tools stay uniform:
a Graph *conversation* plays the role of a Gmail *thread*.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://graph.microsoft.com/v1.0"


def _request(token: str, method: str, path: str, body: dict | None = None) -> dict:
    url = (path if path.startswith("http") else BASE + path).replace(" ", "%20")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    # Ask Graph to return plain-text bodies instead of HTML where possible.
    req.add_header("Prefer", 'outlook.body-content-type="text"')
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Graph {method} {path} failed ({exc.code}): {detail}") from None


def _addr(field: dict | None) -> str | None:
    if not field:
        return None
    ea = field.get("emailAddress", {})
    name, address = ea.get("name"), ea.get("address")
    return f"{name} <{address}>" if name and address else (address or name)


def _addr_list(fields: list[dict] | None) -> str | None:
    parts = [_addr(f) for f in (fields or [])]
    parts = [p for p in parts if p]
    return ", ".join(parts) or None


def _summary(msg: dict) -> dict:
    """Map a Graph message to a thread summary (a conversation stands in for a thread)."""
    return {
        "thread_id": msg.get("conversationId"),
        "subject": msg.get("subject"),
        "from": _addr(msg.get("from")),
        "date": msg.get("receivedDateTime"),
        "message_count": None,  # unknown without fetching the whole conversation
        "snippet": msg.get("bodyPreview"),
        "message_id": msg.get("id"),
    }


_MSG_FIELDS = "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,body"


def search_threads(token: str, query: str, top: int) -> list[dict]:
    """Search the mailbox. Uses Graph $search when a query is given, else recent mail."""
    if query and query.strip():
        q = urllib.parse.quote(f'"{query}"')
        path = f"/me/messages?$search={q}&$top={top}&$select={_MSG_FIELDS}"
    else:
        path = f"/me/messages?$top={top}&$orderby=receivedDateTime desc&$select={_MSG_FIELDS}"
    return [_summary(m) for m in _request(token, "GET", path).get("value", [])]


def get_thread(token: str, conversation_id: str) -> dict:
    """Fetch every message in a conversation, decoded."""
    cid = urllib.parse.quote(conversation_id)
    # Graph rejects $orderby alongside a conversationId $filter
    # ("InefficientFilter"), so fetch unsorted and sort client-side below.
    path = f"/me/messages?$filter=conversationId eq '{cid}'&$select={_MSG_FIELDS}"
    messages = []
    for m in _request(token, "GET", path).get("value", []):
        messages.append(
            {
                "message_id": m.get("id"),
                "from": _addr(m.get("from")),
                "to": _addr_list(m.get("toRecipients")),
                "subject": m.get("subject"),
                "date": m.get("receivedDateTime"),
                "snippet": m.get("bodyPreview"),
                "body": (m.get("body") or {}).get("content", ""),
            }
        )
    messages.sort(key=lambda m: m.get("date") or "")
    return {"thread_id": conversation_id, "messages": messages}


def _message_body(to: str, subject: str, body: str, cc: str | None, bcc: str | None) -> dict:
    def recips(value: str | None) -> list[dict]:
        if not value:
            return []
        return [{"emailAddress": {"address": a.strip()}} for a in value.split(",") if a.strip()]

    return {
        "subject": subject,
        "body": {"contentType": "text", "content": body},
        "toRecipients": recips(to),
        "ccRecipients": recips(cc),
        "bccRecipients": recips(bcc),
    }


def send_message(token: str, to: str, subject: str, body: str, cc=None, bcc=None) -> dict:
    payload = {"message": _message_body(to, subject, body, cc, bcc), "saveToSentItems": True}
    _request(token, "POST", "/me/sendMail", payload)  # 202, empty body
    return {"sent": True, "to": to, "subject": subject}


def create_draft(token: str, to: str, subject: str, body: str, cc=None, bcc=None) -> dict:
    draft = _request(token, "POST", "/me/messages", _message_body(to, subject, body, cc, bcc))
    return {"draft_id": draft.get("id")}


def list_drafts(token: str, top: int) -> list[dict]:
    path = f"/me/mailFolders/drafts/messages?$top={top}&$select=id,subject,toRecipients,receivedDateTime"
    return _request(token, "GET", path).get("value", [])


_WELL_KNOWN_FOLDERS = {
    "archive", "inbox", "junkemail", "deleteditems", "drafts", "sentitems",
}


def _conversation_message_ids(token: str, conversation_id: str) -> list[str]:
    """Every message id in a conversation, across all folders."""
    cid = urllib.parse.quote(conversation_id)
    path = f"/me/messages?$filter=conversationId eq '{cid}'&$select=id"
    return [m["id"] for m in _request(token, "GET", path).get("value", [])]


def resolve_folder(token: str, name: str, create: bool = False) -> str:
    """Resolve a destination folder to a Graph destinationId.

    A well-known name (archive, junkemail, deleteditems, inbox, ...) is accepted
    verbatim -- Graph treats those as valid destinationIds. Otherwise the mailbox
    is searched for a top-level folder with that display name, optionally creating
    it. Custom folders are used (not the built-in "Junk Email"/"Deleted Items")
    when we want mail that won't be auto-purged.
    """
    key = name.strip().lower().replace(" ", "")
    if key in _WELL_KNOWN_FOLDERS:
        return key
    q = urllib.parse.quote(f"displayName eq '{name}'")
    found = _request(token, "GET", f"/me/mailFolders?$filter={q}").get("value", [])
    if found:
        return found[0]["id"]
    if create:
        return _request(token, "POST", "/me/mailFolders", {"displayName": name})["id"]
    raise RuntimeError(f"Outlook folder {name!r} not found (create=False)")


def move_thread(token: str, conversation_id: str, folder: str, create: bool = True) -> dict:
    """Move every message in a conversation to `folder` (well-known or custom)."""
    dest = resolve_folder(token, folder, create=create)
    ids = _conversation_message_ids(token, conversation_id)
    for mid in ids:
        _request(token, "POST", f"/me/messages/{urllib.parse.quote(mid)}/move",
                 {"destinationId": dest})
    return {"thread_id": conversation_id, "moved": len(ids), "destination": folder}


def list_folders(token: str) -> list[dict]:
    """Top-level mail folders with their item counts."""
    path = "/me/mailFolders?$top=100&$select=id,displayName,totalItemCount"
    return [
        {"id": f["id"], "name": f.get("displayName"), "count": f.get("totalItemCount")}
        for f in _request(token, "GET", path).get("value", [])
    ]
