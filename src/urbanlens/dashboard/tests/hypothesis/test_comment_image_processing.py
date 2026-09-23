"""Comment and trip-comment images while their upload is still being processed (P142).

A comment image is re-encoded under a new name before its ``pending_scan`` clears, and the raw file is
deleted, so the same rule as for a photo holds: nothing names the raw file, and the author's placeholder
is swapped for the image once it lands.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.serializers import TripCommentSerializer
from urbanlens.dashboard.external_api.views_wiki import _serialize_comment
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.trips.trip_comments import add_comment
from urbanlens.dashboard.services.trips.trip_errors import TripValidationError

_RAW_PHOTO = "pin_images/rawcmt142/upload-cmt142.jpg"
_RAW_COMMENT_IMAGE = "comment_images/raw-cmt142.jpg"
_ENCODED_COMMENT_IMAGE = "comment_images/encoded-cmt142.webp"


class _Author(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, name="Mill")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)

    def pending_photo(self) -> Image:
        name = default_storage.save(_RAW_PHOTO, ContentFile(b"raw upload bytes with gps"))
        self.addCleanup(default_storage.delete, name)
        return baker.make(Image, profile=self.profile, image=name, pending_scan=True)


class ChooseExistingPendingPhotoTests(_Author):
    """A still-pending photo is the raw upload - unscanned, metadata intact - and must never be copied onto a comment."""

    def test_a_wiki_comment_refuses_it(self) -> None:
        photo = self.pending_photo()

        response = self.client.post(
            reverse("location.wiki.comments", args=[self.location.slug]),
            {"text": "look", "existing_image_id": str(photo.pk)},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Comment.objects.exclude(image="").exclude(image__isnull=True).exists())

    def test_a_pin_note_refuses_it(self) -> None:
        photo = self.pending_photo()

        response = self.client.post(
            reverse("pin.comments", args=[self.pin.slug]),
            {"text": "look", "existing_image_id": str(photo.pk)},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Comment.objects.exclude(image="").exclude(image__isnull=True).exists())

    def test_a_trip_comment_refuses_it(self) -> None:
        trip = baker.make(Trip, creator=self.profile)
        TripMembership.objects.create(trip=trip, profile=self.profile)
        photo = self.pending_photo()

        with self.assertRaises(TripValidationError):
            add_comment(trip, self.profile, text="look", existing_image_id=str(photo.pk))

        self.assertFalse(TripComment.objects.exclude(image="").exclude(image__isnull=True).exists())


class CommentImagePickerTests(_Author):
    def test_a_pending_photo_is_a_placeholder_that_cannot_be_chosen(self) -> None:
        pending = self.pending_photo()

        html = self.client.get(reverse("comments.image_picker")).content.decode()

        self.assertIn(f'data-id="{pending.pk}"', html)
        self.assertIn('data-processing="pending"', html)
        self.assertIn("media-processing", html)
        self.assertIn(f'data-processing-url="{reverse("vault.photos.processing")}"', html)
        self.assertNotIn('src=""', html)
        self.assertNotIn("upload-cmt142", html)


class CommentImageProcessingPollTests(_Author):
    def _poll(self, name: str, pk: int, user: User | None = None) -> dict:
        self.client.force_login(user or self.user)
        response = self.client.get(reverse(name), {"ids": str(pk)})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_author_is_told_it_is_still_processing(self) -> None:
        comment = baker.make(Comment, wiki=self.wiki, profile=self.profile, image=_RAW_COMMENT_IMAGE, pending_scan=True)

        self.assertEqual(
            self._poll("comments.images.processing", comment.pk), {"items": [], "processing": [comment.pk]}
        )

    def test_the_author_gets_the_encoded_file_once_ready(self) -> None:
        comment = baker.make(Comment, wiki=self.wiki, profile=self.profile, image=_ENCODED_COMMENT_IMAGE)

        body = self._poll("comments.images.processing", comment.pk)

        self.assertEqual(body["processing"], [])
        self.assertEqual(body["items"][0]["id"], comment.pk)
        self.assertEqual(body["items"][0]["url"], comment.image.url)

    def test_another_account_learns_nothing(self) -> None:
        comment = baker.make(Comment, wiki=self.wiki, profile=self.profile, image=_ENCODED_COMMENT_IMAGE)

        self.assertEqual(
            self._poll("comments.images.processing", comment.pk, baker.make(User)), {"items": [], "processing": []}
        )

    def test_a_trip_comment_is_answered_by_its_own_endpoint(self) -> None:
        trip = baker.make(Trip, creator=self.profile)
        comment = baker.make(TripComment, trip=trip, author=self.profile, image=_RAW_COMMENT_IMAGE, pending_scan=True)

        self.assertEqual(self._poll("comments.trip_images.processing", comment.pk)["processing"], [comment.pk])
        self.assertEqual(
            self._poll("comments.trip_images.processing", comment.pk, baker.make(User)), {"items": [], "processing": []}
        )


class CommentPanelTests(_Author):
    def test_the_author_sees_a_polled_placeholder(self) -> None:
        comment = baker.make(Comment, wiki=self.wiki, profile=self.profile, image=_RAW_COMMENT_IMAGE, pending_scan=True)

        html = self.client.get(reverse("location.wiki.comments", args=[self.location.slug])).content.decode()

        self.assertIn(f'data-id="{comment.pk}"', html)
        self.assertIn('data-processing="pending"', html)
        self.assertIn(f'data-processing-url="{reverse("comments.images.processing")}"', html)
        self.assertIn("media-processing", html)
        self.assertNotIn("raw-cmt142", html)


class ApiCommentImageTests(_Author):
    def test_a_wiki_comment_names_no_file_while_pending(self) -> None:
        comment = baker.make(Comment, wiki=self.wiki, profile=self.profile, image=_RAW_COMMENT_IMAGE, pending_scan=True)

        row = _serialize_comment(SimpleNamespace(comment=comment, replies=[], parent_was_deleted=False), self.profile)

        self.assertIsNone(row["image_url"])
        self.assertIs(row["image_processing"], True)

    def test_a_wiki_comment_names_its_file_once_ready(self) -> None:
        comment = baker.make(Comment, wiki=self.wiki, profile=self.profile, image=_ENCODED_COMMENT_IMAGE)

        row = _serialize_comment(SimpleNamespace(comment=comment, replies=[], parent_was_deleted=False), self.profile)

        self.assertEqual(row["image_url"], comment.image.url)
        self.assertIs(row["image_processing"], False)

    def test_a_trip_comment_names_no_file_while_pending(self) -> None:
        trip = baker.make(Trip, creator=self.profile)
        comment = baker.make(TripComment, trip=trip, author=self.profile, image=_RAW_COMMENT_IMAGE, pending_scan=True)
        serializer = TripCommentSerializer(context={"viewer": self.profile})

        self.assertIsNone(serializer.get_image_url({"comment": comment}))
        self.assertIs(serializer.get_image_processing({"comment": comment}), True)
