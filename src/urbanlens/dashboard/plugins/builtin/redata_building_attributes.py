"""REData building-attributes plugin: standardized building name/number/year on pinned locations.

Retrieval lives entirely in REData (the standalone service that already owns
property records for this app - see ``plugins.builtin.property_records``):
``RedataGateway.lookup_buildings`` returns every building REData knows about
for the parcel at a coordinate, combined across sources (a county's own
building-footprint layer, plus NY SHPO's CRIS inventory - see
``plugins.builtin.cris_buildings`` for CRIS's own richer USN Point panel).
This plugin surfaces the standardized ``building_number``/``name``/
``year_built`` fields REData normalizes across those sources into a small,
generic "Building Attributes" card - distinct from ``PropertyRecordsPanelSource``'s
parcel-level ``year_built``/``building_sqft`` and from CRIS's own NY-only USN
Point detail.

A parcel can have several buildings; :func:`_fetch_building_payload` always
picks the one nearest the queried coordinate. This is what makes a detail
(child) pin - which has its own coordinates, distinct from its parent's, see
``controllers.detail_pins`` - resolve to *its own* building rather than
whichever one happens to be first in REData's response.

The chosen building's name is also contributed as a :class:`NameProvider`
candidate (``source="redata_building"``), which
``services.locations.name_resolution.default_name_resolver`` gives outright
priority when naming a detail/child pin's location - see that module's
``override_source`` handling.
"""

from __future__ import annotations

import logging
from math import hypot
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.geo_boundary import USA
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider

logger = logging.getLogger(__name__)

_CACHE_SOURCE = "redata_building_attributes"

#: Human-readable labels for a source's own reporting system, for the panel's chip.
_SOURCE_LABELS: dict[str, str] = {
    "county_gis": "County GIS",
    "cris": "NY SHPO (CRIS)",
}


def _nearest_building(buildings: list[dict[str, Any]], latitude: float, longitude: float) -> dict[str, Any] | None:
    """Pick the building closest to a coordinate.

    Buildings returned by :meth:`RedataGateway.lookup_buildings` are already
    scoped to the single parcel matched at this coordinate, so a plain
    Euclidean comparison in degree-space is enough to pick "the" building for
    a query point without needing a real distance/threshold - a parcel with
    several buildings (e.g. a large complex) resolves to whichever one the
    queried coordinate is actually nearest to.

    Args:
        buildings: Building records from :meth:`RedataGateway.lookup_buildings`.
        latitude: WGS-84 latitude of the query point.
        longitude: WGS-84 longitude of the query point.

    Returns:
        The nearest building record, or None when ``buildings`` is empty.
    """
    if not buildings:
        return None

    def _distance(building: dict[str, Any]) -> float:
        lat = building.get("latitude")
        lng = building.get("longitude")
        if lat is None or lng is None:
            return float("inf")
        return hypot(float(lat) - latitude, float(lng) - longitude)

    return min(buildings, key=_distance)


def _fetch_building_payload(latitude: float, longitude: float, *, location: Location | None = None) -> dict[str, Any]:
    """Resolve the parcel at a coordinate and return its nearest building's record.

    Reuses ``plugins.builtin.parcel_buildings``' cached list for this parcel
    when one exists - that plugin performs the identical
    ``lookup_parcel_uuid``/``lookup_buildings`` pair, and on a campus the two
    running independently would double REData's per-pin cost for no new data.
    Only a cold cache falls through to fetching directly.

    Tolerates any failure to reach/resolve REData the same way
    ``plugins.builtin.loopnet``/``cris_buildings`` do (broad catch, cache an
    empty result) rather than ``property_records.py``'s stricter
    source-error passthrough - a missing building record is a much lower-
    stakes gap than a missing property record, so retrying on every
    background enrichment cycle isn't worth the added complexity.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        location: The Location whose cached parcel-buildings list may already
            answer this, when the caller has one.

    Returns:
        The nearest ``BuildingRecord`` dict, or ``{}`` when REData has no
        parcel or no buildings at this coordinate.
    """
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway
    from urbanlens.dashboard.services.locations.site_scope import parcel_buildings

    cached_buildings = parcel_buildings(location)
    if cached_buildings is not None:
        return _nearest_building(cached_buildings, latitude, longitude) or {}

    try:
        gateway = RedataGateway()
        parcel_uuid = gateway.lookup_parcel_uuid(latitude, longitude)
        if not parcel_uuid:
            return {}
        buildings = gateway.lookup_buildings(parcel_uuid)
    except (PropertyRecordsUnavailableError, ValueError):
        logger.debug("redata_building_attributes: no buildings available near %.2f,%.2f", latitude, longitude, exc_info=True)
        return {}

    return _nearest_building(buildings, latitude, longitude) or {}


