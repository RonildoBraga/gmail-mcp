import unittest

from gmail_mcp import mailtext


class HtmlToTextTests(unittest.TestCase):
    def test_keeps_what_a_reader_sees(self):
        html = "<html><head><title>x</title><style>p{color:red}</style></head><body><p>Hello &amp; welcome</p></body></html>"
        self.assertEqual(mailtext.html_to_text(html), "Hello & welcome")

    def test_blocks_lists_and_breaks_become_lines(self):
        html = "<h1>Title</h1><p>One<br>Two</p><ul><li>First</li><li>Second</li></ul>"
        self.assertEqual(mailtext.html_to_text(html), "Title\n\nOne\nTwo\n\n- First\n\n- Second")

    def test_table_cells_stay_on_one_line(self):
        html = "<table><tr><td>Item</td><td>$5.00</td></tr><tr><td>Total</td><td>$5.00</td></tr></table>"
        self.assertEqual(mailtext.html_to_text(html), "Item $5.00\n\nTotal $5.00")

    def test_hidden_content_is_left_out(self):
        hidden = [
            '<div style="display:none">secret</div>', '<span style="visibility: hidden">secret</span>',
            '<div hidden>secret</div>', '<span style="mso-hide:all">secret</span>',
            '<span style="opacity:0">secret</span>', '<span style="font-size:0">secret</span>',
            '<div style="display:none"><table><tr><td><b>secret</b></td></tr></table></div>',
            "<script>secret()</script>", "<noscript>secret</noscript>",
        ]
        for fragment in hidden:
            with self.subTest(fragment=fragment):
                self.assertEqual(mailtext.html_to_text(f"<p>shown</p>{fragment}<p>after</p>"), "shown\n\nafter")

    def test_visible_text_inside_a_zero_font_layout_cell_is_kept(self):
        html = '<td style="font-size:0"><span style="font-size:15px">shown</span></td>'
        self.assertEqual(mailtext.html_to_text(html), "shown")

    def test_partial_opacity_stays_visible(self):
        self.assertEqual(mailtext.html_to_text('<span style="opacity:0.5">shown</span>'), "shown")

    def test_unclosed_tags_do_not_lose_later_text(self):
        self.assertEqual(mailtext.html_to_text("<div><b>one<p>two</div>three"), "one\ntwo\nthree")

    def test_markup_that_hides_everything_falls_back_to_a_rough_result(self):
        self.assertEqual(mailtext.html_to_text('<body style="display:none">text</body>'), "text")

    def test_short_links_are_kept_exactly(self):
        html = '<a href="https://example.com/a">Open</a>'
        self.assertEqual(mailtext.html_to_text(html), "Open (https://example.com/a)")

    def test_long_links_keep_their_domain(self):
        html = f'<a href="https://track.example.com/{"x" * 80}">Open</a>'
        self.assertEqual(mailtext.html_to_text(html), "Open [link: track.example.com]")

    def test_link_whose_text_is_its_url_is_not_repeated(self):
        self.assertEqual(mailtext.html_to_text('<a href="https://a.com/">https://a.com</a>'), "https://a.com")

    def test_image_only_and_mail_links_add_nothing(self):
        html = '<a href="https://a.com/x"><img src="y"></a><a href="mailto:a@example.com">Mail us</a>'
        self.assertEqual(mailtext.html_to_text(html), "Mail us")


class CleanTextTests(unittest.TestCase):
    def test_removes_invisible_filler(self):
        self.assertEqual(mailtext.clean_text("Hi͏ ‌­there​﻿"), "Hi there")

    def test_removes_direction_controls_that_disguise_text(self):
        self.assertEqual(mailtext.clean_text("invoice‮gpj.exe"), "invoicegpj.exe")

    def test_collapses_whitespace_and_blank_lines(self):
        self.assertEqual(mailtext.clean_text("a  \t b\r\n\r\n\r\n\r\n  c d  "), "a b\n\nc d")

    def test_shortens_long_urls_in_plain_text(self):
        url = "https://mandrillapp.com/track/" + "y" * 80
        self.assertEqual(mailtext.clean_text(f"Open <{url}> or {url}"),
                         "Open ([link: mandrillapp.com]) or [link: mandrillapp.com]")

    def test_keeps_short_urls(self):
        self.assertEqual(mailtext.clean_text("See https://example.com/page"), "See https://example.com/page")


class BestTextTests(unittest.TestCase):
    def test_prefers_a_real_plain_part(self):
        self.assertEqual(mailtext.best_text("p" * 300, "<p>html</p>"), "p" * 300)

    def test_uses_html_when_the_plain_part_is_a_stub(self):
        self.assertEqual(mailtext.best_text("View in browser", f"<p>{'h' * 300}</p>"), "h" * 300)

    def test_keeps_a_short_plain_part_when_html_adds_nothing(self):
        self.assertEqual(mailtext.best_text("Thanks", "<p>Hi</p>"), "Thanks")

    def test_html_only_messages_are_converted(self):
        self.assertEqual(mailtext.best_text("", "<p>Hi</p>"), "Hi")


class LimitTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(mailtext.limit("abc", 3), ("abc", False))

    def test_long_text_is_cut_and_reported(self):
        self.assertEqual(mailtext.limit("abcdef", 3), ("abc", True))

    def test_rejects_invalid_limits(self):
        for value in (0, -1, True, "5", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mailtext.limit("abc", value)

    def test_rejects_unknown_formats(self):
        with self.assertRaises(ValueError):
            mailtext.check_body_format("markdown")


if __name__ == "__main__":
    unittest.main()
