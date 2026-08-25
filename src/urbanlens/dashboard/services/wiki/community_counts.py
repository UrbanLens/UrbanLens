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
from typing import TYPE_CHECKING, Any

from django.core.cache import cache

if TYPE_CHECKING:
    from datetime import date

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

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


def _first_of_month(value: date) -> date:
    """Truncate a date to the first day of its month.

    Args:
        value: The date to truncate.

    Returns:
        The same year and month with ``day`` set to 1.
    """
    return value.replace(day=1)


def wiki_community_summary(wiki: Wiki, location: Location) -> dict[str, Any]:
    """Summarize a wiki's community footprint without leaking who pinned it when.

    Counts only root pins (never detail pins) and only distinct profiles, then
    runs the total through :func:`approximate_pin_count`.

    ``first_pinned`` gets two protections that the count already had but the
    date did not:

    - It is truncated to the 1st of the month, because a day-precision "first
      pinned" is a timestamp of one identifiable person's activity.
    - It is suppressed entirely (``None``) whenever ``pin_count_low`` is true.
      With one or two pinners, "first pinned" *is* "when that specific person
      pinned it" - publishing it defeats the whole point of hiding the count.

    Args:
        wiki: The wiki being summarized (its pk keys the count's fuzz cache).
        location: The Location the caller resolved the wiki through - may be
            a different row than ``wiki.location`` when several Locations
            share the wiki's Place (``resolve_visible_wiki`` allows this so
            "everyone who pinned one property reaches the same page from
            their own slug").

    Returns:
        Dict with ``pin_count_low`` (bool), ``pin_count_approx`` (int, or None
        when low), ``first_pinned`` (``date`` truncated to the 1st, or None),
        and ``first_pinned_precision`` (always ``"month"``, so a client never
        renders the value as an exact day).
    """
    from urbanlens.dashboard.models.pin.model import Pin

    # Place-aware: count root pins across every Location sharing this wiki's
    # Place, not just the one Location the caller happened to resolve it
    # through - otherwise "N users have this pinned" undercounts (and varies
    # by which of the place's several pinned coordinates the URL names)
    # whenever more than one Location row exists under the Place. Falls back
    # to the single Location when it has no Place (see
    # services.pins.common_pins.pinned_place_keys, which this mirrors).
    if wiki.place_id is not None:
        root_pins = Pin.objects.filter(location__place_id=wiki.place_id, parent_pin__isnull=True)
    else:
        root_pins = location.pins.filter(parent_pin__isnull=True)
    exact_count = root_pins.values("profile").distinct().count()
    approximate = approximate_pin_count(wiki.pk, exact_count)
    is_low = bool(approximate["is_low"])

    first_pinned: date | None = None
    if not is_low:
        earliest = root_pins.order_by("created").values_list("created", flat=True).first()
        if earliest is not None:
            first_pinned = _first_of_month(earliest.date())

    return {
        "pin_count_low": is_low,
        "pin_count_approx": None if is_low else approximate["value"],
        "first_pinned": first_pinned,
        "first_pinned_precision": "month",
    }
