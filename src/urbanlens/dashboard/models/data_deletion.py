"""Models for self-service account data deletion."""

from __future__ import annotations

import uuid

from django.db import models

from urbanlens.dashboard.models import abstract


class DataDeletionRequest(abstract.Model):
    """Audit row for a user-requested full account data deletion."""

    class Status(models.TextChoices):
        ARCHIVING = "archiving", "Archiving"
        ARCHIVED = "archived", "Archived"
        DELETED = "deleted", "Deleted"
        PURGED = "purged", "Purged"
        FAILED = "failed", "Failed"

    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user_id_snapshot = models.PositiveIntegerField()
    username = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    archive_path = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ARCHIVING)
    deleted_at = models.DateTimeField(null=True, blank=True)
    purge_after = models.DateTimeField()
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["request_id"]), models.Index(fields=["purge_after", "status"])]
