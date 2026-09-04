"""A media task that dies must leave the row in a state somebody can act on.

``process_image_upload`` sets ``Image.upload_processed_at`` on success and,
before this, set nothing at all on permanent failure. There was no
``task_failure`` receiver and no per-task ``on_failure``, so a row whose task
died kept ``upload_processed_at = None`` forever: the uploader saw a photo that
never finished, with no error and no way to retry, and nothing server-side
distinguished "still running" from "died three days ago".

``autoretry_for=(OSError,)`` does not cover it. Those retries run *inside* the
child, so anything that kills the child - OOM, a decoder segfault, a lost worker
- never reaches them.

The owner's ruling, and what these pin down: **the user retries; if they do not,
the upload is discarded**. So there are three states worth asserting, and the
transitions between them:

* stranded - the task died, and nothing has noticed yet;
* failed - noticed, recorded, and offered back to the uploader;
* gone - they did not take the offer, and the bytes are not kept forever.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.issues import PhotoIssueStatus, PhotoUploadFailure, PhotoUploadFailureKind
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.notifications.meta.type import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.upload_failures import (
    MAX_SWEEP_ATTEMPTS,
    MAX_USER_RETRIES,
    discard_failed_upload,
    record_upload_processing_failure,
    retry_upload_processing,
)


class RecordUploadProcessingFailureTests(TestCase):
    """Turning a stranded row into one the uploader can see and act on."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.profile = Profile.objects.get(user=baker.make(User))
        self.image = baker.make(
            Image, profile=self.profile, pending_scan=True, original_filename="cellar.jpg", image="pin_images/c.jpg"
        )

    def test_a_failure_is_recorded_against_the_row(self) -> None:
        record_upload_processing_failure(self.image.pk, "The worker handling this photo stopped unexpectedly.")

        self.image.refresh_from_db()
        self.assertIsNotNone(self.image.upload_failed_at, "nothing distinguished a dead task from a slow one")

    def test_the_uploader_is_told(self) -> None:
        record_upload_processing_failure(self.image.pk, "boom")

        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.profile, notification_type=NotificationType.PHOTO_UPLOAD_FAILED
            ).exists()
        )

    def test_a_reviewable_row_is_created_naming_the_file(self) -> None:
        """The uploader has to be able to tell which photo this was."""
        record_upload_processing_failure(self.image.pk, "boom")

        failure = PhotoUploadFailure.objects.get(image=self.image)
        self.assertEqual(failure.filename, "cellar.jpg")
        self.assertEqual(failure.kind, PhotoUploadFailureKind.PROCESSING_FAILED)
        self.assertEqual(failure.status, PhotoIssueStatus.PENDING)

    def test_the_photo_stays_quarantined(self) -> None:
        """``pending_scan`` must not be cleared by failing.

        It is what keeps unscanned, unstripped bytes out of every gallery. A
        failure is precisely the case where the scan did not finish.
        """
        record_upload_processing_failure(self.image.pk, "boom")

        self.image.refresh_from_db()
        self.assertTrue(self.image.pending_scan)

    def test_recording_twice_leaves_one_reviewable_row(self) -> None:
        """The sweep and any future task_failure receiver can both reach this."""
        record_upload_processing_failure(self.image.pk, "boom")
        record_upload_processing_failure(self.image.pk, "boom again")

        self.assertEqual(
            PhotoUploadFailure.objects.filter(image=self.image, status=PhotoIssueStatus.PENDING).count(), 1
        )

    def test_a_profile_less_row_records_nothing(self) -> None:
        """Enrichment imagery belongs to nobody; there is no one to offer it to."""
        orphan = baker.make(Image, profile=None, pending_scan=True, image="pin_images/o.jpg")

        self.assertIsNone(record_upload_processing_failure(orphan.pk, "boom"))
        self.assertFalse(PhotoUploadFailure.objects.filter(image=orphan).exists())


