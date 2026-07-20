"""Tests for WikipediaGateway's address-verification matching.

``get_article_for_location`` must only accept a geosearch candidate when
there's a genuine positive signal that it's specifically about the queried
place - proximity alone (which is all ``list=geosearch`` guarantees) is not
enough. These tests pin down ``_address_matches``'s stricter rejection
behavior: a nearby candidate with no title/name match and no address mention
in its extract must be rejected, not guessed at.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway, WikipediaMediaGateway, _absolute_media_url

_COMPONENTS = {"locality": "Poughkeepsie", "route": "Main St", "street_number": "103", "administrative_area_level_1": "NY"}


class AddressMatchesTests(SimpleTestCase):
    """WikipediaGateway._address_matches()."""

    def test_title_matching_the_place_name_is_accepted(self) -> None:
        summary = {"title": "Hudson River Psychiatric Center", "extract": ""}
        self.assertTrue(WikipediaGateway._address_matches(summary, {}, name="Hudson River Psychiatric Center"))

    def test_partial_title_match_is_accepted(self) -> None:
        summary = {"title": "Hudson River Psychiatric Center", "extract": ""}
        self.assertTrue(WikipediaGateway._address_matches(summary, {}, name="Hudson River"))

    def test_locality_mentioned_in_extract_is_accepted(self) -> None:
        summary = {"title": "Some Building", "extract": "A building located in Poughkeepsie, New York."}
        self.assertTrue(WikipediaGateway._address_matches(summary, _COMPONENTS, name=""))

    def test_route_mentioned_in_extract_is_accepted(self) -> None:
        summary = {"title": "Some Building", "extract": "Located on Main St in a small town."}
        self.assertTrue(WikipediaGateway._address_matches(summary, _COMPONENTS, name=""))

    def test_street_number_mentioned_in_extract_is_accepted(self) -> None:
        summary = {"title": "Some Building", "extract": "The building at 103 was constructed in 1900."}
        self.assertTrue(WikipediaGateway._address_matches(summary, _COMPONENTS, name=""))

    def test_no_extract_and_no_title_match_is_rejected(self) -> None:
        """A candidate with nothing to verify against must not be accepted on faith."""
        summary = {"title": "Unrelated Article", "extract": ""}
        self.assertFalse(WikipediaGateway._address_matches(summary, _COMPONENTS, name="The Actual Place"))

    def test_short_stub_extract_with_no_address_mention_is_rejected(self) -> None:
        """A merely nearby stub article must not be accepted just for being short."""
        summary = {"title": "Unrelated Stub", "extract": "A short article about something else entirely."}
        self.assertFalse(WikipediaGateway._address_matches(summary, _COMPONENTS, name="The Actual Place"))

    def test_long_extract_with_no_matching_signal_is_rejected(self) -> None:
        summary = {"title": "Unrelated Article", "extract": "A very long article about an entirely different place, " * 20}
        self.assertFalse(WikipediaGateway._address_matches(summary, _COMPONENTS, name="The Actual Place"))

    def test_no_components_and_no_name_is_rejected(self) -> None:
        summary = {"title": "Some Article", "extract": "Some content."}
        self.assertFalse(WikipediaGateway._address_matches(summary, {}, name=""))


class GetArticleForLocationTests(SimpleTestCase):
    """WikipediaGateway.get_article_for_location() end-to-end candidate selection."""

    def setUp(self) -> None:
        super().setUp()
        self.gateway = WikipediaGateway()

    def test_rejects_the_only_candidate_when_it_has_no_matching_signal(self) -> None:
        """A geographically close but otherwise unrelated article must not be returned."""
        with (
            mock.patch.object(WikipediaGateway, "_geo_search", return_value=[{"title": "Nearby Unrelated Place"}]),
            mock.patch.object(WikipediaGateway, "_fetch_summary", return_value={"title": "Nearby Unrelated Place", "extract": "Some other place entirely."}),
        ):
            result = self.gateway.get_article_for_location(40.0, -74.0, _COMPONENTS, name="The Actual Place")
        self.assertIsNone(result)

    def test_accepts_a_candidate_whose_extract_mentions_the_address(self) -> None:
        with (
            mock.patch.object(WikipediaGateway, "_geo_search", return_value=[{"title": "The Actual Place"}]),
            mock.patch.object(
                WikipediaGateway,
                "_fetch_summary",
                return_value={"title": "The Actual Place", "extract": "Located in Poughkeepsie.", "extract_html": "<p>Located in Poughkeepsie.</p>"},
            ),
            mock.patch.object(WikipediaGateway, "_fill_full_extract"),
            mock.patch.object(WikipediaGateway, "_fetch_infobox", return_value=[]),
        ):
            result = self.gateway.get_article_for_location(40.0, -74.0, _COMPONENTS, name="The Actual Place")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["title"], "The Actual Place")
        self.assertEqual(result["infobox"], [])

    def test_skips_a_rejected_candidate_and_accepts_the_next_matching_one(self) -> None:
        candidates = [{"title": "Wrong Nearby Article"}, {"title": "The Actual Place"}]
        summaries = {
            "Wrong Nearby Article": {"title": "Wrong Nearby Article", "extract": "Something unrelated."},
            "The Actual Place": {"title": "The Actual Place", "extract": "Located in Poughkeepsie.", "extract_html": "<p>Located in Poughkeepsie.</p>"},
        }
        with (
            mock.patch.object(WikipediaGateway, "_geo_search", return_value=candidates),
            mock.patch.object(WikipediaGateway, "_fetch_summary", side_effect=lambda title: summaries[title]),
            mock.patch.object(WikipediaGateway, "_fill_full_extract"),
            mock.patch.object(WikipediaGateway, "_fetch_infobox", return_value=[]),
        ):
            result = self.gateway.get_article_for_location(40.0, -74.0, _COMPONENTS, name="The Actual Place")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["title"], "The Actual Place")

    def test_matched_candidate_gets_its_infobox_fetched(self) -> None:
        with (
            mock.patch.object(WikipediaGateway, "_geo_search", return_value=[{"title": "The Actual Place"}]),
            mock.patch.object(
                WikipediaGateway,
                "_fetch_summary",
                return_value={"title": "The Actual Place", "extract": "Located in Poughkeepsie.", "extract_html": "<p>Located in Poughkeepsie.</p>"},
            ),
            mock.patch.object(WikipediaGateway, "_fill_full_extract"),
            mock.patch.object(WikipediaGateway, "_fetch_infobox", return_value=[["Established", "1900"]]) as fetch_infobox,
        ):
            result = self.gateway.get_article_for_location(40.0, -74.0, _COMPONENTS, name="The Actual Place")
        fetch_infobox.assert_called_once_with("The Actual Place")
        assert result is not None
        self.assertEqual(result["infobox"], [["Established", "1900"]])


class AbsoluteMediaUrlTests(SimpleTestCase):
    """_absolute_media_url()."""

    def test_protocol_relative_url_gets_https_prefix(self) -> None:
        self.assertEqual(_absolute_media_url("//upload.wikimedia.org/x.jpg"), "https://upload.wikimedia.org/x.jpg")

    def test_already_absolute_url_is_unchanged(self) -> None:
        self.assertEqual(_absolute_media_url("https://upload.wikimedia.org/x.jpg"), "https://upload.wikimedia.org/x.jpg")

    def test_empty_string_is_unchanged(self) -> None:
        self.assertEqual(_absolute_media_url(""), "")


class GetArticleMediaTests(SimpleTestCase):
    """WikipediaGateway.get_article_media() - reads the article's own curated media list.

    This exists specifically because a Wikimedia Commons text search (see
    WikimediaGateway) can miss images that are only reachable through an
    in-body gallery and aren't independently discoverable by name - see
    docs/prompts/completed.md's "Wikipedia article images not reliably
    reaching Media section" entry.
    """

    def setUp(self) -> None:
        super().setUp()
        self.gateway = WikipediaGateway()

    @staticmethod
    def _response(status_code: int = 200, payload: dict | None = None) -> mock.Mock:
        resp = mock.Mock()
        resp.status_code = status_code
        resp.json.return_value = payload or {}
        resp.raise_for_status = mock.Mock()
        return resp

    def test_returns_image_items_with_absolute_urls(self) -> None:
        payload = {
            "items": [
                {
                    "title": "File:Example.jpg",
                    "type": "image",
                    "srcset": [
                        {"src": "//upload.wikimedia.org/thumb/500px-Example.jpg", "scale": "1x"},
                        {"src": "//upload.wikimedia.org/thumb/1280px-Example.jpg", "scale": "2x"},
                    ],
                },
            ],
        }
        with mock.patch.object(self.gateway.session, "get", return_value=self._response(payload=payload)):
            media = self.gateway.get_article_media("Example Article")
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["title"], "Example.jpg")
        self.assertEqual(media[0]["thumb_url"], "https://upload.wikimedia.org/thumb/500px-Example.jpg")
        self.assertEqual(media[0]["url"], "https://upload.wikimedia.org/thumb/1280px-Example.jpg")

    def test_non_image_items_are_skipped(self) -> None:
        payload = {"items": [{"title": "File:Anthem.ogg", "type": "audio", "srcset": [{"src": "//upload.wikimedia.org/anthem.ogg"}]}]}
        with mock.patch.object(self.gateway.session, "get", return_value=self._response(payload=payload)):
            media = self.gateway.get_article_media("Example Article")
        self.assertEqual(media, [])

    def test_items_with_no_srcset_are_skipped(self) -> None:
        payload = {"items": [{"title": "File:Example.jpg", "type": "image", "srcset": []}]}
        with mock.patch.object(self.gateway.session, "get", return_value=self._response(payload=payload)):
            media = self.gateway.get_article_media("Example Article")
        self.assertEqual(media, [])

    def test_404_returns_empty_list(self) -> None:
        with mock.patch.object(self.gateway.session, "get", return_value=self._response(status_code=404)):
            media = self.gateway.get_article_media("No Such Article")
        self.assertEqual(media, [])

    def test_request_failure_returns_empty_list(self) -> None:
        with mock.patch.object(self.gateway.session, "get", side_effect=ConnectionError("boom")):
            media = self.gateway.get_article_media("Example Article")
        self.assertEqual(media, [])


class WikipediaMediaGatewayTests(SimpleTestCase):
    """WikipediaMediaGateway._generate_media() - the MediaProvider wrapper around get_article_media."""

    def test_yields_media_items_for_the_article(self) -> None:
        gateway = WikipediaMediaGateway()
        with mock.patch.object(
            WikipediaGateway,
            "get_article_media",
            return_value=[{"title": "Example.jpg", "url": "https://upload.wikimedia.org/full.jpg", "thumb_url": "https://upload.wikimedia.org/thumb.jpg"}],
        ):
            items = list(gateway._generate_media("Example Article"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://upload.wikimedia.org/full.jpg")
        self.assertEqual(items[0].caption, "Example.jpg")
        self.assertEqual(items[0].page_url, "https://commons.wikimedia.org/wiki/File:Example.jpg")

    def test_empty_search_term_yields_nothing(self) -> None:
        gateway = WikipediaMediaGateway()
        self.assertEqual(list(gateway._generate_media("")), [])

    def test_skips_urls_already_known_from_wikimedia(self) -> None:
        """The dedup guard against WikimediaPlugin's Commons text-search results."""
        gateway = WikipediaMediaGateway(known_urls=frozenset({"https://upload.wikimedia.org/dup.jpg"}))
        with mock.patch.object(
            WikipediaGateway,
            "get_article_media",
            return_value=[
                {"title": "Dup.jpg", "url": "https://upload.wikimedia.org/dup.jpg", "thumb_url": "https://upload.wikimedia.org/dup-thumb.jpg"},
                {"title": "New.jpg", "url": "https://upload.wikimedia.org/new.jpg", "thumb_url": "https://upload.wikimedia.org/new-thumb.jpg"},
            ],
        ):
            items = list(gateway._generate_media("Example Article"))
        self.assertEqual([item.url for item in items], ["https://upload.wikimedia.org/new.jpg"])


