"""Recomputing a Fact's confidence from its accumulated evidence.

Dispatches per ``Fact.data_type``: NUMBER/POINT facts converge via a
trust-and-recency-weighted centroid (generalizing
``services.photos.photo_coordinates.recompute_estimated_coordinates``); every other
data type (TEXT/CHOICE/BOOL/DATE) converges via trust-weighted agreement
clustering with Bayesian-smoothed confidence, extending
``ConsensusProfile``'s Beta-Bernoulli trust pattern (see
``services.consensus.trust``) rather than inventing a new statistic.

Called async via ``tasks.recompute_fact_confidence`` after every new
``FactEvidence`` row (see ``services.facts.evidence.record_evidence``) -
never inline, per the project's Celery-everything-slow standard.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from urbanlens.dashboard.models.facts.model import Fact, FactDataType, FactStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.facts.model import FactEvidence

logger = logging.getLogger(__name__)

#: Below this many active evidence rows, a fact's value/status is left
#: alone - not enough signal to be worth caching or surfacing. Mirrors
#: ``services.photos.photo_coordinates.MIN_GUESSES_FOR_ESTIMATE``.
MIN_EVIDENCE_FOR_ESTIMATE = 5

#: confidence >= this promotes a fact to CONFIRMED.
CONFIRM_THRESHOLD = 0.75

#: When the leading and runner-up candidates' weighted shares are within this
#: margin, the fact is CONTESTED rather than merely TENTATIVE - the signal
#: Consensus's recheck-round selection (``services.consensus.selection``)
#: looks for.
CONTESTED_MARGIN = 0.15

#: Half-life, in days, of an evidence row's contribution to confidence -
#: an observation from a year ago counts for half as much as a fresh one.
EVIDENCE_HALF_LIFE_DAYS = 365.0

#: Weakly-informative Beta(2, 2) prior, mirroring
#: ``ConsensusProfile.DEFAULT_TRUST_ALPHA``/``DEFAULT_TRUST_BETA`` - a single
#: piece of evidence should never read as 100% confidence.
PRIOR_ALPHA = 2.0
PRIOR_BETA = 2.0

#: How close two NUMBER values must be to count as "agreeing" (e.g. built-year
#: evidence within a year of each other).
NUMBER_AGREEMENT_TOLERANCE = 1.0


@dataclass(frozen=True)
class _WeightedEvidence:
    """One evidence row's value, reduced to just what confidence math needs."""

    value: Any
    weight: float


def _decay(age_days: float) -> float:
    """Exponential-decay multiplier for an evidence row's age."""
    return 0.5 ** (age_days / EVIDENCE_HALF_LIFE_DAYS)


def _weigh(evidence: Sequence[FactEvidence]) -> list[_WeightedEvidence]:
    """Reduce each evidence row to its value and combined trust/reliability/recency weight."""
    now = timezone.now()
    weighted = []
    for row in evidence:
        age_days = max(0.0, (now - row.created).total_seconds() / 86400.0)
        trust = row.submitter_trust_snapshot if row.submitter_trust_snapshot is not None else 1.0
        weight = row.source_reliability * trust * _decay(age_days)
        weighted.append(_WeightedEvidence(value=row.get_value(), weight=weight))
    return weighted


def _aggregate_point(weighted: list[_WeightedEvidence]) -> tuple[Any, float]:
    """Weighted centroid + agreement-based confidence for POINT facts."""
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.services.consensus.fields import AGREEMENT_DISTANCE_METERS, haversine_distance_meters

    total_weight = sum(item.weight for item in weighted)
    if total_weight <= 0:
        return None, 0.0

    avg_lat = sum(item.value.y * item.weight for item in weighted) / total_weight
    avg_lng = sum(item.value.x * item.weight for item in weighted) / total_weight
    centroid = Point(avg_lng, avg_lat, srid=4326)

    agreeing_weight = sum(item.weight for item in weighted if haversine_distance_meters(item.value, centroid) <= AGREEMENT_DISTANCE_METERS)
    agreement_share = agreeing_weight / total_weight
    count_factor = min(1.0, len(weighted) / MIN_EVIDENCE_FOR_ESTIMATE)
    return centroid, agreement_share * count_factor


def _aggregate_number(weighted: list[_WeightedEvidence]) -> tuple[Any, float]:
    """Weighted mean + agreement-based confidence for NUMBER facts."""
    total_weight = sum(item.weight for item in weighted)
    if total_weight <= 0:
        return None, 0.0

    mean = sum(item.value * item.weight for item in weighted) / total_weight
    agreeing_weight = sum(item.weight for item in weighted if abs(item.value - mean) <= NUMBER_AGREEMENT_TOLERANCE)
    agreement_share = agreeing_weight / total_weight
    count_factor = min(1.0, len(weighted) / MIN_EVIDENCE_FOR_ESTIMATE)
    return mean, agreement_share * count_factor


