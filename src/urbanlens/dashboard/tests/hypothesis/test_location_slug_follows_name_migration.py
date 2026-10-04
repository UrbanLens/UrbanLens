"""P250's data migration: slugs left behind by a rename since 0041 move onto the current provider name.

Rows are written past ``Location.save`` (``queryset.update``) so they stand as the runtime left them before it
re-minted on a rename, and the migration runs against the models as ``migrate`` renders them.
"""

from __future__ import annotations

import importlib

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.slug_history import LocationSlugHistory
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.slugs import parent_slug_prefix

migration = importlib.import_module("urbanlens.dashboard.migrations.0054_location_slug_follows_name")

_HISTORICAL_APPS = None


def _historical_apps():
    """The models as the migration sees them under ``migrate``: no properties, no custom managers."""
    global _HISTORICAL_APPS  # noqa: PLW0603 - rendering the state takes seconds; one per test run
    if _HISTORICAL_APPS is None:
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        node = ("dashboard", migration.Migration.dependencies[0][1])
        _HISTORICAL_APPS = MigrationExecutor(connection).loader.project_state(node).apps
    return _HISTORICAL_APPS


class LocationSlugFollowsNameMigrationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self._seq = 0

    def _location(self, name: str, source: str, slug: str) -> Location:
        """A Location as it stood: its name and slug written directly, past the runtime re-mint."""
        self._seq += 1
        location = Location.objects.create(latitude=44.1 + self._seq / 1000, longitude=-75.1 - self._seq / 1000)
        Location.objects.filter(pk=location.pk).update(official_name=name, official_name_source=source, slug=slug)
        location.refresh_from_db()
        return location

    def _run(self) -> None:
        migration.remint_mismatched_slugs(_historical_apps(), None)

    @staticmethod
    def _former(location: Location) -> list[str]:
        return sorted(LocationSlugHistory.objects.filter(location=location).values_list("slug", flat=True))

    def test_a_location_renamed_since_0041_takes_its_new_name_s_slug(self) -> None:
        location = self._location("BLDG 51/MAIN/ADMIN (1871) - NHL", "cris", "hudson-river-state-hospital-32655")

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.slug, "bldg-51mainadmin-1871-nhl")
        self.assertEqual(self._former(location), ["hudson-river-state-hospital-32655"])

    def test_a_location_with_no_provider_name_falls_back_to_its_uuid(self) -> None:
        location = self._location("", "", "old-service-road")

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.slug, str(location.uuid))
        self.assertEqual(self._former(location), ["old-service-road"])

    def test_a_slug_that_fits_is_left_alone(self) -> None:
        fitting = self._location("Main Building", "cris", "main-building-4021")
        unnamed = self._location("", "", "placeholder")
        Location.objects.filter(pk=unnamed.pk).update(slug=str(unnamed.uuid))

        self._run()

        fitting.refresh_from_db()
        unnamed.refresh_from_db()
        self.assertEqual(fitting.slug, "main-building-4021")
        self.assertEqual(unnamed.slug, str(unnamed.uuid))
        self.assertFalse(LocationSlugHistory.objects.exists())

    def test_a_former_slug_of_its_own_is_taken_back(self) -> None:
        self._location("Main Building", "wikipedia", "main-building")
        location = self._location("Main Building", "cris", "administration-77")
        LocationSlugHistory.objects.create(location=location, slug="main-building-77")

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.slug, "main-building-77")
        self.assertEqual(self._former(location), ["administration-77"])

    def test_a_slug_another_location_gave_up_is_never_minted(self) -> None:
        other = self._location("Main Building", "wikipedia", "laundry")
        LocationSlugHistory.objects.create(location=other, slug="main-building")
        location = self._location("Main Building", "cris", "administration")

        self._run()

        location.refresh_from_db()
        self.assertNotEqual(location.slug, "main-building")
        self.assertTrue(location.slug.startswith("main-building"))

    def test_a_wiki_takes_its_location_s_new_name_and_its_children_the_new_prefix(self) -> None:
        campus = self._location("Bannerman Castle", "wikipedia", "hudson-river-state-hospital")
        parent = baker.make(Wiki, location=campus, name="Campus")
        building = self._location("Powerhouse", "cris", "powerhouse")
        child = baker.make(Wiki, location=building, name="Boiler", parent_wiki=parent)
        unnamed = baker.make(Wiki, location=self._location("", "", "x"), name="Lunch with Sam")
        Wiki.objects.filter(pk=parent.pk).update(slug="hudson-river-state-hospital")
        Wiki.objects.filter(pk=child.pk).update(slug="hrsh-powerhouse")
        Wiki.objects.filter(pk=unnamed.pk).update(slug="lunch-with-sam")

        self._run()

        for wiki in (parent, child, unnamed):
            wiki.refresh_from_db()
        self.assertEqual(parent.slug, "bannerman-castle")
        self.assertEqual(child.slug, f"{parent_slug_prefix(['Bannerman Castle'])}-powerhouse")
        self.assertEqual(unnamed.slug, str(unnamed.uuid))

    def test_two_wikis_holding_each_other_s_slugs_both_get_their_own(self) -> None:
        first = baker.make(Wiki, location=self._location("Laundry", "cris", "laundry"), name="A")
        second = baker.make(Wiki, location=self._location("Kitchen", "cris", "kitchen"), name="B")
        Wiki.objects.filter(pk=first.pk).update(slug="kitchen-tmp")
        Wiki.objects.filter(pk=second.pk).update(slug="laundry")
        Wiki.objects.filter(pk=first.pk).update(slug="kitchen")

        self._run()

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.slug, second.slug), ("laundry", "kitchen"))
