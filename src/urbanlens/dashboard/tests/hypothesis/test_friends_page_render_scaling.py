"""Render-time cost of one more friend row on the full friends page (P69 - deliberately unpaginated, never measured)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.render_scaling import RenderTimeScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile


class FriendsPageRowCostTests(RenderTimeScalingMixin, TestCase):
    """The full friends page renders every accepted connection with no cap or slice.

    P69 left it that way on the theory that friend count bounds it regardless of account age, not
    because a row was measured and found cheap - and each row's ``area`` field is an
    ``EncryptedTextField``, so decrypting it is real per-row work a query-count test would never see.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        """Add *count* more accepted friends, each with a populated (encrypted) area field.

        Args:
            count: How many rows to add.
        """
        for index in range(count):
            other = baker.make_recipe("dashboard.user").profile
            other.area = f"Somewhere {index}"
            other.save()
            baker.make_recipe("dashboard.accepted_friendship", from_profile=self.profile, to_profile=other)

    def test_an_extra_friend_does_not_cost_a_fraction_of_the_page(self) -> None:
        self.assert_row_cost_bounded(reverse("friend.page", args=[self.profile.pk]))
