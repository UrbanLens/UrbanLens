"""Tests for UL-219: replies must survive their parent comment's deletion
with their thread context preserved, not silently become unexplained
top-level comments.

Comment.parent is on_delete=SET_NULL (replies were never lost - see
PinCommentDeleteView/WikiCommentDeleteView's own comments), but a reply
whose parent is nulled queries identically to a genuine top-level comment,
so nothing distinguished the two in the UI. models/comments/signals.py's
pre_delete handler now flags every reply with parent_deleted=True before the
FK is nulled, and the comment panel renders a "Replying to a comment that
was deleted" placeholder for exactly those rows.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki


class ParentDeletedSignalTests(TestCase):
    """models.comments.signals.flag_replies_on_parent_delete - model-level behavior."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.owner)

    def test_reply_survives_parent_deletion(self) -> None:
        parent = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner, text="original")
        reply = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner, parent=parent, text="a reply")

        parent.delete()

        reply.refresh_from_db()
        self.assertEqual(reply.text, "a reply")
        self.assertIsNone(reply.parent_id)

    def test_reply_is_flagged_parent_deleted(self) -> None:
        parent = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner)
        reply = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner, parent=parent)

        parent.delete()

        reply.refresh_from_db()
        self.assertTrue(reply.parent_deleted)

    def test_flag_is_not_set_on_unrelated_comments(self) -> None:
        parent = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner)
        baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner, parent=parent)
        unrelated_top_level = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner)
        other_parent = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner)
        unrelated_reply = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner, parent=other_parent)

        parent.delete()

        unrelated_top_level.refresh_from_db()
        unrelated_reply.refresh_from_db()
        self.assertFalse(unrelated_top_level.parent_deleted)
        self.assertFalse(unrelated_reply.parent_deleted)

    def test_deleting_a_comment_with_no_replies_touches_nothing(self) -> None:
        lone = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner)
        lone.delete()  # must not raise

    def test_multiple_replies_are_all_flagged(self) -> None:
        parent = baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner)
        replies = [baker.make(Comment, pin=self.pin, wiki=None, profile=self.owner, parent=parent) for _ in range(3)]

        parent.delete()

        for reply in replies:
            reply.refresh_from_db()
            self.assertTrue(reply.parent_deleted)


class PinCommentPanelParentDeletedRenderingTests(TestCase):
    """The pin comment panel shows a placeholder for orphaned-by-deletion replies."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile)
        self.client.force_login(self.user)

    def test_orphaned_reply_shows_parent_deleted_placeholder(self) -> None:
        parent = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, text="original text")
        baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, parent=parent, text="my reply survives")

        response = self.client.delete(reverse("pin.comment.delete", kwargs={"pin_slug": self.pin.slug, "comment_id": parent.pk}))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("my reply survives", body)
        self.assertIn("Replying to a comment that was deleted", body)
        self.assertNotIn("original text", body)

    def test_genuine_top_level_comment_has_no_placeholder(self) -> None:
        baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, text="a normal top-level comment")

        response = self.client.get(reverse("pin.comments", kwargs={"pin_slug": self.pin.slug}))

        body = response.content.decode()
        self.assertIn("a normal top-level comment", body)
        self.assertNotIn("Replying to a comment that was deleted", body)

    def test_reply_under_a_live_parent_has_no_placeholder(self) -> None:
        parent = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, text="still here")
        baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, parent=parent, text="a normal reply")

        response = self.client.get(reverse("pin.comments", kwargs={"pin_slug": self.pin.slug}))

        body = response.content.decode()
        self.assertIn("a normal reply", body)
        self.assertNotIn("Replying to a comment that was deleted", body)


class WikiCommentPanelParentDeletedRenderingTests(TestCase):
    """The same placeholder rendering path, exercised via the shared wiki comment panel."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile)
        self.wiki = baker.make(Wiki, location=self.pin.location)
        self.client.force_login(self.user)

    def test_orphaned_wiki_reply_shows_parent_deleted_placeholder(self) -> None:
        parent = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.profile, text="wiki original")
        baker.make(Comment, wiki=self.wiki, pin=None, profile=self.profile, parent=parent, text="wiki reply survives")

        response = self.client.delete(reverse("location.wiki.comment.delete", kwargs={"location_slug": self.wiki.location.slug, "comment_id": parent.pk}))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("wiki reply survives", body)
        self.assertIn("Replying to a comment that was deleted", body)
