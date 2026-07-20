"""NY SHPO CRIS plugin: Building USN Point data for pinned locations. New York only.

Infrastructure only for now - see ``docs/redata.md``. REData (the standalone
service that already owns property-records retrieval for this app - see
``plugins.builtin.property_records``) will implement retrieval of NY State
Historic Preservation Office (SHPO) Cultural Resource Information System
(CRIS) "Building USN Point" data and be queried per-pin from there, the same
way ``services.apis.property_records.redata_gateway.RedataGateway`` is queried
today. Until that endpoint exists, :meth:`CrisBuildingPanelSource.fetch` and
:meth:`CrisBuildingEnrichmentSource.fetch` persist an explicit empty result -
the panel/enrichment scheduling, single-flight, and failure-suppression
machinery (``services.external_data``, ``services.enrichment``) is fully
wired and safe to ship this way (it just never shows data yet), without
fabricating a payload shape REData hasn't committed to.

Field names in :meth:`CrisBuildingPanelSource.render_context` (``USNNum``,
``USNName``, ``HouseNum``, ``StreetName``, ``City``, ``Zip``,
``EligibilityDesc``) match the live "Building USN Points" ArcGIS FeatureServer
schema (NYS Office of Parks, Recreation and Historic Preservation), so wiring
up the real fetch later needs no template/rendering changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.geo_boundary import state_boundary
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider

#: Eligibility values that mean the surveyed building/structure no longer
#: exists. Once ``fetch`` below is wired to a real REData lookup, a payload
#: with this ``EligibilityDesc`` should apply the "Demolished" status label via
#: ``services.labels.statuses.add_demolished_status``/``add_demolished_status_to_wiki``
#: (looking up ``pin.location.wiki``, when present) - not implemented yet
#: since there's no real eligibility data to branch on.
_DEMOLISHED_ELIGIBILITY = "Not Eligible - Demolished"


class CrisBuildingPanelSource(CoordinateGatedInfoPanelSource):
    """NY SHPO CRIS "Building USN Point" info for the pin's location. New York only."""

    key = "cris_building"
    cache_source = "cris_building_usn"
    section_id = "cris-building-section"
    icon = "account_balance"
    title = "NY Historic Preservation (CRIS)"
    geo_boundary: ClassVar[GeoBoundary | None] = state_boundary("NY")

    def fetch(self, pin: Pin) -> None:
        """Persist an empty result until REData exposes a CRIS lookup endpoint.

        TODO: once REData implements CRIS retrieval (see module docstring),
        replace this body with a call mirroring
        ``RedataGateway.lookup_parcel`` and apply the Demolished status label
        when the response's ``EligibilityDesc`` is ``_DEMOLISHED_ELIGIBILITY``.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(pin.location, self.cache_source, {}, query_key="")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the Building USN Point card from a cached CRIS payload.

        Field names match the live "Building USN Points" ArcGIS FeatureServer
        schema; see the module docstring.
        """
        data = data or {}
        usn_name = data.get("USNName")
        if not usn_name:
            return None

        address_parts = [part for part in (data.get("HouseNum"), data.get("StreetName")) if part]
        meta = []
        if address_parts:
            meta.append({"label": "Address", "value": " ".join(address_parts)})
        for key, label in (("City", "City"), ("Zip", "ZIP Code"), ("USNNum", "NYSHPO USN Number"), ("EligibilityDesc", "Eligibility Status")):
            value = data.get(key)
            if value:
                meta.append({"label": label, "value": value})

        return {"heading_name": usn_name, "meta": meta, "nested": True}


class CrisBuildingEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the CRIS Building USN Point cache per Location. New York only."""

    key: ClassVar[str] = "cris_building"
    verbose_name: ClassVar[str] = "NY Historic Preservation (CRIS)"
    cache_source: ClassVar[str] = "cris_building_usn"
    geo_boundary: ClassVar[GeoBoundary | None] = state_boundary("NY")

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Persist an empty result until REData exposes a CRIS lookup endpoint.

        See :meth:`CrisBuildingPanelSource.fetch` - same "infrastructure, not
        retrieval yet" stub, sharing the same ``cache_source`` so whichever of
        panel-fetch or background enrichment runs first for a Location fills
        in for the other once a real fetch is implemented.
        """
        return None, f"{location.latitude},{location.longitude}"


class CrisBuildingsPlugin(UrbanLensPlugin):
    """NY State Historic Preservation Office (SHPO) CRIS data for pinned locations. New York only."""

    name: ClassVar[str] = "cris_buildings"
    verbose_name: ClassVar[str] = "NY Historic Preservation (CRIS)"
    description: ClassVar[str] = (
        "Building USN Point data (National Register eligibility, historic districts) "
        "from NY SHPO's Cultural Resource Information System, via REData. New York State only."
    )
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() yet - there's no direct upstream call to
    # rate-limit until REData's CRIS endpoint (and its service key) exists.

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the CRIS Building USN Point pin-detail panel."""
        return [CrisBuildingPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the CRIS Building USN Point cache to scheduled background enrichment."""
        return [CrisBuildingEnrichmentSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the CRIS-listed property name as a place-name candidate."""
        return [LocationCacheNameProvider(source="cris", cache_source="cris_building_usn", keys=("USNName",), verbose_name="NY SHPO (CRIS)")]
