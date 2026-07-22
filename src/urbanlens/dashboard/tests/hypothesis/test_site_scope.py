"""Tests for parcel-vs-building scope (services.locations.site_scope).

Scope decides whether a pin/wiki is described by *its own* building records or
by the buildings nested under it. The rules are small but load-bearing - every
building-level panel consults them - so they are pinned down here: an explicit
user choice always wins, and otherwise the count of child markers typed as
buildings decides.

No external services are involved; the parcel-buildings lookups these tests
exercise read the LocationCache directly.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.locations.site_scope import (
    BUILDING_MATCH_METERS,
    MULTI_BUILDING_THRESHOLD,
    PARCEL_BUILDINGS_CACHE_SOURCE,
    building_child_count,
    classify_building_pin_type,
    has_multiple_buildings,
    is_site_scope,
    looks_like_a_building,
    meters_between,
    nearest_building,
    parcel_buildings,
)

_coord_counter = 0


def _make_location(**kwargs) -> Location:
    """A Location with unique coordinates (they are unique per lat/lng)."""
    global _coord_counter
    _coord_counter += 1
    kwargs.setdefault("latitude", 42.0 + _coord_counter * 0.001)
    kwargs.setdefault("longitude", -73.0 - _coord_counter * 0.001)
    return baker.make(Location, google_place=None, **kwargs)


def _footprint_around(latitude: float, longitude: float) -> str:
    """A closed square (~100 m a side) centred on a coordinate, as WKT.

    Stands in for a real building footprint landed by the boundary provider
    chain - what ``looks_like_a_building`` treats as proof of a building.
    """
    d = 0.0005
    ring = [
        (longitude - d, latitude - d),
        (longitude + d, latitude - d),
        (longitude + d, latitude + d),
        (longitude - d, latitude + d),
        (longitude - d, latitude - d),
    ]
    coords = ", ".join(f"{lng} {lat}" for lng, lat in ring)
    return f"MULTIPOLYGON((({coords})))"


def _make_pin(profile, **kwargs) -> Pin:
    """A pin with its own coordinate-bearing Location."""
    location = kwargs.pop("location", None) or _make_location()
    return baker.make(Pin, profile=profile, location=location, **kwargs)


class MetersBetweenTests(SimpleTestCase):
    """The cheap equirectangular distance used for building matching."""

    def test_identical_points_are_zero_apart(self) -> None:
        self.assertEqual(meters_between(42.0, -73.0, 42.0, -73.0), 0.0)

    def test_one_thousandth_of_a_degree_latitude_is_about_111_metres(self) -> None:
        self.assertAlmostEqual(meters_between(42.0, -73.0, 42.001, -73.0), 111.32, places=1)

    @given(
        lat_a=st.floats(min_value=-60, max_value=60, allow_nan=False),
        lng_a=st.floats(min_value=-179, max_value=179, allow_nan=False),
        lat_b=st.floats(min_value=-60, max_value=60, allow_nan=False),
        lng_b=st.floats(min_value=-179, max_value=179, allow_nan=False),
    )
    @settings(max_examples=200)
    def test_is_symmetric_and_non_negative(self, lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> None:
        forward = meters_between(lat_a, lng_a, lat_b, lng_b)
        self.assertGreaterEqual(forward, 0.0)
        self.assertAlmostEqual(forward, meters_between(lat_b, lng_b, lat_a, lng_a), places=6)


class NearestBuildingTests(SimpleTestCase):
    """Picking the building a coordinate belongs to."""

    near = {"name": "Near", "latitude": 42.0, "longitude": -73.0}
    far = {"name": "Far", "latitude": 42.5, "longitude": -73.5}

    def test_picks_the_closest(self) -> None:
        self.assertEqual(nearest_building([self.far, self.near], 42.0, -73.0), self.near)

    def test_empty_list_yields_none(self) -> None:
        self.assertIsNone(nearest_building([], 42.0, -73.0))

    def test_buildings_without_coordinates_are_skipped(self) -> None:
        self.assertEqual(nearest_building([{"name": "Unlocated"}, self.near], 42.0, -73.0), self.near)

    def test_within_meters_rejects_a_distant_best_match(self) -> None:
        self.assertIsNone(nearest_building([self.far], 42.0, -73.0, within_meters=BUILDING_MATCH_METERS))

    def test_within_meters_accepts_a_close_match(self) -> None:
        self.assertEqual(nearest_building([self.near], 42.00005, -73.0, within_meters=BUILDING_MATCH_METERS), self.near)


class SiteScopeRuleTests(TestCase):
    """is_site_scope: explicit choice first, child building count second."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")
        self.pin = _make_pin(self.profile)

    def _add_children(self, count: int, pin_type: str = PinType.BUILDING) -> None:
        for _ in range(count):
            _make_pin(self.profile, parent_pin=self.pin, pin_type=pin_type)

    def test_a_lone_pin_is_not_site_scope(self) -> None:
        self.assertFalse(is_site_scope(self.pin))

    def test_one_building_child_is_below_the_threshold(self) -> None:
        self._add_children(MULTI_BUILDING_THRESHOLD - 1)
        self.assertFalse(is_site_scope(self.pin))

    def test_threshold_building_children_make_it_a_parcel(self) -> None:
        self._add_children(MULTI_BUILDING_THRESHOLD)
        self.assertTrue(is_site_scope(self.pin))

    def test_non_building_children_never_flip_scope(self) -> None:
        """Entrances, hazards, and landmarks are not buildings, however many there are."""
        for pin_type in (PinType.ENTRANCE, PinType.POINT_OF_INTEREST, PinType.DANGER, PinType.OTHER):
            with self.subTest(pin_type=pin_type):
                pin = _make_pin(self.profile)
                for _ in range(MULTI_BUILDING_THRESHOLD + 3):
                    _make_pin(self.profile, parent_pin=pin, pin_type=pin_type)
                self.assertFalse(is_site_scope(pin))

    def test_explicit_parcel_is_site_scope_with_no_children_at_all(self) -> None:
        pin = _make_pin(self.profile, pin_type=PinType.PARCEL, pin_type_is_user_provided=True)
        self.assertTrue(is_site_scope(pin))

    def test_explicit_building_stays_building_despite_building_children(self) -> None:
        """A deliberate choice outranks the count - the user knows their own site."""
        pin = _make_pin(self.profile, pin_type=PinType.BUILDING, pin_type_is_user_provided=True)
        for _ in range(MULTI_BUILDING_THRESHOLD + 2):
            _make_pin(self.profile, parent_pin=pin, pin_type=PinType.BUILDING)
        self.assertFalse(is_site_scope(pin))

    def test_an_unflagged_parcel_type_still_falls_through_to_counting(self) -> None:
        """pin_type=PARCEL without the flag was guessed, so it doesn't get to decide."""
        pin = _make_pin(self.profile, pin_type=PinType.PARCEL, pin_type_is_user_provided=False)
        self.assertFalse(is_site_scope(pin))

    def test_result_is_memoized_on_the_instance(self) -> None:
        self.assertFalse(is_site_scope(self.pin))
        self._add_children(MULTI_BUILDING_THRESHOLD)
        self.assertFalse(is_site_scope(self.pin), "the cached answer should be reused within one request")
        self.assertTrue(is_site_scope(Pin.objects.get(pk=self.pin.pk)))

    def test_unsaved_pin_has_no_children(self) -> None:
        self.assertEqual(building_child_count(Pin()), 0)


