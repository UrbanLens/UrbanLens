"""Media archive plugins: providers for the pin detail page's combined Media gallery.

Each plugin contributes one :class:`~urbanlens.dashboard.services.external_data.MediaPanelSource`,
which the gallery fetches independently so a slow provider never blocks the
others.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import MediaPanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.services.external_data import PanelSource


class SmithsonianPlugin(UrbanLensPlugin):
    """Smithsonian Open Access media for pinned locations."""

    name: ClassVar[str] = "smithsonian"
    verbose_name: ClassVar[str] = "Smithsonian Open Access"
    description: ClassVar[str] = "Adds Smithsonian Open Access archive media to the pin detail page's Media gallery. USA-centric."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Smithsonian API."""
        return {
            "smithsonian": ServiceDefaults(
                display_name="Smithsonian Open Access",
                calls_per_minute=20,
                calls_per_day=500,
                usa_only=True,
                notes="Free API. USA-centric archive.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Smithsonian media-gallery provider."""
        from urbanlens.dashboard.services.apis.assets.smithsonian import SmithsonianGateway
        from urbanlens.UrbanLens.settings.app import settings

        return [MediaPanelSource("smithsonian", SmithsonianGateway.service_key, lambda: SmithsonianGateway(api_key=settings.smithsonian_api_key or ""))]


class WikimediaPlugin(UrbanLensPlugin):
    """Wikimedia Commons media for pinned locations."""

    name: ClassVar[str] = "wikimedia"
    verbose_name: ClassVar[str] = "Wikimedia Commons"
    description: ClassVar[str] = "Adds Wikimedia Commons media to the pin detail page's Media gallery."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Wikimedia Commons API."""
        return {
            "wikimedia": ServiceDefaults(
                display_name="Wikimedia Commons",
                calls_per_minute=30,
                calls_per_day=1000,
                notes="Free API.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Wikimedia Commons media-gallery provider."""
        from urbanlens.dashboard.services.apis.assets.wikimedia import WikimediaGateway

        return [MediaPanelSource("wikimedia", WikimediaGateway.service_key, WikimediaGateway)]


class LibraryOfCongressPlugin(UrbanLensPlugin):
    """Library of Congress media for pinned locations."""

    name: ClassVar[str] = "library_of_congress"
    verbose_name: ClassVar[str] = "Library of Congress"
    description: ClassVar[str] = "Adds Library of Congress archive media to the pin detail page's Media gallery. USA-centric."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Library of Congress API."""
        return {
            "library_of_congress": ServiceDefaults(
                display_name="Library of Congress",
                calls_per_minute=10,
                calls_per_day=200,
                usa_only=True,
                notes="Free API. USA-centric archive.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Library of Congress media-gallery provider."""
        from urbanlens.dashboard.services.apis.assets.loc import LOCJsonGateway

        return [MediaPanelSource("loc", LOCJsonGateway.service_key, LOCJsonGateway)]
