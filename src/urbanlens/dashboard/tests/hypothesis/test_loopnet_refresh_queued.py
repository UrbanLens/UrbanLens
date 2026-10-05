"""LoopNet's ``refresh_queued`` is honoured: a parcel whose listings REData has only just gone to fetch is asked again soon.

REData's listings endpoint never fetches inline. On a cold parcel it answers ``results: []`` with
``refresh_queued: true``, and caching that as "no listings" hid the parcel's listings for a week.
"""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.loopnet import LoopnetPanelSource
from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway
from urbanlens.dashboard.services.core.gateway import UpstreamBusyError, is_source_outage
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_LISTING = {"uuid": "listing-1", "loopnet_url": "https://www.loopnet.com/Listing/123", "title": "Retail", "photos": []}


class RefreshQueuedTests(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        location = baker.make(
            Location,
            latitude="42.650000",
            longitude="-73.750000",
            street_number="123",
            route="Main St",
            locality="Anytown",
            google_place=None,
        )
        self.pin = baker.make(Pin, profile=baker.make("auth.User").profile, location=location)

    def _fetch(self, body: dict) -> None:
        with (
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_listings", return_value=body),
        ):
            LoopnetPanelSource().fetch(self.pin)

    def _cached(self) -> dict | None:
        row = LocationCache.objects.filter(location=self.pin.location, source="loopnet").first()
        return None if row is None else row.data

    def test_a_queued_refresh_with_nothing_yet_is_not_an_answer(self) -> None:
        with self.assertRaises(UpstreamBusyError) as raised:
            self._fetch({"results": [], "refresh_queued": True})

        self.assertIsNone(self._cached())
        self.assertTrue(is_source_outage(raised.exception))

    def test_it_is_asked_again_within_the_hour(self) -> None:
        with self.assertRaises(UpstreamBusyError) as raised:
            self._fetch({"results": [], "refresh_queued": True})

        self.assertLessEqual(raised.exception.retry_after, 3600)

    def test_listings_already_held_are_shown_while_a_refresh_runs(self) -> None:
        self._fetch({"results": [_LISTING], "refresh_queued": True})

        self.assertEqual(self._cached(), {"listings": [_LISTING]})

    def test_a_settled_empty_answer_is_cached(self) -> None:
        self._fetch({"results": [], "refresh_queued": False})

        self.assertEqual(self._cached(), {})
