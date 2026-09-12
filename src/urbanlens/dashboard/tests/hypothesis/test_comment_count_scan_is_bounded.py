"""The comment-count badge must agree with the thread without reading it.

The badge cannot report a raw ``.count()``: a total that disagrees with the
rendered thread announces that something was hidden and roughly where, which is
the existence oracle ``services.comments.comments`` is built to deny.

Agreeing used to mean applying the ``@loc`` gate in Python, which meant pulling
the full text of every comment carrying a mention on every pin and wiki page
load (N21 H18). Anyone who could comment set what every later viewer of that
page paid, so the badge was given a ceiling and rendered a floor with a ``+``
above it - safe in the direction that matters, but an approximation.

``CommentLocationMention`` makes the same gate an ``EXISTS``, so the count is
both exact and bounded and the ceiling is gone. These tests hold both halves:
the cost does not grow with the thread, and the number is still right.
"""

from __future__ import annotations

from unittest import mock
import uuid as uuid_module

from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.endpoint_scaling import _row_counting_wrapper
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.comments.comments import visible_comment_count
from urbanlens.dashboard.services.notifications import mentions


class _ThreadCase(TestCase):
    """A pin whose comments the viewer can read, and a place they have pinned."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.viewer = Profile.objects.get(user=baker.make("auth.User"))
        self.author = Profile.objects.get(user=baker.make("auth.User"))
        # comment_visibility defaults to ANYTHING_IN_COMMON, which correctly
        # hides a stranger's comments (gate 1). Most tests here are about the
        # other two gates, so the author opts into ANYONE to reach them.
        Profile.objects.filter(pk=self.author.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.author.refresh_from_db()
        self.pinned_location = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=self.pinned_location)
        self.pin = baker.make(Pin, profile=self.viewer)

    def _comment(self, text: str) -> Comment:
        return baker.make(Comment, pin=self.pin, profile=self.author, text=text)

    def _mentioning(self, count: int, *, location_uuid: uuid_module.UUID | None = None) -> None:
        target = location_uuid or self.pinned_location.uuid
        for index in range(count):
            self._comment(f"visiting @[the mill {index}](loc:{target})")

    def _count(self):
        return visible_comment_count(self.pin.comments.all(), self.viewer)


class TheScanIsGoneTests(_ThreadCase):
    """What one commenter can make every later viewer pay: nothing."""

    def test_the_badge_evaluates_no_comment_text_at_all(self) -> None:
        """The gate is a join now, so the Python predicate is never reached."""
        self._mentioning(30)

        with mock.patch.object(mentions, "is_visible_to", wraps=mentions.is_visible_to) as gate:
            self._count()

        self.assertEqual(gate.call_count, 0, f"the badge still evaluated {gate.call_count} comments in Python")

    def test_the_rows_read_do_not_grow_with_the_thread(self) -> None:
        self._mentioning(5)
        rows = [0]
        with connection.execute_wrapper(_row_counting_wrapper(rows)):
            self._count()
        baseline = rows[0]

        self._mentioning(80)
        rows = [0]
        with connection.execute_wrapper(_row_counting_wrapper(rows)):
            self._count()

        self.assertLessEqual(rows[0], baseline, f"80 more comments took the badge from {baseline} rows to {rows[0]}")


class TheCountIsStillRightTests(_ThreadCase):
    """The half that stops the tests above passing against a badge that stopped counting."""

    def test_a_long_thread_counts_exactly_rather_than_reporting_a_floor(self) -> None:
        """The ceiling is gone, so no length turns the badge into an approximation."""
        self._mentioning(40)

        self.assertEqual(int(self._count()), 40)
        self.assertEqual(str(self._count()), "40", "the badge is still rendering a floor")

    def test_an_ordinary_thread_counts_exactly(self) -> None:
        self._comment("no mention here")
        self._comment("nor here")
        self._mentioning(3)

        self.assertEqual(int(self._count()), 5)

    def test_a_mention_of_a_place_the_viewer_has_not_pinned_is_still_dropped(self) -> None:
        self._comment("plain")
        self._mentioning(2, location_uuid=baker.make(Location).uuid)

        self.assertEqual(int(self._count()), 1, "the @loc gate stopped hiding comments the viewer cannot see")

    def test_a_comment_awaiting_its_malware_scan_is_not_counted_for_anyone_else(self) -> None:
        """Gate 2, which the badge has always applied and must keep applying."""
        self._comment("visible")
        baker.make(Comment, pin=self.pin, profile=self.author, text="scanning", pending_scan=True)

        self.assertEqual(int(self._count()), 1)

    def test_a_comment_from_an_author_the_viewer_cannot_read_is_not_counted(self) -> None:
        """Gate 1, which the badge skipped while it was expensive to evaluate.

        A badge that counted those comments disagreed with the thread it sat
        beside, which is the disagreement the badge exists to avoid.
        """
        self._comment("visible")
        stranger = Profile.objects.get(user=baker.make("auth.User"))
        Profile.objects.filter(pk=stranger.pk).update(comment_visibility=VisibilityChoice.NO_ONE)
        baker.make(Comment, pin=self.pin, profile=stranger, text="not for you")

        self.assertEqual(int(self._count()), 1)

    def test_the_badge_is_falsy_when_nothing_is_visible(self) -> None:
        """Every template guards the badge with `{% if %}` before rendering it."""
        self._mentioning(2, location_uuid=baker.make(Location).uuid)

        self.assertFalse(self._count())
