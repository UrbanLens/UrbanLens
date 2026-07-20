"""Tests for Location model properties and LocationQuerySet / LocationManager methods.

Pure property tests use unsaved Location instances (no DB).
Queryset and Manager tests use baker and require PostGIS.
"""
from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location


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


# -- __str__ -------------------------------------------------------------------

class LocationStrTests(TestCase):
    """__str__ returns the official_name when set, or 'Location(<pk>)' as a fallback."""

    def test_named_location_returns_official_name(self) -> None:
        loc: Location = baker.make(Location, official_name="Old Factory", latitude="40.0", longitude="-74.0")
        self.assertEqual(str(loc), "Old Factory")

    def test_empty_name_falls_back_to_pk(self) -> None:
        loc: Location = baker.make(Location, official_name="", latitude="40.0", longitude="-74.0")
        self.assertEqual(str(loc), f"Location({loc.pk})")


# -- to_json -------------------------------------------------------------------

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
            official_name="Abandoned Warehouse",
            latitude="40.100000",
            longitude="-74.200000",
            google_place=google_place,
        )

    def test_returns_dict(self) -> None:
        self.assertIsInstance(self.loc.to_json(), dict)

    def test_contains_all_expected_keys(self) -> None:
        keys = ("id", "official_name", "place_name", "address", "city", "state", "country", "latitude", "longitude")
        result = self.loc.to_json()
        for key in keys:
            self.assertIn(key, result, f"Missing key: {key}")

    def test_official_name_matches_model(self) -> None:
        self.assertEqual(self.loc.to_json()["official_name"], "Abandoned Warehouse")

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


# -- has_place_name ------------------------------------------------------------

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

    def test_no_cached_name_returns_false(self) -> None:
        """place_name is cache-only (see its docstring) - no GooglePlace row at
        all means has_place_name() is False, not a live fallback lookup."""
        loc: Location = baker.make(Location, latitude="40.0", longitude="-74.0", google_place=None)
        self.assertFalse(loc.has_place_name())

    def test_no_cached_name_never_calls_get_place_name(self) -> None:
        """Regression guard: has_place_name()/place_name must never trigger a
        live Google call from a plain property access - see
        tasks.resolve_location_place_name for where that now happens instead."""
        loc: Location = baker.make(Location, latitude="40.0", longitude="-74.0", google_place=None)
        with patch.object(Location, "get_place_name", return_value="Abandoned Power Plant") as mock_get:
            self.assertFalse(loc.has_place_name())
            mock_get.assert_not_called()


# -- slug generation -----------------------------------------------------------

class LocationSlugTests(TestCase):
    """Locations auto-generate a unique URL slug on save."""

    def test_save_assigns_slug_from_name(self) -> None:
        loc: Location = baker.make(Location, official_name="Unnamed Location", latitude="40.0", longitude="-74.0", slug=None)
        self.assertEqual(loc.slug, "unnamed-location")

    def test_duplicate_names_get_numeric_suffix(self) -> None:
        # _generate_slug() appends a random (not sequential) numeric suffix on
        # collision, to avoid a race between concurrent writers reading the
        # same "next available" counter.
        first: Location = baker.make(Location, official_name="Unnamed Location", latitude="40.0", longitude="-74.0")
        second: Location = baker.make(Location, official_name="Unnamed Location", latitude="41.0", longitude="-73.0")
        self.assertEqual(first.slug, "unnamed-location")
        self.assertNotEqual(second.slug, first.slug)
        self.assertRegex(second.slug, r"^unnamed-location-\d+$")

    def test_ensure_slug_backfills_legacy_row(self) -> None:
        loc: Location = baker.make(Location, official_name="Old Factory", latitude="40.0", longitude="-74.0")
        Location.objects.filter(pk=loc.pk).update(slug=None)
        loc.refresh_from_db()
        self.assertIsNone(loc.slug)
        self.assertEqual(loc.ensure_slug(), "old-factory")
        loc.refresh_from_db()
        self.assertEqual(loc.slug, "old-factory")


# -- LocationQuerySet ----------------------------------------------------------

class LocationQuerySetTests(TestCase):
    """LocationQuerySet filter methods: by_name, by_cid, within_bounding_box."""

    def setUp(self):
        self.loc_a = baker.make(
            "dashboard.Location",
            official_name="Old Factory",
            latitude="40.000000",
            longitude="-74.000000",
            google_place=_google_place(None, latitude="40.000000", longitude="-74.000000", cid=12345),
        )
        self.loc_b = baker.make(
            "dashboard.Location",
            official_name="Abandoned Hospital",
            latitude="41.000000",
            longitude="-73.000000",
            google_place=_google_place(None, latitude="41.000000", longitude="-73.000000", cid=99999),
        )

    def test_by_official_name_finds_partial_case_insensitive_match(self) -> None:
        qs = Location.objects.by_official_name("factory")
        self.assertIn(self.loc_a, qs)
        self.assertNotIn(self.loc_b, qs)

    def test_by_official_name_uppercase_still_matches(self) -> None:
        qs = Location.objects.by_official_name("FACTORY")
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


