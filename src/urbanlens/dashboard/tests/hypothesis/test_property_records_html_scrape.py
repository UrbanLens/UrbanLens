"""Tests for the shared Tier 2/3 HTML scrape engine.

Covers: ScrapeRecipe's allowlist validation (SearchField/method), JSON
round-tripping for PropertyJurisdiction.scrape_recipe, the generic label/value
extraction strategies (table rows, definition lists, labeled-class pairs),
and execute_scrape_recipe's request/extraction wiring.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.property_records.html_scrape import (
    ScrapeRecipe,
    SearchField,
    execute_scrape_recipe,
    extract_label_value_pairs,
    recipe_from_dict,
    recipe_to_dict,
)


class ScrapeRecipeValidationTests(TestCase):
    def test_valid_recipe_constructs(self) -> None:
        recipe = ScrapeRecipe(base_url="https://example.gov/search", search_field=SearchField.SITUS_ADDRESS, param_name="addr")
        self.assertEqual(recipe.method, "GET")

    def test_unknown_search_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ScrapeRecipe(base_url="https://example.gov/search", search_field="owner_name", param_name="q")

    def test_unsupported_method_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ScrapeRecipe(base_url="https://example.gov/search", search_field=SearchField.APN, param_name="q", method="PUT")


class RecipeDictRoundTripTests(TestCase):
    def test_round_trips(self) -> None:
        recipe = ScrapeRecipe(base_url="https://example.gov/search", search_field=SearchField.APN, param_name="apn", method="POST", extra_params={"AppID": "1"})
        restored = recipe_from_dict(recipe_to_dict(recipe))
        self.assertEqual(restored, recipe)

    def test_empty_dict_is_none(self) -> None:
        self.assertIsNone(recipe_from_dict({}))

    def test_none_is_none(self) -> None:
        self.assertIsNone(recipe_from_dict(None))

    def test_missing_required_key_is_none(self) -> None:
        self.assertIsNone(recipe_from_dict({"base_url": "https://example.gov"}))

    def test_invalid_search_field_is_none(self) -> None:
        self.assertIsNone(recipe_from_dict({"base_url": "https://example.gov", "search_field": "owner_name", "param_name": "q"}))

    def test_non_string_extra_params_are_dropped_not_raised(self) -> None:
        parsed = recipe_from_dict({"base_url": "https://example.gov", "search_field": "apn", "param_name": "q", "extra_params": {"n": 5}})
        assert parsed is not None
        self.assertEqual(parsed.extra_params, {})


class ExtractLabelValuePairsTests(TestCase):
    def test_table_rows(self) -> None:
        html = "<html><body><table><tr><td>Owner Name</td><td>Jane Smith</td></tr></table></body></html>"
        self.assertEqual(extract_label_value_pairs(html)["Owner Name"], "Jane Smith")

    def test_table_header_cell_label(self) -> None:
        html = "<html><body><table><tr><th>APN</th><td>1-2-3</td></tr></table></body></html>"
        self.assertEqual(extract_label_value_pairs(html)["APN"], "1-2-3")

    def test_label_colon_suffix_is_stripped(self) -> None:
        html = "<html><body><table><tr><td>Owner:</td><td>Jane Smith</td></tr></table></body></html>"
        self.assertIn("Owner", extract_label_value_pairs(html))

    def test_three_cell_rows_are_ignored(self) -> None:
        html = "<html><body><table><tr><td>A</td><td>B</td><td>C</td></tr></table></body></html>"
        self.assertEqual(extract_label_value_pairs(html), {})

    def test_definition_list(self) -> None:
        html = "<html><body><dl><dt>Owner</dt><dd>Jane Smith</dd></dl></body></html>"
        self.assertEqual(extract_label_value_pairs(html)["Owner"], "Jane Smith")

    def test_multiple_definition_pairs_matched_in_order(self) -> None:
        html = "<html><body><dl><dt>Owner</dt><dd>Jane Smith</dd><dt>APN</dt><dd>1-2-3</dd></dl></body></html>"
        pairs = extract_label_value_pairs(html)
        self.assertEqual(pairs["Owner"], "Jane Smith")
        self.assertEqual(pairs["APN"], "1-2-3")

    def test_labeled_class_pairs(self) -> None:
        html = '<html><body><div><span class="label">Owner</span><span class="value">Jane Smith</span></div></body></html>'
        self.assertEqual(extract_label_value_pairs(html)["Owner"], "Jane Smith")

    def test_empty_html_yields_nothing(self) -> None:
        self.assertEqual(extract_label_value_pairs(""), {})

    def test_garbage_html_does_not_raise(self) -> None:
        extract_label_value_pairs("<<<not html at all>>>")

    def test_overlong_label_is_skipped(self) -> None:
        html = f"<html><body><table><tr><td>{'x' * 200}</td><td>value</td></tr></table></body></html>"
        self.assertEqual(extract_label_value_pairs(html), {})


def _mock_response(content: bytes, *, status_code: int = 200) -> mock.Mock:
    """A requests.Response stand-in with a real integer status_code (the engine compares/orders it)."""
    response = mock.Mock()
    response.status_code = status_code
    response.ok = status_code < 400
    response.encoding = "utf-8"
    response.iter_content.return_value = [content]
    return response


class ExecuteScrapeRecipeTests(TestCase):
    def _recipe(self, **overrides) -> ScrapeRecipe:
        defaults = {"base_url": "https://example.gov/search", "search_field": SearchField.SITUS_ADDRESS, "param_name": "addr"}
        defaults.update(overrides)
        return ScrapeRecipe(**defaults)

    def test_successful_get_extracts_fields(self) -> None:
        gateway = mock.Mock()
        gateway.session.get.return_value = _mock_response(b"<table><tr><td>Owner</td><td>Jane Smith</td></tr></table>")

        with (
            mock.patch("urbanlens.dashboard.services.apis.property_records.html_scrape._ScrapeGateway", return_value=gateway),
        ):
            result = execute_scrape_recipe(self._recipe(), situs_address="123 Main St")

        self.assertEqual(result["Owner"], "Jane Smith")
        called_params = gateway.session.get.call_args.kwargs["params"]
        self.assertEqual(called_params["addr"], "123 Main St")

    def test_post_method_sends_data_not_params(self) -> None:
        gateway = mock.Mock()
        gateway.session.post.return_value = _mock_response(b"<table><tr><td>APN</td><td>1-2-3</td></tr></table>")

        with (
            mock.patch("urbanlens.dashboard.services.apis.property_records.html_scrape._ScrapeGateway", return_value=gateway),
        ):
            result = execute_scrape_recipe(self._recipe(method="POST", search_field=SearchField.APN, param_name="apn"), apn="1-2-3")

        self.assertEqual(result["APN"], "1-2-3")
        gateway.session.post.assert_called_once()
        gateway.session.get.assert_not_called()
        self.assertEqual(gateway.session.post.call_args.kwargs["data"]["apn"], "1-2-3")

    def test_transport_failure_raises_source_unreachable_not_no_data(self) -> None:
        """An outage must be distinguishable from 'no data' so callers never negative-cache it."""
        import requests.exceptions

        from urbanlens.dashboard.services.apis.property_records.meta import SourceUnreachableError

        gateway = mock.Mock()
        gateway.session.get.side_effect = requests.exceptions.RequestException
        with (
            mock.patch("urbanlens.dashboard.services.apis.property_records.html_scrape._ScrapeGateway", return_value=gateway),
            self.assertRaises(SourceUnreachableError),
        ):
            execute_scrape_recipe(self._recipe(), situs_address="123 Main St")

    def test_server_error_raises_source_unreachable(self) -> None:
        from urbanlens.dashboard.services.apis.property_records.meta import SourceUnreachableError

        gateway = mock.Mock()
        gateway.session.get.return_value = _mock_response(b"", status_code=500)
        with (
            mock.patch("urbanlens.dashboard.services.apis.property_records.html_scrape._ScrapeGateway", return_value=gateway),
            self.assertRaises(SourceUnreachableError),
        ):
            execute_scrape_recipe(self._recipe(), situs_address="123 Main St")

    def test_client_error_is_no_data_not_an_outage(self) -> None:
        """A 404 means the recipe/page is wrong (cacheable), not that the county is down."""
        gateway = mock.Mock()
        gateway.session.get.return_value = _mock_response(b"", status_code=404)
        with (
            mock.patch("urbanlens.dashboard.services.apis.property_records.html_scrape._ScrapeGateway", return_value=gateway),
        ):
            result = execute_scrape_recipe(self._recipe(), situs_address="123 Main St")
        self.assertIsNone(result)

    def test_response_with_no_extractable_fields_returns_none(self) -> None:
        gateway = mock.Mock()
        gateway.session.get.return_value = _mock_response(b"<html><body>no data here</body></html>")
        with (
            mock.patch("urbanlens.dashboard.services.apis.property_records.html_scrape._ScrapeGateway", return_value=gateway),
        ):
            result = execute_scrape_recipe(self._recipe(), situs_address="123 Main St")
        self.assertIsNone(result)

    def test_extra_params_are_included_in_the_request(self) -> None:
        gateway = mock.Mock()
        gateway.session.get.return_value = _mock_response(b"<table><tr><td>Owner</td><td>Jane Smith</td></tr></table>")
        with (
            mock.patch("urbanlens.dashboard.services.apis.property_records.html_scrape._ScrapeGateway", return_value=gateway),
        ):
            execute_scrape_recipe(self._recipe(extra_params={"AppID": "42"}), situs_address="123 Main St")
        called_params = gateway.session.get.call_args.kwargs["params"]
        self.assertEqual(called_params["AppID"], "42")
