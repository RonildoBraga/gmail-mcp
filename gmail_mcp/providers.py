"""Provider abstraction: one uniform mail interface over Gmail and Graph.

`provider_for(account)` looks up the account's provider and returns the matching
implementation. The MCP tools call these methods and never touch a provider SDK
directly, so adding Microsoft didn't fork every tool.
"""

from __future__ import annotations

from googleapiclient.discovery import build

from . import auth, gmail, graph

_MS_NO_LABELS = "Labels aren't supported for Microsoft (Outlook) accounts yet."


class GmailProvider:
    provider = "google"

    def __init__(self, email: str, creds) -> None:
        self.email = email
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def search_threads(self, query: str, max_results: int) -> dict:
        resp = self.svc.users().threads().list(userId="me", q=query, maxResults=max_results).execute()
        threads = [gmail.thread_summary(self.svc, t["id"]) for t in resp.get("threads", [])]
        return {"account": self.email, "query": query, "threads": threads}

    def get_thread(self, thread_id: str) -> dict:
        t = self.svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        return {"account": self.email, "thread": gmail.render_thread(t)}

    def list_labels(self) -> dict:
        labels = self.svc.users().labels().list(userId="me").execute().get("labels", [])
        return {"account": self.email, "labels": [{"id": l["id"], "name": l["name"]} for l in labels]}

    def create_label(self, name: str) -> dict:
        label = self.svc.users().labels().create(userId="me", body={"name": name}).execute()
        return {"account": self.email, "id": label["id"], "name": label["name"]}

    def modify_labels(self, thread_id: str, add: list[str], remove: list[str]) -> dict:
        self.svc.users().threads().modify(
            userId="me", id=thread_id, body={"addLabelIds": add, "removeLabelIds": remove}
        ).execute()
        return {"account": self.email, "thread_id": thread_id, "added": add, "removed": remove}

    def archive_thread(self, thread_id: str) -> dict:
        """Take a thread out of the inbox (remove the INBOX label); it stays in All Mail."""
        return self.modify_labels(thread_id, add=[], remove=["INBOX"])

    def _label_id(self, name: str) -> str:
        for l in self.svc.users().labels().list(userId="me").execute().get("labels", []):
            if l["name"].lower() == name.lower():
                return l["id"]
        return self.svc.users().labels().create(userId="me", body={"name": name}).execute()["id"]

    def move_thread(self, thread_id: str, folder: str) -> dict:
        """File a thread under a label named `folder` and remove it from the inbox
        ("archive" just removes INBOX). Mirrors Outlook's move-to-folder."""
        if folder.strip().lower() in ("archive", ""):
            return self.archive_thread(thread_id)
        return self.modify_labels(thread_id, add=[self._label_id(folder)], remove=["INBOX"])

    def list_folders(self) -> dict:
        return {"account": self.email, "folders": self.list_labels()["labels"]}

    def create_draft(self, to, subject, body, cc, bcc) -> dict:
        raw = gmail.build_raw_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        draft = self.svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return {"account": self.email, "draft_id": draft["id"]}

    def list_drafts(self, max_results: int) -> dict:
        resp = self.svc.users().drafts().list(userId="me", maxResults=max_results).execute()
        return {"account": self.email, "drafts": resp.get("drafts", [])}

    def send_message(self, to, subject, body, cc, bcc) -> dict:
        raw = gmail.build_raw_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        sent = self.svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"account": self.email, "message_id": sent["id"],
                "thread_id": sent.get("threadId"), "to": to, "subject": subject}


class GraphProvider:
    provider = "microsoft"

    def __init__(self, email: str, token: str) -> None:
        self.email = email
        self.token = token

    def search_threads(self, query: str, max_results: int) -> dict:
        threads = graph.search_threads(self.token, query, max_results)
        return {"account": self.email, "query": query, "threads": threads}

    def get_thread(self, thread_id: str) -> dict:
        return {"account": self.email, "thread": graph.get_thread(self.token, thread_id)}

    def create_draft(self, to, subject, body, cc, bcc) -> dict:
        return {"account": self.email, **graph.create_draft(self.token, to, subject, body, cc, bcc)}

    def list_drafts(self, max_results: int) -> dict:
        return {"account": self.email, "drafts": graph.list_drafts(self.token, max_results)}

    def send_message(self, to, subject, body, cc, bcc) -> dict:
        return {"account": self.email, **graph.send_message(self.token, to, subject, body, cc, bcc)}

    def list_labels(self) -> dict:
        raise ValueError(_MS_NO_LABELS)

    def create_label(self, name: str) -> dict:
        raise ValueError(_MS_NO_LABELS)

    def modify_labels(self, thread_id: str, add: list[str], remove: list[str]) -> dict:
        raise ValueError(_MS_NO_LABELS)

    def archive_thread(self, thread_id: str) -> dict:
        """Move a conversation to the Outlook Archive folder (keeps it, out of inbox)."""
        return {"account": self.email, **graph.move_thread(self.token, thread_id, "archive")}

    def move_thread(self, thread_id: str, folder: str) -> dict:
        """Move a conversation into `folder` (well-known name or a custom folder, created on demand)."""
        return {"account": self.email, **graph.move_thread(self.token, thread_id, folder)}

    def list_folders(self) -> dict:
        return {"account": self.email, "folders": graph.list_folders(self.token)}


def provider_for(account: str | None):
    """Return the provider implementation for `account` (or the default account)."""
    email, provider, auth_data = auth.resolve_provider(account)
    if provider == "google":
        return GmailProvider(email, auth.google_credentials(email, auth_data))
    if provider == "microsoft":
        return GraphProvider(email, auth.graph_token(email, auth_data))
    raise ValueError(f"Unknown provider {provider!r} for {email!r}")
