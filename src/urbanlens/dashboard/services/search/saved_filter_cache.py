"""Backend cache for a saved filter's matching pin uuids.
The cache key embeds both a fingerprint of the profile's pins (``Max(updated)`` plus the pin count, so edits, creates, AND deletes all change it) AND the saved filter's own ``updated`` timestamp, so an entry self-invalidates the moment either the matching pins OR the filter's own criteria change - no manual invalidation signal is needed, and a stale entry can never outlive the data it describes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.cache import cache

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.saved_filter.model import SavedFilter

_CACHE_TIMEOUT = 60 * 60 * 24  # 1 day - the last-updated fingerprints in the key are the real expiry
_CACHE_KEY_TEMPLATE = "saved_filter_pins:{profile_id}:{filter_uuid}:{filter_updated}:{fingerprint}"


def pins_fingerprint(profile: Profile) -> str:
    """Fingerprint of the profile's root pins for cache-key self-invalidation.
    ``Max(updated)`` alone misses deletions - removing any pin other than the most-recently-updated one leaves the max unchanged, so deleted pins' uuids would keep matching from a warm cache entry until its TTL."""
    from urbanlens.dashboard.services.map_pins.fingerprint import pin_collection_state

    return pin_collection_state(profile).fingerprint


def get_or_compute_matching_uuids(profile: Profile, saved_filter: SavedFilter, *, fingerprint: str | None = None) -> list[str]:
    """Return the profile's pin uuids matching ``saved_filter``, using a warm cache when possible.

    Args:
        profile: Owner of both the filter and the pins being matched -
            every query here is scoped to this profile, so this can never
            return or be primed with another user's pin data.
        saved_filter: A ``SavedFilter`` already verified to belong to ``profile``.
        fingerprint: A pre-computed :func:`pins_fingerprint` result, for a
            caller resolving multiple filters for the same profile in one
            request. Computed here when omitted, for single-filter callers.

    Returns:
        List of pin uuid strings matching the filter's criteria.
    """
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.search.filter_criteria import deserialize_criteria

    key = _CACHE_KEY_TEMPLATE.format(
        profile_id=profile.pk,
        filter_uuid=saved_filter.uuid,
        filter_updated=saved_filter.updated.isoformat(),
        fingerprint=fingerprint if fingerprint is not None else pins_fingerprint(profile),
    )
    cached = cache.get(key)
    if cached is not None:
        return cached

    criteria = deserialize_criteria(saved_filter.criteria, profile)
    query = Pin.objects.filter(profile=profile).root_pins().filter_by_criteria(criteria)
    uuids = [str(u) for u in query.values_list("uuid", flat=True)]
    cache.set(key, uuids, _CACHE_TIMEOUT)
    return uuids


def warm_all_for_profile(profile: Profile) -> int:
    """Precompute and cache every one of a profile's saved filters.

    Args:
        profile: Whose saved filters to warm - called right after login so
            the first toolbar toggle of the session hits a warm cache.

    Returns:
        Number of saved filters warmed.
    """
    count = 0
    for saved_filter in profile.saved_filters.all():
        get_or_compute_matching_uuids(profile, saved_filter)
        count += 1
    return count
