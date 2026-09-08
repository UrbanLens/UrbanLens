"""Tests for the REData place-details plugin's panel and enrichment source.

``RedataPlaceDetailsPanelSource``/``RedataPlaceDetailsEnrichmentSource`` both
read ``RedataCidGateway.get_place_detail`` - these tests mock that gateway
and check the ``LocationCache`` row/API payload it produces, never any real
HTTP call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.plugins.builtin.redata_place_details import (
    RedataPlaceDetailsEnrichmentSource,
    RedataPlaceDetailsPanelSource,
)
from urbanlens.dashboard.services.core.gateway import GatewayRequestError

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway.RedataCidGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.plugins.builtin.redata_place_details.redata_configured"


def _location_with_cid(cid: int | None, *, latitude: str = "40.7", longitude: str = "-74.0") -> Location:
    """A Location whose linked GooglePlace row carries ``cid`` (or none, when ``cid`` is None)."""
    google_place = GooglePlace.objects.create(latitude=latitude, longitude=longitude, cid=cid)
    return baker.make("dashboard.Location", latitude=latitude, longitude=longitude, google_place=google_place)


def _location_without_google_place(*, latitude: str = "40.7", longitude: str = "-74.0") -> Location:
    return baker.make("dashboard.Location", latitude=latitude, longitude=longitude, google_place=None)


class RedataPlaceDetailsPanelSourceGateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = RedataPlaceDetailsPanelSource()

    def _pin_for(self, location: Location) -> Pin:
        return baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_requires_redata_configured(self) -> None:
        pin = self._pin_for(_location_with_cid(123456789012345678))
        with mock.patch(_CONFIGURED_PATH, return_value=False):
            self.assertFalse(self.source.gate(pin))
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertTrue(self.source.gate(pin))

    def test_requires_a_linked_cid(self) -> None:
        pin = self._pin_for(_location_without_google_place())
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertFalse(self.source.gate(pin))

    def test_a_google_place_with_no_cid_still_fails_the_gate(self) -> None:
        """Linked to Google, but never CID-resolved - nothing for this panel to read."""
        pin = self._pin_for(_location_with_cid(None))
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertFalse(self.source.gate(pin))


class RedataPlaceDetailsPanelSourceFetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = RedataPlaceDetailsPanelSource()

    def _cached(self, location: Location) -> dict | None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        row = LocationCache.objects.filter(location=location, source="redata_place_details").first()
        return row.data if row else None

    def test_caches_the_place_detail_payload(self) -> None:
        location = _location_with_cid(123456789012345678)
        pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)
        detail = {"cid": 123456789012345678, "name": "Katz's Delicatessen", "rating": 4.6}
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.get_place_detail.return_value = detail
            self.source.fetch(pin)

        self.assertEqual(self._cached(location), detail)
        mock_gateway_cls.return_value.get_place_detail.assert_called_once_with(123456789012345678)

    def test_caches_an_empty_dict_when_redata_has_never_resolved_this_cid(self) -> None:
        """get_place_detail's 404 -> None case."""
        location = _location_with_cid(123456789012345678)
        pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.get_place_detail.return_value = None
            self.source.fetch(pin)

        self.assertEqual(self._cached(location), {})

    def test_caches_an_empty_dict_and_skips_the_gateway_when_the_location_has_no_cid(self) -> None:
        location = _location_without_google_place()
        pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            self.source.fetch(pin)

        self.assertEqual(self._cached(location), {})
        mock_gateway_cls.return_value.get_place_detail.assert_not_called()


