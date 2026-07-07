"""Property-based database tests for the Pin model.

Covers PinManager.get_nearby_or_create and the major PinQuerySet filters.
Each @given example runs inside its own atomic savepoint (rolled back
automatically by hypothesis.extra.django.TestCase).
"""
from __future__ import annotations

import math

from django.contrib.auth.models import User
from urbanlens.core.tests.testcase import TestCase
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.tests.hypothesis.strategies import (
    lat_float,
    lon_float,
    nonempty_name,
    priority,
    reasonable_datetime,
    two_distant_coord_pairs,
)

_db_settings = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _make_location(lat: float = 40.0, lon: float = -74.0) -> Location:
    """Create and save a Location at the given coordinates."""
    return baker.make(Location, latitude=lat, longitude=lon)


# -- PinManager.get_nearby_or_create -------------------------------------------

class GetNearbyOrCreateNullGuardsTests(TestCase):
    """get_nearby_or_create must return (None, False) for degenerate inputs."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_none_lat_none_lon(self) -> None:
        pin, created = Pin.objects.get_nearby_or_create(None, None, self.profile)
        self.assertIsNone(pin)
        self.assertFalse(created)

    def test_none_lat_valid_lon(self) -> None:
        pin, created = Pin.objects.get_nearby_or_create(None, -73.75, self.profile)
        self.assertIsNone(pin)
        self.assertFalse(created)

    def test_valid_lat_none_lon(self) -> None:
        pin, created = Pin.objects.get_nearby_or_create(42.65, None, self.profile)
        self.assertIsNone(pin)
        self.assertFalse(created)

    @given(
        lat=st.one_of(
            st.just(float("nan")),
            st.just(float("inf")),
            st.just(float("-inf")),
        ),
        lon=lon_float,
    )
    @_db_settings
    def test_nan_or_inf_latitude_returns_none(self, lat: float, lon: float) -> None:
        pin, created = Pin.objects.get_nearby_or_create(lat, lon, self.profile)
        self.assertIsNone(pin)
        self.assertFalse(created)

    @given(
        lat=lat_float,
        lon=st.one_of(
            st.just(float("nan")),
            st.just(float("inf")),
            st.just(float("-inf")),
        ),
    )
    @_db_settings
    def test_nan_or_inf_longitude_returns_none(self, lat: float, lon: float) -> None:
        pin, created = Pin.objects.get_nearby_or_create(lat, lon, self.profile)
        self.assertIsNone(pin)
        self.assertFalse(created)

    @given(lat=st.text(min_size=1, max_size=10), lon=st.text(min_size=1, max_size=10))
    @_db_settings
    def test_non_numeric_string_coordinates_return_none(self, lat: str, lon: str) -> None:
        assume(not _is_numeric(lat) or not _is_numeric(lon))
        pin, created = Pin.objects.get_nearby_or_create(lat, lon, self.profile)
        self.assertIsNone(pin)
        self.assertFalse(created)


def _is_numeric(s: str) -> bool:
    try:
        v = float(s)
        return not (math.isnan(v) or math.isinf(v))
    except (ValueError, TypeError):
        return False


class GetNearbyOrCreateCreationTests(TestCase):
    """get_nearby_or_create creates a valid pin for well-formed coordinates."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    @given(lat=lat_float, lon=lon_float)
    @_db_settings
    def test_creates_new_pin_for_fresh_coordinates(self, lat: float, lon: float) -> None:
        pin, created = Pin.objects.get_nearby_or_create(lat, lon, self.profile)
        self.assertIsNotNone(pin)
        self.assertTrue(created, "Expected a new pin to be created")
        self.assertEqual(pin.profile, self.profile)

    @given(lat=lat_float, lon=lon_float)
    @_db_settings
    def test_created_pin_has_correct_coordinates(self, lat: float, lon: float) -> None:
        pin, created = Pin.objects.get_nearby_or_create(lat, lon, self.profile)
        self.assertTrue(created)
        self.assertAlmostEqual(float(pin.latitude), lat, places=4)
        self.assertAlmostEqual(float(pin.longitude), lon, places=4)

    @given(lat=lat_float, lon=lon_float)
    @_db_settings
    def test_second_call_same_coords_returns_existing(self, lat: float, lon: float) -> None:
        """Calling again at exactly the same coordinates must NOT create a second pin."""
        first_pin, first_created = Pin.objects.get_nearby_or_create(lat, lon, self.profile)
        self.assertTrue(first_created)
        second_pin, second_created = Pin.objects.get_nearby_or_create(lat, lon, self.profile)
        self.assertFalse(second_created)
        self.assertEqual(first_pin.pk, second_pin.pk)

    @given(two_distinct=two_distant_coord_pairs())
    @_db_settings
    def test_distant_coordinates_create_independent_pins(
        self,
        two_distinct: tuple[tuple[float, float], tuple[float, float]],
    ) -> None:
        """Two locations > 1° apart must always produce two distinct pins."""
        (lat1, lon1), (lat2, lon2) = two_distinct
        pin_a, created_a = Pin.objects.get_nearby_or_create(lat1, lon1, self.profile)
        pin_b, created_b = Pin.objects.get_nearby_or_create(lat2, lon2, self.profile)
        self.assertTrue(created_a)
        self.assertTrue(created_b)
        self.assertNotEqual(pin_a.pk, pin_b.pk)


# -- PinQuerySet structural filters --------------------------------------------

