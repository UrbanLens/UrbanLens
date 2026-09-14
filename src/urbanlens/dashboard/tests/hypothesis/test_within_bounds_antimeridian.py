"""A map viewport that crosses the date line must return the pins on screen."""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class WithinBoundsAntimeridianTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _pin(self, name: str, latitude: float, longitude: float) -> Pin:
        location = baker.make(Location, latitude=latitude, longitude=longitude)
        return baker.make(Pin, profile=self.profile, location=location, name=name)

    def _names(self, south: float, west: float, north: float, east: float) -> set[str]:
        return set(
            Pin.objects.filter(profile=self.profile)
            .within_bounds(south, west, north, east)
            .values_list("name", flat=True)
        )

    def test_an_ordinary_viewport_is_unaffected(self) -> None:
        """The rest of the planet must behave exactly as before."""
        self._pin("inside", 41.5, -71.5)
        self._pin("outside", 41.5, -60.0)

        self.assertEqual(self._names(41.0, -72.0, 42.0, -71.0), {"inside"})

    def test_a_viewport_crossing_the_date_line_returns_what_is_on_screen(self) -> None:
        self._pin("west of line", -15.0, 179.5)
        self._pin("east of line", -15.0, -179.5)
        self._pin("far away", -15.0, 0.0)

        self.assertEqual(self._names(-20.0, 179.0, -10.0, -179.0), {"west of line", "east of line"})

    def test_a_crossing_viewport_excludes_the_far_side_of_the_planet(self) -> None:
        """The symptom of the old inversion: everything *but* the viewport."""
        self._pin("far away", -15.0, 0.0)

        self.assertEqual(self._names(-20.0, 179.0, -10.0, -179.0), set())

    def test_unwrapped_bounds_from_the_map_client_still_match(self) -> None:
        """Leaflet reports east=181 rather than -179 when panned across."""
        self._pin("east of line", -15.0, -179.5)

        self.assertEqual(self._names(-20.0, 179.0, -10.0, 181.0), {"east of line"})

    def test_latitude_still_bounds_a_crossing_viewport(self) -> None:
        """Crossing the line must not widen the box in the other axis."""
        self._pin("in latitude", -15.0, 179.5)
        self._pin("too far north", 40.0, 179.5)

        self.assertEqual(self._names(-20.0, 179.0, -10.0, -179.0), {"in latitude"})

    def test_a_viewport_ending_exactly_on_the_line_keeps_its_side(self) -> None:
        self._pin("just west", -15.0, 179.9)

        self.assertEqual(self._names(-20.0, 179.0, -10.0, 180.0), {"just west"})
