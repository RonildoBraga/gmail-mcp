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


class InboxScopeTests(unittest.TestCase):
    def path(self, query):
        with patch.object(graph, "_request", return_value={"value": []}) as request:
            result = graph.search_threads("token", query, 5)
        return request.call_args.args[2], result

    def test_inbox_alone_lists_the_inbox_newest_first(self):
        path, result = self.path("in:inbox")
        self.assertTrue(path.startswith("/me/mailFolders/inbox/messages?"))
        self.assertIn("$orderby=receivedDateTime", path)
        self.assertNotIn("ignored_terms", result)

    def test_inbox_scopes_the_rest_of_a_search(self):
        for query in ("in:inbox is:unread", "IN:INBOX is:unread", "is:unread in:inbox",
                      "in:inbox AND is:unread"):
            with self.subTest(query=query):
                path, result = self.path(query)
                self.assertTrue(path.startswith("/me/mailFolders/inbox/messages?$search="))
                self.assertIn("isread", path.lower())
                self.assertNotIn("in%3Ainbox", path)
                self.assertNotIn("ignored_terms", result)

    def test_searches_without_it_cover_the_whole_mailbox(self):
        self.assertTrue(self.path("is:unread")[0].startswith("/me/messages?"))
        self.assertTrue(self.path("")[0].startswith("/me/messages?"))

    def test_inbox_that_cannot_become_a_path_is_reported_not_applied(self):
        for query in ("-in:inbox invoice", "in:inbox OR invoice", "invoice OR in:inbox",
                      "(in:inbox invoice)", "in:sent invoice"):
            with self.subTest(query=query):
                self.assertEqual(graph._inbox_scope(query), (query, False))
                path, result = self.path(query)
                self.assertTrue(path.startswith("/me/messages?"))
                self.assertTrue(result["ignored_terms"])

    def test_a_cursor_from_the_inbox_cannot_continue_a_whole_mailbox_search(self):
        pages = [{"value": [{"id": "1"}, {"id": "2"}]}]
        with patch.object(graph, "_request", side_effect=pages):
            token = graph.search_threads("token", "in:inbox", 1)["next_page_token"]
        with self.assertRaisesRegex(ValueError, "continuation token"):
            graph.search_threads("token", "", 1, token)


class FolderPathTests(unittest.TestCase):
    def test_a_path_walks_child_folders_without_creating_anything(self):
        pages = [{"value": [{"id": "parent/id"}]}, {"value": [{"id": "child"}]}]
        with patch.object(graph, "_request", side_effect=pages) as request:
            self.assertEqual(graph.resolve_folder("token", "Finance/Receipts", create=True), "child")
        first, second = [call.args for call in request.call_args_list]
        self.assertEqual(first[1], "GET")
        self.assertTrue(first[2].startswith("/me/mailFolders?$filter="))
        self.assertIn("Finance", first[2])
        self.assertEqual(second[1], "GET")
        self.assertTrue(second[2].startswith("/me/mailFolders/parent%2Fid/childFolders?$filter="))
        self.assertIn("Receipts", second[2])

    def test_a_missing_child_is_created_under_its_parent_not_at_the_top(self):
        pages = [{"value": [{"id": "parent"}]}, {"value": []}, {"id": "new"}]
        with patch.object(graph, "_request", side_effect=pages) as request:
            self.assertEqual(graph.resolve_folder("token", "Finance/New", create=True), "new")
        self.assertEqual(request.call_args.args[1:4],
                         ("POST", "/me/mailFolders/parent/childFolders", {"displayName": "New"}))

    def test_a_missing_child_is_an_error_unless_creating(self):
        pages = [{"value": [{"id": "parent"}]}, {"value": []}]
        with patch.object(graph, "_request", side_effect=pages) as request, \
                self.assertRaisesRegex(RuntimeError, "not found"):
            graph.resolve_folder("token", "Finance/New")
        self.assertEqual(request.call_count, 2)

    def test_malformed_paths_are_rejected_before_any_request(self):
        with patch.object(graph, "_request") as request:
            for name in ("", "/Finance", "Finance/", "Finance//Receipts", "Finance/ "):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "folder path"):
                    graph.resolve_folder("token", name)
        request.assert_not_called()

    def test_well_known_names_need_no_lookup(self):
        with patch.object(graph, "_request") as request:
            self.assertEqual(graph.resolve_folder("token", "Archive"), "archive")
        request.assert_not_called()

    def test_child_folders_are_listed_by_path(self):
        pages = [
            {"value": [{"id": "a=", "displayName": "Finance", "totalItemCount": 0, "childFolderCount": 1},
                       {"id": "b", "displayName": "Inbox", "totalItemCount": 3}]},
            {"value": [{"id": "c", "displayName": "Receipts", "totalItemCount": 9, "childFolderCount": 1}]},
            {"value": [{"id": "d", "displayName": "2026", "totalItemCount": 1}]},
        ]
        with patch.object(graph, "_request", side_effect=pages) as request:
            folders = graph.list_folders("token")
        self.assertEqual([(f["name"], f["count"]) for f in folders],
                         [("Finance", 0), ("Finance/Receipts", 9), ("Finance/Receipts/2026", 1), ("Inbox", 3)])
        self.assertIn("/me/mailFolders/a%3D/childFolders", request.call_args_list[1].args[2])

    def test_folders_that_delete_mail_are_refused_before_any_request(self):
        with patch.object(graph, "_request") as request:
            for folder in ("Deleted Items", "deleteditems", "Junk Email", "junkemail", " junk email "):
                with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, "Refusing"):
                    graph.move_thread("token", "conversation", folder)
        request.assert_not_called()

    def test_the_custom_junk_folder_is_still_allowed(self):
        pages = [{"value": [{"id": "m"}]}, {"value": [{"id": "junk-folder"}]}, {"id": "moved"}]
        with patch.object(graph, "_request", side_effect=pages) as request:
            result = graph.move_thread("token", "conversation", "Junk")
        self.assertTrue(result["complete"])
        self.assertEqual(request.call_args.args[3], {"destinationId": "junk-folder"})