class WikiSiteScopeTests(TestCase):
    """The same rules apply to community wikis, counting child wikis."""

    def test_child_building_wikis_make_a_wiki_a_parcel(self) -> None:
        wiki = baker.make(Wiki, location=_make_location())
        for _ in range(MULTI_BUILDING_THRESHOLD):
            baker.make(Wiki, location=_make_location(), parent_wiki=wiki, pin_type=PinType.BUILDING)
        self.assertTrue(is_site_scope(wiki))

    def test_a_plain_wiki_is_not_site_scope(self) -> None:
        self.assertFalse(is_site_scope(baker.make(Wiki, location=_make_location())))


class ParcelBuildingsCacheTests(TestCase):
    """parcel_buildings reads the cache and never fetches."""

    def setUp(self) -> None:
        super().setUp()
        self.location = _make_location()

    def test_never_fetched_yields_none(self) -> None:
        self.assertIsNone(parcel_buildings(self.location))

    def test_no_location_yields_none(self) -> None:
        self.assertIsNone(parcel_buildings(None))

    def test_fetched_but_empty_yields_empty_list(self) -> None:
        """"We looked and found nothing" is a different answer from "we never looked"."""
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {})
        self.assertEqual(parcel_buildings(self.location), [])

    def test_cached_buildings_are_returned(self) -> None:
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": [{"name": "A"}, {"name": "B"}]})
        self.assertEqual(len(parcel_buildings(self.location) or []), 2)

    def test_has_multiple_buildings_applies_the_threshold(self) -> None:
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": [{"name": "A"}]})
        self.assertFalse(has_multiple_buildings(self.location))
        LocationCache.set(
            self.location,
            PARCEL_BUILDINGS_CACHE_SOURCE,
            {"buildings": [{"name": f"B{i}"} for i in range(MULTI_BUILDING_THRESHOLD)]},
        )
        self.assertTrue(has_multiple_buildings(self.location))


