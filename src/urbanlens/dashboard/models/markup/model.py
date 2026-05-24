"""PinMarkup model — map annotations (lines, arrows, text) attached to a Pin."""

from __future__ import annotations

import logging
from uuid import uuid4

from django.db.models import CASCADE, CharField, ForeignKey, Index, IntegerField, JSONField, TextChoices, TextField, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.markup.queryset import PinMarkupManager

logger = logging.getLogger(__name__)


class MarkupType(TextChoices):
    """The visual kind of map annotation."""

    LINE = "line", "Line"
    ARROW = "arrow", "Arrow"
    TEXT = "text", "Text"


class PinMarkup(abstract.Model):
    """A map annotation (line, arrow, or text label) attached to a user's Pin.

    Markup items let users annotate a pin's map view with:
    - Lines: polylines showing routes, paths, or areas of interest.
    - Arrows: directed polylines highlighting directions or access points.
    - Text: freestanding labels placed at map coordinates.

    All markup is personal (scoped to the owning profile) and rendered on the
    "Markup" layer in the pin detail map.

    Attributes:
        uuid: Stable public identifier (used in URLs).
        parent_pin: The Pin whose detail map shows this annotation.
        profile: The user who created this annotation.
        markup_type: One of line / arrow / text.
        geometry: GeoJSON geometry dict — LineString for line/arrow, Point for text.
        label: Display text; required for text type, optional for lines/arrows.
        color: CSS hex colour applied to the stroke/fill.
        stroke_width: Line thickness in pixels (also used as font size for text).
    """

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    parent_pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="markup_items",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="markup_items",
    )
    markup_type = CharField(max_length=20, choices=MarkupType.choices)
    geometry = JSONField()
    label = TextField(blank=True, default="")
    color = CharField(max_length=20, blank=True, default="#e53e3e")
    stroke_width = IntegerField(default=3)

    objects = PinMarkupManager()

    def to_json(self) -> dict:
        """Compact serialisation for Leaflet rendering.

        Returns:
            dict with uuid, markup_type, geometry, label, color, stroke_width.
        """
        return {
            "uuid": str(self.uuid),
            "markup_type": self.markup_type,
            "geometry": self.geometry,
            "label": self.label,
            "color": self.color,
            "stroke_width": self.stroke_width,
        }

    def __str__(self) -> str:
        return f"{self.markup_type}: {self.label or '(unlabelled)'} [{self.parent_pin_id}]"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_pin_markup"
        ordering = ["created"]
        indexes = [
            Index(fields=["parent_pin"], name="dashboard_pm_pin_idx"),
            Index(fields=["profile"], name="dashboard_pm_profile_idx"),
        ]
