"""Upload failures and metadata conflicts the user can review on Memories."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, ForeignKey, Index, JSONField, PositiveSmallIntegerField, Q, TextField, UniqueConstraint

from urbanlens.dashboard.models import abstract


class PhotoIssueStatus(abstract.TextChoices):
    """Whether a reviewable photo issue still needs the owner."""

    PENDING = "pending", "Pending"
    RESOLVED = "resolved", "Resolved"
    DISMISSED = "dismissed", "Dismissed"


class PhotoUploadFailureKind(abstract.TextChoices):
    """Why a photo is on the "couldn't upload" list, and what can be done about it.

    The two differ in whether there is anything left to retry. A rejected
    upload never became a row - the bytes are gone, so retrying means picking
    the file again. A processing failure has a stored row whose task died, so
    the server can re-run it without the user finding the original.
    """

    UPLOAD_REJECTED = "upload_rejected", "Upload rejected"
    PROCESSING_FAILED = "processing_failed", "Processing failed"


class PhotoUploadFailure(abstract.DashboardModel):
    """A photo that failed to upload or could not be shown after upload.

    Surfaced on Vault → Photos so the user can see the filename and retry
    without hunting through a toast that has already disappeared - which is
    also what covers the uploader who navigated away before the failure
    happened, since nothing about this waits for them to be on the page.

    Attributes:
        profile: The uploader this failure belongs to.
        filename: The original file name, so they can find it on disk. Kept
            after the photo is discarded, so they can see what went away.
        error: User-facing explanation of what went wrong.
        pin: The pin they were uploading to, if any.
        album: The album they were uploading into, if any.
        image: The stored row whose processing died, when there is one. NULL
            for a rejected upload, which never became a row, and after a
            discard - the failure outlives the photo.
        kind: Whether there is a stored row to re-run, or only a filename.
        user_retries: How many times the owner has asked for a re-run. Bounded:
            a file that deterministically kills the decoder must not be a way to
            feed the sandbox worker forever.
        status: Pending until they retry successfully or dismiss it.
    """

    filename = CharField(max_length=255)
    error = TextField()
    status = CharField(max_length=20, choices=PhotoIssueStatus.choices, default=PhotoIssueStatus.PENDING)
    #: Defaulted to the pre-existing meaning so the five call sites that predate
    #: this field stay correct with no data migration.
    kind = CharField(max_length=20, choices=PhotoUploadFailureKind.choices, default=PhotoUploadFailureKind.UPLOAD_REJECTED)
    user_retries = PositiveSmallIntegerField(default=0)

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="photo_upload_failures")
    pin = ForeignKey("dashboard.Pin", on_delete=SET_NULL, null=True, blank=True, related_name="photo_upload_failures")
    album = ForeignKey("dashboard.Album", on_delete=SET_NULL, null=True, blank=True, related_name="photo_upload_failures")
    image = ForeignKey("dashboard.Image", on_delete=SET_NULL, null=True, blank=True, related_name="upload_failures")

    if TYPE_CHECKING:
        profile_id: int
        pin_id: int | None
        album_id: int | None
        image_id: int | None

    class Meta:
        app_label = "dashboard"
        db_table = "dashboard_photo_upload_failure"
        indexes = [Index(fields=["profile", "status"], name="idx_photo_fail_profile_status")]
        constraints = [
            # Idempotence by constraint rather than by check-then-create: the
            # sweep and any later task_failure receiver can both reach the
            # recorder for one row, and a pre-read races between them.
            UniqueConstraint(fields=["image"], condition=Q(status=PhotoIssueStatus.PENDING), name="uq_photo_fail_pending_image"),
        ]


class PhotoMetadataConflict(abstract.DashboardModel):
    """Two copies of the same photo (same bytes) disagree on metadata.

    Created when a user re-uploads a file they already have, we reuse the
    stored bytes, and at least one field (caption, author, dates, GPS) cannot
    be merged automatically. Review lives on Memories so it does not interrupt
    the upload.

    Attributes:
        profile: The owner of both copies.
        existing_image: The earlier row.
        new_image: The row created for the later upload.
        fields: Mapping of field name to ``[existing_value, new_value]``.
        status: Pending until the owner picks a value per field.
    """

    fields = JSONField(default=dict)
    status = CharField(max_length=20, choices=PhotoIssueStatus.choices, default=PhotoIssueStatus.PENDING)

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="photo_metadata_conflicts")
    existing_image = ForeignKey("dashboard.Image", on_delete=CASCADE, related_name="metadata_conflicts_as_existing")
    new_image = ForeignKey("dashboard.Image", on_delete=CASCADE, related_name="metadata_conflicts_as_new")

    if TYPE_CHECKING:
        profile_id: int
        existing_image_id: int
        new_image_id: int

    class Meta:
        app_label = "dashboard"
        db_table = "dashboard_photo_metadata_conflict"
        indexes = [Index(fields=["profile", "status"], name="idx_photo_meta_profile_status")]
