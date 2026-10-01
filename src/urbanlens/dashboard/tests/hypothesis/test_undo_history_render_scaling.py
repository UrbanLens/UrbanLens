"""Render-time cost of one more undo-history row (P69 - deliberately unpaginated, never measured)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.render_scaling import RenderTimeScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo.model import UndoAction


class UndoHistoryRowCostTests(RenderTimeScalingMixin, TestCase):
    """The settings-page undo panel lists every active entry with no cap or slice.

    P69 left it that way on the theory that the 7-day retention window bounds it regardless of
    account age, not because a row was measured and found cheap. This puts a number on it.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        """Add *count* more active, undoable entries to the profile's stack.

        Args:
            count: How many rows to add.
        """
        existing = UndoAction.objects.filter(profile=self.profile).count()
        for index in range(count):
            baker.make(
                UndoAction,
                profile=self.profile,
                object_repr=f"Thing {existing + index}",
                payload={"note": "row-cost probe"},
            )

    def test_an_extra_undo_entry_does_not_cost_a_fraction_of_the_panel(self) -> None:
        self.assert_row_cost_bounded(reverse("undo.history"))
