"""Render-time cost of one more conversation in the DM sidebar (P69 - deliberately unpaginated, never measured)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.render_scaling import RenderTimeScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.profile.model import Profile


class ConversationListRowCostTests(RenderTimeScalingMixin, TestCase):
    """The DM sidebar lists every conversation the profile has with no cap or slice.

    ``ConversationListQueryScalingTests`` already proved this stays flat on query count, but that
    test can't see render-time cost, and the list is re-fetched on nearly every DM sent anywhere in
    the app - each row also calls ``display_identity_for`` -> ``resolve_visible_identity``, which
    does a ``reverse()`` call per row even with the viewer's visible-profile set pre-resolved.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        """Add *count* more conversations, each a partner with one message in and one out.

        Args:
            count: How many rows to add.
        """
        for _ in range(count):
            partner = baker.make(User).profile
            DirectMessage.objects.create(sender=partner, recipient=self.profile, body="hello there")
            DirectMessage.objects.create(sender=self.profile, recipient=partner, body="hello back")

    def test_an_extra_conversation_does_not_cost_a_fraction_of_the_sidebar(self) -> None:
        self.assert_row_cost_bounded(reverse("messages.list"))
