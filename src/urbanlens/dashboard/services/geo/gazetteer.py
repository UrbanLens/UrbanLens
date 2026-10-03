"""Places known by name: cities of 15,000 or more people worldwide, US states and counties, and countries.

The data is GeoNames' (CC BY 4.0), bundled by ``geonamescache``. It is read once per process into a compact index.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache, lru_cache
import re
import unicodedata

from urbanlens.dashboard.services.geo.distance import haversine_km

MIN_CITY_POPULATION = 15000

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def fold_place_name(name: str) -> str:
    """A place name in the form names are compared in: accents dropped, casefolded, punctuation as single spaces.

    Args:
        name: A place name.

    Returns:
        The folded name.
    """
    decomposed = unicodedata.normalize("NFKD", name.replace("&", " and "))
    plain = "".join(char for char in decomposed if not unicodedata.combining(char)).casefold().replace("'", "").replace("’", "")
    return _NON_ALNUM.sub(" ", plain).strip()


@dataclass(frozen=True, slots=True)
class GazetteerCity:
    """One city.

    Attributes:
        name: Its GeoNames name.
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        country_code: ISO 3166-1 alpha-2 code.
        admin1: First-level division code; the postal code for a US state.
    """

    name: str
    latitude: float
    longitude: float
    country_code: str
    admin1: str

    def km_from(self, latitude: float, longitude: float) -> float:
        """Great-circle distance from a point, in kilometres."""
        return haversine_km(self.latitude, self.longitude, latitude, longitude)


@cache
def _cities() -> dict[str, tuple[GazetteerCity, ...]]:
    import geonamescache

    index: dict[str, list[GazetteerCity]] = {}
    for row in geonamescache.GeonamesCache(min_city_population=MIN_CITY_POPULATION).get_cities().values():
        city = GazetteerCity(str(row["name"]), float(row["latitude"]), float(row["longitude"]), str(row["countrycode"]), str(row.get("admin1code") or ""))
        if folded := fold_place_name(city.name):
            index.setdefault(folded, []).append(city)
    return {name: tuple(cities) for name, cities in index.items()}


def cities_named(folded_name: str) -> tuple[GazetteerCity, ...]:
    """Every city with this name.

    Args:
        folded_name: A name as :func:`fold_place_name` returns it.

    Returns:
        The cities, possibly none.
    """
    return _cities().get(folded_name, ())


@cache
def us_states() -> dict[str, str]:
    """US states (and DC) by folded name.

    Returns:
        Folded state name to its two-letter postal code.
    """
    import geonamescache

    return {fold_place_name(str(state["name"])): str(code) for code, state in geonamescache.GeonamesCache().get_us_states().items()}


@cache
def us_counties() -> frozenset[str]:
    """Every US county's folded name, e.g. ``"dutchess county"``."""
    import geonamescache

    return frozenset(fold_place_name(str(county["name"])) for county in geonamescache.GeonamesCache().get_us_counties())


@cache
def countries() -> dict[str, str]:
    """Countries by folded name.

    Returns:
        Folded country name to its ISO 3166-1 alpha-2 code.
    """
    import geonamescache

    return {fold_place_name(str(country["name"])): str(code) for code, country in geonamescache.GeonamesCache().get_countries().items()}


@cache
def country_names_by_code() -> dict[str, str]:
    """Country names by ISO 3166-1 alpha-2 code."""
    import geonamescache

    return {str(code): str(country["name"]) for code, country in geonamescache.GeonamesCache().get_countries().items()}


@lru_cache(maxsize=1024)
def nearest_city(latitude: float, longitude: float, *, within_km: float) -> GazetteerCity | None:
    """The city nearest a point, when one is close enough.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        within_km: How far away the city may be.

    Returns:
        The nearest city, or None when none is within ``within_km``.
    """
    best: GazetteerCity | None = None
    best_km = within_km
    for cities in _cities().values():
        for city in cities:
            # A degree of latitude is ~111 km; skip the trigonometry for anything clearly out of range.
            if abs(city.latitude - latitude) * 111.0 > best_km:
                continue
            km = city.km_from(latitude, longitude)
            if km <= best_km:
                best, best_km = city, km
    return best
