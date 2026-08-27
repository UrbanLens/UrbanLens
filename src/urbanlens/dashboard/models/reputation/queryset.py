"""QuerySets and managers for the reputation ledger.

Scoping and aggregation only - what a contribution is *worth* is decided by the
rule registry in ``services.reputation``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Self

from django.db.models import Model, Sum

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.reputation.model import ProfileReputation, ReputationEvent
    from urbanlens.dashboard.models.wiki.model import Wiki


def _pk_of(value: Model | int) -> int:
    """Return a primary key from either a model instance or a bare pk.

    Signal handlers only ever hold ``instance.profile_id``, and fetching the
    whole row to scope a query would add a query to every contributing write.
    Mirrors ``services.achievements.activity._owner_filter``, but typed over
    ``Model`` rather than ``Profile`` because the ledger scopes by wiki too.
    """
    return value if isinstance(value, int) else value.pk


class ReputationEventQuerySet(abstract.DashboardQuerySet["ReputationEvent"]):
    """QuerySet for ReputationEvent - the append-only contribution ledger."""

    def for_profile(self, profile: Profile | int) -> Self:
        """Restrict to one profile's rows."""
        return self.filter(profile_id=_pk_of(profile))

    def counting(self) -> Self:
        """Rows that currently contribute to a total.

        Excludes retracted rows and rows the scorer has not valued yet.
        ``value`` is null between the synchronous write and the deferred
        scoring pass, so "unscored" and "worth nothing" are different states
        and must not be summed together.
        """
        return self.filter(retracted=False, value__isnull=False)

    def unscored(self) -> Self:
        """Rows written but not yet valued, oldest first.

        The nightly sweep drains these, which is what makes a lost Celery
        enqueue survivable - the contribution itself was never at risk,
        because the row is written inside the contributor's transaction.
        """
        return self.filter(value__isnull=True, retracted=False).order_by("occurred_at")

    def in_period(self, period_key: str) -> Self:
        """Restrict to one ``YYYY-MM`` bucket."""
        return self.filter(period_key=period_key)

    def for_rule(self, rule_key: str) -> Self:
        """Restrict to one rule's rows."""
        return self.filter(rule_key=rule_key)

    def for_wiki(self, wiki: Wiki | int) -> Self:
        """Restrict to rows earned against one wiki."""
        return self.filter(wiki_id=_pk_of(wiki))

    def total_value(self) -> Decimal:
        """Sum the value of the counting rows in this queryset."""
        return self.counting().aggregate(total=Sum("value"))["total"] or Decimal(0)

    def occurred_since(self, moment: datetime.datetime) -> Self:
        """Restrict to rows at or after *moment*."""
        return self.filter(occurred_at__gte=moment)


class ReputationEventManager(abstract.DashboardManager.from_queryset(ReputationEventQuerySet)):
    """Manager for ReputationEvent."""


class ProfileReputationQuerySet(abstract.DashboardQuerySet["ProfileReputation"]):
    """QuerySet for the denormalised per-profile totals."""

    def for_profile(self, profile: Profile | int) -> Self:
        """Restrict to one profile's row."""
        return self.filter(profile_id=_pk_of(profile))

    def stale(self) -> Self:
        """Rows whose cached total is known to lag the ledger."""
        return self.filter(is_stale=True)


class ProfileReputationManager(abstract.DashboardManager.from_queryset(ProfileReputationQuerySet)):
    """Manager for ProfileReputation."""
