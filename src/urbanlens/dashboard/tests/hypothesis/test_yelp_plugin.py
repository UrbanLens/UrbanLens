"""Tests for the Yelp plugin's REData-backed panel source.

``YelpPanelSource`` now calls ``RedataPointsOfInterestGateway`` (``provider="yelp"``)
instead of the direct Yelp Fusion API - these tests mock that gateway and check
the LocationCache row / MediaItem list it produces, rather than any HTTP call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.plugins.builtin.yelp import YelpPanelSource, _business_from_poi

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

_GATEWAY_PATH = (
    "urbanlens.dashboard.services.apis.locations.redata_points_of_interest_gateway.RedataPointsOfInterestGateway"
)
_CONFIGURED_PATH = "urbanlens.dashboard.plugins.builtin.yelp.redata_configured"


class BusinessFromPoiTests(SimpleTestCase):
    def test_maps_the_documented_attributes(self) -> None:
        poi = {
            "name": "Joe's Diner",
            "url": "https://yelp.com/biz/joes-diner",
            "category": "Diner",
            "attributes": {"rating": 4.5, "review_count": 120, "price": "$$", "phone": "+15551234567"},
        }
        business = _business_from_poi(poi)
        self.assertEqual(business["name"], "Joe's Diner")
        self.assertEqual(business["url"], "https://yelp.com/biz/joes-diner")
        self.assertEqual(business["rating"], 4.5)
        self.assertEqual(business["review_count"], 120)
        self.assertEqual(business["price"], "$$")
        self.assertEqual(business["display_phone"], "+15551234567")
        self.assertEqual(business["categories"], [{"title": "Diner"}])

    def test_missing_attributes_do_not_raise(self) -> None:
        business = _business_from_poi({"name": "Bare Bones"})
        self.assertEqual(business["name"], "Bare Bones")
        self.assertIsNone(business["rating"])
        self.assertEqual(business["photos"], [])
        self.assertIsNone(business["image_url"])
        self.assertEqual(business["categories"], [])

    def test_photos_populate_image_url_from_the_first_entry(self) -> None:
        poi = {
            "name": "Photogenic Place",
            "attributes": {"photos": ["https://example.test/1.jpg", "https://example.test/2.jpg"]},
        }
        business = _business_from_poi(poi)
        self.assertEqual(business["photos"], ["https://example.test/1.jpg", "https://example.test/2.jpg"])
        self.assertEqual(business["image_url"], "https://example.test/1.jpg")


class YelpPanelSourceGateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = YelpPanelSource()

    def _pin(self, *, latitude: float, longitude: float) -> Pin:
        location: Location = baker.make("dashboard.Location", latitude=latitude, longitude=longitude)
        return baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_requires_redata_configured(self) -> None:
        pin = self._pin(latitude=40.0, longitude=-74.0)
        with mock.patch(_CONFIGURED_PATH, return_value=False):
            self.assertFalse(self.source.gate(pin))

    def test_requires_coordinates(self) -> None:
        """``Location.latitude``/``longitude`` are NOT NULL - (0.0, 0.0) ("null island") is
        how a pin with no real coordinates yet reads through ``effective_latitude``/
        ``effective_longitude``, which is what the ``bool(lat and lng)`` check catches."""
        pin = self._pin(latitude=0.0, longitude=0.0)
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertFalse(self.source.gate(pin))

    def test_passes_with_redata_configured_and_coordinates(self) -> None:
        pin = self._pin(latitude=40.0, longitude=-74.0)
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertTrue(self.source.gate(pin))


class YelpPanelSourceFetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = YelpPanelSource()
        self.location: Location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)

    def _cached(self) -> dict | None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        row = LocationCache.objects.filter(location=self.location, source="yelp").first()
        return row.data if row else None

    def test_caches_the_first_result_as_a_business(self) -> None:
        poi = {"name": "Joe's Diner", "url": "https://yelp.com/biz/joes-diner", "attributes": {"rating": 4.5}}
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = [poi]
            self.source.fetch(self.pin)

        data = self._cached()
        assert data is not None
        self.assertEqual(data["business"]["name"], "Joe's Diner")
        self.assertEqual(data["reviews"], [])
        mock_gateway_cls.return_value.find_near.assert_called_once_with(40.0, -74.0, provider="yelp")

    def test_caches_an_empty_dict_when_nothing_is_found(self) -> None:
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = []
            self.source.fetch(self.pin)

        self.assertEqual(self._cached(), {})


class YelpPanelSourceMediaItemsTests(SimpleTestCase):
    def test_builds_a_media_item_per_photo(self) -> None:
        source = YelpPanelSource()
        data = {
            "business": {
                "name": "Joe's Diner",
                "url": "https://yelp.com/biz/joes-diner",
                "photos": ["https://example.test/1.jpg"],
            }
        }
        items = source.media_items(data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://example.test/1.jpg")
        self.assertEqual(items[0].caption, "Joe's Diner")
        self.assertEqual(items[0].source, "Yelp")

    def test_no_photos_yields_no_items(self) -> None:
        source = YelpPanelSource()
        self.assertEqual(source.media_items({"business": {"name": "Photo-less Place"}}), [])

    def test_empty_data_yields_no_items(self) -> None:
        source = YelpPanelSource()
        self.assertEqual(source.media_items({}), [])
