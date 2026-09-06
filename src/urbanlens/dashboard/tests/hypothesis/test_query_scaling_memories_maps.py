"""Memories > Maps renders a card per map without querying per card.

From P68's survey, where it had no `QueryScalingMixin` subclass and no
`django_perf_rec` record - which is how it stayed that way.

The view selected `shared_by__user` and prefetched `items`, and nothing else,
while `MarkupMap.attachments` walks six more reverse relations
(`safety_checkins`, `attached_safety_checkins`, `comments`, `trip_comments`,
`visits`, `direct_messages`). Adding those prefetches alone changed nothing: the
properties called `.first()` and `.select_related(...)` on each manager, and both
build a *new* queryset, so they query straight past a prefetch. Reading
`_prefetched_objects_cache` first is what makes the prefetch count.

Three rounds of measurement, each naming the next relation, ending flat:
58/112 queries at 2/8 cards, then 44/56 once the properties honoured the
prefetch, then 42/48 with `pin__location`, then flat with
`pin__location__wiki` - `Location.display_name` reads its own wiki, and says so
in its docstring.

**The site-admin user directory, the other half of this survey, is not here.**
Resolving `active_subscription_roles` once per row instead of twice took it from
190 queries to 150 at 8 rows, but it still costs about 15 per row: the viewer's
`can_view_contact_info`/`can_view_profile` are resolved per listed profile, and
batching those is a change to how visibility is computed, not a prefetch. A test
asserting flatness there would ship red, so the measurement is in P68 instead.
"""

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