def _render_building_attributes(data: dict[str, Any]) -> dict[str, Any] | None:
    """Build the Building Attributes card context from a cached building payload.

    Shared by the pin-detail panel and the wiki page's equivalent view, so
    both render identically from the same cached data.

    Args:
        data: A cached ``_fetch_building_payload`` result (``{}`` when
            nothing was found).

    Returns:
        A context dict for ``_simple_info_panel.html``, or None when the
        payload has none of the fields this card shows.
    """
    if not data:
        return None

    meta = []
    if data.get("building_number"):
        meta.append({"label": "Building Number", "value": data["building_number"]})
    if data.get("year_built"):
        meta.append({"label": "Year Built", "value": data["year_built"]})

    heading_name = data.get("name") or None
    if not heading_name and not meta:
        return None

    chips = [label] if (label := _SOURCE_LABELS.get(data.get("source") or "")) else []
    return {"heading_name": heading_name, "chips": chips, "meta": meta}


class RedataBuildingAttributesPanelSource(CoordinateGatedInfoPanelSource):
    """Standardized building number/name/year-built card on the pin detail page, via REData."""

    key = "redata_building_attributes"
    cache_source = _CACHE_SOURCE
    section_id = "redata-building-attributes-section"
    icon = "domain"
    title = "Building Attributes"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def fetch(self, pin: Pin) -> None:
        """Resolve the pin's nearest building and cache it, keyed by its own coordinates."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        payload = _fetch_building_payload(lat, lng, location=pin.location)
        LocationCache.set(pin.location, self.cache_source, payload, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Render the chosen building's attributes, or nothing (204).

        A parcel-scope pin gets nothing: the nearest building to a campus's
        own marker is one arbitrary structure out of dozens, and presenting
        its number and year-built as the property's own is exactly the
        confusion parcel scope exists to remove. Those pins show the full
        building list instead (see ``plugins.builtin.parcel_buildings``).
        """
        from urbanlens.dashboard.services.locations.site_scope import is_site_scope

        if is_site_scope(pin):
            return None
        return _render_building_attributes(data or {})


class RedataBuildingAttributesEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the Building Attributes cache per Location - what powers the wiki card."""

    key: ClassVar[str] = "redata_building_attributes"
    verbose_name: ClassVar[str] = "REData Building Attributes"
    cache_source: ClassVar[str] = _CACHE_SOURCE
    service_keys: ClassVar[tuple[str, ...]] = ("redata_api",)
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Resolve the location's nearest building and return it for caching."""
        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        payload = _fetch_building_payload(lat, lng, location=location)
        return payload, f"{lat:.5f},{lng:.5f}"


class RedataBuildingAttributesPlugin(UrbanLensPlugin):
    """Standardized building number/name/year-built data for pinned locations, via REData. USA only."""

    name: ClassVar[str] = "redata_building_attributes"
    verbose_name: ClassVar[str] = "REData Building Attributes"
    description: ClassVar[str] = (
        "Standardized building number, name, and year-built for the building nearest a pin's own coordinates, "
        "combined across REData's sources (county GIS building-footprint layers, NY SHPO CRIS). Distinct from the "
        "parcel-level details already shown in Property Records, and from CRIS's own richer NY-only Building USN "
        "Point card. The building name is also contributed as a name-provider candidate, prioritized above other "
        "sources when naming a detail (child) pin. USA only. Requires UL_REDATA_API_URL/UL_REDATA_API_KEY."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the pin-detail Building Attributes card."""
        return [RedataBuildingAttributesPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute background-fill of building attributes for every pinned/wiki'd Location."""
        return [RedataBuildingAttributesEnrichmentSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the REData building name as a place-name candidate."""
        return [LocationCacheNameProvider(source="redata_building", cache_source=_CACHE_SOURCE, keys=("name",), verbose_name="REData Building Records")]
