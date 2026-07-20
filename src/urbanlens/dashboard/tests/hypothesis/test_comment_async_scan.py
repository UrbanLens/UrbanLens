"""Tests for the async comment-image malware scan (docs/prompts/completed.md).

A newly-uploaded comment/reply/trip-comment photo no longer blocks the POST
on a clamd round-trip: the comment saves immediately with `pending_scan=True`
(visible only to its own author) and a background task clears that flag once
the scan confirms the image is clean, or removes the comment and notifies its
author (with their original text) if the image is rejected or the scanner
stays unavailable through every retry.
"""

from __future__ import annotations

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripComment
from urbanlens.dashboard.services.malware_scan import MalwareScanUnavailableError
from urbanlens.dashboard.tasks import scan_comment_image, scan_trip_comment_image


def _fake_image(name: str = "photo.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/png")


class StartCommentImageScanTests(TestCase):
    """controllers.comments.start_comment_image_scan - the enqueue side."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile)

    def test_sets_pending_scan_and_enqueues_the_pin_wiki_task(self) -> None:
        from urbanlens.dashboard.controllers.comments import start_comment_image_scan

        comment = Comment.objects.create(pin=self.pin, profile=self.profile, text="hi", image=_fake_image())
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            start_comment_image_scan(comment)
        comment.refresh_from_db()
        self.assertTrue(comment.pending_scan)
        enqueue.assert_called_once_with(scan_comment_image, comment.pk)

    def test_enqueues_the_trip_task_for_a_trip_comment(self) -> None:
        from urbanlens.dashboard.controllers.comments import start_comment_image_scan

        trip = baker.make(Trip, creator=self.profile)
        comment = TripComment.objects.create(trip=trip, author=self.profile, text="hi", image=_fake_image())
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            start_comment_image_scan(comment)
        comment.refresh_from_db()
        self.assertTrue(comment.pending_scan)
        enqueue.assert_called_once_with(scan_trip_comment_image, comment.pk)


class ScanCommentImageTaskTests(TestCase):
    """tasks.scan_comment_image - pin/wiki comments."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile)

    def _pending_comment(self, text: str = "check this out") -> Comment:
        return Comment.objects.create(pin=self.pin, profile=self.profile, text=text, image=_fake_image(), pending_scan=True)

    def test_clean_result_clears_pending_scan(self) -> None:
        comment = self._pending_comment()
        with patch("urbanlens.dashboard.tasks.malware_error_for_upload", return_value=None):
            result = scan_comment_image(comment.pk)
        comment.refresh_from_db()
        self.assertTrue(result)
        self.assertFalse(comment.pending_scan)

    def test_infected_result_deletes_the_comment_and_notifies_the_author(self) -> None:
        comment = self._pending_comment(text="my cool photo")
        comment_id = comment.pk
        with patch("urbanlens.dashboard.tasks.malware_error_for_upload", return_value="This file was flagged as malicious."):
            result = scan_comment_image(comment_id)
        self.assertFalse(result)
        self.assertFalse(Comment.objects.filter(pk=comment_id).exists())
        notification = NotificationLog.objects.get(profile=self.profile, notification_type=NotificationType.COMMENT_UPLOAD_FAILED)
        self.assertIn("my cool photo", notification.message)
        self.assertIn("flagged as malicious", notification.message)

    def test_unavailable_scanner_retries_are_exhausted_before_rejecting(self) -> None:
        comment = self._pending_comment(text="retry me")
        comment_id = comment.pk
        with (
            patch("urbanlens.dashboard.tasks.malware_error_for_upload", side_effect=MalwareScanUnavailableError("down")),
            patch.object(scan_comment_image, "max_retries", 0),
        ):
            result = scan_comment_image(comment_id)
        self.assertFalse(result)
        self.assertFalse(Comment.objects.filter(pk=comment_id).exists())
        notification = NotificationLog.objects.get(profile=self.profile, notification_type=NotificationType.COMMENT_UPLOAD_FAILED)
        self.assertIn("retry me", notification.message)
        self.assertIn("unavailable", notification.message)

    def test_missing_comment_is_a_no_op(self) -> None:
        self.assertFalse(scan_comment_image(999_999))

    def test_comment_no_longer_pending_is_a_no_op(self) -> None:
        comment = Comment.objects.create(pin=self.pin, profile=self.profile, text="already scanned", image=_fake_image(), pending_scan=False)
        with patch("urbanlens.dashboard.tasks.malware_error_for_upload") as scan:
            self.assertFalse(scan_comment_image(comment.pk))
        scan.assert_not_called()


