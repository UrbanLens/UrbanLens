"""The comment and photo batches must answer what the per-pair gates answer.

Both surfaces already memoise per distinct author or uploader, which stops one
person being resolved twice and does nothing about the resolution: at
``COMMON_PIN`` or ``ANYTHING_IN_COMMON`` each one reads *both* accounts' whole
``Pin`` table into Python. A thread with twenty distinct authors paid twenty
pairs of full scans, and a community wiki collects photos from everyone who has
been there.

These are privacy gates, so the batches are held to the functions they replace
rather than to written expectations - the pattern ``test_identity_visibility_batch``
established, and which caught a real bug in that fix within minutes. A batch
that says "visible" where the single form says "hidden" shows someone a comment
or a photograph they have no standing right to see.

``can_view_photos_from`` is the interesting one: it is two-sided, so the batch
intersects a "many subjects, one viewer" resolution with a "one subject, many
viewers" one, and the mixed-list cases are what would catch either half being
applied in the wrong direction.

The mirror half came from splitting ``viewers_who_can_see`` into a
parameterised body, so ``test_identity_visibility_batch`` is this change's
other regression guard and is run beside these.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.comments.comments import top_level_comment_queryset, visible_comment_tree


def _profile(**kwargs) -> Profile:
    profile = baker.make(User).profile
    if kwargs:
        Profile.objects.filter(pk=profile.pk).update(**kwargs)
        profile.refresh_from_db()
    return profile


class GateAgreementMixin:
    """Shared scenarios; each subclass names one gate and its field.

    A mixin rather than a base ``TestCase``: a base that is itself collected
    runs every scenario against no field at all, which is six failures that say
    nothing about the code.
    """

    #: Named by each subclass.
    field: str

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = _profile()

    def reference(self, other: Profile) -> bool:
        raise NotImplementedError

    def batch(self, others: list[Profile]) -> set[int]:
        raise NotImplementedError

    def _assert_agrees(self, others: list[Profile]) -> None:
        resolved = self.batch(others)
        assert_agrees(
            self.reference,
            lambda other: other.pk in resolved,
            others,
            describe=lambda other: f"{self.field}={getattr(other, self.field)!r}",
            label=type(self).__name__,
        )

    def _assert_both_outcomes(self, others: list[Profile]) -> None:
        """A scenario everyone fails would agree with a batch returning nothing."""
        self.assertEqual(
            {self.reference(other) for other in others},
            {True, False},
            "this scenario proves nothing unless it contains both outcomes",
        )

    def _befriend(self, other: Profile) -> None:
        Friendship.objects.create(
            from_profile=self.viewer,
            to_profile=other,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )

    def _others(self) -> list[Profile]:
        return [_profile(**{self.field: value}) for value in VisibilityChoice.values]

    def test_every_visibility_with_no_relationship(self) -> None:
        others = self._others()

        self._assert_both_outcomes(others)
        self._assert_agrees(others)

    def test_every_visibility_with_an_accepted_friendship(self) -> None:
        others = self._others()
        for other in others:
            self._befriend(other)

        self._assert_both_outcomes(others)
        self._assert_agrees(others)

    def test_every_visibility_with_a_pin_in_common(self) -> None:
        location = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=location)
        others = self._others()
        for other in others:
            baker.make(Pin, profile=other, location=location)

        self._assert_both_outcomes(others)
        self._assert_agrees(others)

    def test_every_visibility_with_a_trip_in_common(self) -> None:
        trip = baker.make(Trip)
        TripMembership.objects.create(trip=trip, profile=self.viewer)
        others = self._others()
        for other in others:
            TripMembership.objects.create(trip=trip, profile=other)

        self._assert_both_outcomes(others)
        self._assert_agrees(others)

    def test_the_viewer_is_included_when_they_are_in_the_list(self) -> None:
        """Both per-pair forms short-circuit on self, before any setting."""
        others = [*self._others(), self.viewer]

        self._assert_both_outcomes(others)
        self._assert_agrees(others)

    def test_a_mixed_list_does_not_smear_one_answer_across_its_neighbours(self) -> None:
        location = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=location)
        others = []
        for value in VisibilityChoice.values:
            sharing = _profile(**{self.field: value})
            baker.make(Pin, profile=sharing, location=location)
            others.append(sharing)
            others.append(_profile(**{self.field: value}))

        self._assert_both_outcomes(others)
        self._assert_agrees(others)


class CommentAuthorBatchTests(GateAgreementMixin, TestCase):
    field = "comment_visibility"

    def reference(self, other: Profile) -> bool:
        return self.viewer.can_view_comments_from(other)

    def batch(self, others: list[Profile]) -> set[int]:
        return Profile.visible_comment_author_pks(self.viewer, others)


class PhotoUploaderBatchTests(GateAgreementMixin, TestCase):
    field = "photo_upload_visibility"

    def setUp(self) -> None:
        super().setUp()
        # The inherited scenarios vary the *uploader's* half, so the viewer's
        # own filter is opened first - left at its ANYTHING_IN_COMMON default
        # it refuses every stranger and the uploader's setting stops mattering,
        # which would make those scenarios agree on False and prove nothing.
        # The viewer's half has its own cases below.
        self._set_viewer_filter(VisibilityChoice.ANYONE)

    def reference(self, other: Profile) -> bool:
        return self.viewer.can_view_photos_from(other)

    def batch(self, others: list[Profile]) -> set[int]:
        return Profile.visible_photo_uploader_pks(self.viewer, others)

    def _set_viewer_filter(self, value: str) -> None:
        Profile.objects.filter(pk=self.viewer.pk).update(viewer_photo_filter=value)
        self.viewer.refresh_from_db()

    def test_the_viewers_own_filter_is_the_other_half_of_the_gate(self) -> None:
        """Uploaders that pass their own setting must still pass the viewer's."""
        for value in VisibilityChoice.values:
            with self.subTest(viewer_photo_filter=value):
                self._set_viewer_filter(value)
                others = [_profile(photo_upload_visibility=VisibilityChoice.ANYONE) for _ in range(3)]
                self._befriend(others[0])

                self._assert_agrees(others)

    def test_the_viewers_filter_refusing_hides_an_uploader_who_permits(self) -> None:
        """The direction that a one-sided batch would get wrong."""
        self._set_viewer_filter(VisibilityChoice.NO_ONE)
        other = _profile(photo_upload_visibility=VisibilityChoice.ANYONE)

        self.assertFalse(self.viewer.can_view_photos_from(other))
        self.assertNotIn(other.pk, Profile.visible_photo_uploader_pks(self.viewer, [other]))

    def test_the_uploader_refusing_hides_them_from_a_permissive_viewer(self) -> None:
        """And the mirror of it, so neither half can be the only one applied."""
        self._set_viewer_filter(VisibilityChoice.ANYONE)
        other = _profile(photo_upload_visibility=VisibilityChoice.NO_ONE)

        self.assertFalse(self.viewer.can_view_photos_from(other))
        self.assertNotIn(other.pk, Profile.visible_photo_uploader_pks(self.viewer, [other]))

    def test_both_permitting_shows_them(self) -> None:
        """The positive half, without which the two refusals above prove nothing."""
        self._set_viewer_filter(VisibilityChoice.ANYONE)
        other = _profile(photo_upload_visibility=VisibilityChoice.ANYONE)

        self.assertTrue(self.viewer.can_view_photos_from(other))
        self.assertIn(other.pk, Profile.visible_photo_uploader_pks(self.viewer, [other]))