class RetryAndDiscardTests(TestCase):
    """The two things the owner said a failed upload may become."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = Profile.objects.get(user=baker.make(User))
        self.image = baker.make(
            Image, profile=self.profile, pending_scan=True, original_filename="cellar.jpg", image="pin_images/c.jpg"
        )
        self.failure = record_upload_processing_failure(self.image.pk, "boom")

    def test_a_retry_re_enqueues_and_clears_the_failure(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media.upload_failures.safely_enqueue_task") as enqueue:
            self.assertTrue(retry_upload_processing(self.failure, self.profile))

        enqueue.assert_called_once()
        self.image.refresh_from_db()
        self.assertIsNone(self.image.upload_failed_at)
        self.assertEqual(self.image.upload_sweep_attempts, 0, "a user retry restarts the sweep's budget too")

    def test_somebody_else_cannot_retry_your_upload(self) -> None:
        stranger = Profile.objects.get(user=baker.make(User))

        with mock.patch("urbanlens.dashboard.services.media.upload_failures.safely_enqueue_task") as enqueue:
            self.assertFalse(retry_upload_processing(self.failure, stranger))

        enqueue.assert_not_called()

    def test_retrying_is_bounded(self) -> None:
        """A file that deterministically kills the decoder must not be endless.

        Without a ceiling the retry button is a way to feed the same
        child-killing file back to a two-slot sandbox worker forever.
        """
        with mock.patch("urbanlens.dashboard.services.media.upload_failures.safely_enqueue_task"):
            for _ in range(MAX_USER_RETRIES):
                self.failure.refresh_from_db()
                record_upload_processing_failure(self.image.pk, "boom")
                self.assertTrue(retry_upload_processing(self.failure, self.profile))

            self.failure.refresh_from_db()
            record_upload_processing_failure(self.image.pk, "boom")
            self.assertFalse(retry_upload_processing(self.failure, self.profile))

    def test_discarding_removes_the_photo(self) -> None:
        """ "Discard the upload if they don't [retry]" - the owner's ruling."""
        discard_failed_upload(self.failure)

        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())
        self.failure.refresh_from_db()
        self.assertEqual(self.failure.status, PhotoIssueStatus.DISMISSED)

    def test_a_discarded_failure_keeps_its_filename(self) -> None:
        """The row outlives the photo, so the user can see what went away."""
        discard_failed_upload(self.failure)

        self.failure.refresh_from_db()
        self.assertEqual(self.failure.filename, "cellar.jpg")


class UnretriedDiscardSweepTests(TestCase):
    """What happens to a failure nobody comes back for."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = Profile.objects.get(user=baker.make(User))

    def _aged_failure(self, age: datetime.timedelta) -> PhotoUploadFailure:
        image = baker.make(
            Image, profile=self.profile, pending_scan=True, original_filename="old.jpg", image="pin_images/o.jpg"
        )
        failure = record_upload_processing_failure(image.pk, "boom")
        Image.objects.filter(pk=image.pk).update(upload_failed_at=timezone.now() - age)
        return failure

    def test_an_old_unretried_failure_is_discarded(self) -> None:
        from urbanlens.dashboard.services.media.upload_failures import UNRETRIED_DISCARD_AGE
        from urbanlens.dashboard.tasks import discard_unretried_failed_uploads

        failure = self._aged_failure(UNRETRIED_DISCARD_AGE + datetime.timedelta(hours=1))

        self.assertEqual(discard_unretried_failed_uploads(), 1)

        failure.refresh_from_db()
        self.assertEqual(failure.status, PhotoIssueStatus.DISMISSED)

    def test_a_recent_failure_is_left_for_the_user(self) -> None:
        """The window is the user's chance to retry; do not shorten it silently."""
        from urbanlens.dashboard.tasks import discard_unretried_failed_uploads

        failure = self._aged_failure(datetime.timedelta(minutes=5))

        self.assertEqual(discard_unretried_failed_uploads(), 0)

        failure.refresh_from_db()
        self.assertEqual(failure.status, PhotoIssueStatus.PENDING)


