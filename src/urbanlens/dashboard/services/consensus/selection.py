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

from urbanlens.dashboard.services.consensus import eligibility, fields, trust

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.consensus.fields import RoundContent


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
        eligibility.eligible_wikis(profiles[0], exclude_wiki_ids=exclude_wiki_ids)
        if len(profiles) == 1
        else eligibility.eligible_wikis_for_all(profiles, exclude_wiki_ids=exclude_wiki_ids),
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
