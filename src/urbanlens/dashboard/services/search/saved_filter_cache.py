"""Backend cache for a saved filter's matching pin uuids.

Mirrors the ``community_counts.py`` pattern: a plain Redis-backed
``django.core.cache`` entry, not a DB table. The cache key embeds both a
fingerprint of the profile's pins (``Max(updated)`` plus the pin count, so
edits, creates, AND deletes all change it) AND the saved filter's own
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

    ``Max(updated)`` alone misses deletions - removing any pin other than the
    most-recently-updated one leaves the max unchanged, so deleted pins'
    uuids would keep matching from a warm cache entry until its TTL. The pin
    count (same single aggregate query) catches that case; together they
    change on every create, edit, and delete.

    Public (not module-private) so a caller resolving several filters for the
    same profile in one request - ``_apply_toolbar_filters``,
    ``SavedFilterMatchCountsView`` - can compute this DB aggregate once and
    pass it to every :func:`get_or_compute_matching_uuids` call instead of
    each call re-running it: with N saved filters that was N redundant,
    identical queries on every toolbar toggle.
    """
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
