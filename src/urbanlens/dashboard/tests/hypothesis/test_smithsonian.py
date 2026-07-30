"""Tests for SmithsonianGateway - covers get_data, get_images_by_coordinates, and parse_response.

All HTTP calls are mocked so no real network access occurs.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.assets.smithsonian import SmithsonianGateway

_hyp = settings(max_examples=50, deadline=None)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gateway() -> SmithsonianGateway:
    """Return a SmithsonianGateway with a stub session (no real HTTP)."""
    session = MagicMock()
    return SmithsonianGateway(api_key="test-key", session=session)


def _make_si_response(rows: list[dict]) -> dict:
    """Build a minimal Smithsonian API JSON payload."""
    return {"response": {"rows": rows}}


def _make_row(title: str = "Test Image", content_url: str = "http://example.com/img.jpg", thumb_url: str = "http://example.com/thumb.jpg") -> dict:
    """Build a minimal row as returned by the Smithsonian API."""
    return {
        "title": title,
        "content": {
            "descriptiveNonRepeating": {
                "online_media": {
                    "media": [
                        {"content": content_url, "thumbnail": thumb_url},
                    ],
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------

class SmithsonianParseResponseTests(SimpleTestCase):
    """parse_response extracts title, url, and thumbnail from API data."""

    def setUp(self):
        self.gw = _gateway()

    def test_empty_response_returns_empty_list(self):
        result = self.gw.parse_response({})
        self.assertEqual(result, [])

    def test_empty_rows_returns_empty_list(self):
        result = self.gw.parse_response(_make_si_response([]))
        self.assertEqual(result, [])

    def test_single_row_title_extracted(self):
        data = _make_si_response([_make_row(title="Abandoned Mill")])
        result = self.gw.parse_response(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Abandoned Mill")

    def test_single_row_url_extracted(self):
        data = _make_si_response([_make_row(content_url="http://si.edu/img.jpg")])
        result = self.gw.parse_response(data)
        self.assertEqual(result[0]["url"], "http://si.edu/img.jpg")

    def test_single_row_thumbnail_extracted(self):
        data = _make_si_response([_make_row(thumb_url="http://si.edu/thumb.jpg")])
        result = self.gw.parse_response(data)
        self.assertEqual(result[0]["thumbnail"], "http://si.edu/thumb.jpg")

    def test_multiple_rows_all_returned(self):
        rows = [_make_row(title=f"Image {i}") for i in range(3)]
        data = _make_si_response(rows)
        result = self.gw.parse_response(data)
        self.assertEqual(len(result), 3)

    def test_row_missing_media_returns_none_url(self):
        row = {"title": "No Media", "content": {"descriptiveNonRepeating": {"online_media": {"media": []}}}}
        data = _make_si_response([row])
        result = self.gw.parse_response(data)
        self.assertIsNone(result[0]["url"])
        self.assertIsNone(result[0]["thumbnail"])

    def test_row_missing_content_key_returns_none_url(self):
        row = {"title": "Bare Row"}
        data = _make_si_response([row])
        result = self.gw.parse_response(data)
        self.assertIsNone(result[0]["url"])
        self.assertIsNone(result[0]["thumbnail"])

    def test_returns_list_of_dicts(self):
        data = _make_si_response([_make_row()])
        result = self.gw.parse_response(data)
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], dict)

    def test_each_dict_has_title_url_thumbnail_keys(self):
        data = _make_si_response([_make_row()])
        result = self.gw.parse_response(data)
        self.assertIn("title", result[0])
        self.assertIn("url", result[0])
        self.assertIn("thumbnail", result[0])

    @given(st.lists(st.text(min_size=1, max_size=50), min_size=0, max_size=10))
    @_hyp
    def test_returns_same_number_of_items_as_rows(self, titles):
        rows = [_make_row(title=t) for t in titles]
        data = _make_si_response(rows)
        result = self.gw.parse_response(data)
        self.assertEqual(len(result), len(titles))


# ---------------------------------------------------------------------------
# get_data - cache miss path
# ---------------------------------------------------------------------------

class SmithsonianGetDataCacheMissTests(SimpleTestCase):
    """get_data fetches from the API when the cache is empty."""

    def setUp(self):
        self.gw = _gateway()
        self.api_response = _make_si_response([_make_row(title="Cache Miss Result")])

        self.mock_resp = MagicMock()
        self.mock_resp.json.return_value = self.api_response
        self.mock_resp.raise_for_status.return_value = None

        self.gw.session.get.return_value = self.mock_resp

    def test_cache_miss_calls_api(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = None
            self.gw.get_data("abandoned mill")
            self.gw.session.get.assert_called_once()

    def test_cache_miss_stores_result_in_cache(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = None
            self.gw.get_data("abandoned mill")
            mock_cache.set.assert_called_once()
            args = mock_cache.set.call_args
            self.assertEqual(args[0][0], make_cache_key("smithsonian", "abandoned mill"))
            # TTL should be 86400 seconds (24 hours)
            self.assertEqual(args[0][2], 86400)

    def test_cache_miss_returns_parsed_images(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = None
            result = self.gw.get_data("abandoned mill")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["title"], "Cache Miss Result")

    def test_api_called_with_correct_params(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = None
            self.gw.get_data("steel factory")
            call_kwargs = self.gw.session.get.call_args
            params = call_kwargs[1]["params"]
            self.assertEqual(params["q"], "steel factory")
            self.assertEqual(params["api_key"], "test-key")
            # No images-only param: the live endpoint silently ignores the
            # bare `online_media_type` GET param, and every filtering syntax
            # that does work breaks quoted multi-phrase query relevance (see
            # get_data's comment). Filtering happens client-side instead.
            self.assertNotIn("online_media_type", params)

    def test_raises_for_status_is_called(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = None
            self.gw.get_data("test")
            self.mock_resp.raise_for_status.assert_called_once()


# ---------------------------------------------------------------------------
# get_data - cache hit path
# ---------------------------------------------------------------------------

class SmithsonianGetDataCacheHitTests(SimpleTestCase):
    """get_data returns the cached value and skips the HTTP call."""

    def setUp(self):
        self.gw = _gateway()
        self.cached_data = _make_si_response([_make_row(title="Cached Result")])

    def test_cache_hit_skips_api_call(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = self.cached_data
            self.gw.get_data("factory")
            self.gw.session.get.assert_not_called()

    def test_cache_hit_returns_parsed_data(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = self.cached_data
            result = self.gw.get_data("factory")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["title"], "Cached Result")

    def test_cache_hit_does_not_write_to_cache_again(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = self.cached_data
            self.gw.get_data("factory")
            mock_cache.set.assert_not_called()

    def test_cache_key_is_derived_from_search_term(self):
        with patch("urbanlens.dashboard.services.apis.assets.smithsonian.cache") as mock_cache:
            mock_cache.get.return_value = self.cached_data
            self.gw.get_data("unique_term_xyz")
            call_args = mock_cache.get.call_args
            self.assertEqual(call_args[0][0], make_cache_key("smithsonian", "unique_term_xyz"))


# ---------------------------------------------------------------------------
# get_images_by_coordinates
# ---------------------------------------------------------------------------

class SmithsonianGetImagesByCoordinatesTests(SimpleTestCase):
    """get_images_by_coordinates resolves coords to a place name then calls get_data."""

    def setUp(self):
        self.gw = _gateway()
        self.expected_images = [{"title": "Coord Result", "url": "http://x.com/img.jpg", "thumbnail": "http://x.com/t.jpg"}]

    def test_uses_google_geocoding_to_resolve_place_name(self):
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway") as MockGeocoder,
            patch.object(SmithsonianGateway, "get_data", return_value=self.expected_images),
        ):
            mock_geo_instance = MockGeocoder.return_value
            mock_geo_instance.get_place_name.return_value = "Old Factory, NY"

            self.gw.get_images_by_coordinates(40.7, -74.0)

            MockGeocoder.assert_called_once()
            mock_geo_instance.get_place_name.assert_called_once_with(40.7, -74.0)

    def test_passes_resolved_place_name_to_get_data(self):
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway") as MockGeocoder,
            patch.object(SmithsonianGateway, "get_data", return_value=self.expected_images) as mock_get_data,
        ):
            mock_geo_instance = MockGeocoder.return_value
            mock_geo_instance.get_place_name.return_value = "Old Factory, NY"

            self.gw.get_images_by_coordinates(40.7, -74.0)

            mock_get_data.assert_called_once_with("Old Factory, NY")

    def test_when_place_name_is_none_passes_empty_string(self):
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway") as MockGeocoder,
            patch.object(SmithsonianGateway, "get_data", return_value=[]) as mock_get_data,
        ):
            mock_geo_instance = MockGeocoder.return_value
            mock_geo_instance.get_place_name.return_value = None

            self.gw.get_images_by_coordinates(0.0, 0.0)

            mock_get_data.assert_called_once_with("")

    def test_returns_list_from_get_data(self):
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway") as MockGeocoder,
            patch.object(SmithsonianGateway, "get_data", return_value=self.expected_images),
        ):
            mock_geo_instance = MockGeocoder.return_value
            mock_geo_instance.get_place_name.return_value = "Some Place"

            result = self.gw.get_images_by_coordinates(41.0, -73.5)

            self.assertEqual(result, self.expected_images)

    def test_geocoder_instantiated_with_google_api_key(self):
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway") as MockGeocoder,
            patch("urbanlens.dashboard.services.apis.assets.smithsonian.settings") as mock_settings,
            patch.object(SmithsonianGateway, "get_data", return_value=[]),
        ):
            mock_settings.google_unrestricted_api_key = "google-key-xyz"
            mock_geo_instance = MockGeocoder.return_value
            mock_geo_instance.get_place_name.return_value = "Place"

            self.gw.get_images_by_coordinates(40.0, -74.0)

            MockGeocoder.assert_called_once_with(api_key="google-key-xyz")
