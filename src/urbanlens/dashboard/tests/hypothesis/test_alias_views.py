"""Tests for the alias HTMX views: current-name marking, delete guard, "use this name"."""

from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit import WikiEdit


class PinAliasViewTestsBase(TestCase):
    """Shared fixture: a logged-in user owning a named pin."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Current Name", name_is_user_provided=True)
        self.client.force_login(self.user)

    def _mock_place_name(self):
        # The pin overview partial checks pin.has_place_name, which would
        # resolve an uncached Location's place name from Google.
        return patch(
            "urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name",
            return_value=None,
        )


class PinAliasUseViewTests(PinAliasViewTestsBase):
    """POST pin.alias.use renames the pin and keeps both names as aliases."""

    def test_use_alias_renames_pin(self) -> None:
        alias = baker.make(PinAlias, pin=self.pin, name="Better Name")
        with self._mock_place_name():
            response = self.client.post(reverse("pin.alias.use", args=[self.pin.slug, alias.id]))
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "Better Name")
        self.assertTrue(self.pin.name_is_user_provided)
        self.assertCountEqual(list(self.pin.aliases.values_list("name", flat=True)), ["Current Name", "Better Name"])

    def test_use_alias_requires_pin_ownership(self) -> None:
        other_pin = baker.make(Pin, profile=baker.make("auth.User").profile, name="Not Yours")
        alias = other_pin.aliases.get(name="Not Yours")
        response = self.client.post(reverse("pin.alias.use", args=[other_pin.slug, alias.id]))
        self.assertEqual(response.status_code, 404)


class PinAliasDeleteGuardTests(PinAliasViewTestsBase):
    """The alias matching the pin's current name cannot be removed."""

    def test_deleting_current_name_alias_is_blocked(self) -> None:
        alias = self.pin.aliases.get(name="Current Name")
        response = self.client.delete(reverse("pin.alias.delete", args=[self.pin.slug, alias.id]))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.pin.aliases.filter(name="Current Name").exists())

    def test_deleting_other_alias_still_works(self) -> None:
        alias = baker.make(PinAlias, pin=self.pin, name="Disposable Name")
        response = self.client.delete(reverse("pin.alias.delete", args=[self.pin.slug, alias.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.pin.aliases.filter(name="Disposable Name").exists())

    def test_current_alias_is_marked_in_panel(self) -> None:
        response = self.client.get(reverse("pin.aliases", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alias-chip--current")


class LocationAliasUseViewTests(TestCase):
    """POST location.wiki.alias.use renames the wiki and records a WikiEdit."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        self.client.force_login(self.user)

    def test_use_alias_renames_wiki_and_records_edit(self) -> None:
        alias = baker.make(WikiAlias, wiki=self.wiki, name="Restored Mill")
        response = self.client.post(reverse("location.wiki.alias.use", args=[self.location.slug, alias.id]))
        self.assertEqual(response.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Restored Mill")
        self.assertCountEqual(list(self.wiki.aliases.values_list("name", flat=True)), ["Curated Mill", "Restored Mill"])
        edit = WikiEdit.objects.filter(wiki=self.wiki).latest("created")
        self.assertEqual(edit.changes, {"name": {"from": "Curated Mill", "to": "Restored Mill"}})

    def test_deleting_current_name_alias_is_blocked(self) -> None:
        alias = self.wiki.aliases.get(name="Curated Mill")
        response = self.client.delete(reverse("location.wiki.alias.delete", args=[self.location.slug, alias.id]))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.wiki.aliases.filter(name="Curated Mill").exists())
