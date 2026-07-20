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
    # True from creation until the async malware scan (tasks.scan_comment_image)
    # clears a newly-uploaded image - never set for a comment with no image, or
    # one attached via "Choose Existing" (already scanned on its original
    # upload). While True, the comment is visible only to its own author (see
    # controllers.comments._build_context) - not shown to other viewers until
    # the scan confirms it's clean, so posting never has to wait on clamd.
    pending_scan = models.BooleanField(default=False)

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
    # Set by MarkupMap's pre_delete signal when the attached map above is
    # deleted, so the comment can keep showing "map removed" instead of
    # silently losing all trace that one was ever here.
    map_removed = models.BooleanField(default=False)
    # Set by this model's own pre_delete signal (see signals.py) on every
    # reply of a comment that's about to be deleted, before `parent` is
    # nulled out by SET_NULL below. Without this, a reply to a deleted
    # comment silently becomes an unexplained top-level comment - UL-219.
    # With it, the reply keeps rendering in place with a "[Original comment
    # deleted]" placeholder standing in for the parent, instead of losing
    # its thread context entirely.
    parent_deleted = models.BooleanField(default=False)

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
