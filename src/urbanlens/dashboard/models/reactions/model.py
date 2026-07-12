"""Reaction model - emoji reactions to comments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

from urbanlens.dashboard.models import abstract


class Reaction(abstract.DashboardModel):
    """An emoji reaction from a user on a Comment, TripComment, or DirectMessage.

    Exactly one of ``comment``, ``trip_comment``, or ``direct_message`` must be
    set. A profile can react with the same emoji only once per target, but may
    react to the same target with several different emoji.
    """

    emoji = models.CharField(max_length=10)

    profile = models.ForeignKey(
        "dashboard.Profile",
        on_delete=models.CASCADE,
        related_name="reactions",
    )
    comment = models.ForeignKey(
        "dashboard.Comment",
        on_delete=models.CASCADE,
        related_name="reactions",
        null=True,
        blank=True,
    )
    trip_comment = models.ForeignKey(
        "dashboard.TripComment",
        on_delete=models.CASCADE,
        related_name="reactions",
        null=True,
        blank=True,
    )
    direct_message = models.ForeignKey(
        "dashboard.DirectMessage",
        on_delete=models.CASCADE,
        related_name="reactions",
        null=True,
        blank=True,
    )

    if TYPE_CHECKING:
        profile_id: int
        comment_id: int | None
        trip_comment_id: int | None
        direct_message_id: int | None

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_reactions"
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "emoji", "comment"],
                condition=models.Q(comment__isnull=False),
                name="unique_reaction_comment",
            ),
            models.UniqueConstraint(
                fields=["profile", "emoji", "trip_comment"],
                condition=models.Q(trip_comment__isnull=False),
                name="unique_reaction_trip_comment",
            ),
            models.UniqueConstraint(
                fields=["profile", "emoji", "direct_message"],
                condition=models.Q(direct_message__isnull=False),
                name="unique_reaction_direct_message",
            ),
        ]
        indexes = [
            models.Index(fields=["comment"], name="reaction_comment_idx"),
            models.Index(fields=["trip_comment"], name="idxdb_react_trcomment"),
            models.Index(fields=["direct_message"], name="idxdb_react_dm"),
        ]
