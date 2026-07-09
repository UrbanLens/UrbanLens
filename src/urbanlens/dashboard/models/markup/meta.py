from urbanlens.dashboard.models.abstract.choices import TextChoices


class MapLayerMode(TextChoices):
    """Base tile layer selected for a MarkupMap viewport."""

    STANDARD = "standard", "Street"
    SATELLITE = "satellite", "Satellite"
    TOPO = "topo", "Topographic"
    DARK = "dark", "Dark"


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
