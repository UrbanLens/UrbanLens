"""Speculative round-content caching, so the *next* round is ready before a player reaches it.

``session.get_or_create_round`` builds a round's content (picking a location
and generating this mode's imagery/text for it) the moment it's first
needed - normally right as a player finishes the previous round, which puts
that work directly on the critical path of the request that's waiting on it
(most visibly Street View mode's live Google Maps lookup - see
``services.spotguessr.street_view`` - but even Photos/Named Place's now-cheap
DB lookups still cost something). The functions here let a background Celery
task (``tasks.prewarm_spotguessr_round``/``tasks.prewarm_spotguessr_solo_start``)
run that exact same selection ahead of time and stash the result under a
cache key ``get_or_create_round`` checks first - a hit skips straight to
``GameRound.objects.create()``; a miss (the task hasn't finished yet, lost a
race, or was never scheduled) simply falls back to live generation, so
correctness never depends on a prewarm having actually run.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from django.core.cache import cache

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.spotguessr.modes import RoundContent
    from urbanlens.dashboard.services.spotguessr.session import GameConfig

#: Long enough to cover a player's typical time spent on the preceding round
#: (or, for the solo-start prewarm, browsing the settings dialog before
#: clicking start); short enough that a never-consumed prewarm (an abandoned
#: session, or a player who changes a setting before starting) doesn't sit
#: around indefinitely.
PREWARM_TTL_SECONDS = 20 * 60

_PickedRound = tuple[int, "RoundContent"]


def _session_round_key(session_id: int, sequence_index: int) -> str:
    return f"spotguessr:round_prewarm:session:{session_id}:{sequence_index}"


def _solo_start_key(profile_id: int, mode: str, config: GameConfig) -> str:
    # Fingerprints the whole config, not just profile+mode - a speculative
    # prewarm computed for one difficulty/geo_bounds must never be served for
    # a different one. A mismatch is simply a cache miss, not a correctness
    # hazard: get_or_create_round falls back to live generation either way.
    fingerprint = hashlib.sha256(json.dumps(config.to_dict(), sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"spotguessr:round_prewarm:solo_start:{profile_id}:{mode}:{fingerprint}"


def store_for_session(session_id: int, sequence_index: int, location: Location, content: RoundContent) -> None:
    """Cache a pre-selected round for a session's given (not-yet-created) round index."""
    cache.set(_session_round_key(session_id, sequence_index), (location.pk, content), PREWARM_TTL_SECONDS)


def consume_for_session(session_id: int, sequence_index: int) -> _PickedRound | None:
    """Pop (get-then-delete) a session's prewarmed round, if one is cached.

    Args:
        session_id: The session whose round is about to be generated.
        sequence_index: The round's 0-based position within the session.

    Returns:
        ``(location_id, content)``, or None if nothing was prewarmed (or it
        already expired) - the caller should fall back to live generation.
    """
    key = _session_round_key(session_id, sequence_index)
    picked = cache.get(key)
    if picked is not None:
        cache.delete(key)
    return picked


def store_for_solo_start(profile_id: int, mode: str, config: GameConfig, location: Location, content: RoundContent) -> None:
    """Cache a pre-selected first round for a solo player's likely next ``/start/`` call."""
    cache.set(_solo_start_key(profile_id, mode, config), (location.pk, content), PREWARM_TTL_SECONDS)


def consume_for_solo_start(profile_id: int, mode: str, config: GameConfig) -> _PickedRound | None:
    """Pop a solo player's speculatively prewarmed first round, if the config matches exactly.

    Returns:
        ``(location_id, content)``, or None on a miss (never prewarmed,
        expired, or the config doesn't fingerprint-match what was prewarmed).
    """
    key = _solo_start_key(profile_id, mode, config)
    picked = cache.get(key)
    if picked is not None:
        cache.delete(key)
    return picked
