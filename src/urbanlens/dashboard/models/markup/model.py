"""Markup models - reusable MarkupMap containers and PinMarkup annotation items."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from django.core.validators import MaxLengthValidator
from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
    CharField,
    FloatField,
    ForeignKey,
    Index,
    IntegerField,
    JSONField,
    TextField,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.markup.meta import MapLayerMode, MarkupType, SecurityIndicatorType, normalize_layer_mode
from urbanlens.dashboard.models.markup.queryset import MarkupMapManager, PinMarkupManager
from urbanlens.dashboard.services.text_limits import MAX_MARKUP_LABEL_LENGTH

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.safety.model import SafetyCheckin
    from urbanlens.dashboard.models.trips.model import TripComment
    from urbanlens.dashboard.models.visits.model import PinVisit

logger = logging.getLogger(__name__)

# Metres of latitude per degree - used to convert a circle radius back into an
# edge coordinate when round-tripping between GeoJSON-ish geometry and the
# client snapshot format.
_METERS_PER_DEGREE_LAT = 111_320.0


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the great-circle distance between two points in metres.

    Args:
        lat1: Latitude of the first point.
        lng1: Longitude of the first point.
        lat2: Latitude of the second point.
        lng2: Longitude of the second point.

    Returns:
        Distance in metres.
    """
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


