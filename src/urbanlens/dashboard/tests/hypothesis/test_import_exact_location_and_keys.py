"""Imports keep each row at its exact point, never another user's Location, and stay idempotent by a per-pin key."""

from __future__ import annotations

import json
import os
import tempfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinImportSource
from urbanlens.dashboard.services.apis.locations.google.maps import _create_pin_from_confirmed
from urbanlens.dashboard.services.import_export import export as export_service, import_data
from urbanlens.dashboard.services.pins.import_keys import takeout_import_key
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to

# About 10 m north of 40.000000 at this latitude.
NEAR_LAT = 40.00009


class _Base(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = baker.make(User).profile
        self.importer = baker.make(User).profile
        self.owner_location = baker.make(Location, latitude="40.000000", longitude="-74.000000")
        baker.make(Pin, profile=self.owner, location=self.owner_location, name="Owner pin")


class TakeoutImportTests(_Base):
    def _import(self, name="Old Mill", cid=1234567890123, lat=NEAR_LAT, lng=-74.0, location=None):
        return _create_pin_from_confirmed(
            {"name": name, "description": "", "cid": cid, "label_ids": []},
            location=location,
            latitude=lat,
            longitude=lng,
            user_profile=self.importer,
            list_labels=[],
            category_label=None,
            auto_tag=False,
        )

    def test_row_near_another_users_location_gets_its_own_location_and_no_access(self) -> None:
        pin, created = self._import(cid=None)
        self.assertTrue(created)
        self.assertNotEqual(pin.location_id, self.owner_location.pk)
        self.assertEqual(float(pin.location.latitude), NEAR_LAT)
        self.assertFalse(location_visible_to(self.owner_location, self.importer))

    def test_reimporting_the_same_file_twice_creates_no_new_pins(self) -> None:
        first, created = self._import()
        self.assertTrue(created)
        second, created_again = self._import()
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Pin.objects.filter(profile=self.importer).count(), 1)
        self.assertEqual(first.import_source, PinImportSource.GOOGLE_TAKEOUT)

    def test_reimport_after_the_user_moved_the_pin_creates_no_duplicate(self) -> None:
        pin, _ = self._import()
        pin.location = baker.make(Location, latitude="41.5", longitude="-73.5")
        pin.save()
        again, created = self._import()
        self.assertFalse(created)
        self.assertEqual(again.pk, pin.pk)
        self.assertEqual(Pin.objects.filter(profile=self.importer).count(), 1)

    def test_a_changed_google_coordinate_for_the_same_cid_creates_no_duplicate(self) -> None:
        pin, _ = self._import(lat=NEAR_LAT)
        again, created = self._import(lat=NEAR_LAT + 0.01)
        self.assertFalse(created)
        self.assertEqual(again.pk, pin.pk)
        self.assertEqual(Pin.objects.filter(profile=self.importer).count(), 1)

    def test_cidless_rows_are_keyed_by_name_and_point(self) -> None:
        first, _ = self._import(cid=None, name="  Old   MILL ")
        again, created = self._import(cid=None, name="old mill")
        self.assertFalse(created)
        self.assertEqual(first.pk, again.pk)

    def test_an_unkeyed_pin_at_the_exact_location_is_adopted_and_keyed(self) -> None:
        existing = baker.make(
            Pin, profile=self.importer, location=baker.make(Location, latitude="42.0", longitude="-72.0")
        )
        pin, created = self._import(cid=None, lat=42.0, lng=-72.0)
        self.assertFalse(created)
        self.assertEqual(pin.pk, existing.pk)
        existing.refresh_from_db()
        self.assertEqual(existing.import_source, PinImportSource.GOOGLE_TAKEOUT)
        self.assertIsNotNone(existing.import_key)

    def test_key_precedence(self) -> None:
        self.assertEqual(takeout_import_key(cid=12, place_id="p", name="n", latitude=1, longitude=2), "cid:12")
        self.assertEqual(takeout_import_key(cid=None, place_id="p", name="n", latitude=1, longitude=2), "place:p")
        self.assertTrue(
            takeout_import_key(cid=None, place_id=None, name="n", latitude=1, longitude=2).startswith("ll:")
        )


class NestedPinKeyTests(_Base):
    def _import(self, **kwargs):
        return TakeoutImportTests._import(self, **kwargs)

    def test_a_key_match_finds_a_child_pin_and_leaves_its_nesting_alone(self) -> None:
        pin, _ = self._import()
        parent = baker.make(
            Pin, profile=self.importer, location=baker.make(Location, latitude="43.0", longitude="-71.0")
        )
        pin.parent_pin = parent
        pin.save()
        again, created = self._import()
        self.assertFalse(created)
        self.assertEqual(again.pk, pin.pk)
        pin.refresh_from_db()
        self.assertEqual(pin.parent_pin_id, parent.pk)

    def test_the_exact_location_fallback_never_stamps_a_child_pin(self) -> None:
        parent = baker.make(
            Pin, profile=self.importer, location=baker.make(Location, latitude="43.0", longitude="-71.0")
        )
        child = baker.make(
            Pin,
            profile=self.importer,
            parent_pin=parent,
            location=baker.make(Location, latitude="42.0", longitude="-72.0"),
        )
        found, created = self._import(cid=None, lat=42.0, lng=-72.0)
        self.assertFalse(created)
        self.assertEqual(found.pk, child.pk)
        child.refresh_from_db()
        self.assertIsNone(child.import_key)

    def test_an_orphan_google_place_holding_the_cid_does_not_crash_the_import(self) -> None:
        baker.make("dashboard.GooglePlace", latitude="10.000000", longitude="10.000000", cid=1234567890123)
        pin, created = self._import()
        self.assertTrue(created)
        self.assertIsNotNone(pin)


