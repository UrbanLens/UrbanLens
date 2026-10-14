"""
Esri satellite imagery: a provider for the pin detail satellite carousel.
USGS Historical Topo Maps is a different kind of feature entirely (a gallery of individually dated/titled scanned quadrangles, not one carousel slide) and was never part of this carousel - see ``plugins.builtin.usgs``, untouched by this migration."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class EsriPlugin(UrbanLensPlugin):
    """Esri World Imagery satellite basemaps."""

    name: ClassVar[str] = "esri"
    verbose_name: ClassVar[str] = "Esri World Imagery"
    description: ClassVar[str] = "Esri ArcGIS World Imagery (including Wayback historical imagery) in the satellite carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 20

    def setup(self, plugin: PluginContext) -> None:
        """Esri ArcGIS REST rate-limit defaults and the Esri satellite imagery provider."""
        from urbanlens.dashboard.plugins.points import SATELLITE_PROVIDERS, SERVICE_DEFAULTS
        from urbanlens.dashboard.services.apis.locations.esri import EsriGateway

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "esri": ServiceDefaults(
                    display_name="Esri ArcGIS REST",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=20,
                    calls_per_day=500,
                    notes="Public Esri basemap/wayback services. No key required.",
                ),
            },
        )

        plugin.add(SATELLITE_PROVIDERS, EsriGateway)
