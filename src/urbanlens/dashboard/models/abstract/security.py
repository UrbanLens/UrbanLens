from __future__ import annotations

from django.db.models.fields import CharField

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.abstract.model import Model


class SecurityModel(Model):
    """Adds security indicators to a model."""

    fences = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    alarms = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    cameras = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    security = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    signs = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    vps = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    plywood = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    locked = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)

    class Meta(Model.Meta):
        abstract = True
