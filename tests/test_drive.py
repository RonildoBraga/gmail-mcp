import unittest
from unittest.mock import Mock

from gmail_mcp import drive


class DrivePaginationTests(unittest.TestCase):
    def test_short_pages_are_filled_and_continuation_returned(self):
        svc = Mock()
        svc.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "1"}], "nextPageToken": "two"},
            {"files": [{"id": "2"}], "nextPageToken": "three"},
        ]
        result = drive.list_files(svc, 2, q="trashed=false")
        self.assertEqual([f["id"] for f in result["files"]], ["1", "2"])
        self.assertEqual(result["next_page_token"], "three")
        calls = svc.files.return_value.list.call_args_list
        self.assertEqual([c.kwargs["pageSize"] for c in calls], [2, 1])
        self.assertEqual(calls[1].kwargs["pageToken"], "two")
        self.assertIn("nextPageToken", calls[0].kwargs["fields"])

    def test_loop_and_invalid_limits_fail(self):
        svc = Mock()
        svc.files.return_value.list.return_value.execute.return_value = {"nextPageToken": "same"}
        with self.assertRaisesRegex(RuntimeError, "pagination loop"):
            drive.list_files(svc, 2)
        for value in (0, -1, True, "2"):
            with self.assertRaises(ValueError):
                drive.list_files(svc, value)


if __name__ == "__main__":
    unittest.main()
