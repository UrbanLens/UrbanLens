from __future__ import annotations

from django.db.models.fields import CharField

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.abstract.model import DashboardModel

#: (field_name, display_label) pairs, in the order shown throughout the UI
#: (pin edit dialog, pin overview card, and the map filter panel).
SECURITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("fences", "Fences"),
    ("alarms", "Alarms"),
    ("cameras", "Cameras"),
    ("security", "Security"),
    ("signs", "Signs"),
    ("vps", "VPS"),
    ("plywood", "Plywood"),
    ("locked", "Locked"),
)


class SecurityModel(DashboardModel):
    """Adds security indicators to a model."""

    fences = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    alarms = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    cameras = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    security = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    signs = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    vps = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    plywood = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    locked = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)

    class Meta(DashboardModel.Meta):
        abstract = True
