"""Tests for the alias HTMX views: current-name marking, delete guard, "use this name"."""

from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias, WikiAlias
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


class PinDetailHasEverUsedAliasesContextTests(TestCase):
    """has_ever_used_aliases (drives the aliases onboarding card) is profile-wide.

    Regression coverage: it used to be scoped per-pin (checking only the
    viewed pin's own alias list), so a user who had thoroughly used the alias
    feature on other pins still got nagged with "Save private alternate
    names for this pin" on every new, not-yet-named pin.
    """

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _mock_place_name(self):
        return patch(
            "urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name",
            return_value=None,
        )

    def test_false_when_profile_has_never_used_aliases(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name=None)
        with self._mock_place_name():
            response = self.client.get(reverse("pin.details", args=[pin.slug]))
        self.assertFalse(response.context["has_ever_used_aliases"])

    def test_true_when_a_different_pin_has_an_alias(self) -> None:
        other_pin = baker.make(Pin, profile=self.profile, name="Named Elsewhere", name_is_user_provided=True)
        PinAlias.objects.create(pin=other_pin, name="Extra Alias")
        pin = baker.make(Pin, profile=self.profile, name=None)
        with self._mock_place_name():
            response = self.client.get(reverse("pin.details", args=[pin.slug]))
        self.assertTrue(response.context["has_ever_used_aliases"])


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


class PinAliasNicknameTests(PinAliasViewTestsBase):
    """Creating and toggling nickname-only pin aliases."""

    def test_create_alias_with_nickname_checkbox_sets_nickname_kind(self) -> None:
        response = self.client.post(
            reverse("pin.aliases", args=[self.pin.slug]),
            {"name": "Spooky House", "is_nickname": "1"},
        )
        self.assertEqual(response.status_code, 200)
        alias = self.pin.aliases.get(name="Spooky House")
        self.assertEqual(alias.kind, AliasType.NICKNAME)
        self.assertTrue(alias.is_nickname)

    def test_create_alias_without_checkbox_is_not_nickname(self) -> None:
        response = self.client.post(reverse("pin.aliases", args=[self.pin.slug]), {"name": "Another Name"})
        self.assertEqual(response.status_code, 200)
        alias = self.pin.aliases.get(name="Another Name")
        self.assertEqual(alias.kind, AliasType.ALTERNATE)
        self.assertFalse(alias.is_nickname)

    def test_toggle_nickname_flips_kind(self) -> None:
        alias = baker.make(PinAlias, pin=self.pin, name="Toggle Me", kind=AliasType.ALTERNATE)
        response = self.client.post(reverse("pin.alias.toggle_nickname", args=[self.pin.slug, alias.id]))
        self.assertEqual(response.status_code, 200)
        alias.refresh_from_db()
        self.assertTrue(alias.is_nickname)

        response = self.client.post(reverse("pin.alias.toggle_nickname", args=[self.pin.slug, alias.id]))
        self.assertEqual(response.status_code, 200)
        alias.refresh_from_db()
        self.assertFalse(alias.is_nickname)

    def test_toggle_nickname_requires_pin_ownership(self) -> None:
        other_pin = baker.make(Pin, profile=baker.make("auth.User").profile, name="Not Yours")
        alias = other_pin.aliases.get(name="Not Yours")
        response = self.client.post(reverse("pin.alias.toggle_nickname", args=[other_pin.slug, alias.id]))
        self.assertEqual(response.status_code, 404)


