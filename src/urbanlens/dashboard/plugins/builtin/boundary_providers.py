"""Boundary providers: where default parcel and building outlines come from when no plugin offers better."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class BoundaryProvidersPlugin(UrbanLensPlugin):
    """OpenStreetMap, Overture, Microsoft and Google building data as the default boundary chain."""

    name: ClassVar[str] = "boundary_providers"
    verbose_name: ClassVar[str] = "Boundary providers"
    description: ClassVar[str] = "Resolves a pin's property and building outlines from OpenStreetMap, Overture Maps, Microsoft Building Footprints and Google Open Buildings, in that order."
    author: ClassVar[str] = "UrbanLens"

    # Their rate-limit defaults ("overpass", "overture_maps", "microsoft_building_footprints", "google_open_buildings")
    # stay in rate_limiter.SERVICE_REGISTRY.

    def setup(self, plugin: PluginContext) -> None:
        """Register the providers, spaced so a plugin's can slot in between."""
        from urbanlens.dashboard.plugins.points import DATA_SOURCE_CREDITS, DataSourceCredit

        plugin.add(
            DATA_SOURCE_CREDITS,
            DataSourceCredit(
                name="OpenStreetMap",
                category="open_map",
                url="https://www.openstreetmap.org/copyright",
                note="Map data © OpenStreetMap contributors, available under the Open Database License (ODbL). Property and building outlines are queried through Overpass.",
            ),
        )

        from urbanlens.dashboard.plugins.points import BOUNDARY_PROVIDERS
        from urbanlens.dashboard.services.apis.locations.boundaries.google_open_buildings import GoogleOpenBuildingsGateway
        from urbanlens.dashboard.services.apis.locations.boundaries.microsoft_buildings import MicrosoftBuildingFootprintsGateway
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
        from urbanlens.dashboard.services.apis.locations.boundaries.overture import OvertureProvider

        plugin.add(BOUNDARY_PROVIDERS, OverpassGateway, priority=100)
        plugin.add(BOUNDARY_PROVIDERS, OvertureProvider, priority=110)
        plugin.add(BOUNDARY_PROVIDERS, MicrosoftBuildingFootprintsGateway, priority=120)
        plugin.add(BOUNDARY_PROVIDERS, GoogleOpenBuildingsGateway, priority=130)
