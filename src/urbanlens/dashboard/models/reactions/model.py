"""Reaction model - emoji reactions to comments, direct messages, and group messages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.reactions.queryset import ReactionManager


class Reaction(abstract.DashboardModel):
    """An emoji reaction from a user on a Comment, TripComment, DirectMessage, or GroupMessage.
    Exactly one of ``comment``, ``trip_comment``, ``direct_message``, or ``group_message`` must be set.
    A profile can react with the same emoji only once per target, but may react to the same target with several different emoji.
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
    # CASCADE, matching every sibling host: a hard-deleted message must not leave orphan reaction
    # rows that later aggregate against a missing row.
    # (``GroupMessage.deleted_at`` is a soft tombstone and does not delete the row, so reactions on
    # a deleted-for-everyone message survive exactly as the message itself does.)
    group_message = models.ForeignKey(
        "dashboard.GroupMessage",
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
        group_message_id: int | None

    objects = ReactionManager()

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
            models.UniqueConstraint(
                fields=["profile", "emoji", "group_message"],
                condition=models.Q(group_message__isnull=False),
                name="unique_reaction_group_message",
            ),
        ]
        indexes = [
            models.Index(fields=["comment"], name="reaction_comment_idx"),
        ]
