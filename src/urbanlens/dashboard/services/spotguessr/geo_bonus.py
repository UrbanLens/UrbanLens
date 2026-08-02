"""Country/state/city bonus points: a nominal reward for "in the right area," even off-target.

See docs/designs/spotguessr.md's Points section. Pure distance-based scoring
means a guess that nails the right city but the wrong street still often
reads as "basically zero" - these bonuses exist to reduce that feeling,
independent of (and added on top of) the distance curve in ``scoring``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from django.core.cache import cache

from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway
from urbanlens.dashboard.services.security.redact import redact_coordinate

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

COUNTRY_BONUS = 100
STATE_BONUS = 250
CITY_BONUS = 400

#: Nominatim is rate-limited to 1 call/minute app-wide (see
#: ``plugins.builtin.nominatim.NominatimPlugin.get_service_defaults``) - without
#: caching, any multiplayer round with more than one guess/minute would have
#: every guess but the first silently lose its bonus to a
#: ``RateLimitExceededError``. Country/state/city boundaries don't move, so a
#: long TTL is safe for a *genuine* "nothing found" result.
_REVERSE_GEOCODE_CACHE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
#: TTL for a lookup that *failed* (network error, timeout, or the same
#: rate limit this cache exists to work around) rather than genuinely
#: returning "no result". Using the 30-day TTL here would let one transient
#: failure or rate-limited call silently disable the bonus for an entire
#: ~111m cell for a month; a short TTL just means the next guess in that
#: cell retries soon instead.
_REVERSE_GEOCODE_ERROR_CACHE_TTL_SECONDS = 60
#: Decimal places to round guess coordinates to before keying the cache -
#: ~111m of latitude at the equator, coarse enough that guesses landing in
#: the same neighborhood share a cache entry, fine enough to rarely cross a
#: real city/state/country boundary.
_REVERSE_GEOCODE_COORD_PRECISION = 3

#: Distinguishes "never cached" from "cached, and the lookup found nothing" -
#: ``cache.get`` can't tell those apart with a plain ``None`` default, and a
#: failed/empty lookup is itself worth caching so it doesn't get retried
#: every guess in the same neighborhood.
_CACHE_MISS = object()


def _reverse_geocode_admin_cached(latitude: float, longitude: float) -> dict[str, str] | None:
    """Reverse-geocode admin lookup for a guess point, cached by rounded coordinates.

    See ``_REVERSE_GEOCODE_CACHE_TTL_SECONDS``'s docstring for why this cache
    exists - Nominatim's 1 call/minute app-wide limit otherwise makes this
    feature non-functional under real multiplayer load. A failed/rate-limited
    lookup is cached only briefly (``_REVERSE_GEOCODE_ERROR_CACHE_TTL_SECONDS``)
    rather than for the full 30 days, so it doesn't get confused with a
    genuine "nothing found" result - see that constant's docstring.

    Args:
        latitude: WGS-84 latitude of the guess point.
        longitude: WGS-84 longitude of the guess point.

    Returns:
        ``{"country": ..., "state": ..., "city": ...}``, or None if Nominatim
        had no result or the lookup failed - cached either way, but for very
        different durations.
    """
    key = f"spotguessr:geo_bonus:reverse:{round(latitude, _REVERSE_GEOCODE_COORD_PRECISION)},{round(longitude, _REVERSE_GEOCODE_COORD_PRECISION)}"
    cached = cache.get(key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    try:
        admin = NominatimGateway().reverse_geocode_admin(latitude, longitude)
    except Exception:
        logger.exception("Nominatim admin reverse geocode failed for %s,%s", redact_coordinate(latitude), redact_coordinate(longitude))
        cache.set(key, None, _REVERSE_GEOCODE_ERROR_CACHE_TTL_SECONDS)
        return None

    cache.set(key, admin, _REVERSE_GEOCODE_CACHE_TTL_SECONDS)
    return admin


@dataclass(frozen=True)
class BonusScope:
    """Which admin-level bonus tiers are worth offering for a session.

    A tier is only offered when the eligible-location pool actually varies
    on it - if every eligible location is in the same country (e.g. a
    ``geo_bounds``-restricted session, or simply a profile whose pins are
    all in one country), a "guessed the right country" bonus is free points
    for doing nothing, not a skill signal.
    """

    country: bool = False
    state: bool = False
    city: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"country": self.country, "state": self.state, "city": self.city}

    @classmethod
    def from_dict(cls, data: dict) -> BonusScope:
        return cls(country=bool(data.get("country")), state=bool(data.get("state")), city=bool(data.get("city")))


def bonus_scope_for(locations: QuerySet[Location]) -> BonusScope:
    """Which bonus tiers are meaningful for this eligible-location pool.

    Computed empirically from the actual pool (distinct non-empty values)
    rather than trying to reverse-map a ``geo_bounds`` polygon to real-world
    admin boundaries - this works identically whether the constraint came
    from ``geo_bounds``, ``require_visited_all``, or simply "this player
    only has pins in one city."
    """
    countries, states, cities = set(), set(), set()
    for country, state, city in locations.values_list("country", "administrative_area_level_1", "locality"):
        if country:
            countries.add(country.strip().casefold())
        if state:
            states.add(state.strip().casefold())
        if city:
            cities.add(city.strip().casefold())
    return BonusScope(country=len(countries) > 1, state=len(states) > 1, city=len(cities) > 1)


@dataclass(frozen=True)
class BonusResult:
    total: int
    matched_tiers: list[str] = field(default_factory=list)


def _normalize(value: str | None) -> str:
    return (value or "").strip().casefold()


def bonus_points_for_guess(guess_point: Point, location: Location, scope: BonusScope) -> BonusResult:
    """Country/state/city bonus points for a guess, honoring ``scope``.

    Reverse-geocodes ``guess_point`` (one Nominatim call, cached by rounded
    coordinates - see ``_reverse_geocode_admin_cached`` - since this is the
    same rate-limited dependency this feature's own area-search already
    uses) and compares against the answer location's own stored
    ``country``/``state``/``city``. Tiers stack: nailing the city also means
    the country and state matched, so a spot-on guess earns all three. Skips
    the call entirely if ``scope`` offers no tiers this session (nothing to
    charge an API call for).
    """
    if not (scope.country or scope.state or scope.city):
        return BonusResult(total=0)

    admin = _reverse_geocode_admin_cached(guess_point.y, guess_point.x)
    if admin is None:
        return BonusResult(total=0)

    total = 0
    matched: list[str] = []
    if scope.country and _normalize(admin["country"]) == _normalize(location.country):
        total += COUNTRY_BONUS
        matched.append("country")
    if scope.state and _normalize(admin["state"]) == _normalize(location.state):
        total += STATE_BONUS
        matched.append("state")
    if scope.city and _normalize(admin["city"]) == _normalize(location.city):
        total += CITY_BONUS
        matched.append("city")
    return BonusResult(total=total, matched_tiers=matched)