class LocationAliasUseViewTests(TestCase):
    """POST location.wiki.alias.use renames the wiki and records a WikiEdit."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
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

    def test_add_alias_form_is_collapsed_behind_a_header_button(self) -> None:
        """The wiki aliases panel used to show its add-alias input fields
        unconditionally - inconsistent with the pin page's own aliases panel
        (and every other add-flow on pin/wiki pages), which reveals its input
        only after the header "+" button is clicked. Regression guard for
        making the wiki panel match that same consistent pattern."""
        response = self.client.get(reverse("location.wiki.aliases", args=[self.location.slug]))
        self.assertContains(response, 'title="Add alias"')
        self.assertContains(response, "alias-add-form--collapsed")


class LocationAliasNicknameTests(TestCase):
    """Creating and toggling nickname-only wiki aliases."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def test_create_alias_with_nickname_checkbox_sets_nickname_kind(self) -> None:
        response = self.client.post(
            reverse("location.wiki.aliases", args=[self.location.slug]),
            {"name": "The Old Grain Place", "is_nickname": "1"},
        )
        self.assertEqual(response.status_code, 200)
        alias = self.wiki.aliases.get(name="The Old Grain Place")
        self.assertEqual(alias.kind, AliasType.NICKNAME)

    def test_create_alias_without_checkbox_is_not_nickname(self) -> None:
        response = self.client.post(reverse("location.wiki.aliases", args=[self.location.slug]), {"name": "Formal Name"})
        self.assertEqual(response.status_code, 200)
        alias = self.wiki.aliases.get(name="Formal Name")
        self.assertEqual(alias.kind, AliasType.ALTERNATE)

    def test_toggle_nickname_flips_kind(self) -> None:
        alias = baker.make(WikiAlias, wiki=self.wiki, name="Toggle Me", kind=AliasType.OFFICIAL, source="google_places")
        response = self.client.post(reverse("location.wiki.alias.toggle_nickname", args=[self.location.slug, alias.id]))
        self.assertEqual(response.status_code, 200)
        alias.refresh_from_db()
        self.assertTrue(alias.is_nickname)

        response = self.client.post(reverse("location.wiki.alias.toggle_nickname", args=[self.location.slug, alias.id]))
        self.assertEqual(response.status_code, 200)
        alias.refresh_from_db()
        self.assertFalse(alias.is_nickname)
        self.assertEqual(alias.kind, AliasType.ALTERNATE)


class PersistOfficialAliasesForLocationBackfillsPinsTests(TestCase):
    """persist_official_aliases_for_location() backfills PinAlias rows too, not just WikiAlias.

    Regression coverage: it used to only call _add_wiki_aliases, so a pin
    whose location's external data was cached by something other than that
    pin's own panel fetch (background enrichment, another user's pin at the
    same location triggering the fetch first, ...) could go on showing no
    aliases indefinitely even after the wiki for the same location had them.
    """

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name=None)

    def _candidates(self):
        from urbanlens.dashboard.services.locations.name_resolution import NameCandidate

        return [NameCandidate(name="External Name", source="nominatim")]

    def test_backfills_both_wiki_and_pin_aliases(self) -> None:
        from urbanlens.dashboard.services.locations.naming import persist_official_aliases_for_location

        with patch("urbanlens.dashboard.services.locations.naming.external_name_candidates_for_location", return_value=self._candidates()):
            changed = persist_official_aliases_for_location(self.location)

        self.assertTrue(changed)
        self.assertTrue(self.wiki.aliases.filter(name="External Name").exists())
        self.assertTrue(self.pin.aliases.filter(name="External Name").exists())

    def test_pin_alias_view_triggers_the_backfill(self) -> None:
        self.client.force_login(self.user)
        with patch("urbanlens.dashboard.controllers.aliases.persist_official_aliases_for_location", return_value=True) as mocked:
            response = self.client.get(reverse("pin.aliases", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)
        mocked.assert_called_once_with(self.location)


class SharedAliasesExplainerDismissalTests(TestCase):
    """The pin-details and wiki aliases panels share one explainer dismissal key.

    Regression coverage: they used to render with different explainer_id
    values ("pin-aliases-explainer" vs "location-aliases-explainer"), so
    dismissing the "What are aliases and nicknames?" explainer on one page
    had no effect on the other, even though it's the same explanation of the
    same feature.
    """

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Curated Mill")
        self.client.force_login(self.user)

    def test_pin_and_wiki_panels_use_the_same_explainer_id(self) -> None:
        pin_response = self.client.get(reverse("pin.aliases", args=[self.pin.slug]))
        wiki_response = self.client.get(reverse("location.wiki.aliases", args=[self.location.slug]))

        self.assertContains(pin_response, 'data-explainer-id="aliases-explainer"')
        self.assertContains(wiki_response, 'data-explainer-id="aliases-explainer"')
