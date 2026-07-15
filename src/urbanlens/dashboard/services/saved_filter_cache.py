"""Backend cache for a saved filter's matching pin uuids.

Mirrors the ``community_counts.py`` pattern: a plain Redis-backed
``django.core.cache`` entry, not a DB table. The cache key embeds both the
profile's pins ``last_updated`` fingerprint (the same aggregate the
``map.pins.meta`` endpoint already computes) AND the saved filter's own
``updated`` timestamp, so an entry self-invalidates the moment either the
matching pins OR the filter's own criteria change - no manual invalidation
signal is needed, and a stale entry can never outlive the data it describes.
(The filter's own timestamp was missing here for a while: editing a saved
filter's criteria alone, with no pin edited in between, left old - sometimes
empty - results cached indefinitely, which is exactly why the map toolbar
could show 0 matches for a filter that a Lists page smart-list, which never
caches this and always recomputes fresh, correctly showed 400+ matches for.)

Security note: every function here takes a ``Profile`` and only ever queries
``Pin.objects.filter(profile=profile)`` / reads ``saved_filter.criteria`` for
a ``SavedFilter`` already scoped to that same profile by the caller. Nothing
here accepts a bare uuid and resolves it - callers (``controllers/maps.py``)
must resolve ``SavedFilter`` rows via ``SavedFilter.objects.filter(profile=profile,
uuid__in=...)`` first, so a fuzzed/foreign filter uuid simply matches nothing
and is silently dropped rather than ever touching another user's pins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.cache import cache
from django.db.models import Max

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.saved_filter.model import SavedFilter

_CACHE_TIMEOUT = 60 * 60 * 24  # 1 day - the last-updated fingerprints in the key are the real expiry
_CACHE_KEY_TEMPLATE = "saved_filter_pins:{profile_id}:{filter_uuid}:{filter_updated}:{fingerprint}"


def _pins_fingerprint(profile: Profile) -> str:
    from urbanlens.dashboard.models.pin import Pin

    result = Pin.objects.filter(profile=profile).root_pins().aggregate(last_updated=Max("updated"))
    last_updated = result["last_updated"]
    return last_updated.isoformat() if last_updated else "none"


def get_or_compute_matching_uuids(profile: Profile, saved_filter: SavedFilter) -> list[str]:
    """Return the profile's pin uuids matching ``saved_filter``, using a warm cache when possible.

    Args:
        profile: Owner of both the filter and the pins being matched -
            every query here is scoped to this profile, so this can never
            return or be primed with another user's pin data.
        saved_filter: A ``SavedFilter`` already verified to belong to ``profile``.

    Returns:
        List of pin uuid strings matching the filter's criteria.
    """
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.filter_criteria import deserialize_criteria

    key = _CACHE_KEY_TEMPLATE.format(
        profile_id=profile.pk,
        filter_uuid=saved_filter.uuid,
        filter_updated=saved_filter.updated.isoformat(),
        fingerprint=_pins_fingerprint(profile),
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
