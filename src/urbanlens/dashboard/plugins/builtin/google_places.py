"""Google Places plugin: place-name candidates and rate-limit defaults.

Google Places has no pin-detail panel of its own - its details payload is
cached into ``LocationCache`` by the place-details flow (see
``tasks.prefetch_location_external_data``) and the linked ``GooglePlace`` row
carries a resolved place name. This plugin wires both into the plugin system
as name candidates and owns the service's rate-limit defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location


class GooglePlacesNameProvider(NameProvider):
    """Google place names from the linked GooglePlace row and cached Places details."""

    def __init__(self) -> None:
        """Initialize with the ``google_places`` source slug."""
        super().__init__(source="google_places", verbose_name="Google Places")

    def candidates(self, location: Location) -> list[str | None]:
        """Return the cached Google place name and the cached details name.

        Args:
            location: The location to name.

        Returns:
            Raw candidate values; empty entries are filtered by the caller.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        values: list[str | None] = [location.cached_place_name]
        cached = LocationCache.get_fresh(location, "google_places")
        data = cached.data if cached else None
        if isinstance(data, dict):
            values.append(data.get("name"))
        return values


class GooglePlacesPlugin(UrbanLensPlugin):
    """Google Places integration: place names and service defaults."""

    name: ClassVar[str] = "google_places"
    verbose_name: ClassVar[str] = "Google Places"
    description: ClassVar[str] = "Provides place names for locations from the Google Places API."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 10

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Google Places API."""
        return {
            "google_places": ServiceDefaults(
                display_name="Google Places API",
                calls_per_minute=20,
                calls_per_day=200,
                notes="Free tier: $200/month credit. Geocoding/details billed per call.",
            ),
        }

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute Google place names as place-name candidates."""
        return [GooglePlacesNameProvider()]
