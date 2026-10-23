from typing import NamedTuple, overload

from urbanlens.dashboard.models.abstract.choices import TextChoices


class MapLayerMode(TextChoices):
    """
    Base tile layer selected for a MarkupMap viewport.

    Values match the canonical keys used by the shared frontend layers engine
    (``frontend/ts/shared/map-layers.ts``); use :func:`normalize_layer_mode`
    when ingesting client or historical data.
    """

    STREET = "street", "Street"
    SATELLITE = "satellite", "Satellite"
    TOPOGRAPHIC = "topographic", "Topographic"
    DARK = "dark", "Dark"


#: Historical layer-mode identifiers (pre-canonical DB values and old client
#: snapshots) mapped to their canonical replacements. Mirrors the alias table
#: in frontend/ts/shared/map-layers.ts.
LEGACY_LAYER_MODE_ALIASES: dict[str, str] = {
    "standard": MapLayerMode.STREET.value,
    "osm": MapLayerMode.STREET.value,
    "topo": MapLayerMode.TOPOGRAPHIC.value,
    "terrain": MapLayerMode.TOPOGRAPHIC.value,
}


@overload
def normalize_layer_mode(value: object, default: str = ...) -> str: ...
@overload
def normalize_layer_mode(value: object, default: None) -> str | None: ...
def normalize_layer_mode(value: object, default: str | None = MapLayerMode.STREET.value) -> str | None:
    """
    Normalize a layer-mode identifier to a canonical MapLayerMode value.
    Accepts canonical values as-is and maps legacy aliases ("standard", "topo", ...) still present in old client snapshots.

    Args:
        value: The candidate layer-mode identifier.
        default: Returned when ``value`` is unrecognized. Pass None to detect
            invalid input instead of falling back.

    Returns:
        A canonical MapLayerMode value, or ``default`` when unrecognized.
    """
    if isinstance(value, str):
        candidate = value.lower()
        if candidate in MapLayerMode.values:
            return candidate
        if candidate in LEGACY_LAYER_MODE_ALIASES:
            return LEGACY_LAYER_MODE_ALIASES[candidate]
    return default


class MarkupType(TextChoices):
    """The visual kind of map annotation."""

    LINE = "line", "Line"
    ARROW = "arrow", "Arrow"
    TEXT = "text", "Text"
    SQUARE = "square", "Square"
    CIRCLE = "circle", "Circle"
    POLYGON = "polygon", "Polygon"
    PIN = "pin", "Pin"


class SecurityIndicatorType(TextChoices):
    """Optional security feature represented by this markup item."""

    FENCE = "fence", "Fence"
    CAMERA = "camera", "Camera"
    ALARM = "alarm", "Alarm"
    SECURITY = "security", "Security Guard"
    SIGN = "sign", "Sign"
    PLYWOOD = "plywood", "Plywood"
    LOCKED = "locked", "Locked"
    VPS = "vps", "VPS"


#: Curated Material Symbols icon choices offered for a CustomLayer's button
#: (mirrors the small, purpose-picked list used for detail_pin_icon_choices
#: in controllers/pin.py, rather than Label's much larger emoji ICON_CATEGORIES).
CUSTOM_LAYER_ICON_CHOICES = [
    ("layers", "Layers"),
    ("route", "Route"),
    ("terrain", "Terrain"),
    ("warning", "Warning"),
    ("explore", "Explore"),
    ("place", "Place"),
    ("hiking", "Hiking"),
    ("construction", "Construction"),
    ("water", "Water"),
    ("landscape", "Landscape"),
]


class MarkupMapHost(NamedTuple):
    """One reverse relation through which a host model attaches a :class:`MarkupMap`."""

    relation: str
    kind: str
    select_related: tuple[str, ...] = ()


#: Hosts that hold a map as their own: a map linked from any of them is not a draft, so it cannot be
#: adopted by another host. A new FK to MarkupMap belongs here or in the secondary list below.
EXCLUSIVE_MARKUP_MAP_HOSTS: tuple[MarkupMapHost, ...] = (
    MarkupMapHost("safety_checkins", "safety_checkin"),
    MarkupMapHost("comments", "comment", ("pin", "wiki__location")),
    MarkupMapHost("trip_comments", "trip_comment", ("trip",)),
    MarkupMapHost("visits", "visit", ("pin",)),
    MarkupMapHost("direct_messages", "direct_message", ("sender__user", "recipient__user")),
    MarkupMapHost("pin_share_attachments", "pin_share", ("to_profile__user",)),
    MarkupMapHost("pin_list", "pin_list"),
)

#: Hosts that reference a map without owning it (``SafetyCheckin.markup_maps``, a map-reference
#: custom field). They still make it attached, since adopting a reference map as a check-in's route
#: would show it to emergency contacts it was never meant for.
SECONDARY_MARKUP_MAP_HOSTS: tuple[MarkupMapHost, ...] = (
    MarkupMapHost("attached_safety_checkins", "safety_checkin"),
    MarkupMapHost("custom_field_references", "custom_field_reference", ("field", "pin")),
)

MARKUP_MAP_HOSTS: tuple[MarkupMapHost, ...] = EXCLUSIVE_MARKUP_MAP_HOSTS + SECONDARY_MARKUP_MAP_HOSTS
