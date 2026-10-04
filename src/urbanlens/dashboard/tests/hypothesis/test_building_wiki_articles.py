"""A building's wiki carries no Wikipedia article: the article at a building's point is its campus's (P262).

The Wikipedia panel stores the campus's article on a child pin's location when the pin's own point finds none, and
any match at a building's point is the campus's or a neighbour's (``name_tiers.describes_scope``). The cache write
seeded the building's wiki from it: on dev, 11 of HRSH's child wikis opened with the campus's article.
"""

from __future__ import annotations

import importlib

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import (
    EDIT_SUMMARY_IMAGES_LOCALIZED,
    EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA,
    Article,
    ArticleRevision,
)
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import PlaceKind, PlaceRelation
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki.articles import save_article
from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting
from urbanlens.dashboard.services.wiki.wiki_seed import is_untouched_wikipedia_seed, seed_wiki_article_from_wikipedia

from .place_helpers import make_place

migration = importlib.import_module("urbanlens.dashboard.migrations.0053_building_wikis_drop_wikipedia_seed")

_LAT, _LNG = 41.7333, -73.9281
_CAMPUS_ARTICLE = {
    "title": "Hudson River State Hospital",
    "extract": "<p>The <b>Hudson River State Hospital</b> is a former psychiatric hospital.</p>",
    "url": "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital",
    "thumbnail": "",
}
_SEED = "The **Hudson River State Hospital** is a former psychiatric hospital.\n\n_From Wikipedia_"


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


