"""A region drawn across the date line must match the pins inside it.

``filter_by_criteria``'s ``include_regions``/``exclude_regions`` run a planar
``__within`` (``ST_Within`` has no geography implementation, so it is evaluated
as flat degrees). Map clients report *unwrapped* coordinates when the user draws
across the antimeridian - Leaflet gives a box from 179 to 181 rather than 179 to
-179 - while stored points are always folded into [-180, 180].

Measured before the fix: a region drawn across the line matched only the pins
west of it. ``exclude_regions`` is the worse half of the same bug - a region that
matches almost nothing excludes almost nothing, so a filter meant to hide an area
quietly stops hiding it.

``split_at_antimeridian`` folds the overhanging part back to the coordinates
points are stored at. A polygon whose vertices are already folded but which spans
more than 180 degrees is left alone: written literally, those coordinates *do*
describe the long way round, and guessing otherwise would silently reinterpret a
region the user may have meant.
"""

from __future__ import annotations

from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.geo.longitude import split_at_antimeridian


def _box(west: float, east: float, south: float = -20.0, north: float = -10.0) -> MultiPolygon:
    polygon = Polygon(((west, south), (east, south), (east, north), (west, north), (west, south)))
    region = MultiPolygon(polygon)
    region.srid = 4326
    return region


class RegionFilterAntimeridianTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self._pin("west of line", 179.5, -15.0)
        self._pin("east of line", -179.5, -15.0)
        self._pin("far away", 0.0, -15.0)

    def _pin(self, name: str, longitude: float, latitude: float) -> Pin:
        location = baker.make(Location, latitude=latitude, longitude=longitude)
        return baker.make(Pin, profile=self.profile, name=name, location=location)

    def _names(self, criteria: dict) -> set[str]:
        return set(Pin.objects.filter(profile=self.profile).filter_by_criteria(criteria).values_list("name", flat=True))

    def test_an_ordinary_region_is_unaffected(self) -> None:
        """Everywhere except the date line must behave exactly as before."""
        self._pin("in region", -71.5, 41.5)

        self.assertEqual(self._names({"include_regions": _box(-72.0, -71.0, 41.0, 42.0)}), {"in region"})

    def test_an_unwrapped_region_matches_both_sides_of_the_line(self) -> None:
        self.assertEqual(self._names({"include_regions": _box(179.0, 181.0)}), {"west of line", "east of line"})

    def test_it_does_not_pick_up_the_far_side_of_the_planet(self) -> None:
        self.assertNotIn("far away", self._names({"include_regions": _box(179.0, 181.0)}))

    def test_excluding_such_a_region_hides_both_sides(self) -> None:
        """The worse half: a region that matches nothing excludes nothing."""
        self.assertEqual(self._names({"exclude_regions": _box(179.0, 181.0)}), {"far away"})

    def test_a_region_overhanging_westward_also_works(self) -> None:
        self.assertEqual(self._names({"include_regions": _box(-181.0, -179.0)}), {"west of line", "east of line"})

    def test_splitting_leaves_an_ordinary_region_untouched(self) -> None:
        region = _box(-72.0, -71.0, 41.0, 42.0)

        self.assertEqual(split_at_antimeridian(region).extent, region.extent)

    def test_splitting_folds_an_overhanging_region_into_range(self) -> None:
        split = split_at_antimeridian(_box(179.0, 181.0))

        self.assertGreaterEqual(split.extent[0], -180.0)
        self.assertLessEqual(split.extent[2], 180.0)
