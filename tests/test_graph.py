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


if __name__ == "__main__":
    unittest.main()
