"""Regression tests for the CID-deferral split in import_preview_streaming.

A confirmed pin whose cid has neither an existing Location nor a cached
Places lookup must never be placed from the preview's own (unverified)
lat/lng - see maps.py's import_preview_streaming docstring and
docs/redata-cid-resolution.md for why (the free S2-decode heuristic behind
that preview guess is wrong ~31% of the time). It should instead be queued
for background resolution via resolve_deferred_pin_locations.
"""

from __future__ import annotations

import json
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.model import GeocodedLocation
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway


class ImportPreviewCidDeferralTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def _run(self, pins: list[dict]) -> list[dict]:
        events_raw = self.gateway.import_preview_streaming(
            [{"stem": "", "create_category": False, "label_ids": [], "pins": pins}],
            self.profile,
            auto_tag=False,
        )
        events = []
        for line in events_raw:
            assert line.startswith("data: ")
            events.append(json.loads(line[len("data: ") :]))
        return events

    def test_pin_with_uncached_cid_is_deferred_not_created_from_preview_coords(self) -> None:
        """The preview's lat/lng for an unresolved cid is a guess - never trust it."""
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            events = self._run(
                [{"name": "Cresson Sanatorium", "lat": 40.431072, "lng": -78.502283, "description": "", "cid": 12345}],
            )

        self.assertFalse(Pin.objects.filter(profile=self.profile).exists())

        outcomes = [e["outcome"] for e in events if e["type"] == "progress"]
        self.assertEqual(outcomes, ["deferred"])

        complete = next(e for e in events if e["type"] == "complete")
        self.assertEqual(complete["created"], 0)
        self.assertEqual(complete["deferred"], 1)

        deferred_event = next(e for e in events if e["type"] == "deferred")
        self.assertEqual(deferred_event["count"], 1)

        enqueue.assert_called_once()
        args = enqueue.call_args.args
        self.assertEqual(args[1], self.profile.pk)
        deferred_lists = args[2]
        self.assertEqual(len(deferred_lists), 1)
        self.assertEqual(deferred_lists[0]["pins"][0]["cid"], 12345)

    def test_deferred_pin_carries_its_source_maps_url_for_the_background_lookup(self) -> None:
        """resolve_deferred_pin_locations passes this to REData, which resolves via a
        place's own URL faster and more reliably than the bare cid alone."""
        url = "https://www.google.com/maps/place/Cresson+Sanatorium/data=!4m2!3m1!1s0x0:0x3039"
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            self._run(
                [{"name": "Cresson Sanatorium", "lat": 40.431072, "lng": -78.502283, "description": "", "cid": 12345, "maps_url": url}],
            )

        deferred_lists = enqueue.call_args.args[2]
        self.assertEqual(deferred_lists[0]["pins"][0]["maps_url"], url)

    def test_pin_with_cached_cid_is_created_immediately_from_the_cache_not_preview_coords(self) -> None:
        """A cid already resolved by an earlier lookup (cache hit) stays on the fast path."""
        GeocodedLocation.objects.create(
            place_name="cid:99999",
            latitude=40.450732,
            longitude=-78.563382,
            json_response=json.dumps({"result": {"geometry": {"location": {"lat": 40.450732, "lng": -78.563382}}}}),
        )

        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            events = self._run(
                # Deliberately wrong preview coordinates - the cache hit must win.
                [{"name": "Cresson Sanatorium", "lat": 40.431072, "lng": -78.502283, "description": "", "cid": 99999}],
            )

        enqueue.assert_not_called()
        self.assertFalse(any(e["type"] == "deferred" for e in events))

        pin = Pin.objects.get(profile=self.profile, name="Cresson Sanatorium")
        self.assertAlmostEqual(float(pin.location.latitude), 40.450732, places=5)
        self.assertAlmostEqual(float(pin.location.longitude), -78.563382, places=5)

    def test_pin_with_cid_matching_existing_location_stays_on_the_fast_path(self) -> None:
        google_place = baker.make(GooglePlace, cid=55555)
        location = baker.make(Location, latitude=41.0, longitude=-75.0, google_place=google_place)

        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            events = self._run(
                [{"name": "Known Place", "lat": 0.0, "lng": 0.0, "description": "", "cid": 55555}],
            )

        enqueue.assert_not_called()
        self.assertFalse(any(e["type"] == "deferred" for e in events))
        pin = Pin.objects.get(profile=self.profile, name="Known Place")
        self.assertEqual(pin.location_id, location.pk)
