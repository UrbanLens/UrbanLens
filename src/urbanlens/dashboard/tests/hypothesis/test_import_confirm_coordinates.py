"""The confirm step takes each pin's coordinates back from the client, so it checks them as the preview does (P282).

The preview never offers a place off the globe, but the confirm request is the client's own JSON, and Python's JSON
parser reads ``Infinity`` and ``NaN``. A latitude past the poles became a Location there.
"""

from __future__ import annotations

import math
from typing import Any

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway


class ConfirmedImportCoordinatesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.importer = baker.make(User).profile

    def _import(self, latitude: Any, longitude: Any) -> dict[str, Any]:
        pin = {"name": "Imported", "lat": latitude, "lng": longitude, "description": "", "cid": None, "maps_url": ""}
        lists = [{"stem": "", "create_category": False, "label_ids": [], "pins": [pin]}]
        events = list(
            GoogleMapsGateway(api_key="test-key").iter_confirmed_import_events(lists, self.importer, auto_tag=False)
        )
        return next(event for event in events if event["type"] == "complete")

    def test_a_place_off_the_globe_is_skipped(self) -> None:
        for latitude, longitude in [
            (95, -74),
            (-90.5, -74),
            (40, 180.5),
            (40, -200),
            (math.inf, -74),
            (40, -math.inf),
            (math.nan, -74),
            ("north", -74),
            ([40], -74),
        ]:
            with self.subTest(latitude=latitude, longitude=longitude):
                Pin.objects.filter(profile=self.importer).delete()

                complete = self._import(latitude, longitude)

                self.assertEqual(complete["skipped"], 1, complete)
                self.assertFalse(Pin.objects.filter(profile=self.importer).exists())
                self.assertFalse(Location.objects.filter(latitude__gt=90).exists())

    def test_a_place_on_the_globe_is_imported(self) -> None:
        for latitude, longitude in [(40.5, -74.25), (90, 180), (-90, -180), ("40.5", "-74.25")]:
            with self.subTest(latitude=latitude, longitude=longitude):
                Pin.objects.filter(profile=self.importer).delete()

                complete = self._import(latitude, longitude)

                self.assertEqual(complete["created"], 1, complete)