class _ArticleRows(TestCase):
    """Article rows written directly, as the migration finds them."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self._seq = 0
        self.campus = self.wiki()

    def wiki(self, *, parent: Wiki | None = None, place=None) -> Wiki:
        self._seq += 1
        location = baker.make(
            Location, latitude=f"{40.0 + self._seq * 0.01:.6f}", longitude="-74.000000", google_place=None
        )
        return Wiki.objects.create(location=location, place=place, name=f"Wiki {self._seq}", parent_wiki=parent)

    def article(self, wiki: Wiki, revisions: list[tuple[object, str, str]], *, content: str | None = None) -> Article:
        article = Article.objects.create(wiki=wiki, content=content if content is not None else revisions[-1][2])
        for editor, summary, text in revisions:
            ArticleRevision.objects.create(article=article, editor=editor, edit_summary=summary, content=text)
        return article


_LOCALIZED = _SEED.replace("_From", "![HRSH](/dashboard/map/media-copy/abc/)\n\n_From")
_REMOTE = _SEED.replace("_From", "![HRSH](https://upload.wikimedia.org/hrsh.jpg)\n\n_From")


class MigrationTests(_ArticleRows):
    def _migrate(self) -> None:
        migration.drop_building_wikipedia_seeds(_historical_apps(), None)

    def test_an_untouched_seed_on_a_building_wiki_is_removed(self) -> None:
        article = self.article(self.wiki(parent=self.campus), [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED)])

        self._migrate()

        self.assertFalse(Article.objects.filter(pk=article.pk).exists())
        self.assertFalse(ArticleRevision.objects.filter(article_id=article.pk).exists())

    def test_a_seed_whose_images_were_only_stored_here_is_removed(self) -> None:
        """Dev's 11: the seed, then the image-localising command's revision."""
        article = self.article(
            self.wiki(parent=self.campus),
            [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _REMOTE), (None, EDIT_SUMMARY_IMAGES_LOCALIZED, _LOCALIZED)],
        )

        self._migrate()

        self.assertFalse(Article.objects.filter(pk=article.pk).exists())

    def test_a_seed_on_a_child_wiki_holding_a_building_is_removed(self) -> None:
        parcel = make_place(PlaceKind.PARCEL, _square(39.0, -74.0, 0.002))
        building = make_place(PlaceKind.BUILDING, _square(39.0, -74.0, 0.0002), parent=parcel)
        make_place(PlaceKind.BUILDING, None, parent=parcel)
        article = self.article(
            self.wiki(parent=self.campus, place=building), [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED)]
        )

        self._migrate()

        self.assertFalse(Article.objects.filter(pk=article.pk).exists())

    def test_human_content_is_kept(self) -> None:
        building = self.wiki(parent=self.campus)
        kept = [
            self.article(
                self.wiki(parent=self.campus),
                [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED), (self.profile, "Boiler house", _SEED + " More.")],
            ),
            # A person whose account was deleted since: no editor, and not a system summary.
            self.article(
                self.wiki(parent=self.campus),
                [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED), (None, "Fixed a typo", _SEED + " Typo.")],
            ),
            # The localising command's summary over text that changed beyond its images.
            self.article(
                self.wiki(parent=self.campus),
                [
                    (None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _REMOTE),
                    (None, EDIT_SUMMARY_IMAGES_LOCALIZED, _LOCALIZED + "!"),
                ],
            ),
            # Edited outside a revision.
            self.article(
                self.wiki(parent=self.campus), [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED)], content=_SEED + " X"
            ),
            # Written by a person from the start.
            self.article(self.wiki(parent=self.campus), [(self.profile, "", "A person's article.")]),
        ]
        last_edited = self.article(building, [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED)])
        Article.objects.filter(pk=last_edited.pk).update(last_edited_by=self.profile)
        kept.append(last_edited)

        self._migrate()

        self.assertEqual(
            set(Article.objects.filter(pk__in=[a.pk for a in kept]).values_list("pk", flat=True)), {a.pk for a in kept}
        )

    def test_a_seed_that_may_be_the_wikis_own_is_kept(self) -> None:
        site_parcel = make_place(PlaceKind.PARCEL, _square(38.0, -74.0, 0.001))
        house = make_place(PlaceKind.BUILDING, _square(37.0, -74.0, 0.0002), parent=make_place(PlaceKind.PARCEL, None))
        kept = [
            self.article(self.campus, [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED)]),
            self.article(
                self.wiki(parent=self.campus, place=site_parcel), [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED)]
            ),
            # The one building of an ordinary property.
            self.article(
                self.wiki(parent=self.campus, place=house), [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED)]
            ),
        ]

        self._migrate()

        self.assertEqual(Article.objects.filter(pk__in=[a.pk for a in kept]).count(), 3)


class RuntimeAgreesWithTheMigrationTests(_ArticleRows):
    """``wiki_seed.is_untouched_wikipedia_seed`` and the migration's copy of it judge every case alike."""

    def test_both_judge_alike(self) -> None:
        cases = [
            [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED)],
            [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _REMOTE), (None, EDIT_SUMMARY_IMAGES_LOCALIZED, _LOCALIZED)],
            [
                (None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _REMOTE),
                (None, EDIT_SUMMARY_IMAGES_LOCALIZED, _LOCALIZED + "!"),
            ],
            [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED), (self.profile, "Boiler house", _SEED + " More.")],
            [(None, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA, _SEED), (None, "Fixed a typo", _SEED + " Typo.")],
            [(None, "", "A person's article.")],
        ]
        articles = [self.article(self.wiki(parent=self.campus), revisions) for revisions in cases]
        untouched = {article.pk for article in articles if is_untouched_wikipedia_seed(article)}

        migration.drop_building_wikipedia_seeds(_historical_apps(), None)

        removed = {article.pk for article in articles} - set(Article.objects.values_list("pk", flat=True))
        self.assertEqual(removed, untouched)
        self.assertEqual(len(untouched), 2)


_HISTORICAL_APPS = None


def _historical_apps():
    """The models as the migration sees them under ``migrate``."""
    global _HISTORICAL_APPS  # noqa: PLW0603 - rendering the state takes seconds; one per test run
    if _HISTORICAL_APPS is None:
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        node = ("dashboard", migration.Migration.dependencies[0][1])
        _HISTORICAL_APPS = MigrationExecutor(connection).loader.project_state(node).apps
    return _HISTORICAL_APPS