class ClassificationTests(TestCase):
    """Automatic building classification of child markers."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")

    def _location_on_a_footprint(self) -> Location:
        """A location whose generated BUILDING boundary exists - i.e. it is on a building."""
        location = _make_location()
        Boundary.objects.create(
            location=location,
            boundary_type=BoundaryType.BUILDING,
            generated_polygon=_footprint_around(float(location.latitude), float(location.longitude)),
        )
        return location

    def test_a_marker_on_a_building_footprint_is_a_building(self) -> None:
        pin = _make_pin(self.profile, location=self._location_on_a_footprint(), pin_type=PinType.POINT_OF_INTEREST)
        self.assertTrue(classify_building_pin_type(pin))
        pin.refresh_from_db()
        self.assertEqual(pin.pin_type, PinType.BUILDING)
        self.assertFalse(pin.pin_type_is_user_provided, "classifying must not pretend the user chose this")

    def test_a_marker_off_any_building_is_left_alone(self) -> None:
        pin = _make_pin(self.profile, pin_type=PinType.POINT_OF_INTEREST)
        self.assertFalse(classify_building_pin_type(pin))
        pin.refresh_from_db()
        self.assertEqual(pin.pin_type, PinType.POINT_OF_INTEREST)

    def test_a_user_chosen_type_is_never_overwritten(self) -> None:
        pin = _make_pin(
            self.profile,
            location=self._location_on_a_footprint(),
            pin_type=PinType.ENTRANCE,
            pin_type_is_user_provided=True,
        )
        self.assertFalse(classify_building_pin_type(pin))
        pin.refresh_from_db()
        self.assertEqual(pin.pin_type, PinType.ENTRANCE)

    def test_an_already_classified_building_is_not_rewritten(self) -> None:
        pin = _make_pin(self.profile, location=self._location_on_a_footprint(), pin_type=PinType.BUILDING)
        self.assertFalse(classify_building_pin_type(pin))

    def test_proximity_to_a_known_building_classifies_without_a_footprint(self) -> None:
        """Sources that publish only a centroid still support classification."""
        location = _make_location()
        LocationCache.set(
            location,
            PARCEL_BUILDINGS_CACHE_SOURCE,
            {"buildings": [{"name": "Shed", "latitude": float(location.latitude) + 0.00001, "longitude": float(location.longitude)}]},
        )
        pin = _make_pin(self.profile, location=location, pin_type=PinType.POINT_OF_INTEREST)
        self.assertTrue(classify_building_pin_type(pin))

    def test_a_distant_known_building_does_not_classify(self) -> None:
        location = baker.make(Location, latitude="42.000000", longitude="-73.000000", google_place=None)
        LocationCache.set(location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": [{"name": "Shed", "latitude": 42.01, "longitude": -73.0}]})
        pin = _make_pin(self.profile, location=location, pin_type=PinType.POINT_OF_INTEREST)
        self.assertFalse(classify_building_pin_type(pin))

    def test_looks_like_a_building_tolerates_a_missing_location(self) -> None:
        self.assertFalse(looks_like_a_building(None))

    def test_classified_children_flip_their_parent_to_parcel_scope(self) -> None:
        """The end-to-end point of classification: the parent stops being a building."""
        parent = _make_pin(self.profile)
        for _ in range(MULTI_BUILDING_THRESHOLD):
            child = _make_pin(self.profile, parent_pin=parent, location=self._location_on_a_footprint(), pin_type=PinType.POINT_OF_INTEREST)
            classify_building_pin_type(child)
        self.assertTrue(is_site_scope(Pin.objects.get(pk=parent.pk)))
