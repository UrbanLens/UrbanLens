"""Migration 0044 drops the CRIS rows the old site rule wrote, and nothing else (P234)."""

from __future__ import annotations

import importlib

from django.apps import apps
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location

_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0044_cris_site_rows_refetch")
_CRIS = "cris_building_usn"


class DropStaleCrisRowsTests(TestCase):
    def _row(self, data: dict, source: str = _CRIS) -> int:
        return LocationCache.set(baker.make(Location), source, data, "q").location_id

    def test_site_scope_rows_and_rows_naming_a_neighbour_are_dropped(self) -> None:
        campus = self._row({"site_scope": True, "attachments": []})
        neighboured = self._row({"site_scope": False, "district": {"USNName": "Quiet Cove", "contains_point": False}})
        building = self._row({"site_scope": False, "district": {"HistoricName": "HRSH", "contains_point": True}})
        plain = self._row({"USNName": "Old Mill", "attachments": []})
        nothing = self._row({})
        other_source = self._row({"site_scope": True}, source="parcel_buildings")

        _MIGRATION.drop_stale_cris_rows(apps, None)

        remaining = set(LocationCache.objects.values_list("location_id", flat=True))
        self.assertEqual(remaining, {building, plain, nothing, other_source})
        self.assertFalse({campus, neighboured} & remaining)
