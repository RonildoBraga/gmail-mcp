"""Google Drive helpers. Drive is Google-only; service_for guards against
non-Google accounts so the Drive tools fail with a clear message on Microsoft.
"""

from __future__ import annotations

import io

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from . import auth

FIELDS = "id, name, mimeType, size, modifiedTime, parents, webViewLink, trashed"

_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def service_for(account: str | None):
    """Return (email, Drive v3 service) for a Google account (or the default)."""
    email, provider, auth_data = auth.resolve_provider(account)
    if provider != "google":
        raise ValueError(f"Drive is only available for Google accounts ({email!r} is {provider}).")
    creds = auth.google_credentials(email, auth_data)
    return email, build("drive", "v3", credentials=creds, cache_discovery=False)


def file_meta(svc, file_id: str) -> dict:
    return svc.files().get(fileId=file_id, fields=FIELDS, supportsAllDrives=True).execute()


def list_files(svc, max_results: int, page_token: str | None = None, **params) -> dict:
    """Fill a bounded result page and expose continuation instead of truncating."""
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ValueError("max_results must be a positive integer.")
    files, seen = [], set()
    token = page_token
    while len(files) < max_results:
        if token in seen:
            raise RuntimeError("Drive pagination loop detected.")
        seen.add(token)
        resp = svc.files().list(
            **params, fields=f"nextPageToken,files({FIELDS})",
            pageSize=min(1000, max_results - len(files)),
            **({"pageToken": token} if token else {}),
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return {"files": files, "next_page_token": token}


def read_content(svc, file_id: str, max_chars: int = 200_000) -> tuple[dict, str | None]:
    """Return (metadata, text). text is None for binary files (images, PDFs, etc.)."""
    meta = file_meta(svc, file_id)
    mime = meta.get("mimeType", "")
    if mime in _EXPORT:
        data = svc.files().export(fileId=file_id, mimeType=_EXPORT[mime]).execute()
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        return meta, text[:max_chars]
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id, supportsAllDrives=True))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return meta, buf.getvalue().decode("utf-8", "replace")[:max_chars]
    return meta, None
