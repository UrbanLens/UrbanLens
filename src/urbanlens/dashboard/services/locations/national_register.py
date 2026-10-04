"""National Register of Historic Places listings: NPS's record of one, and which listings are a building's own.

A listing's boundary can hold a whole campus while it lists one building there, so a building is the listing's only
when a record says so: the listing's own point stands on it, or CRIS's record of the building calls it listed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

#: REData's provider tag for NPS's National Register.
NATIONAL_REGISTER_PROVIDER = "nps_nrhp"

#: NPS's own page for one listing. A number it has no listing for still answers 200, with an empty page, so only a
#: number NPS's own layer published (REData's ``external_id``) is ever linked.
NPS_RECORD_URL = "https://npgallery.nps.gov/AssetDetail/NRIS/{reference}"

#: NRIS reference numbers: eight digits, nine for listings since 2016.
_REFERENCE_NUMBER = re.compile(r"[0-9]{8,9}")

_HISTORIC_REGISTERS_CACHE_SOURCE = "redata_historic_registers"
_CRIS_CACHE_SOURCE = "cris_building_usn"
_CRIS_LISTED = "listed"
_CONTAINS_POINT_KEY = "contains_point"
_SITE_SCOPE = "site"


def nps_record_url(reference: str | None) -> str | None:
    """NPS's page for a National Register reference number.

    Args:
        reference: An NRIS reference number.

    Returns:
        The URL, or None for anything that is not a reference number (NYSHPO's own ``94NR00622`` among them).
    """
    if not isinstance(reference, str) or not _REFERENCE_NUMBER.fullmatch(reference):
        return None
    return NPS_RECORD_URL.format(reference=reference)


def reference_number(row: dict[str, Any]) -> str | None:
    """The NRIS reference number of a cached National Register row.

    Args:
        row: A Historic Registers row.

    Returns:
        The number, or None for another register's row or one without a valid number.
    """
    if row.get("provider") != NATIONAL_REGISTER_PROVIDER:
        return None
    value = str(row.get("external_id") or "").strip()
    return value if _REFERENCE_NUMBER.fullmatch(value) else None


def stands_on(location: Location, latitude: Any, longitude: Any, *, buildings: list[dict] | None = None) -> bool:
    """Whether a published point is on the building at ``location``.

    Args:
        location: A building's location.
        latitude: The point's latitude.
        longitude: The point's longitude.
        buildings: ``site_scope.site_buildings(location)``, when a caller testing many points already has it.

    Returns:
        True when the location's building outline holds the point or, without an outline, the point is within
        ``BUILDING_MATCH_METERS`` of the location and no other building known on the parcel is nearer to it.
    """
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.plugins.builtin.cris_buildings import building_footprint_of
    from urbanlens.dashboard.services.locations.site_scope import BUILDING_MATCH_METERS, meters_between

    if latitude is None or longitude is None or location.latitude is None or location.longitude is None:
        return False
    try:
        point_latitude, point_longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    footprint = building_footprint_of(location)
    if footprint is not None:
        return bool(footprint.contains(Point(point_longitude, point_latitude, srid=4326)))
    distance = meters_between(point_latitude, point_longitude, float(location.latitude), float(location.longitude))
    return distance <= BUILDING_MATCH_METERS and not _nearer_building(location, point_latitude, point_longitude, distance, buildings)


def _nearer_building(location: Location, latitude: float, longitude: float, distance: float, buildings: list[dict] | None = None) -> bool:
    """Whether another building known on the parcel is nearer the point than ``location`` is."""
    from urbanlens.dashboard.services.locations.site_scope import BUILDING_MATCH_METERS, meters_between, nearest_building, site_buildings

    if buildings is None:
        buildings = site_buildings(location)
    nearest = nearest_building(buildings, latitude, longitude)
    if nearest is None or nearest is nearest_building(buildings, float(location.latitude), float(location.longitude), within_meters=BUILDING_MATCH_METERS):
        return False
    return meters_between(latitude, longitude, float(nearest["latitude"]), float(nearest["longitude"])) < distance


def cris_lists_building(location: Location) -> bool:
    """Whether CRIS's own record of the building at ``location`` says it is listed.

    Args:
        location: A building's location.

    Returns:
        True when the cached CRIS building record is this building's (its point stands on it) and calls it listed.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    row = LocationCache.get_fresh(location, _CRIS_CACHE_SOURCE)
    data = row.data if row is not None and isinstance(row.data, dict) else {}
    if str(data.get("EligibilityDesc") or "").strip().lower() != _CRIS_LISTED:
        return False
    return stands_on(location, data.get("source_latitude"), data.get("source_longitude"))


def building_register_rows(location: Location, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The register rows that are one building's own record, for a building standing among others on a site.

    Args:
        location: The building's location.
        rows: Its cached Historic Registers rows.

    Returns:
        Structure rows whose own point stands on the building (marked as holding it), and, when CRIS's record of the
        building calls it listed, the listings whose boundary holds it. A site-level row's point is somewhere on the
        whole site, so it never picks one building.
    """
    listed = None
    own: list[dict[str, Any]] = []
    for row in rows:
        if row.get("scope") != _SITE_SCOPE and stands_on(location, row.get("source_latitude"), row.get("source_longitude")):
            own.append({**row, _CONTAINS_POINT_KEY: True})
            continue
        if row.get(_CONTAINS_POINT_KEY) is not True:
            continue
        if listed is None:
            listed = cris_lists_building(location)
        if listed:
            own.append(row)
    return own


def containing_listings(location: Location, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The register rows describing the place at ``location`` rather than a neighbour.

    Args:
        location: The pin's location.
        rows: Its cached Historic Registers rows.

    Returns:
        Rows whose boundary holds the location or whose own point stands on it.
    """
    return [row for row in rows if row.get(_CONTAINS_POINT_KEY) is True or stands_on(location, row.get("source_latitude"), row.get("source_longitude"))]


def cached_register_rows(location: Location) -> list[dict[str, Any]]:
    """The location's fresh cached Historic Registers rows."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    row = LocationCache.get_fresh(location, _HISTORIC_REGISTERS_CACHE_SOURCE)
    resources = row.data.get("resources") if row is not None and isinstance(row.data, dict) else None
    return [resource for resource in resources or [] if isinstance(resource, dict)]


def _comparable(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", name.casefold()))


def reference_named(location: Location, name: str) -> str | None:
    """The reference number of the National Register listing cached at ``location`` under exactly this name.

    Args:
        location: Where the Historic Registers rows are cached.
        name: A listing's name, as another register (CRIS) records it.

    Returns:
        The NRIS reference number, or None when no National Register row there has that name.
    """
    wanted = _comparable(name)
    if not wanted:
        return None
    return next((reference for row in cached_register_rows(location) if (reference := reference_number(row)) and _comparable(str(row.get("name") or "")) == wanted), None)


def link_listings(pin: Pin, rows: list[dict[str, Any]]) -> None:
    """Add NPS's record of each National Register listing in ``rows`` to the pin's links and its wiki's.

    Args:
        pin: The pin the listings describe.
        rows: Register rows already known to be this pin's own.
    """
    from urbanlens.dashboard.models.links.model import AutoLinkSource
    from urbanlens.dashboard.services.locations.external_links import add_pin_and_wiki_link

    for row in rows:
        reference = reference_number(row)
        url = nps_record_url(reference)
        if url is not None:
            add_pin_and_wiki_link(pin, pin.location, url, f"National Register #{reference}", source=AutoLinkSource.NATIONAL_REGISTER)
