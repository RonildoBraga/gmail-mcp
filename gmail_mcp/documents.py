"""Read text out of attachments and Drive files, and save them to disk.

These files come from strangers, so parsing happens in a short-lived child
process. A malformed or hostile file can then only cost that process: it can't
hang the server, and it never shares memory with the decrypted account tokens.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

from . import mailtext

MAX_BYTES = 25 * 1024 * 1024
SAVE_DIR = Path.home() / "Downloads" / "gmail-mcp"

_MAX_PDF_PAGES = 500
_MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
_WORKER_TIMEOUT_SECONDS = 60
_DECORATIVE_IMAGE_BYTES = 64 * 1024
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_TEXT_SUFFIXES = {".csv", ".eml", ".ics", ".json", ".log", ".md", ".txt", ".tsv", ".xml"}
_TEXT_TYPES = {"application/json", "application/xml"}
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def is_decorative(mime: str, size: int, inline: bool) -> bool:
    """Logos and tracking images embedded in a message aren't attachments to a reader."""
    return inline and mime.startswith("image/") and size < _DECORATIVE_IMAGE_BYTES


def check_size(size: int) -> None:
    if size > MAX_BYTES:
        raise ValueError(f"This file is {size} bytes; the limit is {MAX_BYTES}.")


def _kind(filename: str, mime: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if mime == _DOCX_TYPE or suffix == ".docx":
        return "docx"
    if mime == _XLSX_TYPE or suffix == ".xlsx":
        return "xlsx"
    if mime == "text/html" or suffix in (".html", ".htm"):
        return "html"
    if mime.startswith("text/") or mime in _TEXT_TYPES or suffix in _TEXT_SUFFIXES:
        return "text"
    return None


def _read_pdf(data: bytes) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        return {"text": None, "note": "This PDF is password protected."}
    pages = reader.pages[:_MAX_PDF_PAGES]
    text = "\n\n".join(page.extract_text() or "" for page in pages).strip()
    result = {"text": mailtext.clean_text(text), "pages": len(reader.pages)}
    if not text:
        result["note"] = "This PDF has no selectable text, so it is probably a scan."
    elif len(reader.pages) > _MAX_PDF_PAGES:
        result["note"] = f"Only the first {_MAX_PDF_PAGES} pages were read."
    return result


def _zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > _MAX_UNZIPPED_BYTES:
        raise ValueError(f"{name} is too large to unpack.")
    return archive.read(info)


def _read_docx(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(_zip_member(archive, "word/document.xml"))
    lines = ("".join(node.text or "" for node in paragraph.iter(f"{_WORD}t"))
             for paragraph in root.iter(f"{_WORD}p"))
    return {"text": mailtext.clean_text("\n".join(lines))}


def _sheet_rows(root: ElementTree.Element, shared: list[str]) -> list[str]:
    rows = []
    for row in root.iter(f"{_SHEET}row"):
        cells = []
        for cell in row.iter(f"{_SHEET}c"):
            value = cell.find(f"{_SHEET}v")
            if cell.get("t") == "inlineStr":
                cells.append("".join(node.text or "" for node in cell.iter(f"{_SHEET}t")))
            elif value is None or value.text is None:
                cells.append("")
            elif cell.get("t") == "s":
                cells.append(shared[int(value.text)])
            else:
                cells.append(value.text)
        if any(cells):
            rows.append("\t".join(cells))
    return rows


def _read_xlsx(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(_zip_member(archive, "xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.iter(f"{_SHEET}t"))
                      for item in root.iter(f"{_SHEET}si")]
        sheets = sorted(name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
        blocks = []
        for number, name in enumerate(sheets, 1):
            rows = _sheet_rows(ElementTree.fromstring(_zip_member(archive, name)), shared)
            blocks.append(f"Sheet {number}\n" + "\n".join(rows))
    return {"text": "\n\n".join(blocks)}


_READERS = {"pdf": _read_pdf, "docx": _read_docx, "xlsx": _read_xlsx}


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def extract_text(data: bytes, filename: str, mime: str) -> dict:
    """Return {"text", optional "pages" and "note"}. text is None when unreadable."""
    kind = _kind(filename, mime)
    if kind is None:
        return {"text": None, "note": f"Text can't be read from this file type ({mime or 'unknown'})."}
    if kind == "text":
        return {"text": _decode(data)}
    if kind == "html":
        return {"text": mailtext.html_to_text(_decode(data))}
    request = json.dumps({"kind": kind}).encode() + b"\n" + data
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-m", "gmail_mcp.documents"], input=request,
            capture_output=True, timeout=_WORKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"text": None, "note": "Reading this file took too long."}
    if result.returncode != 0:
        return {"text": None, "note": "This file is damaged or isn't what its name says."}
    return json.loads(result.stdout)


def safe_filename(name: str) -> str:
    """A plain file name, whatever path or control characters the sender supplied."""
    base = mailtext.remove_invisible(name).replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f\x7f:]", "_", base).strip(" .")
    return base[-180:] or "attachment"


def save(data: bytes, filename: str) -> Path:
    """Write to the private save folder, never replacing an existing file."""
    SAVE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = Path(safe_filename(filename))
    for number in range(1000):
        name = base.name if number == 0 else f"{base.stem} ({number}){base.suffix}"
        path = SAVE_DIR / name
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return path
    raise FileExistsError(f"Too many files named {base.name} in {SAVE_DIR}.")


def _worker() -> None:
    header, _, data = sys.stdin.buffer.read().partition(b"\n")
    json.dump(_READERS[json.loads(header)["kind"]](data), sys.stdout)


if __name__ == "__main__":
    _worker()
