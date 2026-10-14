"""Data providers asked in priority order: geocoding, places, routing, weather, CID resolution and web search."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.cid_resolution import CidResolver
    from urbanlens.dashboard.services.apis.locations.geocode_resolution import GeocodeProvider
    from urbanlens.dashboard.services.apis.locations.places_resolution import PlacesProvider
    from urbanlens.dashboard.services.apis.locations.routing_resolution import RouteProvider
    from urbanlens.dashboard.services.apis.locations.weather_resolution import WeatherProvider
    from urbanlens.dashboard.services.search.search import WebSearchProvider


GEOCODERS: ExtensionPoint[GeocodeProvider] = ExtensionPoint(
    "providers.geocoders",
    "Forward geocoders (address to coordinates), asked in priority order. A provider that fails with ``ProviderUnavailableError`` hands over to the next, unless it ``shields_direct`` and this is not production.",
)

PLACES_PROVIDERS: ExtensionPoint[PlacesProvider] = ExtensionPoint(
    "providers.places",
    "Places data (nearby search, details, photos, autocomplete). The first available provider answers every call; there is no fallback after a failure.",
)

ROUTE_PROVIDERS: ExtensionPoint[RouteProvider] = ExtensionPoint(
    "providers.routes",
    "Driving-route providers, asked in priority order; one that fails with ``ProviderUnavailableError`` hands over to the next.",
)

WEATHER_PROVIDERS: ExtensionPoint[WeatherProvider] = ExtensionPoint(
    "providers.weather",
    "Forecast, sun-time and recorded-weather providers. A provider that declines (None or empty) hands over to the next.",
)

CID_RESOLVERS: ExtensionPoint[CidResolver] = ExtensionPoint(
    "providers.cid_resolvers",
    "Resolvers from a Google Maps CID to coordinates. The first available resolver takes the whole batch.",
)

WEB_SEARCH_PROVIDERS: ExtensionPoint[WebSearchProvider] = ExtensionPoint(
    "providers.web_search",
    "Web search providers. The first available provider answers; with none, a search returns no results.",
)

__all__ = ["CID_RESOLVERS", "GEOCODERS", "PLACES_PROVIDERS", "ROUTE_PROVIDERS", "WEATHER_PROVIDERS", "WEB_SEARCH_PROVIDERS"]
