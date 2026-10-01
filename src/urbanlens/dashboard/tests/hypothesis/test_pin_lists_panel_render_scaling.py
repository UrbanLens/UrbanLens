"""Render-time cost of one more list on the Organize "Lists" tab (P69 - deliberately unpaginated, never measured)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.render_scaling import RenderTimeScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.profile.model import Profile


class PinListsPanelRowCostTests(RenderTimeScalingMixin, TestCase):
    """Organize's "Lists" tab renders every list the profile owns with no cap or slice.

    Structurally invisible to ``test_route_query_scaling.py``'s generic sweep, which hits
    ``lists.list`` without an ``HX-Request`` header and only ever exercises its redirect branch -
    the real panel render needs that header, so it's passed here as an extra client kwarg.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        """Add *count* more of the profile's pin lists, each with a description.

        Args:
            count: How many rows to add.
        """
        existing = PinList.objects.filter(profile=self.profile).count()
        for index in range(count):
            baker.make(
                PinList,
                profile=self.profile,
                name=f"List {existing + index}",
                description=f"Description {existing + index}",
            )

    def test_an_extra_list_does_not_cost_a_fraction_of_the_panel(self) -> None:
        self.assert_row_cost_bounded(reverse("lists.list"), HTTP_HX_REQUEST="true")
