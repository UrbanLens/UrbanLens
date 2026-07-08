"""Satellite imagery plugins: providers for the pin detail satellite carousel.

Plugin ``order`` values control carousel slide order (Google Maps, defined in
its own module, is 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider


class EsriPlugin(UrbanLensPlugin):
    """Esri World Imagery satellite basemaps."""

    name: ClassVar[str] = "esri"
    verbose_name: ClassVar[str] = "Esri World Imagery"
    description: ClassVar[str] = "Esri ArcGIS World Imagery (including Wayback historical imagery) in the satellite carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 20

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the public Esri REST services."""
        return {
            "esri": ServiceDefaults(
                display_name="Esri ArcGIS REST",
                calls_per_minute=20,
                calls_per_day=500,
                notes="Public Esri basemap/wayback services. No key required.",
            ),
        }

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute Esri satellite imagery."""
        from urbanlens.dashboard.services.apis.locations.esri import EsriGateway

        return [EsriGateway()]


class NasaGibsPlugin(UrbanLensPlugin):
    """NASA GIBS satellite imagery."""

    name: ClassVar[str] = "nasa_gibs"
    verbose_name: ClassVar[str] = "NASA GIBS"
    description: ClassVar[str] = "NASA Global Imagery Browse Services satellite imagery in the satellite carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 30

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the NASA GIBS tile services."""
        return {
            "nasa_gibs": ServiceDefaults(
                display_name="NASA GIBS",
                calls_per_minute=20,
                calls_per_day=500,
                notes="Free, no key required. Public NASA tile services.",
            ),
        }

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute NASA GIBS satellite imagery."""
        from urbanlens.dashboard.services.apis.locations.nasa_gibs import NasaGibsGateway

        return [NasaGibsGateway()]


class MapboxPlugin(UrbanLensPlugin):
    """Mapbox satellite imagery."""

    name: ClassVar[str] = "mapbox"
    verbose_name: ClassVar[str] = "Mapbox"
    description: ClassVar[str] = "Mapbox Static Images satellite imagery in the satellite carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 40

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Mapbox Static Images API."""
        return {
            "mapbox": ServiceDefaults(
                display_name="Mapbox",
                calls_per_minute=20,
                calls_per_day=500,
                notes="Requires a Mapbox public access token. Static Images API has a free tier.",
            ),
        }

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute Mapbox satellite imagery."""
        from urbanlens.dashboard.services.apis.locations.mapbox import MapboxGateway

        return [MapboxGateway()]


class BingMapsPlugin(UrbanLensPlugin):
    """Bing Maps satellite imagery."""

    name: ClassVar[str] = "bing_maps"
    verbose_name: ClassVar[str] = "Bing Maps"
    description: ClassVar[str] = "Bing Maps static aerial imagery in the satellite carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 50

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Bing Maps imagery API."""
        return {
            "bing_maps": ServiceDefaults(
                display_name="Bing Maps",
                calls_per_minute=20,
                calls_per_day=500,
                notes="Requires a Bing Maps key from Azure portal. Static imagery has a free tier.",
            ),
        }

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute Bing Maps satellite imagery."""
        from urbanlens.dashboard.services.apis.locations.bing_maps import BingMapsGateway

        return [BingMapsGateway()]


class OpenAerialMapPlugin(UrbanLensPlugin):
    """OpenAerialMap satellite imagery."""

    name: ClassVar[str] = "open_aerial_map"
    verbose_name: ClassVar[str] = "OpenAerialMap"
    description: ClassVar[str] = "Openly licensed aerial imagery from OpenAerialMap in the satellite carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 60

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the OpenAerialMap API."""
        return {
            "open_aerial_map": ServiceDefaults(
                display_name="OpenAerialMap",
                calls_per_minute=20,
                calls_per_day=500,
                notes="Free, no key required. Open licensed aerial imagery metadata.",
            ),
        }

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute OpenAerialMap satellite imagery."""
        from urbanlens.dashboard.services.apis.locations.open_aerial_map import OpenAerialMapGateway

        return [OpenAerialMapGateway()]
