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
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

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

    Buildings returned by :meth:`RedataGateway.lookup_buildings` are already
    scoped to the single parcel matched at this coordinate, so no distance
    threshold is needed - a parcel with several buildings (e.g. a large
    complex) resolves to whichever one the queried coordinate is nearest to.

    Ranking is by *ground* distance, via the same equirectangular helper
    ``services.pins.pin_wiki_sync`` uses to match a marker to a building.
    Comparing raw degree deltas instead would over-weight east-west
    separation - a degree of longitude is only ``cos(latitude)`` as long as
    a degree of latitude - which inverts the ranking of any two buildings
    whose true distances differ by less than that factor (~1.36x at this
    app's latitudes). The chosen building's name gets outright priority when
    naming a detail pin's location, so a wrong pick is user-visible.

    Three kinds of record are excluded before ranking, because REData labels
    them and this card cannot honour them: one that is **off the property**
    (returned deliberately when a parcel sits inside a broad survey zone), one
    that is an **envelope over other records in the same list** (``child_refs`` -
    a campus block, whose number and year are not any single building's), and
    one REData flagged as an **unresolved overlap** (``overlap_refs`` - if it
    cannot say what the record is, neither can this card). `parcel_buildings`
    already has the predicates; this used to rank the raw list, so the nearest
    record won even when it was one of those.
    Each exclusion is a preference, not a hard filter: when nothing survives,
    the next-weakest set is ranked instead, so a parcel whose only record is
    ambiguous still shows what is known rather than going blank.

    Args:
        buildings: Building records from :meth:`RedataGateway.lookup_buildings`.
        latitude: WGS-84 latitude of the query point.
        longitude: WGS-84 longitude of the query point.

    Returns:
        The nearest usable building record, or None when ``buildings`` is empty.
    """
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

    Reuses ``plugins.builtin.parcel_buildings``' cached list for this parcel
    when one exists - that plugin performs the identical
    ``lookup_parcel_uuid``/``lookup_buildings`` pair, and on a campus the two
    running independently would double REData's per-pin cost for no new data.
    Only a cold cache falls through to fetching directly.

    "REData has no building here" is cached as ``{}``; "REData could not be
    asked" is not. The two used to share one broad ``except``, on the reasoning
    that a missing building record is lower-stakes than a missing property
    record - but the existence of a ``LocationCache`` row is what marks a
    source as fetched, so an outage (or an install with no REData configured at
    the moment of the fetch) blanked the card for the whole
    ``external_data_cache_days`` window with nothing to retry it. That is the
    rule ``tests/hypothesis/test_outage_not_cached_as_empty.py`` exists for, and
    it applies here too, so a transient reason now propagates.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        location: The Location whose cached parcel-buildings list may already
            answer this, when the caller has one.

    Returns:
        The nearest ``BuildingRecord`` dict, or ``{}`` when REData has no
        parcel or no buildings at this coordinate.

    Raises:
        PropertyRecordsUnavailableError: REData could not answer for a
            transient reason. Left to propagate so no row is written and the
            source stays retryable.
        ValueError: REData is not configured. Same reasoning - the panel's
            gate normally prevents this, but the background enrichment and the
            wiki card reach here by other routes.
    """
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
        logger.debug("redata_building_attributes: no buildings near %.2f,%.2f (%s)", latitude, longitude, exc.reason)
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

    # A reconciled REData record names its sources in `sources[]`; the flat
    # top-level `source` it replaced is still what Overpass-shaped rows carry.
    from urbanlens.dashboard.plugins.builtin.parcel_buildings import record_sources, source_chips

    return {"heading_name": heading_name, "chips": source_chips(record_sources(data)), "meta": meta}


class RedataBuildingAttributesPanelSource(CoordinateGatedInfoPanelSource):
    """Standardized building number/name/year-built card on the pin detail page, via REData."""

    key = "redata_building_attributes"
    cache_source = _CACHE_SOURCE
    section_id = "redata-building-attributes-section"
    icon = "domain"
    title = "Building Attributes"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self, pin: Pin) -> bool:
        """Also requires REData to be configured - this panel has no other data source.

        Without this the panel was scheduled for every US pin on an install
        with no REData, and its fetch cached an empty payload for each one.
        """
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

    def gate(self) -> bool:
        """Requires REData to be configured - this source has no other backend.

        Without it the cycle picks candidates, every fetch raises, and the run
        logs one exception per location. Answering here skips the source for
        the whole cycle instead, which is what "unavailable" means to
        ``self_reported_skip``.
        """
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
