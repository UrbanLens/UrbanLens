"""The ``refetch_parcel_buildings`` cleanup: dry run by default, and only the rows a pending REData answer could have left.

UrbanLens 0.8.0 kept REData 0.3.7 to 0.3.9's pending 503 for a parcel's buildings as a settled answer: Building
Attributes cached ``{}``, and the parcel-buildings list cached its OpenStreetMap or CRIS fallback, or ``{}``. The
command deletes those rows from the window given, so enrichment and the panels fetch them again; nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location

_SINCE = datetime(2026, 10, 7, 8, 24, tzinfo=UTC)
_UNTIL = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
_INSIDE = datetime(2026, 10, 7, 9, 19, 34, tzinfo=UTC)
_WINDOW = ("--since", _SINCE.isoformat(), "--until", _UNTIL.isoformat())
_REDATA_LIST = {"buildings": [{"name": "Main", "latitude": 41.7, "longitude": -73.9}], "provider": "redata"}
_OSM_LIST = {"buildings": [{"name": "", "latitude": 41.7, "longitude": -73.9}], "provider": "osm"}
_CRIS_LIST = {"buildings": [{"name": "Ward", "latitude": 41.7, "longitude": -73.9}], "provider": "cris"}


class RefetchParcelBuildingsCommandTests(TestCase):
    def _row(self, source: str, data: dict, updated: datetime = _INSIDE) -> LocationCache:
        self._rows += 1
        latitude = f"41.{733000 + self._rows:06d}"
        location = baker.make(Location, latitude=latitude, longitude="-73.930000", google_place=None)
        row = LocationCache.set(location, source, data, query_key=f"{latitude},-73.93000")
        LocationCache.objects.filter(pk=row.pk).update(updated=updated)
        return row

    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("refetch_parcel_buildings", *args, stdout=out)
        return out.getvalue()

    def setUp(self) -> None:
        super().setUp()
        self._rows = 0
        self.attributes_empty = self._row("redata_building_attributes", {})
        self.attributes_found = self._row("redata_building_attributes", {"name": "Main", "year_built": 1871})
        self.buildings_empty = self._row("parcel_buildings", {})
        self.buildings_osm = self._row("parcel_buildings", _OSM_LIST)
        self.buildings_cris = self._row("parcel_buildings", _CRIS_LIST)
        self.buildings_redata = self._row("parcel_buildings", _REDATA_LIST)
        self.before = self._row("redata_building_attributes", {}, updated=_SINCE - timedelta(seconds=1))
        self.after = self._row("parcel_buildings", _OSM_LIST, updated=_UNTIL)
        self.other_source = self._row("property_records", {})

    def _kept(self) -> set[int]:
        return set(LocationCache.objects.values_list("pk", flat=True))

    def test_a_dry_run_counts_what_it_would_clear_and_changes_nothing(self) -> None:
        before = self._kept()

        output = self._run(*_WINDOW)

        self.assertEqual(self._kept(), before)
        self.assertIn("redata_building_attributes: 1", output)
        self.assertIn("parcel_buildings: 3", output)
        self.assertIn("--apply", output)

    def test_apply_clears_only_empty_or_fallback_rows_from_the_window(self) -> None:
        self._run(*_WINDOW, "--apply")

        cleared = {self.attributes_empty.pk, self.buildings_empty.pk, self.buildings_osm.pk, self.buildings_cris.pk}
        kept = {self.attributes_found.pk, self.buildings_redata.pk, self.before.pk, self.after.pk, self.other_source.pk}
        self.assertFalse(self._kept() & cleared)
        self.assertEqual(self._kept() & kept, kept)

    def test_a_second_run_finds_nothing(self) -> None:
        self._run(*_WINDOW, "--apply")

        output = self._run(*_WINDOW)

        self.assertIn("redata_building_attributes: 0", output)
        self.assertIn("parcel_buildings: 0", output)

    def test_the_window_must_be_named_and_run_forwards(self) -> None:
        with self.assertRaises(CommandError):
            self._run("--since", _SINCE.isoformat())
        with self.assertRaises(CommandError):
            self._run("--since", _UNTIL.isoformat(), "--until", _SINCE.isoformat())
        with self.assertRaises(CommandError):
            self._run("--since", "2026-10-07T08:24:00", "--until", _UNTIL.isoformat())
