"""Hydrology plugin: water near a pin - streams, wetlands, watershed - via REData.

Practical site context: a culverted stream or seasonally-flooded wetland
under a property explains standing water, deterioration and access windows.
USA-only (USGS NHD/WBD + USFWS NWI).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.pins.external_data import PanelSource

_MAX_ROWS = 7


class HydrologyPanelSource(RedataInfoPanelSource):
    """Streams, waterbodies and wetlands within 1 km, plus the containing watershed."""

    key = "redata_hydrology"
    cache_source = "redata_hydrology"
    section_id = "hydrology-section"
    icon = "water_drop"
    title = "Water & Hydrology"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    payload_key: ClassVar[str] = "features"

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Streams, waterbodies and wetlands near the pin."""
        from urbanlens.dashboard.services.apis.locations.redata_hydrology_gateway import RedataHydrologyGateway

        return RedataHydrologyGateway().get_hydrology(latitude, longitude, limit=30)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Watershed as the heading; nearest features as the grid."""
        from urbanlens.dashboard.services.apis.locations.redata_hydrology_gateway import HYDROLOGY_KIND_LABELS

        features = (data or {}).get("features") or []
        if not features:
            return None

        watershed = next((feature for feature in features if feature.get("kind") == "watershed"), None)
        nearby = [feature for feature in features if feature.get("kind") != "watershed"]

        context: dict = {}
        if watershed and watershed.get("name"):
            context["heading_name"] = f"{watershed['name']} watershed"

        by_kind: dict[str, int] = {}
        for feature in nearby:
            kind = feature.get("kind") or "stream"
            by_kind[kind] = by_kind.get(kind, 0) + 1
        plurals = {"stream": "streams", "waterbody": "waterbodies", "wetland": "wetlands"}
        context["chips"] = [
            f"{count} {(plurals.get(kind, kind + 's') if count != 1 else HYDROLOGY_KIND_LABELS.get(kind, kind).lower())} within 1 km" for kind, count in sorted(by_kind.items())
        ]

        # Features with a measured distance first, nearest first; unmeasurable
        # rows (NWI wetlands - the source layer has no geometry) follow rather
        # than being dropped or given an invented distance.
        measured = sorted((feature for feature in nearby if feature.get("distance_meters") is not None), key=lambda feature: feature["distance_meters"])
        unmeasured = [feature for feature in nearby if feature.get("distance_meters") is None]
        meta = []
        for feature in (measured + unmeasured)[:_MAX_ROWS]:
            kind_label = HYDROLOGY_KIND_LABELS.get(feature.get("kind") or "", "Feature")
            # A blank name is the survey's own answer (most headwater streams
            # are genuinely unnamed), so label with the kind instead.
            name = feature.get("name") or f"Unnamed {kind_label.lower()}"
            details = []
            if feature.get("distance_meters") is not None:
                details.append(f"{feature['distance_meters']:,.0f} m away")
            area = feature.get("area_sq_km")
            if isinstance(area, (int, float)) and area >= 0.01:
                details.append(f"{area:,.2f} km2")
            attributes = feature.get("attributes") or {}
            if attributes.get("water_regime"):
                details.append(str(attributes["water_regime"]).lower())
            meta.append({"label": kind_label, "value": name + (f" ({', '.join(details)})" if details else "")})

        context["meta"] = meta
        return context


class HydrologyPlugin(UrbanLensPlugin):
    """Water-features context for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_hydrology"
    verbose_name: ClassVar[str] = "Water & Hydrology"
    description: ClassVar[str] = "Shows streams, waterbodies, wetlands and the containing watershed near the pin on the detail page, from USGS and USFWS via REData. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the hydrology pin-detail panel."""
        return [HydrologyPanelSource()]
