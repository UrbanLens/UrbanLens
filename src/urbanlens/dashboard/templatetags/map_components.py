"""Reusable map UI components shared by every Leaflet map on the site.

The main map (``pages/map/index.html``) defines the canonical look and
behavior for the layers panel and the "jump to location" search bar. These
inclusion tags render that exact markup for any map page, and the shared
JavaScript engines bind to it by data attributes:

* ``{% map_layers_panel %}`` pairs with ``window.MapLayers.create(...)``
  (``frontend/ts/shared/map-layers.ts``).
* ``{% map_search_bar %}`` pairs with ``window.LocationSearchEngine.attach(...)``
  (``frontend/ts/shared/location-search-engine.ts``).
* ``{% map_toolbar %}`` renders the top-right tool icon row (screenshot,
  and - on the main map - add/import/search/select) and pairs with
  ``window._openMapToolbarScreenshot(...)`` (``themes/base.html``).

Because the markup and the JS are single-sourced, every map is guaranteed to
present layers and search identically. New layers (e.g. from plugins) can be
added with `register_map_layer`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from django import template
from django.urls import reverse

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
        thumb="dashboard/images/map_layer_child_pins.jpg",
        thumb_alt="Child Pins Layer",
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
register_map_layer(
    MapLayerSpec(
        key="nearby",
        kind="custom",
        label="Nearby Pins",
        aria_label="Show or hide the user's other nearby pins",
        tooltip="Show your other pins near this location",
        icon="share_location",
        thumb="dashboard/images/map_layer_nearby_pins.jpg",
        thumb_alt="Nearby Pins Layer",
        button_id="nearby-pins-button",
    )
)
register_map_layer(
    MapLayerSpec(
        key="past_activities",
        kind="custom",
        label="Past Activities",
        aria_label="Show or hide past activities",
        tooltip="Show completed/past activities on the map",
        icon="history",
        thumb="dashboard/images/map_layer_previous_pins.jpg",
        thumb_alt="Previous Pins Layer",
        button_id="past-activities-button",
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
    show_geolocate: bool = False,
    extra_class: str = "",
) -> dict[str, Any]:
    """Render the shared "jump to location" search bar.

    The emitted ids follow the ``{prefix}-search-*`` scheme consumed by
    ``window.LocationSearchEngine.attach(prefix, options)``.

    Args:
        prefix: Id prefix; must be unique per page.
        placeholder: Input placeholder text.
        show_history: Whether to render the search-history button.
        show_geolocate: Whether to render the "center on my location" button.
            Callers should only pass True when the profile's Live Location
            history setting is enabled - this tag has no profile access of
            its own to check that.
        extra_class: Extra CSS classes for the bar element.

    Returns:
        Context for ``partials/map/_search_bar.html``.
    """
    return {
        "prefix": prefix,
        "placeholder": placeholder,
        "show_history": show_history,
        "show_geolocate": show_geolocate,
        "extra_class": extra_class,
    }


@dataclass(frozen=True)
class MapToolSpec:
    """Declarative description of one top-right toolbar button.

    Attributes:
        key: Stable identifier matched against the ``tools`` argument of
            :func:`map_toolbar`.
        icon: Material Symbols icon name.
        aria_label: Accessible name for the button.
        tooltip: Tooltip text (may include a keyboard shortcut hint).
        tooltip_pos: Tooltip placement (``""`` for float-only, or ``"below"``).
        button_id: Explicit DOM id (kept stable for tests/automation).
        onclick: JS expression for a plain ``onclick`` handler. Ignored for
            ``hx_get_name`` buttons; filled in dynamically for ``screenshot``
            (see :func:`map_toolbar`).
        hx_get_name: URL name to reverse for an ``hx-get`` button, or ``""``.
        hx_target: ``hx-target`` selector, paired with ``hx_get_name``.
        hx_swap: ``hx-swap`` value, paired with ``hx_get_name``.
        extra_html: Extra raw HTML rendered inside the button (e.g. a label).
    """

    key: str
    icon: str
    aria_label: str
    tooltip: str
    tooltip_pos: str = ""
    button_id: str = field(default="")
    onclick: str = ""
    hx_get_name: str = ""
    hx_target: str = ""
    hx_swap: str = ""
    extra_html: str = ""

    def __post_init__(self) -> None:
        """Default ``button_id`` to ``<key>-map-tool-button`` when not provided."""
        if not self.button_id:
            object.__setattr__(self, "button_id", f"{self.key}-map-tool-button")


#: Registry of every known toolbar tool, keyed by ``MapToolSpec.key``. The
#: button ids and copy for add_pin/import/search/select/screenshot are
#: preserved verbatim from the original main-map toolbar markup.
MAP_TOOL_REGISTRY: dict[str, MapToolSpec] = {}


def register_map_tool(spec: MapToolSpec) -> MapToolSpec:
    """Register a toolbar tool so templates can request it by key.

    Args:
        spec: The tool description to register.

    Returns:
        The registered spec (for chaining/inspection).
    """
    MAP_TOOL_REGISTRY[spec.key] = spec
    return spec


register_map_tool(
    MapToolSpec(
        key="add_pin",
        icon="add_location",
        aria_label="Add pin",
        tooltip="Drop a new pin on the map",
        button_id="add-pin-button",
        onclick="openAddPinDialog()",
    )
)
register_map_tool(
    MapToolSpec(
        key="import",
        icon="upload",
        aria_label="Import pins",
        tooltip="Import pins from Google Takeout",
        tooltip_pos="below",
        button_id="import-pins-button",
        hx_get_name="pin.import.form",
        hx_target="#importPinsModal",
        hx_swap="innerHTML",
    )
)
register_map_tool(
    MapToolSpec(
        key="search",
        icon="search",
        aria_label="Filter and search pins",
        tooltip="Filter pins by name, rating, visits, labels, and more (F)",
        tooltip_pos="below",
        button_id="search-pins-button",
        onclick="toggleFilterPanel()",
        extra_html='<span class="fp-active-label" id="fp-active-label" aria-hidden="true" hidden></span>',
    )
)
register_map_tool(
    MapToolSpec(
        key="select",
        icon="check_box",
        aria_label="Select pins",
        tooltip="Select multiple pins to merge, edit, or delete",
        tooltip_pos="below",
        button_id="select-pins-button",
        onclick="toggleSelectMode()",
    )
)
register_map_tool(
    MapToolSpec(
        key="select_detail_pins",
        icon="check_box",
        aria_label="Select sub pins",
        tooltip="Select multiple sub pins to promote or delete",
        tooltip_pos="below",
        button_id="select-detail-pins-button",
        onclick="toggleDetailPinSelectMode()",
    )
)
register_map_tool(
    MapToolSpec(
        key="screenshot",
        icon="photo_camera",
        aria_label="Take a screenshot",
        tooltip="Take a screenshot of the map",
        tooltip_pos="below",
        button_id="screenshot-map-button",
        # onclick is filled in per-call by map_toolbar() - it needs the
        # calling page's map instance and (optionally) context.
    )
)

# -- Markup drawing tools (pin detail, Location wiki, safety check-in maps) ----
# Formerly a dropdown ("Add Detail") docked in .map-bottom-controls; now plain
# icon buttons in the top-right toolbar like every other map tool. Onclick
# handlers are the same window globals createMarkupToolbar() exposes (see
# ts/shared/markup-toolbar.ts and _markup_panel_dialog.html).
register_map_tool(
    MapToolSpec(
        key="markup_pin",
        icon="place",
        aria_label="Add a detail pin",
        tooltip="Add a detail pin",
        tooltip_pos="below",
        button_id="markup-pin-button",
        onclick="openAddPinDialog()",
    )
)
register_map_tool(
    MapToolSpec(
        key="markup_line",
        icon="show_chart",
        aria_label="Draw a line",
        tooltip="Draw a line",
        tooltip_pos="below",
        button_id="markup-line-button",
        onclick="startMarkupDraw('line')",
    )
)
register_map_tool(
    MapToolSpec(
        key="markup_arrow",
        icon="arrow_forward",
        aria_label="Draw an arrow",
        tooltip="Draw an arrow",
        tooltip_pos="below",
        button_id="markup-arrow-button",
        onclick="startMarkupDraw('arrow')",
    )
)
register_map_tool(
    MapToolSpec(
        key="markup_text",
        icon="title",
        aria_label="Add a text label",
        tooltip="Add a text label",
        tooltip_pos="below",
        button_id="markup-text-button",
        onclick="startTextPlacement()",
    )
)
register_map_tool(
    MapToolSpec(
        key="markup_square",
        icon="crop_square",
        aria_label="Draw a square",
        tooltip="Draw a square",
        tooltip_pos="below",
        button_id="markup-square-button",
        onclick="startShapeDraw('square')",
    )
)
register_map_tool(
    MapToolSpec(
        key="markup_circle",
        icon="circle",
        aria_label="Draw a circle",
        tooltip="Draw a circle",
        tooltip_pos="below",
        button_id="markup-circle-button",
        onclick="startShapeDraw('circle')",
    )
)
register_map_tool(
    MapToolSpec(
        key="markup_polygon",
        icon="format_shapes",
        aria_label="Draw a polygon",
        tooltip="Draw a polygon",
        tooltip_pos="below",
        button_id="markup-polygon-button",
        onclick="startShapeDraw('polygon')",
    )
)


@register.inclusion_tag("dashboard/partials/map/_map_toolbar.html")
def map_toolbar(
    tools: str = "screenshot",
    panel_id: str = "map-buttons",
    map_var: str = "window.map",
    screenshot_context: str = "null",
    screenshot_onclick: str = "",
    screenshot_trip_name: str = "",
) -> dict[str, Any]:
    """Render the shared top-right map toolbar (tools only).

    Every map on the site renders this exact markup so the screenshot tool -
    and any future toolbar tool - behaves identically everywhere: same
    placement, same collapse behavior, same underlying screenshot code path
    (``window._openMapToolbarScreenshot``, ``themes/base.html``).

    Args:
        tools: Comma-separated tool keys from :data:`MAP_TOOL_REGISTRY`, in
            display order. Unknown keys are ignored.
        panel_id: DOM id of the toolbar root. Must be unique per page.
        map_var: JS expression evaluating to the page's Leaflet map instance
            (e.g. ``"window.map"``), used to seed the screenshot composer's
            initial view. Ignored if ``screenshot_onclick`` is given.
        screenshot_context: Raw JS expression (e.g. an object literal)
            passed as the composer's ``context`` option, or ``"null"``.
        screenshot_onclick: Full ``onclick`` override for the screenshot
            button, for pages that need bespoke logic (e.g. resolving
            context from a JS config object) instead of the generic
            ``_openMapToolbarScreenshot(map_var, context)`` call.
        screenshot_trip_name: When set, overrides ``screenshot_context`` with
            ``{tripName: ...}`` (JSON-encoded, so it round-trips safely
            through the auto-escaped HTML attribute) so the composer can
            suggest a title based on the trip instead of reverse-geocoding
            the map view.

    Returns:
        Context for ``partials/map/_map_toolbar.html``.
    """
    keys = [k.strip() for k in tools.split(",") if k.strip()]
    if screenshot_trip_name:
        screenshot_context = json.dumps({"tripName": screenshot_trip_name})
    buttons: list[dict[str, Any]] = []
    for key in keys:
        spec = MAP_TOOL_REGISTRY.get(key)
        if not spec:
            continue
        onclick = spec.onclick
        if key == "screenshot":
            onclick = screenshot_onclick or f"_openMapToolbarScreenshot({map_var}, {screenshot_context})"
        buttons.append(
            {
                "spec": spec,
                "hx_get": reverse(spec.hx_get_name) if spec.hx_get_name else "",
                "onclick": onclick,
            }
        )
    return {
        "panel_id": panel_id,
        "buttons": buttons,
    }
