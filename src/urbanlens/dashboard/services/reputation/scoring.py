"""Writing to, valuing, and totalling the reputation ledger.

The split between the two halves is the important part:

``record_event`` runs **synchronously**, inside the contributing request's own
transaction. It is one indexed insert, it cannot be lost to a broker outage,
and a rolled-back contribution rolls its row back too.

``score_event`` runs **later**, off the request path. Working out how badly a
target needed a contribution means querying that target's state, and for photos
it can mean walking external gallery panels - by far the most expensive input in
the model, and exactly the cost this feature must not add to a page load.

That is why ``ReputationEvent.value`` is nullable: between the two halves a row
exists and is worth "not yet known", which is a different thing from worth
nothing.
"""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from urbanlens.dashboard.models.reputation.meta import TargetKind, period_key_for
from urbanlens.dashboard.services.reputation import coefficients
from urbanlens.dashboard.services.reputation.rules import get_rule, resolve_target, score_with

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.reputation.model import ReputationEvent
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


def record_event(
    profile: Profile | int,
    rule_key: str,
    *,
    target: Any | None = None,
    wiki: Wiki | int | None = None,
    occurred_at: datetime.datetime | None = None,
) -> ReputationEvent | None:
    """Write an unscored ledger row for a contribution that just happened.

    Idempotent: the unique constraint on ``(rule_key, target_kind, target_id)``
    means a signal firing twice, or a retried Celery task, produces one row.

    Args:
        profile: Who contributed.
        rule_key: A registered rule. An unknown key is logged and ignored
            rather than raising - a contribution must never fail because the
            bookkeeping around it is misconfigured.
        target: The object contributed, if the rule has one.
        wiki: The wiki it landed on, for per-wiki caps and the admin breakdown.
        occurred_at: When, defaulting to now. Explicit so a backfill can
            attribute a row to the past.

    Returns:
        The row, or None when nothing was written.
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent

    rule = get_rule(rule_key)
    if rule is None:
        logger.warning("record_event: no reputation rule registered as %s", rule_key)
        return None

    profile_id = profile if isinstance(profile, int) else profile.pk
    wiki_id = wiki if isinstance(wiki, int) or wiki is None else wiki.pk
    moment = occurred_at or timezone.now()

    target_id = getattr(target, "pk", None)
    # Fast path. Several subscriptions are not created_only - a photo is
    # usually attached to its wiki by a later "send to wiki", not at upload -
    # so ordinary re-saves re-enter here for a contribution already recorded.
    # Letting those reach the insert costs a savepoint and a rolled-back
    # IntegrityError on a write path users hit constantly. The constraint is
    # still the guarantee; this only avoids paying for it in the common case.
    if target_id is not None and ReputationEvent.objects.filter(rule_key=rule.key, target_kind=rule.target_kind, target_id=target_id).exists():
        return None

    try:
        with transaction.atomic():
            event = ReputationEvent.objects.create(
                profile_id=profile_id,
                rule_key=rule.key,
                target_kind=rule.target_kind,
                target_id=target_id,
                wiki_id=wiki_id,
                value=None,
                occurred_at=moment,
                period_key=period_key_for(timezone.localtime(moment)),
            )
    except IntegrityError:
        # Already recorded. The constraint is the idempotency mechanism, so
        # losing this race is the expected outcome rather than a problem.
        return None

    _mark_stale(profile_id)
    return event


def score_event(event: ReputationEvent) -> Decimal | None:
    """Work out what an unscored row was worth, and store it.

    Applies the rule, then the two central adjustments every rule obeys:
    diminishing returns within the period, and the per-rule and per-wiki
    ceilings. Rules do not implement those themselves, so none can forget to.

    Args:
        event: The row to value.

    Returns:
        The stored value, or None when the contribution did not qualify.
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent

    rule = get_rule(event.rule_key)
    if rule is None:
        logger.warning("score_event: no reputation rule registered as %s", event.rule_key)
        return None

    target = resolve_target(event) if event.target_kind != TargetKind.NONE else None
    if event.target_kind != TargetKind.NONE and event.target_id is not None and target is None:
        # The target was deleted before scoring reached it. Retract rather than
        # score zero, so the row reads as "withdrawn" instead of "worthless".
        retract_event(event, reason="target_deleted")
        return None

    result = score_with(rule, target)
    if result is None:
        retract_event(event, reason="did_not_qualify")
        return None

    value = result.value
    inputs = dict(result.inputs)

    if rule.decays:
        multiplier = _decay_multiplier(event)
        inputs["decay_multiplier"] = str(multiplier)
        value = value * multiplier

    if rule.capped:
        value, cap_note = _apply_caps(event, value)
        if cap_note:
            inputs["capped_by"] = cap_note

    value = value.quantize(Decimal("0.0001"))
    if value < coefficients.DECAY_FLOOR:
        value = Decimal(0)

    inputs["scored_at"] = timezone.now().isoformat()
    ReputationEvent.objects.filter(pk=event.pk).update(value=value, inputs=inputs)
    event.value = value
    event.inputs = inputs
    _mark_stale(event.profile_id)
    return value


