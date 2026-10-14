"""
Wikimedia Commons plugin: a provider for the Private Pin page's combined Media gallery.
It contributes one :class:`~urbanlens.dashboard.services.pins.external_data.DocumentMediaPanelSource`, which the gallery fetches independently so a slow provider never blocks the others.
The books and scans it finds are listed under Article > Sources."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import DocumentMediaPanelSource
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class WikimediaPlugin(UrbanLensPlugin):
    """Wikimedia Commons media for pinned locations."""

    name: ClassVar[str] = "wikimedia"
    verbose_name: ClassVar[str] = "Wikimedia Commons"
    description: ClassVar[str] = "Adds Wikimedia Commons media to the Private Pin page's Media gallery."
    author: ClassVar[str] = "UrbanLens"

    def setup(self, plugin: PluginContext) -> None:
        """Wikimedia Commons rate-limit defaults and media-gallery provider; the books and scans it finds go to Article > Sources."""
        from urbanlens.dashboard.plugins.points import PANEL_SOURCES, SERVICE_DEFAULTS
        from urbanlens.dashboard.services.apis.assets.wikimedia import WikimediaGateway

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "wikimedia": ServiceDefaults(
                    display_name="Wikimedia Commons",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=30,
                    calls_per_day=1000,
                    notes="Free API.",
                ),
            },
        )

        plugin.extend(PANEL_SOURCES, [DocumentMediaPanelSource("wikimedia", WikimediaGateway.service_key, WikimediaGateway, gallery_order=20, gallery_label="Wikimedia")])
