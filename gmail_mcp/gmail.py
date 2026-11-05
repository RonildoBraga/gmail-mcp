"""Gmail API response helpers -- pure functions over Gmail API payloads.

Service construction and account routing now live in providers.py / auth.py;
this module is just the mapping logic the GmailProvider reuses.
"""

from __future__ import annotations

import base64
import html
from email.message import EmailMessage

from . import documents, mailtext


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
        "snippet": _clean_snippet(msgs[0].get("snippet")) if msgs else None,
    }


def _clean_snippet(snippet: str | None) -> str | None:
    return mailtext.clean_text(html.unescape(snippet)) if snippet else snippet


def decode_data(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data")
    return decode_data(data).decode("utf-8", errors="replace") if data else ""


def _walk(payload: dict):
    yield payload
    for part in payload.get("parts") or []:
        yield from _walk(part)


def _extract_body(payload: dict, body_format: str = "text") -> str:
    """Return a message's body as clean text, or as its original HTML."""
    plain = html_body = ""
    for part in _walk(payload):
        if part.get("filename"):
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain" and not plain:
            plain = _decode_part(part)
        elif mime == "text/html" and not html_body:
            html_body = _decode_part(part)
    if body_format == "html":
        return html_body or plain
    return mailtext.best_text(plain, html_body, documents.html_text)


def _is_inline(part: dict) -> bool:
    disposition = _header(part.get("headers", []), "Content-Disposition") or ""
    return disposition.lower().startswith("inline")


def _attachment_parts(payload: dict) -> list[dict]:
    parts = []
    for part in _walk(payload):
        body = part.get("body", {})
        if part.get("filename") and (body.get("attachmentId") or body.get("data")):
            parts.append(part)
    return parts


def list_attachments(payload: dict) -> list[dict]:
    """Files a reader would call attachments, leaving out embedded logos."""
    listed = []
    for part in _attachment_parts(payload):
        mime = part.get("mimeType", "application/octet-stream")
        size = part["body"].get("size", 0)
        if not documents.is_decorative(mime, size, _is_inline(part)):
            listed.append({"attachment_id": part.get("partId"), "filename": part["filename"],
                           "mime_type": mime, "size": size})
    return listed


def find_attachment(payload: dict, attachment_id: str) -> dict:
    # Gmail's own attachment ids change between fetches, but part ids don't.
    for part in _attachment_parts(payload):
        if part.get("partId") == attachment_id:
            return part
    raise ValueError(f"No attachment {attachment_id!r} in this message.")


def render_thread(thread: dict, *, body_format: str = "text",
                  max_chars: int = mailtext.DEFAULT_MAX_CHARS) -> dict:
    """Full thread with per-message headers, bodies and attachments."""
    mailtext.check_body_format(body_format)
    messages = []
    for m in thread.get("messages", []):
        headers = m["payload"].get("headers", [])
        body, truncated = mailtext.limit(_extract_body(m["payload"], body_format), max_chars)
        message = {
            "message_id": m["id"],
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "subject": _header(headers, "Subject"),
            "date": _header(headers, "Date"),
            "snippet": _clean_snippet(m.get("snippet")),
            "body": body,
            "label_ids": m.get("labelIds", []),
            "attachments": list_attachments(m["payload"]),
        }
        if truncated:
            message["body_truncated"] = True
        messages.append(message)
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
