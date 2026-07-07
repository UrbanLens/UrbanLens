"""Comment model - for Pin and Location (wiki) comments."""

from __future__ import annotations

from uuid import uuid4

from django.db import models

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.comments.queryset import CommentManager


class Comment(abstract.FrontendDashboardModel):
    """A user comment on a Pin or a Wiki (community) page.

    Exactly one of ``pin`` or ``wiki`` must be non-null.
    ``parent`` is set only for replies (depth-1 threading).
    """

    pin = models.ForeignKey(
        "dashboard.Pin",
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    wiki = models.ForeignKey(
        "dashboard.Wiki",
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    profile = models.ForeignKey(
        "dashboard.Profile",
        on_delete=models.CASCADE,
        related_name="comments",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="replies",
        null=True,
        blank=True,
    )
    text = models.TextField()
    image = models.ImageField(upload_to="comment_images/", null=True, blank=True)
    # Snapshot of a Leaflet map attached to this comment.
    # Schema: {center_lat, center_lng, zoom, detail_pins: [...], markup: [...]}
    map_data = models.JSONField(null=True, blank=True)

    objects = CommentManager()

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_comments"
        get_latest_by = "updated"
        ordering = ["created"]
        indexes = [models.Index(fields=["uuid"], name="idxdb_comment_uuid")]
