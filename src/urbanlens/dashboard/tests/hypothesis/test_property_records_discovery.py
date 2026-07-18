"""Tests for Tier 1 endpoint discovery's deterministic extraction and AI-output allowlisting.

Network-touching pieces (_validate_endpoint's live GET, search_web) are
mocked throughout - these tests exercise the parts that matter for safety and
correctness: URL extraction, .gov-first ranking, the SSRF guard, and the
strict "AI may only pick a URL already present in the search results" rule.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType
from urbanlens.dashboard.services.apis.property_records.discovery import (
    _extract_candidate_urls,
    _is_safe_public_url,
    _rank_candidates,
    _select_ai_candidate,
)


class ExtractCandidateUrlsTests(TestCase):
    def test_finds_a_mapserver_url(self) -> None:
        text = "Parcel data: https://gis.example.gov/arcgis/rest/services/Parcels/MapServer/2/query for details"
        candidates = _extract_candidate_urls(text)
        self.assertIn(("https://gis.example.gov/arcgis/rest/services/Parcels/MapServer/2", AdapterType.ARCGIS_REST), candidates)

    def test_finds_a_socrata_resource_url(self) -> None:
        text = "Open data: https://data.example.gov/resource/ab12-cd34.json is the endpoint"
        candidates = _extract_candidate_urls(text)
        self.assertIn(("https://data.example.gov/resource/ab12-cd34.json", AdapterType.SOCRATA), candidates)

    def test_no_match_returns_empty_list(self) -> None:
        self.assertEqual(_extract_candidate_urls("nothing relevant here"), [])

    def test_trailing_punctuation_is_stripped(self) -> None:
        text = "See https://gis.example.gov/arcgis/rest/services/Parcels/FeatureServer/0)."
        candidates = _extract_candidate_urls(text)
        self.assertEqual(candidates[0][0], "https://gis.example.gov/arcgis/rest/services/Parcels/FeatureServer/0")


class RankCandidatesTests(TestCase):
    def test_gov_domains_rank_first(self) -> None:
        candidates = [("https://example.com/MapServer/1", AdapterType.ARCGIS_REST), ("https://example.gov/MapServer/1", AdapterType.ARCGIS_REST)]
        ranked = _rank_candidates(candidates)
        self.assertTrue(ranked[0][0].endswith(".gov/MapServer/1"))

    def test_duplicates_are_removed(self) -> None:
        candidates = [("https://example.gov/MapServer/1", AdapterType.ARCGIS_REST), ("https://example.gov/MapServer/1", AdapterType.ARCGIS_REST)]
        self.assertEqual(len(_rank_candidates(candidates)), 1)

    def test_original_order_preserved_within_the_same_rank(self) -> None:
        candidates = [("https://a.gov/MapServer/1", AdapterType.ARCGIS_REST), ("https://b.gov/MapServer/1", AdapterType.ARCGIS_REST)]
        ranked = _rank_candidates(candidates)
        self.assertEqual([url for url, _ in ranked], ["https://a.gov/MapServer/1", "https://b.gov/MapServer/1"])


class IsSafePublicUrlTests(TestCase):
    def test_public_https_url_is_safe(self) -> None:
        self.assertTrue(_is_safe_public_url("https://example.gov/MapServer/1"))

    def test_loopback_ip_is_rejected(self) -> None:
        self.assertFalse(_is_safe_public_url("http://127.0.0.1/MapServer/1"))

    def test_localhost_hostname_is_rejected(self) -> None:
        self.assertFalse(_is_safe_public_url("http://localhost/MapServer/1"))

    def test_private_ip_is_rejected(self) -> None:
        self.assertFalse(_is_safe_public_url("http://10.0.0.5/MapServer/1"))

    def test_non_http_scheme_is_rejected(self) -> None:
        self.assertFalse(_is_safe_public_url("ftp://example.gov/MapServer/1"))


class SelectAiCandidateTests(TestCase):
    """The model may only ever pick a URL verbatim present in the search results - never invent one."""

    def _search_results(self):
        return [
            {"title": "Albany County GIS", "url": "https://gis.albanycounty.gov/arcgis/rest/services/Parcels/MapServer/2", "snippet": "Parcel data"},
            {"title": "Unrelated", "url": "https://example.com/blog", "snippet": "not relevant"},
        ]

    def test_no_search_results_returns_none_without_calling_ai(self) -> None:
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway") as get_gateway:
            result = _select_ai_candidate([])
        self.assertIsNone(result)
        get_gateway.assert_not_called()

    def test_ai_disabled_returns_none(self) -> None:
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=None):
            result = _select_ai_candidate(self._search_results())
        self.assertIsNone(result)

    def test_ai_picks_a_url_present_in_results(self) -> None:
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"url": "https://gis.albanycounty.gov/arcgis/rest/services/Parcels/MapServer/2", "kind": "arcgis"}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_candidate(self._search_results())
        self.assertEqual(result, ("https://gis.albanycounty.gov/arcgis/rest/services/Parcels/MapServer/2", AdapterType.ARCGIS_REST))

    def test_ai_inventing_a_url_not_in_results_is_rejected(self) -> None:
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"url": "https://not-a-real-result.example.com/MapServer/1", "kind": "arcgis"}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_candidate(self._search_results())
        self.assertIsNone(result)

    def test_ai_returning_null_url_is_none(self) -> None:
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"url": null, "kind": null}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_candidate(self._search_results())
        self.assertIsNone(result)

    def test_malformed_ai_json_does_not_raise(self) -> None:
        gateway = mock.Mock()
        gateway.send_prompt.return_value = "not json at all"
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_candidate(self._search_results())
        self.assertIsNone(result)

    def test_socrata_kind_is_recognized(self) -> None:
        results = [{"title": "Data", "url": "https://data.example.gov/resource/ab12-cd34.json", "snippet": ""}]
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"url": "https://data.example.gov/resource/ab12-cd34.json", "kind": "socrata"}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_candidate(results)
        self.assertEqual(result, ("https://data.example.gov/resource/ab12-cd34.json", AdapterType.SOCRATA))
