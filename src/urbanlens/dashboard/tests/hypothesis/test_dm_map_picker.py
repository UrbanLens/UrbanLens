"""Tests for the DM "attach a map" flow: DirectMessageMapPickerView (the
"Choose Existing" tab's data source) and the thread composer's single
attach-map button (replacing the old two-button/two-dialog setup).
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice


def _profile() -> Profile:
    return baker.make("auth.User").profile


def _set_dm_visibility(profile: Profile, visibility: str) -> None:
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=visibility)
    profile.refresh_from_db()


class DirectMessageMapPickerViewTests(TestCase):
    """GET messages.attach_map.picker lists only the caller's own maps, optionally filtered."""

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.other = _profile()
        self.client.force_login(self.me.user)

    def test_lists_only_the_caller_own_maps(self) -> None:
        mine = MarkupMap.objects.create(profile=self.me, title="My Map")
        MarkupMap.objects.create(profile=self.other, title="Someone Else's Map")

        response = self.client.get(reverse("messages.attach_map.picker"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("My Map", content)
        self.assertNotIn("Someone Else's Map", content)
        self.assertIn(str(mine.uuid), content)

    def test_search_filters_by_title(self) -> None:
        MarkupMap.objects.create(profile=self.me, title="Downtown Ruins")
        MarkupMap.objects.create(profile=self.me, title="Coastal Bunker")

        response = self.client.get(reverse("messages.attach_map.picker"), {"q": "ruins"})

        content = response.content.decode()
        self.assertIn("Downtown Ruins", content)
        self.assertNotIn("Coastal Bunker", content)

    def test_no_maps_shows_empty_state(self) -> None:
        response = self.client.get(reverse("messages.attach_map.picker"))
        self.assertContains(response, "don't have any maps yet")

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("messages.attach_map.picker"))
        self.assertNotEqual(response.status_code, 200)


class ThreadComposerAttachMapButtonTests(TestCase):
    """The composer toolbar has exactly one attach-map button (draw/existing merged into one dialog's tabs)."""

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.partner = _profile()
        _set_dm_visibility(self.partner, VisibilityChoice.ANYONE)
        self.me.ensure_slug()
        self.partner.ensure_slug()
        self.client.force_login(self.me.user)

    def test_single_attach_map_button_no_second_existing_map_button(self) -> None:
        response = self.client.get(
            reverse("messages.conversation", kwargs={"profile_slug": self.partner.slug}),
            HTTP_HX_REQUEST="true",
        )
        content = response.content.decode()
        self.assertIn('id="dm-attach-map-btn"', content)
        self.assertNotIn('id="dm-attach-existing-map-btn"', content)
