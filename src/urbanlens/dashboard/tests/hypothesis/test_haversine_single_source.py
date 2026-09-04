"""Every great-circle distance in the codebase must give the same answer.

Five modules had grown their own haversine - profile map centring, public-pin
clustering, consensus answer scoring, Overture boundary matching, and markup
geometry. All five agreed exactly when measured, so nothing was broken; they now
delegate to ``services.geo.distance`` so they cannot stop agreeing.

That consolidation is worth its churn because duplicated geometry primitives in
this codebase have already drifted twice: four independent longitude averages,
one of which put a user's map centre in the Atlantic, and a "nearest pin" lookup
that ordered by a geometry column while a correct distance-ordered helper sat a
few lines away in the same file. Nothing was wrong with these five - the point is
that the sixth copy is where the next bug goes.

The comparison is asserted rather than assumed, including across the
antimeridian, where the formula is naturally correct (it works on the
*difference* between longitudes) but where this codebase has repeatedly not been.
"""

from __future__ import annotations

from django.contrib.gis.geos import Point
from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.markup.model import _haversine_meters as markup_meters
from urbanlens.dashboard.models.profile.model import _haversine_km as profile_km
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import _haversine_m as overture_meters
from urbanlens.dashboard.services.consensus.fields import haversine_distance_meters
from urbanlens.dashboard.services.geo.distance import haversine_km, haversine_meters
from urbanlens.dashboard.services.pins.public_pins import _km_between as public_pins_km

_lat = st.floats(min_value=-89.0, max_value=89.0, allow_nan=False, allow_infinity=False)
_lng = st.floats(min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False)


def _all_in_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> dict[str, float]:
    return {
        "canonical": haversine_meters(lat1, lng1, lat2, lng2),
        "profile": profile_km((lat1, lng1), (lat2, lng2)) * 1000.0,
        "consensus": haversine_distance_meters(Point(lng1, lat1, srid=4326), Point(lng2, lat2, srid=4326)),
        "overture": overture_meters(lat1, lng1, lat2, lng2),
        "markup": markup_meters(lat1, lng1, lat2, lng2),
        "public_pins": public_pins_km(lat1, lng1, lat2, lng2) * 1000.0,
    }


class HaversineSingleSourceTests(SimpleTestCase):
    def test_a_short_distance_is_the_same_everywhere(self) -> None:
        values = _all_in_meters(41.35, -71.45, 41.36, -71.46)

        self.assertAlmostEqual(max(values.values()) - min(values.values()), 0.0, places=6)

    def test_a_long_distance_is_the_same_everywhere(self) -> None:
        values = _all_in_meters(41.35, -71.45, 51.50, -0.12)

        self.assertAlmostEqual(max(values.values()) - min(values.values()), 0.0, places=6)

    def test_the_antimeridian_is_the_same_everywhere(self) -> None:
        """Where this codebase's other geometry has repeatedly gone wrong."""
        values = _all_in_meters(-16.5, 179.99, -16.5, -179.99)

        self.assertAlmostEqual(max(values.values()) - min(values.values()), 0.0, places=6)

    def test_the_antimeridian_distance_is_short_not_half_the_planet(self) -> None:
        """Two points 2km apart must not read as 20,000km."""
        self.assertLess(haversine_meters(-16.5, 179.99, -16.5, -179.99), 5_000.0)

    def test_kilometres_and_metres_agree(self) -> None:
        self.assertAlmostEqual(
            haversine_km(41.35, -71.45, 51.50, -0.12) * 1000.0, haversine_meters(41.35, -71.45, 51.50, -0.12), places=6
        )

    def test_a_zero_distance_is_zero(self) -> None:
        self.assertAlmostEqual(haversine_meters(41.35, -71.45, 41.35, -71.45), 0.0, places=9)

    @given(lat1=_lat, lng1=_lng, lat2=_lat, lng2=_lng)
    @settings(max_examples=40, deadline=None)
    def test_all_implementations_agree_for_arbitrary_points(
        self, lat1: float, lng1: float, lat2: float, lng2: float
    ) -> None:
        values = _all_in_meters(lat1, lng1, lat2, lng2)
        spread = max(values.values()) - min(values.values())

        self.assertLess(spread, 1e-6, f"implementations diverged by {spread}m: {values}")

    @given(lat1=_lat, lng1=_lng, lat2=_lat, lng2=_lng)
    @settings(max_examples=40, deadline=None)
    def test_distance_is_symmetric_and_bounded(self, lat1: float, lng1: float, lat2: float, lng2: float) -> None:
        there = haversine_meters(lat1, lng1, lat2, lng2)

        self.assertAlmostEqual(there, haversine_meters(lat2, lng2, lat1, lng1), places=6)
        self.assertLessEqual(
            there, 20_100_000.0, "no two points on Earth are further apart than half its circumference"
        )
