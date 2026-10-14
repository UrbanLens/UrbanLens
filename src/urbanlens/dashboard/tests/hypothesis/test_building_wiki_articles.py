"""
A building's wiki carries no Wikipedia article: the article at a building's point is its campus's.

The Wikipedia panel stores the campus's article on a child pin's location when the pin's own point finds none, and
any match at a building's point is the campus's or a neighbour's (``name_tiers.describes_scope``). The cache write
seeded the building's wiki from it: on dev, 11 of HRSH's child wikis opened with the campus's article.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import (
    Article,
)
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import PlaceKind, PlaceRelation
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki.articles import save_article
from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting
from urbanlens.dashboard.services.wiki.wiki_seed import seed_wiki_article_from_wikipedia

from .place_helpers import make_place

_LAT, _LNG = 41.7333, -73.9281
_CAMPUS_ARTICLE = {
    "title": "Hudson River State Hospital",
    "extract": "<p>The <b>Hudson River State Hospital</b> is a former psychiatric hospital.</p>",
    "url": "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital",
    "thumbnail": "",
}
_SEED = "The **Hudson River State Hospital** is a former psychiatric hospital.\n\n_From Wikipedia_"
_HOLD_SECONDS = 1.5


def _square(latitude: float, longitude: float, half: float) -> MultiPolygon:
    ring = (
        (longitude - half, latitude - half),
        (longitude + half, latitude - half),
        (longitude + half, latitude + half),
        (longitude - half, latitude + half),
        (longitude - half, latitude - half),
    )
    return MultiPolygon(Polygon(ring), srid=4326)


class _Campus(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.parcel = make_place(PlaceKind.PARCEL, _square(_LAT, _LNG, 0.002))
        for _ in range(2):
            make_place(PlaceKind.BUILDING, None, parent=self.parcel)
        self.campus_location = self.location_at(_LAT, _LNG)
        self.campus_wiki, _ = Wiki.objects.get_or_create_for_location(self.campus_location)
        self.building_location = self.location_at(_LAT + 0.001, _LNG + 0.001)
        self.building_wiki = Wiki.objects.create(
            location=self.building_location, place=None, name="Building", parent_wiki=self.campus_wiki
        )

    def location_at(self, latitude: float, longitude: float) -> Location:
        location = baker.make(Location, latitude=f"{latitude:.6f}", longitude=f"{longitude:.6f}", google_place=None)
        location.refresh_from_db()
        return location

    def cache_campus_article(self, location: Location) -> None:
        LocationCache.objects.create(location=location, source="wikipedia", data=_CAMPUS_ARTICLE)


class SeedingTests(_Campus):
    def test_a_building_wiki_is_not_seeded(self) -> None:
        self.cache_campus_article(self.building_location)

        self.assertIsNone(seed_wiki_article_from_wikipedia(self.building_location))
        self.assertFalse(Article.objects.filter(wiki=self.building_wiki).exists())

    def test_the_cache_write_neither_seeds_nor_links_a_building_wiki(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(self.building_location, "wikipedia", _CAMPUS_ARTICLE, query_key="Hudson River")

        self.assertFalse(Article.objects.filter(wiki=self.building_wiki).exists())
        self.assertFalse(self.building_wiki.links.exists())

    def test_a_child_wiki_holding_a_building_place_is_not_seeded(self) -> None:
        building = make_place(PlaceKind.BUILDING, _square(_LAT - 0.001, _LNG, 0.0002), parent=self.parcel)
        location = self.location_at(_LAT - 0.001, _LNG)
        wiki = Wiki.objects.create(location=location, place=building, name="Held", parent_wiki=self.campus_wiki)
        self.cache_campus_article(location)

        self.assertIsNone(seed_wiki_article_from_wikipedia(location))
        self.assertFalse(Article.objects.filter(wiki=wiki).exists())

    def test_the_building_of_an_ordinary_property_is_still_seeded(self) -> None:
        """One building on one parcel: the article at its point is the property's, which the building is."""
        parcel = make_place(PlaceKind.PARCEL, _square(_LAT - 0.01, _LNG, 0.001))
        house = make_place(PlaceKind.BUILDING, _square(_LAT - 0.01, _LNG, 0.0002), parent=parcel)
        parcel_wiki = Wiki.objects.create(location=self.location_at(_LAT - 0.0108, _LNG), place=parcel, name="Lot")
        location = self.location_at(_LAT - 0.01, _LNG)
        house_wiki = Wiki.objects.create(location=location, place=house, name="House", parent_wiki=parcel_wiki)
        self.cache_campus_article(location)

        article = seed_wiki_article_from_wikipedia(location)

        self.assertEqual(article.wiki_id, house_wiki.pk)

    def test_the_campus_wiki_is_still_seeded(self) -> None:
        self.cache_campus_article(self.campus_location)

        article = seed_wiki_article_from_wikipedia(self.campus_location)

        self.assertEqual(article.wiki_id, self.campus_wiki.pk)

    def test_a_parcel_nested_under_its_site_is_still_seeded(self) -> None:
        """A property of its own: its point's article may be its own."""
        site = make_place(PlaceKind.SITE, _square(_LAT + 0.01, _LNG, 0.004))
        parcel = make_place(
            PlaceKind.PARCEL, _square(_LAT + 0.01, _LNG, 0.001), parent=site, relation=PlaceRelation.MEMBER_OF
        )
        site_wiki = Wiki.objects.create(location=self.location_at(_LAT + 0.013, _LNG), place=site, name="Site")
        location = self.location_at(_LAT + 0.01, _LNG)
        parcel_wiki = Wiki.objects.create(location=location, place=parcel, name="Parcel", parent_wiki=site_wiki)
        self.cache_campus_article(location)

        article = seed_wiki_article_from_wikipedia(location)

        self.assertEqual(article.wiki_id, parcel_wiki.pk)


class NestingTests(_Campus):
    """A root wiki seeded from its own point, later nested under its campus's."""

    def setUp(self) -> None:
        super().setUp()
        building = make_place(PlaceKind.BUILDING, _square(_LAT - 0.001, _LNG, 0.0002), parent=self.parcel)
        self.location = self.location_at(_LAT - 0.001, _LNG)
        self.root = Wiki.objects.create(location=self.location, place=building, name="Root")
        self.cache_campus_article(self.location)
        self.article = seed_wiki_article_from_wikipedia(self.location)

    def test_the_fixture_seeds_the_root(self) -> None:
        self.assertIsNotNone(self.article)
        self.assertIsNone(self.root.parent_wiki_id)

    def test_nesting_drops_the_untouched_seed(self) -> None:
        reconcile_wiki_nesting(self.root)

        self.root.refresh_from_db()
        self.assertEqual(self.root.parent_wiki_id, self.campus_wiki.pk)
        self.assertFalse(Article.objects.filter(wiki=self.root).exists())

    def test_nesting_keeps_an_article_someone_edited(self) -> None:
        save_article(editor=self.profile, content=self.article.content + "\n\nThe boiler house.", wiki=self.root)

        reconcile_wiki_nesting(self.root)

        self.assertTrue(Article.objects.filter(wiki=self.root).exists())
