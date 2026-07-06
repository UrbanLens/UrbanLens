"""PinMarkup model - map annotations (lines, arrows, text) attached to a Pin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import (
    CASCADE,
    CharField,
    ForeignKey,
    Index,
    IntegerField,
    JSONField,
    TextField,
    UUIDField,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.markup.meta import MarkupType, SecurityIndicatorType
from urbanlens.dashboard.models.markup.queryset import PinMarkupManager

logger = logging.getLogger(__name__)


class PinMarkup(abstract.Model):
    """A map annotation attached to a user's Pin, or shared on a Location's wiki.

    Markup items let users annotate a map view with lines, arrows, text
    labels, and geometric shapes (squares, circles, free polygons).

    Exactly one of ``parent_pin`` / ``parent_location`` is set, mirroring how
    ``Pin`` itself distinguishes a personal detail pin (``parent_pin`` set)
    from a community detail pin (``parent_location`` set). Pin-scoped markup
    is personal (only the owning profile can see/edit it, rendered on the
    "Markup" layer in the pin detail map); Location-scoped markup is shared
    community data, editable by any signed-in user, rendered on the wiki map.

    Attributes:
        uuid: Stable public identifier (used in URLs).
        parent_pin: The Pin whose detail map shows this annotation, if personal.
        parent_location: The Location whose wiki map shows this annotation, if shared.
        profile: The user who created this annotation.
        markup_type: One of line / arrow / text / square / circle / polygon.
        geometry: GeoJSON-style geometry dict.
            - LineString for line/arrow
            - Point for text
            - Polygon for square/polygon
            - {"type":"Circle","coordinates":[lng,lat],"radius":meters} for circle
        label: Display text; optional for all types.
        color: Primary CSS hex colour (fill for shapes, text colour for text type).
        stroke_width: Line thickness in pixels; doubles as font size for text.
        border_color: Secondary colour - outline/stroke for shapes and lines;
            background colour for text labels. Empty string means use the
            renderer default. The sentinel value ``"none"`` means no border /
            transparent background.
        fill_opacity: Fill/text opacity as a 0-100 integer (percent).
        border_opacity: Border/background opacity as a 0-100 integer (percent).
    """

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    markup_type = CharField(max_length=20, choices=MarkupType.choices)
    geometry = JSONField()
    label = TextField(blank=True, default="")
    color = CharField(max_length=20, blank=True, default="#e53e3e")
    stroke_width = IntegerField(default=3)
    border_color = CharField(max_length=20, blank=True, default="")
    fill_opacity = IntegerField(default=87)
    border_opacity = IntegerField(default=100)
    security_indicator = CharField(
        max_length=20,
        blank=True,
        default="",
        choices=SecurityIndicatorType.choices,
    )

    parent_pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="markup_items",
    )
    parent_location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="markup_items",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="markup_items",
    )

    if TYPE_CHECKING:
        parent_pin_id: int | None
        parent_location_id: int | None
        profile_id: int

    objects = PinMarkupManager()

    def to_json(self) -> dict:
        """Compact serialisation for Leaflet rendering.

        Returns:
            dict with uuid, markup_type, geometry, label, color, stroke_width,
            border_color.
        """
        return {
            "uuid": str(self.uuid),
            "markup_type": self.markup_type,
            "geometry": self.geometry,
            "label": self.label,
            "color": self.color,
            "stroke_width": self.stroke_width,
            "border_color": self.border_color,
            "fill_opacity": self.fill_opacity,
            "border_opacity": self.border_opacity,
            "security_indicator": self.security_indicator,
        }

    def __str__(self) -> str:
        owner = f"pin={self.parent_pin_id}" if self.parent_pin_id else f"location={self.parent_location_id}"
        return f"{self.markup_type}: {self.label or '(unlabelled)'} [{owner}]"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_pin_markup"
        ordering = ["created"]
        indexes = [
            Index(fields=["parent_pin"], name="idxdb_pm_pin"),
            Index(fields=["parent_location"], name="idxdb_pm_location"),
            Index(fields=["profile"], name="idxdb_pm_profile"),
        ]
