"""A reply to a reply must be refused at creation, not silently swallowed.

Every comment tree in this app (pin, wiki, trip, and the external API's own
copy) renders replies exactly one level deep: `visible_comment_tree` /
`build_comment_tree` walk a top-level comment's `.replies.all()` once and
never recurse into a reply's own replies. Before this fix, none of the four
comment-creation call sites restricted `parent_id` to a top-level comment, so
replying to an existing reply was accepted and persisted - and then rendered
nowhere, forever, while the comment-count badge (which counts the full,
depth-blind queryset) still included it. This pins that a reply-to-a-reply is
refused the same way an unknown/foreign parent id already was, on all four
paths.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key


class PinReplyToReplyRejectedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile)
        self.top = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, text="top level")
        self.reply = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, parent=self.top, text="a reply")

    def _url(self) -> str:
        return reverse("pin.comments", kwargs={"pin_slug": self.pin.slug})

    def test_replying_to_a_top_level_comment_succeeds(self) -> None:
        response = self.client.post(self._url(), {"text": "a fine reply", "parent_id": self.top.pk})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Comment.objects.filter(text="a fine reply", parent=self.top).exists())

    def test_replying_to_a_reply_is_rejected(self) -> None:
        response = self.client.post(self._url(), {"text": "a nested reply", "parent_id": self.reply.pk})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Comment.objects.filter(text="a nested reply").exists())


class WikiReplyToReplyRejectedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.profile, location=self.location)
        self.top = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.profile, text="top level")
        self.reply = baker.make(
            Comment, wiki=self.wiki, pin=None, profile=self.profile, parent=self.top, text="a reply"
        )

    def _url(self) -> str:
        return reverse("location.wiki.comments", args=[self.location.slug])

    def test_replying_to_a_top_level_comment_succeeds(self) -> None:
        response = self.client.post(self._url(), {"text": "a fine reply", "parent_id": self.top.pk})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Comment.objects.filter(text="a fine reply", parent=self.top).exists())

    def test_replying_to_a_reply_is_rejected(self) -> None:
        response = self.client.post(self._url(), {"text": "a nested reply", "parent_id": self.reply.pk})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Comment.objects.filter(text="a nested reply").exists())


class TripReplyToReplyRejectedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.trip = baker.make(Trip, creator=self.profile, name="Shared Trip")
        TripMembership.objects.create(trip=self.trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        self.top = baker.make(TripComment, trip=self.trip, author=self.profile, text="top level")
        self.reply = baker.make(TripComment, trip=self.trip, author=self.profile, parent=self.top, text="a reply")

    def _url(self) -> str:
        return reverse("trips.comments", args=[self.trip.slug])

    def test_replying_to_a_top_level_comment_succeeds(self) -> None:
        response = self.client.post(self._url(), {"text": "a fine reply", "parent_id": self.top.pk})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(TripComment.objects.filter(text="a fine reply", parent=self.top).exists())

    def test_replying_to_a_reply_is_rejected(self) -> None:
        response = self.client.post(self._url(), {"text": "a nested reply", "parent_id": self.reply.pk})

        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(TripComment.objects.filter(text="a nested reply").exists())


class ExternalApiReplyToReplyRejectedTests(TestCase):
    """The external API's own copy of comment creation (views_wiki.py::_create_comment)."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        _key, self.raw_key = generate_api_key(self.user, "Reply client")
        self.pin = baker.make(Pin, profile=self.profile)
        self.top = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, text="top level")
        self.reply = baker.make(Comment, pin=self.pin, wiki=None, profile=self.profile, parent=self.top, text="a reply")

    def _headers(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def _url(self) -> str:
        return f"/dashboard/api/external/v1/pins/{self.pin.slug}/comments/"

    def test_replying_to_a_top_level_comment_succeeds(self) -> None:
        response = self.client.post(self._url(), {"text": "a fine reply", "parent_id": self.top.pk}, **self._headers())

        self.assertEqual(response.status_code, 201)
        self.assertTrue(Comment.objects.filter(text="a fine reply", parent=self.top).exists())

    def test_replying_to_a_reply_is_rejected(self) -> None:
        response = self.client.post(
            self._url(), {"text": "a nested reply", "parent_id": self.reply.pk}, **self._headers()
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Comment.objects.filter(text="a nested reply").exists())
