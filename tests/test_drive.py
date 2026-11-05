import unittest
from unittest.mock import MagicMock, Mock, patch

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


class ReadContentTests(unittest.TestCase):
    def read(self, meta):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = meta
        return service

    def test_pdfs_are_read_as_text(self):
        service = self.read({"name": "bill.pdf", "mimeType": "application/pdf", "size": "9"})
        extracted = {"text": "Invoice", "pages": 1}
        with patch.object(drive, "_download", return_value=b"%PDF"), \
             patch.object(drive.documents, "extract_text", return_value=extracted) as extract:
            result = drive.read_content(service, "f1")
        extract.assert_called_once_with(b"%PDF", "bill.pdf", "application/pdf")
        self.assertEqual((result["text"], result["pages"], result["truncated"]), ("Invoice", 1, False))

    def test_unreadable_files_return_a_note(self):
        service = self.read({"name": "a.png", "mimeType": "image/png", "size": "9"})
        extracted = {"text": None, "note": "no"}
        with patch.object(drive, "_download", return_value=b"x"), \
             patch.object(drive.documents, "extract_text", return_value=extracted):
            result = drive.read_content(service, "f1")
        self.assertIsNone(result["text"])
        self.assertEqual(result["note"], "no")

    def test_google_files_are_exported(self):
        service = self.read({"name": "Doc", "mimeType": "application/vnd.google-apps.document"})
        service.files.return_value.export.return_value.execute.return_value = b"hello"
        self.assertEqual(drive.read_content(service, "f1")["text"], "hello")

    def test_google_types_without_an_export_are_explained(self):
        service = self.read({"name": "Form", "mimeType": "application/vnd.google-apps.form"})
        result = drive.read_content(service, "f1")
        self.assertIsNone(result["text"])
        self.assertIn("exported", result["note"])

    def test_long_text_is_cut_and_flagged(self):
        service = self.read({"name": "Doc", "mimeType": "application/vnd.google-apps.document"})
        service.files.return_value.export.return_value.execute.return_value = b"abcdef"
        result = drive.read_content(service, "f1", max_chars=3)
        self.assertEqual((result["text"], result["truncated"]), ("abc", True))

    def test_oversized_files_are_refused_before_download(self):
        size = str(drive.documents.MAX_BYTES + 1)
        service = self.read({"name": "big.pdf", "mimeType": "application/pdf", "size": size})
        with patch.object(drive, "_download") as download, self.assertRaises(ValueError):
            drive.read_content(service, "f1")
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
