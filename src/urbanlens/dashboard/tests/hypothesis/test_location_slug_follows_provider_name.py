"""P250: a Location's slug follows its current provider name, as migration 0041 decides it.

A provider rename re-mints the slug, a cleared name falls back to the uuid, and the slug given up goes to the slug
history so links to it still resolve. A name flipping back reuses its former slug, and the history stays bounded.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.slug_history import MAX_FORMER_SLUGS, LocationSlugHistory
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.slugs import parent_slug_prefix
from urbanlens.dashboard.services.locations.name_resolution import NameCandidate
from urbanlens.dashboard.services.locations.naming import (
    _retire_rejected_name,
    update_location_name_from_external_sources,
)


class _Locations(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self._seq = 0

    def _location(self, name: str | None = None, source: str = "", **extra) -> Location:
        self._seq += 1
        return Location.objects.create(
            latitude=43.1 + self._seq / 1000,
            longitude=-74.1 - self._seq / 1000,
            official_name=name,
            official_name_source=source,
            **extra,
        )

    @staticmethod
    def _rename(location: Location, name: str, source: str = "cris") -> None:
        location.official_name = name
        location.official_name_source = source
        location.save(update_fields=["official_name", "official_name_source", "updated"])

    @staticmethod
    def _former(location: Location) -> list[str]:
        return sorted(LocationSlugHistory.objects.filter(location=location).values_list("slug", flat=True))


class ProviderRenameTests(_Locations):
    def test_a_provider_rename_re_mints_the_slug_from_the_new_name(self) -> None:
        location = self._location("Hudson River State Hospital", "wikipedia")
        self.assertEqual(location.slug, "hudson-river-state-hospital")

        update_location_name_from_external_sources(location, extra_candidates=[("wikipedia", "Main Building")])

        location.refresh_from_db()
        self.assertEqual(location.official_name, "Main Building")
        self.assertEqual(location.slug, "main-building")

    def test_the_slug_given_up_is_recorded_and_still_resolves(self) -> None:
        location = self._location("Hudson River State Hospital", "wikipedia")

        self._rename(location, "Main Building")

        self.assertEqual(self._former(location), ["hudson-river-state-hospital"])
        self.assertEqual(Location.objects.from_url_slug("hudson-river-state-hospital").pk, location.pk)

    def test_a_viewer_following_the_old_slug_is_moved_to_the_new_one(self) -> None:
        location = self._location("Hudson River State Hospital", "wikipedia")
        baker.make(Wiki, location=location, name="Hudson River State Hospital")
        user = baker.make(User)
        baker.make(Pin, profile=user.profile, location=location)
        self.client.force_login(user)

        self._rename(location, "Main Building")

        response = self.client.get(reverse("location.wiki", args=["hudson-river-state-hospital"]))
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("location.wiki", args=["main-building"]))

    def test_a_new_source_for_the_same_name_keeps_the_slug(self) -> None:
        location = self._location("Main Building", "wikipedia")

        self._rename(location, "Main Building", "cris")

        location.refresh_from_db()
        self.assertEqual(location.slug, "main-building")
        self.assertEqual(self._former(location), [])

    def test_a_suffixed_slug_the_new_name_could_still_give_is_kept(self) -> None:
        self._location("Main Building", "wikipedia")
        location = self._location("Main Building", "cris")
        minted = location.slug
        self.assertNotEqual(minted, "main-building")

        self._rename(location, "Main Building", "redata_building")

        location.refresh_from_db()
        self.assertEqual(location.slug, minted)


class ClearedNameTests(_Locations):
    def test_a_cleared_name_falls_back_to_the_uuid(self) -> None:
        location = self._location("Old Service Road", "nominatim")

        _retire_rejected_name(location, None, [NameCandidate(name="Old Service Road", source="nominatim")])

        location.refresh_from_db()
        self.assertEqual(location.official_name, "")
        self.assertEqual(location.slug, str(location.uuid))
        self.assertEqual(self._former(location), ["old-service-road"])

    def test_a_name_saved_without_a_source_falls_back_to_the_uuid(self) -> None:
        location = self._location("Old Grain Mill", "wikipedia")

        location.official_name_source = ""
        location.save(update_fields=["official_name_source", "updated"])

        location.refresh_from_db()
        self.assertEqual(location.slug, str(location.uuid))
        self.assertEqual(Location.objects.from_url_slug("old-grain-mill").pk, location.pk)

    def test_an_unrelated_save_leaves_a_slug_alone(self) -> None:
        """Only a change of name re-mints; a row saved for another reason keeps whatever slug it has."""
        location = self._location(slug="hand-picked")

        location.zipcode = "12601"
        location.save()

        location.refresh_from_db()
        self.assertEqual(location.slug, "hand-picked")


class WikiSlugTests(_Locations):
    def test_a_rename_re_mints_the_wiki_slug_from_the_new_name(self) -> None:
        location = self._location("Hudson River State Hospital", "wikipedia")
        wiki = Wiki.objects.create(location=location, name="Sam's Place")
        self.assertEqual(wiki.slug, "hudson-river-state-hospital")

        self._rename(location, "Main Building")

        wiki.refresh_from_db()
        self.assertEqual(wiki.slug, "main-building")
        self.assertEqual(wiki.name, "Sam's Place")

    def test_a_cleared_name_puts_the_wiki_on_its_uuid(self) -> None:
        location = self._location("Old Service Road", "nominatim")
        wiki = Wiki.objects.create(location=location, name="Old Service Road")

        _retire_rejected_name(location, wiki, [NameCandidate(name="Old Service Road", source="nominatim")])

        wiki.refresh_from_db()
        self.assertEqual(wiki.slug, str(wiki.uuid))

    def test_a_child_wiki_takes_the_parent_s_new_prefix(self) -> None:
        campus = self._location("Hudson River State Hospital", "wikipedia")
        parent = Wiki.objects.create(location=campus, name="HRSH")
        child = Wiki.objects.create(location=self._location("Powerhouse", "cris"), name="Boiler", parent_wiki=parent)
        self.assertEqual(child.slug, f"{parent_slug_prefix(['Hudson River State Hospital'])}-powerhouse")

        self._rename(campus, "Bannerman Castle")

        child.refresh_from_db()
        self.assertEqual(child.slug, f"{parent_slug_prefix(['Bannerman Castle'])}-powerhouse")

    def test_a_child_wiki_s_own_rename_keeps_the_parent_s_prefix(self) -> None:
        parent = Wiki.objects.create(location=self._location("Hudson River State Hospital", "wikipedia"), name="HRSH")
        child_location = self._location("Powerhouse", "cris")
        child = Wiki.objects.create(location=child_location, name="Boiler", parent_wiki=parent)

        self._rename(child_location, "Laundry")

        child.refresh_from_db()
        self.assertEqual(child.slug, f"{parent_slug_prefix(['Hudson River State Hospital'])}-laundry")


class ThrashTests(_Locations):
    def test_a_provider_flipping_between_two_names_reuses_its_two_slugs(self) -> None:
        # Both readable slugs are already someone else's, so each name mints a numbered one.
        self._location("Main Building", "wikipedia")
        self._location("Administration", "wikipedia")
        location = self._location("Main Building", "cris")
        first = location.slug
        self._rename(location, "Administration")
        second = location.slug
        self.assertNotIn(first, ("main-building", second))

        for _ in range(5):
            self._rename(location, "Main Building")
            self.assertEqual(location.slug, first)
            self._rename(location, "Administration")
            self.assertEqual(location.slug, second)

        self.assertEqual(self._former(location), [first])

    def test_a_former_slug_taken_meanwhile_by_another_location_mints_a_fresh_one(self) -> None:
        """The check that a former slug is free and the write that takes it back are two statements."""
        location = self._location("Administration", "cris")
        LocationSlugHistory.objects.create(location=location, slug="main-building-77")
        rival = self._location("Laundry", "cris")
        Location.objects.filter(pk=rival.pk).update(slug="main-building-77")
        real = Location._slug_is_taken
        calls: list[str] = []

        def raced(instance: Location, candidate: str) -> bool:
            calls.append(candidate)
            return False if len(calls) == 1 else real(instance, candidate)

        with mock.patch.object(Location, "_slug_is_taken", raced):
            self._rename(location, "Main Building")

        location.refresh_from_db()
        self.assertEqual(calls[0], "main-building-77")
        self.assertNotEqual(location.slug, "main-building-77")
        self.assertTrue(location.slug.startswith("main-building"))
        self.assertIn("administration", self._former(location))

    def test_the_history_of_one_location_is_bounded(self) -> None:
        location = self._location("Name 0", "cris")

        for number in range(1, MAX_FORMER_SLUGS + 6):
            self._rename(location, f"Name {number}")

        self.assertEqual(LocationSlugHistory.objects.filter(location=location).count(), MAX_FORMER_SLUGS)
        self.assertEqual(Location.objects.from_url_slug(f"name-{MAX_FORMER_SLUGS + 4}").pk, location.pk)


class PinSlugTests(_Locations):
    def test_a_provider_rename_leaves_pin_slugs_alone(self) -> None:
        location = self._location("Hudson River State Hospital", "wikipedia")
        pin = baker.make(Pin, profile=baker.make(User).profile, location=location, name="")
        slug = pin.ensure_slug()

        self._rename(location, "Main Building")

        pin.refresh_from_db()
        self.assertEqual(pin.slug, slug)
