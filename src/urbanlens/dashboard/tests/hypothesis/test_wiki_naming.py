"""A community wiki is named from public sources, never from a private pin, and never over a person."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, WikiAutoRemoval
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.revision import WikiFieldRevision
from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources
from urbanlens.dashboard.services.wiki.wiki_naming import adopt_public_name, is_provisional_name

_HRSH = "Hudson River State Hospital"
_PRIVATE = "e2e private campus notes"
_ARTICLE = {
    "title": _HRSH,
    "extract": "<p>The Hudson River State Hospital is a former psychiatric hospital in Poughkeepsie, New York.</p>",
    "url": "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital",
}


def _latest_name_source(wiki: Wiki) -> str | None:
    return (
        WikiFieldRevision.objects.filter(target_id=wiki.pk, field_name="name")
        .order_by("-id")
        .values_list("source", flat=True)
        .first()
    )


class AdoptPublicNameTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.73328, longitude=-73.92812, official_name="")

    def _wiki(self, name: str) -> Wiki:
        with writing_as(WriteSource.AUTOMATIC):
            return baker.make(Wiki, location=self.location, name=name)

    def test_a_placeholder_takes_the_public_name(self) -> None:
        wiki = self._wiki("Unnamed Location")

        self.assertTrue(adopt_public_name(wiki, _HRSH, source="wikipedia"))

        wiki.refresh_from_db()
        self.assertEqual(wiki.name, _HRSH)
        self.assertEqual(_latest_name_source(wiki), WriteSource.AUTOMATIC)

    def test_the_adopted_name_is_one_of_the_wikis_aliases_credited_to_its_source(self) -> None:
        wiki = self._wiki("Unnamed Location")

        adopt_public_name(wiki, _HRSH, source="wikipedia")

        alias = WikiAlias.objects.get(wiki=wiki, name__iexact=_HRSH)
        self.assertEqual(alias.source, "wikipedia")

    def test_a_name_a_person_wrote_is_never_replaced(self) -> None:
        wiki = self._wiki("Unnamed Location")
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            wiki.name = "Our Asylum"
            wiki.save(update_fields=["name", "updated"])

        self.assertFalse(adopt_public_name(wiki, _HRSH, source="wikipedia"))

        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Our Asylum")

    def test_a_person_renaming_between_read_and_write_is_not_clobbered(self) -> None:
        wiki = self._wiki("Unnamed Location")
        stale = Wiki.objects.get(pk=wiki.pk)
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            wiki.name = "Our Asylum"
            wiki.save(update_fields=["name", "updated"])

        self.assertFalse(adopt_public_name(stale, _HRSH, source="wikipedia"))

        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Our Asylum")

    def test_an_automatic_name_from_a_real_source_is_not_replaced(self) -> None:
        wiki = self._wiki("Hudson Heritage")

        self.assertFalse(adopt_public_name(wiki, _HRSH, source="wikipedia"))

    def test_a_fallback_source_name_is_upgraded_by_a_better_source(self) -> None:
        wiki = self._wiki("Unnamed Location")
        adopt_public_name(wiki, "Dutchess County Parking", source="google_places")
        self.assertTrue(is_provisional_name(wiki))

        self.assertTrue(adopt_public_name(wiki, _HRSH, source="wikipedia"))

        wiki.refresh_from_db()
        self.assertEqual(wiki.name, _HRSH)

    def test_a_fallback_name_a_person_then_confirmed_is_kept(self) -> None:
        wiki = self._wiki("Unnamed Location")
        adopt_public_name(wiki, "Dutchess County Parking", source="google_places")
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            wiki.name = "Dutchess County Parking"
            wiki.save(update_fields=["name", "updated"])

        self.assertFalse(adopt_public_name(wiki, _HRSH, source="wikipedia"))

    def test_a_name_someone_removed_is_not_brought_back(self) -> None:
        wiki = self._wiki("Unnamed Location")
        WikiAutoRemoval.objects.record(wiki=wiki, kind=AutoRemovalKind.ALIAS, value=_HRSH)

        self.assertFalse(adopt_public_name(wiki, _HRSH, source="wikipedia"))

    def test_a_meaningless_candidate_is_refused(self) -> None:
        wiki = self._wiki("Unnamed Location")

        self.assertFalse(adopt_public_name(wiki, "Unnamed Location in Poughkeepsie, NY", source="wikipedia"))


class AutomaticWikiCreationProvenanceTests(TestCase):
    def test_a_wiki_created_inside_a_request_is_not_credited_to_the_person(self) -> None:
        baker.make(User)
        profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.7, longitude=-73.9, official_name="Hudson Heritage")

        with writing_as(WriteSource.USER, actor=profile.pk):
            wiki, created = Wiki.objects.get_or_create_for_location(location)

        self.assertTrue(created)
        self.assertEqual(_latest_name_source(wiki), WriteSource.AUTOMATIC)


class LocationNamingReachesTheWikiTests(TestCase):
    """update_location_name_from_external_sources, the one path every cached name source feeds."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.place = baker.make(Place, kind=PlaceKind.PARCEL)
        self.anchor = baker.make(Location, latitude=41.73328, longitude=-73.92812, place=self.place, official_name="")
        with writing_as(WriteSource.AUTOMATIC):
            self.wiki = baker.make(Wiki, location=self.anchor, place=self.place, name="Unnamed Location")

    def _pin(self, location: Location, *, name: str = _PRIVATE, parent: Pin | None = None) -> Pin:
        return baker.make(
            Pin, profile=self.profile, location=location, name=name, name_is_user_provided=True, parent_pin=parent
        )

    def test_a_private_pin_name_never_names_the_wiki_or_the_location(self) -> None:
        self._pin(self.anchor)

        update_location_name_from_external_sources(self.anchor)

        self.wiki.refresh_from_db()
        self.anchor.refresh_from_db()
        self.assertNotIn(_PRIVATE, self.wiki.name.lower())
        self.assertNotIn(_PRIVATE, (self.anchor.official_name or "").lower())

    def test_a_wikipedia_match_names_the_wiki_and_the_location_but_not_the_pin(self) -> None:
        pin = self._pin(self.anchor)
        LocationCache.set(self.anchor, "wikipedia", _ARTICLE, query_key="")

        update_location_name_from_external_sources(self.anchor)

        self.wiki.refresh_from_db()
        self.anchor.refresh_from_db()
        pin.refresh_from_db()
        self.assertEqual(self.wiki.name, _HRSH)
        self.assertEqual(self.anchor.official_name, _HRSH)
        self.assertEqual(pin.name, _PRIVATE)
        self.assertTrue(self.wiki.aliases.filter(name__iexact=_HRSH).exists())

    def test_a_person_named_wiki_keeps_its_name(self) -> None:
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            self.wiki.name = "The Castle"
            self.wiki.save(update_fields=["name", "updated"])
        LocationCache.set(self.anchor, "wikipedia", _ARTICLE, query_key="")

        update_location_name_from_external_sources(self.anchor)

        self.wiki.refresh_from_db()
        self.anchor.refresh_from_db()
        self.assertEqual(self.wiki.name, "The Castle")
        self.assertEqual(self.anchor.official_name, _HRSH)

    def test_a_campus_sibling_with_its_own_root_pin_names_the_places_wiki(self) -> None:
        sibling = baker.make(Location, latitude=41.733453, longitude=-73.923558, place=self.place, official_name="")
        self._pin(sibling)
        LocationCache.set(sibling, "wikipedia", _ARTICLE, query_key="")

        update_location_name_from_external_sources(sibling)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _HRSH)

    def test_a_building_child_pin_does_not_rename_the_parcel_wiki(self) -> None:
        root = self._pin(self.anchor)
        building = baker.make(Location, latitude=41.7331, longitude=-73.9285, place=self.place, official_name="")
        self._pin(building, name="Building 12", parent=root)
        LocationCache.set(building, "wikipedia", {**_ARTICLE, "title": "Kirkbride Building"}, query_key="")

        update_location_name_from_external_sources(building)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Unnamed Location")
        self.assertFalse(self.wiki.aliases.filter(name__iexact="Kirkbride Building").exists())

    def test_a_wikipedia_cache_write_renames_the_wiki_without_anyone_asking(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(self.anchor, "wikipedia", _ARTICLE, query_key="")

        self.wiki.refresh_from_db()
        self.anchor.refresh_from_db()
        self.assertEqual(self.wiki.name, _HRSH)
        self.assertEqual(self.anchor.official_name, _HRSH)


class EnrichWikiLocationPublicNamingTests(TestCase):
    """tasks.enrich_wiki_location: cached public names first, the live Google chain only as a stand-in."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.location = baker.make(
            Location, latitude=41.73328, longitude=-73.92812, official_name="", google_place=None
        )
        with writing_as(WriteSource.AUTOMATIC):
            self.wiki = baker.make(Wiki, location=self.location, name="Unnamed Location")

    def _enrich(self, google_name: str | None) -> None:
        from urbanlens.dashboard import tasks

        with (
            patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService.ensure_linked"),
            patch(
                "urbanlens.dashboard.services.locations.google.PlaceNameResolverChain.resolve", return_value=google_name
            ),
            patch("urbanlens.dashboard.services.locations.boundaries.boundary_generation_ran", return_value=True),
        ):
            tasks.enrich_wiki_location(self.wiki.pk)

    def test_a_cached_public_name_beats_the_live_google_guess(self) -> None:
        LocationCache.set(self.location, "wikipedia", _ARTICLE, query_key="")

        self._enrich("Dutchess County Parking")

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _HRSH)

    def test_the_google_stand_in_is_an_alias_and_gives_way_to_wikipedia(self) -> None:
        self._enrich("Dutchess County Parking")
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Dutchess County Parking")
        self.assertTrue(self.wiki.aliases.filter(name__iexact="Dutchess County Parking").exists())

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(self.location, "wikipedia", _ARTICLE, query_key="")

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, _HRSH)

    def test_a_person_named_wiki_is_left_alone(self) -> None:
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            self.wiki.name = "The Castle"
            self.wiki.save(update_fields=["name", "updated"])
        LocationCache.set(self.location, "wikipedia", _ARTICLE, query_key="")

        self._enrich("Dutchess County Parking")

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "The Castle")
