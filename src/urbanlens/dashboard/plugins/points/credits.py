"""Data sources the home and credits pages list, and the categories they are grouped under."""

from __future__ import annotations

from dataclasses import dataclass

from urbanlens.dashboard.plugins.extensions import ExtensionPoint, Registry


@dataclass(frozen=True, slots=True)
class DataSourceCategory:
    """
    A group of data sources the home and credits pages show together.

    Attributes:
        title: The group's heading.
        icon: A Material Symbols name.
        description: One sentence on what the sources supply.
        order: Where the group falls among the others; lower comes first.
    """

    title: str
    icon: str
    description: str
    order: int


@dataclass(frozen=True, slots=True)
class MapAttribution:
    """
    The attribution a map provider asks for, shown on the credits page as a map card.

    Attributes:
        icon: A Material Symbols name for the card.
        text: The attribution line, ending where ``terms_label`` begins.
        terms_label: The text of the link to the provider's terms or licence.
        terms_url: Where that link goes.
    """

    icon: str
    text: str
    terms_label: str
    terms_url: str


@dataclass(frozen=True, slots=True)
class DataSourceCredit:
    """
    One external data source a plugin serves.

    Attributes:
        name: The source's display name.
        category: The key of the category it is listed under; a source whose key no category has is not listed.
        url: Where the source is documented or credited; empty for none.
        note: One sentence on what the source supplies.
        attribution: Set, the source is a map card carrying this attribution instead of a line in its category.
    """

    name: str
    category: str
    url: str = ""
    note: str = ""
    attribution: MapAttribution | None = None


DATA_SOURCE_CATEGORIES: Registry[str, DataSourceCategory] = Registry(
    "credits.data_source_categories",
    "Categories the home and credits pages group data sources under, by key. Core's are registered at the default priority; a plugin whose sources fit one better registers the same key at a lower priority number to replace its title, icon and description.",
)

DATA_SOURCE_CATEGORIES.update(
    {
        "reference": DataSourceCategory(
            title="Reference & media",
            icon="menu_book",
            description="Reference articles and freely licensed media matched to each site by name and coordinates.",
            order=10,
        ),
        "places": DataSourceCategory(
            title="Places & street view",
            icon="place",
            description="Place names, address lookup and street-level imagery for a location.",
            order=20,
        ),
        "open_map": DataSourceCategory(
            title="Open map data",
            icon="map",
            description="Community-maintained map data and reverse geocoding that fill in addresses and nearby features.",
            order=30,
        ),
        "archives": DataSourceCategory(
            title="Historical archives",
            icon="museum",
            description="Photographs, collection records and historical maps from archives that help reconstruct a site's past.",
            order=40,
        ),
        "search": DataSourceCategory(
            title="Search",
            icon="travel_explore",
            description="Web search results for news and mentions.",
            order=50,
        ),
        "planning": DataSourceCategory(
            title="Planning context",
            icon="wb_sunny",
            description="Forecasts, map overlays and regional data for planning a trip.",
            order=60,
        ),
    },
)

DATA_SOURCE_CREDITS: ExtensionPoint[DataSourceCredit] = ExtensionPoint(
    "credits.data_sources",
    "Data sources the home and credits pages credit. A plugin adds each source it calls or serves, under a category key; a source with no contribution is not credited.",
)

__all__ = ["DATA_SOURCE_CATEGORIES", "DATA_SOURCE_CREDITS", "DataSourceCategory", "DataSourceCredit", "MapAttribution"]
