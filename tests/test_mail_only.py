import asyncio
import base64
import importlib
import json
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError

from gmail_mcp import auth, graph, mail_only, server

GMAIL = "allowed@example.com"
OUTLOOK = "allowed@outlook.com"


def call(scoped, name, arguments):
    response = asyncio.run(scoped.call_tool(name, arguments))
    return json.loads(response[0].text)


def cursor(name="search_threads", account=OUTLOOK, query="q", next_url=None):
    payload = {
        "scope": f"search:{account}:{query}" if name == "search_threads" else f"drafts:{account}",
        "pending": [], "next": next_url, "seen": [],
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


class MailOnlyDiscoveryTests(unittest.TestCase):
    def test_empty_allowlist_advertises_and_executes_no_tools(self):
        with patch.object(auth, "store") as store, patch.object(auth, "resolve_provider") as resolve:
            scoped = mail_only.create_server()
            self.assertEqual(asyncio.run(scoped.list_tools()), [])
            with self.assertRaisesRegex(ToolError, "Unknown tool"):
                call(scoped, "search_threads", {"account": GMAIL, "query": "q"})
        store.assert_not_called()
        resolve.assert_not_called()

    def test_discovery_enforces_modes_without_changing_full_server(self):
        before = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
        read_only = mail_only.create_server([GMAIL])
        organize = mail_only.create_server([GMAIL], "organize")
        readonly_tools = {tool.name: tool for tool in asyncio.run(read_only.list_tools())}
        organizing_tools = {tool.name: tool for tool in asyncio.run(organize.list_tools())}
        self.assertEqual(set(readonly_tools), mail_only.READ_TOOLS)
        self.assertEqual(set(organizing_tools), mail_only.READ_TOOLS | mail_only.ORGANIZE_TOOLS)
        for name, tool in organizing_tools.items():
            self.assertIn("account", tool.inputSchema["required"], name)
            self.assertEqual(tool.inputSchema["properties"]["account"]["type"], "string", name)
            self.assertEqual(tool.annotations.readOnlyHint, name in mail_only.READ_TOOLS, name)
            self.assertIn("default account is never used", tool.description, name)
        self.assertIn("archive and inbox only", organizing_tools["move_thread"].description)
        self.assertIn("forwarding actions are blocked", organizing_tools["create_filter"].description)
        self.assertIn("move/copy", organizing_tools["update_inbox_rule"].description)
        self.assertEqual({tool.name for tool in asyncio.run(server.mcp.list_tools())}, before)
        self.assertIn("send_message", before)
        self.assertIn("drive_read", before)
        self.assertEqual({tool.name for tool in asyncio.run(read_only.list_tools())}, mail_only.READ_TOOLS)

    def test_disallowed_invocation_is_rejected_not_merely_hidden(self):
        for mode in mail_only.MODES:
            scoped = mail_only.create_server([GMAIL], mode)
            for name in ("send_message", "create_draft", "drive_read", "add_account", "list_accounts",
                         "remove_account", "set_default_account", "search_all_threads", "delete_message"):
                with self.subTest(mode=mode, tool=name), self.assertRaisesRegex(ToolError, "Unknown tool"):
                    call(scoped, name, {"account": GMAIL})
        scoped = mail_only.create_server([GMAIL])
        for name in mail_only.ORGANIZE_TOOLS:
            with self.subTest(tool=name), self.assertRaisesRegex(ToolError, "Unknown tool"):
                call(scoped, name, {"account": GMAIL})

    def test_invalid_modes_and_wildcards_fail_closed(self):
        for accounts in (["*"], ["*@example.com"], [" allowed@example.com"], ["missing-at"], GMAIL):
            with self.subTest(accounts=accounts), self.assertRaises(ValueError):
                mail_only.create_server(accounts)
        with self.assertRaises(ValueError):
            mail_only.create_server([GMAIL], "full")

    def test_import_and_creation_never_initialize_credentials(self):
        with patch.object(auth, "AccountStore") as factory, patch.object(auth, "store") as store:
            importlib.reload(mail_only)
            mail_only.create_server([GMAIL, OUTLOOK])
        factory.assert_not_called()
        store.assert_not_called()


class MailOnlyAccountTests(unittest.TestCase):
    arguments = {
        "get_account_capabilities": {}, "search_threads": {"query": "q"},
        "get_thread": {"thread_id": "t"}, "list_labels": {}, "list_folders": {}, "list_drafts": {},
        "list_filters": {}, "get_filter": {"filter_id": "f"}, "list_inbox_rules": {},
        "get_inbox_rule": {"rule_id": "r"}, "create_label": {"name": "custom"},
        "label_thread": {"thread_id": "t", "label_ids": ["Label_1"]},
        "unlabel_thread": {"thread_id": "t", "label_ids": ["Label_1"]},
        "archive_thread": {"thread_id": "t"}, "move_thread": {"thread_id": "t", "folder": "archive"},
        "create_filter": {"filter": {"criteria": {}, "action": {"addLabelIds": ["Label_1"]}}},
        "replace_filter": {"filter_id": "f", "filter": {"criteria": {}, "action": {"removeLabelIds": ["INBOX"]}}},
        "delete_filter": {"filter_id": "f"}, "create_inbox_rule": {
            "rule": {"displayName": "read", "sequence": 1, "actions": {"markAsRead": True}}},
        "update_inbox_rule": {"rule_id": "r", "patch": {"actions": {"markAsRead": True}}},
        "delete_inbox_rule": {"rule_id": "r"},
    }

    def test_every_tool_requires_allowed_exact_account_before_any_lookup(self):
        scoped = mail_only.create_server([GMAIL, OUTLOOK], "organize")
        with patch.object(auth, "store") as store, patch.object(auth, "resolve_provider") as resolve, \
                patch.object(server, "provider_for") as provider:
            for name, arguments in self.arguments.items():
                for account in (None, "unapproved@example.com", GMAIL.upper(), " " + GMAIL):
                    with self.subTest(tool=name, account=account), self.assertRaises(ToolError):
                        call(scoped, name, {**arguments, "account": account})
                with self.subTest(tool=name, account="omitted"), self.assertRaises(ToolError):
                    call(scoped, name, arguments)
        store.assert_not_called()
        resolve.assert_not_called()
        provider.assert_not_called()

    def test_allowed_account_reuses_existing_provider_and_bounded_cursor_arguments(self):
        scoped = mail_only.create_server([GMAIL])
        provider = Mock()
        provider.search_threads.return_value = {"account": GMAIL, "threads": [], "next_page_token": "more"}
        with patch.object(server, "provider_for", return_value=provider) as factory:
            result = call(scoped, "search_threads", {"account": GMAIL, "query": "q", "max_results": 3,
                                                     "page_token": "native-gmail-token"})
        factory.assert_called_once_with(GMAIL)
        provider.search_threads.assert_called_once_with("q", 3, "native-gmail-token")
        self.assertEqual(result["next_page_token"], "more")

    def test_capabilities_describe_profile_without_claiming_send_or_drive(self):
        scoped = mail_only.create_server([OUTLOOK])
        with patch.object(auth, "resolve_provider", return_value=(OUTLOOK, "microsoft", {"scopes": ["Mail.Send"]})), \
                patch.object(server, "provider_for") as provider:
            result = call(scoped, "get_account_capabilities", {"account": OUTLOOK})
        provider.assert_not_called()
        self.assertFalse(result["capabilities"]["send"])
        self.assertFalse(result["capabilities"]["drive"])
        self.assertFalse(result["capabilities"]["settings_mutations"])
        self.assertEqual(result["mode"], "read-only")
        self.assertEqual(result["allowed_tools"], sorted(mail_only.READ_TOOLS))
        self.assertEqual(result["stored_scopes"], ["Mail.Send"])

    def test_missing_approved_account_does_not_disclose_other_accounts_or_use_default(self):
        scoped = mail_only.create_server([GMAIL], "organize")
        diagnostic = f"Unknown account {GMAIL!r}. Connected: ['private-unapproved@example.com']"
        with patch.object(auth, "resolve_provider", side_effect=ValueError(diagnostic)) as resolve, \
                patch.object(auth, "store") as store:
            for name, arguments in (
                ("get_account_capabilities", {}), ("get_thread", {"thread_id": "t"}),
                ("move_thread", {"thread_id": "t", "folder": "archive"}),
            ):
                with self.subTest(tool=name), self.assertRaises(ToolError) as caught:
                    call(scoped, name, {"account": GMAIL, **arguments})
                self.assertNotIn("private-unapproved@example.com", str(caught.exception))
                self.assertNotIn("Connected:", str(caught.exception))
                self.assertIn("approved account is not connected", str(caught.exception))
            self.assertEqual(resolve.call_count, 3)
            self.assertTrue(all(item.args == (GMAIL,) for item in resolve.call_args_list))
        store.assert_not_called()


class MailOnlySettingsTests(unittest.TestCase):
    def setUp(self):
        self.scoped = mail_only.create_server([GMAIL, OUTLOOK], "organize")

    def test_gmail_forwarding_unknown_actions_and_protected_labels_blocked_before_credentials(self):
        actions = [{"forward": "someone@example.com"}, {"unknownFutureAction": True},
                   {"addLabelIds": ["TRASH"]}, {"removeLabelIds": ["spam"]}]
        with patch.object(auth, "resolve_provider") as resolve, patch.object(server, "provider_for") as provider:
            for action in actions:
                for name in ("create_filter", "replace_filter"):
                    arguments = {"account": GMAIL, "filter": {"criteria": {}, "action": action}}
                    if name == "replace_filter":
                        arguments["filter_id"] = "f"
                    with self.subTest(action=action, tool=name), self.assertRaises(ToolError):
                        call(self.scoped, name, arguments)
        resolve.assert_not_called()
        provider.assert_not_called()

    def test_graph_outbound_delete_folder_and_unknown_actions_are_rejected(self):
        for action in ("forwardTo", "forwardAsAttachmentTo", "redirectTo", "permanentDelete", "delete",
                       "moveToFolder", "copyToFolder", "unknownFutureAction"):
            with patch.object(auth, "resolve_provider") as resolve, patch.object(server, "provider_for") as provider:
                for name in ("create_inbox_rule", "update_inbox_rule"):
                    body = {"actions": {action: True}}
                    arguments = {"account": OUTLOOK, "rule": body} if name == "create_inbox_rule" else {
                        "account": OUTLOOK, "rule_id": "r", "patch": body}
                    with self.subTest(action=action, tool=name), self.assertRaises(ToolError):
                        call(self.scoped, name, arguments)
                resolve.assert_not_called()
                provider.assert_not_called()

    def test_settings_preview_defaults_and_definition_deletions_are_preserved(self):
        for name, arguments in (
            ("create_filter", {"filter": {"criteria": {}, "action": {"addLabelIds": ["Label_1"]}}}),
            ("delete_filter", {"filter_id": "f"}), ("delete_inbox_rule", {"rule_id": "r"}),
        ):
            account = OUTLOOK if name.endswith("inbox_rule") else GMAIL
            with patch.object(server, name, return_value={"dry_run": True}) as operation:
                self.assertEqual(call(self.scoped, name, {"account": account, **arguments}), {"dry_run": True})
            self.assertTrue(operation.call_args.kwargs["dry_run"])
            self.assertEqual(operation.call_args.kwargs["account"], account)

    def test_active_unsafe_existing_rule_cannot_be_enabled_or_broadened(self):
        current = {"rule": {"actions": {"forwardTo": [{"emailAddress": {"address": "elsewhere@example.com"}}]},
                             "isEnabled": True}, "expected_version": "safe-read-version"}
        with patch.object(server, "get_inbox_rule", return_value=current), \
                patch.object(server, "update_inbox_rule") as update:
            for rule_patch in ({"isEnabled": True}, {"conditions": {"subjectContains": ["anything"]}}):
                with self.subTest(patch=rule_patch), self.assertRaises(ToolError):
                    call(self.scoped, "update_inbox_rule", {"account": OUTLOOK, "rule_id": "r", "patch": rule_patch})
        update.assert_not_called()

    def test_unsafe_existing_rule_can_be_disabled_or_replaced_with_safe_actions(self):
        with patch.object(server, "get_inbox_rule") as get, \
                patch.object(server, "update_inbox_rule", return_value={"dry_run": True}) as update:
            for rule_patch in ({"isEnabled": False}, {"actions": {"markAsRead": True}}):
                call(self.scoped, "update_inbox_rule", {"account": OUTLOOK, "rule_id": "r", "patch": rule_patch})
        get.assert_not_called()
        self.assertEqual(update.call_count, 2)

    def test_rule_guard_snapshot_is_bound_to_applied_version(self):
        current = {"rule": {"actions": {"markAsRead": True}}, "expected_version": "guard-snapshot"}
        with patch.object(server, "get_inbox_rule", return_value=current), \
                patch.object(server, "update_inbox_rule", return_value={"status": "updated"}) as update:
            with self.assertRaisesRegex(ToolError, "preview it again"):
                call(self.scoped, "update_inbox_rule", {"account": OUTLOOK, "rule_id": "r", "patch": {"isEnabled": True},
                                                       "dry_run": False, "expected_version": "different-snapshot"})
            update.assert_not_called()
            call(self.scoped, "update_inbox_rule", {"account": OUTLOOK, "rule_id": "r", "patch": {"isEnabled": True},
                                                   "dry_run": False, "expected_version": "guard-snapshot"})
            update.assert_called_once()

    def test_disabled_unsafe_rule_stays_disabled_but_enabling_is_rejected(self):
        current = {"rule": {"actions": {"permanentDelete": True}, "isEnabled": False}, "expected_version": "v"}
        with patch.object(server, "get_inbox_rule", return_value=current), \
                patch.object(server, "update_inbox_rule", return_value={"dry_run": True}) as update:
            call(self.scoped, "update_inbox_rule", {"account": OUTLOOK, "rule_id": "r", "patch": {"displayName": "disabled"}})
            with self.assertRaises(ToolError):
                call(self.scoped, "update_inbox_rule", {"account": OUTLOOK, "rule_id": "r", "patch": {"isEnabled": True}})
            update.assert_called_once()


class MailOnlyOrganizingTests(unittest.TestCase):
    def setUp(self):
        self.scoped = mail_only.create_server([GMAIL, OUTLOOK], "organize")

    def test_protected_labels_and_english_folder_names_block_before_credentials(self):
        with patch.object(auth, "resolve_provider") as resolve:
            for name in ("label_thread", "unlabel_thread"):
                with self.subTest(tool=name), self.assertRaises(ToolError):
                    call(self.scoped, name, {"account": GMAIL, "thread_id": "t", "label_ids": ["TRASH"]})
            for folder in ("Trash", "SPAM", "Deleted Items", "Junk Email"):
                with self.subTest(folder=folder), self.assertRaises(ToolError):
                    call(self.scoped, "move_thread", {"account": OUTLOOK, "thread_id": "t", "folder": folder})
        resolve.assert_not_called()

    def test_outlook_only_vetted_well_known_archive_and_inbox_are_allowed(self):
        with patch.object(auth, "resolve_provider", return_value=(OUTLOOK, "microsoft", {})), \
                patch.object(server, "move_thread", return_value={"moved": 1}) as move:
            for folder in ("Itens Excluídos", "Lixo Eletrônico", "custom", "Outbox", "a-rchive"):
                with self.subTest(folder=folder), self.assertRaises(ToolError):
                    call(self.scoped, "move_thread", {"account": OUTLOOK, "thread_id": "t", "folder": folder})
            move.assert_not_called()
            for folder in ("archive", "Inbox"):
                self.assertEqual(call(self.scoped, "move_thread", {"account": OUTLOOK, "thread_id": "t", "folder": folder}), {"moved": 1})
        self.assertEqual(move.call_count, 2)

    def test_gmail_localized_protected_label_cannot_bypass_name_check(self):
        with patch.object(auth, "resolve_provider", return_value=(GMAIL, "google", {})), \
                patch.object(server, "list_labels", return_value={"labels": [{"id": "TRASH", "name": "Lixeira"}]}), \
                patch.object(server, "move_thread") as move:
            with self.assertRaises(ToolError):
                call(self.scoped, "move_thread", {"account": GMAIL, "thread_id": "t", "folder": "Lixeira"})
        move.assert_not_called()


class MailOnlyCursorTests(unittest.TestCase):
    def setUp(self):
        self.scoped = mail_only.create_server([GMAIL, OUTLOOK])

    def test_forged_graph_routes_rejected_before_provider_or_credentials(self):
        paths = ["/v1.0/users/unapproved@example.com/messages", "/v1.0/me/drive",
                 "/v1.0/me/messages/../drive", "/v1.0/me/messages%2F..%2Fdrive"]
        urls = ["https://graph.microsoft.com" + path for path in paths]
        urls.extend(["https://evil.example/v1.0/me/messages", "http://graph.microsoft.com/v1.0/me/messages"])
        with patch.object(auth, "resolve_provider") as resolve, patch.object(server, "provider_for") as provider:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ToolError):
                    call(self.scoped, "search_threads", {"account": OUTLOOK, "query": "q", "page_token": cursor(next_url=url)})
        resolve.assert_not_called()
        provider.assert_not_called()

    def test_cursor_is_bound_to_account_query_and_operation(self):
        with patch.object(server, "provider_for") as provider:
            for token in (cursor(account="other@example.com"), cursor(query="different"),
                          cursor(name="list_drafts")):
                with self.subTest(token=token), self.assertRaises(ToolError):
                    call(self.scoped, "search_threads", {"account": OUTLOOK, "query": "q", "page_token": token})
        provider.assert_not_called()

    def test_valid_graph_collection_continuation_passes_unchanged(self):
        provider = Mock()
        provider.list_drafts.return_value = {"drafts": []}
        token = cursor(name="list_drafts", next_url=graph.BASE + "/me/mailFolders/drafts/messages?$skiptoken=opaque")
        with patch.object(server, "provider_for", return_value=provider):
            call(self.scoped, "list_drafts", {"account": OUTLOOK, "page_token": token})
        provider.list_drafts.assert_called_once_with(20, token)


class MailOnlyCLITests(unittest.TestCase):
    def test_cli_defaults_to_no_accounts_and_read_only(self):
        with patch.dict(mail_only.os.environ, {}, clear=True), patch.object(mail_only, "create_server") as create:
            mail_only.main([])
        create.assert_called_once_with([], "read-only")
        create.return_value.run.assert_called_once_with()

    def test_environment_and_explicit_cli_override(self):
        env = {"GMAIL_MCP_ALLOWED_ACCOUNTS": GMAIL + ", " + OUTLOOK, "GMAIL_MCP_MODE": "organize"}
        with patch.dict(mail_only.os.environ, env, clear=True), patch.object(mail_only, "create_server") as create:
            mail_only.main([])
            create.assert_called_once_with([GMAIL, OUTLOOK], "organize")
            create.reset_mock()
            mail_only.main(["--allow-account", GMAIL, "--mode", "read-only"])
            create.assert_called_once_with([GMAIL], "read-only")


if __name__ == "__main__":
    unittest.main()
