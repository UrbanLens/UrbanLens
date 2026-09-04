"""Tests for services.apis.locations.places_resolution's provider dispatch.

Covers the REData-vs-Google-Places choice for each of the seven call sites
this module centralizes, mirroring test_cid_resolution.py's structure for the
analogous CID-resolution chokepoint.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import places_resolution
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings


@contextmanager
def _redata_configured():
    with (
        mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
        mock.patch.object(settings, "redata_api_key", "test-key"),
    ):
        yield


@contextmanager
def _redata_not_configured():
    with mock.patch.object(settings, "redata_api_url", None), mock.patch.object(settings, "redata_api_key", None):
        yield


class SearchNearbyLandmarksTests(SimpleTestCase):
    def test_redata_configured_reshapes_into_new_api_shape(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.search_nearby.return_value = [
                {
                    "place_id": "p1",
                    "name": "Old Mill",
                    "latitude": 1.0,
                    "longitude": 2.0,
                    "formatted_address": "123 Main St",
                    "types": ["historical_landmark"],
                }
            ]
            result = places_resolution.search_nearby_landmarks(1.0, 2.0, 500, ["historical_landmark"], api_key="key")

        self.assertEqual(
            result,
            [
                {
                    "id": "p1",
                    "displayName": {"text": "Old Mill"},
                    "location": {"latitude": 1.0, "longitude": 2.0},
                    "shortFormattedAddress": "123 Main St",
                    "types": ["historical_landmark"],
                    "rating": None,
                    "userRatingCount": None,
                }
            ],
        )

    def test_not_configured_calls_google_directly(self) -> None:
        with (
            _redata_not_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.search_nearby.return_value = [{"id": "p1", "rating": 4.5}]
            result = places_resolution.search_nearby_landmarks(1.0, 2.0, 500, ["historical_landmark"], api_key="key")

        self.assertEqual(result, [{"id": "p1", "rating": 4.5}])
        gw_cls.assert_called_once_with(api_key="key")
        gw_cls.return_value.search_nearby.assert_called_once_with(
            1.0, 2.0, radius=500, included_types=["historical_landmark"]
        )


class GetPlaceDetailsFullTests(SimpleTestCase):
    def test_redata_configured_nulls_out_enterprise_tier_fields(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.get_place.return_value = {
                "name": "Sydney Opera House",
                "formatted_address": "Bennelong Point",
                "google_maps_uri": "https://maps.google.com/?cid=1",
            }
            result = places_resolution.get_place_details_full("p1", api_key="key")

        self.assertEqual(
            result,
            {
                "name": "Sydney Opera House",
                "formatted_address": "Bennelong Point",
                "rating": None,
                "editorial_summary": None,
                "opening_hours": None,
                "website": None,
                "url": "https://maps.google.com/?cid=1",
                "photos": [],
            },
        )

    def test_redata_configured_place_not_found_returns_empty_dict(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.get_place.return_value = None
            self.assertEqual(places_resolution.get_place_details_full("missing", api_key="key"), {})

    def test_redata_error_propagates_uncaught(self) -> None:
        """No caching-of-transient-failure: the caller's own except-block (which
        skips caching on failure) must still see this, not a swallowed {}."""
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.get_place.side_effect = GatewayRequestError("boom")
            with pytest.raises(GatewayRequestError):
                places_resolution.get_place_details_full("p1", api_key="key")

    def test_not_configured_requests_the_full_legacy_field_list(self) -> None:
        with (
            _redata_not_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.get_place_details.return_value = {"name": "x"}
            result = places_resolution.get_place_details_full("p1", api_key="key")

        self.assertEqual(result, {"name": "x"})
        gw_cls.return_value.get_place_details.assert_called_once_with(
            "p1",
            fields=[
                "name",
                "formatted_address",
                "rating",
                "editorial_summary",
                "opening_hours",
                "website",
                "url",
                "photos",
            ],
        )


class FindNearestPlacePhotosTests(SimpleTestCase):
    def test_redata_configured_builds_composite_identifiers(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.search_nearby.return_value = [{"place_id": "p1"}]
            gw_cls.return_value.get_place.return_value = {"photo_records": [{"id": 5}, {"id": 6}]}
            place_id, identifiers = places_resolution.find_nearest_place_photos(1.0, 2.0, api_key="key")

        self.assertEqual(place_id, "p1")
        self.assertEqual(identifiers, ["redata:p1:5", "redata:p1:6"])

    def test_redata_configured_no_nearby_place_returns_none(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.search_nearby.return_value = []
            self.assertEqual(places_resolution.find_nearest_place_photos(1.0, 2.0, api_key="key"), (None, []))

    def test_redata_error_propagates_uncaught_for_panel_framework_retry(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.search_nearby.side_effect = GatewayRequestError("boom")
            with pytest.raises(GatewayRequestError):
                places_resolution.find_nearest_place_photos(1.0, 2.0, api_key="key")

    def test_not_configured_calls_google_directly(self) -> None:
        with (
            _redata_not_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.find_nearest_place_id.return_value = "p1"
            gw_cls.return_value.get_place_photo_names.return_value = ["places/p1/photos/abc"]
            result = places_resolution.find_nearest_place_photos(1.0, 2.0, api_key="key")

        self.assertEqual(result, ("p1", ["places/p1/photos/abc"]))


class DownloadPhotoTests(SimpleTestCase):
    def test_redata_prefixed_identifier_dispatches_to_redata(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls:
            gw_cls.return_value.download_photo.return_value = (b"bytes", "image/jpeg")
            result = places_resolution.download_photo("redata:p1:5", api_key="key")

        self.assertEqual(result, (b"bytes", "image/jpeg"))
        gw_cls.return_value.download_photo.assert_called_once_with("p1", 5)

    def test_redata_confirmed_gone_raises_photo_not_found(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls:
            gw_cls.return_value.download_photo.return_value = None
            with pytest.raises(places_resolution.PhotoNotFoundError):
                places_resolution.download_photo("redata:p1:5", api_key="key")

    def test_plain_identifier_dispatches_to_google(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway") as gw_cls:
            gw_cls.return_value.get_photo_media.return_value = (b"bytes", "image/jpeg")
            result = places_resolution.download_photo("places/p1/photos/abc", api_key="key")

        self.assertEqual(result, (b"bytes", "image/jpeg"))
        gw_cls.return_value.get_photo_media.assert_called_once_with("places/p1/photos/abc")

    def test_dispatch_ignores_current_redata_configuration(self) -> None:
        """An already-persisted identifier must be honored by its own prefix,
        even if REData is (or isn't) configured right now."""
        with (
            _redata_not_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.download_photo.return_value = (b"bytes", "image/jpeg")
            places_resolution.download_photo("redata:p1:5", api_key="key")

        gw_cls.return_value.download_photo.assert_called_once_with("p1", 5)


class AutocompletePredictionsTests(SimpleTestCase):
    def test_redata_configured_normalizes_flat_shape(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.autocomplete.return_value = [
                {"kind": "place", "place_id": "p1", "main_text": "Sydney Opera House", "secondary_text": "Sydney NSW"}
            ]
            result = places_resolution.autocomplete_predictions("sydney op", api_key="key")

        self.assertEqual(
            result, [{"place_id": "p1", "main_text": "Sydney Opera House", "secondary_text": "Sydney NSW"}]
        )

    def test_not_configured_normalizes_legacy_structured_formatting(self) -> None:
        with (
            _redata_not_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.autocomplete.return_value = [
                {
                    "place_id": "p1",
                    "description": "fallback",
                    "structured_formatting": {"main_text": "Sydney Opera House", "secondary_text": "Sydney NSW"},
                }
            ]
            result = places_resolution.autocomplete_predictions("sydney op", api_key="key")

        self.assertEqual(
            result, [{"place_id": "p1", "main_text": "Sydney Opera House", "secondary_text": "Sydney NSW"}]
        )


class ResolvePlaceCoordinatesTests(SimpleTestCase):
    def test_redata_configured_reads_flat_lat_lng(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.get_place.return_value = {"latitude": 1.5, "longitude": 2.5, "name": "Old Mill"}
            result = places_resolution.resolve_place_coordinates("p1", api_key="key")

        self.assertEqual(result, (1.5, 2.5, "Old Mill"))

    def test_redata_configured_place_not_found(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.get_place.return_value = None
            self.assertEqual(places_resolution.resolve_place_coordinates("p1", api_key="key"), (None, None, None))

    def test_not_configured_reads_legacy_nested_geometry(self) -> None:
        with (
            _redata_not_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.get_place_details.return_value = {
                "geometry": {"location": {"lat": 1.5, "lng": 2.5}},
                "name": "Old Mill",
            }
            result = places_resolution.resolve_place_coordinates("p1", api_key="key")

        self.assertEqual(result, (1.5, 2.5, "Old Mill"))
        gw_cls.return_value.get_place_details.assert_called_once_with("p1", fields=["geometry", "name"])


class ResolveNameFromNearbyTests(SimpleTestCase):
    def test_redata_configured_skips_locality_only_results(self) -> None:
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.search_nearby.return_value = [
                {"name": "Poughkeepsie", "types": ["locality", "political"]},
                {"name": "College Hill Golf Course", "types": ["golf_course", "point_of_interest"]},
            ]
            self.assertEqual(
                places_resolution.resolve_name_from_nearby(1.0, 2.0, 50, api_key="key"), "College Hill Golf Course"
            )

    def test_redata_error_is_swallowed_to_none(self) -> None:
        """Unlike most other functions here, this one must swallow - the caller's
        own except clause is narrower than GatewayRequestError."""
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.RedataPlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.search_nearby.side_effect = GatewayRequestError("boom")
            self.assertIsNone(places_resolution.resolve_name_from_nearby(1.0, 2.0, 50, api_key="key"))

    def test_not_configured_with_no_api_key_returns_none_without_calling_google(self) -> None:
        with (
            _redata_not_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway") as gw_cls,
        ):
            self.assertIsNone(places_resolution.resolve_name_from_nearby(1.0, 2.0, 50, api_key=""))
            gw_cls.assert_not_called()

    def test_not_configured_calls_google_get_data(self) -> None:
        with (
            _redata_not_configured(),
            mock.patch("urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway") as gw_cls,
        ):
            gw_cls.return_value.get_data.return_value = [{"name": "College Hill Golf Course", "types": ["golf_course"]}]
            self.assertEqual(
                places_resolution.resolve_name_from_nearby(1.0, 2.0, 50, api_key="key"), "College Hill Golf Course"
            )
