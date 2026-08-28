"""Upload failures and metadata conflicts the user can review on Memories."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, ForeignKey, Index, JSONField, TextField

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.album.model import Album
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile


class PhotoIssueStatus(abstract.TextChoices):
    """Whether a reviewable photo issue still needs the owner."""

    PENDING = "pending", "Pending"
    RESOLVED = "resolved", "Resolved"
    DISMISSED = "dismissed", "Dismissed"


class PhotoUploadFailure(abstract.DashboardModel):
    """A photo that failed to upload or could not be shown after upload.

    Surfaced on Memories → Photos so the user can see the filename and retry
    without hunting through a toast that has already disappeared.

    Attributes:
        profile: The uploader this failure belongs to.
        filename: The original file name, so they can find it on disk.
        error: User-facing explanation of what went wrong.
        pin: The pin they were uploading to, if any.
        album: The album they were uploading into, if any.
        status: Pending until they retry successfully or dismiss it.
    """

    filename = CharField(max_length=255)
    error = TextField()
    status = CharField(max_length=20, choices=PhotoIssueStatus.choices, default=PhotoIssueStatus.PENDING)

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="photo_upload_failures")
    pin = ForeignKey("dashboard.Pin", on_delete=SET_NULL, null=True, blank=True, related_name="photo_upload_failures")
    album = ForeignKey("dashboard.Album", on_delete=SET_NULL, null=True, blank=True, related_name="photo_upload_failures")

    if TYPE_CHECKING:
        profile_id: int
        pin_id: int | None
        album_id: int | None

    class Meta:
        app_label = "dashboard"
        db_table = "dashboard_photo_upload_failure"
        indexes = [Index(fields=["profile", "status"], name="idx_photo_fail_profile_status")]


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
        existing_image: Image
        new_image: Image
        profile: Profile
        pin: Pin | None
        album: Album | None

    class Meta:
        app_label = "dashboard"
        db_table = "dashboard_photo_metadata_conflict"
        indexes = [Index(fields=["profile", "status"], name="idx_photo_meta_profile_status")]