class ScanTripCommentImageTaskTests(TestCase):
    """tasks.scan_trip_comment_image - mirrors ScanCommentImageTaskTests for trip comments."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.trip = baker.make(Trip, creator=self.profile)

    def _pending_comment(self, text: str = "trip photo") -> TripComment:
        return TripComment.objects.create(trip=self.trip, author=self.profile, text=text, image=_fake_image(), pending_scan=True)

    def test_clean_result_clears_pending_scan(self) -> None:
        comment = self._pending_comment()
        with patch("urbanlens.dashboard.tasks.malware_error_for_upload", return_value=None):
            result = scan_trip_comment_image(comment.pk)
        comment.refresh_from_db()
        self.assertTrue(result)
        self.assertFalse(comment.pending_scan)

    def test_infected_result_deletes_the_comment_and_notifies_the_author(self) -> None:
        comment = self._pending_comment(text="trip photo text")
        comment_id = comment.pk
        with patch("urbanlens.dashboard.tasks.malware_error_for_upload", return_value="This file was flagged as malicious."):
            result = scan_trip_comment_image(comment_id)
        self.assertFalse(result)
        self.assertFalse(TripComment.objects.filter(pk=comment_id).exists())
        notification = NotificationLog.objects.get(profile=self.profile, notification_type=NotificationType.COMMENT_UPLOAD_FAILED)
        self.assertIn("trip photo text", notification.message)


class CommentVisibilityWhilePendingScanTests(TestCase):
    """controllers.comments._build_context - a pending-scan comment is visible only to its author.

    Pin comments are always self-authored (the pin owner is the only
    possible viewer), so this only matters in practice for wiki (and trip)
    comments - covered here via the wiki panel, which any pin-having viewer
    of the location can see.
    """

    def setUp(self) -> None:
        self.author = baker.make("auth.User").profile
        self.viewer = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        # Both profiles have a pin at the same location - satisfies the
        # default ANYTHING_IN_COMMON comment_visibility gate (unrelated to
        # pending_scan) so that check doesn't confound these assertions.
        baker.make(Pin, profile=self.viewer, location=self.location)
        baker.make(Pin, profile=self.author, location=self.location)
        self.comment = Comment.objects.create(wiki=self.wiki, profile=self.author, text="pending photo comment", image=_fake_image(), pending_scan=True)
        self.request = RequestFactory().get("/")

    def test_other_viewer_does_not_see_the_pending_comment(self) -> None:
        from urbanlens.dashboard.controllers.comments import _build_context

        ctx = _build_context(self.wiki.comments.all(), self.viewer, self.request, wiki=self.wiki, context_type="wiki")
        rendered_ids = [item["comment"].pk for item in ctx["rendered_comments"]]
        self.assertNotIn(self.comment.pk, rendered_ids)

    def test_author_still_sees_their_own_pending_comment(self) -> None:
        from urbanlens.dashboard.controllers.comments import _build_context

        ctx = _build_context(self.wiki.comments.all(), self.author, self.request, wiki=self.wiki, context_type="wiki")
        rendered_ids = [item["comment"].pk for item in ctx["rendered_comments"]]
        self.assertIn(self.comment.pk, rendered_ids)

    def test_comment_becomes_visible_to_others_once_scan_clears(self) -> None:
        from urbanlens.dashboard.controllers.comments import _build_context

        Comment.objects.filter(pk=self.comment.pk).update(pending_scan=False)
        ctx = _build_context(self.wiki.comments.all(), self.viewer, self.request, wiki=self.wiki, context_type="wiki")
        rendered_ids = [item["comment"].pk for item in ctx["rendered_comments"]]
        self.assertIn(self.comment.pk, rendered_ids)


class PostingACommentPhotoDoesNotBlockOnTheScanTests(TestCase):
    """End-to-end through the real view: posting a photo never touches clamd synchronously."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile)
        self.client.force_login(self.user)

    def test_post_succeeds_immediately_and_never_calls_the_scanner(self) -> None:
        with patch("urbanlens.dashboard.services.malware_scan.malware_error_for_upload") as scan, patch("urbanlens.dashboard.services.celery.safely_enqueue_task"):
            response = self.client.post(
                reverse("pin.comments", args=[self.pin.slug]),
                {"text": "check out this photo", "image": _fake_image()},
            )
        self.assertEqual(response.status_code, 200)
        scan.assert_not_called()
        comment = Comment.objects.get(pin=self.pin)
        self.assertTrue(comment.pending_scan)