class MarkupMap(abstract.FrontendDashboardModel):
    """A standalone, reusable map view with user-drawn markup on it.

    A MarkupMap owns a saved viewport (centre, zoom, base layer, borders
    overlay) plus a set of :class:`PinMarkup` items (``items`` related name).
    Other models attach a map by holding a nullable FK to it - safety
    check-ins (``SafetyCheckin.markup_map``), comments (``Comment.markup_map``
    / ``TripComment.markup_map``), and pin visits (``PinVisit.markup_map``) -
    so a map can exist before its host does (e.g. drawn on the check-in
    creation page and linked once the check-in is created) and can be reused
    or managed independently on the Memories > Maps page.

    Attributes:
        uuid: Stable public identifier (used in URLs).
        profile: The user who owns this map.
        title: Optional display label shown on the Memories > Maps page.
        center_latitude: Saved viewport centre latitude.
        center_longitude: Saved viewport centre longitude.
        zoom: Saved viewport zoom level.
        layer_mode: Base tile layer (street / satellite / topographic / dark).
        show_borders: Whether the geopolitical-borders overlay is enabled.
    """

    title = CharField(max_length=200, blank=True, default="")
    center_latitude = FloatField(null=True, blank=True)
    center_longitude = FloatField(null=True, blank=True)
    zoom = FloatField(null=True, blank=True)
    layer_mode = CharField(max_length=20, choices=MapLayerMode.choices, default=MapLayerMode.STREET)
    show_borders = BooleanField(default=False)

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="markup_maps",
    )
    # The map this one was cloned from via "Add to my maps", if any. SET_NULL
    # so a clone's provenance badge can fall back to `shared_by` even after
    # the original map is deleted.
    cloned_from = ForeignKey(
        "self",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="clones",
    )
    # The profile who most recently sent this map to its current owner (via a
    # DM attachment, a standalone map share, or a pin-share attachment) at the
    # time it was cloned. Denormalized from the share event rather than
    # derived from `cloned_from.profile` so the "From X" badge survives even
    # if the source map is later deleted, and so a forwarded chain always
    # shows the immediate sender rather than the original creator.
    shared_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    objects = MarkupMapManager()

    if TYPE_CHECKING:
        profile_id: int
        cloned_from_id: int | None
        shared_by_id: int | None
        items: QuerySet[PinMarkup]
        safety_checkins: QuerySet[SafetyCheckin]
        attached_safety_checkins: QuerySet[SafetyCheckin]
        comments: QuerySet[Comment]
        trip_comments: QuerySet[TripComment]
        visits: QuerySet[PinVisit]
        clones: QuerySet[MarkupMap]

    @property
    def attachment(self) -> tuple[str, Any] | None:
        """Return the first host object this map is attached to, if any.

        Returns:
            Tuple of (kind, instance) where kind is one of ``safety_checkin``
            / ``comment`` / ``trip_comment`` / ``visit``, or None when the map
            is unattached (a draft).
        """
        checkin = self.safety_checkins.first()
        if checkin is not None:
            return ("safety_checkin", checkin)
        comment = self.comments.select_related("pin", "wiki__location").first()
        if comment is not None:
            return ("comment", comment)
        trip_comment = self.trip_comments.select_related("trip").first()
        if trip_comment is not None:
            return ("trip_comment", trip_comment)
        visit = self.visits.select_related("pin").first()
        if visit is not None:
            return ("visit", visit)
        # Secondary (many-to-many) safety check-in attachments, checked last since a map
        # attached this way is meant to be reusable across hosts, unlike the exclusive
        # relations above - see SafetyCheckin.markup_maps.
        attached_checkin = self.attached_safety_checkins.first()
        if attached_checkin is not None:
            return ("safety_checkin", attached_checkin)
        return None

    @property
    def is_attached(self) -> bool:
        """Whether any host model currently links to this map.

        Returns:
            True when a safety check-in, comment, trip comment, or visit
            references this map.
        """
        return self.attachment is not None

    def to_snapshot(self) -> dict:
        """Serialize this map (viewport + items) into the client snapshot format.

        The snapshot format is the JSON schema the shared frontend map
        composer/viewer speaks: ``{center_lat, center_lng, zoom, layer_mode,
        show_borders, markup: [shape, ...]}`` with shapes carrying
        ``latlngs`` as ``[lat, lng]`` pairs. It is embedded into templates
        (via ``json_script``) for read-only rendering and prefilled into the
        composer when editing.

        Returns:
            Snapshot dict, always with valid centre coordinates (falls back
            to 0,0 when the viewport was never saved).
        """
        # .all() (not .order_by()) so a prefetched items cache is reused; the
        # model's default ordering is already ["created"].
        shapes = [shape for shape in (item.to_snapshot_shape() for item in self.items.all()) if shape is not None]
        return {
            "center_lat": self.center_latitude if self.center_latitude is not None else 0.0,
            "center_lng": self.center_longitude if self.center_longitude is not None else 0.0,
            "zoom": self.zoom if self.zoom is not None else 13,
            "layer_mode": self.layer_mode,
            "show_borders": self.show_borders,
            "markup": shapes,
        }

    def replace_items_from_snapshot(self, snapshot: dict) -> None:
        """Replace this map's viewport and items with a sanitized snapshot's content.

        Args:
            snapshot: A snapshot dict already validated by
                ``services.map_snapshot.sanitize_map_data``.
        """
        self.center_latitude = snapshot.get("center_lat")
        self.center_longitude = snapshot.get("center_lng")
        self.zoom = snapshot.get("zoom")
        self.layer_mode = normalize_layer_mode(snapshot.get("layer_mode"))
        self.show_borders = bool(snapshot.get("show_borders"))
        self.save()
        self.items.all().delete()
        for shape in snapshot.get("markup") or []:
            item = PinMarkup.from_snapshot_shape(shape)
            if item is None:
                continue
            item.parent_map = self
            item.profile_id = self.profile_id
            item.save()

    def __str__(self) -> str:
        """Return a human-readable description of this map.

        Returns:
            String like "<title> (profile <id>)".
        """
        return f"{self.title or 'Untitled map'} (profile {self.profile_id})"

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_markup_maps"
        ordering = ["-updated"]
        indexes = [
            Index(fields=["uuid"], name="idxdb_mm_uuid"),
            Index(fields=["profile"], name="idxdb_mm_profile"),
            Index(fields=["profile", "cloned_from"], name="idxdb_mm_profile_clonedfrom"),
        ]


