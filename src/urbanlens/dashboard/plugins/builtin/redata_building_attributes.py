"""REData building-attributes plugin: standardized building name/number/year on pinned locations.
This is what makes a detail (child) pin - which has its own coordinates, distinct from its parent's, see ``controllers.detail_pins`` - resolve to *its own* building rather than whichever one happens to be first in REData's response."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.security.redact import redact_coordinate

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

_CACHE_SOURCE = "redata_building_attributes"


def _nearest_building(buildings: list[dict[str, Any]], latitude: float, longitude: float) -> dict[str, Any] | None:
    """Pick the building closest to a coordinate.
    The chosen building's name gets outright priority when naming a detail pin's location, so a wrong pick is user-visible.

    Args:
        buildings: Building records from :meth:`RedataGateway.lookup_buildings`.
        latitude: WGS-84 latitude of the query point.
        longitude: WGS-84 longitude of the query point.

    Returns:
        The nearest usable building record, or None when ``buildings`` is empty."""
    if not buildings:
        return None

    from urbanlens.dashboard.plugins.builtin.parcel_buildings import buildings_on_property, confident_buildings, countable_buildings

    on_property = buildings_on_property(buildings)
    leaves = countable_buildings(buildings)
    # Matched by identity: these helpers return the same dict objects, and a
    # record dict is neither hashable nor reliably unique by value.
    confident_ids = {id(record) for record in confident_buildings(buildings)}
    unambiguous = [record for record in leaves if id(record) in confident_ids]
    buildings = unambiguous or leaves or on_property or buildings

    from urbanlens.dashboard.services.locations.site_scope import meters_between

    def _distance(building: dict[str, Any]) -> float:
        lat = building.get("latitude")
        lng = building.get("longitude")
        if lat is None or lng is None:
            return float("inf")
        return meters_between(float(lat), float(lng), latitude, longitude)

    return min(buildings, key=_distance)


def _fetch_building_payload(latitude: float, longitude: float, *, location: Location | None = None) -> dict[str, Any]:
    """Resolve the parcel at a coordinate and return its nearest building's record.
    Only a cold cache falls through to fetching directly.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        location: The Location whose cached parcel-buildings list may already answer this, when the caller has one.

    Returns:
        The nearest ``BuildingRecord`` dict, or ``{}`` when REData has no parcel or no buildings at this coordinate.

    Raises:
        PropertyRecordsUnavailableError: REData could not answer for a transient reason.
        ValueError: REData is not configured."""
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import TRANSIENT_REASONS, PropertyRecordsUnavailableError, RedataGateway
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
    except PropertyRecordsUnavailableError as exc:
        if exc.reason in TRANSIENT_REASONS:
            raise
        # Every other reason is REData's settled answer about this coordinate
        # (no coverage, manual lookup only, nothing found) and is worth caching.
        logger.debug(
            "redata_building_attributes: no buildings near %s,%s (%s)",
            redact_coordinate(latitude),
            redact_coordinate(longitude),
            exc.reason,
        )
        return {}

    return _nearest_building(buildings, latitude, longitude) or {}


def _render_building_attributes(data: dict[str, Any]) -> dict[str, Any] | None:
    """Build the Building Attributes card context from a cached building payload.

    Args:
        data: A cached ``_fetch_building_payload`` result (``{}`` when nothing was found).

    Returns:
        A context dict for ``_simple_info_panel.html``, or None when the payload has none of the fields this card shows."""
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

    # A reconciled REData record names its sources in `sources[]`; the flat
    # top-level `source` it replaced is still what Overpass-shaped rows carry.
    from urbanlens.dashboard.plugins.builtin.parcel_buildings import record_sources, source_chips

    return {"heading_name": heading_name, "chips": source_chips(record_sources(data)), "meta": meta}


class RedataBuildingAttributesPanelSource(CoordinateGatedInfoPanelSource):
    """Standardized building number/name/year-built card on the Private Pin page, via REData."""

    key = "redata_building_attributes"
    cache_source = _CACHE_SOURCE
    section_id = "redata-building-attributes-section"
    icon = "domain"
    title = "Building Attributes"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self, pin: Pin) -> bool:
        """Also requires REData to be configured - this panel has no other data source.
        Without this the panel was scheduled for every US pin on an install with no REData, and its fetch cached an empty payload for each one."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Resolve the pin's nearest building and cache it, keyed by its own coordinates."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        payload = _fetch_building_payload(lat, lng, location=pin.location)
        LocationCache.set(pin.location, self.cache_source, payload, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Render the chosen building's attributes, or nothing (204).
        Those pins show the full building list instead (see ``plugins.builtin.parcel_buildings``)."""
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

    def gate(self) -> bool:
        """Requires REData to be configured - this source has no other backend.
        Without it the cycle picks candidates, every fetch raises, and the run logs one exception per location."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        return redata_configured()

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
