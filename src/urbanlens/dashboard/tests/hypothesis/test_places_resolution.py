"""Tests for services.apis.locations.places_resolution's provider dispatch."""

from __future__ import annotations

from typing import Any, ClassVar
from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.points import PLACES_PROVIDERS
from urbanlens.dashboard.services.apis.locations import places_resolution
from urbanlens.dashboard.services.apis.locations.places_resolution import GooglePlacesProvider, PlacesProvider
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, ProviderUnavailableError
from urbanlens.UrbanLens.settings.app import settings

_GOOGLE_GATEWAY = "urbanlens.dashboard.services.apis.locations.places_resolution.GooglePlacesGateway"


class _FakePlaces(PlacesProvider):
    """A provider whose every answer is canned; it remembers the calls it was asked."""

    shields_direct: ClassVar[bool] = False

    def __init__(self, key: str, *, is_available: bool = True, photo_prefix: str | None = None) -> None:
        self.key = key
        self.is_available = is_available
        self.photo_prefix = photo_prefix
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.failure: Exception | None = None

    def available(self) -> bool:
        return self.is_available

    def service_keys(self) -> tuple[str, ...]:
        return (f"{self.key}_places",)

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.failure is not None:
            raise self.failure

    def search_nearby_landmarks(
        self, latitude: float, longitude: float, radius: float, included_types: list[str]
    ) -> list[dict[str, Any]]:
        self._record("search_nearby_landmarks", latitude, longitude, radius, included_types)
        return [{"id": self.key}]

    def get_place_details_full(self, place_id: str) -> dict[str, Any]:
        self._record("get_place_details_full", place_id)
        return {"name": self.key}

    def find_nearest_place_photos(self, latitude: float, longitude: float) -> tuple[str | None, list[str]]:
        self._record("find_nearest_place_photos", latitude, longitude)
        return self.key, [f"{self.photo_prefix or ''}photo"]

    def owns_photo(self, photo_identifier: str) -> bool:
        return self.photo_prefix is not None and photo_identifier.startswith(self.photo_prefix)

    def download_photo(self, photo_identifier: str) -> tuple[bytes, str]:
        self._record("download_photo", photo_identifier)
        return self.key.encode(), "image/jpeg"

    def autocomplete_predictions(self, query: str) -> list[dict[str, str]]:
        self._record("autocomplete_predictions", query)
        return [{"place_id": self.key, "main_text": query, "secondary_text": ""}]

    def resolve_place_coordinates(self, place_id: str) -> tuple[float | None, float | None, str | None]:
        self._record("resolve_place_coordinates", place_id)
        return 1.0, 2.0, self.key

    def resolve_name_from_nearby(self, latitude: float, longitude: float, radius: float) -> str | None:
        self._record("resolve_name_from_nearby", latitude, longitude, radius)
        return self.key


