"""NY SHPO CRIS plugin: Building USN Point data for pinned locations. New York only.

Retrieval lives entirely in REData (the standalone service that already owns
property records for this app - see ``plugins.builtin.property_records``):
``RedataGateway.lookup_cultural_resources`` finds resources near the pin's
coordinate (NY's Cultural Resource Information System is REData's only
current provider - everywhere else returns nothing, the "real search, no
matches" shape), and ``fetch_cultural_resource_detail`` fetches the first
"building"-type match's full record, including attachment/photo metadata.
Attachments are exposed to the pin's Media gallery via
:meth:`CrisBuildingPanelSource.media_items`, streamed through
:class:`~urbanlens.dashboard.controllers.pin.PinCrisAttachmentView` so
REData's API key never reaches the browser (same reasoning as every other
authenticated media proxy in this app).

Field names in :meth:`CrisBuildingPanelSource.render_context` (``USNNum``,
``USNName``, ``HouseNum``, ``StreetName``, ``City``, ``Zip``,
``EligibilityDesc``) match the live "Building USN Points" ArcGIS FeatureServer
schema (NYS Office of Parks, Recreation and Historic Preservation) - REData's
lookup response nests these under the resource's own ``attributes`` dict, so
``fetch`` flattens that dict onto the top level of the cached payload,
keeping ``render_context`` unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource, GalleryMediaSource
from urbanlens.dashboard.services.geo_boundary import state_boundary
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider

logger = logging.getLogger(__name__)

#: Eligibility values that mean the surveyed building/structure no longer
#: exists. Once real eligibility data starts flowing (it now does, via
#: fetch() below), a payload with this ``EligibilityDesc`` should apply the
#: "Demolished" status label via
#: ``services.labels.statuses.add_demolished_status``/``add_demolished_status_to_wiki``
#: (looking up ``pin.location.wiki``, when present) - not implemented yet;
#: tracked separately from this plugin's media-gallery integration.
_DEMOLISHED_ELIGIBILITY = "Not Eligible - Demolished"

#: Only "building" resources carry the USN Point fields this panel renders;
#: the other CRIS resource types (district/national-register-listing/
#: archaeological-buffer-area) are out of scope for this specific plugin.
_RESOURCE_TYPE = "building"

#: A resource's real detail-fetch never runs on every page load - REData
#: caches ``detail_payload``/``attachments`` on the resource itself once
#: fetched, so this only needs to happen again after this TTL, exactly like
#: every other LocationCache-backed panel's own freshness window.
_RADIUS_METERS = 200


class CrisBuildingPanelSource(CoordinateGatedInfoPanelSource, GalleryMediaSource):
    """NY SHPO CRIS "Building USN Point" info for the pin's location. New York only."""

    key = "cris_building"
    cache_source = "cris_building_usn"
    section_id = "cris-building-section"
    icon = "account_balance"
    title = "NY Historic Preservation (CRIS)"
    geo_boundary: ClassVar[GeoBoundary | None] = state_boundary("NY")

    def fetch(self, pin: Pin) -> None:
        """Find the nearest CRIS "building" resource and cache its info + attachments."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

        location = pin.location
        lat = float(location.latitude) if location and location.latitude is not None else None
        lng = float(location.longitude) if location and location.longitude is not None else None
        if lat is None or lng is None:
            LocationCache.set(pin.location, self.cache_source, {}, query_key="")
            return

        query_key = f"{lat},{lng}"
        try:
            gateway = RedataGateway()
            resources = gateway.lookup_cultural_resources(lat, lng, radius_meters=_RADIUS_METERS)
            building = next((r for r in resources if r.get("resource_type") == _RESOURCE_TYPE), None)
            if building is None:
                LocationCache.set(pin.location, self.cache_source, {}, query_key=query_key)
                return
            resource_uuid = building.get("uuid")
            detail = gateway.fetch_cultural_resource_detail(resource_uuid) if resource_uuid else building
        except (PropertyRecordsUnavailableError, ValueError):
            logger.debug("CrisBuildingPanelSource.fetch: no building resource available for pin %s", pin.pk, exc_info=True)
            LocationCache.set(pin.location, self.cache_source, {}, query_key=query_key)
            return

        # Flatten the resource's own `attributes` (the raw ArcGIS layer
        # feature's fields - USNName, USNNum, HouseNum, ...) onto the top
        # level, matching what render_context already expects.
        data = dict(detail.get("attributes") or {})
        data["resource_uuid"] = detail.get("uuid") or resource_uuid
        data["attachments"] = detail.get("attachments") or []
        LocationCache.set(pin.location, self.cache_source, data, query_key=query_key)

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

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached CRIS attachments (photos and documents) into gallery items.

        Args:
            data: This source's cached payload (see :meth:`fetch`), including
                ``resource_uuid`` and ``attachments``.

        Returns:
            One item per attachment, proxied through
            ``PinCrisAttachmentView`` (never a raw REData URL). Document-kind
            attachments get an empty ``thumb_url`` - the Media gallery
            already renders a fallback icon tile for those (see
            ``MediaItem.thumb_url``'s own docstring).
        """
        from django.urls import reverse

        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        resource_uuid = data.get("resource_uuid")
        if not resource_uuid:
            return []

        items: list[MediaItem] = []
        for attachment in data.get("attachments") or []:
            attachment_id = attachment.get("id")
            if attachment_id is None:
                continue
            proxy_url = reverse("pin.cris.attachment", args=[resource_uuid, attachment_id])
            is_photo = attachment.get("kind") == "PHOTO"
            caption = attachment.get("name") or attachment.get("attachment_type") or ""
            items.append(MediaItem(url=proxy_url, thumb_url=proxy_url if is_photo else "", caption=caption, source="NY Historic Preservation (CRIS)"))
        return items


class CrisBuildingEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the CRIS Building USN Point cache per Location. New York only."""

    key: ClassVar[str] = "cris_building"
    verbose_name: ClassVar[str] = "NY Historic Preservation (CRIS)"
    cache_source: ClassVar[str] = "cris_building_usn"
    geo_boundary: ClassVar[GeoBoundary | None] = state_boundary("NY")

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Find the nearest CRIS "building" resource and return its flattened info.

        Shares ``cache_source`` with :class:`CrisBuildingPanelSource`, so
        whichever of panel-fetch or background enrichment runs first for a
        Location fills in for the other - matches that class's own ``fetch``
        (attachments aren't fetched here, since enrichment only needs the
        info-card fields, not the Media gallery).
        """
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

        query_key = f"{location.latitude},{location.longitude}"
        try:
            resources = RedataGateway().lookup_cultural_resources(float(location.latitude), float(location.longitude), radius_meters=_RADIUS_METERS)
        except (PropertyRecordsUnavailableError, ValueError):
            return None, query_key
        building = next((r for r in resources if r.get("resource_type") == _RESOURCE_TYPE), None)
        if building is None:
            return None, query_key
        data = dict(building.get("attributes") or {})
        data["resource_uuid"] = building.get("uuid")
        data["attachments"] = building.get("attachments") or []
        return data, query_key


class CrisBuildingsPlugin(UrbanLensPlugin):
    """NY State Historic Preservation Office (SHPO) CRIS data for pinned locations. New York only."""

    name: ClassVar[str] = "cris_buildings"
    verbose_name: ClassVar[str] = "NY Historic Preservation (CRIS)"
    description: ClassVar[str] = (
        "Building USN Point data (National Register eligibility, historic districts) and its photos/documents, "
        "from NY SHPO's Cultural Resource Information System, via REData. New York State only."
    )
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() override - this plugin calls REData's own API
    # (service key "redata_api"), already registered by plugins.builtin.property_records.

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the CRIS Building USN Point pin-detail panel (also a Media-gallery source)."""
        return [CrisBuildingPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the CRIS Building USN Point cache to scheduled background enrichment."""
        return [CrisBuildingEnrichmentSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the CRIS-listed property name as a place-name candidate."""
        return [LocationCacheNameProvider(source="cris", cache_source="cris_building_usn", keys=("USNName",), verbose_name="NY SHPO (CRIS)")]