class PinMarkup(abstract.FrontendDashboardModel):
    """A map annotation attached to a user's Pin, a Wiki page, or a MarkupMap.

    Markup items let users annotate a map view with lines, arrows, text
    labels, and geometric shapes (squares, circles, free polygons).

    Exactly one of ``parent_pin`` / ``parent_wiki`` / ``parent_map`` is set,
    mirroring how ``Pin`` itself distinguishes a personal detail pin
    (``parent_pin`` set) from a community detail pin (``parent_wiki`` set).
    Pin-scoped markup is personal (only the owning profile can see/edit it,
    rendered on the "Markup" layer in the pin detail map); Wiki-scoped
    markup is shared community data, editable by any signed-in user, rendered
    on the wiki map; map-scoped markup belongs to a standalone
    :class:`MarkupMap` (safety check-in routes, comment maps, visit maps).

    Attributes:
        uuid: Stable public identifier (used in URLs).
        parent_pin: The Pin whose detail map shows this annotation, if personal.
        parent_wiki: The Wiki whose map shows this annotation, if shared.
        parent_map: The MarkupMap this annotation belongs to, if map-scoped.
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

    markup_type = CharField(max_length=20, choices=MarkupType.choices)
    geometry = JSONField()
    label = TextField(blank=True, default="", max_length=MAX_MARKUP_LABEL_LENGTH, validators=[MaxLengthValidator(MAX_MARKUP_LABEL_LENGTH)])
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
    parent_wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="markup_items",
    )
    parent_map = ForeignKey(
        MarkupMap,
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="items",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="markup_items",
    )

    if TYPE_CHECKING:
        parent_pin_id: int | None
        parent_wiki_id: int | None
        parent_map_id: int | None
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

    def to_snapshot_shape(self) -> dict | None:
        """Convert this item into the client snapshot shape format.

        The snapshot format carries ``latlngs`` as ``[lat, lng]`` pairs and
        uses ``rect`` for squares - it is what ``MarkupEngine.renderShape``
        consumes on read-only comment/visit/memories map renders.

        Returns:
            Shape dict, or None when the stored geometry is malformed.
        """
        geometry = self.geometry if isinstance(self.geometry, dict) else {}
        coordinates = geometry.get("coordinates")
        if coordinates is None:
            return None
        shape: dict = {
            "type": "rect" if self.markup_type == MarkupType.SQUARE else self.markup_type,
            "color": self.color,
            "stroke_width": self.stroke_width,
            "fill_opacity": self.fill_opacity,
            "border_opacity": self.border_opacity,
        }
        if self.border_color:
            shape["border_color"] = self.border_color
        if self.label:
            shape["label"] = self.label

        try:
            if self.markup_type in (MarkupType.LINE, MarkupType.ARROW):
                shape["latlngs"] = [[c[1], c[0]] for c in coordinates]
            elif self.markup_type == MarkupType.TEXT:
                latlngs = [[coordinates[1], coordinates[0]]]
                box_corner = geometry.get("box_corner")
                if box_corner:
                    latlngs.append([box_corner[1], box_corner[0]])
                shape["latlngs"] = latlngs
                shape["label"] = self.label or ""
            elif self.markup_type == MarkupType.CIRCLE:
                lng, lat = coordinates
                radius = float(geometry.get("radius") or 0)
                # Recreate an "edge" point due east of the centre so the
                # two-point client circle format round-trips the radius.
                dlng = radius / (_METERS_PER_DEGREE_LAT * max(math.cos(math.radians(lat)), 1e-6))
                shape["latlngs"] = [[lat, lng], [lat, lng + dlng]]
            elif self.markup_type == MarkupType.SQUARE:
                ring = coordinates[0]
                shape["latlngs"] = [[ring[0][1], ring[0][0]], [ring[2][1], ring[2][0]]]
            elif self.markup_type == MarkupType.POLYGON:
                ring = coordinates[0]
                # Drop the GeoJSON closing point - the client re-closes polygons.
                points = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
                shape["latlngs"] = [[c[1], c[0]] for c in points]
            elif self.markup_type == MarkupType.PIN:
                shape["latlngs"] = [[coordinates[1], coordinates[0]]]
            else:
                return None
        except (TypeError, IndexError, ValueError):
            logger.warning("Malformed geometry on PinMarkup %s; skipping snapshot conversion", self.pk)
            return None
        if not shape.get("latlngs"):
            return None
        return shape

    @classmethod
    def from_snapshot_shape(cls, shape: dict) -> PinMarkup | None:
        """Build an (unsaved) PinMarkup from a sanitized client snapshot shape.

        Args:
            shape: A shape dict already cleaned by
                ``services.map_snapshot._sanitize_markup_shapes`` -
                ``latlngs`` are ``[lat, lng]`` pairs.

        Returns:
            Unsaved PinMarkup instance (no parent/profile set), or None when
            the shape cannot be converted.
        """
        shape_type = shape.get("type")
        latlngs = shape.get("latlngs") or []
        geometry: dict | None = None
        markup_type: str | None = None

        if shape_type in ("line", "arrow") and len(latlngs) >= 2:
            markup_type = shape_type
            geometry = {"type": "LineString", "coordinates": [[ll[1], ll[0]] for ll in latlngs]}
        elif shape_type == "text" and latlngs:
            markup_type = MarkupType.TEXT
            geometry = {"type": "Point", "coordinates": [latlngs[0][1], latlngs[0][0]]}
            if len(latlngs) > 1:
                geometry["box_corner"] = [latlngs[1][1], latlngs[1][0]]
        elif shape_type == "circle" and len(latlngs) >= 2:
            markup_type = MarkupType.CIRCLE
            (lat1, lng1), (lat2, lng2) = latlngs[0], latlngs[1]
            geometry = {
                "type": "Circle",
                "coordinates": [lng1, lat1],
                "radius": _haversine_meters(lat1, lng1, lat2, lng2),
            }
        elif shape_type == "rect" and len(latlngs) >= 2:
            markup_type = MarkupType.SQUARE
            north, west = latlngs[0]
            south, east = latlngs[1]
            geometry = {
                "type": "Polygon",
                "coordinates": [[[west, north], [east, north], [east, south], [west, south], [west, north]]],
            }
        elif shape_type == "polygon" and len(latlngs) >= 3:
            markup_type = MarkupType.POLYGON
            ring = [[ll[1], ll[0]] for ll in latlngs]
            ring.append(ring[0])
            geometry = {"type": "Polygon", "coordinates": [ring]}
        elif shape_type == "pin" and latlngs:
            markup_type = MarkupType.PIN
            geometry = {"type": "Point", "coordinates": [latlngs[0][1], latlngs[0][0]]}

        if markup_type is None or geometry is None:
            return None

        stroke_width = shape.get("stroke_width")
        return cls(
            markup_type=markup_type,
            geometry=geometry,
            label=str(shape.get("label") or ""),
            color=shape.get("color") or "#e53e3e",
            stroke_width=int(stroke_width) if stroke_width is not None else (16 if markup_type == MarkupType.TEXT else 3),
            border_color=shape.get("border_color") or "",
            fill_opacity=int(shape.get("fill_opacity", 87)),
            border_opacity=int(shape.get("border_opacity", 100)),
        )

    def __str__(self) -> str:
        if self.parent_pin_id:
            owner = f"pin={self.parent_pin_id}"
        elif self.parent_wiki_id:
            owner = f"wiki={self.parent_wiki_id}"
        else:
            owner = f"map={self.parent_map_id}"
        return f"{self.markup_type}: {self.label or '(unlabelled)'} [{owner}]"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_markup"
        ordering = ["created"]
        indexes = [
            Index(fields=["parent_pin"], name="idxdb_pm_pin"),
            Index(fields=["parent_wiki"], name="idxdb_pm_wiki"),
            Index(fields=["parent_map"], name="idxdb_pm_map"),
            Index(fields=["profile"], name="idxdb_pm_profile"),
        ]
