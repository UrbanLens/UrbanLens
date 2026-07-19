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
from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

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
            mock.patch.object(WikipediaGateway, "_fill_short_extract"),
        ):
            result = self.gateway.get_article_for_location(40.0, -74.0, _COMPONENTS, name="The Actual Place")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["title"], "The Actual Place")

    def test_skips_a_rejected_candidate_and_accepts_the_next_matching_one(self) -> None:
        candidates = [{"title": "Wrong Nearby Article"}, {"title": "The Actual Place"}]
        summaries = {
            "Wrong Nearby Article": {"title": "Wrong Nearby Article", "extract": "Something unrelated."},
            "The Actual Place": {"title": "The Actual Place", "extract": "Located in Poughkeepsie.", "extract_html": "<p>Located in Poughkeepsie.</p>"},
        }
        with (
            mock.patch.object(WikipediaGateway, "_geo_search", return_value=candidates),
            mock.patch.object(WikipediaGateway, "_fetch_summary", side_effect=lambda title: summaries[title]),
            mock.patch.object(WikipediaGateway, "_fill_short_extract"),
        ):
            result = self.gateway.get_article_for_location(40.0, -74.0, _COMPONENTS, name="The Actual Place")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["title"], "The Actual Place")
