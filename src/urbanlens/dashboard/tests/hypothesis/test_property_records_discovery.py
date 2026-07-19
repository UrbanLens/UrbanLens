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
    _extract_forms,
    _is_safe_public_url,
    _rank_candidates,
    _select_ai_candidate,
    _select_ai_form_recipe,
    _validate_endpoint,
    apply_tier3_discovery,
    discover_tier3_recipe,
)
from urbanlens.dashboard.services.apis.property_records.html_scrape import SearchField


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


class ValidateEndpointTests(TestCase):
    """Only URLs the Tier 1 gateway can genuinely query may ever reach the registry."""

    _FETCH_JSON = "urbanlens.dashboard.services.apis.property_records.discovery._fetch_json"

    def test_arcgis_layer_with_fields_validates_as_itself(self) -> None:
        with mock.patch(self._FETCH_JSON, return_value={"name": "Parcels", "fields": [{"name": "APN"}], "type": "Feature Layer"}):
            result = _validate_endpoint("https://gis.example.gov/rest/services/Parcels/MapServer/2", AdapterType.ARCGIS_REST)
        self.assertEqual(result, "https://gis.example.gov/rest/services/Parcels/MapServer/2")

    def test_arcgis_layer_with_a_generic_name_but_enough_parcel_fields_validates(self) -> None:
        """No parcel-ish layer name, but the fields themselves are unambiguous (matches real Douglas County OR data: TAXID + LEGAL)."""
        body = {"name": "Layer7", "fields": [{"name": "TAXID"}, {"name": "LEGAL"}]}
        with mock.patch(self._FETCH_JSON, return_value=body):
            result = _validate_endpoint("https://gis.example.gov/rest/services/Common/MapServer/7", AdapterType.ARCGIS_REST)
        self.assertEqual(result, "https://gis.example.gov/rest/services/Common/MapServer/7")

    def test_arcgis_layer_with_unrelated_name_and_fields_is_rejected(self) -> None:
        """Regression guard: a real, responsive, well-formed ArcGIS layer that isn't parcel data
        (observed live: a Virginia county's public-schools sites layer) must not validate."""
        body = {"name": "LCPSSITES", "fields": [{"name": "SCH_CODE"}, {"name": "CLASS"}, {"name": "JURISDICTION"}]}
        with mock.patch(self._FETCH_JSON, return_value=body):
            result = _validate_endpoint("https://gis.example.gov/rest/services/Cloud/LCPSSITES/FeatureServer/50", AdapterType.ARCGIS_REST)
        self.assertIsNone(result)

    def test_arcgis_layer_with_only_one_matching_field_is_rejected(self) -> None:
        """A single coincidentally-matching field name isn't enough signal on its own."""
        body = {"name": "Layer7", "fields": [{"name": "NAME"}, {"name": "TAXID"}]}
        with mock.patch(self._FETCH_JSON, return_value=body):
            result = _validate_endpoint("https://gis.example.gov/rest/services/Common/MapServer/7", AdapterType.ARCGIS_REST)
        self.assertIsNone(result)

    def test_arcgis_service_root_is_refined_to_its_parcel_layer(self) -> None:
        """A bare .../MapServer describes the service but can't answer /query - saving it as-is
        would validate fine yet never return data. It must be refined to a queryable layer."""
        root_body = {"capabilities": "Map,Query", "layers": [{"id": 0, "name": "Roads"}, {"id": 2, "name": "Tax Parcels"}]}
        layer_body = {"fields": [{"name": "APN"}], "type": "Feature Layer"}

        def fake_fetch(url, params):
            if url.endswith("/MapServer"):
                return root_body
            if url.endswith("/MapServer/2"):
                return layer_body
            return {"error": {"code": 400}}

        with mock.patch(self._FETCH_JSON, side_effect=fake_fetch):
            result = _validate_endpoint("https://gis.example.gov/rest/services/Parcels/MapServer", AdapterType.ARCGIS_REST)
        self.assertEqual(result, "https://gis.example.gov/rest/services/Parcels/MapServer/2")

    def test_arcgis_service_root_with_no_parcel_layer_is_rejected(self) -> None:
        root_body = {"capabilities": "Map,Query", "layers": [{"id": 0, "name": "Roads"}, {"id": 1, "name": "Hydrology"}]}
        with mock.patch(self._FETCH_JSON, return_value=root_body):
            result = _validate_endpoint("https://gis.example.gov/rest/services/Base/MapServer", AdapterType.ARCGIS_REST)
        self.assertIsNone(result)

    def test_arcgis_error_body_is_rejected(self) -> None:
        with mock.patch(self._FETCH_JSON, return_value={"error": {"code": 400}}):
            self.assertIsNone(_validate_endpoint("https://gis.example.gov/rest/services/Parcels/MapServer/2", AdapterType.ARCGIS_REST))

    def test_unreachable_endpoint_is_rejected(self) -> None:
        with mock.patch(self._FETCH_JSON, return_value=None):
            self.assertIsNone(_validate_endpoint("https://gis.example.gov/rest/services/Parcels/MapServer/2", AdapterType.ARCGIS_REST))

    def test_socrata_list_response_validates(self) -> None:
        with mock.patch(self._FETCH_JSON, return_value=[{"apn": "1", "owner_name": "Jane Smith"}]):
            result = _validate_endpoint("https://data.example.gov/resource/ab12-cd34.json", AdapterType.SOCRATA)
        self.assertEqual(result, "https://data.example.gov/resource/ab12-cd34.json")

    def test_socrata_non_list_response_is_rejected(self) -> None:
        with mock.patch(self._FETCH_JSON, return_value={"error": True}):
            self.assertIsNone(_validate_endpoint("https://data.example.gov/resource/ab12-cd34.json", AdapterType.SOCRATA))

    def test_socrata_empty_result_set_is_rejected(self) -> None:
        """An empty list (dataset exists but nothing came back for this probe) isn't confirmation of anything."""
        with mock.patch(self._FETCH_JSON, return_value=[]):
            self.assertIsNone(_validate_endpoint("https://data.example.gov/resource/ab12-cd34.json", AdapterType.SOCRATA))

    def test_socrata_unrelated_dataset_is_rejected(self) -> None:
        with mock.patch(self._FETCH_JSON, return_value=[{"restaurant_name": "Joe's Diner", "grade": "A"}]):
            self.assertIsNone(_validate_endpoint("https://data.example.gov/resource/ab12-cd34.json", AdapterType.SOCRATA))

    def test_unsafe_url_is_rejected_without_any_request(self) -> None:
        with mock.patch(self._FETCH_JSON) as fetch:
            self.assertIsNone(_validate_endpoint("http://127.0.0.1/MapServer/1", AdapterType.ARCGIS_REST))
        fetch.assert_not_called()


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


