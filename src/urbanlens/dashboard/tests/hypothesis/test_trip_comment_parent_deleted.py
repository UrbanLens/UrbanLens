"""UL-219, ported to trips: a reply must survive its parent comment's
deletion with its thread context preserved, not silently become an
unexplained top-level comment.

TripComment.parent is on_delete=SET_NULL, identical in shape to
dashboard.Comment.parent - but until this fix, nothing flagged a trip
reply when its parent was deleted, so it re-rendered as an ordinary
top-level comment, textually and structurally indistinguishable from one
that was always top-level. Mirrors test_comment_parent_deleted.py.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership


class TripCommentParentDeletedSignalTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = baker.make(User).profile
        self.trip = baker.make(Trip, creator=self.owner, name="Shared Trip")

    def test_reply_survives_parent_deletion(self) -> None:
        parent = baker.make(TripComment, trip=self.trip, author=self.owner, text="original")
        reply = baker.make(TripComment, trip=self.trip, author=self.owner, parent=parent, text="a reply")

        parent.delete()

        reply.refresh_from_db()
        self.assertEqual(reply.text, "a reply")
        self.assertIsNone(reply.parent_id)

    def test_reply_is_flagged_parent_deleted(self) -> None:
        parent = baker.make(TripComment, trip=self.trip, author=self.owner)
        reply = baker.make(TripComment, trip=self.trip, author=self.owner, parent=parent)

        parent.delete()

        reply.refresh_from_db()
        self.assertTrue(reply.parent_deleted)

    def test_flag_is_not_set_on_unrelated_comments(self) -> None:
        parent = baker.make(TripComment, trip=self.trip, author=self.owner)
        baker.make(TripComment, trip=self.trip, author=self.owner, parent=parent)
        unrelated_top_level = baker.make(TripComment, trip=self.trip, author=self.owner)
        other_parent = baker.make(TripComment, trip=self.trip, author=self.owner)
        unrelated_reply = baker.make(TripComment, trip=self.trip, author=self.owner, parent=other_parent)

        parent.delete()

        unrelated_top_level.refresh_from_db()
        unrelated_reply.refresh_from_db()
        self.assertFalse(unrelated_top_level.parent_deleted)
        self.assertFalse(unrelated_reply.parent_deleted)

    def test_deleting_a_comment_with_no_replies_touches_nothing(self) -> None:
        lone = baker.make(TripComment, trip=self.trip, author=self.owner)
        lone.delete()  # must not raise

    def test_multiple_replies_are_all_flagged(self) -> None:
        parent = baker.make(TripComment, trip=self.trip, author=self.owner)
        replies = [baker.make(TripComment, trip=self.trip, author=self.owner, parent=parent) for _ in range(3)]

        parent.delete()

        for reply in replies:
            reply.refresh_from_db()
            self.assertTrue(reply.parent_deleted)


class TripCommentPanelParentDeletedRenderingTests(TestCase):
    """The trip comment panel shows a placeholder for orphaned-by-deletion replies."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.trip = baker.make(Trip, creator=self.profile, name="Shared Trip")
        TripMembership.objects.create(trip=self.trip, profile=self.profile, status=TripMembership.STATUS_JOINED)

    def test_orphaned_reply_shows_parent_deleted_placeholder(self) -> None:
        parent = baker.make(TripComment, trip=self.trip, author=self.profile, text="original text")
        baker.make(TripComment, trip=self.trip, author=self.profile, parent=parent, text="my reply survives")

        response = self.client.delete(reverse("trips.comment.delete", args=[self.trip.slug, parent.pk]))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("my reply survives", body)
        self.assertIn("Replying to a comment that was deleted", body)
        self.assertNotIn("original text", body)

    def test_genuine_top_level_comment_has_no_placeholder(self) -> None:
        baker.make(TripComment, trip=self.trip, author=self.profile, text="a normal top-level comment")

        response = self.client.get(reverse("trips.comments", args=[self.trip.slug]))

        body = response.content.decode()
        self.assertIn("a normal top-level comment", body)
        self.assertNotIn("Replying to a comment that was deleted", body)

    def test_reply_under_a_live_parent_has_no_placeholder(self) -> None:
        parent = baker.make(TripComment, trip=self.trip, author=self.profile, text="still here")
        baker.make(TripComment, trip=self.trip, author=self.profile, parent=parent, text="a normal reply")

        response = self.client.get(reverse("trips.comments", args=[self.trip.slug]))

        body = response.content.decode()
        self.assertIn("a normal reply", body)
        self.assertNotIn("Replying to a comment that was deleted", body)
