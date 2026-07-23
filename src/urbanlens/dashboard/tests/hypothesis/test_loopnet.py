"""Tests for the LoopNet commercial-listings plugin.

Retrieval calls REData's parcel-uuid and listings endpoints (see the module
docstring in plugins.builtin.loopnet) - RedataGateway itself is mocked, so no
real network access occurs. Covers address gating, fetch()'s parcel-uuid ->
listings pipeline (and its graceful degradation when REData is unconfigured/
unavailable/has no parcel), and media_items() building proxy URLs for each
listing's photos.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.loopnet import LoopnetPanelSource, LoopnetPlugin
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway


def _make_profile():
    from urbanlens.dashboard.models.profile.model import Profile

    user = baker.make("auth.User")
    return Profile.objects.get(user=user)


_LISTING = {
    "uuid": "listing-1",
    "loopnet_url": "https://www.loopnet.com/Listing/123",
    "title": "123 Main St - Retail Building",
    "photos": [{"id": 1, "position": 0, "content_type": "image/jpeg"}, {"id": 2, "position": 1, "content_type": "image/jpeg"}],
}


class AddressTests(SimpleTestCase):
    def test_full_address_is_assembled(self) -> None:
        location = Location(street_number="123", route="Main St", locality="Anytown", administrative_area_level_1="ST")
        pin = Pin(location=location)
        self.assertEqual(LoopnetPanelSource.address(pin), "123 Main St, Anytown, ST")

    def test_no_route_yields_empty_string(self) -> None:
        location = Location(street_number="123", route="", locality="Anytown")
        pin = Pin(location=location)
        self.assertEqual(LoopnetPanelSource.address(pin), "")

    def test_no_location_yields_empty_string(self) -> None:
        # Pin.location is a non-nullable FK - Pin(location=None) would raise
        # accessing .location on an unsaved instance, so this uses a bare
        # duck-typed stand-in rather than a real (impossible) Pin state.
        stub_pin = SimpleNamespace(location=None)
        self.assertEqual(LoopnetPanelSource.address(stub_pin), "")


class GateTests(TestCase):
    def test_gate_true_with_an_address(self) -> None:
        location = baker.make(Location, street_number="123", route="Main St", google_place=None)
        pin = baker.make(Pin, profile=_make_profile(), location=location)
        self.assertTrue(LoopnetPanelSource().gate(pin))

    def test_gate_false_without_a_route(self) -> None:
        location = baker.make(Location, street_number="123", route="", google_place=None)
        pin = baker.make(Pin, profile=_make_profile(), location=location)
        self.assertFalse(LoopnetPanelSource().gate(pin))


class FetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="42.650000", longitude="-73.750000", street_number="123", route="Main St", locality="Anytown", google_place=None)
        self.pin = baker.make(Pin, profile=_make_profile(), location=self.location)

    def test_fetch_stores_listings_from_the_resolved_parcel(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1") as mock_uuid,
            patch.object(RedataGateway, "lookup_listings", return_value={"results": [_LISTING], "refresh_queued": False}) as mock_listings,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            LoopnetPanelSource().fetch(self.pin)

        mock_uuid.assert_called_once()
        mock_listings.assert_called_once_with("parcel-1")
        data = mock_set.call_args[0][2]
        self.assertEqual(data["listings"], [_LISTING])

    def test_no_parcel_found_persists_empty(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value=None),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            LoopnetPanelSource().fetch(self.pin)
        data = mock_set.call_args[0][2]
        self.assertEqual(data, {})

    def test_no_listings_persists_empty(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_listings", return_value={"results": [], "refresh_queued": True}),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            LoopnetPanelSource().fetch(self.pin)
        data = mock_set.call_args[0][2]
        self.assertEqual(data, {})

    def test_unavailable_gracefully_persists_empty(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", side_effect=PropertyRecordsUnavailableError("source_error", "boom")),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            LoopnetPanelSource().fetch(self.pin)
        data = mock_set.call_args[0][2]
        self.assertEqual(data, {})

    def test_unconfigured_gateway_gracefully_persists_empty(self) -> None:
        """RedataGateway() raises ValueError (not PropertyRecordsUnavailableError) when unconfigured."""
        with patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set:
            LoopnetPanelSource().fetch(self.pin)
        data = mock_set.call_args[0][2]
        self.assertEqual(data, {})

    def test_no_coordinates_persists_empty_without_calling_redata(self) -> None:
        # Location.latitude/longitude are non-nullable at the DB level, so this
        # (admittedly defensive-only, given the schema) branch is exercised
        # with a duck-typed stand-in rather than a real, impossible-to-persist Location.
        stub_location = SimpleNamespace(latitude=None, longitude=None)
        stub_pin = MagicMock(location=stub_location)
        with (
            patch.object(LoopnetPanelSource, "address", return_value="123 Main St"),
            patch.object(RedataGateway, "lookup_parcel_uuid") as mock_uuid,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            LoopnetPanelSource().fetch(stub_pin)
        mock_uuid.assert_not_called()
        data = mock_set.call_args[0][2]
        self.assertEqual(data, {})


class MediaItemsTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = LoopnetPanelSource()

    def test_builds_one_item_per_photo(self) -> None:
        items = self.source.media_items({"listings": [_LISTING]})
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].caption, "123 Main St - Retail Building")
        self.assertEqual(items[0].page_url, "https://www.loopnet.com/Listing/123")
        self.assertIn("listing-1", items[0].url)
        self.assertIn("/1/", items[0].url)
        self.assertIn("/2/", items[1].url)

    def test_no_listings_yields_no_items(self) -> None:
        self.assertEqual(self.source.media_items({}), [])

    def test_listing_without_uuid_is_skipped(self) -> None:
        listing = {**_LISTING, "uuid": None}
        self.assertEqual(self.source.media_items({"listings": [listing]}), [])

    def test_listing_without_photos_yields_no_items(self) -> None:
        listing = {**_LISTING, "photos": []}
        self.assertEqual(self.source.media_items({"listings": [listing]}), [])


class PluginContributionsTests(SimpleTestCase):
    def test_contributes_one_panel_source(self) -> None:
        sources = LoopnetPlugin().get_panel_sources()
        self.assertEqual([type(source) for source in sources], [LoopnetPanelSource])
