"""Places known by name: cities of 15,000 or more people worldwide, US states and counties, and countries.

The data is GeoNames' (CC BY 4.0), extracted from ``geonamescache`` by :mod:`.build_gazetteer` into two small files
here, because parsing the package's JSON cost each process a second and 60 MB. Read once per process.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache, lru_cache
import json
import lzma
from pathlib import Path
import re
from typing import Any
import unicodedata

from urbanlens.dashboard.services.geo.distance import haversine_km

MIN_CITY_POPULATION = 15000
CITIES_FILE = Path(__file__).with_name("data") / "geonames_cities.tsv.xz"
REGIONS_FILE = Path(__file__).with_name("data") / "geonames_regions.json"

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
    index: dict[str, list[GazetteerCity]] = {}
    with lzma.open(CITIES_FILE, "rt", encoding="utf-8") as lines:
        for line in lines:
            if line.startswith("#"):
                continue
            name, latitude, longitude, country_code, admin1 = line.rstrip("\n").split("\t")
            if folded := fold_place_name(name):
                index.setdefault(folded, []).append(GazetteerCity(name, float(latitude), float(longitude), country_code, admin1))
    return {name: tuple(cities) for name, cities in index.items()}


@cache
def _regions() -> dict[str, Any]:
    with REGIONS_FILE.open(encoding="utf-8") as file:
        regions: dict[str, Any] = json.load(file)
    return regions


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
    return {fold_place_name(str(name)): str(code) for code, name in _regions()["us_states"].items()}


@cache
def us_counties() -> frozenset[str]:
    """Every US county's folded name, e.g. ``"dutchess county"``."""
    return frozenset(fold_place_name(str(name)) for name in _regions()["us_counties"])


@cache
def countries() -> dict[str, str]:
    """Countries by folded name.

    Returns:
        Folded country name to its ISO 3166-1 alpha-2 code.
    """
    return {fold_place_name(str(name)): str(code) for code, name in _regions()["countries"].items()}


@cache
def country_names_by_code() -> dict[str, str]:
    """Country names by ISO 3166-1 alpha-2 code."""
    return {str(code): str(name) for code, name in _regions()["countries"].items()}


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