class RedataPlaceDetailsPanelSourceApiPayloadTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        self.source = RedataPlaceDetailsPanelSource()
        self.location = _location_with_cid(123456789012345678)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)
        self.LocationCache = LocationCache

    def _cache(self, data: dict) -> None:
        self.LocationCache.set(self.location, "redata_place_details", data, query_key="123456789012345678")

    def test_never_fetched_returns_none(self) -> None:
        self.assertIsNone(self.source.api_payload(self.pin))

    def test_404_case_yields_no_panel(self) -> None:
        """An empty cached row (this CID was never resolved by REData) renders no card at all."""
        self._cache({})

        self.assertIsNone(self.source.api_payload(self.pin))

    def test_full_record_renders_every_compact_field(self) -> None:
        self._cache(
            {
                "cid": 123456789012345678,
                "name": "Katz's Delicatessen",
                "category": "Delicatessen",
                "rating": 4.6,
                "review_count": 15234,
                "price_level": "$$",
                "hours": {"summary": "Open 24 hours", "monday": "8 AM to 11 PM"},
                "phone_number": "(212) 254-2246",
                "website": "https://katzsdelicatessen.com/",
                "scrape_pending": False,
                "media": [],
            }
        )

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        info = payload["info"]
        self.assertEqual(info["heading_name"], "Katz's Delicatessen")
        self.assertEqual(info["chips"], ["Delicatessen"])
        meta = {row["label"]: row["value"] for row in info["meta"]}
        self.assertEqual(meta["Rating"], "4.6 (15,234 reviews)")
        self.assertEqual(meta["Price"], "$$")
        self.assertEqual(meta["Hours"], "Open 24 hours")
        self.assertEqual(meta["Phone"], "(212) 254-2246")
        self.assertEqual(info["footer_link"], {"url": "https://katzsdelicatessen.com/", "label": "Visit website"})
        self.assertEqual(payload["media"], [])

    def test_scrape_pending_still_renders_from_whatever_has_landed(self) -> None:
        """The deep scrape hasn't run yet - only the name is known - but that's still a real card."""
        self._cache({"cid": 123456789012345678, "name": "Katz's Delicatessen", "scrape_pending": True})

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        self.assertEqual(payload["info"]["heading_name"], "Katz's Delicatessen")
        self.assertEqual(payload["info"]["meta"], [])
        self.assertEqual(payload["info"]["chips"], [])
        self.assertIsNone(payload["info"]["footer_link"])
        self.assertEqual(payload["media"], [])

    def test_only_photo_kind_media_is_included_up_to_three(self) -> None:
        self._cache(
            {
                "cid": 123456789012345678,
                "name": "Katz's Delicatessen",
                "media": [
                    {"id": 1, "kind": "photo", "content_type": "image/jpeg"},
                    {"id": 2, "kind": "video", "content_type": "video/mp4"},
                    {"id": 3, "kind": "photo", "content_type": "image/jpeg"},
                    {"id": 4, "kind": "photo", "content_type": "image/jpeg"},
                    {"id": 5, "kind": "photo", "content_type": "image/jpeg"},
                ],
            }
        )

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        media = payload["media"]
        self.assertEqual(len(media), 3)
        expected_urls = {reverse("pin.place_cid.media", args=[123456789012345678, media_id]) for media_id in (1, 3, 4)}
        self.assertEqual({item["url"] for item in media}, expected_urls)

    def test_a_row_with_no_name_at_all_is_no_panel_even_with_other_fields(self) -> None:
        """A row this plugin wrote before REData had a name yet - not worth a card."""
        self._cache({"cid": 123456789012345678, "rating": 4.6})

        self.assertIsNone(self.source.api_payload(self.pin))


class RedataPlaceDetailsEnrichmentSourceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = RedataPlaceDetailsEnrichmentSource()

    def test_gate_requires_redata_configured(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False):
            self.assertFalse(self.source.gate())
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertTrue(self.source.gate())

    def test_fetch_returns_none_and_skips_the_gateway_when_the_location_has_no_cid(self) -> None:
        location = _location_without_google_place()
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            data, query_key = self.source.fetch(location)

        self.assertIsNone(data)
        self.assertEqual(query_key, "")
        mock_gateway_cls.return_value.get_place_detail.assert_not_called()

    def test_fetch_returns_the_detail_payload_and_query_key(self) -> None:
        location = _location_with_cid(123456789012345678)
        detail = {"cid": 123456789012345678, "name": "Katz's Delicatessen"}
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.get_place_detail.return_value = detail
            data, query_key = self.source.fetch(location)

        self.assertEqual(data, detail)
        self.assertEqual(query_key, "123456789012345678")
        mock_gateway_cls.return_value.get_place_detail.assert_called_once_with(123456789012345678)

    def test_fetch_swallows_a_transient_gateway_failure_as_nothing_found(self) -> None:
        location = _location_with_cid(123456789012345678)
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.get_place_detail.side_effect = GatewayRequestError("REData unreachable")
            data, query_key = self.source.fetch(location)

        self.assertIsNone(data)
        self.assertEqual(query_key, "123456789012345678")

    def test_missing_filter_excludes_locations_without_a_cid(self) -> None:
        from urbanlens.dashboard.models.location.model import Location

        with_cid = _location_with_cid(123456789012345678, latitude="10.0", longitude="10.0")
        without_cid = _location_without_google_place(latitude="20.0", longitude="20.0")

        matches = set(Location.objects.filter(self.source.missing_filter()).values_list("pk", flat=True))

        self.assertIn(with_cid.pk, matches)
        self.assertNotIn(without_cid.pk, matches)

    def test_missing_filter_excludes_locations_already_cached(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.models.location.model import Location

        location = _location_with_cid(123456789012345678)
        LocationCache.set(
            location, "redata_place_details", {"name": "Katz's Delicatessen"}, query_key="123456789012345678"
        )

        matches = set(Location.objects.filter(self.source.missing_filter()).values_list("pk", flat=True))

        self.assertNotIn(location.pk, matches)
