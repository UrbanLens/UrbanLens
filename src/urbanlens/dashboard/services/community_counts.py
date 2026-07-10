"""Privacy-preserving display of community wiki membership counts.

The exact number of users who have a place pinned is sensitive: showing it
lets someone place a pin and watch the count to learn whether (and when)
other users are interested in a location. Instead the UI shows:

- "fewer than 3" when under :data:`MIN_VISIBLE_PIN_COUNT` users have the
  place pinned, so a single new pin never reveals itself; and
- an approximate count ("about 7") above that, fuzzed by a few people and
  cached per wiki for a day so refreshing the page (or switching accounts)
  cannot be used to average out the noise or catch the moment it changes.
"""

from __future__ import annotations

import secrets

from django.core.cache import cache

# Below this many distinct users, no number is shown at all.
MIN_VISIBLE_PIN_COUNT = 3

# The fuzzed count differs from the exact count by at most this many people.
_FUZZ_SPREAD = 2

# How long one fuzzed value is served before re-rolling (seconds).
_FUZZ_CACHE_TIMEOUT = 60 * 60 * 24

_CACHE_KEY_TEMPLATE = "wiki_pin_count_fuzz:{wiki_id}"


def approximate_pin_count(wiki_id: int, exact_count: int) -> dict[str, object]:
    """Build the privacy-preserving display form of a wiki's pinned-user count.

    Args:
        wiki_id: Primary key of the wiki the count belongs to (cache key).
        exact_count: The exact number of distinct users with this place pinned.

    Returns:
        Dict with ``is_low`` (True when the count is under
        :data:`MIN_VISIBLE_PIN_COUNT` and no number should be shown) and
        ``value`` (the fuzzed count to display, or None when ``is_low``).
    """
    if exact_count < MIN_VISIBLE_PIN_COUNT:
        return {"is_low": True, "value": None}

    key = _CACHE_KEY_TEMPLATE.format(wiki_id=wiki_id)
    value = cache.get(key)
    if not isinstance(value, int):
        # secrets avoids the seedable module-level PRNG; the fuzz must not be
        # predictable or reproducible across processes.
        offset = secrets.randbelow(_FUZZ_SPREAD * 2 + 1) - _FUZZ_SPREAD
        value = max(MIN_VISIBLE_PIN_COUNT, exact_count + offset)
        cache.set(key, value, _FUZZ_CACHE_TIMEOUT)
    return {"is_low": False, "value": value}
