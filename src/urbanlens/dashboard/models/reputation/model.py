"""The hidden reputation ledger.

Two models. :class:`ReputationEvent` is the append-only truth - one row per
contribution, carrying the inputs that produced its value so a score can be
explained later. :class:`ProfileReputation` is a cache of the sum, because the
consumers of this system read a total on the request path and summing a growing
ledger per read is exactly the cost this feature is required not to add.

Three properties are deliberate and worth not undoing:

**Rows are written synchronously; only their *value* is deferred.** The ledger
has no source of truth outside itself - unlike every achievement metric, which
is a count over other tables and can be recomputed at any time. A row written
by a Celery task would be lost whenever the broker is briefly unreachable,
because ``safely_enqueue_task`` swallows that and returns None. So the row is
inserted inside the contributor's own transaction (a rolled-back contribution
rolls its row back with it), with ``value`` null, and the scorer fills it in
afterwards. ``ReputationEventQuerySet.unscored`` is what the nightly sweep
drains.

**Value is a Decimal, not an integer.** The diminishing-returns curve is
fractional by design - a full point for the first comment in a month, half for
the second, a quarter for the third.

**Retraction is a flag, not a deletion or a negative row.** A wiki edit's
``reverted`` state is current state rather than history - reverting a revert
clears it - so retraction has to be re-applicable in both directions. A
compensating negative row could not be un-applied.

This score is never shown to the user. See ``docs/designs/reputation-and-gating.md``
for what it is for, and for why the gate that consumes it is still an open
design question.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
    CharField,
    DateTimeField,
    DecimalField,
    ForeignKey,
    Index,
    IntegerField,
    JSONField,
    OneToOneField,
    UniqueConstraint,
)
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.reputation.meta import TargetKind
from urbanlens.dashboard.models.reputation.queryset import ProfileReputationManager, ReputationEventManager


def rule_choices() -> list[tuple[str, str]]:
    """Return the registered rule keys, for ``rule_key``'s choices.

    A callable rather than a list, matching ``Achievement.metric``: the
    registry is populated at import time by whatever rules are installed, so
    passing the function means registering a new rule never generates a
    migration.
    """
    from urbanlens.dashboard.services.reputation.rules import rule_choices as registry_choices

    return registry_choices()


class ReputationEvent(abstract.DashboardModel):
    """One scored contribution.

    Attributes:
        profile: Who earned it.
        rule_key: Which registered rule produced this row. Choices come from
            the rule registry at runtime.
        target_kind: What sort of thing the contribution was about.
        target_id: Primary key of that thing, or None for rules with no
            discrete target (tenure, for instance).
        wiki: The wiki the contribution landed on, when there was one. Stored
            alongside the generic target because the two things that need to
            group by it - per-wiki caps and the admin breakdown - would
            otherwise have to dereference target_kind/target_id per row.
        value: What the contribution was worth. **Null until the scorer has
            run**; null and zero are different states.
        inputs: The snapshot the scorer worked from - the target's state at the
            moment the contribution arrived. Kept because a score nobody can
            explain cannot be tuned, and because the target's state will have
            moved on by the time anyone asks.
        occurred_at: When the contribution happened. Set explicitly rather than
            auto_now_add so a backfill can attribute a row to the past.
        period_key: The ``YYYY-MM`` bucket, denormalised so per-period caps and
            decay are an indexed equality filter.
        retracted: Whether this row currently counts. Reversible.
        retracted_reason: Why, for the audit trail.
    """

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="reputation_events")
    rule_key = CharField(max_length=64, choices=rule_choices, db_index=True)
    target_kind = CharField(max_length=32, choices=TargetKind.choices, default=TargetKind.NONE)
    target_id = IntegerField(null=True, blank=True)
    wiki = ForeignKey("dashboard.Wiki", on_delete=SET_NULL, null=True, blank=True, related_name="reputation_events")
    value = DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    inputs = JSONField(default=dict, blank=True)
    occurred_at = DateTimeField(default=timezone.now, db_index=True)
    period_key = CharField(max_length=7, db_index=True)
    retracted = BooleanField(default=False)
    retracted_reason = CharField(max_length=64, blank=True, default="")

    objects = ReputationEventManager()

    if TYPE_CHECKING:
        profile_id: int
        wiki_id: int | None

    def __str__(self) -> str:
        return f"{self.rule_key} for profile {self.profile_id} ({self.value if self.value is not None else 'unscored'})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_reputation_events"
        ordering = ["-occurred_at"]
        get_latest_by = "occurred_at"
        constraints = [
            # The idempotency key. Celery runs with acks_late and retries on
            # OSError, so any task that writes a row can run twice; and a
            # signal can fire again on a re-save. One row per (rule, target)
            # makes a repeat a no-op at the database rather than something
            # each caller has to remember to check.
            #
            # Rules with no discrete target (tenure, activity streaks) pass
            # target_id=None, which Postgres treats as distinct in a unique
            # index - those rules carry the period in their rule_key's target
            # instead. See services.reputation.scoring.record_event.
            UniqueConstraint(fields=["rule_key", "target_kind", "target_id"], name="uniq_reputation_event_target"),
        ]
        indexes = [
            Index(fields=["profile", "-occurred_at"], name="idxdb_repev_profile_date"),
            Index(fields=["profile", "rule_key", "period_key"], name="idxdb_repev_pf_rule_per"),
            Index(fields=["profile", "wiki"], name="idxdb_repev_profile_wiki"),
        ]


class ProfileReputation(abstract.DashboardModel):
    """The cached sum of one profile's ledger.

    Attributes:
        profile: The owner.
        total: Sum of the counting rows. Can fall when a contribution is
            retracted.
        lifetime_earned: Sum of everything ever awarded, ignoring retraction.
            Monotonic by construction, and deliberately separate: anything that
            grants durable standing must read this rather than ``total``, so
            that reverting somebody's edits cannot be used to take away access
            they already had. See R7 in the design doc.
        is_stale: Set when a ledger write happens, cleared by the recompute.
            Lets the nightly sweep find the profiles a lost enqueue stranded
            without scanning every row.
        computed_at: When the totals were last rebuilt from the ledger.
    """

    profile = OneToOneField("dashboard.Profile", on_delete=CASCADE, related_name="reputation")
    total = DecimalField(max_digits=14, decimal_places=4, default=Decimal(0))
    lifetime_earned = DecimalField(max_digits=14, decimal_places=4, default=Decimal(0))
    is_stale = BooleanField(default=False, db_index=True)
    computed_at = DateTimeField(null=True, blank=True)

    objects = ProfileReputationManager()

    if TYPE_CHECKING:
        profile_id: int

    def __str__(self) -> str:
        return f"Reputation {self.total} for profile {self.profile_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_profile_reputation"
        ordering = ["-total"]
