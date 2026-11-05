import contextlib
import io
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from gmail_mcp import documents


def pdf_with(text: str) -> bytes:
    """A minimal one-page PDF with real text, built without a PDF library."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    start = out.tell()
    out.write(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % start)
    return out.getvalue()


def zipped(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return out.getvalue()


WORD = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
SHEET = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


class ExtractTextTests(unittest.TestCase):
    def test_pdf_text_and_page_count(self):
        result = documents.extract_text(pdf_with("Invoice total 42"), "bill.pdf", "application/pdf")
        self.assertEqual(result, {"text": "Invoice total 42", "pages": 1})

    def test_pdf_is_recognised_by_name_when_its_type_is_generic(self):
        result = documents.extract_text(pdf_with("Hello"), "bill.PDF", "application/octet-stream")
        self.assertEqual(result["text"], "Hello")

    def test_pdf_without_text_says_it_is_probably_a_scan(self):
        result = documents.extract_text(pdf_with(""), "scan.pdf", "application/pdf")
        self.assertEqual(result["text"], "")
        self.assertIn("scan", result["note"])

    def test_damaged_files_report_a_note_instead_of_raising(self):
        for name, mime in (("x.pdf", "application/pdf"), ("x.docx", ""), ("x.xlsx", "")):
            with self.subTest(name=name):
                result = documents.extract_text(b"not really a document", name, mime)
                self.assertIsNone(result["text"])
                self.assertIn("damaged", result["note"])

    def test_word_paragraphs(self):
        data = zipped({"word/document.xml": f'<w:document {WORD}><w:body><w:p><w:r><w:t>Hello </w:t></w:r>'
                                            f'<w:r><w:t>there</w:t></w:r></w:p><w:p><w:r><w:t>Bye</w:t></w:r></w:p>'
                                            "</w:body></w:document>"})
        self.assertEqual(documents.extract_text(data, "a.docx", "")["text"], "Hello there\nBye")

    def test_excel_rows_with_shared_and_inline_strings(self):
        data = zipped({
            "xl/sharedStrings.xml": f"<sst {SHEET}><si><t>Name</t></si></sst>",
            "xl/worksheets/sheet1.xml": f'<worksheet {SHEET}><sheetData><row><c t="s"><v>0</v></c><c><v>5</v></c>'
                                        '<c t="inlineStr"><is><t>note</t></is></c></row></sheetData></worksheet>',
        })
        self.assertEqual(documents.extract_text(data, "a.xlsx", "")["text"], "Sheet 1\nName\t5\tnote")

    def test_oversized_zip_members_are_refused(self):
        data = zipped({"word/document.xml": "x"})
        with patch.object(documents, "_MAX_UNZIPPED_BYTES", 0):
            with self.assertRaises(ValueError):
                documents._read_docx(data)

    def test_plain_text_encodings(self):
        cases = [(b"\xef\xbb\xbfhello", "hello"), ("héllo".encode("latin-1"), "héllo"),
                 ("hello".encode("utf-16"), "hello")]
        for data, expected in cases:
            with self.subTest(data=data):
                self.assertEqual(documents.extract_text(data, "a.txt", "text/plain")["text"], expected)

    def test_html_attachments_are_cleaned(self):
        result = documents.extract_text(b"<style>x</style><p>Hello</p>", "a.html", "text/html")
        self.assertEqual(result["text"], "Hello")

    def test_unsupported_types_explain_themselves(self):
        result = documents.extract_text(b"\x89PNG", "a.png", "image/png")
        self.assertIsNone(result["text"])
        self.assertIn("image/png", result["note"])

    def test_slow_files_cannot_hang_the_server(self):
        expired = subprocess.TimeoutExpired("python", 60)
        with patch.object(documents.subprocess, "run", side_effect=expired):
            result = documents.extract_text(b"%PDF", "a.pdf", "application/pdf")
        self.assertIsNone(result["text"])
        self.assertIn("too long", result["note"])

    def test_parsing_runs_in_an_isolated_child_process(self):
        done = subprocess.CompletedProcess([], 0, b'{"text": "ok"}', b"")
        with patch.object(documents.subprocess, "run", return_value=done) as run:
            documents.extract_text(b"%PDF", "a.pdf", "application/pdf")
        self.assertEqual(run.call_args.args[0][1:3], ["-I", "-c"])
        self.assertIn("timeout", run.call_args.kwargs)

    def test_files_planted_in_the_working_directory_are_not_imported(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "pypdf.py").write_text("raise SystemExit(9)\n")
            with contextlib.chdir(folder):
                result = documents.extract_text(pdf_with("Hello"), "a.pdf", "application/pdf")
        self.assertEqual(result["text"], "Hello")


class DecorativeImageTests(unittest.TestCase):
    def test_small_inline_images_are_decorative(self):
        self.assertTrue(documents.is_decorative("image/png", 5_000, True))

    def test_photos_and_regular_attachments_are_kept(self):
        self.assertFalse(documents.is_decorative("image/jpeg", 2_000_000, True))
        self.assertFalse(documents.is_decorative("image/png", 5_000, False))
        self.assertFalse(documents.is_decorative("application/pdf", 5_000, True))


class SaveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.target = Path(folder.name) / "saved"
        self.enterContext(patch.object(documents, "SAVE_DIR", self.target))

    def test_saved_files_are_private(self):
        path = documents.save(b"data", "a.pdf")
        self.assertEqual(path.read_bytes(), b"data")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)

    def test_never_replaces_an_existing_file(self):
        first = documents.save(b"one", "a.pdf")
        second = documents.save(b"two", "a.pdf")
        self.assertEqual(first.read_bytes(), b"one")
        self.assertEqual(second.name, "a (1).pdf")

    def test_sender_supplied_names_stay_inside_the_folder(self):
        names = ["../../etc/passwd", "/etc/passwd", "..\\..\\evil.txt", "a\x00b.txt", "...", ""]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(documents.save(b"x", name).parent, self.target)

    def test_names_cannot_disguise_their_extension(self):
        self.assertEqual(documents.safe_filename("invoice‮fdp.exe"), "invoicefdp.exe")

    def test_long_names_keep_their_extension(self):
        self.assertTrue(documents.safe_filename("a" * 400 + ".pdf").endswith(".pdf"))

    def test_size_limit(self):
        with self.assertRaises(ValueError):
            documents.check_size(documents.MAX_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
