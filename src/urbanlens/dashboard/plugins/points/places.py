"""Pin, place and media data: panels, imagery, names, enrichment, labels and photo keywords."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint

if TYPE_CHECKING:
    from collections.abc import Callable

    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider, StreetViewProvider
    from urbanlens.dashboard.services.labels.matching import LabelMatcher
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.photos.photo_keywords import PhotoKeywordProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource


PANEL_SOURCES: ExtensionPoint[PanelSource] = ExtensionPoint(
    "pins.panel_sources",
    "External-data panels on the private pin and wiki pages.",
)

SATELLITE_PROVIDERS: ExtensionPoint[Callable[[], SatelliteViewProvider]] = ExtensionPoint(
    "imagery.satellite_providers",
    "Factories for the satellite-imagery carousel's providers, called each time the chain runs.",
)

STREET_VIEW_PROVIDERS: ExtensionPoint[Callable[[], StreetViewProvider]] = ExtensionPoint(
    "imagery.street_view_providers",
    "Factories for the street-level imagery carousel's providers, called each time the chain runs.",
)

NAME_PROVIDERS: ExtensionPoint[NameProvider] = ExtensionPoint(
    "locations.name_providers",
    "Place-name candidate providers; priority order breaks ties among unprioritized candidates.",
)

ENRICHMENT_SOURCES: ExtensionPoint[EnrichmentSource] = ExtensionPoint(
    "locations.enrichment_sources",
    "Background-enrichment sources the scheduled enrichment task runs, in priority order.",
)

LABEL_MATCHERS: ExtensionPoint[LabelMatcher] = ExtensionPoint(
    "labels.matchers",
    "Auto-tagging stages, asked in priority order to choose labels from those earlier stages left. Core's keyword matcher runs at 50.",
)

PHOTO_KEYWORD_PROVIDERS: ExtensionPoint[PhotoKeywordProvider] = ExtensionPoint(
    "photos.keyword_providers",
    "Photo keywording strategies; each stores its own keywords per image.",
)

__all__ = ["ENRICHMENT_SOURCES", "LABEL_MATCHERS", "NAME_PROVIDERS", "PANEL_SOURCES", "PHOTO_KEYWORD_PROVIDERS", "SATELLITE_PROVIDERS", "STREET_VIEW_PROVIDERS"]