class JsonArchiveImportTests(_Base):
    def _import_archive(self, temp_dir: str) -> None:
        import_data._import_pins(
            self.importer, temp_dir, import_data.ImportResult(), pin_uuid_map={}, label_uuid_map={}
        )

    def test_archive_row_near_another_users_location_keeps_its_own_point(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_pins(self.owner, temp_dir)
            path = os.path.join(temp_dir, "pins.json")
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
            rows[0]["latitude"] = NEAR_LAT
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
            self._import_archive(temp_dir)
        imported = Pin.objects.get(profile=self.importer)
        self.assertNotEqual(imported.location_id, self.owner_location.pk)
        self.assertEqual(float(imported.location.latitude), NEAR_LAT)
        self.assertFalse(location_visible_to(self.owner_location, self.importer))

    def test_reimporting_an_archive_is_idempotent_even_after_the_pin_moved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_pins(self.owner, temp_dir)
            self._import_archive(temp_dir)
            self._import_archive(temp_dir)
            self.assertEqual(Pin.objects.filter(profile=self.importer).count(), 1)
            imported = Pin.objects.get(profile=self.importer)
            self.assertEqual(imported.import_source, PinImportSource.JSON_ARCHIVE)
            imported.location = baker.make(Location, latitude="41.5", longitude="-73.5")
            imported.save()
            self._import_archive(temp_dir)
        self.assertEqual(Pin.objects.filter(profile=self.importer).count(), 1)

    def _export_with_child(self, temp_dir: str) -> Pin:
        top = Pin.objects.get(profile=self.owner)
        baker.make(
            Pin,
            profile=self.owner,
            parent_pin=top,
            name="Boiler room",
            location=baker.make(Location, latitude="40.0003", longitude="-74.0003"),
        )
        export_service._export_pins(self.owner, temp_dir)
        return top

    def test_archive_children_attach_to_a_matched_child_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._export_with_child(temp_dir)
            self._import_archive(temp_dir)
            top = Pin.objects.get(profile=self.importer, parent_pin__isnull=True)
            root = baker.make(
                Pin, profile=self.importer, location=baker.make(Location, latitude="43.0", longitude="-71.0")
            )
            top.parent_pin = root
            top.save()
            self._import_archive(temp_dir)
            top.refresh_from_db()
            self.assertEqual(top.parent_pin_id, root.pk)
            self.assertEqual(Pin.objects.filter(profile=self.importer).count(), 3)
            self.assertEqual(Pin.objects.get(profile=self.importer, name="Boiler room").parent_pin_id, top.pk)

    def test_a_repeated_uuid_in_one_archive_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_pins(self.owner, temp_dir)
            path = os.path.join(temp_dir, "pins.json")
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([*rows, {**rows[0], "name": "Twin"}], fh)
            result = import_data.ImportResult()
            import_data._import_pins(self.importer, temp_dir, result, pin_uuid_map={}, label_uuid_map={})
        self.assertEqual(Pin.objects.filter(profile=self.importer).count(), 1)
        self.assertTrue(any("same uuid" in warning for warning in result.warnings))


class BackfillMigrationTests(_Base):
    def test_backfill_keys_one_root_pin_per_cid_preferring_a_current_location(self) -> None:
        import importlib

        from django.db import connection

        migration = importlib.import_module("urbanlens.dashboard.migrations.0004_backfill_pin_import_keys")
        place = baker.make("dashboard.GooglePlace", latitude="20.000000", longitude="20.000000", cid=555)
        old = baker.make(Location, latitude="20.0", longitude="20.0", google_place=place)
        Location.objects.filter(pk=old.pk).update(created="2020-01-01T00:00:00Z")
        new = baker.make(Location, latitude="20.5", longitude="20.5", google_place=place)
        old_pin = baker.make(Pin, profile=self.importer, location=old)
        new_pin = baker.make(Pin, profile=self.importer, location=new)
        migration.key_takeout_pins(None, type("Editor", (), {"connection": connection})())
        old_pin.refresh_from_db()
        new_pin.refresh_from_db()
        self.assertEqual(new_pin.import_key, "cid:555")
        self.assertEqual(new_pin.import_source, PinImportSource.GOOGLE_TAKEOUT)
        self.assertIsNone(old_pin.import_key)
