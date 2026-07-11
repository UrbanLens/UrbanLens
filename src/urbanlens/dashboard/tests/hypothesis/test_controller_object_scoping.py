"""Object-level authorization tests for HTMX controller endpoints.

Covers the regressions where several endpoints resolved objects from
client-supplied identifiers without scoping them to the requesting user:

- Pin gallery endpoints (and the map upload endpoint) looked pins up by bare
  slug. Pin slugs are only unique per profile, so this both matched other
  users' pins and raised MultipleObjectsReturned (a 500) whenever two users
  shared a slug.
- The badge membership panels fetched any Badge by id, letting a forged id
  attach (and thereby expose) another user's private badge.
- Comment reactions accepted any comment id, and trip-comment reactions never
  checked trip membership.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.badges.meta import KIND_TAG
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin


def _pin_with_slug(profile, slug: str) -> Pin:
    """Create a pin with an explicit slug for collision scenarios."""
    return baker.make(Pin, profile=profile, slug=slug)


class PinGalleryScopingTests(TestCase):
    """Gallery endpoints must only resolve the requesting user's pins."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.other = baker.make(User)
        self.client.force_login(self.user)
        self.own_pin = _pin_with_slug(self.user.profile, "shared-slug")
        self.foreign_pin = _pin_with_slug(self.other.profile, "foreign-only")

    def test_own_gallery_renders(self) -> None:
        response = self.client.get(reverse("pin.gallery", args=[self.own_pin.slug]))
        self.assertEqual(response.status_code, 200)

    def test_foreign_gallery_404s(self) -> None:
        response = self.client.get(reverse("pin.gallery", args=[self.foreign_pin.slug]))
        self.assertEqual(response.status_code, 404)

    def test_cross_user_slug_collision_does_not_500(self) -> None:
        """Two users sharing a slug must resolve to the requester's pin."""
        _pin_with_slug(self.other.profile, "shared-slug")
        response = self.client.get(reverse("pin.gallery", args=["shared-slug"]))
        self.assertEqual(response.status_code, 200)

    def test_gallery_json_foreign_pin_404s(self) -> None:
        response = self.client.get(reverse("pin.gallery.json", args=[self.foreign_pin.slug]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_upload_to_foreign_pin_via_gallery(self) -> None:
        response = self.client.post(
            reverse("pin.gallery", args=[self.foreign_pin.slug]),
            data={"image": SimpleUploadedFile("x.jpg", b"fake-jpeg-bytes", content_type="image/jpeg")},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Image.objects.filter(pin=self.foreign_pin).exists())

    def test_cannot_upload_to_foreign_pin_via_map_endpoint(self) -> None:
        response = self.client.post(
            reverse("pin.upload_image", args=[self.foreign_pin.slug]),
            data={"image": SimpleUploadedFile("x.jpg", b"fake-jpeg-bytes", content_type="image/jpeg")},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Image.objects.filter(pin=self.foreign_pin).exists())

    def test_weather_forecast_foreign_pin_404s(self) -> None:
        response = self.client.get(reverse("pin.weather_forecast", args=[self.foreign_pin.slug]))
        self.assertEqual(response.status_code, 404)


class BadgeMembershipVisibilityTests(TestCase):
    """Only badges visible to the requester may be attached to pins or wikis."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.other = baker.make(User)
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.user.profile)
        self.own_badge = baker.make(Badge, kind=KIND_TAG, profile=self.user.profile)
        self.foreign_badge = baker.make(Badge, kind=KIND_TAG, profile=self.other.profile)

    def _add(self, badge_id: int):
        return self.client.post(
            reverse("badge.pin", kwargs={"badge_kind": "tag", "pin_slug": self.pin.slug}),
            data={"badge_id": badge_id, "action": "add"},
        )

    def test_own_badge_can_be_added(self) -> None:
        response = self._add(self.own_badge.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.own_badge, self.pin.badges.all())

    def test_foreign_private_badge_is_rejected(self) -> None:
        response = self._add(self.foreign_badge.id)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(self.foreign_badge, self.pin.badges.all())

    def test_global_badge_can_be_added(self) -> None:
        global_badge = baker.make(Badge, kind=KIND_TAG, profile=None)
        response = self._add(global_badge.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn(global_badge, self.pin.badges.all())


class CommentReactionScopingTests(TestCase):
    """Reactions must be limited to comments the requester can actually see."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.other = baker.make(User)
        self.client.force_login(self.user)

    def _react(self, comment_id: int):
        return self.client.post(
            reverse("comment.react", args=[comment_id]),
            data={"emoji": "👍"},
        )

    def test_can_react_to_comment_on_own_pin(self) -> None:
        pin = baker.make(Pin, profile=self.user.profile)
        comment = baker.make(Comment, pin=pin, wiki=None, profile=self.user.profile)
        self.assertEqual(self._react(comment.id).status_code, 200)

    def test_cannot_react_to_comment_on_foreign_pin(self) -> None:
        pin = baker.make(Pin, profile=self.other.profile)
        comment = baker.make(Comment, pin=pin, wiki=None, profile=self.other.profile)
        self.assertEqual(self._react(comment.id).status_code, 404)


class TripCommentReactionMembershipTests(TestCase):
    """Trip comment reactions require trip membership."""

    def setUp(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip, TripComment

        self.member = baker.make(User)
        self.outsider = baker.make(User)
        self.trip = baker.make(Trip, creator=self.member.profile)
        self.comment = baker.make(TripComment, trip=self.trip, author=self.member.profile)

    def _react(self):
        return self.client.post(
            reverse("trips.comment.react", args=[self.trip.uuid, self.comment.id]),
            data={"emoji": "👍"},
        )

    def test_creator_can_react(self) -> None:
        self.client.force_login(self.member)
        self.assertEqual(self._react().status_code, 200)

    def test_non_member_cannot_react(self) -> None:
        self.client.force_login(self.outsider)
        self.assertEqual(self._react().status_code, 403)
