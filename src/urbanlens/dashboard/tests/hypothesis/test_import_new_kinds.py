"""Round-trip tests for the chunk-487 importers: safety, map annotations, saved searches.

The chunk-469 kinds were export-only; these importers close the loop. The
load-bearing behaviors: a live-status safety check-in must NOT import (an
archive restore must never re-arm reminders or page contacts), overlays
attach only through their parent pin, and re-importing the same archive is a
no-op everywhere.
"""

from __future__ import annotations

import tempfile

from django.contrib.auth.models import User
from django.contrib.gis.geos import LineString
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
from urbanlens.dashboard.services.import_export.export import _export_map_annotations, _export_safety, _export_saved_searches
from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_map_annotations, _import_safety, _import_saved_searches


class NewKindImportTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.source = baker.make(User).profile
        self.target = baker.make(User).profile

    def _kwargs(self, pin_uuid_map=None):
        return {"pin_uuid_map": pin_uuid_map or {}, "label_uuid_map": {}}

    def test_safety_round_trip_imports_concluded_and_refuses_live(self) -> None:
        concluded = baker.make("dashboard.SafetyCheckin", profile=self.source, title="Mill roof", status=SafetyCheckinStatus.FOUND_SAFE)
        baker.make("dashboard.SafetyCheckinContact", checkin=concluded, name="Sam", email="sam@example.test")
        baker.make("dashboard.SafetyCheckinMessage", checkin=concluded, body="made it out")
        baker.make("dashboard.SafetyCheckin", profile=self.source, title="Tonight's plan", status=SafetyCheckinStatus.SCHEDULED)

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_safety(self.source, temp_dir)
            _import_safety(self.target, temp_dir, ImportResult(), **self._kwargs())
            _import_safety(self.target, temp_dir, ImportResult(), **self._kwargs())  # idempotent

        imported = SafetyCheckin.objects.filter(profile=self.target)
        self.assertEqual(imported.count(), 1, "live-status check-ins must not import - restoring one would re-arm reminders")
        row = imported.get()
        self.assertEqual(row.status, SafetyCheckinStatus.FOUND_SAFE)
        self.assertEqual(row.contacts.count(), 1)
        self.assertEqual(row.messages.count(), 1)
        self.assertFalse(row.contacts.filter(notified_at__isnull=False).exists())

    def test_map_annotations_round_trip_with_pin_mapping(self) -> None:
        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
        from urbanlens.dashboard.models.markup.model import MarkupMap

        location = baker.make("dashboard.Location", latitude=41.5, longitude=-74.5)
        source_pin = baker.make_recipe("dashboard.pin", profile=self.source, location=location)
        markup_map = baker.make("dashboard.MarkupMap", profile=self.source, title="Access sketch")
        baker.make("dashboard.PinMarkup", parent_map=markup_map, profile=self.source, markup_type="line", geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]})
        overlay = MapImageOverlay(profile=self.source, parent_pin=source_pin, name="Sanborn", image_url="https://example.test/s.jpg")
        overlay.set_corners([[1, 0], [1, 1], [0, 1], [0, 0]])
        overlay.save()

        target_pin = baker.make_recipe("dashboard.pin", profile=self.target, location=baker.make("dashboard.Location", latitude=41.6, longitude=-74.6))

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_map_annotations(self.source, temp_dir)
            mapping = {str(source_pin.uuid): target_pin.pk}
            _import_map_annotations(self.target, temp_dir, ImportResult(), **self._kwargs(mapping))
            _import_map_annotations(self.target, temp_dir, ImportResult(), **self._kwargs(mapping))

        maps = MarkupMap.objects.filter(profile=self.target)
        self.assertEqual(maps.count(), 1)
        self.assertEqual(maps.get().items.count(), 1)
        overlays = MapImageOverlay.objects.filter(profile=self.target)
        self.assertEqual(overlays.count(), 1)
        self.assertEqual(overlays.get().parent_pin_id, target_pin.pk)

    def test_overlay_without_a_mapped_parent_is_skipped_not_orphaned(self) -> None:
        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

        location = baker.make("dashboard.Location", latitude=41.7, longitude=-74.7)
        source_pin = baker.make_recipe("dashboard.pin", profile=self.source, location=location)
        overlay = MapImageOverlay(profile=self.source, parent_pin=source_pin, name="Orphanable", image_url="https://example.test/o.jpg")
        overlay.set_corners([[1, 0], [1, 1], [0, 1], [0, 0]])
        overlay.save()

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_map_annotations(self.source, temp_dir)
            _import_map_annotations(self.target, temp_dir, ImportResult(), **self._kwargs({}))

        self.assertFalse(MapImageOverlay.objects.filter(profile=self.target).exists())

    def test_saved_searches_round_trip_and_existing_filter_wins(self) -> None:
        from urbanlens.dashboard.models.routes.model import Route, RouteSource
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        baker.make("dashboard.SavedFilter", profile=self.source, name="ruins", criteria={"labels": ["ruin"]})
        baker.make("dashboard.Route", profile=self.source, name="approach", source=RouteSource.GPX_TRACK, path=LineString((0, 0), (1, 1), srid=4326))
        baker.make("dashboard.SavedFilter", profile=self.target, name="Ruins", criteria={"labels": ["mine"]})

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_saved_searches(self.source, temp_dir)
            _import_saved_searches(self.target, temp_dir, ImportResult(), **self._kwargs())
            _import_saved_searches(self.target, temp_dir, ImportResult(), **self._kwargs())

        filters = SavedFilter.objects.filter(profile=self.target)
        self.assertEqual(filters.count(), 1, "the user's refined filter must win over the archive's copy (case-insensitive name match)")
        self.assertEqual(filters.get().criteria, {"labels": ["mine"]})
        routes = Route.objects.filter(profile=self.target)
        self.assertEqual(routes.count(), 1)
        self.assertEqual(len(routes.get().path.coords), 2)
