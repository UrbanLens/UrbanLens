"""CommentLocationMention - which locations a comment's text names.

Gate 3 of the comment visibility rules (see ``services.comments.comments``)
drops a comment entirely when it names a location the viewer has not pinned.
Answering that by parsing text meant no caller could page a comment list in
SQL, because which rows a page should contain was not knowable until every row
had been read and parsed.

These rows are derived from the comment's ``text`` and written by its
``save`` (see :class:`LocationMentioningModel`); they are not independently
editable - the text is the source of truth and the rows are its index.

One table for both comment kinds, because it is one gate: ``Comment`` (pins and
wikis) and ``TripComment`` apply the identical rule to the identical field, and
a second table would be a second place for the derivation to be forgotten.

``location_uuid``, not a ``Location`` foreign key, on purpose: a comment naming
a uuid that resolves to nothing is hidden from everybody (nobody has pinned a
location that does not exist), and an FK could not record one, which would make
such a comment visible to all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import CheckConstraint, Index, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.db.models import Manager


class LocationMentioningModel(models.Model):
    """Keeps a comment model's mention rows in step with its text.

    A mixin rather than a call at each write site: the visibility gate reads
    these rows, so a path that stored text without deriving them would not
    fail - it would quietly show a comment to someone who has not pinned what
    it names.

    Attributes:
        mention_owner_field: Name of this model's foreign key on
            :class:`CommentLocationMention`.
        location_mentions: The reverse manager for those rows. Declared here
            because the mixin genuinely requires it - a model that mixes this
            in without the matching foreign key would derive nothing.
    """

    mention_owner_field: str = ""

    if TYPE_CHECKING:
        location_mentions: Manager[CommentLocationMention]

    class Meta:
        abstract = True

    def save(self, *args, **kwargs) -> None:
        """Persist the row and re-derive the locations its text names.

        Args:
            *args: Passed through to ``Model.save``.
            **kwargs: Passed through to ``Model.save``. ``update_fields``
                without ``text`` skips the sync, since nothing it reads moved.
        """
        update_fields = kwargs.get("update_fields")
        text_may_have_changed = update_fields is None or "text" in set(update_fields)
        super().save(*args, **kwargs)
        if text_may_have_changed:
            self.sync_location_mentions()

    def sync_location_mentions(self) -> None:
        """Make the mention rows match what ``text`` currently names."""
        from urbanlens.dashboard.services.notifications.mentions import extract_location_uuids

        named = set(extract_location_uuids(getattr(self, "text", "") or ""))
        recorded = set(self.location_mentions.values_list("location_uuid", flat=True))
        if named == recorded:
            return
        if stale := recorded - named:
            self.location_mentions.filter(location_uuid__in=stale).delete()
        if added := named - recorded:
            CommentLocationMention.objects.bulk_create(
                [CommentLocationMention(**{self.mention_owner_field: self}, location_uuid=value) for value in added],
                ignore_conflicts=True,
            )


class CommentLocationMention(abstract.DashboardModel):
    """One location named by one comment, of either kind."""

    comment = models.ForeignKey("dashboard.Comment", on_delete=models.CASCADE, related_name="location_mentions", null=True, blank=True)
    trip_comment = models.ForeignKey("dashboard.TripComment", on_delete=models.CASCADE, related_name="location_mentions", null=True, blank=True)
    location_uuid = models.UUIDField()

    if TYPE_CHECKING:
        comment_id: int | None
        trip_comment_id: int | None

    def __str__(self) -> str:
        """Return a human-readable description of this mention.

        Returns:
            String like "Comment 5 mentions 0c8f...".
        """
        owner = f"Comment {self.comment_id}" if self.comment_id is not None else f"TripComment {self.trip_comment_id}"
        return f"{owner} mentions {self.location_uuid}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_comment_location_mentions"
        ordering = ["id"]
        indexes = [Index(fields=["location_uuid"], name="idxdb_cmtloc_uuid")]
        constraints = [
            UniqueConstraint(fields=["comment", "location_uuid"], name="uq_cmtloc_one_per_comment"),
            UniqueConstraint(fields=["trip_comment", "location_uuid"], name="uq_cmtloc_one_per_trip_comment"),
            CheckConstraint(
                condition=Q(comment__isnull=False, trip_comment__isnull=True) | Q(comment__isnull=True, trip_comment__isnull=False),
                name="ck_cmtloc_exactly_one_owner",
            ),
        ]
