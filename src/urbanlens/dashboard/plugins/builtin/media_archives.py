"""Media archive plugins: providers for the pin detail page's combined Media gallery.

Each plugin contributes one :class:`~urbanlens.dashboard.services.pins.external_data.MediaPanelSource`,
which the gallery fetches independently so a slow provider never blocks the
others.

Smithsonian, Library of Congress and Internet Archive are all now REData-backed
(``services.apis.locations.redata_reference_documents_gateway`` - see that
module's docstring) and no longer call their archive directly. Wikimedia
Commons is intentionally untouched: REData's ``/reference-documents/search/``
has no ``wikimedia``/``wikimedia_commons`` provider today, so there is nothing
to migrate it to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import MediaPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class SmithsonianPlugin(UrbanLensPlugin):
    """Smithsonian Open Access media for pinned locations."""

    name: ClassVar[str] = "smithsonian"
    verbose_name: ClassVar[str] = "Smithsonian Open Access"
    description: ClassVar[str] = "Adds Smithsonian Open Access archive media to the pin detail page's Media gallery. USA-centric. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Smithsonian media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import SmithsonianMediaProvider

        return [MediaPanelSource("smithsonian", SmithsonianMediaProvider.service_key, SmithsonianMediaProvider)]


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
    description: ClassVar[str] = "Adds Library of Congress archive media to the pin detail page's Media gallery. USA-centric. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Library of Congress media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import LibraryOfCongressMediaProvider

        return [MediaPanelSource("loc", LibraryOfCongressMediaProvider.service_key, LibraryOfCongressMediaProvider)]


class DigitalCommonwealthPlugin(UrbanLensPlugin):
    """Digital Commonwealth media for pinned locations. Massachusetts only."""

    name: ClassVar[str] = "digital_commonwealth"
    verbose_name: ClassVar[str] = "Digital Commonwealth"
    description: ClassVar[str] = "Photographs, maps, and documents from Massachusetts libraries, museums, and archives for the pin detail page's Media gallery. Massachusetts pins only. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Digital Commonwealth media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import DigitalCommonwealthMediaProvider

        return [MediaPanelSource("digital_commonwealth", DigitalCommonwealthMediaProvider.service_key, DigitalCommonwealthMediaProvider)]


class InternetArchivePlugin(UrbanLensPlugin):
    """Internet Archive media for pinned locations."""

    name: ClassVar[str] = "internet_archive"
    verbose_name: ClassVar[str] = "Internet Archive"
    description: ClassVar[str] = "Free, open-source full-text/media search across archive.org's books, photos, newspapers, and recordings for the pin detail page's Media gallery. Via REData."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Internet Archive media-gallery provider."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import InternetArchiveMediaProvider

        return [MediaPanelSource("internet_archive", InternetArchiveMediaProvider.service_key, InternetArchiveMediaProvider)]
