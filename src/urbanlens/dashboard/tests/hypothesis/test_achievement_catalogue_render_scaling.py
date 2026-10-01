"""Render-time cost of one more award in the full achievement catalogue (P69 - deliberately unpaginated, never measured)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.render_scaling import RenderTimeScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.achievements.model import Achievement
from urbanlens.dashboard.models.profile.model import Profile

#: Held fixed across every seeded achievement so ``compute_values`` computes this one metric's
#: value once for the whole render, regardless of how many achievement rows use it - isolating the
#: per-row list/template cost from the (real, but separately-scaled) cost of a *new* metric.
_METRIC = "pins_created"


class AchievementCatalogueRowCostTests(RenderTimeScalingMixin, TestCase):
    """The full award catalogue lists every active achievement with no cap or slice.

    P69 left it that way on the theory that the number of awards the site defines (not account
    age) bounds it, not because a row was measured and found cheap.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        """Add *count* more active, non-secret achievements on the same metric.

        Args:
            count: How many rows to add.
        """
        existing = Achievement.objects.filter(metric=_METRIC).count()
        for index in range(count):
            baker.make(
                Achievement,
                metric=_METRIC,
                threshold=existing + index + 1,
                is_active=True,
                is_secret=False,
            )

    def test_an_extra_achievement_does_not_cost_a_fraction_of_the_catalogue(self) -> None:
        self.assert_row_cost_bounded(reverse("achievement.list", args=[self.profile.slug]))
