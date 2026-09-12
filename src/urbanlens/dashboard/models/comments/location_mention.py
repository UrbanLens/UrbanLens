"""CommentLocationMention - which locations one comment's text names.

Gate 3 of the comment visibility rules (see ``services.comments.comments``)
drops a comment entirely when it names a location the viewer has not pinned.
Answering that by parsing text meant no caller could page a comment list in
SQL, because which rows a page should contain was not knowable until every row
had been read and parsed.

These rows are derived from ``Comment.text``, written by ``Comment.save``, and
are not independently editable - the text is the source of truth and the rows
are its index.

``location_uuid``, not a ``Location`` foreign key, on purpose: a comment naming
a uuid that resolves to nothing is hidden from everybody (nobody has pinned a
location that does not exist), and an FK could not record one, which would make
such a comment visible to all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Index, UniqueConstraint

from urbanlens.dashboard.models import abstract


class CommentLocationMention(abstract.DashboardModel):
    """One location named by one comment."""

    comment = models.ForeignKey("dashboard.Comment", on_delete=models.CASCADE, related_name="location_mentions")
    location_uuid = models.UUIDField()

    if TYPE_CHECKING:
        comment_id: int

    def __str__(self) -> str:
        """Return a human-readable description of this mention.

        Returns:
            String like "Comment 5 mentions 0c8f...".
        """
        return f"Comment {self.comment_id} mentions {self.location_uuid}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_comment_location_mentions"
        ordering = ["id"]
        indexes = [Index(fields=["location_uuid"], name="idxdb_cmtloc_uuid")]
        constraints = [UniqueConstraint(fields=["comment", "location_uuid"], name="uq_cmtloc_one_per_comment")]
