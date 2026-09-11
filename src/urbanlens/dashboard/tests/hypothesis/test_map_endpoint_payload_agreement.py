"""Every endpoint that hands the map a pin must hand it the same shape.

Three endpoints feed the same client-side `_pinStore` and the same versioned
localStorage cache: `map.pins` (the bulk fetch), `map.pin.json` (the targeted
refresh after an edit) and `map.search` (the filter panel). Only the first
returned `MapPinPayloadService`'s payload as-is; the other two went through
`map_data_context`, which rewrote it - tags collapsed to a comma-separated string
with the objects moved to a `tags_data` key, categories to a string, dates
reformatted, status capitalized.

Nothing failed when they disagreed, because each was self-consistent. The
damage showed up on the client, which reads `tags_data` for the edit dialog's
label pre-fill and for client-side label filtering: a pin loaded in bulk has no
such key, so its labels read as empty, while the same pin re-fetched after an
edit has them. Two shapes in one store, and the cache persisted whichever
arrived last.
"""

from __future__ import annotations

import json
import re
from typing import Any

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

_EMBEDDED_PINS = re.compile(rb'<script id="map-filter-pins" type="application/json">(.*?)</script>', re.DOTALL)


class MapEndpointPayloadAgreementTests(TestCase):
    """`map.pins`, `map.pin.json` and `map.search` must agree pin for pin."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)
        labels = [
            baker.make(Label, kind="tag", name="Endpoint Tag", icon="star", color="#ff0000"),
            baker.make(Label, kind="category", name="Endpoint Category", icon="home", color="#00ff00"),
            baker.make(Label, kind="status", name="Endpoint Status"),
        ]
        self.pins = []
        for index in range(3):
            location = baker.make(
                Location,
                latitude=f"43.{index:06d}",
                longitude=f"-71.{index:06d}",
                official_name=f"Endpoint Place {index}",
                street_number="9",
                route="Elm St",
                locality="Concord",
                administrative_area_level_1="NH",
            )
            pin = baker.make(Pin, profile=self.profile, location=location, name=f"Endpoint Pin {index}")
            pin.labels.set(labels)
            self.pins.append(pin)

    def _bulk_payloads(self) -> dict[str, dict[str, Any]]:
        response = self.client.get(reverse("map.pins"))
        self.assertEqual(response.status_code, 200)
        return {pin["uuid"]: pin for pin in response.json()["pins"]}

    def _single_payload(self, pin: Pin) -> dict[str, Any]:
        response = self.client.get(reverse("map.pin.json", kwargs={"pin_slug": pin.slug or str(pin.uuid)}))
        self.assertEqual(response.status_code, 200)
        return response.json()["pin"]

    def _filter_payloads(self) -> dict[str, dict[str, Any]]:
        response = self.client.post(reverse("map.search"), {})
        self.assertEqual(response.status_code, 200)
        match = _EMBEDDED_PINS.search(response.content)
        self.assertIsNotNone(
            match,
            "the filter response embeds no pin document - it is still rendering a script block per pin",
        )
        assert match is not None
        return {pin["uuid"]: pin for pin in json.loads(match.group(1))}

    def test_the_single_pin_refresh_returns_what_the_bulk_fetch_returns(self) -> None:
        bulk = self._bulk_payloads()

        for pin in self.pins:
            self.assertEqual(self._single_payload(pin), bulk[str(pin.uuid)], f"map.pin.json disagrees for pin {pin.pk}")

    def test_the_filter_panel_returns_what_the_bulk_fetch_returns(self) -> None:
        bulk = self._bulk_payloads()

        self.assertEqual(self._filter_payloads(), bulk)

    def test_the_filter_response_renders_no_per_pin_script_block(self) -> None:
        """One embedded document, however many pins - not one literal each."""
        response = self.client.post(reverse("map.search"), {})

        self.assertEqual(
            response.content.count(b"viewLocationUrl:"),
            0,
            "the filter response still builds a per-pin object literal in the template",
        )
        self.assertEqual(response.content.count(b'id="map-filter-pins"'), 1)
