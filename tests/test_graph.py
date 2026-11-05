import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from gmail_mcp import graph


class GraphRequestTests(unittest.TestCase):
    def test_rejects_untrusted_urls_before_attaching_token(self):
        unsafe = [
            "http://graph.microsoft.com/v1.0/me/messages",
            "https://evil.example/messages", "//evil.example/messages",
            "https://graph.microsoft.com.evil.example/messages",
            "https://graph.microsoft.com:444/messages",
            "https://user@graph.microsoft.com/messages",
            "https://@graph.microsoft.com/messages",
            "https://graph.microsoft.com/messages#fragment",
            "https://graph.microsoft.com\\@evil.example/messages",
            "https://graph.microsoft.com/\nmessages",
        ]
        with patch.object(graph._OPENER, "open") as send:
            for url in unsafe:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    graph._request("secret", "GET", url)
            send.assert_not_called()

    def test_request_uses_timeout_and_unredirected_bearer(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"ok": True}).encode()
        with patch.object(graph._OPENER, "open", return_value=response) as send:
            self.assertEqual(graph._request("secret", "GET", "/me/messages"), {"ok": True})
        req = send.call_args.args[0]
        self.assertEqual(req.full_url, graph.BASE + "/me/messages")
        self.assertEqual(req.unredirected_hdrs["Authorization"], "Bearer secret")
        self.assertEqual(send.call_args.kwargs, {"timeout": 30})
        self.assertIsNone(graph._NoRedirect().redirect_request(None, None, 302, "", {},
                                                             "https://evil.example"))

    def test_permission_error_has_status_and_reconnect_guidance(self):
        failure = urllib.error.HTTPError(graph.BASE, 403, "denied", {}, io.BytesIO(b"denied"))
        with patch.object(graph._OPENER, "open", side_effect=failure):
            with self.assertRaises(graph.GraphError) as caught:
                graph._request("secret", "GET", "/me/messages")
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("reconnect explicitly", str(caught.exception))


class GraphEnumerationTests(unittest.TestCase):
    def test_full_thread_reads_all_pages_and_sorts(self):
        next_url = graph.BASE + "/me/messages?$skiptoken=opaque"
        pages = [
            {"value": [{"id": "2", "receivedDateTime": "2026-02-02"}],
             "@odata.nextLink": next_url},
            {"value": [{"id": "1", "receivedDateTime": "2026-01-01"}]},
        ]
        with patch.object(graph, "_request", side_effect=pages) as request:
            result = graph.get_thread("token", "a'b/c")
        self.assertEqual([item["message_id"] for item in result["messages"]], ["1", "2"])
        self.assertIn("a%27%27b%2Fc", request.call_args_list[0].args[2])
        self.assertEqual(request.call_args_list[1].args[2], next_url)

    def test_folders_follow_pages(self):
        pages = [{"value": [{"id": "1", "displayName": "First"}],
                  "@odata.nextLink": graph.BASE + "/me/mailFolders?$skiptoken=next"},
                 {"value": [{"id": "2", "displayName": "Second"}]}]
        with patch.object(graph, "_request", side_effect=pages):
            self.assertEqual([f["id"] for f in graph.list_folders("token")], ["1", "2"])

    def test_pagination_loop_stops_before_any_move_or_folder_write(self):
        next_url = graph.BASE + "/me/messages?$skiptoken=repeat"
        pages = [{"value": [{"id": "1"}], "@odata.nextLink": next_url},
                 {"value": [{"id": "2"}], "@odata.nextLink": next_url}]
        with patch.object(graph, "_request", side_effect=pages) as request:
            with self.assertRaisesRegex(RuntimeError, "pagination loop"):
                graph.move_thread("token", "conversation", "New custom folder")
        self.assertEqual([c.args[1] for c in request.call_args_list], ["GET", "GET"])

    def test_move_collects_pages_before_mutating_and_reports_partial_failure(self):
        pages = [{"value": [{"id": "a/b"}],
                  "@odata.nextLink": graph.BASE + "/me/messages?$skiptoken=next"},
                 {"value": [{"id": "second"}, {"id": "third"}]},
                 {"id": "moved-a"}, graph.GraphError("denied", 403)]
        with patch.object(graph, "_request", side_effect=pages) as request:
            result = graph.move_thread("token", "conversation", "archive")
        self.assertEqual([c.args[1] for c in request.call_args_list], ["GET", "GET", "POST", "POST"])
        self.assertIn("/a%2Fb/move", request.call_args_list[2].args[2])
        self.assertFalse(result["complete"])
        self.assertFalse(result["outcome_uncertain"])
        self.assertEqual(result["moved_messages"], [{"previous_id": "a/b", "id": "moved-a"}])
        self.assertEqual(result["failed_message_id"], "second")
        self.assertEqual(result["remaining_message_ids"], ["third"])

    def test_transport_failure_is_uncertain_and_never_retried(self):
        with patch.object(graph, "_request", side_effect=[{"value": [{"id": "a"}]},
                                                         urllib.error.URLError("timeout")]) as request:
            result = graph.move_thread("token", "conversation", "archive")
        self.assertTrue(result["outcome_uncertain"])
        self.assertEqual(request.call_count, 2)

    def test_http_timeout_is_uncertain_and_never_retried(self):
        with patch.object(graph, "_request", side_effect=[{"value": [{"id": "a"}]},
                                                         graph.GraphError("timeout", 408)]) as request:
            result = graph.move_thread("token", "conversation", "archive")
        self.assertTrue(result["outcome_uncertain"])
        self.assertEqual(request.call_count, 2)

    def test_no_messages_does_not_create_destination(self):
        with patch.object(graph, "_request", return_value={"value": []}) as request:
            self.assertEqual(graph.move_thread("token", "empty", "New folder")["moved"], 0)
        request.assert_called_once()

    def test_nextlink_foreign_origin_is_rejected(self):
        with patch.object(graph, "_request", return_value={"value": [],
                                                          "@odata.nextLink": "https://evil.example"}) as request:
            with self.assertRaises(ValueError):
                graph.list_folders("token")
        request.assert_called_once()


