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
        for name in ("get_account_capabilities", "list_filters", "get_inbox_rule", "drive_read"):
            self.assertTrue(tools[name].annotations.readOnlyHint, name)
        for name in ("create_filter", "delete_filter", "replace_filter", "update_inbox_rule", "delete_inbox_rule"):
            self.assertFalse(tools[name].annotations.readOnlyHint, name)
            self.assertTrue(tools[name].annotations.destructiveHint, name)
        self.assertTrue(tools["create_filter"].inputSchema["properties"]["dry_run"]["default"])


if __name__ == "__main__":
    unittest.main()
