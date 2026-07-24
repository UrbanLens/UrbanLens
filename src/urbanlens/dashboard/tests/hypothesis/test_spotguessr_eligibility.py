"""Tests for services.spotguessr.eligibility - "pinned by every participant"."""

from __future__ import annotations

from itertools import count

from django.contrib.gis.geos import Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.spotguessr.eligibility import eligible_locations

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class EligibleLocationsTests(TestCase):
    def setUp(self) -> None:
        self.alice = _make_profile()
        self.bob = _make_profile()

    def test_no_profiles_returns_nothing(self) -> None:
        self.assertFalse(eligible_locations([]).exists())

    def test_solo_player_sees_their_own_pins(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        self.assertEqual(list(eligible_locations([self.alice])), [location])

    def test_solo_player_never_sees_locations_they_havent_pinned(self) -> None:
        _make_location()
        self.assertEqual(list(eligible_locations([self.alice])), [])

    def test_location_must_be_pinned_by_every_participant(self) -> None:
        both_pinned = _make_location()
        only_alice = _make_location()
        baker.make(Pin, profile=self.alice, location=both_pinned)
        baker.make(Pin, profile=self.bob, location=both_pinned)
        baker.make(Pin, profile=self.alice, location=only_alice)

        self.assertEqual(list(eligible_locations([self.alice, self.bob])), [both_pinned])

    def test_require_visited_by_all_excludes_pinned_but_unvisited(self) -> None:
        location = _make_location()
        pin = baker.make(Pin, profile=self.alice, location=location)

        self.assertEqual(list(eligible_locations([self.alice], require_visited_by_all=True)), [])

        baker.make(PinVisit, pin=pin, visited_at=timezone.now())
        self.assertEqual(list(eligible_locations([self.alice], require_visited_by_all=True)), [location])

    def test_exclude_location_ids_removes_already_used_locations(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        self.assertEqual(list(eligible_locations([self.alice], exclude_location_ids=[location.pk])), [])

    def test_geo_bounds_restricts_to_locations_inside_the_polygon(self) -> None:
        inside = baker.make(Location, latitude="42.650000", longitude="-73.760000")
        outside = baker.make(Location, latitude="10.000000", longitude="10.000000")
        baker.make(Pin, profile=self.alice, location=inside)
        baker.make(Pin, profile=self.alice, location=outside)

        bounds = Polygon.from_bbox((-74.0, 42.0, -73.0, 43.0))
        bounds.srid = 4326
        self.assertEqual(list(eligible_locations([self.alice], geo_bounds=bounds)), [inside])
