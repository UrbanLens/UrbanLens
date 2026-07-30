"""QuerySet/Manager for WikiStatVote, including composite (average) calculation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from django.db.models import Avg, Count

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.services.community_counts import approximate_pin_count

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote

# Ordinal position of each of the four community stat fields (mirrors
# WikiStatField in wiki_stat_vote/model.py, duplicated as plain strings here
# rather than imported, since model.py imports this module at load time and
# importing the enum back would be circular). Used only to build a synthetic
# per-field cache id for the fuzz-count reuse in composite() below.
_STAT_FIELD_ORDINALS = {"danger": 0, "vulnerability": 1, "priority": 2, "rating": 3}


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
            The composite average and a privacy-fuzzed vote count for that
            field. The raw vote count is never returned as-is: showing it
            exactly would let someone place a vote and watch the number tick
            to learn precisely when someone else votes - the same concern
            ``services.community_counts.approximate_pin_count`` fuzzes away
            for a wiki's pinned-user count, reused here unchanged.
        """
        agg = self.for_wiki(wiki).for_field(field).aggregate(avg=Avg("value"), count=Count("id"))
        count = agg["count"] or 0
        avg = agg["avg"]
        if avg is None:
            return WikiStatComposite(rounded=None, exact=None, count=0)

        # round-half-up so e.g. a 2.5 average displays as 3 filled stars,
        # matching normal user expectation - Python's builtin round() rounds
        # half-to-even instead (2.5 -> 2), which reads as a display bug here.
        rounded = int(Decimal(str(avg)).quantize(Decimal(1), rounding=ROUND_HALF_UP))

        # Reuse approximate_pin_count's fuzz-and-cache logic wholesale rather than
        # reimplementing it. It keys its cache purely off the id we pass in, so a
        # synthetic, guaranteed-negative id (real Wiki pks are always positive)
        # keeps this fuzz-cache entry from colliding with the wiki's own
        # pinned-user-count fuzz entry, or with another stat field's, for the
        # same wiki.
        field_ordinal = _STAT_FIELD_ORDINALS.get(field, len(_STAT_FIELD_ORDINALS))
        fuzz_id = -((wiki.pk * 100) + field_ordinal) - 1
        fuzzed = approximate_pin_count(fuzz_id, count)
        display_count = 0
        if not fuzzed["is_low"]:
            fuzzed_value = fuzzed["value"]
            if isinstance(fuzzed_value, int):
                display_count = fuzzed_value

        return WikiStatComposite(rounded=rounded, exact=round(avg, 1), count=display_count)

    def my_vote(self, wiki: Wiki, field: str, profile: Profile | None) -> int | None:
        """Return ``profile``'s own vote value for ``field`` on ``wiki``, if any."""
        if profile is None:
            return None
        vote = self.for_wiki(wiki).for_field(field).filter(profile=profile).first()
        return vote.value if vote else None

    def cast(self, wiki: Wiki, profile: Profile, field: str, value: int) -> WikiStatVote:
        """Record ``profile``'s vote of ``value`` on ``field`` for ``wiki``.

        One vote per ``(wiki, profile, field)``: re-voting replaces the
        previous value rather than adding a second ballot.

        Args:
            wiki: The wiki being voted on.
            profile: The voting profile.
            field: One of :class:`WikiStatField`'s values.
            value: The 1-5 rating. Callers validate the range - a value
                outside it should clear the vote via :meth:`clear`, not be
                stored.

        Returns:
            The created or updated vote.
        """
        vote, _created = self.update_or_create(wiki=wiki, profile=profile, field=field, defaults={"value": value})
        return vote

    def clear(self, wiki: Wiki, profile: Profile, field: str) -> bool:
        """Withdraw ``profile``'s vote on ``field`` for ``wiki``.

        Args:
            wiki: The wiki whose vote to withdraw.
            profile: The profile withdrawing their vote.
            field: One of :class:`WikiStatField`'s values.

        Returns:
            True when a vote was actually removed, False when there was none.
        """
        deleted_count, _per_model = self.for_wiki(wiki).for_field(field).filter(profile=profile).delete()
        return bool(deleted_count)


class WikiStatVoteManager(abstract.DashboardManager.from_queryset(WikiStatVoteQuerySet)):
    """Manager for WikiStatVote."""
