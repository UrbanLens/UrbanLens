"""Regression tests for the direct (no-preview) pin import path.

Every import parser (CSV, KML, GeoJSON, shapefile, GPX, WKT/WKB, OSM XML)
embeds ``"profile": user_profile`` in the pin dicts it yields, and
``import_pins_streaming`` passes those dicts straight through as the
``defaults`` for ``Pin.objects.get_nearby_or_create`` - which also receives
``profile`` as an explicit argument.  Creating a *new* pin then raised
``TypeError: create() got multiple values for keyword argument 'profile'``,
killing the SSE stream mid-response (seen in production as nginx
"upstream prematurely closed connection").

These tests run the real import path without mocking ``get_nearby_or_create``
so the collision cannot silently regress.
"""

from __future__ import annotations

import json

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.text_limits import MAX_PIN_DESCRIPTION_LENGTH


def _events(sse_lines: list[str]) -> list[dict]:
    """Decode a list of ``data: {...}\\n\\n`` SSE strings into event dicts.

    Args:
        sse_lines: Raw SSE strings yielded by an import generator.

    Returns:
        The decoded JSON payload of each event, in order.
    """
    return [json.loads(line.removeprefix("data: ").strip()) for line in sse_lines]


class GetNearbyOrCreateProfileDefaultsTests(TestCase):
    """get_nearby_or_create() tolerates parser dicts that carry a ``profile`` key."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def test_profile_in_defaults_does_not_conflict_with_argument(self) -> None:
        pin, created = Pin.objects.get_nearby_or_create(
            40.0,
            -74.0,
            self.profile,
            defaults={"profile": self.profile, "name": "Old Mill"},
        )

        self.assertTrue(created)
        self.assertEqual(pin.profile, self.profile)
        self.assertEqual(pin.name, "Old Mill")


class ImportPinsStreamingCreatesPinsTests(TestCase):
    """import_pins_streaming() creates pins from parser output end-to-end."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def test_csv_import_creates_new_pin(self) -> None:
        csv_bytes = b"name,latitude,longitude\nOld Mill,40.0,-74.0\n"

        events = _events(list(self.gateway.import_pins_streaming([("pins.csv", csv_bytes)], self.profile)))

        complete = [event for event in events if event.get("type") == "complete"]
        self.assertEqual(len(complete), 1, f"expected a complete event, got: {events}")
        self.assertEqual(complete[0]["created"], 1)
        self.assertEqual(complete[0]["skipped"], 0)

        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertEqual(pin.latitude, 40.0)
        self.assertEqual(pin.longitude, -74.0)

    def test_oversized_description_is_clamped_not_left_unbounded(self) -> None:
        """Pin.save() never calls full_clean(), so this direct-create path must
        clamp itself - nothing else enforces the model's own MaxLengthValidator."""
        huge_description = "x" * (MAX_PIN_DESCRIPTION_LENGTH + 1000)
        csv_bytes = f"name,latitude,longitude,description\nOld Mill,40.0,-74.0,{huge_description}\n".encode()

        list(self.gateway.import_pins_streaming([("pins.csv", csv_bytes)], self.profile))

        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertEqual(len(pin.description), MAX_PIN_DESCRIPTION_LENGTH)