class SearchTranslationTests(unittest.TestCase):
    def test_translates_gmail_syntax_to_kql(self):
        cases = {
            "invoice": "invoice",
            '"tax invoice"': '"tax invoice"',
            "invoice OR receipt": "invoice OR receipt",
            "(invoice OR receipt) amazon": "(invoice OR receipt) amazon",
            "from:a@example.com": "from:a@example.com",
            "from:(a.com OR b.com)": "from:(a.com OR b.com)",
            'subject:"order 12"': 'subject:"order 12"',
            "after:2025/9/1": "received>=2025-09-01",
            "before:2026-07-01": "received<2026-07-01",
            "after:09/28/2025 before:2026/7/1": "received>=2025-09-28 received<2026-07-01",
            "has:attachment": "hasattachments:true",
            "is:unread": "isread:false",
            "is:read": "isread:true",
            "invoice -amazon": "invoice NOT amazon",
            '-"tax invoice"': 'NOT "tax invoice"',
            "-from:a.com": "NOT from:a.com",
            "-(invoice OR receipt) amazon": "NOT (invoice OR receipt) amazon",
            '"keep ( spacing )" OR x': '"keep ( spacing )" OR x',
            "received>=2025-09-01": "received>=2025-09-01",
            "hasattachments:true": "hasattachments:true",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(graph.gmail_query_to_kql(query), (expected, []))

    def test_relative_dates_count_back_from_today(self):
        today = graph.dt.date(2026, 11, 5)
        with patch.object(graph.dt, "date", wraps=graph.dt.date) as date:
            date.today.return_value = today
            self.assertEqual(graph.gmail_query_to_kql("newer_than:2d")[0], "received>=2026-11-03")
            self.assertEqual(graph.gmail_query_to_kql("older_than:1y")[0], "received<2025-11-05")

    def test_terms_graph_cannot_run_are_dropped_and_reported(self):
        cases = {
            "label:Junk invoice": ("invoice", ["label:Junk"]),
            "invoice -label:Junk": ("invoice", ["-label:Junk"]),
            "invoice NOT label:Junk": ("invoice", ["label:Junk"]),
            "NOT label:Junk invoice": ("invoice", ["label:Junk"]),
            "category:promotions": ("", ["category:promotions"]),
            "invoice OR label:Junk": ("invoice", ["label:Junk"]),
            "label:Junk OR invoice": ("invoice", ["label:Junk"]),
            "(label:a OR label:b) invoice": ("invoice", ["label:a", "label:b"]),
            "(invoice OR label:Junk) amazon": ("(invoice) amazon", ["label:Junk"]),
            "invoice NOT (label:Junk) receipt": ("invoice receipt", ["label:Junk"]),
            "-(label:Junk) receipt": ("receipt", ["label:Junk"]),
            "NOT (label:a OR label:b) receipt": ("receipt", ["label:a", "label:b"]),
            "NOT ((label:Junk)) receipt": ("receipt", ["label:Junk"]),
            "invoice OR label:Junk receipt": ("invoice receipt", ["label:Junk"]),
            "invoice OR (label:Junk) receipt": ("invoice receipt", ["label:Junk"]),
            "amazon label:Junk OR invoice": ("amazon invoice", ["label:Junk"]),
            "(label:a label:b) invoice": ("invoice", ["label:a", "label:b"]),
            "is:starred invoice": ("invoice", ["is:starred"]),
            "has:drive invoice": ("invoice", ["has:drive"]),
            "after:someday invoice": ("invoice", ["after:someday"]),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(graph.gmail_query_to_kql(query), expected)

    def search_path(self, query):
        with patch.object(graph, "_bounded_items", return_value=([], None)) as items:
            result = graph.search_threads("token", query, 5)
        path = items.call_args.args[1]
        term = path.split("$search=")[1].split("&")[0]
        return graph.urllib.parse.unquote(term), result

    def test_quotes_in_a_query_are_escaped_for_graph(self):
        term, _ = self.search_path('subject:"order 12" OR "tax invoice"')
        self.assertEqual(term, '"subject:\\"order 12\\" OR \\"tax invoice\\""')

    def test_backslashes_are_escaped_before_quotes(self):
        term, _ = self.search_path('"a\\b"')
        self.assertEqual(term, '"\\"a\\\\b\\""')

    def test_a_query_of_only_gmail_terms_finds_nothing_rather_than_everything(self):
        with patch.object(graph, "_bounded_items") as items:
            result = graph.search_threads("token", "label:Junk", 5)
        items.assert_not_called()
        self.assertEqual(result, {"threads": [], "next_page_token": None, "ignored_terms": ["label:Junk"]})

    def test_ignored_terms_are_reported_with_results(self):
        _, result = self.search_path("label:Junk invoice")
        self.assertEqual(result["ignored_terms"], ["label:Junk"])

    def test_clean_queries_add_no_extra_fields(self):
        _, result = self.search_path("invoice")
        self.assertNotIn("ignored_terms", result)


if __name__ == "__main__":
    unittest.main()