def _values_agree(a: Any, b: Any) -> bool:
    """Whether two categorical (TEXT/CHOICE/BOOL/DATE) values count as the same answer."""
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _cluster_categorical(weighted: list[_WeightedEvidence]) -> tuple[list[tuple[float, Any]], float]:
    """Group categorical evidence into agreement clusters.

    Returns:
        ``(totals, total_weight)`` - ``totals`` is ``[(cluster_weight, value), ...]``
        sorted heaviest-first; ``total_weight`` is the sum of all of them.
    """
    clusters: list[list[_WeightedEvidence]] = []
    for item in weighted:
        cluster = next((cluster for cluster in clusters if _values_agree(cluster[0].value, item.value)), None)
        if cluster is None:
            clusters.append([item])
        else:
            cluster.append(item)

    totals = sorted(((sum(item.weight for item in cluster), cluster[0].value) for cluster in clusters), reverse=True)
    total_weight = sum(weight for weight, _value in totals)
    return totals, total_weight


def _confidence_for_weight(weight: float, total_weight: float) -> float:
    """Bayesian-smoothed confidence that a cluster of this weight is the true value."""
    return (weight + PRIOR_ALPHA) / (total_weight + PRIOR_ALPHA + PRIOR_BETA)


def _status_for(confidence: float, *, contested: bool) -> str:
    if contested:
        return FactStatus.CONTESTED
    if confidence >= CONFIRM_THRESHOLD:
        return FactStatus.CONFIRMED
    return FactStatus.TENTATIVE


def resolve_categorical(
    totals: list[tuple[float, Any]],
    total_weight: float,
    *,
    previous_value: Any,
    previously_confirmed: bool,
) -> tuple[Any, float, str]:
    """Decide the reported ``(value, confidence, status)`` for a categorical fact.

    A previously-``CONFIRMED`` value only changes if the new leading cluster
    both disagrees with it and itself clears ``CONFIRM_THRESHOLD`` - a
    confirmed value doesn't flip-flop on one noisy new observation. When the
    leading cluster is held back by that gate, confidence/status describe
    the *held* value's own standing, never the challenger's - so they always
    describe whatever value is actually being reported, not a value the
    fact isn't holding.

    Args:
        totals: Cluster weights from :func:`_cluster_categorical`, heaviest
            first.
        total_weight: Sum of every cluster's weight.
        previous_value: The fact's currently stored value.
        previously_confirmed: Whether the fact's status was already
            ``CONFIRMED`` going into this recomputation.

    Returns:
        ``(value, confidence, status)``.
    """
    if not totals:
        return None, 0.0, FactStatus.UNCONFIRMED

    top_weight, top_value = totals[0]
    contested = len(totals) > 1 and total_weight > 0 and (top_weight - totals[1][0]) / total_weight < CONTESTED_MARGIN

    if previously_confirmed and not _values_agree(top_value, previous_value):
        top_confidence = _confidence_for_weight(top_weight, total_weight)
        if top_confidence < CONFIRM_THRESHOLD:
            held_weight = next((weight for weight, value in totals if _values_agree(value, previous_value)), 0.0)
            confidence = _confidence_for_weight(held_weight, total_weight)
            return previous_value, confidence, _status_for(confidence, contested=False)

    confidence = _confidence_for_weight(top_weight, total_weight)
    return top_value, confidence, _status_for(confidence, contested=contested)


def recompute(fact_id: int) -> None:
    """Recompute one Fact's ``confidence``/``status``/value from its accumulated evidence.

    A no-op (beyond bumping ``evidence_count``/``last_evidence_at``) below
    ``MIN_EVIDENCE_FOR_ESTIMATE`` active evidence rows. For categorical facts
    (TEXT/CHOICE/BOOL/DATE), a previously-``CONFIRMED`` value is protected
    from flip-flopping by :func:`resolve_categorical`. NUMBER/POINT facts
    always recompute their centroid/mean fresh, mirroring
    ``services.photos.photo_coordinates.recompute_estimated_coordinates`` - a
    weighted average can't suddenly jump to a wildly different value from
    one new observation the way a discrete categorical winner can, so no
    equivalent gate is needed there.

    Args:
        fact_id: pk of the ``Fact`` to recompute.
    """
    try:
        fact = Fact.objects.get(pk=fact_id)
    except Fact.DoesNotExist:
        return

    evidence = list(fact.evidence.filter(superseded=False))
    fact.evidence_count = len(evidence)
    if evidence:
        fact.last_evidence_at = max(row.created for row in evidence)

    if len(evidence) < MIN_EVIDENCE_FOR_ESTIMATE:
        fact.last_recomputed_at = timezone.now()
        fact.save(update_fields=["evidence_count", "last_evidence_at", "last_recomputed_at", "updated"])
        return

    weighted = _weigh(evidence)
    if fact.data_type == FactDataType.POINT:
        winning_value, confidence = _aggregate_point(weighted)
        status = _status_for(confidence, contested=False)
    elif fact.data_type == FactDataType.NUMBER:
        winning_value, confidence = _aggregate_number(weighted)
        status = _status_for(confidence, contested=False)
    else:
        totals, total_weight = _cluster_categorical(weighted)
        winning_value, confidence, status = resolve_categorical(
            totals,
            total_weight,
            previous_value=fact.get_value(),
            previously_confirmed=fact.status == FactStatus.CONFIRMED,
        )

    if winning_value is not None and fact.get_value() != winning_value:
        fact.set_value(winning_value)

    fact.confidence = confidence
    fact.status = status
    fact.last_recomputed_at = timezone.now()
    fact.save()
