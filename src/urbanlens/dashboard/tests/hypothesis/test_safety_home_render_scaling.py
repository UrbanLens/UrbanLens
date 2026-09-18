"""Render-time cost of one more check-in on the safety overview page (P69 - deliberately unpaginated, never measured)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.render_scaling import RenderTimeScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact


class SafetyHomeRowCostTests(RenderTimeScalingMixin, TestCase):
    """The safety overview page lists every check-in the profile owns with no cap or slice.

    P69 left it that way on the theory that ``SafetyPreference.auto_delete_after_days`` bounds it
    (it's nullable and defaults to "never", so in practice it often doesn't), not because a row was
    measured and found cheap.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        """Add *count* more resolved check-ins, each with a couple of emergency contacts.

        Args:
            count: How many rows to add.
        """
        for index in range(count):
            checkin = baker.make(SafetyCheckin, profile=self.profile, title=f"Trip {index}")
            baker.make(SafetyCheckinContact, checkin=checkin, email=f"contact-{index}-a@example.com")
            baker.make(SafetyCheckinContact, checkin=checkin, email=f"contact-{index}-b@example.com")

    def test_an_extra_checkin_does_not_cost_a_fraction_of_the_page(self) -> None:
        self.assert_row_cost_bounded(reverse("safety.home"))
