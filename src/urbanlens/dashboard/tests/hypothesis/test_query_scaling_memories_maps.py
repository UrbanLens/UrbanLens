"""Memories > Maps renders a card per map without querying per card."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.query_scaling import QueryScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

_FIRST_BATCH = 2
_SECOND_BATCH = 6


class MemoriesMapsQueryScalingTests(QueryScalingMixin, TestCase):
    """Each map card's attachments must be prefetched, not fetched per card."""

    first_batch = _FIRST_BATCH
    second_batch = _SECOND_BATCH

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        for _ in range(count):
            location = baker.make(Location)
            pin = baker.make(Pin, profile=self.profile, location=location)
            markup_map = baker.make(MarkupMap, profile=self.profile)
            # An attachment on each card, so `attachments` has something to walk -
            # a card with none would never reach the relations under test.
            baker.make(Comment, profile=self.profile, pin=pin, markup_map=markup_map)

    def test_map_cards_do_not_query_per_card(self) -> None:
        self.assert_flat("/dashboard/memories/maps/")
