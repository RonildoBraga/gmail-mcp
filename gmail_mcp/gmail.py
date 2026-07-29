"""Gmail API response helpers -- pure functions over Gmail API payloads.

Service construction and account routing now live in providers.py / auth.py;
this module is just the mapping logic the GmailProvider reuses.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage


def _header(headers: list[dict], name: str) -> str | None:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def thread_summary(svc, thread_id: str) -> dict:
    """Lightweight thread overview: subject/from/date + message count + snippet."""
    t = svc.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    msgs = t.get("messages", [])
    first_headers = msgs[0]["payload"]["headers"] if msgs else []
    return {
        "thread_id": thread_id,
        "subject": _header(first_headers, "Subject"),
        "from": _header(first_headers, "From"),
        "date": _header(first_headers, "Date"),
        "message_count": len(msgs),
        "snippet": msgs[0].get("snippet") if msgs else None,
    }


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data")
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""


def _extract_body(payload: dict) -> str:
    """Walk a MIME tree and return the best text body (prefers text/plain)."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload)
    if mime.startswith("multipart/"):
        parts = payload.get("parts", [])
        plain = next((p for p in parts if p.get("mimeType") == "text/plain"), None)
        if plain:
            return _decode_part(plain)
        for p in parts:
            body = _extract_body(p)
            if body:
                return body
    if mime == "text/html":
        return _decode_part(payload)
    return ""


def render_thread(thread: dict) -> dict:
    """Full thread with per-message headers and decoded bodies."""
    messages = []
    for m in thread.get("messages", []):
        headers = m["payload"].get("headers", [])
        messages.append({
            "message_id": m["id"],
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "subject": _header(headers, "Subject"),
            "date": _header(headers, "Date"),
            "snippet": m.get("snippet"),
            "body": _extract_body(m["payload"]),
            "label_ids": m.get("labelIds", []),
        })
    return {"thread_id": thread.get("id"), "messages": messages}


def build_raw_message(*, to: str, subject: str, body: str,
                      cc: str | None = None, bcc: str | None = None) -> str:
    """Compose an RFC-822 message and base64url-encode it for the Gmail API."""
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()
