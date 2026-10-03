"""Rebuilds the gazetteer's bundled data from ``geonamescache``, a development dependency.

Run it after upgrading geonamescache: ``bun run gazetteer:build``.
"""

from __future__ import annotations

import json
import lzma

from urbanlens.dashboard.services.geo.gazetteer import CITIES_FILE, MIN_CITY_POPULATION, REGIONS_FILE

ATTRIBUTION = "GeoNames (https://www.geonames.org/), CC BY 4.0, via geonamescache"


def city_lines() -> list[str]:
    """Every city of at least :data:`MIN_CITY_POPULATION` people as a tab-separated line: name, latitude, longitude, country code, admin1 code."""
    import geonamescache

    rows = geonamescache.GeonamesCache(min_city_population=MIN_CITY_POPULATION).get_cities().values()
    lines = (f"{row['name']}\t{float(row['latitude']):.3f}\t{float(row['longitude']):.3f}\t{row['countrycode']}\t{row.get('admin1code') or ''}" for row in rows)
    # Neighbours compress best side by side: sorted by country and region, the file is a fifth smaller.
    return sorted(lines, key=lambda line: (*line.split("\t")[3:], line))


def regions() -> dict[str, object]:
    """US states by postal code, US county names, and countries by ISO code."""
    import geonamescache

    source = geonamescache.GeonamesCache()
    return {
        "attribution": ATTRIBUTION,
        "us_states": {code: state["name"] for code, state in sorted(source.get_us_states().items())},
        "us_counties": sorted({county["name"] for county in source.get_us_counties()}),
        "countries": {code: country["name"] for code, country in sorted(source.get_countries().items())},
    }


def write() -> None:
    """Write both files, byte-for-byte reproducibly."""
    text = "\n".join([f"# {ATTRIBUTION}", *city_lines()]) + "\n"
    CITIES_FILE.write_bytes(lzma.compress(text.encode("utf-8"), preset=9 | lzma.PRESET_EXTREME))
    REGIONS_FILE.write_text(json.dumps(regions(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write()
