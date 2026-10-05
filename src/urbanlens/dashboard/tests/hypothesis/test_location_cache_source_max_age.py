"""A cache-backed panel whose data goes stale in hours is refreshed in hours, not after the site-wide week.

REData keeps air-quality readings 1-3 hours and NPS alerts a few hours; ``LocationCache`` served both as current for
``external_data_cache_days`` (7 by default).
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.nps import NpsPanelSource
from urbanlens.dashboard.plugins.builtin.redata_air_quality import AirQualityPanelSource
from urbanlens.dashboard.services.pins.external_data import panel_readiness
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_READING = {
    "provider": "open_meteo_air",
    "source_kind": "modelled",
    "station_name": "",
    "observed_at": "2026-10-05T14:00:00Z",
    "us_aqi": 42.0,
    "pm2_5": 8.1,
}


class SourceMaxAgeTests(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        location = baker.make(Location, latitude=40.5, longitude=-74.5)
        self.pin = baker.make(Pin, profile=baker.make(User).profile, location=location, parent_pin=None)
        self.source = AirQualityPanelSource()

    def _cache(self, source: str, data: dict, *, age: timedelta) -> None:
        LocationCache.set(self.pin.location, source, data)
        LocationCache.objects.filter(location=self.pin.location, source=source).update(updated=timezone.now() - age)

    def test_the_short_lived_sources_declare_their_own_age(self) -> None:
        for source, longest in ((AirQualityPanelSource(), timedelta(hours=3)), (NpsPanelSource(), timedelta(hours=6))):
            with self.subTest(source=source.key):
                assert source.cache_max_age is not None
                self.assertLessEqual(source.cache_max_age, longest)

    def test_a_reading_older_than_its_source_allows_is_stale(self) -> None:
        self._cache(self.source.cache_source, {"readings": [_READING]}, age=timedelta(hours=5))

        self.assertIsNone(self.source.cached_data(self.pin))
        self.assertFalse(panel_readiness(self.pin, [self.source])[self.source.key])

    def test_a_recent_reading_is_fresh(self) -> None:
        self._cache(self.source.cache_source, {"readings": [_READING]}, age=timedelta(minutes=20))

        self.assertEqual(self.source.cached_data(self.pin), {"readings": [_READING]})
        self.assertTrue(panel_readiness(self.pin, [self.source])[self.source.key])

    def test_other_sources_keep_the_site_wide_window(self) -> None:
        self._cache("redata_permits", {"permits": []}, age=timedelta(days=2))

        self.assertIsNotNone(LocationCache.get_fresh(self.pin.location, "redata_permits"))

    def test_get_fresh_honours_a_shorter_age(self) -> None:
        self._cache(self.source.cache_source, {"readings": []}, age=timedelta(hours=5))

        self.assertIsNotNone(LocationCache.get_fresh(self.pin.location, self.source.cache_source))
        self.assertIsNone(
            LocationCache.get_fresh(self.pin.location, self.source.cache_source, max_age=timedelta(hours=1))
        )

    def test_a_building_does_not_inherit_a_stale_site_reading(self) -> None:
        """Air quality is site-level: a nested pin takes its site's answer, which must itself still be current."""
        site_location = baker.make(Location, latitude=40.5001, longitude=-74.5001)
        site = baker.make(Pin, profile=self.pin.profile, location=site_location, parent_pin=None)
        self.pin.parent_pin = site
        self.pin.save()
        LocationCache.set(site_location, self.source.cache_source, {"readings": [_READING]})
        LocationCache.objects.filter(location=site_location).update(updated=timezone.now() - timedelta(hours=5))

        # The site's stale row may not be copied; a fresh site fetch is what adopt_site_answer must ask for.
        with self.assertRaises(AssertionError), self._no_upstream():
            self.source.adopt_site_answer(self.pin)

        self.assertIsNone(self.source.cached_data(self.pin))

    def test_an_adopted_answer_keeps_the_sites_age(self) -> None:
        """Copied as new, a 50-minute-old site reading would read as current to the building for another hour."""
        site_location = baker.make(Location, latitude=40.5001, longitude=-74.5001)
        site = baker.make(Pin, profile=self.pin.profile, location=site_location, parent_pin=None)
        self.pin.parent_pin = site
        self.pin.save()
        LocationCache.set(site_location, self.source.cache_source, {"readings": [_READING]})
        fetched = timezone.now() - timedelta(minutes=50)
        LocationCache.objects.filter(location=site_location).update(updated=fetched)

        with self._no_upstream():
            self.assertTrue(self.source.adopt_site_answer(self.pin))

        adopted = LocationCache.objects.get(location=self.pin.location, source=self.source.cache_source)
        self.assertEqual(adopted.updated, fetched)

    def _no_upstream(self):
        from unittest import mock

        return mock.patch.object(AirQualityPanelSource, "fetch_envelope", side_effect=AssertionError("asked REData"))


class AirQualityTimestampTests(TestCase):
    def test_the_card_says_when_the_reading_was_taken(self) -> None:
        location = baker.make(Location, latitude=40.5, longitude=-74.5)
        pin = baker.make(Pin, profile=baker.make(User).profile, location=location)

        context = AirQualityPanelSource().render_context(pin, {"readings": [_READING]})

        assert context is not None
        observed = [row for row in context.get("meta", []) if row["label"] == "Observed"]
        self.assertEqual(len(observed), 1)
        self.assertIn("2026", observed[0]["value"])
