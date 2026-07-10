"""QuerySet/Manager for WikiStatVote, including composite (average) calculation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Avg, Count

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote


@dataclass(slots=True, frozen=True)
class WikiStatComposite:
    """The community composite for one stat field on one Wiki.

    Attributes:
        rounded: Nearest whole star (1-5) for the filled-star display, or
            None when nobody has voted yet.
        exact: The precise average (e.g. ``3.4``), or None with no votes.
        count: Number of votes the composite is drawn from.
    """

    rounded: int | None
    exact: float | None
    count: int


class WikiStatVoteQuerySet(abstract.DashboardQuerySet["WikiStatVote"]):
    """QuerySet for WikiStatVote, scoped by wiki/field/profile."""

    def for_wiki(self, wiki: Wiki) -> WikiStatVoteQuerySet:
        """Restrict to votes cast on ``wiki``."""
        return self.filter(wiki=wiki)

    def for_field(self, field: str) -> WikiStatVoteQuerySet:
        """Restrict to votes on the given stat field."""
        return self.filter(field=field)

    def composite(self, wiki: Wiki, field: str) -> WikiStatComposite:
        """Compute the community composite for ``field`` on ``wiki``.

        Args:
            wiki: The wiki whose votes to aggregate.
            field: One of :class:`WikiStatField`'s values.

        Returns:
            The composite average and vote count for that field.
        """
        agg = self.for_wiki(wiki).for_field(field).aggregate(avg=Avg("value"), count=Count("id"))
        count = agg["count"] or 0
        avg = agg["avg"]
        if avg is None:
            return WikiStatComposite(rounded=None, exact=None, count=0)
        return WikiStatComposite(rounded=round(avg), exact=round(avg, 1), count=count)

    def my_vote(self, wiki: Wiki, field: str, profile: Profile | None) -> int | None:
        """Return ``profile``'s own vote value for ``field`` on ``wiki``, if any."""
        if profile is None:
            return None
        vote = self.for_wiki(wiki).for_field(field).filter(profile=profile).first()
        return vote.value if vote else None


class WikiStatVoteManager(abstract.DashboardManager.from_queryset(WikiStatVoteQuerySet)):
    """Manager for WikiStatVote."""
