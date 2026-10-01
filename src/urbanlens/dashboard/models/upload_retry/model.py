"""An upload storage failed on, waiting to be tried again rather than dropped or rejected (P119)."""

from __future__ import annotations

from django.db.models import CharField, DateTimeField, Index, PositiveBigIntegerField, PositiveIntegerField, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.upload_retry.queryset import UploadRetryManager


class UploadRetry(abstract.DashboardModel):
    """One upload waiting for storage. ``created`` is when it began waiting, and ``updated`` its latest failure."""

    #: A held field's key, e.g. ``"dashboard.Profile.avatar"``, or ``"dashboard.Comment.image"``.
    target = CharField(max_length=100)
    object_id = PositiveBigIntegerField()
    #: The stored name the upload was waiting on when it last failed.
    name = CharField(max_length=255)
    attempts = PositiveIntegerField(default=0)
    next_attempt_at = DateTimeField()
    last_error = CharField(max_length=255, blank=True, default="")
    #: When storage first said the file is gone.
    gone_since = DateTimeField(null=True, blank=True)
    admin_notified_at = DateTimeField(null=True, blank=True)

    objects = UploadRetryManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_upload_retries"
        ordering = ["next_attempt_at"]
        indexes = [Index(fields=["next_attempt_at"], name="idxdb_uploadretry_next")]
        constraints = [UniqueConstraint(fields=["target", "object_id"], name="uq_uploadretry_target_object")]

    def __str__(self) -> str:
        return f"UploadRetry({self.target} {self.object_id}, {self.attempts} attempts)"