class SweepBoundTests(TestCase):
    """The recovery sweep gives up rather than re-enqueuing forever."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = Profile.objects.get(user=baker.make(User))

    def test_the_sweep_records_a_failure_once_its_budget_is_spent(self) -> None:
        """The loop this closes: a row whose child dies is re-fed every tick.

        Each pass costs a sandbox slot on a file that has already killed a
        worker, and nothing ever tells the uploader.
        """
        from urbanlens.dashboard.tasks import STALLED_UPLOAD_AGE, requeue_stalled_pending_uploads

        image = baker.make(
            Image, profile=self.profile, pending_scan=True, original_filename="doomed.jpg", image="pin_images/d.jpg"
        )
        Image.objects.filter(pk=image.pk).update(
            created=timezone.now() - STALLED_UPLOAD_AGE - datetime.timedelta(hours=1)
        )

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            for _ in range(MAX_SWEEP_ATTEMPTS):
                requeue_stalled_pending_uploads()
            self.assertEqual(enqueue.call_count, MAX_SWEEP_ATTEMPTS)

            requeue_stalled_pending_uploads()
            self.assertEqual(
                enqueue.call_count, MAX_SWEEP_ATTEMPTS, "the sweep kept re-enqueuing a row it had already given up on"
            )

        self.assertTrue(PhotoUploadFailure.objects.filter(image=image, status=PhotoIssueStatus.PENDING).exists())

    def test_a_recorded_failure_is_not_swept_again(self) -> None:
        from urbanlens.dashboard.tasks import STALLED_UPLOAD_AGE, requeue_stalled_pending_uploads

        image = baker.make(Image, profile=self.profile, pending_scan=True, image="pin_images/d.jpg")
        Image.objects.filter(pk=image.pk).update(
            created=timezone.now() - STALLED_UPLOAD_AGE - datetime.timedelta(hours=1)
        )
        record_upload_processing_failure(image.pk, "boom")

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            requeue_stalled_pending_uploads()

        enqueue.assert_not_called()


class FailureCardViewTests(TestCase):
    """The affordances the owner asked for, reached the way a person reaches them."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.image = baker.make(
            Image, profile=self.profile, pending_scan=True, original_filename="cellar.jpg", image="pin_images/c.jpg"
        )
        self.failure = record_upload_processing_failure(self.image.pk, "boom")

    def test_the_page_offers_retry_and_discard_for_a_processing_failure(self) -> None:
        from django.urls import reverse

        response = self.client.get(reverse("vault.photos"))

        self.assertContains(response, reverse("vault.photos.failures.retry", args=[self.failure.pk]))
        self.assertContains(response, reverse("vault.photos.failures.discard", args=[self.failure.pk]))

    def test_a_failed_photo_is_not_also_in_the_attention_queue(self) -> None:
        """One photo, one card. It has actions of its own under "Couldn't upload"."""
        from django.urls import reverse

        response = self.client.get(reverse("vault.photos"))

        self.assertNotIn(self.image, [card["image"] for card in response.context["attention_cards"]])

    def test_retry_queues_a_re_run(self) -> None:
        from django.urls import reverse

        with mock.patch("urbanlens.dashboard.services.media.upload_failures.safely_enqueue_task") as enqueue:
            response = self.client.post(reverse("vault.photos.failures.retry", args=[self.failure.pk]))

        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once()

    def test_discard_removes_the_photo(self) -> None:
        from django.urls import reverse

        response = self.client.post(reverse("vault.photos.failures.discard", args=[self.failure.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())

    def test_somebody_else_gets_a_404_not_a_refusal(self) -> None:
        """No oracle: a stranger learns nothing about whether this failure exists."""
        from django.urls import reverse

        other = baker.make(User)
        self.client.force_login(other)

        self.assertEqual(
            self.client.post(reverse("vault.photos.failures.retry", args=[self.failure.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.post(reverse("vault.photos.failures.discard", args=[self.failure.pk])).status_code, 404
        )

    def test_a_refused_retry_puts_the_card_back(self) -> None:
        """A refusal from a card-swapping button must not swallow the card.

        `_toast`'s own docstring documents this trap: an empty body reports the
        problem and removes the only thing left to act on.
        """
        from django.urls import reverse

        type(self.failure).objects.filter(pk=self.failure.pk).update(user_retries=MAX_USER_RETRIES)

        response = self.client.post(reverse("vault.photos.failures.retry", args=[self.failure.pk]))

        self.assertContains(response, f"photo-failure-{self.failure.pk}")