class ExtractFormsTests(TestCase):
    def test_extracts_action_method_and_input_names(self) -> None:
        html = '<html><body><form action="/search" method="post"><input name="addr"><input name="apn"></form></body></html>'
        forms = _extract_forms(html, "https://example.gov/page")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["action"], "https://example.gov/search")
        self.assertEqual(forms[0]["method"], "POST")
        self.assertEqual(forms[0]["inputs"], ["addr", "apn"])

    def test_form_with_no_named_inputs_is_skipped(self) -> None:
        html = '<html><body><form action="/search"><input type="submit"></form></body></html>'
        self.assertEqual(_extract_forms(html, "https://example.gov/page"), [])

    def test_relative_action_resolved_against_base_url(self) -> None:
        html = '<html><body><form action="search.aspx"><input name="q"></form></body></html>'
        forms = _extract_forms(html, "https://example.gov/parcels/index.aspx")
        self.assertEqual(forms[0]["action"], "https://example.gov/parcels/search.aspx")

    def test_missing_method_defaults_to_get(self) -> None:
        html = '<html><body><form action="/search"><input name="q"></form></body></html>'
        self.assertEqual(_extract_forms(html, "https://example.gov")[0]["method"], "GET")

    def test_select_elements_count_as_named_inputs(self) -> None:
        html = '<html><body><form action="/search"><select name="kind"></select></form></body></html>'
        self.assertEqual(_extract_forms(html, "https://example.gov")[0]["inputs"], ["kind"])

    def test_garbage_html_does_not_raise(self) -> None:
        self.assertEqual(_extract_forms("<<<not html>>>", "https://example.gov"), [])

    def test_no_forms_yields_empty_list(self) -> None:
        self.assertEqual(_extract_forms("<html><body>no forms here</body></html>", "https://example.gov"), [])


