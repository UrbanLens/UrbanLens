"""The viewer-scoped sets behind ``ImageQuerySet.visible_to`` are resolved once.

``visible_to`` is eager: it resolves the viewer's friends, pinned locations,
trip memberships and reachable wikis before it can build its filter. Those four
describe the *viewer*, not the queryset, so every call in one request wants the
same answer - and a page can call it several times. Album detail resolves the
same visibility four separate times (``visible_album_item_pairs``,
``album_images_page``, ``eligible_images_for``, and the picker payload), which
was four copies of all four lookups on a set that cannot change mid-request.

Two opposing requirements, so both are pinned: the memo has to collapse repeat
calls on one profile instance, *and* it must not leak across instances - a
long-lived Celery worker holding a stale friend set would silently widen or
narrow who can see a photo, which is the one thing this code decides.
"""

from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile

#: Tables only the viewer-scoped lookups touch.
_VIEWER_TABLES = ("dashboard_friendships", "dashboard_pins", "dashboard_trip_memberships")


def _viewer_lookup_queries(captured: CaptureQueriesContext) -> list[str]:
    return [query["sql"] for query in captured.captured_queries if any(table in query["sql"] for table in _VIEWER_TABLES)]


class VisibleToMemoTests(TestCase):
    """One resolution per profile instance, and none carried between them."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer = Profile.objects.get(user=baker.make("auth.User"))
        self.uploader = Profile.objects.get(user=baker.make("auth.User"))
        Friendship.objects.create(from_profile=self.viewer, to_profile=self.uploader, status=FriendshipStatus.ACCEPTED)
        baker.make(Image, profile=self.uploader, pending_scan=False)
        baker.make(Image, profile=self.viewer, pending_scan=False)

    def _resolve(self, times: int) -> list[str]:
        """Measure the viewer-table queries of *times* calls on a **fresh** profile.

        Reloading matters: the memo hangs on the instance, so reusing one across
        two measurements would leave the second reading a cache the first warmed,
        and the comparison would be between different things.

        Args:
            times: How many ``visible_to`` calls to make.

        Returns:
            The SQL of every query that touched a viewer-scoped table.
        """
        viewer = Profile.objects.get(pk=self.viewer.pk)
        with CaptureQueriesContext(connection) as captured:
            for _ in range(times):
                list(Image.objects.all().visible_to(viewer).values_list("pk", flat=True))
        return _viewer_lookup_queries(captured)

    def test_a_single_resolution_reads_the_viewer_tables(self) -> None:
        # Establishes the baseline the assertions below are a comparison
        # against; without it "no extra queries" could mean "no queries".
        self.assertGreater(len(self._resolve(1)), 0)

    def test_repeat_calls_on_one_instance_do_not_reread_them(self) -> None:
        once = len(self._resolve(1))
        four_times = len(self._resolve(4))

        self.assertEqual(four_times, once, "Each visible_to call re-derived the viewer's friend/pin/trip sets; the album detail view makes four.")

    def test_a_freshly_loaded_profile_resolves_again(self) -> None:
        """The memo must not outlive the instance it is attached to."""
        self._resolve(1)
        reloaded = Profile.objects.get(pk=self.viewer.pk)

        with CaptureQueriesContext(connection) as captured:
            list(Image.objects.all().visible_to(reloaded).values_list("pk", flat=True))

        self.assertGreater(len(_viewer_lookup_queries(captured)), 0)

    def test_the_memo_does_not_change_the_answer(self) -> None:
        first = set(Image.objects.all().visible_to(self.viewer).values_list("pk", flat=True))
        second = set(Image.objects.all().visible_to(self.viewer).values_list("pk", flat=True))

        self.assertEqual(first, second)
        self.assertIn(Image.objects.get(profile=self.viewer).pk, first, "A viewer always sees their own uploads.")

    def test_a_new_friendship_is_seen_by_a_reloaded_profile(self) -> None:
        """The staleness this trades away, and the bound on it.

        Within one request the set is fixed, which is the point. The next
        request loads a new ``Profile``, so a friendship made in between is
        picked up without anything having to invalidate a cache.
        """
        stranger = Profile.objects.get(user=baker.make("auth.User"))
        queryset = Image.objects.all()
        # Warm this instance specifically. `_resolve` deliberately reloads, so it
        # would leave self.viewer cold and this test would prove nothing.
        queryset._get_friend_ids(self.viewer)
        Friendship.objects.create(from_profile=self.viewer, to_profile=stranger, status=FriendshipStatus.ACCEPTED)

        self.assertNotIn(stranger.pk, queryset._get_friend_ids(self.viewer))
        self.assertIn(stranger.pk, queryset._get_friend_ids(Profile.objects.get(pk=self.viewer.pk)))
