import unittest
from unittest.mock import MagicMock, patch

from gmail_mcp import graph
from gmail_mcp.providers import GmailProvider, GraphProvider


class GmailPaginationTests(unittest.TestCase):
    def setUp(self):
        self.provider = object.__new__(GmailProvider)
        self.provider.email = "google@example.com"
        self.provider.svc = MagicMock()

    def test_search_aggregates_short_pages_with_budget_and_returns_cursor(self):
        resource = self.provider.svc.users.return_value.threads.return_value
        resource.list.return_value.execute.side_effect = [
            {"threads": [{"id": "1"}], "nextPageToken": "second"},
            {"threads": [{"id": "2"}, {"id": "3"}], "nextPageToken": "third"},
        ]
        with patch("gmail_mcp.providers.gmail.thread_summary", side_effect=lambda svc, tid: {"id": tid}):
            result = self.provider.search_threads("query", 3)
        self.assertEqual(result["threads"], [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        self.assertEqual(result["next_page_token"], "third")
        self.assertEqual(resource.list.call_args_list[0].kwargs["maxResults"], 3)
        self.assertEqual(resource.list.call_args_list[1].kwargs["maxResults"], 2)
        self.assertEqual(resource.list.call_args_list[1].kwargs["pageToken"], "second")

    def test_drafts_continuation_empty_page_and_exhaustion(self):
        resource = self.provider.svc.users.return_value.drafts.return_value
        resource.list.return_value.execute.side_effect = [
            {"nextPageToken": "next"}, {"drafts": [{"id": "a"}]},
        ]
        result = self.provider.list_drafts(4, "resume")
        self.assertEqual(result["drafts"], [{"id": "a"}])
        self.assertIsNone(result["next_page_token"])
        self.assertEqual(resource.list.call_args_list[0].kwargs["pageToken"], "resume")

    def test_gmail_page_size_is_capped(self):
        resource = self.provider.svc.users.return_value.drafts.return_value
        resource.list.return_value.execute.return_value = {"drafts": []}
        self.provider.list_drafts(900)
        self.assertEqual(resource.list.call_args.kwargs["maxResults"], 500)

    def test_repeated_gmail_cursor_raises(self):
        resource = self.provider.svc.users.return_value.drafts.return_value
        resource.list.return_value.execute.return_value = {"nextPageToken": "repeat"}
        with self.assertRaisesRegex(RuntimeError, "pagination loop"):
            self.provider.list_drafts(3)
        self.assertEqual(resource.list.call_count, 2)

    def test_invalid_limit_makes_no_api_call(self):
        for limit in (0, -1, True, "2"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.provider.list_drafts(limit)
        self.provider.svc.users.return_value.drafts.return_value.list.assert_not_called()


class GraphPaginationTests(unittest.TestCase):
    def test_short_pages_overflow_is_preserved_and_nextlink_is_unchanged(self):
        next_url = graph.BASE + "/me/messages?$skiptoken=opaque%2Bsigned&$top=3"
        third_url = graph.BASE + "/me/messages?$skiptoken=last&$top=3"
        pages = [
            {"value": [{"id": "1"}], "@odata.nextLink": next_url},
            {"value": [{"id": "2"}, {"id": "3"}, {"id": "4"}],
             "@odata.nextLink": third_url},
            {"value": [{"id": "5"}]},
        ]
        with patch.object(graph, "_request", side_effect=pages) as request:
            first = graph.search_threads("token", "query", 3, account="a@example.com")
            second = graph.search_threads("token", "query", 3, first["next_page_token"],
                                          account="a@example.com")
        self.assertEqual([item["message_id"] for item in first["threads"]], ["1", "2", "3"])
        self.assertEqual([item["message_id"] for item in second["threads"]], ["4", "5"])
        self.assertIsNone(second["next_page_token"])
        self.assertEqual(request.call_args_list[1].args[2], next_url)
        self.assertEqual(request.call_args_list[2].args[2], third_url)

    def test_cursor_rejects_different_account_query_and_operation(self):
        pages = [{"value": [{"id": "1"}],
                  "@odata.nextLink": graph.BASE + "/me/messages?$skiptoken=next"}]
        with patch.object(graph, "_request", side_effect=pages) as request:
            cursor = graph.search_threads("token", "query", 1, account="first")["next_page_token"]
            with self.assertRaises(ValueError):
                graph.search_threads("token", "query", 1, cursor, account="second")
            with self.assertRaises(ValueError):
                graph.search_threads("token", "different", 1, cursor, account="first")
            with self.assertRaises(ValueError):
                graph.list_drafts("token", 1, cursor, account="first")
        request.assert_called_once()

    def test_repeated_graph_nextlink_raises_even_at_budget_boundary(self):
        repeated = graph.BASE + "/me/messages?$skiptoken=repeat"
        pages = [{"value": [], "@odata.nextLink": repeated},
                 {"value": [{"id": "1"}], "@odata.nextLink": repeated}]
        with patch.object(graph, "_request", side_effect=pages):
            with self.assertRaisesRegex(RuntimeError, "pagination loop"):
                graph.search_threads("token", "", 1)

    def test_graph_loop_is_detected_across_resumed_calls(self):
        second_url = graph.BASE + "/me/messages?$skiptoken=second"
        third_url = graph.BASE + "/me/messages?$skiptoken=third"
        pages = [{"value": [{"id": "1"}], "@odata.nextLink": second_url},
                 {"value": [{"id": "2"}], "@odata.nextLink": third_url},
                 {"value": [{"id": "3"}], "@odata.nextLink": second_url}]
        with patch.object(graph, "_request", side_effect=pages):
            cursor = graph.search_threads("token", "", 1)["next_page_token"]
            cursor = graph.search_threads("token", "", 1, cursor)["next_page_token"]
            with self.assertRaisesRegex(RuntimeError, "pagination loop"):
                graph.search_threads("token", "", 1, cursor)

    def test_graph_provider_preserves_existing_payload_and_adds_cursor(self):
        provider = GraphProvider("outlook@example.com", "token")
        with patch.object(graph, "_request", return_value={"value": [{"id": "draft"}]}):
            result = provider.list_drafts(2)
        self.assertEqual(result, {"account": "outlook@example.com", "drafts": [{"id": "draft"}],
                                  "next_page_token": None})


if __name__ == "__main__":
    unittest.main()
