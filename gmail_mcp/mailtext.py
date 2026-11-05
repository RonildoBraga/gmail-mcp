"""Turn raw email bodies into short, readable text.

Marketing and transactional mail is mostly markup, tracking links and invisible
filler. Reading it raw costs thousands of tokens and hides what the sender wrote,
so bodies are reduced to the text a person would see. Text a reader can't see,
such as hidden preheaders, is left out rather than passed on as if it were content.

Long links keep their domain, so a suspicious destination stays visible. Callers
that need an exact URL can ask for the original HTML.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections import Counter
from collections.abc import Callable
from html.parser import HTMLParser

DEFAULT_MAX_CHARS = 20_000

_LONGEST_KEPT_URL = 60
_SUBSTANTIAL_PLAIN_TEXT = 200
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
              "param", "source", "track", "wbr"}
_SKIPPED_TAGS = {"head", "noscript", "script", "style", "svg", "template", "title"}
_BLOCK_TAGS = {"address", "article", "aside", "blockquote", "center", "div", "dl", "dt", "dd",
               "fieldset", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
               "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot", "thead", "tr",
               "ul"}
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)",
    re.IGNORECASE,
)
_FONT_SIZE = re.compile(r"font-size\s*:\s*([0-9.]+)", re.IGNORECASE)
_INVISIBLE = re.compile(
    "[­͏؜ᅟᅠ឴឵᠋-᠎​-‏‪-‮"
    "⁠-⁯ㅤ︀-️﻿ﾠ]"
)
_ANGLED_URL = re.compile(r"<(https?://[^\s<>]+)>", re.IGNORECASE)
_BARE_URL = re.compile(r"""(?<!\[link: )https?://[^\s<>()"']+""", re.IGNORECASE)


def remove_invisible(text: str) -> str:
    return _INVISIBLE.sub("", text)


def _link_label(url: str) -> str:
    return f"[link: {urllib.parse.urlsplit(url).hostname or 'unknown'}]"


def _shorten_url(url: str) -> str:
    return url if len(url) <= _LONGEST_KEPT_URL else _link_label(url)


def clean_text(text: str) -> str:
    """Remove invisible characters, long tracking URLs and excess whitespace."""
    text = remove_invisible(text).replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _ANGLED_URL.sub(lambda match: f"({_shorten_url(match[1])})", text)
    text = _BARE_URL.sub(lambda match: _shorten_url(match[0]), text)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # Each open element: tag, hidden, zero font size, link target, parts index.
        self._open: list[dict] = []
        # Senders control this markup, so per-tag checks must not scan the stack:
        # that makes deeply nested or unbalanced HTML quadratic.
        self._open_counts: Counter[str] = Counter()

    def _visible(self) -> bool:
        top = self._open[-1] if self._open else None
        return not top or not (top["hidden"] or top["zero_font"])

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        style = attributes.get("style") or ""
        parent = self._open[-1] if self._open else {"hidden": False, "zero_font": False}
        hidden = (parent["hidden"] or tag in _SKIPPED_TAGS or "hidden" in attributes
                  or bool(_HIDDEN_STYLE.search(style)))
        size = _FONT_SIZE.search(style)
        zero_font = float(size[1]) == 0 if size else parent["zero_font"]
        if tag == "br" or tag == "hr":
            self.parts.append("\n")
        elif tag == "li" and not hidden:
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        if tag not in _VOID_TAGS:
            # Links can't nest, and following nested ones would re-read their text.
            href = attributes.get("href") if tag == "a" and not self._open_counts["a"] else None
            self._open.append({"tag": tag, "hidden": hidden, "zero_font": zero_font,
                               "href": href, "start": len(self.parts)})
            self._open_counts[tag] += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._open_counts[tag]:
            return
        while self._open:
            element = self._open.pop()
            self._open_counts[element["tag"]] -= 1
            if element["tag"] == tag:
                break
        if tag in _BLOCK_TAGS or tag == "li":
            self.parts.append("\n")
        if element["href"] and not element["hidden"]:
            self._mark_link(element)

    def _mark_link(self, element: dict) -> None:
        href = element["href"].strip()
        label = "".join(self.parts[element["start"]:]).strip()
        if not label or not href.lower().startswith(("http://", "https://")):
            return
        if href.rstrip("/") == label.rstrip("/"):
            return
        self.parts.append(f" ({href})" if len(href) <= _LONGEST_KEPT_URL else f" {_link_label(href)}")

    def handle_data(self, data: str) -> None:
        if not self._visible():
            return
        self.parts.append(data if self._open_counts["pre"] else re.sub(r"\s+", " ", data))


def html_to_text(source: str) -> str:
    """The text a person would see when reading this HTML."""
    parser = _TextExtractor()
    parser.feed(source)
    parser.close()
    text = clean_text("".join(parser.parts))
    if text or not source.strip():
        return text
    # Malformed markup can hide everything; a rough result beats an empty one.
    return clean_text(html.unescape(re.sub(r"(?s)<[^>]+>", " ", source)))


def best_text(plain: str, html_body: str, convert: Callable[[str], str] = html_to_text) -> str:
    """Prefer the plain part, unless it is only a stub for a fuller HTML part."""
    plain_text = clean_text(plain) if plain else ""
    if not html_body or len(plain_text) >= _SUBSTANTIAL_PLAIN_TEXT:
        return plain_text
    html_text = convert(html_body)
    return html_text if len(html_text) > len(plain_text) else plain_text


def check_body_format(body_format: str) -> None:
    if body_format not in ("text", "html"):
        raise ValueError("body_format must be 'text' or 'html'.")


def limit(text: str, max_chars: int) -> tuple[str, bool]:
    """Return text cut to max_chars, and whether anything was removed."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer.")
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True
