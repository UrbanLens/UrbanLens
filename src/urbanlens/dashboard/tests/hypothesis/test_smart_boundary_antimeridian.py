"""A smart list whose boundary crosses the date line must still match its pins.

Chunk 125 fixed `filter_by_criteria`'s `include_regions`/`exclude_regions` to split
regions at the antimeridian, but `PinList.smart_boundary` is a *separate* field
queried by its own `__within` calls, and those were missed. There are two of them,
and they answer different questions:

- `_pin_in_boundary` decides whether one pin belongs, on every pin save;
- `_boundary_matching_ids` resolves the whole list during a resync.

Both had to be fixed, or a pin would join the list on save and vanish on the next
resync (or the reverse), which is worse than either being wrong consistently.

The unwrapped form (Leaflet gives 179 to 181 when the user draws across the line)
is the one that actually arrives. The ordinary case is asserted alongside, because
a boundary filter that changed behaviour for the rest of the world would be a far
worse bug than this one.
"""

from __future__ import annotations

from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.pin_list_membership import _boundary_matching_ids, _pin_in_boundary


def _region(west: float, east: float, south: float = -20.0, north: float = -10.0) -> MultiPolygon:
    region = MultiPolygon(Polygon(((west, south), (east, south), (east, north), (west, north), (west, south))))
    region.srid = 4326
    return region


class SmartBoundaryAntimeridianTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _pin(self, name: str, longitude: float, latitude: float = -15.0) -> Pin:
        return baker.make(
            Pin, profile=self.profile, name=name, location=baker.make(Location, latitude=latitude, longitude=longitude)
        )

    def _list(self, region: MultiPolygon) -> PinList:
        return baker.make(PinList, profile=self.profile, name="Across the line", smart_boundary=region)

    def test_an_ordinary_boundary_still_matches(self) -> None:
        pin = self._pin("inside", -71.5, 41.5)
        pin_list = self._list(_region(-72.0, -71.0, 41.0, 42.0))

        self.assertTrue(_pin_in_boundary(pin, pin_list))
        self.assertEqual(_boundary_matching_ids(pin_list), {pin.pk})

    def test_a_pin_east_of_the_line_belongs(self) -> None:
        pin = self._pin("east of line", -179.5)
        pin_list = self._list(_region(179.0, 181.0))

        self.assertTrue(_pin_in_boundary(pin, pin_list), "the per-pin check missed it")

    def test_the_resync_finds_both_sides(self) -> None:
        west = self._pin("west of line", 179.5)
        east = self._pin("east of line", -179.5)
        self._pin("far away", 0.0)
        pin_list = self._list(_region(179.0, 181.0))

        self.assertEqual(_boundary_matching_ids(pin_list), {west.pk, east.pk})

    def test_both_paths_agree(self) -> None:
        """They answer the same question at different times; disagreeing means a
        pin joins on save and vanishes on resync."""
        east = self._pin("east of line", -179.5)
        pin_list = self._list(_region(179.0, 181.0))

        self.assertEqual(_pin_in_boundary(east, pin_list), east.pk in _boundary_matching_ids(pin_list))

    def test_the_far_side_of_the_planet_is_excluded(self) -> None:
        far = self._pin("far away", 0.0)
        pin_list = self._list(_region(179.0, 181.0))

        self.assertFalse(_pin_in_boundary(far, pin_list))
        self.assertNotIn(far.pk, _boundary_matching_ids(pin_list))

    def test_a_list_with_no_boundary_matches_nothing(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="No boundary", smart_boundary=None)

        self.assertEqual(_boundary_matching_ids(pin_list), set())
