"""Provider operations are entirely mocked; no store, tokens, or network needed."""

import copy
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gmail_mcp import rules


FILTER = {"criteria": {"from": "billing@example.test"}, "action": {"addLabelIds": ["Label_1"]}}
OLD_FILTER = {"id": "old", **FILTER}
RULE = {"id": "rule/one", "displayName": "Billing", "sequence": 7, "isEnabled": False,
        "isReadOnly": False, "conditions": {"senderContains": ["billing"]},
        "exceptions": {"subjectContains": ["personal"]}, "actions": {"markAsRead": True}}


class ProviderDenied(Exception):
    def __init__(self, status=403):
        super().__init__("Provider denied request")
        self.resp = SimpleNamespace(status=status)


class GmailFiltersTests(unittest.TestCase):
    def setUp(self):
        self.resource = MagicMock()
        self.provider = SimpleNamespace(email="gmail@example.test", provider="google", svc=MagicMock())
        self.provider.svc.users.return_value.settings.return_value.filters.return_value = self.resource
        self.resource.get.return_value.execute.return_value = copy.deepcopy(OLD_FILTER)
        self.resource.create.return_value.execute.return_value = {"id": "new", **copy.deepcopy(FILTER)}

    def test_list_and_get_use_native_objects_without_retries(self):
        self.resource.list.return_value.execute.return_value = {"filter": [OLD_FILTER]}
        self.assertEqual(rules.list_filters(self.provider)["filters"], [OLD_FILTER])
        result = rules.get_filter(self.provider, "old")
        self.assertEqual(result["filter"], OLD_FILTER)
        self.assertEqual(len(result["expected_version"]), 64)
        self.resource.get.assert_called_once_with(userId="me", id="old")
        self.resource.get.return_value.execute.assert_called_once_with(num_retries=0)

    def test_every_preview_performs_no_writes_and_pins_apply_account(self):
        previews = [rules.create_filter(self.provider, FILTER),
                    rules.delete_filter(self.provider, "old"),
                    rules.replace_filter(self.provider, "old", FILTER)]
        for preview in previews:
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["account"], self.provider.email)
            self.assertEqual(preview["apply"]["account"], self.provider.email)
            self.assertIs(preview["apply"]["dry_run"], False)
        self.resource.create.assert_not_called()
        self.resource.delete.assert_not_called()

    def test_create_returns_recovery_id(self):
        result = rules.create_filter(self.provider, FILTER, dry_run=False)
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["recovery"]["filter_id"], "new")
        self.resource.create.assert_called_once_with(userId="me", body=FILTER)

    def test_delete_preserves_recreation_body_and_requires_preview(self):
        with self.assertRaisesRegex(ValueError, "expected_version"):
            rules.delete_filter(self.provider, "old", dry_run=False)
        preview = rules.delete_filter(self.provider, "old")
        result = rules.delete_filter(self.provider, "old", False, preview["expected_version"])
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["recovery"]["filter"], FILTER)
        self.assertNotIn("id", result["recovery"]["filter"])
        self.resource.delete.assert_called_once_with(userId="me", id="old")

    def test_stale_snapshot_and_cross_account_versions_refuse_mutation(self):
        version = rules.get_filter(self.provider, "old")["expected_version"]
        self.provider.email = "different@example.test"
        with self.assertRaisesRegex(ValueError, "stale"):
            rules.replace_filter(self.provider, "old", FILTER, False, version)
        self.provider.email = "gmail@example.test"
        self.resource.get.return_value.execute.return_value = {**OLD_FILTER, "criteria": {"query": "new"}}
        with self.assertRaisesRegex(ValueError, "stale"):
            rules.delete_filter(self.provider, "old", False, version)
        self.resource.create.assert_not_called()
        self.resource.delete.assert_not_called()

    def test_replace_creates_before_deleting_and_returns_both_ids(self):
        version = rules.get_filter(self.provider, "old")["expected_version"]
        self.resource.reset_mock()
        result = rules.replace_filter(self.provider, "old", FILTER, False, version)
        calls = [call[0] for call in self.resource.mock_calls if call[0] in ("get", "create", "delete")]
        self.assertEqual(calls, ["get", "create", "delete"])
        self.assertEqual(result["status"], "replaced")
        self.assertEqual((result["old_filter_id"], result["new_filter_id"]), ("old", "new"))
        self.assertEqual(result["recovery"]["old_filter"], FILTER)

    def test_replace_create_network_failure_leaves_old_and_never_retries(self):
        version = rules.get_filter(self.provider, "old")["expected_version"]
        self.resource.create.return_value.execute.side_effect = urllib.error.URLError("lost response")
        result = rules.replace_filter(self.provider, "old", FILTER, False, version)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["old_filter_delete_attempted"])
        self.resource.delete.assert_not_called()
        self.resource.create.assert_called_once()

    def test_replace_limit_error_leaves_old_and_reports_definitive_failure(self):
        version = rules.get_filter(self.provider, "old")["expected_version"]
        self.resource.create.return_value.execute.side_effect = ProviderDenied(400)
        result = rules.replace_filter(self.provider, "old", FILTER, False, version)
        self.assertEqual(result["status"], "failed")
        self.resource.delete.assert_not_called()

    def test_replace_delete_network_failure_reports_partial_and_retains_recovery(self):
        version = rules.get_filter(self.provider, "old")["expected_version"]
        self.resource.delete.return_value.execute.side_effect = urllib.error.URLError("lost response")
        result = rules.replace_filter(self.provider, "old", FILTER, False, version)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["delete_outcome"], "uncertain")
        self.assertEqual(result["new_filter_id"], "new")
        self.assertEqual(result["recovery"]["old_filter_id"], "old")
        self.assertFalse(result["retry_safe"])

    def test_unexpected_same_id_never_deletes_old(self):
        version = rules.get_filter(self.provider, "old")["expected_version"]
        self.resource.create.return_value.execute.return_value = OLD_FILTER
        result = rules.replace_filter(self.provider, "old", FILTER, False, version)
        self.assertEqual(result["status"], "uncertain")
        self.resource.delete.assert_not_called()

    def test_permission_errors_include_explicit_reconnect_guidance(self):
        self.resource.list.return_value.execute.side_effect = ProviderDenied()
        with self.assertRaisesRegex(PermissionError, "reconnect it explicitly"):
            rules.list_filters(self.provider)
        self.resource.create.return_value.execute.side_effect = ProviderDenied()
        result = rules.create_filter(self.provider, FILTER, False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("gmail.settings.basic", result["error"])

    def test_validation_rejects_output_id_empty_actions_and_wrong_provider(self):
        for body in ({**FILTER, "id": "forbidden"}, {"criteria": {}, "action": {}}, {"action": []}):
            with self.assertRaises(ValueError):
                rules.create_filter(self.provider, body, False)
        self.provider.provider = "microsoft"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            rules.list_filters(self.provider)
        self.resource.create.assert_not_called()


class InboxRulesTests(unittest.TestCase):
    def setUp(self):
        self.provider = SimpleNamespace(email="outlook@example.test", provider="microsoft", token="fake")
        self.snapshot = copy.deepcopy(RULE)
        self.request_patch = patch.object(rules.graph, "_request")
        self.request = self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.request.return_value = self.snapshot

    def version(self):
        return rules.get_inbox_rule(self.provider, "rule/one")["expected_version"]

    def test_list_follows_graph_nextlinks_and_preserves_native_order(self):
        next_url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messageRules?$skip=1"
        self.request.side_effect = [{"value": [{"id": "one", "sequence": 7}], "@odata.nextLink": next_url},
                                    {"value": [{"id": "two", "sequence": 2}]}]
        result = rules.list_inbox_rules(self.provider)
        self.assertEqual([r["id"] for r in result["rules"]], ["one", "two"])
        self.assertEqual(self.request.call_args_list[1].args[2], next_url)

    def test_list_detects_loop(self):
        self.request.return_value = {"value": [], "@odata.nextLink": rules._RULES}
        with self.assertRaisesRegex(RuntimeError, "pagination loop"):
            rules.list_inbox_rules(self.provider)
        self.assertEqual(self.request.call_count, 1)

    def test_get_encodes_entire_id_and_versions_native_snapshot(self):
        result = rules.get_inbox_rule(self.provider, "rule/one")
        self.assertEqual(result["rule"], RULE)
        self.assertEqual(self.request.call_args.args[2], rules._RULES + "/rule%2Fone")

    def test_all_previews_perform_gets_only(self):
        body = rules._rule_restore(self.snapshot)
        previews = [rules.create_inbox_rule(self.provider, body),
                    rules.update_inbox_rule(self.provider, "rule/one", {"displayName": "New"}),
                    rules.delete_inbox_rule(self.provider, "rule/one")]
        for preview in previews:
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["apply"]["account"], self.provider.email)
        self.assertTrue(all(call.args[1] == "GET" for call in self.request.call_args_list))

    def test_create_requires_sequence_and_retains_disabled_and_exceptions(self):
        body = rules._rule_restore(self.snapshot)
        result = rules.create_inbox_rule(self.provider, body, False)
        self.assertEqual(result["status"], "created")
        self.assertEqual(self.request.call_args.args[1:], ("POST", rules._RULES, body))
        self.assertFalse(result["rule"]["isEnabled"])
        self.assertEqual(result["recovery"]["rule_id"], "rule/one")
        with self.assertRaisesRegex(ValueError, "sequence"):
            rules.create_inbox_rule(self.provider, {"displayName": "No order", "actions": {}}, False)

    def test_patch_only_supplied_fields_preserves_sequence_and_false(self):
        version = self.version()
        patch_body = {"isEnabled": False, "exceptions": {"subjectContains": ["new"]}}
        result = rules.update_inbox_rule(self.provider, "rule/one", patch_body, False, version)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(self.request.call_args.args[1:], ("PATCH", rules._RULES + "/rule%2Fone", patch_body))
        self.assertNotIn("sequence", self.request.call_args.args[3])
        self.assertEqual(result["recovery"]["patch"]["sequence"], 7)
        self.assertFalse(result["recovery"]["patch"]["isEnabled"])
        self.assertEqual(result["recovery"]["patch"]["exceptions"], RULE["exceptions"])

    def test_explicit_sequence_sent_without_other_defaults(self):
        version = self.version()
        rules.update_inbox_rule(self.provider, "rule/one", {"sequence": 4}, False, version)
        self.assertEqual(self.request.call_args.args[3], {"sequence": 4})

    def test_readonly_rules_refuse_update_and_delete(self):
        self.snapshot["isReadOnly"] = True
        for action in (lambda: rules.update_inbox_rule(self.provider, "rule/one", {"isEnabled": True}),
                       lambda: rules.delete_inbox_rule(self.provider, "rule/one")):
            with self.assertRaisesRegex(ValueError, "isReadOnly"):
                action()
        self.assertTrue(all(call.args[1] == "GET" for call in self.request.call_args_list))

    def test_delete_recovery_retains_order_and_exceptions_without_output_fields(self):
        version = self.version()
        result = rules.delete_inbox_rule(self.provider, "rule/one", False, version)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["recovery"]["rule"]["sequence"], 7)
        self.assertEqual(result["recovery"]["rule"]["exceptions"], RULE["exceptions"])
        self.assertFalse(result["recovery"]["rule"]["isEnabled"])
        self.assertNotIn("id", result["recovery"]["rule"])
        self.assertNotIn("isReadOnly", result["recovery"]["rule"])

    def test_changed_account_or_stale_snapshot_refuses_patch(self):
        version = self.version()
        self.provider.email = "other@example.test"
        with self.assertRaisesRegex(ValueError, "stale"):
            rules.update_inbox_rule(self.provider, "rule/one", {"displayName": "new"}, False, version)
        self.provider.email = "outlook@example.test"
        self.snapshot["sequence"] = 10
        with self.assertRaisesRegex(ValueError, "stale"):
            rules.delete_inbox_rule(self.provider, "rule/one", False, version)
        self.assertTrue(all(call.args[1] == "GET" for call in self.request.call_args_list))

    def test_network_patch_failure_reports_unknown_and_does_not_retry(self):
        version = self.version()
        self.request.side_effect = [self.snapshot, urllib.error.URLError("lost response")]
        self.request.reset_mock()
        result = rules.update_inbox_rule(self.provider, "rule/one", {"displayName": "new"}, False, version)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.request.call_count, 2)
        self.assertFalse(result["retry_safe"])
        self.assertIn("before", result)

    def test_graph_permission_guidance(self):
        error = RuntimeError("Graph GET failed (403): insufficient scope")
        self.request.side_effect = error
        with self.assertRaisesRegex(PermissionError, "MailboxSettings.ReadWrite"):
            rules.list_inbox_rules(self.provider)

    def test_missing_create_id_reports_unknown_not_success(self):
        self.request.return_value = {}
        result = rules.create_inbox_rule(self.provider, rules._rule_restore(RULE), False)
        self.assertEqual(result["status"], "uncertain")

    def test_patch_restore_clears_new_exceptions_when_previously_absent(self):
        del self.snapshot["exceptions"]
        preview = rules.update_inbox_rule(self.provider, "rule/one", {"exceptions": {"subjectContains": ["x"]}})
        self.assertEqual(preview["recovery"]["patch"]["exceptions"], {})

    def test_invalid_patches_rejected_without_requests(self):
        for patch_body in ({}, {"id": "one"}, {"isReadOnly": False}, {"isEnabled": None},
                           {"sequence": True}, {"sequence": -1}, {"actions": []}):
            with self.assertRaises(ValueError):
                rules.update_inbox_rule(self.provider, "rule/one", patch_body, False)
        self.request.assert_not_called()

    def test_wrong_provider_has_clear_error(self):
        self.provider.provider = "google"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            rules.list_inbox_rules(self.provider)
        self.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
