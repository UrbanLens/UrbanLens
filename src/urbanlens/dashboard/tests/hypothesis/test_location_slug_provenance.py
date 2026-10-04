"""P186: a wiki's URL slug comes only from a provider's name for its Location, or the uuid.

Community text (wiki names, aliases, child-wiki slugs) never reaches a Location or Wiki slug; pin slugs, which only their
owner sees, still come from the pin's own name.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.slug_history import LocationSlugHistory
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.slugs import parent_slug_prefix
from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources


class _Locations(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self._seq = 0

    def _location(self, name: str | None = None, source: str = "") -> Location:
        self._seq += 1
        return Location.objects.create(
            latitude=43.1 + self._seq / 1000,
            longitude=-74.1 - self._seq / 1000,
            official_name=name,
            official_name_source=source,
        )


class ProviderNameReMintTests(_Locations):
    def test_a_provider_name_arriving_re_mints_a_uuid_slug(self) -> None:
        location = self._location()
        self.assertEqual(location.slug, str(location.uuid))

        update_location_name_from_external_sources(location, extra_candidates=[("wikipedia", "Old Grain Mill")])

        location.refresh_from_db()
        self.assertEqual(location.official_name, "Old Grain Mill")
        self.assertEqual(location.official_name_source, "wikipedia")
        self.assertEqual(location.slug, "old-grain-mill")

    def test_a_provider_confirming_an_unproven_name_re_mints_the_slug(self) -> None:
        location = self._location("Old Grain Mill")
        self.assertEqual(location.slug, str(location.uuid))

        update_location_name_from_external_sources(location, extra_candidates=[("wikipedia", "Old Grain Mill")])

        location.refresh_from_db()
        self.assertEqual(location.official_name_source, "wikipedia")
        self.assertEqual(location.slug, "old-grain-mill")

    def test_a_later_provider_rename_re_mints_the_slug(self) -> None:
        location = self._location("Old Grain Mill", "wikipedia")

        update_location_name_from_external_sources(location, extra_candidates=[("wikipedia", "Smith Brothers Mill")])

        location.refresh_from_db()
        self.assertEqual(location.official_name, "Smith Brothers Mill")
        self.assertEqual(location.slug, "smith-brothers-mill")

    def test_saving_a_provider_name_directly_re_mints_a_uuid_slug(self) -> None:
        location = self._location()

        location.official_name = "Powerhouse"
        location.official_name_source = "cris"
        location.save(update_fields=["official_name", "official_name_source", "updated"])

        location.refresh_from_db()
        self.assertEqual(location.slug, "powerhouse")

    def test_a_name_saved_without_a_source_leaves_the_uuid_slug(self) -> None:
        location = self._location()

        location.official_name = "Lunch with Sam"
        location.save(update_fields=["official_name", "updated"])

        location.refresh_from_db()
        self.assertEqual(location.slug, str(location.uuid))

    def test_the_uuid_slug_a_re_mint_replaces_is_not_recorded(self) -> None:
        """A uuid always resolves by itself, so it needs no history row."""
        location = self._location()

        update_location_name_from_external_sources(location, extra_candidates=[("wikipedia", "Old Grain Mill")])

        self.assertFalse(LocationSlugHistory.objects.filter(location=location).exists())


class CommunityTextNeverReachesTheLocationSlugTests(_Locations):
    def test_a_community_wiki_rename_never_changes_the_location_slug(self) -> None:
        location = self._location("Old Grain Mill", "wikipedia")
        wiki = Wiki.objects.create(location=location, name="Old Grain Mill")

        wiki.name = "Where Sam Lives"
        wiki.save()

        location.refresh_from_db()
        wiki.refresh_from_db()
        self.assertEqual(location.slug, "old-grain-mill")
        self.assertEqual(wiki.slug, "old-grain-mill")

    def test_a_child_wiki_no_longer_copies_its_name_into_the_location_slug(self) -> None:
        parent = Wiki.objects.create(
            location=self._location("Hudson River State Hospital", "historic_register"), name="HRSH"
        )
        child_location = self._location()

        Wiki.objects.create(location=child_location, name="Powerhouse", parent_wiki=parent)

        child_location.refresh_from_db()
        self.assertEqual(child_location.slug, str(child_location.uuid))

    def test_a_wiki_on_an_unnamed_location_is_slugged_by_its_uuid_not_its_name(self) -> None:
        wiki = Wiki.objects.create(location=self._location(), name="Lunch with Sam")

        self.assertEqual(wiki.slug, str(wiki.uuid))

    def test_a_wiki_takes_its_slug_from_the_provider_name_not_its_own(self) -> None:
        wiki = Wiki.objects.create(location=self._location("Old Grain Mill", "wikipedia"), name="Lunch with Sam")

        self.assertEqual(wiki.slug, "old-grain-mill")

    def test_a_child_wiki_prefix_comes_from_the_parent_provider_name_not_its_aliases(self) -> None:
        parent = Wiki.objects.create(
            location=self._location("Hudson River State Hospital", "historic_register"), name="Sam's Place"
        )
        WikiAlias.objects.create(wiki=parent, name="SAMS")

        child = Wiki.objects.create(location=self._location("Powerhouse", "cris"), name="Boiler", parent_wiki=parent)

        self.assertEqual(child.slug, f"{parent_slug_prefix(['Hudson River State Hospital'])}-powerhouse")

    def test_a_provider_name_arriving_re_mints_the_wiki_uuid_slug_too(self) -> None:
        location = self._location()
        wiki = Wiki.objects.create(location=location, name="Lunch with Sam")

        update_location_name_from_external_sources(location, extra_candidates=[("wikipedia", "Old Grain Mill")])

        wiki.refresh_from_db()
        self.assertEqual(wiki.slug, "old-grain-mill")


class SlugHistoryTests(_Locations):
    def test_a_changed_slug_is_recorded_and_still_resolves(self) -> None:
        location = self._location("Old Grain Mill", "wikipedia")

        location.slug = "smith-mill"
        location.save(update_fields=["slug", "updated"])

        self.assertTrue(LocationSlugHistory.objects.filter(location=location, slug="old-grain-mill").exists())
        self.assertEqual(Location.objects.from_url_slug("old-grain-mill").pk, location.pk)
        self.assertEqual(Location.objects.from_url_slug("smith-mill").pk, location.pk)

    def test_an_unknown_slug_finds_nothing(self) -> None:
        self.assertIsNone(Location.objects.from_url_slug("never-was-a-slug"))

    def test_a_new_slug_never_takes_another_location_s_former_slug(self) -> None:
        first = self._location("Old Grain Mill", "wikipedia")
        first.slug = "smith-mill"
        first.save(update_fields=["slug", "updated"])

        second = self._location("Old Grain Mill", "google_places")

        self.assertNotEqual(second.slug, "old-grain-mill")
        self.assertEqual(Location.objects.from_url_slug("old-grain-mill").pk, first.pk)

    def test_taking_back_a_former_slug_drops_its_history_row(self) -> None:
        location = self._location("Old Grain Mill", "wikipedia")
        location.slug = "smith-mill"
        location.save(update_fields=["slug", "updated"])

        location.slug = "old-grain-mill"
        location.save(update_fields=["slug", "updated"])

        self.assertEqual(
            list(LocationSlugHistory.objects.filter(location=location).values_list("slug", flat=True)), ["smith-mill"]
        )

    def test_a_current_slug_wins_over_another_location_s_former_one(self) -> None:
        moved = self._location("Old Grain Mill", "wikipedia")
        moved.slug = "smith-mill"
        moved.save(update_fields=["slug", "updated"])
        holder = self._location()
        Location.objects.filter(pk=holder.pk).update(slug="old-grain-mill")

        self.assertEqual(Location.objects.from_url_slug("old-grain-mill").pk, holder.pk)


class PinSlugTests(_Locations):
    def test_pin_slugs_come_from_the_pin_name_and_are_scoped_to_their_owner(self) -> None:
        location = self._location()
        mine = baker.make(Pin, profile=baker.make(User).profile, location=location, name="My Secret Spot")
        theirs = baker.make(Pin, profile=baker.make(User).profile, location=location, name="My Secret Spot")

        self.assertEqual(mine.ensure_slug(), "my-secret-spot")
        self.assertEqual(theirs.ensure_slug(), "my-secret-spot")

    def test_a_pin_name_never_reaches_the_location_slug(self) -> None:
        location = self._location()
        pin = baker.make(Pin, profile=baker.make(User).profile, location=location, name="My Secret Spot")
        pin.backfill_wiki_link_slugs()

        location.refresh_from_db()
        self.assertEqual(location.slug, str(location.uuid))
