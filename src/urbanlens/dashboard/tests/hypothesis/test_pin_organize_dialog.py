"""Tests for the Private Pin page's "Organization" combined label/list dialog.

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
    """The Private Pin page's combined Organize ("+") dialog."""

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

    def test_lists_tab_add_button_uses_the_list_slug_not_a_nonexistent_uuid(self) -> None:
        """Regression guard: this dialog's per-list "add" button called
        addPinsToList(pin_list.uuid) - PinList has no uuid field, so Django
        silently rendered an empty string, calling addPinsToList('') and
        making every add-to-list attempt from the pin details page 404
        (the map page's own add-to-list dialog already used .slug correctly)."""
        pin_list = baker.make("dashboard.PinList", profile=self.profile, name="My Favorites")

        response = self.client.get(reverse("label.pin", kwargs={"label_kind": "tag", "pin_slug": self.pin.slug}))

        self.assertContains(response, f"addPinsToList('{pin_list.slug}')")
        self.assertNotContains(response, "addPinsToList('')")

    def test_lists_tab_excludes_people_labels(self) -> None:
        """This dialog's own Labels tab already correctly excludes People
        labels (via location_labels()) - locking that in. The actual bug
        reported alongside this was a *different*, unfiltered Label.objects
        query in the CSV/GPX import wizard's label list (see
        test_import_wizard_label_list_excludes_people_labels below)."""
        from urbanlens.dashboard.models.labels.meta import KIND_TAG, KIND_USER
        from urbanlens.dashboard.models.labels.model import Label

        person = baker.make(Label, kind=KIND_USER, profile=self.profile, name="Alex Person")
        tag = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="Regular Tag")

        response = self.client.get(reverse("label.pin", kwargs={"label_kind": "tag", "pin_slug": self.pin.slug}))

        self.assertNotContains(response, "Alex Person")
        self.assertContains(response, "Regular Tag")
        del person, tag

    def test_import_wizard_label_list_excludes_people_labels(self) -> None:
        """Regression guard: the CSV/GPX import wizard's per-row label
        dropdown was built from an unfiltered Label.objects.visible_to(...)
        query, so People labels (which can't be applied to pins) leaked into
        it even though every other pin-label picker already excludes them."""
        from urbanlens.dashboard.models.labels.meta import KIND_TAG, KIND_USER
        from urbanlens.dashboard.models.labels.model import Label

        baker.make(Label, kind=KIND_USER, profile=self.profile, name="Alex Person")
        baker.make(Label, kind=KIND_TAG, profile=self.profile, name="Regular Tag")

        labels = list(Label.objects.visible_to(self.profile).location_labels().ordered())

        self.assertIn("Regular Tag", [b.name for b in labels])
        self.assertNotIn("Alex Person", [b.name for b in labels])


class LabelCreateAndAddTests(TestCase):
    """Inline "create a new label" from the pin/wiki Add Labels dialog.

    Mirrors LabelImageMembershipView's existing create_and_add support (see
    test_media_labels.py) - the same action, extended to the pin and location
    membership routes.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude="40.100000", longitude="-74.100000")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=location, name="Label Create Test Pin")

    def test_dialog_renders_a_create_row_for_the_pin_route(self) -> None:
        response = self.client.get(reverse("label.pin", kwargs={"label_kind": "tag", "pin_slug": self.pin.slug}))
        self.assertContains(response, "tad-create-row")

    def test_create_and_add_makes_a_tag_and_applies_it_to_the_pin(self) -> None:
        from urbanlens.dashboard.models.labels.meta import KIND_TAG
        from urbanlens.dashboard.models.labels.model import Label

        response = self.client.post(
            reverse("label.pin", kwargs={"label_kind": "tag", "pin_slug": self.pin.slug}),
            data={"action": "create_and_add", "name": "Freshly Explored"},
        )
        self.assertEqual(response.status_code, 200)
        created = Label.objects.get(profile=self.profile, kind=KIND_TAG, name="Freshly Explored")
        self.assertIn(created.id, set(self.pin.labels.values_list("id", flat=True)))

    def test_create_and_add_reuses_an_existing_tag_of_that_name(self) -> None:
        from urbanlens.dashboard.models.labels.meta import KIND_TAG
        from urbanlens.dashboard.models.labels.model import Label

        existing = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="Overgrown")

        response = self.client.post(
            reverse("label.pin", kwargs={"label_kind": "tag", "pin_slug": self.pin.slug}),
            data={"action": "create_and_add", "name": "Overgrown"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Label.objects.filter(profile=self.profile, kind=KIND_TAG, name="Overgrown").count(), 1)
        self.assertIn(existing.id, set(self.pin.labels.values_list("id", flat=True)))

    def test_create_and_add_requires_a_name(self) -> None:
        response = self.client.post(
            reverse("label.pin", kwargs={"label_kind": "tag", "pin_slug": self.pin.slug}),
            data={"action": "create_and_add", "name": ""},
        )
        self.assertEqual(response.status_code, 400)

    def test_location_route_also_supports_create_and_add(self) -> None:
        from urbanlens.dashboard.models.labels.meta import KIND_TAG
        from urbanlens.dashboard.models.labels.model import Label

        wiki = baker.make("dashboard.Wiki", location=self.pin.location)
        response = self.client.post(
            reverse("label.location", kwargs={"label_kind": "tag", "location_slug": self.pin.location.slug}),
            data={"action": "create_and_add", "name": "Rooftop Access"},
        )
        self.assertEqual(response.status_code, 200)
        created = Label.objects.get(profile=self.profile, kind=KIND_TAG, name="Rooftop Access")
        self.assertIn(created.id, set(wiki.labels.values_list("id", flat=True)))


class CustomFieldsAddFormVisibilityTests(TestCase):
    """The Private Pin page's Custom Fields "+" form should start hidden."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude="41.000000", longitude="-75.000000")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=location, name="Field Test Pin")

    def test_add_form_hidden_when_pin_has_no_custom_fields(self) -> None:
        response = self.client.get(reverse("pin.custom_fields", args=[self.pin.slug]))
        self.assertContains(response, 'class="cf-add-form cf-def-form" hidden')

    def test_add_form_still_hidden_when_pin_already_has_custom_fields(self) -> None:
        """The add form must start hidden regardless of whether any rows are shown above it."""
        baker.make("dashboard.CustomField", profile=self.profile, entity_type="pin", name="Gate Code")
        response = self.client.get(reverse("pin.custom_fields", args=[self.pin.slug]))
        self.assertContains(response, "Gate Code")
        self.assertContains(response, 'class="cf-add-form cf-def-form" hidden')


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
        # "Share to Community Wiki" button instead of rendering nothing.
        overview = self.client.get(reverse("pin.overview", args=[pin.slug]))
        self.assertContains(overview, "Share to Community Wiki")

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
        self.assertContains(overview, "Share to Community Wiki")
        location.refresh_from_db()
        self.assertIsNotNone(location.slug)
