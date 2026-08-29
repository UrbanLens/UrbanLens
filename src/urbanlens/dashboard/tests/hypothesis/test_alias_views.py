"""Tests for the alias HTMX views: current-name marking, delete guard, "use this name"."""

from __future__ import annotations

import json
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

    def test_delete_requires_pin_ownership(self) -> None:
        other_pin = baker.make(Pin, profile=baker.make("auth.User").profile, name="Not Yours")
        alias = other_pin.aliases.get(name="Not Yours")
        response = self.client.delete(reverse("pin.alias.delete", args=[other_pin.slug, alias.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(other_pin.aliases.filter(name="Not Yours").exists())


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

    def test_create_alias_with_duplicate_name_is_rejected(self) -> None:
        baker.make(PinAlias, pin=self.pin, name="Existing Name")
        response = self.client.post(
            reverse("pin.aliases", args=[self.pin.slug]),
            {"name": "existing name"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.pin.aliases.filter(name__iexact="existing name").count(), 1)

    def test_create_alias_that_sanitizes_to_empty_is_rejected(self) -> None:
        """A name made only of stripped characters must be rejected with 400, not crash.

        ``create_pin_alias`` sanitizes the name internally and raises
        ``ValueError`` when nothing survives (see ``test_alias_name_validation.py``),
        but this view's own pre-check only catches an *already*-empty submission
        and does not catch that ``ValueError`` - unlike the wiki alias view's
        equivalent branch, which sanitizes before checking for emptiness for
        exactly this reason (see ``LocationAliasNicknameTests`` in this file).
        Currently reproduces a real bug: see audit concerns.
        """
        response = self.client.post(reverse("pin.aliases", args=[self.pin.slug]), {"name": "<>"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.pin.aliases.filter(name="").exists())


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

    def test_create_alias_with_duplicate_name_is_rejected(self) -> None:
        baker.make(WikiAlias, wiki=self.wiki, name="Existing Name")
        response = self.client.post(
            reverse("location.wiki.aliases", args=[self.location.slug]),
            {"name": "existing name"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.wiki.aliases.filter(name__iexact="existing name").count(), 1)

    def test_create_alias_that_sanitizes_to_empty_is_rejected(self) -> None:
        """A name made only of stripped characters must not silently become a blank alias.

        Contrast with ``PinAliasNicknameTests.test_create_alias_that_sanitizes_to_empty_is_rejected``:
        this view sanitizes *before* checking for emptiness, precisely to avoid
        the bug that check reproduces on the pin side.
        """
        response = self.client.post(reverse("location.wiki.aliases", args=[self.location.slug]), {"name": "<>"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.wiki.aliases.filter(name="").exists())

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


class AliasPanelHeaderTitleAlignmentTests(TestCase):
    """The "Aliases" card-header title sat visibly out of place compared to
    every other section on the wiki/pin page. Root cause: the explainer
    anchor icon was rendered as its own sibling *before* the title <span>,
    instead of nested inside it like every other page's title does (see
    _page_explainer_anchor.html's own docstring example) - .card-header's CSS
    grid explicitly excludes `.ul-explainer-anchor` from the title's grid
    area (`> span:not(...):not(.ul-explainer-anchor)`), so the anchor got
    auto-placed into a stray grid cell instead, throwing the header's layout
    off. Moving the include inside the <span> fixes it without touching the
    shared header CSS at all.
    """

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Title Alignment Pin")
        self.client.force_login(self.user)

    def test_explainer_anchor_is_nested_inside_the_title_span(self) -> None:
        content = self.client.get(reverse("pin.aliases", args=[self.pin.slug])).content.decode()
        title_start = content.index("<span>Aliases")
        title_end = content.index("</span>", title_start)
        anchor_start = content.index('id="aliases-explainer-anchor"')
        self.assertLess(title_start, anchor_start)
        self.assertLess(anchor_start, title_end)


class LocationAliasUseGoesThroughTheServiceTests(TestCase):
    """The wiki "use this name" view must not re-implement the rename itself.

    It used to assign ``wiki.name`` and hand-write a ``WikiEdit`` row, which is
    a second implementation of ``services.wiki.wiki_aliases.promote_wiki_alias_to_name``
    and carried two defects the service does not have. Both are covered here so
    a future re-inlining of the logic fails loudly rather than quietly
    reintroducing them.
    """

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.410000", longitude="-73.410000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def test_promoting_the_name_the_wiki_already_has_writes_no_history(self) -> None:
        """A no-op rename must leave the audit trail untouched.

        The old inline version always wrote a ``WikiEdit``, so promoting the
        alias that was already the name produced a junk
        ``{"name": {"from": "X", "to": "X"}}`` row. Those rows are not
        cosmetic: the history is what people read to see who changed what, and
        a client retrying a request whose response it never saw could pad it
        with edits that changed nothing.
        """
        alias = self.wiki.aliases.get(name="Curated Mill")
        before = WikiEdit.objects.filter(wiki=self.wiki).count()

        response = self.client.post(reverse("location.wiki.alias.use", args=[self.location.slug, alias.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(WikiEdit.objects.filter(wiki=self.wiki).count(), before)
        # Nothing happened, so nothing is announced - no rename toast and no
        # wikiRenamed event for other page components to react to.
        self.assertNotIn("HX-Trigger", response.headers)

    def test_announced_name_is_the_one_that_was_actually_stored(self) -> None:
        """The toast and the wikiRenamed event must report the sanitized name.

        ``Wiki.save()`` runs the incoming name through ``sanitize_name``, so the
        alias text and the stored name can differ. The old inline version echoed
        the raw alias, telling the user the place had been renamed to something
        that is not what the database now holds. The alias row here is written
        with ``.update()`` to bypass ``WikiAlias.save()``'s own sanitizing, which
        is how a row predating that sanitizer would look.
        """
        alias = baker.make(WikiAlias, wiki=self.wiki, name="Placeholder Mill")
        raw_name = "Restored <b>Mill</b>"
        WikiAlias.objects.filter(pk=alias.pk).update(name=raw_name)

        response = self.client.post(reverse("location.wiki.alias.use", args=[self.location.slug, alias.id]))

        self.assertEqual(response.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertNotEqual(self.wiki.name, raw_name)
        triggers = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(triggers["wikiRenamed"]["name"], self.wiki.name)
        self.assertIn(self.wiki.name, triggers["showToast"]["message"])
        self.assertNotIn(raw_name, triggers["showToast"]["message"])