class FetchInfoboxTests(SimpleTestCase):
    """WikipediaGateway._fetch_infobox() - regression coverage for the
    "started from Wikipedia" seed missing the infobox" report
    (docs/prompts/completed.md).

    _fetch_summary/_fetch_extended_extract are both backed by the
    TextExtracts extension, which strips infoboxes before returning "extract"
    text - _fetch_infobox instead parses action=parse's real rendered HTML,
    which is the only Wikipedia response that carries the infobox table.
    """

    def setUp(self) -> None:
        super().setUp()
        self.gateway = WikipediaGateway()

    @staticmethod
    def _response(payload: dict) -> mock.Mock:
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.json.return_value = payload
        return resp

    # A trimmed version of the "New St. Joseph Cemetery" infobox from the
    # original bug report: a title row (th only, no td), an image/map row
    # (td only, no th), a section-divider row ("Details", th only), then
    # real label/value fact rows - including one with inline markup
    # (a <span> around a non-breaking space in "Owned by") and a linked label
    # ("Find a Grave") to confirm text_content() flattens both correctly.
    _INFOBOX_HTML = """
    <table class="infobox vcard">
    <tbody>
    <tr><th colspan="2" class="infobox-above">New St. Joseph Cemetery</th></tr>
    <tr><td colspan="2" class="infobox-image"><a><img src="map.png"></a></td></tr>
    <tr><th colspan="2" class="infobox-header">Details</th></tr>
    <tr><th class="infobox-label">Established</th><td class="infobox-data">1843/1853</td></tr>
    <tr><th class="infobox-label">Country</th><td class="infobox-data">US</td></tr>
    <tr><th class="infobox-label">Owned<span>&nbsp;</span>by</th><td class="infobox-data">St. Joseph New Cemetery Association</td></tr>
    <tr><th class="infobox-label"><a href="/wiki/Find_a_Grave">Find a Grave</a></th><td class="infobox-data"><a href="https://example.com">New St. Joseph Cemetery</a></td></tr>
    </tbody>
    </table>
    """

    def test_extracts_label_value_pairs_skipping_title_map_and_header_rows(self) -> None:
        payload = {"parse": {"text": f"<div>{self._INFOBOX_HTML}</div>"}}
        with mock.patch.object(self.gateway.session, "get", return_value=self._response(payload)):
            pairs = self.gateway._fetch_infobox("New St. Joseph Cemetery")
        self.assertEqual(
            pairs,
            [
                ["Established", "1843/1853"],
                ["Country", "US"],
                ["Owned by", "St. Joseph New Cemetery Association"],
                ["Find a Grave", "New St. Joseph Cemetery"],
            ],
        )

    def test_no_infobox_table_returns_empty_list(self) -> None:
        payload = {"parse": {"text": "<div><p>No infobox here.</p></div>"}}
        with mock.patch.object(self.gateway.session, "get", return_value=self._response(payload)):
            self.assertEqual(self.gateway._fetch_infobox("Some Article"), [])

    def test_empty_parse_text_returns_empty_list(self) -> None:
        with mock.patch.object(self.gateway.session, "get", return_value=self._response({"parse": {"text": ""}})):
            self.assertEqual(self.gateway._fetch_infobox("Some Article"), [])

    def test_request_failure_returns_empty_list(self) -> None:
        with mock.patch.object(self.gateway.session, "get", side_effect=ConnectionError("boom")):
            self.assertEqual(self.gateway._fetch_infobox("Some Article"), [])

    def test_row_count_is_capped(self) -> None:
        rows = "".join(f'<tr><th class="infobox-label">Field {i}</th><td class="infobox-data">Value {i}</td></tr>' for i in range(50))
        payload = {"parse": {"text": f'<div><table class="infobox">{rows}</table></div>'}}
        with mock.patch.object(self.gateway.session, "get", return_value=self._response(payload)):
            pairs = self.gateway._fetch_infobox("Some Article")
        self.assertEqual(len(pairs), 20)
