"""Tests for services.import_formats.html_description: strip_html, extract_image_urls, extract_link_urls."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.import_formats.html_description import extract_image_urls, extract_link_urls, strip_html


class StripHtmlTests(SimpleTestCase):
    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(strip_html(""), "")

    def test_none_returns_none(self) -> None:
        self.assertIsNone(strip_html(None))

    def test_plain_text_is_unchanged(self) -> None:
        self.assertEqual(strip_html("Just plain text"), "Just plain text")

    def test_br_becomes_newline(self) -> None:
        self.assertEqual(strip_html("City: Poughkeepsie<br>State: NY"), "City: Poughkeepsie\nState: NY")

    def test_br_self_closing_variants_are_handled(self) -> None:
        for variant in ("<br>", "<br/>", "<br />", "<BR>"):
            with self.subTest(variant=variant):
                self.assertEqual(strip_html(f"a{variant}b"), "a\nb")

    def test_other_tags_are_removed(self) -> None:
        self.assertEqual(strip_html('<img src="x.jpg" height="200">Caption'), "Caption")

    def test_realistic_kmz_description(self) -> None:
        html = (
            '<img src="https://mymaps.usercontent.google.com/hostedimage/x" height="200" width="auto" />'
            "<br><br>City: Poughkeepsie<br>State: NY<br>Status: Under Rehab"
        )
        result = strip_html(html)
        self.assertNotIn("<img", result)
        self.assertIn("City: Poughkeepsie", result)
        self.assertIn("State: NY", result)


class ExtractImageUrlsTests(SimpleTestCase):
    def test_no_images_returns_empty_list(self) -> None:
        self.assertEqual(extract_image_urls("no images here"), [])

    def test_finds_single_image(self) -> None:
        urls = extract_image_urls('<img src="https://example.com/a.jpg" height="200">')
        self.assertEqual(urls, ["https://example.com/a.jpg"])

    def test_finds_multiple_images_in_order(self) -> None:
        html = '<img src="https://example.com/a.jpg"><p>text</p><img src="https://example.com/b.jpg">'
        self.assertEqual(extract_image_urls(html), ["https://example.com/a.jpg", "https://example.com/b.jpg"])

    def test_deduplicates(self) -> None:
        html = '<img src="https://example.com/a.jpg"><img src="https://example.com/a.jpg">'
        self.assertEqual(extract_image_urls(html), ["https://example.com/a.jpg"])


class ExtractLinkUrlsTests(SimpleTestCase):
    def test_finds_anchor_href(self) -> None:
        urls = extract_link_urls('<a href="https://example.com/story">Read more</a>')
        self.assertEqual(urls, ["https://example.com/story"])

    def test_finds_bare_url_in_plain_text(self) -> None:
        urls = extract_link_urls("Tour: https://www.poughkeepsiejournal.com/story/news/local/2021")
        self.assertEqual(urls, ["https://www.poughkeepsiejournal.com/story/news/local/2021"])

    def test_bare_url_does_not_swallow_trailing_punctuation(self) -> None:
        urls = extract_link_urls("See https://example.com/page.")
        self.assertEqual(urls, ["https://example.com/page"])

    def test_image_urls_are_excluded(self) -> None:
        html = '<img src="https://example.com/photo.jpg" height="200">Tour: https://example.com/story'
        urls = extract_link_urls(html)
        self.assertNotIn("https://example.com/photo.jpg", urls)
        self.assertIn("https://example.com/story", urls)

    def test_deduplicates_anchor_and_bare_occurrence(self) -> None:
        html = '<a href="https://example.com/x">link</a> also see https://example.com/x'
        self.assertEqual(extract_link_urls(html), ["https://example.com/x"])

    def test_no_links_returns_empty_list(self) -> None:
        self.assertEqual(extract_link_urls("no links here"), [])
