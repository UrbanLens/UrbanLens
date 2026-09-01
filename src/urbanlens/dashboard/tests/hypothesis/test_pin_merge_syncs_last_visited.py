"""Merging pins recomputes the survivor's last_visited from the visits it absorbed.

``merge_pins`` repoints the loser's ``PinVisit`` rows to the survivor with
``queryset.update()``. ``Pin.last_visited`` is a denormalized copy of the newest
such row, maintained by ``sync_last_visited`` - so absorbing a more recently
visited pin left the survivor advertising an older date than its own visit history
supports, on both the map popup and the Private Pin page.

Fixing it also settles a second staleness: ``sync_last_visited`` saves the pin,
which fires the ``post_save`` receiver that refreshes the cached map payload. The
merge previously issued no cache invalidation for the survivor at all, despite the
survivor gaining visits, images and labels.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.pins.pin_merge import merge_pins


class PinMergeSyncsLastVisitedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.now = timezone.now()

    def _pin_with_visit(self, *, visited_at) -> Pin:
        pin = baker.make(Pin, profile=self.profile, last_visited=visited_at)
        baker.make(PinVisit, pin=pin, visited_at=visited_at)
        return pin

    def test_the_survivor_takes_the_newest_absorbed_visit(self) -> None:
        old = self.now - timedelta(days=90)
        recent = self.now - timedelta(days=2)
        survivor = self._pin_with_visit(visited_at=old)
        loser = self._pin_with_visit(visited_at=recent)

        merged = merge_pins(survivor, loser, self.profile)

        self.assertEqual(
            merged.last_visited,
            recent,
            "survivor absorbed a more recent visit but still advertises its own older date",
        )

    def test_a_survivor_already_newer_keeps_its_own_date(self) -> None:
        recent = self.now - timedelta(days=2)
        old = self.now - timedelta(days=90)
        survivor = self._pin_with_visit(visited_at=recent)
        loser = self._pin_with_visit(visited_at=old)

        merged = merge_pins(survivor, loser, self.profile)

        self.assertEqual(merged.last_visited, recent)

    def test_a_survivor_with_no_visits_at_all_stays_never_visited(self) -> None:
        survivor = baker.make(Pin, profile=self.profile, last_visited=None)
        loser = baker.make(Pin, profile=self.profile, last_visited=None)

        merged = merge_pins(survivor, loser, self.profile)

        self.assertIsNone(merged.last_visited)
