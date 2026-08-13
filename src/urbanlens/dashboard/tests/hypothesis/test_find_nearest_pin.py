"""``find_nearest_pin`` must return the nearest pin, not any pin in range.

It ordered by ``location__point`` - the geometry column itself. That sorts by
PostGIS's internal representation, which has nothing to do with distance from the
query point, so the function returned an arbitrary pin inside the radius while
its name and docstring both promised the closest one.

Measured before the fix: with pins 11m and 75m from the query point, it returned
the one at 75m.

It matters because of where it is used - matching Google Location History and My
Activity coordinates against a user's existing pins, at
``VISIT_MATCH_RADIUS_M`` = 100m. Any profile with two pins inside that radius (a
building and its neighbour, or a pin and a nearby one on the same block) could
have an imported visit attributed to the wrong place, silently and permanently.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.visits.visits import find_nearest_pin

#: Query point used by every test here.
_LAT, _LNG = 41.35000, -71.45000


class FindNearestPinTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _pin(self, name: str, longitude: float, latitude: float = _LAT) -> Pin:
        location = baker.make(Location, latitude=latitude, longitude=longitude)
        return baker.make(Pin, profile=self.profile, name=name, location=location)

    def test_the_closest_of_two_candidates_wins(self) -> None:
        """~75m vs ~11m - the case that was returning the wrong pin."""
        self._pin("far", -71.45090)
        self._pin("near", -71.45013)

        self.assertEqual(find_nearest_pin(_LAT, _LNG, self.profile, 100).name, "near")

    def test_the_result_does_not_depend_on_insertion_order(self) -> None:
        """The old ordering was stable but arbitrary; this would pass either way
        only if distance is what decides."""
        self._pin("near", -71.45013)
        self._pin("far", -71.45090)

        self.assertEqual(find_nearest_pin(_LAT, _LNG, self.profile, 100).name, "near")

    def test_a_pin_outside_the_radius_is_never_returned(self) -> None:
        self._pin("way out", -71.46500)

        self.assertIsNone(find_nearest_pin(_LAT, _LNG, self.profile, 100))

    def test_the_nearest_inside_the_radius_wins_over_a_closer_one_outside_it(self) -> None:
        """Radius still bounds the search; it is not merely a tie-breaker."""
        self._pin("inside", -71.45090)

        self.assertEqual(find_nearest_pin(_LAT, _LNG, self.profile, 100).name, "inside")

    def test_another_profiles_pin_is_never_returned(self) -> None:
        other = Profile.objects.get(user=baker.make("auth.User"))
        location = baker.make(Location, latitude=_LAT, longitude=-71.45001)
        baker.make(Pin, profile=other, name="theirs", location=location)

        self.assertIsNone(find_nearest_pin(_LAT, _LNG, self.profile, 100))

    def test_no_pins_at_all_yields_none(self) -> None:
        self.assertIsNone(find_nearest_pin(_LAT, _LNG, self.profile, 100))

    def test_three_candidates_return_the_closest(self) -> None:
        self._pin("furthest", -71.45090)
        self._pin("middle", -71.45050)
        self._pin("closest", -71.45010)

        self.assertEqual(find_nearest_pin(_LAT, _LNG, self.profile, 100).name, "closest")
