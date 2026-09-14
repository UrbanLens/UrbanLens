"""Backend cache for a saved filter's matching pin uuids."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings

from urbanlens.dashboard.services.core import bounded_cache

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.saved_filter.model import SavedFilter

logger = logging.getLogger(__name__)

_CACHE_TIMEOUT = 60 * 60 * 24  # 1 day - the last-updated fingerprints in the key are the real expiry
_CACHE_KEY_TEMPLATE = "saved_filter_pins:{profile_id}:{filter_uuid}:{filter_updated}:{fingerprint}"

#: Names the entry currently live for this (profile, filter), so writing a new
#: one can drop the one it replaces. A fingerprinted key self-invalidates - the
#: next read cannot find it - but it does not self-clean: it holds its bytes for
#: the whole TTL in the instance that also holds sessions.
_CURRENT_KEY_TEMPLATE = "saved_filter_pins_current:{profile_id}:{filter_uuid}"


def pins_fingerprint(profile: Profile) -> str:
    """Fingerprint of the profile's root pins for cache-key self-invalidation.
    ``Max(updated)`` alone misses deletions - removing any pin other than the most-recently-updated one leaves the max unchanged, so deleted pins' uuids would keep matching from a warm cache entry until its TTL."""
    from urbanlens.dashboard.services.map_pins.fingerprint import pin_collection_state

    return pin_collection_state(profile).fingerprint


def get_or_compute_matching_uuids(profile: Profile, saved_filter: SavedFilter, *, fingerprint: str | None = None) -> list[str]:
    """Return the profile's pin uuids matching ``saved_filter``, using a warm cache when possible.

    Args:
        profile: Owner of both the filter and the pins being matched - every query here is scoped to this profile, so this can never return or be primed with another user's pin data.
        saved_filter: A ``SavedFilter`` already verified to belong to ``profile``.
        fingerprint: A pre-computed :func:`pins_fingerprint` result, for a caller resolving multiple filters for the same profile in one request.

    Returns:
        List of pin uuid strings matching the filter's criteria."""
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.search.filter_criteria import deserialize_criteria

    key = _CACHE_KEY_TEMPLATE.format(
        profile_id=profile.pk,
        filter_uuid=saved_filter.uuid,
        filter_updated=saved_filter.updated.isoformat(),
        fingerprint=fingerprint if fingerprint is not None else pins_fingerprint(profile),
    )
    label = f"Saved filter {saved_filter.uuid} matches"
    cached = bounded_cache.get_or_none(key, label=label)
    if cached is not None:
        return cached

    criteria = deserialize_criteria(saved_filter.criteria, profile)
    query = Pin.objects.filter(profile=profile).root_pins().filter_by_criteria(criteria)
    uuids = [str(u) for u in query.values_list("uuid", flat=True)]
    _store(profile, saved_filter, key, uuids, label=label)
    return uuids


def _store(profile: Profile, saved_filter: SavedFilter, key: str, uuids: list[str], *, label: str) -> None:
    """Cache *uuids* under *key* and drop whichever entry it supersedes.

    Args:
        profile: Owner of the filter.
        saved_filter: The filter whose matches these are.
        key: The fingerprinted key the caller resolved.
        uuids: The matching pin uuids.
        label: What is being stored, for the log lines.
    """
    ceiling = settings.SAVED_FILTER_MAX_CACHED_UUIDS
    if len(uuids) > ceiling:
        logger.warning("%s matched %d pins, over the %d cache ceiling; recomputing each time instead.", label, len(uuids), ceiling)
        return

    pointer = _CURRENT_KEY_TEMPLATE.format(profile_id=profile.pk, filter_uuid=saved_filter.uuid)
    superseded = bounded_cache.get_or_none(pointer, label=label)
    if not bounded_cache.set_or_skip(key, uuids, _CACHE_TIMEOUT, label=label):
        return
    bounded_cache.set_or_skip(pointer, key, _CACHE_TIMEOUT, label=f"{label} pointer")
    if superseded is not None and superseded != key:
        # Safe without a lock: a key names the exact pin state it describes, so
        # a key that is not this one describes state that has already moved.
        bounded_cache.delete_quietly(superseded, label=f"{label} (superseded)")


def warm_all_for_profile(profile: Profile) -> int:
    """Precompute and cache every one of a profile's saved filters.

    Args:
        profile: Whose saved filters to warm - called right after login so the first toolbar toggle of the session hits a warm cache.

    Returns:
        Number of saved filters warmed."""
    count = 0
    for saved_filter in profile.saved_filters.all():
        get_or_compute_matching_uuids(profile, saved_filter)
        count += 1
    return count
