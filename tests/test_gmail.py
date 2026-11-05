import base64
import unittest
from unittest.mock import MagicMock, patch

from gmail_mcp import gmail, providers


def encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def part(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": encoded(text)}}


def attachment(part_id="2", filename="bill.pdf", mime="application/pdf", size=9000, inline=False):
    disposition = "inline" if inline else "attachment"
    return {"partId": part_id, "filename": filename, "mimeType": mime,
            "headers": [{"name": "Content-Disposition", "value": f'{disposition}; filename="x"'}],
            "body": {"attachmentId": "volatile", "size": size}}


def thread(payload: dict, snippet: str = "s") -> dict:
    payload.setdefault("headers", [{"name": "Subject", "value": "Hi"}])
    return {"id": "t1", "messages": [{"id": "m1", "snippet": snippet, "payload": payload}]}


class RenderThreadTests(unittest.TestCase):
    def rendered(self, payload, **options):
        return gmail.render_thread(thread(payload), **options)["messages"][0]

    def test_html_only_mail_becomes_clean_text(self):
        message = self.rendered(part("text/html", "<style>x</style><p>Your order</p>"))
        self.assertEqual(message["body"], "Your order")

    def test_very_large_html_is_converted_outside_the_server(self):
        with patch.object(gmail.documents, "_LARGEST_IN_PROCESS_HTML", 4), \
             patch.object(gmail.documents, "_run_worker", return_value={"text": "converted"}) as worker:
            self.assertEqual(self.rendered(part("text/html", "<p>big</p>"))["body"], "converted")
        worker.assert_called_once()

    def test_nested_plain_part_is_preferred(self):
        alternative = {"mimeType": "multipart/alternative",
                       "parts": [part("text/plain", "p" * 300), part("text/html", "<p>html</p>")]}
        payload = {"mimeType": "multipart/mixed", "parts": [alternative, attachment()]}
        self.assertEqual(self.rendered(payload)["body"], "p" * 300)

    def test_html_format_returns_the_original_markup_and_links(self):
        html = '<a href="https://example.com/very/long">x</a>'
        self.assertEqual(self.rendered(part("text/html", html), body_format="html")["body"], html)

    def test_text_attachments_are_never_treated_as_the_body(self):
        note = {**part("text/plain", "attached notes"), "filename": "notes.txt", "partId": "2"}
        payload = {"mimeType": "multipart/mixed", "parts": [part("text/html", "<p>real body</p>"), note]}
        self.assertEqual(self.rendered(payload)["body"], "real body")

    def test_long_bodies_are_cut_and_flagged(self):
        message = self.rendered(part("text/plain", "x" * 50), max_chars=10)
        self.assertEqual(len(message["body"]), 10)
        self.assertTrue(message["body_truncated"])

    def test_complete_bodies_carry_no_truncation_flag(self):
        self.assertNotIn("body_truncated", self.rendered(part("text/plain", "short")))

    def test_rejects_unknown_formats(self):
        with self.assertRaises(ValueError):
            gmail.render_thread(thread(part("text/plain", "x")), body_format="pdf")

    def test_snippets_lose_entities_and_invisible_filler(self):
        message = gmail.render_thread(thread(part("text/plain", "x"), "Don&#39;t͏ miss"))["messages"][0]
        self.assertEqual(message["snippet"], "Don't miss")

    def test_attachments_are_listed_by_their_stable_part_id(self):
        payload = {"mimeType": "multipart/mixed", "parts": [part("text/plain", "x"), attachment()]}
        self.assertEqual(self.rendered(payload)["attachments"], [
            {"attachment_id": "2", "filename": "bill.pdf", "mime_type": "application/pdf", "size": 9000}])

    def test_embedded_logos_are_not_listed(self):
        logo = attachment("3", "logo.png", "image/png", 4000, inline=True)
        photo = attachment("4", "photo.jpg", "image/jpeg", 2_000_000, inline=True)
        payload = {"mimeType": "multipart/related", "parts": [part("text/html", "<p>x</p>"), logo, photo]}
        self.assertEqual([a["filename"] for a in self.rendered(payload)["attachments"]], ["photo.jpg"])


class GmailAttachmentTests(unittest.TestCase):
    def provider(self, payload):
        provider = providers.GmailProvider.__new__(providers.GmailProvider)
        provider.email = "a@example.com"
        provider.svc = MagicMock()
        self.messages = provider.svc.users.return_value.messages.return_value
        self.messages.get.return_value.execute.return_value = {"payload": payload}
        return provider

    def test_downloads_using_a_fresh_gmail_attachment_id(self):
        provider = self.provider({"mimeType": "multipart/mixed", "parts": [attachment()]})
        self.messages.attachments.return_value.get.return_value.execute.return_value = {"data": encoded("pdf-bytes")}
        result = provider.get_attachment("m1", "2")
        self.assertEqual(result, {"filename": "bill.pdf", "mime_type": "application/pdf", "data": b"pdf-bytes"})
        self.assertEqual(self.messages.attachments.return_value.get.call_args.kwargs["id"], "volatile")

    def test_small_attachments_stored_inline_need_no_second_request(self):
        small = {"partId": "2", "filename": "a.txt", "mimeType": "text/plain",
                 "body": {"data": encoded("hi"), "size": 2}}
        provider = self.provider({"mimeType": "multipart/mixed", "parts": [small]})
        self.assertEqual(provider.get_attachment("m1", "2")["data"], b"hi")
        self.messages.attachments.assert_not_called()

    def test_unknown_attachment_is_a_clear_error(self):
        provider = self.provider({"mimeType": "multipart/mixed", "parts": [attachment()]})
        with self.assertRaisesRegex(ValueError, "No attachment"):
            provider.get_attachment("m1", "9")

    def test_oversized_attachment_is_refused_before_download(self):
        provider = self.provider({"mimeType": "multipart/mixed", "parts": [attachment(size=10**9)]})
        with self.assertRaises(ValueError):
            provider.get_attachment("m1", "2")
        self.messages.attachments.assert_not_called()


class AutoDeletingLabelTests(unittest.TestCase):
    def setUp(self):
        self.provider = providers.GmailProvider.__new__(providers.GmailProvider)
        self.provider.email = "a@example.com"
        self.provider.svc = MagicMock()
        users = self.provider.svc.users.return_value
        self.threads = users.threads.return_value
        self.labels = users.labels.return_value
        self.labels.list.return_value.execute.return_value = {"labels": [
            {"id": "TRASH", "name": "TRASH"},
            {"id": "SPAM", "name": "SPAM"},
            {"id": "Label_1", "name": "Junk"},
        ]}

    def test_labelling_never_applies_trash_or_spam(self):
        for label in ("TRASH", "SPAM", "trash", " Spam "):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "30 days"):
                self.provider.modify_labels("t1", add=["Label_1", label], remove=[])
        self.threads.modify.assert_not_called()

    def test_moving_to_trash_or_spam_is_refused_without_creating_a_label(self):
        for folder in ("Trash", "spam"):
            with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, "30 days"):
                self.provider.move_thread("t1", folder)
        self.threads.modify.assert_not_called()
        self.labels.create.assert_not_called()

    def test_custom_labels_and_restoring_from_trash_still_reach_gmail(self):
        self.provider.modify_labels("t1", add=["Label_1"], remove=["TRASH", "SPAM"])
        self.threads.modify.assert_called_once_with(
            userId="me", id="t1",
            body={"addLabelIds": ["Label_1"], "removeLabelIds": ["TRASH", "SPAM"]},
        )

    def test_filing_under_a_custom_label_and_archiving_still_work(self):
        self.provider.move_thread("t1", "Junk")
        self.provider.archive_thread("t2")
        bodies = [call.kwargs["body"] for call in self.threads.modify.call_args_list]
        self.assertEqual(bodies, [
            {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]},
            {"addLabelIds": [], "removeLabelIds": ["INBOX"]},
        ])


if __name__ == "__main__":
    unittest.main()
