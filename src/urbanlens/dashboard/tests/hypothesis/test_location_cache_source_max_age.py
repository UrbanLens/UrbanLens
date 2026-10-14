"""
A cache-backed panel whose data goes stale in hours is refreshed in hours, not after the site-wide week.

``LocationCache`` served every source as current for ``external_data_cache_days`` (7 by default), whatever its
upstream said about how long the data lasts.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.panel_fakes import info_panel
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.external_data import panel_readiness

_READING = {"rows": ["42 AQI"]}


class SourceMaxAgeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        location = baker.make(Location, latitude=40.5, longitude=-74.5)
        self.pin = baker.make(Pin, profile=baker.make(User).profile, location=location, parent_pin=None)
        self.source = info_panel("hourly", site_level=True, cache_max_age=timedelta(hours=1), inspects_content=True)

    def _cache(self, source: str, data: dict, *, age: timedelta) -> None:
        LocationCache.set(self.pin.location, source, data)
        LocationCache.objects.filter(location=self.pin.location, source=source).update(updated=timezone.now() - age)

    def test_a_reading_older_than_its_source_allows_is_stale(self) -> None:
        self._cache(self.source.cache_source, _READING, age=timedelta(hours=5))

        self.assertIsNone(self.source.cached_data(self.pin))
        self.assertFalse(panel_readiness(self.pin, [self.source])[self.source.key])

    def test_a_recent_reading_is_fresh(self) -> None:
        self._cache(self.source.cache_source, _READING, age=timedelta(minutes=20))

        self.assertEqual(self.source.cached_data(self.pin), _READING)
        self.assertTrue(panel_readiness(self.pin, [self.source])[self.source.key])

    def test_other_sources_keep_the_site_wide_window(self) -> None:
        self._cache("unhurried", {"rows": []}, age=timedelta(days=2))

        self.assertIsNotNone(LocationCache.get_fresh(self.pin.location, "unhurried"))

    def test_get_fresh_honours_a_shorter_age(self) -> None:
        self._cache(self.source.cache_source, {"rows": []}, age=timedelta(hours=5))

        self.assertIsNotNone(LocationCache.get_fresh(self.pin.location, self.source.cache_source))
        self.assertIsNone(
            LocationCache.get_fresh(self.pin.location, self.source.cache_source, max_age=timedelta(hours=1))
        )

    def test_a_building_does_not_inherit_a_stale_site_reading(self) -> None:
        """A site-level panel gives a nested pin its site's answer, which must itself still be current."""
        site_location = baker.make(Location, latitude=40.5001, longitude=-74.5001)
        site = baker.make(Pin, profile=self.pin.profile, location=site_location, parent_pin=None)
        self.pin.parent_pin = site
        self.pin.save()
        LocationCache.set(site_location, self.source.cache_source, _READING)
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
        LocationCache.set(site_location, self.source.cache_source, _READING)
        fetched = timezone.now() - timedelta(minutes=50)
        LocationCache.objects.filter(location=site_location).update(updated=fetched)

        with self._no_upstream():
            self.assertTrue(self.source.adopt_site_answer(self.pin))

        adopted = LocationCache.objects.get(location=self.pin.location, source=self.source.cache_source)
        self.assertEqual(adopted.updated, fetched)

    def _no_upstream(self):
        return mock.patch.object(type(self.source), "fetch", side_effect=AssertionError("asked the upstream"))