class ProviderChoiceTests(SimpleTestCase):
    def test_the_first_available_provider_answers_every_call(self) -> None:
        skipped = _FakePlaces("skipped", is_available=False)
        first = _FakePlaces("first")
        second = _FakePlaces("second")
        with PLACES_PROVIDERS.overridden([skipped, first, second]):
            self.assertEqual(places_resolution.search_nearby_landmarks(1.0, 2.0, 500, ["park"]), [{"id": "first"}])
            self.assertEqual(places_resolution.get_place_details_full("p1"), {"name": "first"})
            self.assertEqual(places_resolution.find_nearest_place_photos(1.0, 2.0), ("first", ["photo"]))
            self.assertEqual(places_resolution.autocomplete_predictions("sy")[0]["place_id"], "first")
            self.assertEqual(places_resolution.resolve_place_coordinates("p1"), (1.0, 2.0, "first"))
            self.assertEqual(places_resolution.resolve_name_from_nearby(1.0, 2.0, 50), "first")

        self.assertEqual(skipped.calls, [])
        self.assertEqual(second.calls, [])

    def test_a_failure_is_not_handed_to_the_next_provider(self) -> None:
        failing = _FakePlaces("failing")
        failing.failure = GatewayRequestError("boom")
        fallback = _FakePlaces("fallback")
        with PLACES_PROVIDERS.overridden([failing, fallback]), pytest.raises(GatewayRequestError):
            places_resolution.get_place_details_full("p1")

        self.assertEqual(fallback.calls, [])

    def test_identity_helpers_follow_the_active_provider(self) -> None:
        with PLACES_PROVIDERS.overridden([_FakePlaces("off", is_available=False), _FakePlaces("on")]):
            self.assertTrue(places_resolution.available())
            self.assertEqual(places_resolution.active_provider(), "on")
            self.assertEqual(places_resolution.service_keys(), ("on_places",))
            self.assertFalse(places_resolution.shields_direct())

    def test_shields_direct_reports_the_active_providers_flag(self) -> None:
        shielding = _FakePlaces("shielding")
        shielding.shields_direct = True
        with PLACES_PROVIDERS.overridden([shielding]):
            self.assertTrue(places_resolution.shields_direct())

    def test_with_no_available_provider_nothing_is_available_and_lookups_raise(self) -> None:
        with PLACES_PROVIDERS.overridden([_FakePlaces("off", is_available=False)]):
            self.assertFalse(places_resolution.available())
            self.assertEqual(places_resolution.active_provider(), "")
            self.assertEqual(places_resolution.service_keys(), ())
            self.assertFalse(places_resolution.shields_direct())
            with pytest.raises(ProviderUnavailableError):
                places_resolution.get_place_details_full("p1")
            with pytest.raises(ProviderUnavailableError):
                places_resolution.autocomplete_predictions("sy")

    def test_the_nearby_name_lookup_is_none_without_a_provider(self) -> None:
        with PLACES_PROVIDERS.overridden([]):
            self.assertIsNone(places_resolution.resolve_name_from_nearby(1.0, 2.0, 50))

    def test_a_request_failure_in_the_nearby_name_lookup_is_swallowed_to_none(self) -> None:
        """The caller's own except clause is narrower than GatewayRequestError."""
        failing = _FakePlaces("failing")
        failing.failure = GatewayRequestError("boom")
        with PLACES_PROVIDERS.overridden([failing]):
            self.assertIsNone(places_resolution.resolve_name_from_nearby(1.0, 2.0, 50))


class DownloadPhotoTests(SimpleTestCase):
    def test_the_provider_that_owns_the_identifier_downloads_it_whatever_is_available_now(self) -> None:
        """A persisted identifier is honoured by its own prefix, even when its provider is not the active one."""
        owner = _FakePlaces("owner", is_available=False, photo_prefix="own:")
        active = _FakePlaces("active", photo_prefix="act:")
        with PLACES_PROVIDERS.overridden([active, owner]):
            result = places_resolution.download_photo("own:p1:5")

        self.assertEqual(result, (b"owner", "image/jpeg"))
        self.assertEqual(active.calls, [])

    def test_no_owner_raises_photo_not_found(self) -> None:
        with (
            PLACES_PROVIDERS.overridden([_FakePlaces("a", photo_prefix="a:")]),
            pytest.raises(places_resolution.PhotoNotFoundError),
        ):
            places_resolution.download_photo("unknown/photo")


class GooglePlacesProviderTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(settings, "google_unrestricted_api_key", "key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_is_available_only_with_the_unrestricted_key(self) -> None:
        self.assertTrue(GooglePlacesProvider().available())
        with mock.patch.object(settings, "google_unrestricted_api_key", None):
            self.assertFalse(GooglePlacesProvider().available())

    def test_nearby_landmarks_are_passed_through_in_places_api_new_shape(self) -> None:
        with PLACES_PROVIDERS.overridden([GooglePlacesProvider()]), mock.patch(_GOOGLE_GATEWAY) as gw_cls:
            gw_cls.return_value.search_nearby.return_value = [{"id": "p1", "rating": 4.5}]
            result = places_resolution.search_nearby_landmarks(1.0, 2.0, 500, ["historical_landmark"])

        self.assertEqual(result, [{"id": "p1", "rating": 4.5}])
        gw_cls.assert_called_once_with(api_key="key")
        gw_cls.return_value.search_nearby.assert_called_once_with(
            1.0, 2.0, radius=500, included_types=["historical_landmark"]
        )

    def test_place_details_request_the_full_legacy_field_list(self) -> None:
        with PLACES_PROVIDERS.overridden([GooglePlacesProvider()]), mock.patch(_GOOGLE_GATEWAY) as gw_cls:
            gw_cls.return_value.get_place_details.return_value = {"name": "x"}
            result = places_resolution.get_place_details_full("p1")

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

    def test_the_nearest_place_and_its_photo_names(self) -> None:
        with PLACES_PROVIDERS.overridden([GooglePlacesProvider()]), mock.patch(_GOOGLE_GATEWAY) as gw_cls:
            gw_cls.return_value.find_nearest_place_id.return_value = "p1"
            gw_cls.return_value.get_place_photo_names.return_value = ["places/p1/photos/abc"]
            result = places_resolution.find_nearest_place_photos(1.0, 2.0)

        self.assertEqual(result, ("p1", ["places/p1/photos/abc"]))

    def test_it_owns_every_photo_identifier_and_downloads_by_resource_name(self) -> None:
        provider = GooglePlacesProvider()
        self.assertTrue(provider.owns_photo("places/p1/photos/abc"))
        with PLACES_PROVIDERS.overridden([provider]), mock.patch(_GOOGLE_GATEWAY) as gw_cls:
            gw_cls.return_value.get_photo_media.return_value = (b"bytes", "image/jpeg")
            result = places_resolution.download_photo("places/p1/photos/abc")

        self.assertEqual(result, (b"bytes", "image/jpeg"))
        gw_cls.return_value.get_photo_media.assert_called_once_with("places/p1/photos/abc")

    def test_autocomplete_normalizes_legacy_structured_formatting(self) -> None:
        with PLACES_PROVIDERS.overridden([GooglePlacesProvider()]), mock.patch(_GOOGLE_GATEWAY) as gw_cls:
            gw_cls.return_value.autocomplete.return_value = [
                {
                    "place_id": "p1",
                    "description": "fallback",
                    "structured_formatting": {"main_text": "Sydney Opera House", "secondary_text": "Sydney NSW"},
                }
            ]
            result = places_resolution.autocomplete_predictions("sydney op")

        self.assertEqual(
            result, [{"place_id": "p1", "main_text": "Sydney Opera House", "secondary_text": "Sydney NSW"}]
        )

    def test_coordinates_read_the_legacy_nested_geometry_asking_for_nothing_more(self) -> None:
        with PLACES_PROVIDERS.overridden([GooglePlacesProvider()]), mock.patch(_GOOGLE_GATEWAY) as gw_cls:
            gw_cls.return_value.get_place_details.return_value = {
                "geometry": {"location": {"lat": 1.5, "lng": 2.5}},
                "name": "Old Mill",
            }
            result = places_resolution.resolve_place_coordinates("p1")

        self.assertEqual(result, (1.5, 2.5, "Old Mill"))
        gw_cls.return_value.get_place_details.assert_called_once_with("p1", fields=["geometry", "name"])

    def test_the_nearby_name_skips_locality_only_results(self) -> None:
        with PLACES_PROVIDERS.overridden([GooglePlacesProvider()]), mock.patch(_GOOGLE_GATEWAY) as gw_cls:
            gw_cls.return_value.get_data.return_value = [
                {"name": "Poughkeepsie", "types": ["locality", "political"]},
                {"name": "College Hill Golf Course", "types": ["golf_course", "point_of_interest"]},
            ]
            self.assertEqual(places_resolution.resolve_name_from_nearby(1.0, 2.0, 50), "College Hill Golf Course")

    def test_the_nearby_name_without_a_key_is_none_and_calls_nothing(self) -> None:
        with (
            PLACES_PROVIDERS.overridden([GooglePlacesProvider()]),
            mock.patch.object(settings, "google_unrestricted_api_key", None),
            mock.patch(_GOOGLE_GATEWAY) as gw_cls,
        ):
            self.assertIsNone(GooglePlacesProvider().resolve_name_from_nearby(1.0, 2.0, 50))

        gw_cls.assert_not_called()
