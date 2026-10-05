"""A wiki made before its location's boundary arrived becomes the property's page once the boundary does.

The pin's save creates the wiki at once; the parcel comes later from the provider chain. The wiki stayed placeless,
so a second pin elsewhere on the parcel got a wiki of its own and the campus's building wikis were never given out.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries, generate_location_boundaries
from urbanlens.dashboard.tests.hypothesis.building_fixtures import CAMPUS_LAT, CAMPUS_LNG, offset, parcel_square

_CHAIN = "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries"


def _parcel() -> MultiPolygon:
    return MultiPolygon(GEOSGeometry(json.dumps(parcel_square()), srid=4326), srid=4326)


class WikiClaimsItsParcelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=CAMPUS_LAT, longitude=CAMPUS_LNG, place=None)
        self.wiki = Wiki.objects.get_or_create_for_location(self.location)[0]

    def generate(self, location: Location) -> Place | None:
        with mock.patch(_CHAIN, return_value=ResolvedBoundaries(property_polygon=_parcel())):
            return generate_location_boundaries(location)

    def test_the_wiki_takes_the_parcel_its_location_now_stands_on(self) -> None:
        self.assertIsNone(self.wiki.place_id)

        place = self.generate(self.location)

        self.wiki.refresh_from_db()
        self.assertEqual(place.kind, PlaceKind.PARCEL)
        self.assertEqual(self.wiki.place_id, place.pk)

    def test_a_second_pin_on_the_parcel_shares_the_wiki(self) -> None:
        self.generate(self.location)
        latitude, longitude = offset(-80, 60)
        elsewhere = baker.make(Location, latitude=latitude, longitude=longitude, place=None)
        self.generate(elsewhere)

        self.assertEqual(Wiki.objects.get_or_create_for_location(elsewhere), (self.wiki, False))

    def test_a_parcel_another_wiki_holds_is_left_to_nesting(self) -> None:
        latitude, longitude = offset(-80, 60)
        holder_location = baker.make(Location, latitude=latitude, longitude=longitude, place=None)
        holder = Wiki.objects.get_or_create_for_location(holder_location)[0]
        place = self.generate(holder_location)
        holder.refresh_from_db()
        self.assertEqual(holder.place_id, place.pk)

        self.generate(self.location)

        self.wiki.refresh_from_db()
        self.assertIsNone(self.wiki.place_id)
