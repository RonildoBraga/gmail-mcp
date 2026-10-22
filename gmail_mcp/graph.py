"""Microsoft Graph mail helpers (delegated, on a bearer access token).

Kept dependency-free (stdlib urllib). Graph's shape is mapped onto the same
thread/message vocabulary the Gmail side uses, so the MCP tools stay uniform:
a Graph *conversation* plays the role of a Gmail *thread*.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://graph.microsoft.com/v1.0"


class GraphError(RuntimeError):
    """A definitive HTTP response, distinct from an uncertain transport failure."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a mailbox bearer token to a redirect destination.
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _url(path: str) -> str:
    """Only the public Graph HTTPS origin may receive our bearer token."""
    if not isinstance(path, str):
        raise ValueError("Graph path must be a string")
    url = BASE + path if path.startswith("/") and not path.startswith("//") else path
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com"
            or parsed.port not in (None, 443) or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment or "\\" in url or any(ord(c) < 32 for c in url)):
        raise ValueError("Refusing Graph URL outside https://graph.microsoft.com")
    return url.replace(" ", "%20")


def _request(token: str, method: str, path: str, body: dict | None = None) -> dict:
    url = _url(path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    # Ask Graph to return plain-text bodies instead of HTML where possible.
    req.add_header("Prefer", 'outlook.body-content-type="text"')
    try:
        with _OPENER.open(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        guidance = (
            " Existing credentials may lack permission; reconnect explicitly if needed."
            if exc.code in (401, 403) else ""
        )
        raise GraphError(
            f"Graph {method} failed ({exc.code}): {detail}{guidance}", exc.code
        ) from None


def _all_items(token: str, path: str) -> list[dict]:
    """Exhaust a Graph collection; never return a silently truncated result."""
    items, seen = [], set()
    while path:
        url = _url(path)
        if url in seen:
            raise RuntimeError("Graph pagination loop detected")
        seen.add(url)
        response = _request(token, "GET", path)
        items.extend(response.get("value", []))
        path = response.get("@odata.nextLink")
    return items


def _limit(top: int) -> int:
    if isinstance(top, bool) or not isinstance(top, int) or top < 1:
        raise ValueError("max_results must be a positive integer")
    return top


def _bounded_items(token: str, path: str, top: int, page_token: str | None,
                   scope: str) -> tuple[list[dict], str | None]:
    """Follow native nextLinks unchanged, retaining overflow in an opaque cursor.

    Graph may return fewer items than $top, and its next page may then exceed the
    caller's remaining budget. Buffer that overflow instead of dropping it or
    rewriting an opaque nextLink. Cursors are bound to operation/query/account.
    """
    _limit(top)
    pending, seen = [], set()
    if page_token:
        try:
            cursor = json.loads(base64.urlsafe_b64decode(page_token.encode()).decode())
            if (cursor["scope"] != scope or not isinstance(cursor["pending"], list)
                    or not all(isinstance(item, dict) for item in cursor["pending"])
                    or not isinstance(cursor["seen"], list)
                    or not all(isinstance(url, str) for url in cursor["seen"])):
                raise ValueError
            path, pending = cursor["next"], cursor["pending"]
            seen = set(cursor["seen"])
            if path is not None:
                _url(path)
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid continuation token for this account/query") from exc
    items = pending[:top]
    pending = pending[top:]
    while len(items) < top and path:
        url = _url(path)
        if url in seen:
            raise RuntimeError("Graph pagination loop detected")
        seen.add(url)
        response = _request(token, "GET", path)
        values = response.get("value", [])
        count = top - len(items)
        items.extend(values[:count])
        pending = values[count:]
        path = response.get("@odata.nextLink")
        if path:
            if _url(path) in seen:
                raise RuntimeError("Graph pagination loop detected")
    next_token = None
    if pending or path:
        cursor = {"scope": scope, "pending": pending, "next": path, "seen": sorted(seen)}
        next_token = base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode()
    return items, next_token


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


def search_threads(token: str, query: str, top: int, page_token: str | None = None,
                   *, account: str = "") -> dict:
    """Search the mailbox. Uses Graph $search when a query is given, else recent mail."""
    if query and query.strip():
        q = urllib.parse.quote(f'"{query}"')
        path = f"/me/messages?$search={q}&$top={min(_limit(top), 1000)}&$select={_MSG_FIELDS.removesuffix(',body')}"
    else:
        path = f"/me/messages?$top={min(_limit(top), 1000)}&$orderby=receivedDateTime desc&$select={_MSG_FIELDS.removesuffix(',body')}"
    items, next_token = _bounded_items(token, path, top, page_token, f"search:{account}:{query}")
    return {"threads": [_summary(m) for m in items], "next_page_token": next_token}


def get_thread(token: str, conversation_id: str) -> dict:
    """Fetch every message in a conversation, decoded."""
    cid = urllib.parse.quote(conversation_id.replace("'", "''"), safe="")
    # Graph rejects $orderby alongside a conversationId $filter
    # ("InefficientFilter"), so fetch unsorted and sort client-side below.
    path = f"/me/messages?$filter=conversationId eq '{cid}'&$select={_MSG_FIELDS}"
    messages = []
    for m in _all_items(token, path):
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


def list_drafts(token: str, top: int, page_token: str | None = None,
                *, account: str = "") -> dict:
    path = f"/me/mailFolders/drafts/messages?$top={min(_limit(top), 1000)}&$select=id,subject,toRecipients,receivedDateTime"
    items, next_token = _bounded_items(token, path, top, page_token, f"drafts:{account}")
    return {"drafts": items, "next_page_token": next_token}


_WELL_KNOWN_FOLDERS = {
    "archive", "inbox", "junkemail", "deleteditems", "drafts", "sentitems",
}


def _conversation_message_ids(token: str, conversation_id: str) -> list[str]:
    """Every message id in a conversation, across all folders."""
    cid = urllib.parse.quote(conversation_id.replace("'", "''"), safe="")
    path = f"/me/messages?$filter=conversationId eq '{cid}'&$select=id"
    return list(dict.fromkeys(m["id"] for m in _all_items(token, path)))


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
    escaped_name = name.replace("'", "''")
    q = urllib.parse.quote(f"displayName eq '{escaped_name}'", safe="")
    found = _all_items(token, f"/me/mailFolders?$filter={q}")
    if found:
        return found[0]["id"]
    if create:
        return _request(token, "POST", "/me/mailFolders", {"displayName": name})["id"]
    raise RuntimeError(f"Outlook folder {name!r} not found (create=False)")


def move_thread(token: str, conversation_id: str, folder: str, create: bool = True) -> dict:
    """Move every message in a conversation to `folder` (well-known or custom)."""
    ids = _conversation_message_ids(token, conversation_id)
    if not ids:
        return {"thread_id": conversation_id, "moved": 0, "destination": folder}
    dest = resolve_folder(token, folder, create=create)
    moved = []
    for index, mid in enumerate(ids):
        try:
            result = _request(token, "POST", f"/me/messages/{urllib.parse.quote(mid, safe='')}/move",
                              {"destinationId": dest})
        except Exception as exc:
            # A transport/server failure can occur after the move was applied.
            # Stop: retrying automatically could move the same mail twice.
            uncertain = (not isinstance(exc, GraphError)
                         or exc.status == 408 or exc.status >= 500)
            return {"thread_id": conversation_id, "destination": folder,
                    "destination_id": dest, "moved": len(moved), "moved_messages": moved,
                    "failed_message_id": mid, "remaining_message_ids": ids[index + 1:],
                    "outcome_uncertain": uncertain, "error": str(exc), "complete": False}
        moved.append({"previous_id": mid, "id": result.get("id")})
    return {"thread_id": conversation_id, "moved": len(moved), "destination": folder,
            "destination_id": dest, "moved_messages": moved, "complete": True}


def list_folders(token: str) -> list[dict]:
    """Top-level mail folders with their item counts."""
    path = "/me/mailFolders?$top=100&$select=id,displayName,totalItemCount"
    return [
        {"id": f["id"], "name": f.get("displayName"), "count": f.get("totalItemCount")}
        for f in _all_items(token, path)
    ]
