"""Overture Maps building-attributes plugin: physical building characteristics panel."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource, PanelPlacement
from urbanlens.dashboard.services.sandbox.queues import Queue

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

#: Where REData holds no Overture, get_building_attributes() and get_nearby_places() are each independent S3 GeoParquet range reads
#: (connect/request timeouts of 10s/30s each - see OvertureMapsGateway), and observed in production
#: to occasionally each take close to their own ceiling, compounding to 100s+ for one fetch() call
#: when run back-to-back. get_nearby_places() is the less essential of the two.
_NEARBY_PLACES_BUDGET_SECONDS = 20.0

#: How a cached row names its nearby places when they were not heard from, so the row lapses within the hour.
_PLACES_SOURCE = "overture_places"


class OvertureBuildingAttributesPanelSource(CoordinateGatedInfoPanelSource):
    """Overture Maps building characteristics and nearby named places for the pin's location."""

    key = "overture_building_attributes"
    cache_source = "overture_building_attributes"
    section_id = "overture-building-section"
    icon = "apartment"
    title = "Building Characteristics"
    placement: ClassVar[PanelPlacement] = PanelPlacement.PROPERTY
    building_level: ClassVar[bool] = True
    tab_order: ClassVar[int] = 20
    # The prefork pool, not the fast thread-pool queue - where REData holds no Overture, OvertureMapsGateway reads GeoParquet via
    # pyarrow/geopandas (real CPU-bound parsing/geometry work, same class of cost as
    # BoundaryPanelSource's shapely work), and several running concurrently on a thread pool would
    # cause enough GIL contention to slow down every other panel sharing it. See PanelSource.queue.
    queue = Queue.INTERACTIVE

    def gate(self, pin: Pin) -> bool:
        """Where REData's Overture mirror covers the pin only REData may answer, so an install without it has nothing to fetch there."""
        from urbanlens.dashboard.services.apis.locations.boundaries.overture import served_by_redata
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        if not super().gate(pin):
            return False
        return redata_configured() or not served_by_redata(float(pin.effective_latitude or 0), float(pin.effective_longitude or 0))

    def fetch(self, pin: Pin) -> None:
        """Look up the pinned building's Overture attributes and cache the result.

        A building with its nearby places unheard from (skipped for time, or their source out) is cached marked
        partial, so the row lapses within the hour; with no building either, nothing is cached.

        Raises:
            GatewayRequestError: The building lookup failed, or the places lookup failed with no building to keep.
        """
        from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY, LocationCache
        from urbanlens.dashboard.models.place.external_tag import ExternalTagSource, PlaceExternalTag
        from urbanlens.dashboard.services.apis.locations.boundaries.overture import OvertureProvider
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError, is_source_outage
        from urbanlens.dashboard.services.locations.external_tags import extract_overture_tags

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        provider = OvertureProvider()

        started = time.monotonic()
        attributes = provider.get_building_attributes(lat, lng) or {}

        target_place = pin.location.place
        if attributes and target_place is not None and not PlaceExternalTag.is_fresh_for(target_place, ExternalTagSource.OVERTURE):
            PlaceExternalTag.sync_for_source(target_place, ExternalTagSource.OVERTURE, extract_overture_tags(attributes))

        nearby_places: list = []
        unanswered: list[str] = []
        elapsed = time.monotonic() - started
        if elapsed < _NEARBY_PLACES_BUDGET_SECONDS:
            try:
                nearby_places = provider.get_nearby_places(lat, lng, radius_m=150, limit=5)
            except GatewayRequestError as exc:  # outage-cache-ok: the building is kept, marked partial, for the hour
                if not attributes or not is_source_outage(exc):
                    raise
                logger.info("Overture nearby places for pin %s unavailable; keeping its building briefly: %s", pin.pk, exc)
                unanswered.append(_PLACES_SOURCE)
        else:
            logger.warning(
                "Overture building-attributes fetch for pin %s skipping get_nearby_places - get_building_attributes already took %.1fs",
                pin.pk,
                elapsed,
            )
            unanswered.append(_PLACES_SOURCE)

        data: dict = {**attributes, "nearby_places": nearby_places}
        if unanswered:
            data[UNANSWERED_SOURCES_KEY] = unanswered
        LocationCache.set(pin.location, self.cache_source, data, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the building-characteristics card from Overture's attribute + nearby-places lookup."""
        from urbanlens.dashboard.services.locations.site_scope import is_site_scope

        if not data or is_site_scope(pin):
            return None

        chips = [data["subtype"].replace("_", " ").title()] if data.get("subtype") else []
        meta = []
        if data.get("height_m"):
            meta.append({"label": "Height", "value": f"{data['height_m']:.0f} m"})
        if data.get("num_floors"):
            meta.append({"label": "Floors", "value": str(data["num_floors"])})
        if data.get("roof_shape"):
            meta.append({"label": "Roof Shape", "value": data["roof_shape"].replace("_", " ").title()})
        if data.get("roof_material"):
            meta.append({"label": "Roof Material", "value": data["roof_material"].replace("_", " ").title()})

        facts = []
        for place in data.get("nearby_places") or []:
            category = (place.get("category") or "").replace("_", " ").title()
            status = place.get("operating_status")
            status_suffix = " (closed)" if status == "closed" else ""
            text = f"{place['name']}{status_suffix} - {category} ({place['distance_m']:.0f}m)" if category else f"{place['name']}{status_suffix} ({place['distance_m']:.0f}m)"
            facts.append({"icon": "storefront", "text": text})

        if not chips and not meta and not facts:
            return None

        return {"heading_name": data.get("primary_name"), "chips": chips, "facts": facts, "meta": meta}


class OvertureBuildingAttributesPlugin(UrbanLensPlugin):
    """Overture Maps building characteristics for pinned locations."""

    name: ClassVar[str] = "overture_building_attributes"
    verbose_name: ClassVar[str] = "Overture Building Characteristics"
    description: ClassVar[str] = (
        "Free, open-data building class/height/floor-count/roof details from Overture Maps' Buildings "
        "theme, plus nearby named places from its Places theme (same dataset already used for footprint "
        "boundaries). Where REData's own Overture mirror covers the pin (most of the US) these come from it and "
        "need UL_REDATA_API_URL/UL_REDATA_API_KEY; elsewhere from Overture's public release."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the building-characteristics pin-detail panel."""
        return [OvertureBuildingAttributesPanelSource()]
