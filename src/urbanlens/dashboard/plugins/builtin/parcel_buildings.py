"""Parcel buildings plugin: every structure standing on a pin's property.

The counterpart to ``plugins.builtin.redata_building_attributes``, which
answers "what is *this* building?" - this one answers "what buildings are on
this property?", which is the only sensible question for a campus, a mill
complex, or an institutional site where one pin covers a hundred structures.

Two providers, in order:

1. **REData** (``RedataGateway.lookup_parcel_uuid`` → ``lookup_buildings``),
   which combines a county's own building-footprint layer with NY SHPO's CRIS
   inventory and is the only source that carries real building *numbers* and
   *names* - the "Building 154 / Tool Shed" identifiers a site's own signage
   and paperwork use.
2. **Overpass** (``OverpassGateway.buildings_within``) against the location's
   effective property boundary, for anywhere REData has no parcel coverage.
   OSM footprints have no building numbers, but a named-and-located list still
   beats nothing.

The cached list is what powers the "Buildings on this Property" panel on both
the pin detail page and the wiki, the "would you like to add pins for the
buildings here?" offer, and the child-pin classifier's fallback proximity test
(see ``services.locations.site_scope``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.external_data import LocationCachePanelSource
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.external_data import PanelSource

logger = logging.getLogger(__name__)

#: Human-readable labels for each provider's own reporting system, for the
#: per-row source chip. Mirrors redata_building_attributes._SOURCE_LABELS,
#: plus the OSM entry only this plugin's fallback can produce.
SOURCE_LABELS: dict[str, str] = {
    "county_gis": "County GIS",
    "cris": "NY SHPO (CRIS)",
    "osm": "OpenStreetMap",
}


def fetch_parcel_buildings(location: Location) -> dict[str, Any]:
    """Resolve every building on a location's parcel, REData first then Overpass.

    Failures are tolerated the same way ``redata_building_attributes`` and
    ``cris_buildings`` tolerate theirs (broad catch, cache an empty result):
    a missing building list is a low-stakes gap, and retrying it on every
    background enrichment cycle isn't worth the added complexity.

    Args:
        location: The location whose parcel to enumerate.

    Returns:
        ``{"buildings": [...], "provider": "redata"|"osm"}``, or ``{}`` when
        neither provider found anything.
    """
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

    latitude = float(location.latitude or 0)
    longitude = float(location.longitude or 0)

    try:
        gateway = RedataGateway()
        parcel_uuid = gateway.lookup_parcel_uuid(latitude, longitude)
        buildings = gateway.lookup_buildings(parcel_uuid) if parcel_uuid else []
    except (PropertyRecordsUnavailableError, ValueError):
        logger.debug("parcel_buildings: REData unavailable at %s,%s", latitude, longitude, exc_info=True)
        buildings = []

    if buildings:
        return {"buildings": list(buildings), "provider": "redata"}

    osm_buildings = _overpass_buildings(location)
    if osm_buildings:
        return {"buildings": osm_buildings, "provider": "osm"}
    return {}


def _overpass_buildings(location: Location) -> list[dict[str, Any]]:
    """OSM buildings inside the location's effective property boundary.

    Skipped entirely when the only "boundary" available is the synthesized
    default circle (see ``Boundary.effective_polygon``) - counting the
    buildings inside an arbitrary 50 m disc around a coordinate would report
    a neighbour's house as being on this parcel.

    Args:
        location: The location whose property boundary bounds the search.

    Returns:
        Building records, or ``[]`` when there's no real boundary to search
        inside or Overpass found nothing.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
    from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

    row = Boundary.objects.row_for_location(location, BoundaryType.PROPERTY)
    polygon = row.drawn_or_generated_polygon if row is not None else None
    if polygon is None:
        return []

    try:
        return OverpassGateway().buildings_within(polygon)
    except Exception:
        # Matches OverpassGateway's own callers (services.locations.boundaries):
        # every failure mode here - transient mirror outage, rate limit, a
        # malformed ring - is a missing list, never a broken page.
        logger.debug("parcel_buildings: Overpass lookup failed for location %s", location.pk, exc_info=True)
        return []


def match_child_marker(building: dict[str, Any], candidates: list) -> Any | None:
    """The child marker standing at a building, if one already does.

    Args:
        building: A cached building record with ``latitude``/``longitude``.
        candidates: Child markers (pins or wikis) not yet matched to a building.

    Returns:
        The nearest candidate within ``BUILDING_MATCH_METERS``, or None.
    """
    from urbanlens.dashboard.services.locations.site_scope import BUILDING_MATCH_METERS, meters_between

    lat, lng = building.get("latitude"), building.get("longitude")
    if lat is None or lng is None or not candidates:
        return None

    def distance(candidate) -> float:
        return meters_between(float(candidate.effective_latitude), float(candidate.effective_longitude), float(lat), float(lng))

    nearest = min(candidates, key=distance)
    return nearest if distance(nearest) <= BUILDING_MATCH_METERS else None