class CommentThreadGateScalingTests(TestCase):
    """A thread's gate cost must not track how many people wrote in it.

    The per-author memo made the cost *distinct* authors rather than comments,
    which is the axis a thread actually grows along: a busy pin or a community
    wiki collects a comment each from many different people.
    """

    #: Two runs of one thread legitimately differ by a query or so.
    TOLERANCE = 2

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = _profile()
        self.pin = baker.make(Pin, profile=self.viewer, location=baker.make(Location))
        # The setting that makes the gate expensive, and the default.
        Profile.objects.filter(pk=self.viewer.pk).update(comment_visibility=VisibilityChoice.ANYTHING_IN_COMMON)
        self.shared = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=self.shared)

    def _add_authors(self, count: int) -> None:
        for _ in range(count):
            author = _profile(comment_visibility=VisibilityChoice.ANYTHING_IN_COMMON)
            baker.make(Pin, profile=author, location=self.shared)
            baker.make(Comment, pin=self.pin, profile=author, text="a comment")

    def _measure(self) -> tuple[int, int]:
        top_level = list(top_level_comment_queryset(self.pin.comments.all().visible_to(self.viewer)))
        with CaptureQueriesContext(connection) as captured:
            visible = visible_comment_tree(top_level, self.viewer)
        return len(captured), len(visible)

    def test_query_count_does_not_grow_with_the_number_of_distinct_authors(self) -> None:
        self._add_authors(2)
        first_queries, first_visible = self._measure()

        self._add_authors(18)
        second_queries, second_visible = self._measure()

        self.assertGreater(second_visible, first_visible, "the seed does not exercise this - the thread did not grow")
        self.assertLessEqual(
            second_queries - first_queries,
            self.TOLERANCE,
            f"{first_queries} queries for {first_visible} authors, {second_queries} for {second_visible}",
        )
