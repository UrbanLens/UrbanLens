"""Tests for the pin detail page's "Organization" combined label/list dialog.

Covers three related bugs fixed together:

- The "+" button used to open a dropdown choosing between two separate dialogs
  (one for labels, one for lists). It now opens a single dialog with "Labels"
  and "Lists" tabs (``_label_dialog.html``), and only the pin route gets the
  "Lists" tab - location/image label dialogs are unaffected.
- The custom fields "add a field" form was only hidden once the pin already
  had at least one field, so it showed unprompted on pins with none.
- A pin whose (legacy) ``Location`` had no slug hid the wiki create/view
  button entirely instead of falling back or backfilling the slug.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin


class PinOrganizeDialogTests(TestCase):
    """The pin detail page's combined Organize ("+") dialog."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude="40.000000", longitude="-74.000000")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=location, name="Test Pin")

    def test_organize_add_button_opens_dialog_directly(self) -> None:
        """The "+" button should showModal() the dialog directly - no more dropdown menu."""
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertNotContains(response, "organization-add-menu")
        self.assertContains(response, f"category-add-dialog-{self.pin.slug}")

    def test_label_panel_includes_lists_tab_with_pin_lists(self) -> None:
        baker.make("dashboard.PinList", profile=self.profile, name="My Favorites")
        response = self.client.get(reverse("label.pin", kwargs={"label_kind": "tag", "pin_slug": self.pin.slug}))
        self.assertContains(response, "tad-top-tab")
        self.assertContains(response, "My Favorites")
        self.assertContains(response, "add-to-list-results")

    def test_location_label_dialog_has_no_lists_tab(self) -> None:
        """Only the pin route gets the Lists tab - locations have no PinList concept."""
        wiki = baker.make("dashboard.Wiki", location=self.pin.location)
        response = self.client.get(reverse("label.location", kwargs={"label_kind": "tag", "location_slug": self.pin.location.slug}))
        self.assertNotContains(response, "tad-top-tabs")
        del wiki


class CustomFieldsAddFormVisibilityTests(TestCase):
    """The pin detail page's Custom Fields "+" form should start hidden."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude="41.000000", longitude="-75.000000")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=location, name="Field Test Pin")

    def test_add_form_hidden_when_pin_has_no_custom_fields(self) -> None:
        response = self.client.get(reverse("pin.custom_fields", args=[self.pin.slug]))
        self.assertContains(response, 'class="cf-add-form" hidden')

    def test_add_form_still_hidden_when_pin_already_has_custom_fields(self) -> None:
        """The add form must start hidden regardless of whether any rows are shown above it."""
        baker.make("dashboard.CustomField", profile=self.profile, entity_type="pin", name="Gate Code")
        response = self.client.get(reverse("pin.custom_fields", args=[self.pin.slug]))
        self.assertContains(response, "Gate Code")
        self.assertContains(response, 'class="cf-add-form" hidden')


class PinWikiLinkVisibilityTests(TestCase):
    """A pin's wiki create/view link must render even when the Location predates slugs."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_legacy_location_without_slug_gets_backfilled_and_shows_create_button(self) -> None:
        # `route` set so PinOverviewView's reverse-geocode backfill (unrelated
        # to this test) short-circuits instead of making a live API call.
        location = baker.make("dashboard.Location", latitude="42.000000", longitude="-76.000000", route="Main St")
        pin = baker.make("dashboard.Pin", profile=self.profile, location=location, name="Legacy Pin")
        # Location.save() always auto-generates a slug, so simulate a legacy row
        # that predates slug generation by clearing it out from under save().
        type(location).objects.filter(pk=location.pk).update(slug=None)
        location.refresh_from_db()
        self.assertIsNone(location.slug)

        # First hit is the main page load, which backfills pin.location.slug.
        self.client.get(reverse("pin.details", args=[pin.slug]))
        location.refresh_from_db()
        self.assertIsNotNone(location.slug)

        # The overview HTMX fragment (loaded right after) should now show the
        # "Create Community Wiki" button instead of rendering nothing.
        overview = self.client.get(reverse("pin.overview", args=[pin.slug]))
        self.assertContains(overview, "Create Community Wiki")

    def test_overview_fragment_backfills_the_slug_even_without_a_prior_page_load(self) -> None:
        """PinOverviewView must not depend on PinController.view having run first.

        The backfill used to live only in PinController.view - a direct hit to
        the HTMX overview endpoint (e.g. a bookmarked fragment URL, or any
        future caller that skips the full page load) would render with the
        wiki link permanently hidden even though nothing else was wrong with
        the pin.
        """
        location = baker.make("dashboard.Location", latitude="43.000000", longitude="-77.000000", route="Main St")
        pin = baker.make("dashboard.Pin", profile=self.profile, location=location, name="Legacy Pin 2")
        type(location).objects.filter(pk=location.pk).update(slug=None)
        location.refresh_from_db()
        self.assertIsNone(location.slug)

        overview = self.client.get(reverse("pin.overview", args=[pin.slug]))
        self.assertContains(overview, "Create Community Wiki")
        location.refresh_from_db()
        self.assertIsNotNone(location.slug)
