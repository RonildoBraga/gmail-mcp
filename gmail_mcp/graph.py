"""Microsoft Graph mail helpers (delegated, on a bearer access token).

Kept dependency-free (stdlib urllib). Graph's shape is mapped onto the same
thread/message vocabulary the Gmail side uses, so the MCP tools stay uniform:
a Graph *conversation* plays the role of a Gmail *thread*.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import documents, mailtext

BASE = "https://graph.microsoft.com/v1.0"
_PLAIN_BODIES = 'outlook.body-content-type="text"'
_HTML_BODIES = 'outlook.body-content-type="html"'

_SEARCH_TERM = re.compile(r"""
    (?P<negated>-)?
    (?:
        (?P<field>[A-Za-z_]+):(?P<value>"[^"]*"|\([^()]*\)|[^\s()]+)
      | (?P<phrase>"[^"]*")
      | (?P<word>[^\s()"]+)
      | (?P<opening>\()
    )
    | (?P<closing>\))
""", re.VERBOSE)
_GMAIL_ONLY_FIELDS = {"category", "deliveredto", "filename", "in", "label", "larger", "list",
                      "rfc822msgid", "smaller"}
_CONNECTORS = ("OR", "AND", "NOT")
_DROPPED = object()


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


def _send(token: str, method: str, path: str, body: dict | None, prefer: str,
          *, limit: int | None = None) -> bytes:
    url = _url(path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", prefer)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return resp.read() if limit is None else resp.read(limit)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        guidance = (
            " Existing credentials may lack permission; reconnect explicitly if needed."
            if exc.code in (401, 403) else ""
        )
        raise GraphError(
            f"Graph {method} failed ({exc.code}): {detail}{guidance}", exc.code
        ) from None


def _request(token: str, method: str, path: str, body: dict | None = None,
             *, prefer: str = _PLAIN_BODIES) -> dict:
    raw = _send(token, method, path, body, prefer)
    return json.loads(raw) if raw else {}


def _all_items(token: str, path: str, *, prefer: str = _PLAIN_BODIES) -> list[dict]:
    """Exhaust a Graph collection; never return a silently truncated result."""
    items, seen = [], set()
    while path:
        url = _url(path)
        if url in seen:
            raise RuntimeError("Graph pagination loop detected")
        seen.add(url)
        response = _request(token, "GET", path, prefer=prefer)
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
        "snippet": _clean_preview(msg.get("bodyPreview")),
        "message_id": msg.get("id"),
    }


def _clean_preview(preview: str | None) -> str | None:
    return mailtext.clean_text(preview) if preview else preview


_MSG_FIELDS = ("id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,"
               "bodyPreview,hasAttachments,body")


def _search_date(value: str) -> str | None:
    """Gmail dates are Y/M/D or M/D/Y. KQL needs ISO dates."""
    for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _relative_date(value: str) -> str | None:
    match = re.fullmatch(r"(\d+)([dmy])", value.lower())
    if not match:
        return None
    days = int(match[1]) * {"d": 1, "m": 30, "y": 365}[match[2]]
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def _kql_for_field(field: str, value: str) -> str | None:
    """KQL for one Gmail operator. None means Graph has no equivalent."""
    name = field.lower()
    if name in _GMAIL_ONLY_FIELDS:
        return None
    if name in ("after", "before"):
        date = _search_date(value)
        return date and f"received{'>=' if name == 'after' else '<'}{date}"
    if name in ("newer_than", "older_than"):
        date = _relative_date(value)
        return date and f"received{'>=' if name == 'newer_than' else '<'}{date}"
    if name == "has":
        return "hasattachments:true" if value.lower() == "attachment" else None
    if name == "is":
        return {"unread": "isread:false", "read": "isread:true"}.get(value.lower())
    return f"{field}:{value}"


def _without_dropped_terms(tokens: list) -> list[str]:
    """Remove dropped terms along with the operators that applied to them.

    A negation or connector left behind would attach to the next surviving term
    and change what the query means, so it goes with the term it belonged to.
    """
    tokens = list(tokens)
    index = 0
    while index < len(tokens):
        before = tokens[index - 1] if index else None
        after = tokens[index + 1] if index + 1 < len(tokens) else None
        if tokens[index] == ")" and before == "(":
            tokens[index - 1:index + 1] = [_DROPPED]
            index -= 1
        elif tokens[index] is not _DROPPED:
            index += 1
        elif before == "NOT":
            del tokens[index - 1]
            index -= 1
        elif before == "(" and after == ")":
            tokens[index - 1:index + 2] = [_DROPPED]
            index -= 1
        elif before in ("OR", "AND"):
            del tokens[index - 1:index + 1]
            index -= 1
        elif after in ("OR", "AND"):
            del tokens[index:index + 2]
        else:
            del tokens[index]
    return tokens


def _render_kql(tokens: list) -> str:
    """Join tokens into a query, leaving no dangling connector at either end."""
    tokens = _without_dropped_terms(tokens)
    while tokens and tokens[-1] in _CONNECTORS:
        tokens.pop()
    while tokens and tokens[0] in ("OR", "AND"):
        tokens.pop(0)
    text = ""
    for token in tokens:
        if text and token != ")" and not text.endswith("("):
            text += " "
        text += token
    return text


def gmail_query_to_kql(query: str) -> tuple[str, list[str]]:
    """Translate Gmail search syntax for Graph `$search`.

    Graph treats an operator it doesn't know, such as `after:` or `label:`, as a
    term no message can match, so an untranslated query silently finds nothing.
    Terms with no Graph equivalent are returned so callers can report them.
    """
    tokens: list = []
    ignored: list[str] = []
    for match in _SEARCH_TERM.finditer(query):
        if match["closing"]:
            tokens.append(")")
            continue
        negated = bool(match["negated"])
        if match["opening"]:
            tokens.extend(["NOT", "("] if negated else ["("])
            continue
        if match["field"]:
            term = _kql_for_field(match["field"], match["value"])
            if term is None:
                ignored.append(match[0])
                tokens.append(_DROPPED)
                continue
        else:
            term = match["phrase"] or match["word"]
        tokens.extend(["NOT", term] if negated else [term])
    return _render_kql(tokens), ignored


def _inbox_scope(query: str) -> tuple[str, bool]:
    """Take a top-level `in:inbox` out of a query, because Graph scopes it by path.

    Inside brackets, negated or joined by OR it can't become a path, so it stays in
    the query and is reported in `ignored_terms` like other Gmail-only operators.
    """
    matches = list(_SEARCH_TERM.finditer(query))
    spans, depth = [], 0
    for index, match in enumerate(matches):
        depth += 1 if match["opening"] else -1 if match["closing"] else 0
        if (depth or match["negated"] or (match["field"] or "").lower() != "in"
                or match["value"].lower() != "inbox"):
            continue
        neighbours = matches[max(index - 1, 0):index] + matches[index + 1:index + 2]
        if not any(neighbour[0] == "OR" for neighbour in neighbours):
            spans.append(match.span())
    for start, end in reversed(spans):
        query = query[:start] + query[end:]
    return query, bool(spans)


def search_threads(token: str, query: str, top: int, page_token: str | None = None,
                   *, account: str = "") -> dict:
    """Search the mailbox. Uses Graph $search when a query is given, else recent mail.
    `in:inbox` limits either one to the inbox."""
    ignored: list[str] = []
    terms, inbox_only = _inbox_scope(query or "")
    base = "/me/mailFolders/inbox/messages" if inbox_only else "/me/messages"
    if terms.strip():
        kql, ignored = gmail_query_to_kql(terms)
        if not kql:
            return {"threads": [], "next_page_token": None, "ignored_terms": ignored}
        escaped = kql.replace("\\", "\\\\").replace('"', '\\"')
        q = urllib.parse.quote(f'"{escaped}"')
        path = f"{base}?$search={q}&$top={min(_limit(top), 1000)}&$select={_MSG_FIELDS.removesuffix(',body')}"
    else:
        path = f"{base}?$top={min(_limit(top), 1000)}&$orderby=receivedDateTime desc&$select={_MSG_FIELDS.removesuffix(',body')}"
    items, next_token = _bounded_items(token, path, top, page_token, f"search:{account}:{query}")
    result = {"threads": [_summary(m) for m in items], "next_page_token": next_token}
    if ignored:
        result["ignored_terms"] = ignored
    return result


def _attachment_path(message_id: str, attachment_id: str | None = None) -> str:
    path = f"/me/messages/{urllib.parse.quote(message_id, safe='')}/attachments"
    return path if attachment_id is None else f"{path}/{urllib.parse.quote(attachment_id, safe='')}"


def list_attachments(token: str, message_id: str) -> list[dict]:
    """Files a reader would call attachments, leaving out embedded logos."""
    path = f"{_attachment_path(message_id)}?$select=id,name,contentType,size,isInline"
    listed = []
    for item in _all_items(token, path):
        mime = item.get("contentType") or "application/octet-stream"
        size = item.get("size") or 0
        if not documents.is_decorative(mime, size, bool(item.get("isInline"))):
            listed.append({"attachment_id": item.get("id"), "filename": item.get("name"),
                           "mime_type": mime, "size": size})
    return listed


def get_attachment(token: str, message_id: str, attachment_id: str) -> dict:
    path = _attachment_path(message_id, attachment_id)
    meta = _request(token, "GET", f"{path}?$select=id,name,contentType,size")
    documents.check_size(meta.get("size") or 0)
    data = _send(token, "GET", f"{path}/$value", None, _PLAIN_BODIES, limit=documents.MAX_BYTES + 1)
    documents.check_size(len(data))
    return {"filename": meta.get("name") or "attachment",
            "mime_type": meta.get("contentType") or "application/octet-stream", "data": data}


def get_thread(token: str, conversation_id: str, *, body_format: str = "text",
               max_chars: int = mailtext.DEFAULT_MAX_CHARS) -> dict:
    """Fetch every message in a conversation, with clean bodies and attachments."""
    mailtext.check_body_format(body_format)
    cid = urllib.parse.quote(conversation_id.replace("'", "''"), safe="")
    # Graph rejects $orderby alongside a conversationId $filter
    # ("InefficientFilter"), so fetch unsorted and sort client-side below.
    path = f"/me/messages?$filter=conversationId eq '{cid}'&$select={_MSG_FIELDS}"
    prefer = _HTML_BODIES if body_format == "html" else _PLAIN_BODIES
    messages = []
    for m in _all_items(token, path, prefer=prefer):
        content = (m.get("body") or {}).get("content", "")
        text = mailtext.clean_text(content) if body_format == "text" else content
        body, truncated = mailtext.limit(text, max_chars)
        message = {
            "message_id": m.get("id"),
            "from": _addr(m.get("from")),
            "to": _addr_list(m.get("toRecipients")),
            "subject": m.get("subject"),
            "date": m.get("receivedDateTime"),
            "snippet": _clean_preview(m.get("bodyPreview")),
            "body": body,
            "attachments": list_attachments(token, m["id"]) if m.get("hasAttachments") else [],
        }
        if truncated:
            message["body_truncated"] = True
        messages.append(message)
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
_AUTO_DELETING_FOLDERS = {"junkemail", "deleteditems"}
_FOLDER_FIELDS = "$top=100&$select=id,displayName,totalItemCount,childFolderCount"


def _conversation_message_ids(token: str, conversation_id: str) -> list[str]:
    """Every message id in a conversation, across all folders."""
    cid = urllib.parse.quote(conversation_id.replace("'", "''"), safe="")
    path = f"/me/messages?$filter=conversationId eq '{cid}'&$select=id"
    return list(dict.fromkeys(m["id"] for m in _all_items(token, path)))


def _child_folder(token: str, parent_id: str | None, name: str, create: bool) -> str:
    """Find, or optionally create, a folder directly under `parent_id`."""
    base = "/me/mailFolders"
    if parent_id:
        base += f"/{urllib.parse.quote(parent_id, safe='')}/childFolders"
    escaped_name = name.replace("'", "''")
    q = urllib.parse.quote(f"displayName eq '{escaped_name}'", safe="")
    found = _all_items(token, f"{base}?$filter={q}")
    if found:
        return found[0]["id"]
    if create:
        return _request(token, "POST", base, {"displayName": name})["id"]
    raise RuntimeError(f"Outlook folder {name!r} not found (create=False)")


def resolve_folder(token: str, name: str, create: bool = False) -> str:
    """Resolve a destination folder to a Graph destinationId.

    A well-known name (archive, junkemail, deleteditems, inbox, ...) is accepted
    verbatim -- Graph treats those as valid destinationIds. Otherwise each segment
    of a path such as Finance/Receipts is looked up under the one before it,
    optionally creating what's missing.
    """
    key = name.strip().lower().replace(" ", "")
    if key in _WELL_KNOWN_FOLDERS:
        return key
    parts = [part.strip() for part in name.split("/")]
    if not all(parts):
        raise ValueError(f"Invalid folder path {name!r}")
    folder_id = None
    for part in parts:
        folder_id = _child_folder(token, folder_id, part, create)
    return folder_id


def move_thread(token: str, conversation_id: str, folder: str, create: bool = True) -> dict:
    """Move every message in a conversation to `folder` (well-known or custom).

    Outlook purges Junk Email and Deleted Items by itself, so nothing is filed there.
    """
    if folder.strip().lower().replace(" ", "") in _AUTO_DELETING_FOLDERS:
        raise ValueError(
            f"Refusing to move mail to {folder}: Outlook deletes it from there automatically. "
            "Use a custom folder or archive the thread instead."
        )
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


def _folders_under(token: str, path: str, prefix: str = "") -> list[dict]:
    rows = []
    for folder in _all_items(token, path):
        name = f"{prefix}{folder.get('displayName')}"
        rows.append({"id": folder["id"], "name": name, "count": folder.get("totalItemCount")})
        if folder.get("childFolderCount"):
            quoted = urllib.parse.quote(folder["id"], safe="")
            rows += _folders_under(token, f"/me/mailFolders/{quoted}/childFolders?{_FOLDER_FIELDS}",
                                   f"{name}/")
    return rows


def list_folders(token: str) -> list[dict]:
    """Mail folders with their item counts. Child folders are named by path."""
    return _folders_under(token, f"/me/mailFolders?{_FOLDER_FIELDS}")
