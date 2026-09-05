"""Tests for the Elevation panel's render_context(), fetch(), and gate().

Covers the pin-detail "Elevation" info panel, now sourced through REData's
``/elevation/`` endpoint instead of a direct Open-Elevation call.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.open_elevation import ElevationPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope


class ElevationPanelSourceRenderTests(TestCase):
    """render_context() for the elevation panel."""

    def setUp(self) -> None:
        super().setUp()
        self.source = ElevationPanelSource()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_missing_elevation_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {}))

    def test_failed_lookup_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"elevation_m": None}))

    def test_positive_elevation_shows_above_sea_level(self) -> None:
        ctx = self.source.render_context(self.pin, {"elevation_m": 1603.0})
        assert ctx is not None
        text = ctx["facts"][0]["text"]
        self.assertIn("1,603 m", text)
        self.assertIn("above sea level", text)

    def test_negative_elevation_shows_below_sea_level(self) -> None:
        ctx = self.source.render_context(self.pin, {"elevation_m": -80.0})
        assert ctx is not None
        text = ctx["facts"][0]["text"]
        # The magnitude is shown unsigned - "below sea level" already conveys sign.
        self.assertIn("80 m", text)
        self.assertNotIn("-80", text)
        self.assertIn("below sea level", text)

    def test_zero_elevation_shows_above_sea_level(self) -> None:
        ctx = self.source.render_context(self.pin, {"elevation_m": 0.0})
        assert ctx is not None
        self.assertIn("above sea level", ctx["facts"][0]["text"])


class ElevationPanelSourceFetchTests(TestCase):
    """fetch() persists both a real result and an explicit failure."""

    def setUp(self) -> None:
        super().setUp()
        self.source = ElevationPanelSource()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_fetch_caches_the_finest_resolution_reading_and_keeps_the_raw_list(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(
            count=2,
            complete=True,
            results=[
                {
                    "provider": "usgs_epqs",
                    "dataset": "3DEP",
                    "resolution_meters": 10,
                    "elevation_meters": 245.0,
                    "status": "ok",
                },
                {
                    "provider": "open_meteo",
                    "dataset": "Copernicus DEM GLO-90",
                    "resolution_meters": 90,
                    "elevation_meters": 240.0,
                    "status": "ok",
                },
            ],
        )
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_elevation_gateway.RedataElevationGateway"
        ) as mock_gateway_cls:
            mock_gateway_cls.return_value.get_elevation.return_value = envelope
            self.source.fetch(self.pin)
        cached = LocationCache.get_fresh(self.pin.location, "open_elevation")
        assert cached is not None
        self.assertEqual(cached.data["elevation_m"], 245.0)
        self.assertEqual(cached.data["readings"], envelope.results)

    def test_fetch_caches_a_null_reading_from_the_finest_model_as_a_real_answer(self) -> None:
        """``elevation_meters: null`` alongside ``status: "ok"`` is a genuine answer (e.g. open ocean), not skipped for a coarser model's value."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(
            count=2,
            complete=True,
            results=[
                {
                    "provider": "usgs_epqs",
                    "dataset": "3DEP",
                    "resolution_meters": 10,
                    "elevation_meters": None,
                    "status": "ok",
                },
                {
                    "provider": "open_meteo",
                    "dataset": "Copernicus DEM GLO-90",
                    "resolution_meters": 90,
                    "elevation_meters": 240.0,
                    "status": "ok",
                },
            ],
        )
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_elevation_gateway.RedataElevationGateway"
        ) as mock_gateway_cls:
            mock_gateway_cls.return_value.get_elevation.return_value = envelope
            self.source.fetch(self.pin)
        cached = LocationCache.get_fresh(self.pin.location, "open_elevation")
        assert cached is not None
        self.assertIsNone(cached.data["elevation_m"])

    def test_fetch_caches_an_explicit_failure_when_no_dem_covers_the_point(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(count=0, complete=True, results=[])
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_elevation_gateway.RedataElevationGateway"
        ) as mock_gateway_cls:
            mock_gateway_cls.return_value.get_elevation.return_value = envelope
            self.source.fetch(self.pin)
        cached = LocationCache.get_fresh(self.pin.location, "open_elevation")
        assert cached is not None
        self.assertIsNone(cached.data["elevation_m"])
        self.assertEqual(cached.data["readings"], [])


class ElevationPanelSourceGateTests(TestCase):
    """gate() also requires REData to be configured, since this panel has no other data source."""

    def setUp(self) -> None:
        super().setUp()
        self.source = ElevationPanelSource()
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_gate_is_false_when_redata_is_not_configured(self) -> None:
        with mock.patch("urbanlens.dashboard.plugins.builtin.open_elevation.redata_configured", return_value=False):
            self.assertFalse(self.source.gate(self.pin))

    def test_gate_is_true_when_redata_is_configured_and_coordinates_exist(self) -> None:
        with mock.patch("urbanlens.dashboard.plugins.builtin.open_elevation.redata_configured", return_value=True):
            self.assertTrue(self.source.gate(self.pin))


class ElevationFormattingPropertyTests(SimpleTestCase):
    """Pure meters<->feet formatting properties, no DB needed."""

    @given(st.floats(min_value=0, max_value=9000, allow_nan=False, allow_infinity=False))
    @settings(max_examples=30, deadline=None)
    def test_positive_elevations_never_render_a_minus_sign(self, elevation_m: float) -> None:
        source = ElevationPanelSource()
        ctx = source.render_context(Pin(), {"elevation_m": elevation_m})
        assert ctx is not None
        self.assertNotIn("-", ctx["facts"][0]["text"])

    @given(st.floats(min_value=-500, max_value=-0.01, allow_nan=False, allow_infinity=False))
    @settings(max_examples=30, deadline=None)
    def test_negative_elevations_say_below_sea_level_not_minus_sign(self, elevation_m: float) -> None:
        source = ElevationPanelSource()
        ctx = source.render_context(Pin(), {"elevation_m": elevation_m})
        assert ctx is not None
        text = ctx["facts"][0]["text"]
        self.assertIn("below sea level", text)
        self.assertNotIn("-", text)
