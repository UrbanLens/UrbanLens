from urbanlens.dashboard.models.abstract.choices import TextChoices


class MapLayerMode(TextChoices):
    """Base tile layer selected for a MarkupMap viewport.

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


def normalize_layer_mode(value: object, default: str | None = MapLayerMode.STREET.value) -> str | None:
    """Normalize a layer-mode identifier to a canonical MapLayerMode value.

    Accepts canonical values as-is and maps legacy aliases ("standard",
    "topo", ...) still present in old client snapshots.

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
