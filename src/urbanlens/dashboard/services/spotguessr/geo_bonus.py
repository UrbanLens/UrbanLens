"""Country/state/city bonus points: a nominal reward for "in the right area," even off-target."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from django.core.cache import cache

from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.dashboard.services.security.redact import redact_coordinate

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

COUNTRY_BONUS = 100
STATE_BONUS = 250
CITY_BONUS = 400

#: Country/state/city boundaries don't move, so a long TTL is safe for a *genuine* "nothing found"
#: result.
_REVERSE_GEOCODE_CACHE_TTL_SECONDS = 60 * 60 * 24 * 30
#: TTL for a lookup that *failed* (network error, timeout, or the same rate limit this cache exists
#: to work around) rather than genuinely returning "no result".
_REVERSE_GEOCODE_ERROR_CACHE_TTL_SECONDS = 60
_REVERSE_GEOCODE_COORD_PRECISION = 3

#: Distinguishes "never cached" from "cached, and the lookup found nothing" - ``cache.get`` can't
#: tell those apart with a plain ``None`` default, and a failed/empty lookup is itself worth caching
#: so it doesn't get retried every guess in the same neighborhood.
_CACHE_MISS = object()


def _reverse_geocode_admin_cached(latitude: float, longitude: float) -> dict[str, str] | None:
    """Reverse-geocode admin lookup for a guess point, cached by rounded coordinates.

    Args:
        latitude: WGS-84 latitude of the guess point.
        longitude: WGS-84 longitude of the guess point.

    Returns:
        ``{"country": ..., "state": ..., "city": ...}``, or None if Nominatim had no result or the lookup failed - cached either way, but for very different durations."""
    key = f"spotguessr:geo_bonus:reverse:{round(latitude, _REVERSE_GEOCODE_COORD_PRECISION)},{round(longitude, _REVERSE_GEOCODE_COORD_PRECISION)}"
    cached = cache.get(key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    try:
        admin = NominatimGateway().reverse_geocode_admin(latitude, longitude)
    except RateLimitExceededError:
        # Expected, and the reason this cache exists: the limit is one call a minute app-wide, so
        # any multiplayer round with more than one uncached guess a minute hits it by design.
        # A traceback per occurrence would bury the genuine failures handled below.
        logger.debug("Nominatim admin reverse geocode rate-limited for %s,%s", redact_coordinate(latitude), redact_coordinate(longitude))
        cache.set(key, None, _REVERSE_GEOCODE_ERROR_CACHE_TTL_SECONDS)
        return None
    except Exception:
        logger.exception("Nominatim admin reverse geocode failed for %s,%s", redact_coordinate(latitude), redact_coordinate(longitude))
        cache.set(key, None, _REVERSE_GEOCODE_ERROR_CACHE_TTL_SECONDS)
        return None

    cache.set(key, admin, _REVERSE_GEOCODE_CACHE_TTL_SECONDS)
    return admin


@dataclass(frozen=True)
class BonusScope:
    """Which admin-level bonus tiers are worth offering for a session."""

    country: bool = False
    state: bool = False
    city: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"country": self.country, "state": self.state, "city": self.city}

    @classmethod
    def from_dict(cls, data: dict) -> BonusScope:
        return cls(country=bool(data.get("country")), state=bool(data.get("state")), city=bool(data.get("city")))


def bonus_scope_for(locations: QuerySet[Location]) -> BonusScope:
    """Which bonus tiers are meaningful for this eligible-location pool."""
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
    Tiers stack: nailing the city also means the country and state matched, so a spot-on guess earns all three."""
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