def building_rows(buildings: list[dict[str, Any]], children: list, url_for=None) -> list[dict[str, Any]]:
    """Pair each known building with the child marker that already covers it.

    Shared by the pin detail panel and the wiki's equivalent view so both
    render identically from the same cached data - the only difference being
    whether ``children`` are child pins or child wikis, which both expose the
    same ``effective_latitude``/``effective_longitude``/``pin_type`` surface.

    Args:
        buildings: Cached building records (see :func:`fetch_parcel_buildings`).
        children: The marker's direct children, to match against.
        url_for: Optional callable turning a matched child into a link target;
            omit for child wikis, which are markers on their parent's page
            rather than pages of their own.

    Returns:
        One row per building, sorted by building number then name, each with
        ``name``, ``building_number``, ``year_built``, ``source_label``,
        ``latitude``, ``longitude``, ``child_name``, and ``child_url`` - the
        last two empty when this building has no marker yet.
    """
    rows: list[dict[str, Any]] = []
    unmatched = list(children)
    for building in buildings:
        child = match_child_marker(building, unmatched)
        if child is not None:
            # One child can only stand for one building - on a dense campus
            # the same pin would otherwise claim several neighbouring
            # footprints and leave real ones looking unpinned.
            unmatched.remove(child)
        rows.append(
            {
                "name": building.get("name") or "",
                "building_number": building.get("building_number") or "",
                "year_built": building.get("year_built") or "",
                "source_label": SOURCE_LABELS.get(building.get("source") or "", ""),
                "latitude": building.get("latitude"),
                "longitude": building.get("longitude"),
                "child_name": _marker_name(child) if child is not None else "",
                "child_url": (url_for(child) if url_for is not None else "") if child is not None else "",
            },
        )

    return sorted(rows, key=_row_sort_key)


def _marker_name(marker) -> str:
    """Display name of a child pin or child wiki (Wiki has no ``effective_name``)."""
    return getattr(marker, "effective_name", None) or getattr(marker, "name", "") or ""


def _row_sort_key(row: dict[str, Any]) -> tuple:
    """Sort buildings by number (numerically when they are numbers), then name.

    Building numbers on a campus are the identifiers people actually navigate
    by, and they are almost always numeric - so "Building 9" has to sort
    before "Building 10", which a plain string sort gets wrong. Anything
    non-numeric falls back to its own string, after all the numbered ones.
    """
    number = str(row.get("building_number") or "").strip()
    if number.isdigit():
        return (0, int(number), "")
    if number:
        return (1, 0, number.casefold())
    return (2, 0, str(row.get("name") or "").casefold())


class ParcelBuildingsPanelSource(LocationCachePanelSource):
    """Every building on the pin's parcel, for the "Buildings on this Property" panel.

    Rendered by ``PinController.parcel_buildings`` rather than the generic
    ``panel_info`` dispatch: each row links to (or offers to create) the child
    pin for that building, which is well past what
    ``_simple_info_panel.html``'s label/value grid can express.
    """

    key = PARCEL_BUILDINGS_CACHE_SOURCE
    cache_source = PARCEL_BUILDINGS_CACHE_SOURCE
    section_id = "parcel-buildings-section"
    icon = "apartment"
    title = "Buildings on this Property"

    def gate(self, pin: Pin) -> bool:
        """Only for a root pin with coordinates - a child pin has no sub-buildings."""
        if pin.parent_pin_id is not None:
            return False
        return bool(pin.effective_latitude and pin.effective_longitude)

    def fetch(self, pin: Pin) -> None:
        """Enumerate the parcel's buildings and cache them against the pin's location."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        location = pin.location
        payload = fetch_parcel_buildings(location)
        LocationCache.set(location, self.cache_source, payload, query_key=f"{float(location.latitude or 0):.5f},{float(location.longitude or 0):.5f}")


class ParcelBuildingsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the parcel buildings cache per Location - what the wiki page reads."""

    key: ClassVar[str] = PARCEL_BUILDINGS_CACHE_SOURCE
    verbose_name: ClassVar[str] = "Buildings on the parcel"
    cache_source: ClassVar[str] = PARCEL_BUILDINGS_CACHE_SOURCE
    service_keys: ClassVar[tuple[str, ...]] = ("redata_api", "overpass")
    calls_per_item: ClassVar[int] = 2

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Enumerate the location's parcel buildings and return them for caching."""
        payload = fetch_parcel_buildings(location)
        return payload, f"{float(location.latitude or 0):.5f},{float(location.longitude or 0):.5f}"


class ParcelBuildingsPlugin(UrbanLensPlugin):
    """Lists every building standing on a pinned property, via REData or OpenStreetMap."""

    name: ClassVar[str] = "parcel_buildings"
    verbose_name: ClassVar[str] = "Parcel Buildings"
    description: ClassVar[str] = (
        "Enumerates every building on a pin's parcel - names and building numbers from REData "
        "(county GIS building-footprint layers plus NY SHPO CRIS), falling back to OpenStreetMap "
        "footprints inside the property boundary. Powers the 'Buildings on this Property' panel, the "
        "offer to bulk-create child pins for a multi-building site, and automatic building "
        "classification of child pins."
    )
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() override - both providers' service keys
    # ("redata_api", "overpass") are already registered by
    # plugins.builtin.property_records and the boundary provider chain.

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the parcel buildings pin-detail panel."""
        return [ParcelBuildingsPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute background-fill of parcel buildings for every pinned/wiki'd Location."""
        return [ParcelBuildingsEnrichmentSource()]
