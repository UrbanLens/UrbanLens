"""Reaction model - emoji reactions to comments."""

from __future__ import annotations

from django.db import models

from urbanlens.dashboard.models import abstract


class Reaction(abstract.DashboardModel):
    """An emoji reaction from a user on a Comment or TripComment.

    Exactly one of ``comment`` or ``trip_comment`` must be set.
    A profile can react with the same emoji only once per comment.
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
        ]
        indexes = [
            models.Index(fields=["comment"], name="reaction_comment_idx"),
            models.Index(fields=["trip_comment"], name="idxdb_react_trcomment"),
        ]
