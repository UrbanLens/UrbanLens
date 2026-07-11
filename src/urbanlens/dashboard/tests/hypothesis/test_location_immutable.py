"""Tests for Location identity immutability (the save() guard in Location.save()).

The DB-trigger layer (migration 0009) is enforced by PostgreSQL and exercised
implicitly by any code path that bypasses save(); these tests cover the
application-level guard, which raises a clean ValueError before the write.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location

_LAT = Decimal("41.500000")
_LNG = Decimal("-73.500000")


class LocationImmutabilityTests(TestCase):
    """Location.save() refuses to mutate identity fields on an existing row."""

    def _location(self, **overrides):
        data = {"latitude": _LAT, "longitude": _LNG, "official_name": "Original"}
        data.update(overrides)
        return baker.make("dashboard.Location", **data)

    def test_changing_latitude_raises(self):
        location = self._location()
        location.latitude = Decimal("42.000000")
        with self.assertRaises(ValueError):
            location.save()

    def test_changing_longitude_raises(self):
        location = self._location()
        location.longitude = Decimal("-72.000000")
        with self.assertRaises(ValueError):
            location.save()

    def test_address_components_stay_mutable(self):
        """Address is geocoded metadata, not identity - backfilling/editing it is allowed."""
        location = self._location(locality=None, route=None)
        location.locality = "Poughkeepsie"  # backfill an empty component
        location.route = "Main St"
        location.save()
        location.refresh_from_db()
        self.assertEqual(location.locality, "Poughkeepsie")
        self.assertEqual(location.route, "Main St")

    def test_equal_float_reassignment_does_not_raise(self):
        """Assigning the same coordinate as a float must not trip the Decimal guard."""
        location = self._location()
        location.latitude = float(_LAT)  # 41.5 as float, numerically identical
        location.official_name = "Renamed"
        location.save()  # should not raise
        location.refresh_from_db()
        self.assertEqual(location.official_name, "Renamed")

    def test_mutating_cache_field_is_allowed(self):
        location = self._location()
        location.official_name = "New external name"
        location.save()
        location.refresh_from_db()
        self.assertEqual(location.official_name, "New external name")

    def test_slug_regeneration_still_works(self):
        """regenerate_slug() saves with update_fields=['slug']; identity is untouched."""
        location = self._location()
        original_lat = location.latitude
        location.regenerate_slug()
        location.refresh_from_db()
        self.assertTrue(location.slug)
        self.assertEqual(location.latitude, original_lat)

    def test_update_fields_excluding_identity_skips_check(self):
        """A save scoped to non-identity fields writes even if an identity attr diverged in memory."""
        location = self._location()
        location.latitude = Decimal("99.000000")  # diverged in memory, but not being written
        location.official_name = "Scoped update"
        location.save(update_fields=["official_name"])
        location.refresh_from_db()
        self.assertEqual(location.official_name, "Scoped update")
        self.assertEqual(location.latitude, _LAT)  # unchanged on disk

    def test_get_nearby_or_create_makes_a_new_row_for_new_coords(self):
        """The intended pattern: distinct coordinates yield a distinct Location."""
        first = self._location()
        second, created = Location.objects.get_nearby_or_create(latitude=10.0, longitude=20.0)
        self.assertTrue(created)
        self.assertNotEqual(first.pk, second.pk)

    @settings(max_examples=25, deadline=None)
    @given(new_lat=st.decimals(min_value=-89, max_value=89, places=6).filter(lambda d: d != _LAT))
    def test_any_latitude_change_raises(self, new_lat):
        location = self._location()
        location.latitude = new_lat
        with self.assertRaises(ValueError):
            location.save()
