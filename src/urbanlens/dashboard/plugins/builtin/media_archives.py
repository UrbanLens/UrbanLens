"""Media archive plugins: providers for the Private Pin page's combined Media gallery.
Each plugin contributes one :class:`~urbanlens.dashboard.services.pins.external_data.DocumentMediaPanelSource`, which the gallery fetches independently so a slow provider never blocks the others.
The books and scans a provider finds are listed under Article > Sources; a REData archive's only from what its gallery search cached (``fetched_for_sources=False``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import DocumentMediaPanelSource
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class SmithsonianPlugin(UrbanLensPlugin):
    """Smithsonian Open Access media for pinned locations."""

    name: ClassVar[str] = "smithsonian"
    verbose_name: ClassVar[str] = "Smithsonian Open Access"
    description: ClassVar[str] = "Adds Smithsonian Open Access archive media to the Private Pin page's Media gallery. USA-centric. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Smithsonian media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import SmithsonianMediaProvider

        return [DocumentMediaPanelSource("smithsonian", SmithsonianMediaProvider.service_key, SmithsonianMediaProvider, fetched_for_sources=False)]


class WikimediaPlugin(UrbanLensPlugin):
    """Wikimedia Commons media for pinned locations."""

    name: ClassVar[str] = "wikimedia"
    verbose_name: ClassVar[str] = "Wikimedia Commons"
    description: ClassVar[str] = "Adds Wikimedia Commons media to the Private Pin page's Media gallery."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Wikimedia Commons API."""
        return {
            "wikimedia": ServiceDefaults(
                display_name="Wikimedia Commons",
                category=EgressCategory.QUOTA,
                calls_per_minute=30,
                calls_per_day=1000,
                notes="Free API.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Wikimedia Commons media-gallery provider; the books and scans it finds go to Article > Sources."""
        from urbanlens.dashboard.services.apis.assets.wikimedia import WikimediaGateway

        return [DocumentMediaPanelSource("wikimedia", WikimediaGateway.service_key, WikimediaGateway)]


class LibraryOfCongressPlugin(UrbanLensPlugin):
    """Library of Congress media for pinned locations."""

    name: ClassVar[str] = "library_of_congress"
    verbose_name: ClassVar[str] = "Library of Congress"
    description: ClassVar[str] = "Adds Library of Congress archive media to the Private Pin page's Media gallery. USA-centric. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Library of Congress media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import LibraryOfCongressMediaProvider

        return [DocumentMediaPanelSource("loc", LibraryOfCongressMediaProvider.service_key, LibraryOfCongressMediaProvider, fetched_for_sources=False)]


class DigitalCommonwealthPlugin(UrbanLensPlugin):
    """Digital Commonwealth media for pinned locations. Massachusetts only."""

    name: ClassVar[str] = "digital_commonwealth"
    verbose_name: ClassVar[str] = "Digital Commonwealth"
    description: ClassVar[str] = "Photographs, maps, and documents from Massachusetts libraries, museums, and archives for the Private Pin page's Media gallery. Massachusetts pins only. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Digital Commonwealth media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import DigitalCommonwealthMediaProvider

        return [DocumentMediaPanelSource("digital_commonwealth", DigitalCommonwealthMediaProvider.service_key, DigitalCommonwealthMediaProvider, fetched_for_sources=False)]


class InternetArchivePlugin(UrbanLensPlugin):
    """Internet Archive media for pinned locations."""

    name: ClassVar[str] = "internet_archive"
    verbose_name: ClassVar[str] = "Internet Archive"
    description: ClassVar[str] = "Free, open-source full-text/media search across archive.org's books, photos, newspapers, and recordings for the Private Pin page's Media gallery. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Internet Archive media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import InternetArchiveMediaProvider

        return [DocumentMediaPanelSource("internet_archive", InternetArchiveMediaProvider.service_key, InternetArchiveMediaProvider, fetched_for_sources=False)]


class ChroniclingAmericaPlugin(UrbanLensPlugin):
    """Historic newspaper coverage for pinned locations."""

    name: ClassVar[str] = "chronicling_america"
    verbose_name: ClassVar[str] = "Historic Newspapers (Chronicling America)"
    description: ClassVar[str] = "Adds dated historic-newspaper pages (Library of Congress Chronicling America, 1794-1963) to the Private Pin page's Media gallery. USA only. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Chronicling America media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import ChroniclingAmericaMediaProvider

        return [DocumentMediaPanelSource("chronicling_america", ChroniclingAmericaMediaProvider.service_key, ChroniclingAmericaMediaProvider, fetched_for_sources=False)]
