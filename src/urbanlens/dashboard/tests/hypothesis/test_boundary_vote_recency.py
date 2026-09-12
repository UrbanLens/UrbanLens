"""Re-affirming a boundary vote must refresh its recency weight."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote
from urbanlens.dashboard.services.geo.boundary_voting import cast_boundary_vote, vote_weight


class BoundaryVoteRecencyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile

    def test_vote_weight_decays_with_age(self) -> None:
        """Anti-vacuity: the weighting this test depends on must actually decay."""
        now = timezone.now()
        fresh = vote_weight(now, now)
        old = vote_weight(now - datetime.timedelta(days=60), now)
        self.assertGreater(fresh, old, "weights do not decay - the recency test below would prove nothing")

    def test_re_affirming_the_same_choice_moves_the_updated_timestamp(self) -> None:
        from django.contrib.gis.geos import MultiPolygon, Polygon

        from urbanlens.dashboard.models.boundary.model import BoundarySource, BoundaryType
        from urbanlens.dashboard.models.place.model import Place

        place = baker.make(Place)
        # A votable candidate is externally-sourced, property-typed, has real
        # geometry, and belongs to no pin/wiki/profile (see boundary_options).
        boundary = baker.make(
            "dashboard.Boundary",
            place=place,
            pin=None,
            wiki=None,
            profile=None,
            source=BoundarySource.REDATA.value,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=MultiPolygon(Polygon(((0, 0), (0, 1), (1, 1), (1, 0), (0, 0)))),
        )

        vote = cast_boundary_vote(place, self.profile, boundary.pk)
        stale = timezone.now() - datetime.timedelta(days=30)
        BoundaryVote.objects.filter(pk=vote.pk).update(updated=stale)

        cast_boundary_vote(place, self.profile, boundary.pk)

        vote.refresh_from_db()
        self.assertGreater(
            vote.updated,
            stale,
            "re-affirming did not refresh `updated` - the vote keeps decaying, contradicting cast_boundary_vote's documented contract",
        )
        self.assertEqual(
            BoundaryVote.objects.filter(place=place, profile=self.profile).count(),
            1,
            "re-voting must update the row, never add a second",
        )
