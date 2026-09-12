"""The Journal page must not query per entry, or per page of them."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.query_scaling import QueryScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki

_FIRST_BATCH = 3
_SECOND_BATCH = 9


class MemoriesJournalQueryScalingTests(QueryScalingMixin, TestCase):
    """One page of entries, at a query cost that does not follow the account."""

    first_batch = _FIRST_BATCH
    second_batch = _SECOND_BATCH

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)
        self.seeded = 0

    def seed_rows(self, count: int) -> None:
        for _ in range(count):
            self.seeded += 1
            location = baker.make(
                Location, latitude=f"{40 + self.seeded * 0.01:.6f}", longitude=f"{-74 + self.seeded * 0.01:.6f}"
            )
            # A wiki on the location, since that is the last hop of the chain
            # each entry's title walks - without one the relation is never read.
            baker.make(Wiki, location=location, name=f"Wiki {self.seeded}")
            pin = baker.make(Pin, profile=self.profile, location=location)
            baker.make(
                PinVisit,
                pin=pin,
                visited_at=timezone.now() - datetime.timedelta(days=self.seeded),
                notes=f"note {self.seeded}",
            )

    def test_journal_entries_do_not_query_per_entry(self) -> None:
        self.assert_flat("/dashboard/memories/journal/")
