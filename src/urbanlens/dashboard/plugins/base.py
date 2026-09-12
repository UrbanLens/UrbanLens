"""Base class for UrbanLens plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.hooks import HookRegistry
    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider, StreetViewProvider
    from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.photos.photo_keywords import PhotoKeywordProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class UrbanLensPlugin:
    """One pluggable integration: metadata plus typed contribution points.
    Subclasses set ``name`` (a unique slug) and override whichever contribution methods apply - every contribution is optional, so this is deliberately a plain class rather than an ABC.

    Attributes:
        name: Unique plugin slug (e.g. ``"nps"``).
        verbose_name: Human-readable name shown in the admin UI.
        description: One-or-two sentence summary for the admin UI.
        version: Plugin version string.
        author: Plugin author, shown in the admin UI.
        order: Sort key for aggregated contributions (e.g. the order imagery providers appear in a carousel)."""

    name: ClassVar[str] = ""
    verbose_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0"
    author: ClassVar[str] = ""
    order: ClassVar[int] = 100

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Default rate-limit configuration for this plugin's service keys.

        Returns:
            Mapping of service key to its defaults; empty when the plugin makes no rate-limited API calls."""
        return {}

    def get_panel_sources(self) -> list[PanelSource]:
        """Pin-detail external-data panels contributed by this plugin.

        Returns:
            PanelSource instances to add to the panel registry; empty when the plugin contributes no panels."""
        return []

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Satellite-imagery providers for the pin-detail satellite carousel.
        Called each time the carousel's provider chain runs, so returning freshly constructed gateway instances is expected.

        Returns:
            Provider gateway instances; empty when the plugin contributes no satellite imagery."""
        return []

    def get_name_providers(self) -> list[NameProvider]:
        """Place-name candidate providers contributed by this plugin.

        Returns:
            NameProvider instances; empty when the plugin contributes no place names."""
        return []

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Street-level imagery providers for the pin-detail street carousel.
        Called each time the carousel's provider chain runs, so returning freshly constructed gateway instances is expected.

        Returns:
            Provider gateway instances; empty when the plugin contributes no street-level imagery."""
        return []

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Background-enrichment sources contributed by this plugin.
        Each source tracks its own per-location completion, so contributing one never affects when another provider runs.

        Returns:
            EnrichmentSource instances; empty when the plugin contributes no background enrichment."""
        return []

    def get_photo_keyword_providers(self) -> list[PhotoKeywordProvider]:
        """Photo keywording strategies contributed by this plugin.

        Returns:
            PhotoKeywordProvider instances; empty when the plugin contributes no photo keywording."""
        return []

    def register(self, hooks: HookRegistry) -> None:
        """Attach action/filter callbacks to the shared hook bus."""
