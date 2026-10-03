"""Migration 0038 drops cached searches that may have been built from someone's own names; nothing else (P188)."""

from __future__ import annotations

import importlib

from django.apps import apps
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location

_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0038_location_cache_drop_name_built_searches")

NAME_BUILT = (
    "web_search",
    "flickr",
    "searxng_images",
    "gdelt_v2",
    "smithsonian",
    "wikimedia",
    "library_of_congress",
    "digital_commonwealth",
    "internet_archive",
    "chronicling_america",
)
KEPT = ("wikipedia", "wikipedia_media", "nps", "nominatim", "google_images", "cris_building_usn", "gdelt")


class DropNameBuiltSearchesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        for source in (*NAME_BUILT, *KEPT):
            LocationCache.set(self.location, source, {"items": [{"url": f"https://example.com/{source}.jpg"}]}, "q")

    def test_every_name_built_search_is_dropped_and_every_other_row_kept(self) -> None:
        _MIGRATION.drop_name_built_searches(apps, None)

        remaining = set(LocationCache.objects.filter(location=self.location).values_list("source", flat=True))
        self.assertEqual(remaining, set(KEPT))

    def test_the_list_matches_every_source_that_now_caches_per_audience(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import NameSearchSource, panel_sources

        audienced = {source.cache_source for source in panel_sources().values() if isinstance(source, NameSearchSource)}
        self.assertEqual(audienced | {"web_search"}, set(_MIGRATION.NAME_BUILT_SOURCES))