class GraphMessageTests(unittest.TestCase):
    def thread(self, message, attachments=(), **options):
        pages = [{"value": [message]}, {"value": list(attachments)}]
        with patch.object(graph, "_request", side_effect=pages) as request:
            return graph.get_thread("token", "c1", **options)["messages"][0], request

    def test_bodies_and_previews_are_cleaned(self):
        message, _ = self.thread({"id": "1", "bodyPreview": "Hi\u034f there",
                                  "body": {"content": "Hello\u200b   you\r\n\r\n\r\n\r\nBye"}})
        self.assertEqual(message["body"], "Hello you\n\nBye")
        self.assertEqual(message["snippet"], "Hi there")

    def test_text_is_requested_by_default_and_html_on_demand(self):
        _, request = self.thread({"id": "1"})
        self.assertEqual(request.call_args_list[0].kwargs["prefer"], 'outlook.body-content-type="text"')
        message, request = self.thread({"id": "1", "body": {"content": "<p>x</p>"}}, body_format="html")
        self.assertEqual(request.call_args_list[0].kwargs["prefer"], 'outlook.body-content-type="html"')
        self.assertEqual(message["body"], "<p>x</p>")

    def test_long_bodies_are_cut_and_flagged(self):
        message, _ = self.thread({"id": "1", "body": {"content": "x" * 50}}, max_chars=10)
        self.assertEqual(len(message["body"]), 10)
        self.assertTrue(message["body_truncated"])

    def test_messages_without_attachments_need_no_extra_request(self):
        message, request = self.thread({"id": "1", "hasAttachments": False})
        self.assertEqual(message["attachments"], [])
        self.assertEqual(request.call_count, 1)

    def test_attachments_are_listed_without_embedded_logos(self):
        message, _ = self.thread({"id": "m/1", "hasAttachments": True}, [
            {"id": "a1", "name": "bill.pdf", "contentType": "application/pdf", "size": 9000, "isInline": False},
            {"id": "a2", "name": "logo.png", "contentType": "image/png", "size": 4000, "isInline": True},
        ])
        self.assertEqual(message["attachments"], [
            {"attachment_id": "a1", "filename": "bill.pdf", "mime_type": "application/pdf", "size": 9000}])

    def test_attachment_paths_escape_ids(self):
        self.assertEqual(graph._attachment_path("m/1=", "a/2"), "/me/messages/m%2F1%3D/attachments/a%2F2")

    def test_downloads_the_raw_attachment_bytes(self):
        meta = {"name": "bill.pdf", "contentType": "application/pdf", "size": 3}
        with patch.object(graph, "_request", return_value=meta), \
             patch.object(graph, "_send", return_value=b"pdf") as send:
            result = graph.get_attachment("token", "m1", "a1")
        self.assertEqual(result, {"filename": "bill.pdf", "mime_type": "application/pdf", "data": b"pdf"})
        self.assertTrue(send.call_args.args[2].endswith("/attachments/a1/$value"))

    def test_oversized_attachment_is_refused_before_download(self):
        meta = {"name": "big.pdf", "size": graph.documents.MAX_BYTES + 1}
        with patch.object(graph, "_request", return_value=meta), patch.object(graph, "_send") as send:
            with self.assertRaises(ValueError):
                graph.get_attachment("token", "m1", "a1")
        send.assert_not_called()

    def test_download_size_is_checked_even_when_metadata_understates_it(self):
        big = b"x" * (graph.documents.MAX_BYTES + 1)
        with patch.object(graph, "_request", return_value={"size": 1}), \
             patch.object(graph, "_send", return_value=big):
            with self.assertRaises(ValueError):
                graph.get_attachment("token", "m1", "a1")

    def test_requests_read_no_more_than_the_limit(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"data"
        with patch.object(graph._OPENER, "open", return_value=response):
            graph._send("secret", "GET", "/me/messages/1/attachments/2/$value", None, "x", limit=9)
        response.__enter__.return_value.read.assert_called_once_with(9)


if __name__ == "__main__":
    unittest.main()
