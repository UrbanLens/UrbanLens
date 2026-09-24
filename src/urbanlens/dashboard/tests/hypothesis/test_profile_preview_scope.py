"""Profile preview simulates the ghost viewer for the previewed page's own URLs, not for whatever it links to (G2-11)."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.profile import profile_preview
from urbanlens.dashboard.services.profile.profile_preview import SESSION_KEY


class PreviewScopeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner = baker.make(User).profile
        Profile.objects.filter(pk=self.owner.pk).update(profile_visibility=VisibilityChoice.ANYONE)
        self.owner.refresh_from_db()
        self.other = baker.make(User).profile
        self.client.force_login(self.owner.user)
        self.client.post(reverse("profile.preview", args=[VisibilityChoice.ANYONE]))
        self.preview_path = reverse("profile.view_user", kwargs={"profile_slug": self.owner.slug})
        patcher = mock.patch(
            "urbanlens.dashboard.middleware.create_ghost_viewer", side_effect=profile_preview.create_ghost_viewer
        )
        self.ghost = patcher.start()
        self.addCleanup(patcher.stop)

    def _from_preview_page(self) -> dict[str, str]:
        return {"HTTP_HX_REQUEST": "true", "HTTP_REFERER": f"http://testserver{self.preview_path}"}

    def test_an_unrelated_poll_from_the_preview_page_runs_as_the_owner(self) -> None:
        """The exploit: a Referer is all it took to answer any HTMX GET as the ghost."""
        response = self.client.get(reverse("notifications.unread_count"), **self._from_preview_page())

        self.assertEqual(response.status_code, 200)
        self.ghost.assert_not_called()

    def test_the_previewed_page_itself_runs_as_the_ghost(self) -> None:
        self.client.get(self.preview_path)

        self.ghost.assert_called_once()

    def test_the_pages_own_fragments_run_as_the_ghost_whatever_the_referer(self) -> None:
        self.client.get(reverse("friend.list", args=[self.owner.pk]), HTTP_HX_REQUEST="true")
        self.client.get(reverse("achievement.profile_panel", args=[self.owner.slug]), HTTP_HX_REQUEST="true")

        self.assertEqual(self.ghost.call_count, 2)

    def test_the_same_fragment_for_someone_else_is_not_simulated(self) -> None:
        self.client.get(reverse("friend.list", args=[self.other.pk]), **self._from_preview_page())

        self.ghost.assert_not_called()

    def test_the_profiles_own_subpages_stay_in_the_preview(self) -> None:
        self.client.get(reverse("profile.common_pins", args=[self.owner.slug]), HTTP_ACCEPT="text/html")

        self.ghost.assert_called_once()
        self.assertIn(SESSION_KEY, self.client.session)

    def test_leaving_for_another_page_still_ends_the_preview(self) -> None:
        self.client.get(reverse("settings.view"), HTTP_ACCEPT="text/html")

        self.assertNotIn(SESSION_KEY, self.client.session)
        self.ghost.assert_not_called()

    def test_a_plain_form_post_from_the_preview_page_is_blocked(self) -> None:
        """The hero's buttons are plain forms naming the previewed profile; none may act mid-preview."""
        response = self.client.post(
            reverse("friend.block", args=[self.owner.pk]), HTTP_REFERER=f"http://testserver{self.preview_path}"
        )

        self.assertEqual(response.status_code, 403)

    def test_signing_out_from_the_preview_page_still_works(self) -> None:
        """Writes that do not touch the previewed profile, like the navbar's sign-out, are the owner's own."""
        response = self.client.post(reverse("logout"), HTTP_REFERER=f"http://testserver{self.preview_path}")

        self.assertNotEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)
