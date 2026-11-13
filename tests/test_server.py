import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gmail_mcp import auth, server


class ServerTests(unittest.TestCase):
    def test_settings_apply_needs_explicit_account_before_credentials(self):
        with patch.object(auth, "resolve_provider") as resolve:
            with self.assertRaisesRegex(ValueError, "explicit account"):
                server.create_filter({"criteria": {}, "action": {}}, dry_run=False)
        resolve.assert_not_called()

    def test_unsupported_settings_do_not_acquire_credentials(self):
        with patch.object(auth, "resolve_provider", return_value=("outlook@example.com", "microsoft", {})), \
             patch.object(server, "provider_for") as provider:
            with self.assertRaisesRegex(ValueError, "unsupported.*microsoft"):
                server.list_filters("outlook@example.com")
        provider.assert_not_called()

    def test_preview_pins_resolved_default(self):
        p = SimpleNamespace(email="first@example.com", provider="google")
        with patch.object(auth, "resolve_provider", return_value=(p.email, p.provider, {})), \
             patch.object(server, "provider_for", return_value=p) as provider, \
             patch.object(server.rules, "create_filter", return_value={"dry_run": True}) as create:
            self.assertEqual(server.create_filter({"criteria": {}, "action": {}}), {"dry_run": True})
        provider.assert_called_once_with("first@example.com")
        create.assert_called_once_with(p, {"criteria": {}, "action": {}}, True)

    def test_capabilities_are_available_without_refresh(self):
        with patch.object(auth, "resolve_provider", return_value=("a@example.com", "microsoft", {"scopes": ["Mail.Read"]})), \
             patch.object(server, "provider_for") as provider:
            result = server.get_account_capabilities("a@example.com")
        provider.assert_not_called()
        self.assertTrue(result["capabilities"]["outlook_inbox_rules"])
        self.assertFalse(result["capabilities"]["gmail_filters"])
        self.assertFalse(result["capabilities"]["drive"])
        self.assertFalse(result["capabilities"]["calendar"])
        self.assertEqual(result["stored_scopes"], ["Mail.Read"])

    def test_cross_account_search_reports_errors_and_continuations(self):
        store = Mock()
        store.list_emails.return_value = ["gmail@example.com", "outlook@example.com"]
        good = Mock()
        good.search_threads.return_value = {"threads": [{"thread_id": "t"}], "next_page_token": "next"}
        with patch.object(auth, "store", return_value=store), \
             patch.object(server, "provider_for", side_effect=[good, ValueError("expired")]):
            result = server.search_all_threads("query", max_results=3)
        self.assertEqual(result["threads"][0]["account"], "gmail@example.com")
        self.assertEqual(result["errors"], {"outlook@example.com": "expired"})
        self.assertEqual(result["next_page_tokens"], {"gmail@example.com": "next"})

    def test_read_and_destructive_tool_annotations(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        for name in ("get_account_capabilities", "list_filters", "get_inbox_rule", "drive_read",
                     "calendar_list_calendars", "calendar_list_events", "calendar_get_event"):
            self.assertTrue(tools[name].annotations.readOnlyHint, name)
        for name in ("create_filter", "delete_filter", "replace_filter", "update_inbox_rule", "delete_inbox_rule",
                     "calendar_create_event", "calendar_update_event", "calendar_delete_event"):
            self.assertFalse(tools[name].annotations.readOnlyHint, name)
            self.assertTrue(tools[name].annotations.destructiveHint, name)
        self.assertTrue(tools["create_filter"].inputSchema["properties"]["dry_run"]["default"])


class AttachmentToolTests(unittest.TestCase):
    def provider(self):
        provider = Mock(email="a@example.com")
        provider.get_attachment.return_value = {
            "filename": "bill.pdf", "mime_type": "application/pdf", "data": b"pdf-bytes"}
        return provider

    def test_read_returns_text_with_its_source(self):
        provider = self.provider()
        with patch.object(server, "provider_for", return_value=provider), \
             patch.object(server.documents, "extract_text", return_value={"text": "Total 5", "pages": 1}):
            result = server.read_attachment("m1", "2", "a@example.com")
        self.assertEqual(result, {"account": "a@example.com", "filename": "bill.pdf",
                                  "mime_type": "application/pdf", "size": 9, "text": "Total 5",
                                  "pages": 1, "truncated": False})

    def test_read_limits_long_text(self):
        provider = self.provider()
        with patch.object(server, "provider_for", return_value=provider), \
             patch.object(server.documents, "extract_text", return_value={"text": "abcdef"}):
            result = server.read_attachment("m1", "2", max_chars=3)
        self.assertEqual((result["text"], result["truncated"]), ("abc", True))

    def test_read_passes_on_a_note_when_there_is_no_text(self):
        provider = self.provider()
        with patch.object(server, "provider_for", return_value=provider), \
             patch.object(server.documents, "extract_text", return_value={"text": None, "note": "no"}):
            result = server.read_attachment("m1", "2")
        self.assertIsNone(result["text"])
        self.assertEqual(result["note"], "no")

    def test_save_reports_where_the_file_went(self):
        provider = self.provider()
        with patch.object(server, "provider_for", return_value=provider), \
             patch.object(server.documents, "save", return_value="/tmp/bill.pdf") as save:
            result = server.save_attachment("m1", "2")
        save.assert_called_once_with(b"pdf-bytes", "bill.pdf")
        self.assertEqual(result, {"account": "a@example.com", "path": "/tmp/bill.pdf", "size": 9})

    def test_get_thread_forwards_reading_options(self):
        provider = self.provider()
        with patch.object(server, "provider_for", return_value=provider):
            server.get_thread("t1", "a@example.com", "html", 50)
        provider.get_thread.assert_called_once_with("t1", "html", 50)

    def test_drive_read_reports_notes_and_pages(self):
        read = {"meta": {"id": "1", "name": "a.pdf", "mimeType": "application/pdf"}, "text": "x",
                "pages": 2, "truncated": False}
        with patch.object(server.drive, "service_for", return_value=("a@example.com", Mock())), \
             patch.object(server.drive, "read_content", return_value=read):
            result = server.drive_read("1")
        self.assertEqual((result["content"], result["binary"], result["pages"]), ("x", False, 2))

    def test_drive_read_marks_unreadable_files_as_binary(self):
        read = {"meta": {"id": "1", "name": "a.png", "mimeType": "image/png"}, "text": None, "note": "no"}
        with patch.object(server.drive, "service_for", return_value=("a@example.com", Mock())), \
             patch.object(server.drive, "read_content", return_value=read):
            result = server.drive_read("1")
        self.assertEqual((result["content"], result["binary"], result["note"]), (None, True, "no"))


if __name__ == "__main__":
    unittest.main()
