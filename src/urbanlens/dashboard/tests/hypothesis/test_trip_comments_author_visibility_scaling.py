"""The trip comment tree re-asks "may I see this author" once per comment.

The trip half of N21 H43/H52. `visible_comment_tree`, which the pin and wiki
panels use, builds a `can_view` dict keyed by author precisely because - in its
own words - "each call runs up to 3 extra query pairs, which is redundant when
one author appears several times in a thread".

`trips.trip_comments.build_comment_tree` copied the gate and not the dict. Its
comment even says the check works "exactly as pin/wiki comments already do",
which is true of the gate and not of the memo beside it. So a trip thread calls
`can_view_comments_from` once per top-level comment and again per reply, and at
the `COMMON_PIN` visibility setting that reaches `_have_common_pin`, which reads
both accounts' entire pin sets into Python.

A trip thread is exactly where one author appears many times.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.services.trips.trip_comments import build_comment_tree


class _TripThreadCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = baker.make(User).profile
        self.author = baker.make(User).profile

        # COMMON_PIN is the setting that makes the check expensive: it sends
        # can_view_comments_from through _have_common_pin, which scans both.
        self.author.comment_visibility = VisibilityChoice.COMMON_PIN
        self.author.save(update_fields=["comment_visibility"])
        location = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=location)
        baker.make(Pin, profile=self.author, location=location)

        self.trip = baker.make(Trip, creator=self.viewer, name="Shared Trip")
        for profile in (self.viewer, self.author):
            TripMembership.objects.create(trip=self.trip, profile=profile, status=TripMembership.STATUS_JOINED)

    def _add_comments(self, count: int) -> None:
        for index in range(count):
            TripComment.objects.create(trip=self.trip, author=self.author, text=f"comment {index}")


class TheAuthorCheckIsAnsweredOnceTests(_TripThreadCase):
    """One author, many comments: the verdict cannot differ between them."""

    def _queries_for(self, comments: int) -> int:
        self._add_comments(comments)
        with CaptureQueriesContext(connection) as captured:
            tree = build_comment_tree(self.trip, self.viewer)
        assert len(tree) == comments, (len(tree), comments)  # nosec B101
        return len(captured.captured_queries)

    def test_more_comments_from_one_author_do_not_cost_more_queries(self) -> None:
        small = self._queries_for(2)
        TripComment.objects.all().delete()
        large = self._queries_for(12)

        self.assertLessEqual(
            large - small,
            2,
            f"ten more comments from the same author cost {large - small} more queries",
        )


class TheAnswerIsStillRightTests(_TripThreadCase):
    """A memo that returns the wrong verdict is worse than the cost it saves."""

    def test_a_visible_author_is_still_shown(self) -> None:
        self._add_comments(2)

        self.assertEqual(len(build_comment_tree(self.trip, self.viewer)), 2)

    def test_a_hidden_author_is_still_dropped(self) -> None:
        """The anti-vacuity half: a memo that always says yes would show these."""
        self.author.comment_visibility = VisibilityChoice.NO_ONE
        self.author.save(update_fields=["comment_visibility"])
        self._add_comments(2)

        self.assertEqual(build_comment_tree(self.trip, self.viewer), [])

    def test_two_authors_are_answered_separately(self) -> None:
        """One memo entry per author, not one verdict for the whole thread."""
        hidden = baker.make(User).profile
        hidden.comment_visibility = VisibilityChoice.NO_ONE
        hidden.save(update_fields=["comment_visibility"])
        TripMembership.objects.create(trip=self.trip, profile=hidden, status=TripMembership.STATUS_JOINED)
        self._add_comments(2)
        TripComment.objects.create(trip=self.trip, author=hidden, text="should not appear")

        tree = build_comment_tree(self.trip, self.viewer)

        self.assertEqual(len(tree), 2)
        self.assertEqual({entry["comment"].author_id for entry in tree}, {self.author.pk})

    def test_a_reply_from_a_hidden_author_is_dropped_too(self) -> None:
        """Replies run the same gate, so they need the same memo and the same answer."""
        hidden = baker.make(User).profile
        hidden.comment_visibility = VisibilityChoice.NO_ONE
        hidden.save(update_fields=["comment_visibility"])
        TripMembership.objects.create(trip=self.trip, profile=hidden, status=TripMembership.STATUS_JOINED)
        self._add_comments(1)
        parent = TripComment.objects.get()
        TripComment.objects.create(trip=self.trip, author=hidden, text="hidden reply", parent=parent)

        tree = build_comment_tree(self.trip, self.viewer)

        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]["replies"], [])


class TheReactionPrefetchIsActuallyUsedTests(_TripThreadCase):
    """`_aggregate_reactions` re-queried every prefetch handed to it.

    It calls `.select_related("profile")` on the queryset it is given. On a
    prefetched relation that is not a refinement - it clones the queryset, which
    drops the populated result cache, so every caller that carefully prefetched
    `reactions` paid a query per comment anyway. Six call sites across the pin,
    wiki and trip comment surfaces hand it a prefetched `.reactions.all()`.

    The join was never needed: the aggregate reads `r.emoji` and `r.profile_id`,
    both of which are columns on the reaction row.
    """

    def _react_to_each(self) -> None:
        from urbanlens.dashboard.models.reactions.model import Reaction

        for comment in TripComment.objects.all():
            Reaction.objects.create(trip_comment=comment, profile=self.viewer, emoji="👍")

    def _queries_for(self, comments: int) -> int:
        self._add_comments(comments)
        self._react_to_each()
        with CaptureQueriesContext(connection) as captured:
            build_comment_tree(self.trip, self.viewer)
        return len(captured.captured_queries)

    def test_reacted_comments_do_not_cost_a_query_each(self) -> None:
        small = self._queries_for(2)
        TripComment.objects.all().delete()
        large = self._queries_for(12)

        self.assertLessEqual(
            large - small,
            2,
            f"ten more reacted comments cost {large - small} more queries",
        )

    def test_the_reaction_aggregate_is_still_right(self) -> None:
        """The anti-vacuity half: dropping the join must not drop the data."""
        self._add_comments(1)
        self._react_to_each()

        tree = build_comment_tree(self.trip, self.viewer)

        self.assertEqual(tree[0]["reactions"], {"👍": {"count": 1, "reacted_by": [self.viewer.pk]}})
