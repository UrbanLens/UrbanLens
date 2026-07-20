"""Tests for the Open-Elevation panel's render_context() and fetch().

Covers the "Elevation" pin-detail info panel added to the previously
UI-less Open-Elevation plugin - see docs/prompts/completed.md.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.open_elevation import ElevationPanelSource


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

    def test_fetch_caches_a_successful_lookup(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with mock.patch("urbanlens.dashboard.services.apis.elevation.open_elevation.OpenElevationGateway.get_elevation", return_value=245.0):
            self.source.fetch(self.pin)
        cached = LocationCache.get_fresh(self.pin.location, "open_elevation")
        assert cached is not None
        self.assertEqual(cached.data["elevation_m"], 245.0)

    def test_fetch_caches_an_explicit_failure_rather_than_leaving_it_unfetched(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with mock.patch("urbanlens.dashboard.services.apis.elevation.open_elevation.OpenElevationGateway.get_elevation", return_value=None):
            self.source.fetch(self.pin)
        cached = LocationCache.get_fresh(self.pin.location, "open_elevation")
        assert cached is not None
        self.assertIsNone(cached.data["elevation_m"])


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