class SelectAiFormRecipeTests(TestCase):
    """The model may only pick a form by index and a field name that genuinely exists on it."""

    def _forms(self):
        return [
            {"action": "https://example.gov/search", "method": "GET", "inputs": ["addr", "submit"], "html": '<form action="/search"><input name="addr"><input name="submit" type="submit"></form>'},
        ]

    def test_no_forms_returns_none_without_calling_ai(self) -> None:
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway") as get_gateway:
            result = _select_ai_form_recipe([], "https://example.gov")
        self.assertIsNone(result)
        get_gateway.assert_not_called()

    def test_ai_disabled_returns_none(self) -> None:
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=None):
            result = _select_ai_form_recipe(self._forms(), "https://example.gov")
        self.assertIsNone(result)

    def test_valid_proposal_builds_a_recipe(self) -> None:
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"form_index": 0, "search_field": "situs_address", "param_name": "addr"}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            recipe = _select_ai_form_recipe(self._forms(), "https://example.gov")
        assert recipe is not None
        self.assertEqual(recipe.base_url, "https://example.gov/search")
        self.assertEqual(recipe.search_field, SearchField.SITUS_ADDRESS)
        self.assertEqual(recipe.param_name, "addr")

    def test_hallucinated_field_name_not_on_the_real_form_is_rejected(self) -> None:
        """Regression guard: the model must be cross-checked against the form's
        real input names, not trusted outright - this is the core compliance
        guarantee this discovery path relies on."""
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"form_index": 0, "search_field": "situs_address", "param_name": "made_up_field_name"}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_form_recipe(self._forms(), "https://example.gov")
        self.assertIsNone(result)

    def test_out_of_range_form_index_is_rejected(self) -> None:
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"form_index": 5, "search_field": "situs_address", "param_name": "addr"}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_form_recipe(self._forms(), "https://example.gov")
        self.assertIsNone(result)

    def test_invalid_search_field_is_rejected(self) -> None:
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"form_index": 0, "search_field": "owner_name", "param_name": "addr"}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_form_recipe(self._forms(), "https://example.gov")
        self.assertIsNone(result)

    def test_null_form_index_is_rejected(self) -> None:
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"form_index": null}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_form_recipe(self._forms(), "https://example.gov")
        self.assertIsNone(result)

    def test_action_on_a_different_host_is_rejected(self) -> None:
        """A form action pointing at a different domain must never be trusted -
        even if the model didn't invent it, cross-host redirection is exactly
        the kind of thing this discovery path must not silently follow."""
        forms = [{"action": "https://attacker.example.com/search", "method": "GET", "inputs": ["addr"], "html": ""}]
        gateway = mock.Mock()
        gateway.send_prompt.return_value = '{"form_index": 0, "search_field": "situs_address", "param_name": "addr"}'
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            result = _select_ai_form_recipe(forms, "https://example.gov/page")
        self.assertIsNone(result)


class DiscoverTier3RecipeTests(TestCase):
    def _jurisdiction(self, **overrides):
        from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction

        defaults = {"fips": "36001", "county_name": "Albany County", "state": "NY"}
        defaults.update(overrides)
        return PropertyJurisdiction(**defaults)

    def test_no_assessor_url_returns_none_without_any_request(self) -> None:
        with mock.patch("requests.get") as get:
            result = discover_tier3_recipe(self._jurisdiction(assessor_url=""))
        self.assertIsNone(result)
        get.assert_not_called()


class ApplyTier3DiscoveryTests(TestCase):
    def test_saves_recipe_and_never_sets_last_verified(self) -> None:
        from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction
        from urbanlens.dashboard.services.apis.property_records.html_scrape import ScrapeRecipe

        jurisdiction = PropertyJurisdiction.objects.create(fips="36001", county_name="Albany County", state="NY")
        recipe = ScrapeRecipe(base_url="https://example.gov/search", search_field=SearchField.SITUS_ADDRESS, param_name="addr")

        apply_tier3_discovery(jurisdiction, recipe)
        jurisdiction.refresh_from_db()

        self.assertEqual(jurisdiction.scrape_recipe["base_url"], "https://example.gov/search")
        self.assertEqual(jurisdiction.adapter_type, AdapterType.CUSTOM_SCRAPER)
        self.assertIsNone(jurisdiction.last_verified)
        self.assertIn("NOT yet confirmed", jurisdiction.notes)
