"""Tests for Location model properties and LocationQuerySet / LocationManager methods.

Pure property tests use unsaved Location instances (no DB).
Queryset and Manager tests use baker and require PostGIS.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location

_hyp = settings(max_examples=50, deadline=None)
_date_st = st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))


def _google_place(
    cached_place_name: str | None = None,
    *,
    latitude: str = "40.0",
    longitude: str = "-74.0",
    cid: int | None = None,
) -> GooglePlace:
    """Create a GooglePlace row for tests."""
    return GooglePlace.objects.create(
        latitude=latitude,
        longitude=longitude,
        cached_place_name=cached_place_name,
        cid=cid,
    )


def _loc(**kwargs) -> Location:
    """Create an unsaved Location for pure-property testing."""
    loc = Location()
    loc.date_last_active = None
    loc.date_abandoned = None
    loc.name = ""
    for k, v in kwargs.items():
        setattr(loc, k, v)
    return loc


# ── effective_date_last_active ────────────────────────────────────────────────

class LocationEffectiveDateLastActiveTests(TestCase):
    """effective_date_last_active returns date_last_active, infers from abandoned, or None."""

    def test_returns_date_last_active_when_set(self) -> None:
        d = date(2022, 6, 15)
        loc = _loc(date_last_active=d)
        self.assertEqual(loc.effective_date_last_active, d)

    def test_returns_one_day_before_abandoned_when_only_abandoned_set(self) -> None:
        abandoned = date(2021, 3, 20)
        loc = _loc(date_last_active=None, date_abandoned=abandoned)
        self.assertEqual(loc.effective_date_last_active, date(2021, 3, 19))

    def test_returns_none_when_both_fields_are_none(self) -> None:
        loc = _loc(date_last_active=None, date_abandoned=None)
        self.assertIsNone(loc.effective_date_last_active)

    def test_date_last_active_takes_priority_over_abandoned(self) -> None:
        loc = _loc(date_last_active=date(2020, 1, 10), date_abandoned=date(2020, 5, 1))
        self.assertEqual(loc.effective_date_last_active, date(2020, 1, 10))

    @given(_date_st)
    @_hyp
    def test_date_last_active_always_returned_when_set(self, d: date) -> None:
        loc = _loc(date_last_active=d, date_abandoned=None)
        self.assertEqual(loc.effective_date_last_active, d)

    @given(_date_st)
    @_hyp
    def test_abandoned_only_returns_one_day_before(self, d: date) -> None:
        loc = _loc(date_last_active=None, date_abandoned=d)
        self.assertEqual(loc.effective_date_last_active, d - timedelta(days=1))


# ── __str__ ───────────────────────────────────────────────────────────────────

class LocationStrTests(TestCase):
    """__str__ returns the name when set, or 'Location(<pk>)' as a fallback."""

    def test_named_location_returns_name(self) -> None:
        loc: Location = baker.make(Location, name="Old Factory", latitude="40.0", longitude="-74.0")
        self.assertEqual(str(loc), "Old Factory")

    def test_empty_name_falls_back_to_pk(self) -> None:
        loc: Location = baker.make(Location, name="", latitude="40.0", longitude="-74.0")
        self.assertEqual(str(loc), f"Location({loc.pk})")


# ── to_json ───────────────────────────────────────────────────────────────────

class LocationToJsonTests(TestCase):
    """to_json() serialises key fields; cached_place_name avoids the Google API."""

    def setUp(self):
        google_place = _google_place(
            "Warehouse (Google Maps)",
            latitude="40.100000",
            longitude="-74.200000",
        )
        self.loc = baker.make(
            "dashboard.Location",
            name="Abandoned Warehouse",
            latitude="40.100000",
            longitude="-74.200000",
            google_place=google_place,
        )

    def test_returns_dict(self) -> None:
        self.assertIsInstance(self.loc.to_json(), dict)

    def test_contains_all_expected_keys(self) -> None:
        keys = ("id", "name", "place_name", "description", "address", "city", "state", "country", "latitude", "longitude")
        result = self.loc.to_json()
        for key in keys:
            self.assertIn(key, result, f"Missing key: {key}")

    def test_name_matches_model(self) -> None:
        self.assertEqual(self.loc.to_json()["name"], "Abandoned Warehouse")

    def test_place_name_uses_cached_value(self) -> None:
        self.assertEqual(self.loc.to_json()["place_name"], "Warehouse (Google Maps)")

    def test_latitude_is_float(self) -> None:
        self.assertIsInstance(self.loc.to_json()["latitude"], float)

    def test_longitude_is_float(self) -> None:
        self.assertIsInstance(self.loc.to_json()["longitude"], float)

    def test_latitude_value_matches(self) -> None:
        self.assertAlmostEqual(self.loc.to_json()["latitude"], 40.1, places=3)

    def test_longitude_value_matches(self) -> None:
        self.assertAlmostEqual(self.loc.to_json()["longitude"], -74.2, places=3)


# ── has_place_name ────────────────────────────────────────────────────────────

class LocationHasPlaceNameTests(TestCase):
    """has_place_name() is True only when a meaningful place name is available."""

    def test_meaningful_cached_name_returns_true(self) -> None:
        google_place = _google_place("Steel Factory")
        loc: Location = baker.make(Location, latitude="40.0", longitude="-74.0", google_place=google_place)
        self.assertTrue(loc.has_place_name())

    def test_no_information_available_returns_false(self) -> None:
        google_place = _google_place("No Information Available")
        loc: Location = baker.make(Location, latitude="40.0", longitude="-74.0", google_place=google_place)
        self.assertFalse(loc.has_place_name())

    def test_mocked_fallback_no_information_returns_false(self) -> None:
        loc: Location = baker.make(Location, latitude="40.0", longitude="-74.0", google_place=None)
        with patch.object(Location, "get_place_name", return_value="No Information Available"):
            self.assertFalse(loc.has_place_name())

    def test_mocked_fallback_real_name_returns_true(self) -> None:
        loc: Location = baker.make(Location, latitude="40.0", longitude="-74.0", google_place=None)
        with patch.object(Location, "get_place_name", return_value="Abandoned Power Plant"):
            self.assertTrue(loc.has_place_name())


# ── slug generation ───────────────────────────────────────────────────────────

class LocationSlugTests(TestCase):
    """Locations auto-generate a unique URL slug on save."""

    def test_save_assigns_slug_from_name(self) -> None:
        loc: Location = baker.make(Location, name="Unnamed Location", latitude="40.0", longitude="-74.0", slug=None)
        self.assertEqual(loc.slug, "unnamed-location")

    def test_duplicate_names_get_numeric_suffix(self) -> None:
        first: Location = baker.make(Location, name="Unnamed Location", latitude="40.0", longitude="-74.0")
        second: Location = baker.make(Location, name="Unnamed Location", latitude="41.0", longitude="-73.0")
        self.assertEqual(first.slug, "unnamed-location")
        self.assertEqual(second.slug, "unnamed-location-2")

    def test_ensure_slug_backfills_legacy_row(self) -> None:
        loc: Location = baker.make(Location, name="Old Factory", latitude="40.0", longitude="-74.0")
        Location.objects.filter(pk=loc.pk).update(slug=None)
        loc.refresh_from_db()
        self.assertIsNone(loc.slug)
        self.assertEqual(loc.ensure_slug(), "old-factory")
        loc.refresh_from_db()
        self.assertEqual(loc.slug, "old-factory")


# ── LocationQuerySet ──────────────────────────────────────────────────────────

class LocationQuerySetTests(TestCase):
    """LocationQuerySet filter methods: by_name, by_cid, within_bounding_box."""

    def setUp(self):
        self.loc_a = baker.make(
            "dashboard.Location",
            name="Old Factory",
            latitude="40.000000",
            longitude="-74.000000",
            google_place=_google_place(None, latitude="40.000000", longitude="-74.000000", cid=12345),
        )
        self.loc_b = baker.make(
            "dashboard.Location",
            name="Abandoned Hospital",
            latitude="41.000000",
            longitude="-73.000000",
            google_place=_google_place(None, latitude="41.000000", longitude="-73.000000", cid=99999),
        )

    def test_by_name_finds_partial_case_insensitive_match(self) -> None:
        qs = Location.objects.by_name("factory")
        self.assertIn(self.loc_a, qs)
        self.assertNotIn(self.loc_b, qs)

    def test_by_name_uppercase_still_matches(self) -> None:
        qs = Location.objects.by_name("FACTORY")
        self.assertIn(self.loc_a, qs)

    def test_by_cid_finds_exact_match(self) -> None:
        qs = Location.objects.by_cid(12345)
        self.assertIn(self.loc_a, qs)
        self.assertNotIn(self.loc_b, qs)

    def test_by_cid_empty_for_nonexistent(self) -> None:
        self.assertFalse(Location.objects.by_cid(0).exists())

    def test_within_bounding_box_finds_location_at_its_own_center(self) -> None:
        qs = Location.objects.within_bounding_box(40.0, -74.0)
        self.assertIn(self.loc_a, qs)

    def test_within_bounding_box_excludes_far_away_point(self) -> None:
        # A coordinate 2 degrees away is well outside the ~50 m default bbox.
        qs = Location.objects.within_bounding_box(48.0, -74.0)
        self.assertNotIn(self.loc_a, qs)


# ── LocationManager ───────────────────────────────────────────────────────────

class LocationManagerGetForPointTests(TestCase):
    """get_for_point() resolves via bounding_box containment, falling back to proximity."""

    def setUp(self):
        self.loc = baker.make(
            "dashboard.Location", name="Target", latitude="40.000000", longitude="-74.000000",
        )

    def test_exact_coordinate_is_found(self) -> None:
        result = Location.objects.get_for_point(40.0, -74.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, self.loc.pk)

    def test_far_away_coordinate_returns_none(self) -> None:
        result = Location.objects.get_for_point(51.5, -0.1)
        self.assertIsNone(result)


class LocationManagerGetAllForPointTests(TestCase):
    """get_all_for_point() returns a queryset of all matching locations."""

    def setUp(self):
        self.loc = baker.make(
            "dashboard.Location", name="Target", latitude="40.000000", longitude="-74.000000",
        )

    def test_returns_location_at_its_own_center(self) -> None:
        qs = Location.objects.get_all_for_point(40.0, -74.0)
        self.assertIn(self.loc, qs)

    def test_far_away_returns_empty_queryset(self) -> None:
        qs = Location.objects.get_all_for_point(51.5, -0.1)
        self.assertFalse(qs.exists())


class LocationManagerGetNearbyOrCreateTests(TestCase):
    """get_nearby_or_create() finds a nearby location or creates one."""

    def setUp(self):
        self.existing = baker.make(
            "dashboard.Location", name="Nearby", latitude="40.000000", longitude="-74.000000",
        )

    def test_same_point_finds_existing_location(self) -> None:
        loc, created = Location.objects.get_nearby_or_create(
            40.0, -74.0, defaults={"name": "Should Not Be Created"},
        )
        self.assertFalse(created)
        self.assertEqual(loc.pk, self.existing.pk)

    def test_distant_point_creates_new_location(self) -> None:
        loc, created = Location.objects.get_nearby_or_create(
            51.5, -0.1, defaults={"name": "London Place"},
        )
        self.assertTrue(created)
        self.assertNotEqual(loc.pk, self.existing.pk)

    def test_created_location_is_persisted(self) -> None:
        loc, created = Location.objects.get_nearby_or_create(
            51.5, -0.1, defaults={"name": "London Place"},
        )
        self.assertTrue(Location.objects.filter(pk=loc.pk).exists())

    def test_returned_coordinates_match_requested(self) -> None:
        loc, _ = Location.objects.get_nearby_or_create(
            51.5, -0.1, defaults={"name": "London Place"},
        )
        self.assertAlmostEqual(float(loc.latitude), 51.5, places=3)
        self.assertAlmostEqual(float(loc.longitude), -0.1, places=3)

class LocationExternalNameRefreshTests(TestCase):
    """External API data can replace placeholder location names."""

    def test_google_place_name_replaces_unnamed_location(self) -> None:
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

        google_place = _google_place("Grand Central Terminal")
        loc: Location = baker.make(
            Location,
            name="Unnamed Location",
            latitude="40.752700",
            longitude="-73.977200",
            google_place=google_place,
        )

        self.assertTrue(update_location_name_from_external_sources(loc))
        loc.refresh_from_db()
        self.assertEqual(loc.name, "Grand Central Terminal")

    def test_meaningful_location_name_is_preserved(self) -> None:
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

        google_place = _google_place("External Name")
        loc: Location = baker.make(
            Location,
            name="User Curated Name",
            latitude="40.752701",
            longitude="-73.977201",
            google_place=google_place,
        )

        self.assertFalse(update_location_name_from_external_sources(loc))
        loc.refresh_from_db()
        self.assertEqual(loc.name, "User Curated Name")
