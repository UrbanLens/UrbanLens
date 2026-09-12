"""`TripCommentDeleteView` and `trip_comments.delete_comment` had zero coverage."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership


class TripCommentDeleteTests(TestCase):
    """Author or trip creator, and nobody else."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.creator_user = baker.make(User)
        self.creator = self.creator_user.profile
        self.author_user = baker.make(User)
        self.author = self.author_user.profile
        self.bystander_user = baker.make(User)
        self.bystander = self.bystander_user.profile

        self.trip = baker.make(Trip, creator=self.creator, name="Shared Trip")
        for profile in (self.creator, self.author, self.bystander):
            TripMembership.objects.create(trip=self.trip, profile=profile, status=TripMembership.STATUS_JOINED)

        self.comment = baker.make(TripComment, trip=self.trip, author=self.author, text="Worth a look")
        self.url = reverse("trips.comment.delete", args=[self.trip.slug, self.comment.pk])

    def _delete_as(self, user: User):
        self.client.force_login(user)
        return self.client.delete(self.url)

    def test_the_author_can_delete_their_own_comment(self) -> None:
        response = self._delete_as(self.author_user)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripComment.objects.filter(pk=self.comment.pk).exists())

    def test_the_trip_creator_can_delete_somebody_elses_comment(self) -> None:
        """The creator override the gate exists for - moderating their own trip."""
        response = self._delete_as(self.creator_user)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripComment.objects.filter(pk=self.comment.pk).exists())

    def test_another_member_cannot_delete_it(self) -> None:
        """Joined membership is not permission; this is the assertion the gate is for."""
        response = self._delete_as(self.bystander_user)

        self.assertNotEqual(response.status_code, 200)
        self.assertTrue(
            TripComment.objects.filter(pk=self.comment.pk).exists(),
            "a member who is neither author nor creator must not delete a comment",
        )

    def test_a_non_member_cannot_delete_it(self) -> None:
        """`trip_or_not_found` should stop this before the comment gate is reached."""
        outsider = baker.make(User)

        response = self._delete_as(outsider)

        self.assertNotEqual(response.status_code, 200)
        self.assertTrue(TripComment.objects.filter(pk=self.comment.pk).exists())

    def test_an_orphaned_comment_is_deletable_by_the_creator_only(self) -> None:
        """`author` is SET_NULL, so the gate's set becomes `{None, creator}`.

        A bug that let a null author match the viewer would hand every comment
        whose writer deleted their account to any member at all.
        """
        TripComment.objects.filter(pk=self.comment.pk).update(author=None)

        self.assertNotEqual(self._delete_as(self.bystander_user).status_code, 200)
        self.assertTrue(TripComment.objects.filter(pk=self.comment.pk).exists())

        self.assertEqual(self._delete_as(self.creator_user).status_code, 200)
        self.assertFalse(TripComment.objects.filter(pk=self.comment.pk).exists())

    def test_deleting_a_comment_removes_the_markup_map_it_carried(self) -> None:
        """`delete_comment` removes the attached map, not just the comment row."""
        from urbanlens.dashboard.models.markup.model import MarkupMap

        markup_map = baker.make(MarkupMap, profile=self.author)
        TripComment.objects.filter(pk=self.comment.pk).update(markup_map=markup_map)

        response = self._delete_as(self.author_user)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(MarkupMap.objects.filter(pk=markup_map.pk).exists(), "the attached map must go too")

    def test_a_comment_on_another_trip_is_not_reachable_through_this_one(self) -> None:
        """`get_comment(trip, comment_id)` must scope by trip, not just by id."""
        other_trip = baker.make(Trip, creator=self.creator, name="Other Trip")
        TripMembership.objects.create(trip=other_trip, profile=self.creator, status=TripMembership.STATUS_JOINED)
        elsewhere = baker.make(TripComment, trip=other_trip, author=self.creator, text="Different trip")

        self.client.force_login(self.creator_user)
        response = self.client.delete(reverse("trips.comment.delete", args=[self.trip.slug, elsewhere.pk]))

        self.assertNotEqual(response.status_code, 200)
        self.assertTrue(TripComment.objects.filter(pk=elsewhere.pk).exists())
