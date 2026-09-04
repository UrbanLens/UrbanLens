"""Resolving one viewer's photo visibility repeatedly, without changing what it means.

``visible_to`` is eager: it resolves the viewer's friends, pinned locations,
trip memberships and reachable wikis before it can build its filter. Those four
describe the *viewer*, not the queryset, so a page calling it several times for
one viewer pays for all four each time - album detail does it four times, which
measured at 60 queries for a 30-photo vault album.

The saving is opt-in, and that is the point of this file. An earlier version
cached on first read, which silently changed the answer for any caller that
wrote something and then asked: ``test_gaining_a_pin_at_the_far_place_grants_the_photo``
creates a pin and immediately re-checks, and got the answer from before the pin.
So a caller has to say it is about to ask repeatedly, and the default stays a
fresh read.

Both halves are pinned here, because getting either wrong is silent: an
unprimed read that goes stale is a photo shown to somebody who should not see
it or hidden from somebody who should, and a primed read that does not save
anything is just slower.
"""

from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.queryset import prime_viewer_scope
from urbanlens.dashboard.models.profile.model import Profile

#: Tables only the viewer-scoped lookups touch.
_VIEWER_TABLES = ("dashboard_friendships", "dashboard_pins", "dashboard_trip_memberships")


def _viewer_lookup_queries(captured: CaptureQueriesContext) -> list[str]:
    return [
        query["sql"] for query in captured.captured_queries if any(table in query["sql"] for table in _VIEWER_TABLES)
    ]


class ViewerScopePrimingTests(TestCase):
    """What priming saves, and what it must not change."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer = Profile.objects.get(user=baker.make("auth.User"))
        self.uploader = Profile.objects.get(user=baker.make("auth.User"))
        Friendship.objects.create(from_profile=self.viewer, to_profile=self.uploader, status=FriendshipStatus.ACCEPTED)
        baker.make(Image, profile=self.uploader, pending_scan=False)
        baker.make(Image, profile=self.viewer, pending_scan=False)

    def _resolve(self, times: int, *, primed: bool) -> list[str]:
        """Measure the viewer-table queries of *times* calls on a fresh profile.

        Reloading matters: a primed value hangs on the instance, so reusing one
        across measurements would leave the second reading what the first
        primed, and the comparison would be between different things.

        Args:
            times: How many ``visible_to`` calls to make.
            primed: Whether to prime the viewer first.

        Returns:
            The SQL of every query that touched a viewer-scoped table.
        """
        viewer = Profile.objects.get(pk=self.viewer.pk)
        if primed:
            prime_viewer_scope(viewer)
        with CaptureQueriesContext(connection) as captured:
            for _ in range(times):
                list(Image.objects.all().visible_to(viewer).values_list("pk", flat=True))
        return _viewer_lookup_queries(captured)

    def test_an_unprimed_resolution_reads_the_viewer_tables(self) -> None:
        # The baseline the assertions below compare against; without it "no
        # extra queries" could mean "no queries".
        self.assertGreater(len(self._resolve(1, primed=False)), 0)

    def test_priming_collapses_repeat_reads(self) -> None:
        unprimed = len(self._resolve(4, primed=False))
        primed = len(self._resolve(4, primed=True))

        self.assertLess(primed, unprimed, "priming saved nothing; album detail resolves this four times")

    def test_an_unprimed_caller_still_reads_fresh_every_time(self) -> None:
        """The default must not have changed for anybody who did not opt in."""
        once = len(self._resolve(1, primed=False))
        four_times = len(self._resolve(4, primed=False))

        self.assertEqual(four_times, once * 4)

    def test_an_unprimed_read_sees_a_friendship_made_a_moment_ago(self) -> None:
        """The bug the first version of this shipped.

        A caller that writes and then asks must see its own write. Caching on
        first read made ``visible_to`` answer from before it.
        """
        queryset = Image.objects.all()
        stranger = Profile.objects.get(user=baker.make("auth.User"))
        queryset._get_friend_ids(self.viewer)  # a read that must not cache

        Friendship.objects.create(from_profile=self.viewer, to_profile=stranger, status=FriendshipStatus.ACCEPTED)

        self.assertIn(stranger.pk, queryset._get_friend_ids(self.viewer))

    def test_a_primed_read_is_deliberately_a_snapshot(self) -> None:
        """The trade priming makes, stated so it cannot be a surprise.

        Having opted in, the caller gets the set as it was when primed. That is
        why the opt-in exists, and why it is not the default.
        """
        queryset = Image.objects.all()
        stranger = Profile.objects.get(user=baker.make("auth.User"))
        prime_viewer_scope(self.viewer)

        Friendship.objects.create(from_profile=self.viewer, to_profile=stranger, status=FriendshipStatus.ACCEPTED)

        self.assertNotIn(stranger.pk, queryset._get_friend_ids(self.viewer))
        self.assertIn(stranger.pk, queryset._get_friend_ids(Profile.objects.get(pk=self.viewer.pk)))

    def test_priming_does_not_change_the_answer(self) -> None:
        unprimed = set(
            Image.objects.all().visible_to(Profile.objects.get(pk=self.viewer.pk)).values_list("pk", flat=True)
        )

        primed_viewer = Profile.objects.get(pk=self.viewer.pk)
        prime_viewer_scope(primed_viewer)
        primed = set(Image.objects.all().visible_to(primed_viewer).values_list("pk", flat=True))

        self.assertEqual(unprimed, primed)
        self.assertIn(Image.objects.get(profile=self.viewer).pk, primed, "a viewer always sees their own uploads")
