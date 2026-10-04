"""P188: the Wikipedia panel's campus fallback reads the places a building stands in, never its owner's pin nesting.

The match lands in the Location's shared ``wikipedia`` row, so whichever article one owner's own nesting picked was
the article every viewer of the place saw.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation
from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaPanelSource

_MODULE = "urbanlens.dashboard.plugins.builtin.wikipedia"
CAMPUS_ARTICLE = {"title": "Hudson River State Hospital", "url": "https://en.wikipedia.org/wiki/HRSH", "extract": "x"}


class CampusFallbackTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        campus = Place.objects.create(kind=PlaceKind.PARCEL, name="Hudson River State Hospital")
        building = Place.objects.create(kind=PlaceKind.BUILDING, parent=campus, parent_relation=PlaceRelation.PART_OF)
        self.campus_location = Location.objects.create(latitude="41.733000", longitude="-73.915000", place=campus)
        self.building_location = Location.objects.create(latitude="41.739000", longitude="-73.905000", place=building)
        no_address = dict.fromkeys(("locality", "route", "street_number", "administrative_area_level_1"), "")
        self.enterContext(mock.patch(f"{_MODULE}.match_address_components", return_value=no_address))
        for seeding in ("seed_pin_from_cached_wikipedia", "apply_wikipedia_cover_if_missing"):
            self.enterContext(mock.patch(f"urbanlens.dashboard.services.wiki.wiki_seed.{seeding}"))
        self.searched: list[tuple[float, float]] = []

    def _lookup(self, lat: float, lng: float, *_args, **_kwargs) -> dict | None:
        self.searched.append((round(lat, 3), round(lng, 3)))
        return CAMPUS_ARTICLE if (round(lat, 3), round(lng, 3)) == (41.733, -73.915) else None

    def _fetch(self, pin: Pin) -> dict:
        with mock.patch(
            "urbanlens.dashboard.services.apis.assets.wikipedia.WikipediaGateway.get_article_for_location",
            side_effect=self._lookup,
        ):
            WikipediaPanelSource().fetch(pin)
        return LocationCache.objects.get(location=pin.location, source="wikipedia", audience="").data

    def test_a_building_finds_its_campus_article_through_the_place_it_stands_in(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=self.building_location, parent_pin=None)

        self.assertEqual(self._fetch(pin).get("title"), CAMPUS_ARTICLE["title"])

    def test_the_owner_s_own_nesting_is_never_searched(self) -> None:
        """A pin filed under a pin elsewhere must not pick the article every viewer of this place sees."""
        loner = Location.objects.create(latitude="40.100000", longitude="-75.100000")
        elsewhere = baker.make(Pin, profile=self.profile, location=self.campus_location, parent_pin=None)
        pin = baker.make(Pin, profile=self.profile, location=loner, parent_pin=elsewhere)

        data = self._fetch(pin)

        self.assertEqual(data.get("title", ""), "")
        self.assertEqual(self.searched, [(40.1, -75.1)])
