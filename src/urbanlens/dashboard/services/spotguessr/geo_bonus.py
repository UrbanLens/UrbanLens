"""Country/state/city bonus points: a nominal reward for "in the right area," even off-target.

See docs/designs/spotguessr.md's Points section. Pure distance-based scoring
means a guess that nails the right city but the wrong street still often
reads as "basically zero" - these bonuses exist to reduce that feeling,
independent of (and added on top of) the distance curve in ``scoring``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.location.model import Location

COUNTRY_BONUS = 100
STATE_BONUS = 250
CITY_BONUS = 400


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

    Reverse-geocodes ``guess_point`` (one Nominatim call - the same
    dependency this feature's own area-search already uses) and compares
    against the answer location's own stored ``country``/``state``/``city``.
    Tiers stack: nailing the city also means the country and state matched,
    so a spot-on guess earns all three. Skips the call entirely if ``scope``
    offers no tiers this session (nothing to charge an API call for).
    """
    if not (scope.country or scope.state or scope.city):
        return BonusResult(total=0)

    admin = NominatimGateway().reverse_geocode_admin(guess_point.y, guess_point.x)
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