# -- LocationManager -----------------------------------------------------------

class LocationManagerGetForPointTests(TestCase):
    """get_for_point() resolves via bounding_box containment, falling back to proximity."""

    def setUp(self):
        self.loc = baker.make(
            "dashboard.Location", official_name="Target", latitude="40.000000", longitude="-74.000000",
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
            "dashboard.Location", official_name="Target", latitude="40.000000", longitude="-74.000000",
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
            "dashboard.Location", official_name="Nearby", latitude="40.000000", longitude="-74.000000",
        )

    def test_same_point_finds_existing_location(self) -> None:
        loc, created = Location.objects.get_nearby_or_create(
            40.0, -74.0, defaults={"official_name":"Should Not Be Created"},
        )
        self.assertFalse(created)
        self.assertEqual(loc.pk, self.existing.pk)

    def test_distant_point_creates_new_location(self) -> None:
        loc, created = Location.objects.get_nearby_or_create(
            51.5, -0.1, defaults={"official_name":"London Place"},
        )
        self.assertTrue(created)
        self.assertNotEqual(loc.pk, self.existing.pk)

    def test_created_location_is_persisted(self) -> None:
        loc, _created = Location.objects.get_nearby_or_create(
            51.5, -0.1, defaults={"official_name":"London Place"},
        )
        self.assertTrue(Location.objects.filter(pk=loc.pk).exists())

    def test_returned_coordinates_match_requested(self) -> None:
        loc, _ = Location.objects.get_nearby_or_create(
            51.5, -0.1, defaults={"official_name":"London Place"},
        )
        self.assertAlmostEqual(float(loc.latitude), 51.5, places=3)
        self.assertAlmostEqual(float(loc.longitude), -0.1, places=3)


class LocationExternalNameRefreshTests(TestCase):
    """External API data refreshes Location.official_name and the wiki's name/aliases."""

    def test_google_place_name_replaces_unnamed_wiki(self) -> None:
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

        google_place = _google_place("Grand Central Terminal")
        loc: Location = baker.make(
            Location,
            latitude="40.752700",
            longitude="-73.977200",
            google_place=google_place,
        )
        wiki = baker.make("dashboard.Wiki", location=loc, name="Unnamed Location")

        self.assertTrue(update_location_name_from_external_sources(loc))
        loc.refresh_from_db()
        wiki.refresh_from_db()
        self.assertEqual(loc.official_name, "Grand Central Terminal")
        self.assertEqual(wiki.name, "Grand Central Terminal")

    def test_meaningful_wiki_name_is_preserved(self) -> None:
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

        google_place = _google_place("External Name")
        loc: Location = baker.make(
            Location,
            latitude="40.752701",
            longitude="-73.977201",
            google_place=google_place,
        )
        wiki = baker.make("dashboard.Wiki", location=loc, name="User Curated Name")

        self.assertTrue(update_location_name_from_external_sources(loc))
        loc.refresh_from_db()
        wiki.refresh_from_db()
        self.assertEqual(loc.official_name, "External Name")
        self.assertEqual(wiki.name, "User Curated Name")
        # The alias list holds every known name, including the current one.
        self.assertCountEqual(list(wiki.aliases.values_list("name", flat=True)), ["User Curated Name", "External Name"])

    def test_external_names_are_added_as_wiki_aliases(self) -> None:
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

        loc: Location = baker.make(
            Location,
            latitude="40.752702",
            longitude="-73.977202",
        )
        wiki = baker.make("dashboard.Wiki", location=loc, name="Curated Mill")

        self.assertTrue(
            update_location_name_from_external_sources(
                loc,
                extra_candidates=[("wikipedia", "Old Mill"), ("nps", "Historic Mill")],
            ),
        )
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Curated Mill")
        self.assertCountEqual(list(wiki.aliases.values_list("name", flat=True)), ["Curated Mill", "Old Mill", "Historic Mill"])

    def test_promoted_external_name_is_recorded_as_wiki_alias(self) -> None:
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

        loc: Location = baker.make(
            Location,
            latitude="40.752703",
            longitude="-73.977203",
        )
        wiki = baker.make("dashboard.Wiki", location=loc, name="Unnamed Location")

        self.assertTrue(
            update_location_name_from_external_sources(
                loc,
                extra_candidates=[("google_places", "Grand Hall"), ("wikipedia", "Grand Hall Museum")],
            ),
        )
        loc.refresh_from_db()
        wiki.refresh_from_db()
        # Google Places is fallback-only (see naming._FALLBACK_ONLY_SOURCES) and is
        # dropped outright once another source (wikipedia) has a candidate, so
        # "Grand Hall" never enters the pipeline at all - no tie to break.
        self.assertEqual(loc.official_name, "Grand Hall Museum")
        self.assertEqual(wiki.name, "Grand Hall Museum")
        # The promoted name is itself an alias (the list includes the current name).
        self.assertCountEqual(list(wiki.aliases.values_list("name", flat=True)), ["Grand Hall Museum"])
