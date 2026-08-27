"""The EPA plugin's mile distances must come from the shared helpers.

``_miles_between`` used to reach into ``models.profile.model._haversine_km`` - a
*private* helper in a model module, imported from a plugin - and multiply by an
inline ``0.621371``. Both the distance and the unit conversion already existed as
shared, tested code (``services.geo.distance`` and ``services.core.units``), so
this was a third copy of one and a second copy of the other, in a layer that
should not know about either model internals or conversion constants.

The value was correct, and is asserted here against an independent reference (one
degree of latitude is ~69.09 miles on a sphere of the radius this codebase uses)
rather than against the implementation, so re-inlining a constant - or fumbling
the conversion direction, which would read as plausible until someone checked a
number - fails this test.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.profile.meta import DistanceUnit
from urbanlens.dashboard.plugins.builtin.epa_echo import _miles_between
from urbanlens.dashboard.services.core.units import km_to_display
from urbanlens.dashboard.services.geo.distance import haversine_km


class EpaEchoDistanceUnitsTests(SimpleTestCase):
    def test_one_degree_of_latitude_is_about_69_miles(self) -> None:
        """An independent reference, not a restatement of the implementation."""
        self.assertAlmostEqual(_miles_between(0.0, 0.0, 1.0, 0.0), 69.09, places=1)

    def test_it_agrees_with_the_shared_helpers(self) -> None:
        expected = km_to_display(haversine_km(41.35, -71.45, 41.36, -71.46), DistanceUnit.MILES)

        self.assertAlmostEqual(_miles_between(41.35, -71.45, 41.36, -71.46), expected, places=9)

    def test_miles_are_smaller_numbers_than_kilometres(self) -> None:
        """Catches an inverted conversion, which reads plausibly either way."""
        km = haversine_km(41.35, -71.45, 51.50, -0.12)
        miles = _miles_between(41.35, -71.45, 51.50, -0.12)

        self.assertLess(miles, km)
        self.assertGreater(miles, km * 0.5)

    def test_a_zero_distance_is_zero(self) -> None:
        self.assertAlmostEqual(_miles_between(41.35, -71.45, 41.35, -71.45), 0.0, places=9)
