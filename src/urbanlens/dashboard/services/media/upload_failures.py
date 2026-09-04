"""What happens to an upload whose processing task died.

``process_image_upload`` marks success and, before this module, marked nothing
at all on permanent failure. A row whose task died kept ``upload_processed_at =
None`` forever: the uploader saw a photo that never finished, with no error and
no way to retry, and nothing server-side distinguished "still running" from
"died three days ago". ``autoretry_for=(OSError,)`` does not help - those
retries run *inside* the child, so anything that kills the child (OOM, a decoder
segfault, a lost worker) never reaches them.

The product decision this implements: **the user retries; if they do not, the
upload is discarded.** So a failure becomes a reviewable row on Vault → Photos
rather than a toast, which also covers an uploader who navigated away before the
failure happened - nothing here waits for them to be on the page.

Detection is the recovery sweep's job rather than a ``task_failure`` receiver's.
The sweep already exists, already runs hourly, and already knows how to
re-enqueue; giving it a budget is a smaller change than introducing a second
decision-maker in the worker's MainProcess, where a stale database connection
after a restart is its own failure mode. A receiver would only make the same
outcome arrive sooner, and can be added later behind its own tests.

Everything here is bounded in both directions: the sweep gives up after
:data:`MAX_SWEEP_ATTEMPTS`, the user after :data:`MAX_USER_RETRIES`, and an
untouched failure is discarded after :data:`UNRETRIED_DISCARD_AGE`. A file that
deterministically kills the decoder must not be a way to occupy a two-slot
sandbox worker forever, and that is exactly the file most likely to end up here.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Final

from django.db import IntegrityError, transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from urbanlens.dashboard.services.core.celery import safely_enqueue_task

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.issues import PhotoUploadFailure
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: How many times the recovery sweep re-enqueues one row before recording it as
#: failed. Two, because the sweep only ever sees rows that have already been
#: pending for ``STALLED_UPLOAD_AGE`` - a row reaching it once is already the
#: unusual case, and a third pass buys little against a file that has now killed
#: two workers.
MAX_SWEEP_ATTEMPTS: Final[int] = 2

#: How many times the owner may ask for a re-run from the UI. Separate from the
#: sweep's budget on purpose: a person retrying knows something the sweep does
#: not (the server was down, they will try later), so their attempts are theirs
#: to spend - but they are still finite.
MAX_USER_RETRIES: Final[int] = 2

#: How long a failed upload waits for its owner before being discarded. Long
#: enough to survive a weekend, since the notification may be the only prompt
#: and people do not check a photo vault daily. Must exceed the sweep interval,
#: or a row could be discarded before the sweep has finished with it.
UNRETRIED_DISCARD_AGE: Final[timedelta] = timedelta(days=7)


def _failure_url(image: Image) -> str:
    """Where to send the uploader to act on a failed upload.

    Args:
        image: The row that failed.

    Returns:
        A URL, or ``""`` when none can be built.
    """
    try:
        return reverse("vault.photos")
    except NoReverseMatch:  # pragma: no cover - the route is registered
        logger.warning("Could not build a photo URL while notifying about a failed upload (image %s)", image.pk)
        return ""


def record_upload_processing_failure(image_id: int, reason: str) -> PhotoUploadFailure | None:
    """Record that this row's processing died, and tell its owner.

    Deliberately leaves ``pending_scan`` set. That flag is what keeps unscanned,
    unstripped bytes out of every gallery, and a processing failure is precisely
    the case where the scan did not finish - clearing it here would publish the
    bytes this failed to check.

    Idempotent by database constraint rather than by reading first, because the
    sweep and any future ``task_failure`` receiver can both reach this for one
    row and a pre-read races between them.

    Args:
        image_id: The row whose task died.
        reason: User-facing explanation.

    Returns:
        The reviewable row, or None when there is nobody to offer it to - a
        profile-less row is enrichment imagery belonging to no one.
    """
    from urbanlens.dashboard.models.images.issues import PhotoIssueStatus, PhotoUploadFailure, PhotoUploadFailureKind
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.notifications.meta.type import NotificationType
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    image = Image.objects.filter(pk=image_id).select_related("profile", "pin").first()
    if image is None:
        return None
    if image.profile is None:
        logger.info("Upload processing failed for profile-less image %s; nothing to offer back", image_id)
        Image.objects.filter(pk=image_id).update(upload_failed_at=timezone.now())
        return None

    Image.objects.filter(pk=image_id).update(upload_failed_at=timezone.now())

    existing = PhotoUploadFailure.objects.filter(image=image, status=PhotoIssueStatus.PENDING).first()
    if existing is not None:
        return existing

    try:
        with transaction.atomic():
            failure = PhotoUploadFailure.objects.create(
                profile=image.profile,
                filename=(image.original_filename or "photo")[:255],
                error=reason,
                pin=image.pin,
                image=image,
                kind=PhotoUploadFailureKind.PROCESSING_FAILED,
            )
    except IntegrityError:
        # Another writer got there first; the constraint is the point.
        return PhotoUploadFailure.objects.filter(image=image, status=PhotoIssueStatus.PENDING).first()

    NotificationLog.objects.notify(
        profile=image.profile,
        notification_type=NotificationType.PHOTO_UPLOAD_FAILED,
        title="A photo could not be processed",
        message=reason,
        url=_failure_url(image),
    )
    return failure


def reenqueue_upload(image: Image) -> None:
    """Put a row back on the processing queue, with the cap its source implies.

    A profile-less row is provider imagery whose longest-edge cap lived only at
    the call site that created it, so it is recovered from ``source`` rather
    than left to the generic default - the same recovery
    ``requeue_stalled_pending_uploads`` does.

    Args:
        image: The row to reprocess.
    """
    from urbanlens.dashboard.services.photos.photo_enrichment import enriched_max_dimension
    from urbanlens.dashboard.tasks import process_image_upload

    max_dimension = None if image.profile_id is not None else enriched_max_dimension(image.source)
    safely_enqueue_task(process_image_upload, image.pk, max_dimension)


def retry_upload_processing(failure: PhotoUploadFailure, profile: Profile) -> bool:
    """Re-run processing for a failed upload, at its owner's request.

    Args:
        failure: The reviewable row.
        profile: Who is asking. Checked against the row's owner.

    Returns:
        Whether a re-run was queued. False when the asker is not the owner,
        the photo is gone, or this failure has used up its retries.
    """
    from urbanlens.dashboard.models.images.model import Image

    if failure.profile_id != profile.pk:
        return False
    if failure.image_id is None or failure.user_retries >= MAX_USER_RETRIES:
        return False

    image = Image.objects.filter(pk=failure.image_id).first()
    if image is None:
        return False

    # A person retrying knows something the sweep does not, so their request
    # restores the sweep's budget as well as spending one of their own.
    Image.objects.filter(pk=image.pk).update(upload_failed_at=None, upload_sweep_attempts=0)
    type(failure).objects.filter(pk=failure.pk).update(user_retries=failure.user_retries + 1)
    reenqueue_upload(image)
    logger.info("Re-queued upload processing for image %s at its owner's request", image.pk)
    return True


def teardown_image_and_siblings(image: Image) -> None:
    """Delete a row and any deduplicated siblings pointing at its file.

    A sibling holds no file of its own - it points at this row's. Deleting the
    original without them leaves rows whose bytes are gone, which
    ``_clear_orphaned_dedup_siblings`` later un-quarantines and publishes.

    Args:
        image: The row being removed.
    """
    from urbanlens.dashboard.models.images.model import Image as ImageModel, QuotaExemption
    from urbanlens.dashboard.services.media.images import delete_stored_file

    siblings = list(ImageModel.objects.filter(checksum=image.checksum, quota_exempt_reason=QuotaExemption.DEDUPLICATED).exclude(pk=image.pk)) if image.checksum else []
    sibling_pks = [sibling.pk for sibling in siblings]
    for sibling in siblings:
        delete_stored_file(sibling, also_deleting=[image.pk, *sibling_pks])
        sibling.delete()
    delete_stored_file(image, also_deleting=sibling_pks)
    image.delete()


def discard_failed_upload(failure: PhotoUploadFailure) -> None:
    """Throw away the photo behind a failed upload, keeping the record of it.

    The failure row outlives the photo on purpose: the filename is how the
    uploader recognises which picture went away, and it is the only trace left
    once the bytes are gone.

    Args:
        failure: The reviewable row.
    """
    from urbanlens.dashboard.models.images.issues import PhotoIssueStatus
    from urbanlens.dashboard.models.images.model import Image

    if failure.image_id is not None:
        image = Image.objects.filter(pk=failure.image_id).first()
        if image is not None:
            teardown_image_and_siblings(image)

    type(failure).objects.filter(pk=failure.pk).update(status=PhotoIssueStatus.DISMISSED, image=None)
