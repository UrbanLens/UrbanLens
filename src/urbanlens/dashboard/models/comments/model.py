"""Comment model - for Pin and Wiki comments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db import models

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.comments.queryset import CommentManager
from urbanlens.dashboard.services.text_limits import MAX_COMMENT_TEXT_LENGTH


class Comment(abstract.FrontendDashboardModel):
    """A user comment on a Pin or a Wiki (community) page.

    Exactly one of ``pin`` or ``wiki`` must be non-null.
    ``parent`` is set only for replies (depth-1 threading).
    """

    text = models.TextField(max_length=MAX_COMMENT_TEXT_LENGTH, validators=[MaxLengthValidator(MAX_COMMENT_TEXT_LENGTH)])
    image = models.ImageField(upload_to="comment_images/", null=True, blank=True)

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
    # Standalone map (viewport + markup items) attached to this comment.
    markup_map = models.ForeignKey(
        "dashboard.MarkupMap",
        on_delete=models.SET_NULL,
        related_name="comments",
        null=True,
        blank=True,
    )

    if TYPE_CHECKING:
        pin_id: int | None
        wiki_id: int | None
        profile_id: int
        parent_id: int | None
        markup_map_id: int | None

    objects = CommentManager()

    @property
    def map_data(self) -> dict | None:
        """Client snapshot of the attached markup map, if any.

        Kept as a property so templates and viewer JS that consumed the old
        ``map_data`` JSON column keep working against the MarkupMap relation.

        Returns:
            Snapshot dict ({center_lat, center_lng, zoom, layer_mode,
            show_borders, markup}) or None when no map is attached.
        """
        return self.markup_map.to_snapshot() if self.markup_map else None

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_comments"
        get_latest_by = "updated"
        ordering = ["created"]
        indexes = [models.Index(fields=["uuid"], name="idxdb_comment_uuid")]
