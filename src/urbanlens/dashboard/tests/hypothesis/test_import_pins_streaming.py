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
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_DESCRIPTION_LENGTH


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

    def test_csv_with_only_latitude_longitude_and_utf8_bom_creates_pin(self) -> None:
        """Excel-exported lat/lng-only CSVs (UTF-8 BOM, no name column) must import."""
        csv_bytes = "\ufefflatitude,longitude\n40.0,-74.0\n".encode("utf-8")

        events = _events(list(self.gateway.import_pins_streaming([("coords.csv", csv_bytes)], self.profile)))

        complete = [event for event in events if event.get("type") == "complete"]
        self.assertEqual(len(complete), 1, f"expected a complete event, got: {events}")
        self.assertEqual(complete[0]["created"], 1)
        self.assertEqual(complete[0]["skipped"], 0)

        pin = Pin.objects.get(profile=self.profile)
        self.assertEqual(pin.name, "Unnamed")
        self.assertEqual(pin.latitude, 40.0)
        self.assertEqual(pin.longitude, -74.0)

    def test_quoted_latitude_longitude_only_csv_creates_all_pins(self) -> None:
        """Regression: quoted lat/lng-only CSV (no name column) must import every row.

        This is the shape Excel / Sheets produce for a two-column coordinates
        export - every field quoted, header included, no other columns.
        """
        csv_bytes = (
            b'"latitude","longitude"\n'
            b'"34.0162419","-78.2885742"\n'
            b'"34.0663120","-84.3255615"\n'
            b'"34.0708623","-84.3310547"\n'
            b'"34.2345124","-83.4741211"\n'
            b'"34.5518114","-88.5607910"\n'
            b'"34.6083452","-94.5950317"\n'
            b'"34.7235549","-79.8486328"\n'
            b'"34.7732038","-93.7380981"\n'
            b'"34.9219710","-79.7607422"\n'
            b'"35.2523481","-90.1263428"\n'
            b'"35.4606700","-90.0384521"\n'
            b'"35.7643435","-83.1665039"\n'
            b'"35.9602230","-83.9135742"\n'
            b'"35.9602230","-78.0688477"\n'
            b'"35.9646691","-83.9080811"\n'
            b'"35.9780062","-83.9575195"\n'
            b'"35.9957854","-81.9799805"\n'
            b'"35.9957854","-78.9697266"\n'
            b'"35.9957854","-78.9038086"\n'
            b'"35.9957854","-77.7612305"\n'
            b'"36.0490990","-86.6601563"\n'
        )
        # Same bytes Excel often emits - UTF-8 BOM prefixing the first header.
        bom_csv_bytes = b"\xef\xbb\xbf" + csv_bytes

        for label, payload in (("plain", csv_bytes), ("bom", bom_csv_bytes)):
            with self.subTest(label=label):
                Pin.objects.filter(profile=self.profile).delete()
                events = _events(list(self.gateway.import_pins_streaming([("coords.csv", payload)], self.profile)))
                complete = [event for event in events if event.get("type") == "complete"]
                self.assertEqual(len(complete), 1, f"expected a complete event, got: {events}")
                self.assertEqual(complete[0]["created"], 21, complete[0])
                self.assertEqual(complete[0]["skipped"], 0, complete[0])
                self.assertEqual(Pin.objects.filter(profile=self.profile).count(), 21)

    def test_html_description_is_stripped_and_link_extracted(self) -> None:
        csv_bytes = (
            b"name,latitude,longitude,description\n"
            b'Old Mill,40.0,-74.0,"City: Poughkeepsie<br>Tour: https://example.com/story"\n'
        )

        list(self.gateway.import_pins_streaming([("pins.csv", csv_bytes)], self.profile))

        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertNotIn("<br>", pin.description)
        self.assertIn("City: Poughkeepsie", pin.description)
        self.assertTrue(pin.links.filter(url="https://example.com/story").exists())

    def test_oversized_description_is_clamped_not_left_unbounded(self) -> None:
        """Pin.save() never calls full_clean(), so this direct-create path must
        clamp itself - nothing else enforces the model's own MaxLengthValidator."""
        huge_description = "x" * (MAX_PIN_DESCRIPTION_LENGTH + 1000)
        csv_bytes = f"name,latitude,longitude,description\nOld Mill,40.0,-74.0,{huge_description}\n".encode()

        list(self.gateway.import_pins_streaming([("pins.csv", csv_bytes)], self.profile))

        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertEqual(len(pin.description), MAX_PIN_DESCRIPTION_LENGTH)

    def test_takeout_url_row_does_not_crash_on_preview_only_dict_keys(self) -> None:
        """_csv_row_iter() embeds "s2_guess"/"maps_url" in every Takeout-URL row for the
        preview/deferred-lookup flow (see cid_resolution.resolve_cids) - this direct,
        no-preview path never consumes them and must strip them before they reach
        Pin.objects.create(**defaults), which has no such fields and would raise
        TypeError, killing the SSE stream mid-response (the same failure mode as the
        get_nearby_or_create profile-collision bug this file otherwise covers).
        """
        csv_bytes = (
            b"Title,URL\n"
            b'Black Point Ruins,"https://www.google.com/maps/place/Black+Point+Ruins/data=!4m2!3m1!1s0x89e5bd8b55e7f8fd:0x59ac8820518a7e79"\n'
        )

        events = _events(list(self.gateway.import_pins_streaming([("pins.csv", csv_bytes)], self.profile)))

        complete = [event for event in events if event.get("type") == "complete"]
        self.assertEqual(len(complete), 1, f"expected a complete event, got: {events}")
        self.assertEqual(complete[0]["created"], 1, complete[0])
        self.assertEqual(complete[0]["skipped"], 0, complete[0])
        self.assertTrue(Pin.objects.filter(profile=self.profile, name="Black Point Ruins").exists())
