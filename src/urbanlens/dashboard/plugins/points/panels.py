"""The Private Pin page's panels, the media proxies, and what runs when a location is created or a property first pinned."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint

if TYPE_CHECKING:
    from django.urls import URLPattern, URLResolver

    from urbanlens.dashboard.services.locations.prefetch import LocationPrefetcher
    from urbanlens.dashboard.services.map.nearby_places import NearbyParksProvider
    from urbanlens.dashboard.services.pins.bootstrap import LocationPrewarmer


LOCATION_PREWARMERS: ExtensionPoint[LocationPrewarmer] = ExtensionPoint(
    "pins.location_prewarmers",
    "Services told to start gathering what they know about a newly pinned point, once, before the bootstrap's own fetches ask for it. Each decides for itself whether it is available and how often to ask.",
)

LOCATION_PREFETCHERS: ExtensionPoint[LocationPrefetcher] = ExtensionPoint(
    "locations.prefetchers",
    "Services that cache what they know about a newly created Location so the first page that wants it finds it stored. Each decides for itself whether it is available and whether its answer is already stored.",
)

NEARBY_PARK_PROVIDERS: ExtensionPoint[NearbyParksProvider] = ExtensionPoint(
    "map.nearby_park_providers",
    "Searchable catalogues of national parks behind the Places layer's parks source. The first available one answers; with none, the layer has no parks.",
)

PIN_URLS: ExtensionPoint[URLPattern | URLResolver] = ExtensionPoint(
    "pins.urls",
    "URL patterns mounted under ``map/pin/`` ahead of its ``<slug:pin_slug>/`` routes, for a file served by its own id: a path such as ``media/<uuid>/`` would otherwise be read as a pin slug. Like every plugin route, linked to only from a template slot.",
)

__all__ = ["LOCATION_PREFETCHERS", "LOCATION_PREWARMERS", "NEARBY_PARK_PROVIDERS", "PIN_URLS"]
