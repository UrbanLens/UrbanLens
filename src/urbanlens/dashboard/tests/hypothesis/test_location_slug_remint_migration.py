"""P186's data migration: an existing Location keeps a readable slug only when stored provider data proves its name.

Rows predate any record of where ``official_name`` came from, so the migration accepts a name only when the linked
GooglePlace or one of the Location's own cached provider responses holds it. Everything else falls back to the uuid,
and the slug it replaces goes to the slug history so old links keep working.
"""

from __future__ import annotations

import importlib

from django.apps import apps as django_apps
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.slug_history import LocationSlugHistory
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.slugs import parent_slug_prefix

migration = importlib.import_module("urbanlens.dashboard.migrations.0041_location_slug_remint")


class LocationSlugRemintMigrationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")
        self._seq = 0

    def _legacy(self, name: str | None, slug: str | None = None, **kwargs) -> Location:
        """A row as it stood before provenance existed: any name, any slug, no source."""
        self._seq += 1
        location = Location.objects.create(
            latitude=44.1 + self._seq / 1000, longitude=-75.1 - self._seq / 1000, **kwargs
        )
        Location.objects.filter(pk=location.pk).update(
            official_name=name, official_name_source="", slug=slug or str(location.uuid)
        )
        return location

    def _run(self) -> None:
        migration.remint_location_slugs(django_apps, None)

    def _history(self, location: Location) -> list[str]:
        return list(LocationSlugHistory.objects.filter(location=location).values_list("slug", flat=True))

    def test_a_name_the_linked_google_place_holds_is_proven(self) -> None:
        place = GooglePlace.objects.create(latitude="44.5", longitude="-75.5", cached_place_name="GRAND HALL.")
        location = self._legacy("Grand Hall", "grand-hall", google_place=place)

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.official_name_source, "google_places")
        self.assertEqual(location.slug, "grand-hall")
        self.assertEqual(self._history(location), [])

    def test_a_name_a_cached_provider_response_holds_is_proven(self) -> None:
        location = self._legacy("Old Grain Mill", "old-grain-mill-2")
        LocationCache.objects.create(
            location=location, source="wikipedia", data={"title": "Old Grain Mill", "extract": "..."}
        )

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.official_name_source, "wikipedia")
        self.assertEqual(location.slug, "old-grain-mill-2")

    def test_a_name_echoed_outside_a_provider_s_name_field_is_not_proof(self) -> None:
        """A cached response can carry the query it was sent, which may be the very text being tested."""
        location = self._legacy("Lunch with Sam", "lunch-with-sam")
        LocationCache.objects.create(
            location=location, source="wikipedia", data={"query": "Lunch with Sam", "title": "Something Else"}
        )
        LocationCache.objects.create(location=location, source="web_search", data={"name": "Lunch with Sam"})

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.official_name_source, "")
        self.assertEqual(location.slug, str(location.uuid))

    def test_an_unproven_name_loses_its_readable_slug_to_the_uuid(self) -> None:
        location = self._legacy("Lunch with Sam", "lunch-with-sam")
        LocationCache.objects.create(location=location, source="wikipedia", data={"title": "Something Else"})

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.slug, str(location.uuid))
        self.assertEqual(location.official_name, "Lunch with Sam")
        self.assertEqual(location.official_name_source, "")
        self.assertEqual(self._history(location), ["lunch-with-sam"])

    def test_an_unnamed_uuid_row_is_untouched(self) -> None:
        location = self._legacy(None)

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.slug, str(location.uuid))
        self.assertEqual(self._history(location), [])

    def test_a_proven_name_on_a_uuid_slug_gets_a_readable_one(self) -> None:
        place = GooglePlace.objects.create(latitude="44.6", longitude="-75.6", cached_place_name="Named Later Works")
        location = self._legacy("Named Later Works", google_place=place)

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.slug, "named-later-works")
        self.assertEqual(self._history(location), [])

    def test_a_slug_copied_from_a_child_wiki_is_re_minted_from_the_proven_name(self) -> None:
        place = GooglePlace.objects.create(latitude="44.7", longitude="-75.7", cached_place_name="Powerhouse")
        location = self._legacy("Powerhouse", "hrsh-powerhouse", google_place=place)

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.slug, "powerhouse")
        self.assertEqual(self._history(location), ["hrsh-powerhouse"])

    def test_wiki_slugs_follow_the_proven_location_name_or_the_wiki_uuid(self) -> None:
        place = GooglePlace.objects.create(
            latitude="44.8", longitude="-75.8", cached_place_name="Hudson River State Hospital"
        )
        proven = self._legacy("Hudson River State Hospital", google_place=place)
        unproven = self._legacy("Lunch with Sam", "lunch-with-sam")
        child_place = GooglePlace.objects.create(latitude="44.9", longitude="-75.9", cached_place_name="Powerhouse")
        child_location = self._legacy("Powerhouse", google_place=child_place)
        parent = Wiki.objects.create(location=proven, name="Community Name")
        child = Wiki.objects.create(location=child_location, name="Boiler", parent_wiki=parent)
        mine = Wiki.objects.create(location=unproven, name="Lunch with Sam")
        Wiki.objects.filter(pk=parent.pk).update(slug="community-name")
        Wiki.objects.filter(pk=child.pk).update(slug="cn-boiler")
        Wiki.objects.filter(pk=mine.pk).update(slug="lunch-with-sam")

        self._run()

        for wiki in (parent, child, mine):
            wiki.refresh_from_db()
        self.assertEqual(parent.slug, "hudson-river-state-hospital")
        self.assertEqual(child.slug, f"{parent_slug_prefix(['Hudson River State Hospital'])}-powerhouse")
        self.assertEqual(mine.slug, str(mine.uuid))

    def _legacy_batch(self, count: int) -> None:
        """``count`` rows of each kind the migration meets: proven, unproven with a readable slug and a wiki, unnamed."""
        for _ in range(count):
            proven = self._legacy("Old Grain Mill")
            name = f"Old Grain Mill {proven.pk}"
            Location.objects.filter(pk=proven.pk).update(official_name=name, slug=f"old-grain-mill-{proven.pk}")
            LocationCache.objects.create(location=proven, source="wikipedia", data={"title": name})
            unproven = self._legacy("Lunch with Sam")
            Location.objects.filter(pk=unproven.pk).update(slug=f"lunch-with-sam-{unproven.pk}")
            wiki = Wiki.objects.create(location=unproven, name="Lunch with Sam")
            Wiki.objects.filter(pk=wiki.pk).update(slug=f"lunch-with-sam-{wiki.pk}")
            self._legacy(None)

    def _queries_for_a_run(self) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            self._run()
        return len(queries)

    def test_the_query_count_does_not_grow_with_the_number_of_locations(self) -> None:
        """Production holds ~135k cached responses; one query per Location inside one transaction does not scale."""
        self._legacy_batch(3)
        few = self._queries_for_a_run()
        self._legacy_batch(30)
        many = self._queries_for_a_run()

        self.assertEqual(Location.objects.exclude(official_name_source="").count(), 33)
        self.assertEqual(LocationSlugHistory.objects.count(), 33)
        self.assertEqual(many, few)

    def test_long_sibling_child_wiki_slugs_are_stable_across_runs(self) -> None:
        """A child's slug may be a grown candidate or the full name with a suffix; both fit their name."""
        name = "138 Hudson View Dr, Poughkeepsie, NY 12601, USA"
        parent_place = GooglePlace.objects.create(
            latitude="45.0", longitude="-76.0", cached_place_name="Hudson River State Hospital"
        )
        parent = Wiki.objects.create(
            location=self._legacy("Hudson River State Hospital", google_place=parent_place), name="Campus"
        )
        children = []
        for index in range(3):
            place = GooglePlace.objects.create(latitude=f"45.1{index}", longitude="-76.1", cached_place_name=name)
            children.append(
                Wiki.objects.create(location=self._legacy(name, google_place=place), name="X", parent_wiki=parent)
            )
        self._run()
        first = list(
            Wiki.objects.filter(pk__in=[child.pk for child in children]).order_by("pk").values_list("slug", flat=True)
        )

        self._run()

        second = list(
            Wiki.objects.filter(pk__in=[child.pk for child in children]).order_by("pk").values_list("slug", flat=True)
        )
        self.assertEqual(second, first)
        self.assertEqual(len(set(first)), 3)

    def test_running_twice_changes_nothing_more(self) -> None:
        location = self._legacy("Lunch with Sam", "lunch-with-sam")
        self._run()
        location.refresh_from_db()
        first = (location.slug, self._history(location))

        self._run()

        location.refresh_from_db()
        self.assertEqual((location.slug, self._history(location)), first)
