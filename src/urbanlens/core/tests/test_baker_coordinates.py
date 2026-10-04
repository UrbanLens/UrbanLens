"""A baked location is on the globe (P50).

Baker filled ``Location.latitude`` with up to 99.999999, past the pole for about one location in ten, and code that
checks a coordinate refused those at random: an export/import round trip lost its pin once in ten runs.
"""

from __future__ import annotations

from decimal import Decimal

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class BakedCoordinatesTests(TestCase):
    def test_every_baked_latitude_is_on_the_globe(self) -> None:
        locations = baker.make(Location, _quantity=300)
        locations += [baker.make(Pin).location for _ in range(30)]

        latitudes = [location.latitude for location in locations]
        self.assertTrue(all(isinstance(latitude, Decimal) for latitude in latitudes))
        self.assertEqual([latitude for latitude in latitudes if not 0 <= latitude < 90], [])

    def test_a_latitude_the_test_gives_is_kept(self) -> None:
        self.assertEqual(baker.make(Location, latitude=Decimal("-41.5")).latitude, Decimal("-41.5"))
