"""Tests for the pin detail page hero: click-to-rename title, and the layout
fix that stopped the eyebrow/title/subtitle/back-link/wiki-box from
overlapping (see _pin_detail_hero_body.html, _page_hero.scss).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class PinHeroEditableNameTests(TestCase):
    """The hero title renders as a click-to-rename element, wired to pin.edit."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        user = baker.make(User)
        self.profile = Profile.objects.get(user=user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, name="Old Mill", name_is_user_provided=True)
        self.client.force_login(user)

    def _get(self):
        return self.client.get(reverse("pin.details", args=[self.pin.slug]))

    def test_hero_title_is_marked_editable(self) -> None:
        response = self._get()
        self.assertContains(response, "pin-name-editable")

    def test_hero_title_carries_the_raw_name_for_the_edit_input(self) -> None:
        response = self._get()
        self.assertContains(response, 'data-raw-name="Old Mill"')

    def test_hero_title_wiring_posts_to_pin_edit(self) -> None:
        response = self._get()
        self.assertContains(response, reverse("pin.edit", args=[self.pin.slug]))

    def test_hero_title_falls_back_to_the_location_derived_name(self) -> None:
        """A pin with no user-provided name still shows (and is editable
        starting from) whatever name is actually displayed - the location's
        name, not a blank field."""
        unnamed_pin = baker.make(Pin, profile=self.profile, name=None)
        response = self.client.get(reverse("pin.details", args=[unnamed_pin.slug]))
        self.assertContains(response, f'data-raw-name="{unnamed_pin.effective_name}"')

    def test_renaming_via_pin_edit_updates_the_displayed_name(self) -> None:
        """End-to-end: the endpoint the hero's inline editor posts to actually
        renames the pin (already covered in depth by PinEditNameAliasTests -
        this just confirms the hero's own markup/endpoint pairing is real)."""
        response = self.client.post(reverse("pin.edit", args=[self.pin.slug]), {"name": "New Mill"})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "New Mill")


class PinHeroLayoutTests(TestCase):
    """The hero's markup no longer positions the wiki box independently of
    the title/subtitle block (see _page_hero.scss's removal of
    position:absolute from .pin-hero-wiki-box, the reported "clustering")."""

    def setUp(self) -> None:
        baker.make(User)
        user = baker.make(User)
        self.profile = Profile.objects.get(user=user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, name="Old Mill")
        self.client.force_login(user)

    def test_hero_body_is_a_single_stacked_column(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertContains(response, "pin-detail-hero-body")
        self.assertContains(response, "pin-detail-hero-main")
