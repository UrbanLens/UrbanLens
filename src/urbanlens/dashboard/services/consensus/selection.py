"""Which wiki/field-kind pair becomes a session's next round.

Combines eligibility (``services.consensus.eligibility``), the field-kind
registry (``services.consensus.fields``), and trust-check injection
(``services.consensus.trust``) into one pick - mirrors
``services.spotguessr.session.get_or_create_round``'s location-selection
loop plus ``services.spotguessr.modes``'s per-mode dispatch, combined.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.consensus.model import ConsensusFieldKind
from urbanlens.dashboard.services.consensus import eligibility, fields, trust

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.consensus.fields import RoundContent

#: Probability of attempting a "recheck" round - reasking about a
#: Facts-tracked wiki field that's TENTATIVE/CONTESTED - instead of an
#: ordinary missing-field round. See ``services.facts.consumption.
#: get_facts_needing_confirmation``.
RECHECK_INJECT_PROBABILITY = 0.2

#: The four plain wiki-attribute field kinds Facts tracks confidence for -
#: mirrors ``services.facts.evidence.CONSENSUS_FIELD_KIND_TO_FACT_KEY`` minus
#: ``PHOTO_COORDINATES`` (Image-scoped, not part of this wiki-pool shim).
_RECHECK_FIELD_KINDS = (
    ConsensusFieldKind.WIKI_NAME,
    ConsensusFieldKind.WIKI_DESCRIPTION,
    ConsensusFieldKind.WIKI_INDOOR_OUTDOOR,
    ConsensusFieldKind.WIKI_PIN_TYPE,
)


@dataclass(frozen=True)
class RoundSelection:
    """Resolved content for the next round to create."""

    wiki: Wiki
    field_kind: str
    content: RoundContent
    is_check_round: bool
    #: The confirmed value, only for a check round - JSON-safe, snapshotted
    #: onto ``ConsensusRound.known_answer_snapshot`` by the caller.
    known_value: Any | None


def pick_next_round_content(profiles: Iterable[Profile], *, exclude_wiki_ids: Iterable[int] = ()) -> RoundSelection | None:
    """Pick the next round's wiki, field kind, and content for ``profiles``.

    Args:
        profiles: Every joined participant (a single profile for solo play).
        exclude_wiki_ids: Wikis already used earlier in this session.

    Returns:
        The resolved selection, or None if nothing eligible/usable remains
        (the caller should treat this as "no more rounds possible").
    """
    profiles = list(profiles)
    if not profiles:
        return None

    pool = list(
        eligibility.eligible_wikis(profiles[0], exclude_wiki_ids=exclude_wiki_ids) if len(profiles) == 1 else eligibility.eligible_wikis_for_all(profiles, exclude_wiki_ids=exclude_wiki_ids),
    )
    if not pool:
        return None

    wants_check = should_inject_check(profiles)
    if wants_check:
        selection = _pick_check_round(pool)
        if selection is not None:
            return selection
        # No wiki in the pool has any confirmed data to check against yet -
        # fall through to an ordinary round rather than skip this round entirely.

    if random.random() < RECHECK_INJECT_PROBABILITY:  # noqa: S311 - not a cryptographic/security use
        selection = _pick_recheck_round(pool)
        if selection is not None:
            return selection

    return _pick_normal_round(pool)


def should_inject_check(profiles: list[Profile]) -> bool:
    """Whether the next round for ``profiles`` should be a trust-check round."""
    if len(profiles) == 1:
        return trust.should_inject_check(profiles[0])
    return trust.should_inject_check_for_profiles(profiles)


def _pick_normal_round(pool: list[Wiki]) -> RoundSelection | None:
    candidates: list[tuple[Wiki, str, fields.ConsensusFieldStrategy]] = []
    for kind in fields.all_kinds():
        strategy = fields.get_strategy(kind)
        if strategy is None:
            continue
        candidates.extend((wiki, kind, strategy) for wiki in strategy.find_missing(pool))
    random.shuffle(candidates)

    for wiki, kind, strategy in candidates:
        content = strategy.build_round(wiki)
        if content is not None:
            return RoundSelection(wiki=wiki, field_kind=kind, content=content, is_check_round=False, known_value=None)
    return None


def _pick_recheck_round(pool: list[Wiki]) -> RoundSelection | None:
    """Pick a round re-asking about a wiki field Facts has flagged as TENTATIVE/CONTESTED.

    Additive to ordinary round selection - reuses the same
    ``ConsensusFieldStrategy.build_round``/``apply_answer`` machinery as a
    normal round for these four kinds, so a recheck round is
    indistinguishable from an ordinary one to the client (unlike a
    trust-check round, it's never disguised - it's a genuine round whose
    answer really does get applied).
    """
    from urbanlens.dashboard.models.facts.model import Fact, FactStatus

    wiki_ids = [wiki.pk for wiki in pool]
    if not wiki_ids:
        return None

    candidates = list(
        Fact.objects.filter(
            wiki_id__in=wiki_ids,
            key__in=_RECHECK_FIELD_KINDS,
            status__in=[FactStatus.TENTATIVE, FactStatus.CONTESTED],
        ).order_by("confidence"),
    )
    if not candidates:
        return None

    by_wiki = {wiki.pk: wiki for wiki in pool}
    random.shuffle(candidates)
    for fact in candidates:
        wiki = by_wiki.get(fact.wiki_id)
        if wiki is None:
            continue
        strategy = fields.get_strategy(fact.key)
        if strategy is None:
            continue
        content = strategy.build_round(wiki)
        if content is not None:
            return RoundSelection(wiki=wiki, field_kind=fact.key, content=content, is_check_round=False, known_value=None)
    return None


def _pick_check_round(pool: list[Wiki]) -> RoundSelection | None:
    candidates: list[tuple[Wiki, str, fields.ConsensusFieldStrategy]] = []
    for kind in fields.all_kinds():
        strategy = fields.get_strategy(kind)
        if strategy is None:
            continue
        candidates.extend((wiki, kind, strategy) for wiki in strategy.find_known(pool))
    random.shuffle(candidates)

    for wiki, kind, strategy in candidates:
        result = strategy.build_check_round(wiki)
        if result is not None:
            content, known_value = result
            return RoundSelection(wiki=wiki, field_kind=kind, content=content, is_check_round=True, known_value=known_value)
    return None
