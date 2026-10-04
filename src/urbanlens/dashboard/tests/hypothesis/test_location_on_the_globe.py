"""A Location is never created off the globe, whichever caller asks (P285).

``Location.latitude``/``longitude`` are ``numeric(9, 6)``, which hold up to ±999.999999, and nothing in the database
stops a latitude of 95. P282-P284 checked each route that posts or imports a coordinate; these two methods are where
every one of them ends, so a caller that forgets is refused here.
"""

from __future__ import annotations

from decimal import Decimal

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.queryset import CoordinateOffTheGlobeError

_OFF_THE_GLOBE = (
    (90.000001, 0.0),
    (-95, 0),
    (0, 180.000001),
    (0, "-500"),
    (float("inf"), 0),
    (0, float("nan")),
    ("abc", 0),
    (True, 0),
    (None, 0),
)
_EDGES = ((90, 180), (-90, -180), (Decimal(0), "0"))


class LocationOnTheGlobeTests(TestCase):
    def test_an_exact_place_off_the_globe_is_refused(self) -> None:
        for latitude, longitude in _OFF_THE_GLOBE:
            with self.subTest(latitude=latitude, longitude=longitude), self.assertRaises(CoordinateOffTheGlobeError):
                Location.objects.get_exact_or_create(latitude, longitude)

        self.assertFalse(Location.objects.exists())

    def test_a_nearby_place_off_the_globe_is_refused(self) -> None:
        for latitude, longitude in _OFF_THE_GLOBE:
            with self.subTest(latitude=latitude, longitude=longitude), self.assertRaises(CoordinateOffTheGlobeError):
                Location.objects.get_nearby_or_create(latitude, longitude, threshold_meters=25)

        self.assertFalse(Location.objects.exists())

    def test_the_edges_of_the_globe_are_places(self) -> None:
        """Anti-vacuity, and the bounds are inclusive."""
        for latitude, longitude in _EDGES:
            with self.subTest(latitude=latitude, longitude=longitude):
                location, created = Location.objects.get_exact_or_create(latitude, longitude)

                self.assertTrue(created)
                self.assertEqual((location.latitude, location.longitude), (Decimal(latitude), Decimal(longitude)))

    def test_the_refusal_is_a_value_error_callers_already_catch(self) -> None:
        self.assertTrue(issubclass(CoordinateOffTheGlobeError, ValueError))
