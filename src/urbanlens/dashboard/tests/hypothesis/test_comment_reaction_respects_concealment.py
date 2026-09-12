"""CommentReactionView must apply wiki concealment, like every sibling by-id wiki-comment path already does."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.models.wiki.model import Wiki


class CommentReactionRespectsConcealmentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        self.stranger = baker.make(User).profile
        # comment_visibility defaults to ANYTHING_IN_COMMON, which the viewer
        # and a total stranger share none of - open it up so the only gate
        # under test in either case is concealment itself.
        Profile.objects.filter(pk=self.stranger.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.comment = baker.make(
            "dashboard.Comment", wiki=self.wiki, pin=None, profile=self.stranger, text="a stranger's comment"
        )

        self.viewer_user = baker.make(User)
        # A pin is what grants wiki access at all; concealment is a separate,
        # finer-grained question about this specific stranger's own rows.
        baker.make(Pin, profile=self.viewer_user.profile, location=self.location, parent_pin=None)
        self.client.force_login(self.viewer_user)

    def _url(self) -> str:
        return reverse("comment.react", kwargs={"comment_id": self.comment.pk})

    def test_reacting_to_a_concealed_strangers_comment_is_refused(self) -> None:
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            response = self.client.post(self._url(), {"emoji": "🔥"})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Reaction.objects.filter(comment=self.comment).exists())

    def test_reacting_to_the_same_comment_still_works_when_not_concealed(self) -> None:
        """Anti-vacuity: concealment_active is hardcoded False in production - unmocked, this must keep working."""
        response = self.client.post(self._url(), {"emoji": "🔥"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Reaction.objects.filter(comment=self.comment).exists())
