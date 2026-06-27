"""Tests for shared GooglePlace cache rows and GooglePlaceService."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.google.place_info import GooglePlaceService


class GooglePlaceServiceTests(TestCase):
    """GooglePlaceService deduplicates API results by coordinate pair."""

    def test_same_coordinates_reuse_single_row(self) -> None:
        service = GooglePlaceService()
        first = service.get_or_create_for_coordinates("40.0", "-74.0", place_name="Steel Mill", fetch_if_missing=False)
        second = service.get_or_create_for_coordinates("40.0", "-74.0", place_name="Ignored", fetch_if_missing=False)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(GooglePlace.objects.count(), 1)

    def test_different_coordinates_create_separate_rows(self) -> None:
        service = GooglePlaceService()
        first = service.get_or_create_for_coordinates(40.0, -74.0, place_name="Mill A", fetch_if_missing=False)
        second = service.get_or_create_for_coordinates(41.0, -73.0, place_name="Mill B", fetch_if_missing=False)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(GooglePlace.objects.count(), 2)

    def test_location_and_pin_share_google_place_at_same_coordinates(self) -> None:
        google_place = GooglePlace.objects.create(
            latitude=Decimal("40.000000"),
            longitude=Decimal("-74.000000"),
            cached_place_name="Shared Place",
        )
        location = baker.make(Location, latitude="40.000000", longitude="-74.000000", google_place=google_place)
        pin = baker.make_recipe(
            "dashboard.pin",
            latitude="40.000000",
            longitude="-74.000000",
            google_place=google_place,
            location=location,
        )
        self.assertEqual(location.google_place_id, pin.google_place_id)
        self.assertEqual(location.place_name, pin.place_name)

    def test_set_cid_for_entity_links_and_stores_cid(self) -> None:
        location = baker.make(Location, latitude="40.000000", longitude="-74.000000", google_place=None)
        google_place = GooglePlaceService().set_cid_for_entity(location, 9876543210)
        location.refresh_from_db()
        self.assertEqual(location.google_place_id, google_place.pk)
        self.assertEqual(location.cid, Decimal(9876543210))

    def test_resolve_place_name_fetches_when_missing(self) -> None:
        google_place = GooglePlace.objects.create(latitude=Decimal("40.0"), longitude=Decimal("-74.0"))
        service = GooglePlaceService()
        with mock.patch.object(service, "_resolve_name", return_value="Resolved Mill") as mock_resolve:
            name = service.resolve_place_name(google_place)
        mock_resolve.assert_called_once()
        self.assertEqual(name, "Resolved Mill")
        google_place.refresh_from_db()
        self.assertEqual(google_place.cached_place_name, "Resolved Mill")
