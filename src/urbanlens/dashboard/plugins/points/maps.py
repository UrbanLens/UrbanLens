"""Map tiles, overlays and layers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint

if TYPE_CHECKING:
    from urbanlens.dashboard.services.locations.temporal_imagery import TemporalFeatureProvider
    from urbanlens.dashboard.services.map.basemap_catalogue import TileCatalogue
    from urbanlens.dashboard.services.map.image_overlays import OverlaySource
    from urbanlens.dashboard.services.map.pin_layers import PinMapLayer


TILE_CATALOGUES: ExtensionPoint[TileCatalogue] = ExtensionPoint(
    "maps.tile_catalogues",
    "Services that publish basemap layers beyond core's built-in vendor ones and serve their tiles. The first available one publishes the catalogue the map pages embed and proxies the layers it names.",
)

OVERLAY_SOURCES: ExtensionPoint[OverlaySource] = ExtensionPoint(
    "maps.overlay_sources",
    "Places a pin's or wiki's map can take georeferenced image overlays from, beyond uploads: each offers a browse list in the overlay manager and recognises its own tile addresses when an archive is imported.",
)

PIN_MAP_LAYERS: ExtensionPoint[PinMapLayer] = ExtensionPoint(
    "maps.pin_layers",
    "Layers the Private Pin page's map offers beyond core's: a button in the layers panel plus a JSON endpoint of features drawn when it is switched on.",
)

TEMPORAL_FEATURE_PROVIDERS: ExtensionPoint[TemporalFeatureProvider] = ExtensionPoint(
    "maps.temporal_feature_providers",
    "Sources of dated map features for the beta time slider that answer instead of OpenHistoricalMap's public API, asked in priority order; the first available one is used.",
)

__all__ = ["OVERLAY_SOURCES", "PIN_MAP_LAYERS", "TEMPORAL_FEATURE_PROVIDERS", "TILE_CATALOGUES"]