def _decay_multiplier(event: ReputationEvent) -> Decimal:
    """Return the diminishing-returns factor for this row.

    A full point for the first contribution of a kind in the period, half for
    the second, a quarter for the third. Counts rows *before* this one so the
    factor does not depend on the order the scorer happens to reach them in.
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent

    earlier = (
        ReputationEvent.objects.for_profile(event.profile_id)
        .for_rule(event.rule_key)
        .in_period(event.period_key)
        .filter(retracted=False, occurred_at__lt=event.occurred_at)
        .count()
    )
    return coefficients.DECAY_RATIO**earlier


def _apply_caps(event: ReputationEvent, value: Decimal) -> tuple[Decimal, str]:
    """Trim *value* to whatever the period ceilings still allow.

    Returns:
        The allowed value, and a short note naming the binding cap (empty when
        neither bound).
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent

    same_rule = ReputationEvent.objects.for_profile(event.profile_id).for_rule(event.rule_key).in_period(event.period_key).exclude(pk=event.pk).total_value()
    rule_headroom = coefficients.PER_RULE_PERIOD_CAP - same_rule

    headroom = rule_headroom
    note = "per_rule" if value > rule_headroom else ""

    if event.wiki_id is not None:
        same_wiki = ReputationEvent.objects.for_profile(event.profile_id).for_wiki(event.wiki_id).in_period(event.period_key).exclude(pk=event.pk).total_value()
        wiki_headroom = coefficients.PER_WIKI_PERIOD_CAP - same_wiki
        if wiki_headroom < headroom:
            headroom = wiki_headroom
            note = "per_wiki" if value > wiki_headroom else note

    if headroom <= 0:
        return Decimal(0), note or "exhausted"
    return (min(value, headroom), note)


def retract_event(event: ReputationEvent, *, reason: str) -> bool:
    """Stop a row counting, reversibly.

    A wiki edit's ``reverted`` flag is current state rather than history -
    reverting a revert clears it - so retraction has to be undoable. That is
    why this sets a flag instead of deleting the row or writing a compensating
    negative one.

    Args:
        event: The row.
        reason: Short machine-readable label, stored for the audit trail.

    Returns:
        Whether this call changed anything.
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent

    updated = ReputationEvent.objects.filter(pk=event.pk, retracted=False).update(retracted=True, retracted_reason=reason)
    if not updated:
        return False
    event.retracted = True
    event.retracted_reason = reason
    _mark_stale(event.profile_id)
    return True


def restore_event(event: ReputationEvent) -> bool:
    """Undo a retraction - the revert-of-a-revert case.

    Args:
        event: The row.

    Returns:
        Whether this call changed anything.
    """
    from urbanlens.dashboard.models.reputation.model import ReputationEvent

    updated = ReputationEvent.objects.filter(pk=event.pk, retracted=True).update(retracted=False, retracted_reason="")
    if not updated:
        return False
    event.retracted = False
    event.retracted_reason = ""
    _mark_stale(event.profile_id)
    return True


def _mark_stale(profile_id: int) -> None:
    """Flag a profile's cached total as lagging the ledger.

    Cheap compare-and-swap rather than a lock: the transition is one-way until
    the recompute clears it, so a concurrent writer setting the same flag is a
    harmless no-op.
    """
    from urbanlens.dashboard.models.reputation.model import ProfileReputation

    if ProfileReputation.objects.filter(profile_id=profile_id, is_stale=False).update(is_stale=True):
        return
    # Either it was already stale, or the profile has no row yet - and the
    # second case is a brand-new contributor, whose very first event would
    # otherwise never be found by the sweep that looks for stale rows.
    ProfileReputation.objects.get_or_create(profile_id=profile_id, defaults={"is_stale": True})


def recompute_total(profile: Profile | int) -> Decimal:
    """Rebuild a profile's cached totals from the ledger.

    The ledger is truth and this is a cache, so the sum is recomputed rather
    than incremented - an incremental total drifts the first time a row is
    retracted, backfilled, or scored out of order.

    ``lifetime_earned`` deliberately ignores retraction: anything granting
    durable standing reads it, so that reverting somebody's contributions
    cannot be used to take away access they already had.

    Args:
        profile: Whose totals to rebuild.

    Returns:
        The new total.
    """
    from django.db.models import Sum

    from urbanlens.dashboard.models.reputation.model import ProfileReputation, ReputationEvent

    profile_id = profile if isinstance(profile, int) else profile.pk
    rows = ReputationEvent.objects.for_profile(profile_id)
    total = rows.total_value()
    lifetime = rows.filter(value__isnull=False).aggregate(total=Sum("value"))["total"] or Decimal(0)

    with transaction.atomic():
        record, _ = ProfileReputation.objects.select_for_update().get_or_create(profile_id=profile_id)
        record.total = total
        # Monotonic: a retraction lowers `total` but must never lower this.
        record.lifetime_earned = max(record.lifetime_earned, lifetime)
        record.is_stale = False
        record.computed_at = timezone.now()
        record.save(update_fields=["total", "lifetime_earned", "is_stale", "computed_at", "updated"])

    return total