class PinQuerySetRootPinsTests(TestCase):
    """root_pins() must exclude all sub-pin variants."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    @given(n_root=st.integers(min_value=0, max_value=5), n_child=st.integers(min_value=0, max_value=5))
    @_db_settings
    def test_root_pins_count_excludes_detail_pins(self, n_root: int, n_child: int) -> None:
        """root_pins() count must equal exactly the number of top-level pins."""
        roots = [baker.make(Pin, profile=self.profile, parent_pin=None, parent_wiki=None) for _ in range(n_root)]
        for root in roots[:n_child]:
            baker.make(Pin, profile=self.profile, parent_pin=root)
        root_count = Pin.objects.filter(profile=self.profile).root_pins().count()
        self.assertEqual(root_count, n_root)

    @given(n=st.integers(min_value=1, max_value=5))
    @_db_settings
    def test_detail_pins_are_excluded_from_root_pins(self, n: int) -> None:
        parent = baker.make(Pin, profile=self.profile, parent_pin=None, parent_wiki=None)
        children = [baker.make(Pin, profile=self.profile, parent_pin=parent) for _ in range(n)]
        root_qs = Pin.objects.filter(profile=self.profile).root_pins()
        child_ids = {c.pk for c in children}
        root_ids = set(root_qs.values_list("pk", flat=True))
        self.assertTrue(child_ids.isdisjoint(root_ids), "Detail pins must not appear in root_pins()")

    @given(n=st.integers(min_value=1, max_value=5))
    @_db_settings
    def test_detail_pins_queryset_excludes_roots(self, n: int) -> None:
        parent = baker.make(Pin, profile=self.profile, parent_pin=None, parent_wiki=None)
        children = {baker.make(Pin, profile=self.profile, parent_pin=parent).pk for _ in range(n)}
        detail_ids = set(Pin.objects.filter(profile=self.profile).detail_pins().values_list("pk", flat=True))
        self.assertEqual(detail_ids, children)


# -- PinQuerySet visit filters -------------------------------------------------

class PinQuerySetVisitFiltersTests(TestCase):
    """never_visited() and related visit filters."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    @given(
        n_visited=st.integers(min_value=0, max_value=5),
        n_unvisited=st.integers(min_value=0, max_value=5),
    )
    @_db_settings
    def test_never_visited_returns_only_unvisited_pins(
        self,
        n_visited: int,
        n_unvisited: int,
    ) -> None:
        for _ in range(n_visited):
            baker.make(Pin, profile=self.profile, last_visited=baker.random_gen.gen_datetime())
        unvisited_ids = {baker.make(Pin, profile=self.profile, last_visited=None).pk for _ in range(n_unvisited)}
        qs = Pin.objects.filter(profile=self.profile).never_visited()
        returned_ids = set(qs.values_list("pk", flat=True))
        self.assertEqual(returned_ids, unvisited_ids)

    @given(n=st.integers(min_value=1, max_value=6))
    @_db_settings
    def test_never_visited_has_no_last_visited_date(self, n: int) -> None:
        for _ in range(n):
            baker.make(Pin, profile=self.profile, last_visited=None)
        qs = Pin.objects.filter(profile=self.profile).never_visited()
        self.assertFalse(qs.filter(last_visited__isnull=False).exists())


# -- PinQuerySet name filter ---------------------------------------------------

class PinQuerySetByNameTests(TestCase):
    """by_name() performs a case-insensitive substring search on name."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    @given(nonempty_name)
    @_db_settings
    def test_pin_is_found_by_exact_name(self, name: str) -> None:
        assume(len(name.strip()) >= 1)
        pin = baker.make(Pin, profile=self.profile, name=name)
        qs = Pin.objects.filter(profile=self.profile).by_name(name)
        self.assertIn(pin.pk, qs.values_list("pk", flat=True))

    @given(nonempty_name)
    @_db_settings
    def test_pin_is_found_by_lowercase_name(self, name: str) -> None:
        assume(len(name.strip()) >= 1)
        pin = baker.make(Pin, profile=self.profile, name=name)
        qs = Pin.objects.filter(profile=self.profile).by_name(name.lower())
        self.assertIn(pin.pk, qs.values_list("pk", flat=True))

    @given(nonempty_name)
    @_db_settings
    def test_pin_is_found_by_uppercase_name(self, name: str) -> None:
        assume(len(name.strip()) >= 1)
        pin = baker.make(Pin, profile=self.profile, name=name)
        qs = Pin.objects.filter(profile=self.profile).by_name(name.upper())
        self.assertIn(pin.pk, qs.values_list("pk", flat=True))



# -- PinQuerySet priority filter -----------------------------------------------

class PinQuerySetPriorityTests(TestCase):
    """by_priority() is an exact-match filter."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    @given(priority)
    @_db_settings
    def test_by_priority_returns_only_matching_pins(self, prio: int) -> None:
        target = baker.make(Pin, profile=self.profile, priority=prio)
        # Decoy with a different priority value.
        decoy_prio = prio + 1
        baker.make(Pin, profile=self.profile, priority=decoy_prio)
        qs = Pin.objects.filter(profile=self.profile).by_priority(prio)
        self.assertIn(target.pk, qs.values_list("pk", flat=True))
        self.assertNotIn(
            decoy_prio,
            qs.values_list("priority", flat=True),
            "by_priority must not return pins with a different priority",
        )
