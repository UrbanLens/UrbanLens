"""Tests for the USGS Earthquake Hazards panel's fetch(), gate(), and event-type filtering.

Now sourced through REData's ``/hazards/`` endpoint instead of a direct USGS
FDSN call - that endpoint is shared across hazard kinds, so fetch() filters
down to earthquakes for this specifically-seismic panel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.usgs_earthquakes import UsgsEarthquakePanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class UsgsEarthquakePanelSourceRenderTests(TestCase):
    """render_context() for the seismic-activity panel."""

    def setUp(self) -> None:
        super().setUp()
        self.source = UsgsEarthquakePanelSource()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_no_events_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"events": []}))

    def test_event_renders_magnitude_place_and_link(self) -> None:
        data = {"events": [{"event_type": "earthquake", "magnitude": 4.2, "magnitude_scale": "Mw", "place": "10km N of Nowhere", "occurred_at": "2026-01-01T00:00:00Z", "url": "https://example.com"}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["label"], "M4.2")
        self.assertIn("10km N of Nowhere", ctx["meta"][0]["value"])
        self.assertEqual(ctx["meta"][0]["href"], "https://example.com")


class UsgsEarthquakePanelSourceFetchTests(TestCase):
    """fetch() filters REData's hazards results down to earthquakes and persists them."""

    def setUp(self) -> None:
        super().setUp()
        self.source = UsgsEarthquakePanelSource()
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_fetch_keeps_only_earthquake_events(self) -> None:
        """The shared hazards endpoint may carry non-seismic event kinds - this panel is specifically about seismic activity."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(
            count=2,
            complete=True,
            results=[
                {"event_type": "earthquake", "magnitude": 4.2, "place": "10km N of Nowhere", "occurred_at": "2026-01-01T00:00:00Z", "url": "https://example.com"},
                {"event_type": "wildfire", "magnitude": None, "place": "Elsewhere", "occurred_at": "2026-01-02T00:00:00Z", "url": "https://example.com/2"},
            ],
        )
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_hazards_gateway.RedataHazardsGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_hazard_events.return_value = envelope
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "usgs_earthquakes")
        assert cached is not None
        self.assertEqual(len(cached.data["events"]), 1)
        self.assertEqual(cached.data["events"][0]["event_type"], "earthquake")
        mock_gateway_cls.return_value.get_hazard_events.assert_called_once_with(40.5, -74.5, radius_meters=100_000, min_magnitude=3.0, years=10, limit=10)

    def test_fetch_caches_an_explicit_empty_result(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(count=0, complete=True, results=[])
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_hazards_gateway.RedataHazardsGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_hazard_events.return_value = envelope
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "usgs_earthquakes")
        assert cached is not None
        self.assertEqual(cached.data["events"], [])


class UsgsEarthquakePanelSourceGateTests(TestCase):
    """gate() also requires REData to be configured, since this panel has no other data source."""

    def setUp(self) -> None:
        super().setUp()
        self.source = UsgsEarthquakePanelSource()
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_gate_is_false_when_redata_is_not_configured(self) -> None:
        with mock.patch("urbanlens.dashboard.plugins.builtin.usgs_earthquakes.redata_configured", return_value=False):
            self.assertFalse(self.source.gate(self.pin))

    def test_gate_is_true_when_redata_is_configured_and_coordinates_exist(self) -> None:
        with mock.patch("urbanlens.dashboard.plugins.builtin.usgs_earthquakes.redata_configured", return_value=True):
            self.assertTrue(self.source.gate(self.pin))
