"""The DM thread's Block button must update the thread in place, not navigate away.

The other buttons in the partner menu (Mute, Block images) are hx-post with
hx-target="#dm-thread-pane" - Block was a bare, non-HTMX `<form>` submit,
copy-pasted from the profile page's own Block button (where a full-page
redirect to "the same profile page" is correct because the user is already
there). Inside the DM thread panel it instead ejected the user from their
conversation to the just-blocked person's profile page. This pins that the
button is now HTMX, targets #dm-thread-pane, and the re-rendered thread
reflects the block (composer locked) instead of returning a placeholder
string.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice


class DmBlockButtonHtmxTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.partner_user = baker.make(User)
        self.partner = self.partner_user.profile
        Profile.objects.filter(pk=self.partner.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
        self.partner.refresh_from_db()
        self.partner.ensure_slug()
        self.client.force_login(self.user)

    def _url(self) -> str:
        return reverse("friend.block", args=[self.partner.pk])

    def test_the_thread_markup_uses_htmx_for_block(self) -> None:
        response = self.client.get(reverse("messages.conversation", args=[self.partner.slug]), HTTP_HX_REQUEST="true")

        body = response.content.decode()
        self.assertIn(f'hx-post="{self._url()}"', body)
        self.assertIn('hx-target="#dm-thread-pane"', body)

    def test_blocking_via_htmx_returns_the_thread_partial_not_a_redirect(self) -> None:
        response = self.client.post(self._url(), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="dm-thread"', response.content)

    def test_blocking_via_htmx_creates_the_block(self) -> None:
        self.client.post(self._url(), HTTP_HX_REQUEST="true")

        self.assertTrue(
            Friendship.objects.filter(
                from_profile=self.profile, to_profile=self.partner, status=FriendshipStatus.BLOCKED
            ).exists()
        )

    def test_the_rerendered_thread_has_the_composer_locked(self) -> None:
        response = self.client.post(self._url(), HTTP_HX_REQUEST="true")

        self.assertNotIn(b'id="dm-composer"', response.content)

    def test_blocking_without_htmx_still_redirects_as_before(self) -> None:
        """The profile page's own Block form (a plain submit) is untouched."""
        response = self.client.post(self._url())

        self.assertEqual(response.status_code, 302)
        self.assertIn(self.partner.slug, response["Location"])
