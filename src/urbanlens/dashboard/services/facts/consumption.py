"""Read-side queries for consumers of Facts: AI writing agents and Consensus's recheck-round selection.

See ``services.facts.evidence`` for the write path and
``services.facts.confidence`` for how ``confidence``/``status`` are derived.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.facts.model import Fact, FactStatus

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

#: Default confidence floor for :func:`get_trusted_facts` - matches
#: ``services.facts.confidence.CONFIRM_THRESHOLD``, the same bar that
#: promotes a fact to CONFIRMED.
DEFAULT_MIN_CONFIDENCE = 0.75

#: Default cap for :func:`get_facts_needing_confirmation`.
DEFAULT_CONFIRMATION_LIMIT = 20


def get_trusted_facts(
    *,
    wiki: Wiki | None = None,
    location: Location | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Fact]:
    """Facts a consumer (e.g. an AI writing agent) should treat as trustworthy for this subject.

    Args:
        wiki: Restrict to this wiki's facts.
        location: Restrict to this location's facts.
        min_confidence: Minimum confidence to include.

    Returns:
        Matching facts, most confident first.
    """
    facts = Fact.objects.min_confidence(min_confidence)
    if wiki is not None:
        facts = facts.for_wiki(wiki)
    if location is not None:
        facts = facts.for_location(location)
    return list(facts.order_by("-confidence"))


def get_facts_needing_confirmation(
    *,
    subject_type: str | None = None,
    limit: int = DEFAULT_CONFIRMATION_LIMIT,
) -> list[Fact]:
    """Facts worth asking a player (or reviewer) to help confirm - contested first, then least confident.

    Used by Consensus's recheck-round selection
    (``services.consensus.selection._pick_recheck_round``).

    Args:
        subject_type: Restrict to one ``FactSubjectType``, or None for any.
        limit: Maximum rows to return.

    Returns:
        Matching facts, ordered contested-first, then ascending confidence.
    """
    facts = Fact.objects.filter(status__in=[FactStatus.TENTATIVE, FactStatus.CONTESTED])
    if subject_type is not None:
        facts = facts.filter(subject_type=subject_type)
    # "contested" < "tentative" alphabetically, so this ordering happens to
    # put CONTESTED rows first without needing a Case/When priority mapping.
    return list(facts.order_by("status", "confidence")[:limit])
