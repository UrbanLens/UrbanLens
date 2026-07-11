"""WikiStatVote - one profile's vote on a community stat field for a Wiki.

Danger, vulnerability, priority, and rating are personal star ratings on a
Pin. A Wiki has no single owner, so the equivalent fields there are a
composite (average) of every contributing profile's vote rather than a
single stored value - this model holds those individual votes.

Deleting a profile's vote (rather than storing a zero) is how a vote is
cleared, so the composite average is never skewed by "no opinion" rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import CASCADE, ForeignKey, Index, TextChoices, UniqueConstraint
from django.db.models.fields import CharField, IntegerField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.wiki_stat_vote.queryset import WikiStatVoteManager


class WikiStatField(TextChoices):
    """The community stat fields a profile may vote on for a Wiki."""

    DANGER = "danger", "Danger"
    VULNERABILITY = "vulnerability", "Vulnerability"
    PRIORITY = "priority", "Priority"
    RATING = "rating", "Rating"


class WikiStatVote(abstract.DashboardModel):
    """A single profile's 1-5 vote on one community stat field for a Wiki."""

    field = CharField(max_length=20, choices=WikiStatField.choices)
    value = IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])

    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        related_name="stat_votes",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="wiki_stat_votes",
    )

    if TYPE_CHECKING:
        wiki_id: int
        profile_id: int

    objects = WikiStatVoteManager()

    def __str__(self) -> str:
        return f"{self.get_field_display()}={self.value} on wiki {self.wiki_id} by profile {self.profile_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_wiki_stat_votes"
        indexes = [
            Index(fields=["wiki", "field"], name="idxdb_wsv_wiki_field"),
        ]
        constraints = [
            UniqueConstraint(fields=["wiki", "profile", "field"], name="db_wiki_stat_vote_unique"),
        ]
