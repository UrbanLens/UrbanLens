"""Reusable map UI components shared by every Leaflet map on the site.

The main map (``pages/map/index.html``) defines the canonical look and
behavior for the layers panel and the "jump to location" search bar. These
inclusion tags render that exact markup for any map page, and the shared
JavaScript engines bind to it by data attributes:

* ``{% map_layers_panel %}`` pairs with ``window.MapLayers.create(...)``
  (``frontend/ts/shared/map-layers.ts``).
* ``{% map_search_bar %}`` pairs with ``window.LocationSearchEngine.attach(...)``
  (``frontend/ts/shared/location-search-engine.ts``).

Because the markup and the JS are single-sourced, every map is guaranteed to
present layers and search identically. New layers (e.g. from plugins) can be
added with `register_map_layer`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django import template

register = template.Library()


@dataclass(frozen=True)
class MapLayerSpec:
    """Declarative description of one layer button in the layers panel.

    Attributes:
        key: Stable identifier bound to ``data-map-layer`` and matched by the
            JS engine (``street``, ``terrain``, ``satellite``, ...).
        kind: How the JS engine treats the button: ``base`` (mutually
            exclusive tile layers), ``overlay`` (independent tile overlays),
            ``action`` (e.g. dark mode), or ``custom`` (page-registered
            toggles such as pins or photos).
        label: Short label shown under the thumbnail.
        aria_label: Accessible name for the button.
        tooltip: Tooltip text (may include the keyboard shortcut hint).
        thumb: Static path of the thumbnail image, or empty to fall back to
            ``icon``.
        thumb_alt: Alt text for the thumbnail image.
        icon: Material Symbols icon name used in the compact strip variant
            and as the thumbnail fallback.
        button_id: Explicit DOM id (kept stable for tests/automation).
    """

    key: str
    kind: str
    label: str
    aria_label: str
    tooltip: str
    icon: str
    thumb: str = ""
    thumb_alt: str = ""
    button_id: str = field(default="")

    def __post_init__(self) -> None:
        """Default ``button_id`` to ``<key>-layer-button`` when not provided."""
        if not self.button_id:
            object.__setattr__(self, "button_id", f"{self.key}-layer-button")


#: Registry of every known layer button, keyed by ``MapLayerSpec.key``.
#: The button ids and copy for street/terrain/satellite/weather/pins/dark/
#: borders/places are preserved verbatim from the original main-map markup.
MAP_LAYER_REGISTRY: dict[str, MapLayerSpec] = {}


def register_map_layer(spec: MapLayerSpec) -> MapLayerSpec:
    """Register a layer button so templates can request it by key.

    Plugins may call this at import time to contribute new layer buttons;
    pages opt in by listing the key in their ``{% map_layers_panel %}`` call
    and registering a matching custom toggle with the JS engine.

    Args:
        spec: The layer description to register.

    Returns:
        The registered spec (for chaining/inspection).
    """
    MAP_LAYER_REGISTRY[spec.key] = spec
    return spec


register_map_layer(
    MapLayerSpec(
        key="street",
        kind="base",
        label="Street",
        aria_label="Street Map",
        tooltip="Street map",
        icon="map",
        thumb="dashboard/images/map_layer_street.jpg",
        thumb_alt="Street Layer",
        button_id="street-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="terrain",
        kind="base",
        label="Terrain",
        aria_label="Topography Map",
        tooltip="Terrain map (T)",
        icon="terrain",
        thumb="dashboard/images/map_layer_topography.jpg",
        thumb_alt="Terrain Layer",
        button_id="topography-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="satellite",
        kind="base",
        label="Satellite",
        aria_label="Satellite Map",
        tooltip="Satellite map (S)",
        icon="globe",
        thumb="dashboard/images/map_layer_satellite.jpg",
        thumb_alt="Satellite Imagery Layer",
        button_id="satellite-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="weather",
        kind="overlay",
        label="Weather",
        aria_label="Weather overlay",
        tooltip="Weather overlay (W)",
        icon="rainy",
        thumb="dashboard/images/map_layer_weather.jpg",
        thumb_alt="Weather Layer",
        button_id="weather-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="pins",
        kind="custom",
        label="Pins",
        aria_label="Show or hide pins",
        tooltip="Show or hide pins (P)",
        icon="location_on",
        thumb="dashboard/images/map_layer_pins.jpg",
        thumb_alt="Pins Layer",
        button_id="toggle-pins-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="childpins",
        kind="custom",
        label="Sub Pins",
        aria_label="Show or hide sub pins",
        tooltip="Show pins nested inside other pins",
        icon="subdirectory_arrow_right",
        button_id="child-pins-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="dark",
        kind="action",
        label="Dark",
        aria_label="Toggle Dark Mode",
        tooltip="Dark mode (D)",
        icon="dark_mode",
        thumb="dashboard/images/map_layer_dark.jpg",
        thumb_alt="Dark Mode",
        button_id="dark-mode-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="borders",
        kind="overlay",
        label="Borders",
        aria_label="Toggle Geopolitical boundaries",
        tooltip="Borders layer (B)",
        icon="public",
        thumb="dashboard/images/map_layer_borders.jpg",
        thumb_alt="Borders Layer",
        button_id="boundaries-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="places",
        kind="custom",
        label="Places",
        aria_label="Toggle Places layer",
        tooltip="Places layer",
        icon="travel_explore",
        thumb="dashboard/images/map_layer_places.jpg",
        thumb_alt="Places Layer",
        button_id="places-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="details",
        kind="custom",
        label="Markup",
        aria_label="Show or hide markup and additional pins",
        tooltip="Markup and additional pins",
        icon="layers",
        thumb="dashboard/images/map_layer_markup.webp",
        thumb_alt="Markup Layer",
        button_id="details-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="photos",
        kind="custom",
        label="Photos",
        aria_label="Show or hide photos",
        tooltip="Photos",
        icon="photo_library",
        thumb="dashboard/images/map_layer_photos.webp",
        thumb_alt="Photos Layer",
        button_id="photos-button",
    )
)


@register.inclusion_tag("dashboard/partials/map/_layers_panel.html")
def map_layers_panel(
    layers: str = "street,terrain,satellite,weather,dark,borders",
    variant: str = "panel",
    panel_id: str = "map-layers-panel",
    extra_class: str = "",
) -> dict[str, Any]:
    """Render the shared map layers component.

    Args:
        layers: Comma-separated layer keys from :data:`MAP_LAYER_REGISTRY`,
            in display order. Unknown keys are ignored so a page can list a
            plugin layer that may not be installed.
        variant: ``panel`` for the main-map flyout (thumbnails, opens from a
            Layers toggle) or ``strip`` for the compact icon row used inside
            dialogs (e.g. the comment map composer).
        panel_id: DOM id of the component root.
        extra_class: Extra CSS classes for the root (e.g.
            ``map-layers-panel--inline`` to dock it in a
            ``.map-bottom-controls`` row).

    Returns:
        Context for ``partials/map/_layers_panel.html``.
    """
    keys = [k.strip() for k in layers.split(",") if k.strip()]
    buttons = [MAP_LAYER_REGISTRY[k] for k in keys if k in MAP_LAYER_REGISTRY]
    return {
        "buttons": buttons,
        "variant": variant,
        "panel_id": panel_id,
        "extra_class": extra_class,
    }


@register.inclusion_tag("dashboard/partials/map/_search_bar.html")
def map_search_bar(
    prefix: str = "addr",
    placeholder: str = "Search pins, addresses, coordinates...",
    show_history: bool = True,
    extra_class: str = "",
) -> dict[str, Any]:
    """Render the shared "jump to location" search bar.

    The emitted ids follow the ``{prefix}-search-*`` scheme consumed by
    ``window.LocationSearchEngine.attach(prefix, options)``.

    Args:
        prefix: Id prefix; must be unique per page.
        placeholder: Input placeholder text.
        show_history: Whether to render the search-history button.
        extra_class: Extra CSS classes for the bar element.

    Returns:
        Context for ``partials/map/_search_bar.html``.
    """
    return {
        "prefix": prefix,
        "placeholder": placeholder,
        "show_history": show_history,
        "extra_class": extra_class,
    }
