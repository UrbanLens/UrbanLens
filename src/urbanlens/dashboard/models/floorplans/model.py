"""Interior floorplans for individual buildings - floors, rooms, elements, locks.

This mirrors REData's floorplan schema deliberately, shape for shape (see
``../REData/src/redata/parcels/models/floorplan/``): REData owns the eventual
aggregation of external floorplan sources, while UrbanLens holds what users
author by hand. Keeping the structures identical means one document format,
one editor, and a future push/pull between the two that is a field-copy, not
a translation. Local deviations are strictly additive: ``place``/``pin``/
``profile`` anchor a plan into UrbanLens's own graph, and every item can carry
the owner's ``labels`` - both invisible to the upstream shape.

The design decisions inherited from that schema, restated:

- **One generic element table.** Walls, windows, doors, stairs, fixtures and
  keys differ in ``kind``, not schema: optional geometry, material, condition,
  build date, description, provenance. Kind-specific detail (glazing, swing
  direction) rides in ``attributes``.
- **Openings mount on surfaces via a self-FK** (``mounted_on``): a window or
  door can sit in a wall, a floor, a ceiling or a roof, and "which surface"
  is a relationship, not a coordinate.
- **Locks are their own table**: a door has zero-to-many, and
  ``key_attributes`` is data a consumer matches keys against, not prose.
- **Versioning is whole-document**: a layout change is a new ``Floorplan``
  with a later ``valid_from``. "The floorplan as of 1954" is the query.
- **Sources and references are per-plan pools** every item points into, so
  ten walls traced from one scanned drawing share one source row and one
  photo can evidence a wall, its door, and the door's lock at once.
- **Geometry is WGS-84** so every element lands directly on the same map as
  parcels and footprints; the vertical dimension is explicit numeric columns,
  not 3D geometry.

Floorplans are absent by default and never load with a building - most
buildings will never have one, and the common case pays nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.db.models import GeometryField
from django.db.models import (
    CASCADE,
    PROTECT,
    SET_NULL,
    DateField,
    FloatField,
    ForeignKey,
    ManyToManyField,
    TextChoices,
    URLField,
)
from django.db.models.fields import CharField, IntegerField, SmallIntegerField, TextField
from django.db.models.fields.json import JSONField

from urbanlens.dashboard.models import abstract

from .queryset import FloorplanManager


class FloorplanElementKind(TextChoices):
    """What a :class:`FloorplanElement` physically is. Mirrors REData's enum."""

    WALL = "wall", "Wall"
    FLOOR = "floor", "Floor surface"
    CEILING = "ceiling", "Ceiling"
    ROOF = "roof", "Roof"
    COLUMN = "column", "Column"
    WINDOW = "window", "Window"
    DOOR = "door", "Door"
    STAIR = "stair", "Stair"
    FIXTURE = "fixture", "Fixture"
    KEY = "key", "Key"
    OTHER = "other", "Other"


class FloorplanReferenceKind(TextChoices):
    """The media type of a :class:`FloorplanReference`. Mirrors REData's enum."""

    PHOTO = "photo", "Photo"
    PDF = "pdf", "PDF"
    VIDEO = "video", "Video"
    DOCUMENT = "document", "Document"
    MODEL = "model", "3D model / CAD file"
    OTHER = "other", "Other"


class FloorplanItem(abstract.FrontendDashboardModel):
    """Fields shared by every floorplan row - the document's "item" surface.

    Attributes:
        description: Free text about this item.
        condition: Current state ("intact", "collapsed", "sealed").
        built_date: When this item was built or installed.
        attributes: Anything else a producer knows, unmerged and unjudged.
        source: Where this item's information came from (plan's source pool).
        references: Media showing this item (plan's reference pool).
        labels: The owner's labels - an UrbanLens addition, absent upstream.
    """

    description = TextField(blank=True, default="")
    condition = CharField(max_length=255, blank=True, default="")
    built_date = DateField(null=True, blank=True)
    attributes = JSONField(default=dict, blank=True)
    source = ForeignKey("dashboard.FloorplanSource", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    references = ManyToManyField("dashboard.FloorplanReference", blank=True, related_name="%(class)ss")
    labels = ManyToManyField("dashboard.Label", blank=True, related_name="floorplan_%(class)ss")

    class Meta(abstract.FrontendDashboardModel.Meta):
        abstract = True


class Floorplan(FloorplanItem):
    """One dated version of one building's floorplan.

    Attributes:
        place: The building place this plan describes (UrbanLens anchor).
        pin: The pin it was authored from, when personal.
        profile: The local author; null for plans mirrored from upstream.
        building_ref: REData's reconciliation ref for the building, when
            known - the identity a future push/pull correlates on.
        building_name: Free-text fallback naming the building.
        name: Version label ("As built", "After the 1962 fire").
        valid_from: The date this version takes effect; null is the original
            baseline, in force from the beginning of time.
        floor_count: What a source says the building has - may exceed the
            floors actually modelled.
    """

    place = ForeignKey("dashboard.Place", on_delete=PROTECT, related_name="floorplans")
    pin = ForeignKey("dashboard.Pin", on_delete=SET_NULL, null=True, blank=True, related_name="floorplans")
    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, null=True, blank=True, related_name="floorplans")
    building_ref = CharField(max_length=255, blank=True, default="")
    building_name = CharField(max_length=255, blank=True, default="")
    name = CharField(max_length=255, blank=True, default="")
    valid_from = DateField(null=True, blank=True)
    floor_count = IntegerField(null=True, blank=True)

    objects = FloorplanManager()

    if TYPE_CHECKING:
        place_id: int
        profile_id: int | None

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplans"

    def __str__(self) -> str:
        stamp = self.valid_from.isoformat() if self.valid_from else "baseline"
        return f"{self.name or 'Floorplan'} ({stamp})"


class FloorplanSource(abstract.FrontendDashboardModel):
    """One provenance record in a plan's source pool.

    Any combination of a URL, a stored file, a note and an author - as thin
    as "measured on site, 2019 visit" or as concrete as a scanned HABS sheet.

    Attributes:
        floorplan: The plan whose pool this row belongs to.
        title: Display label for the source.
        url: Link to the origin.
        file: A stored document (PDF page, image) the fact was read from.
        note: Free-text explanation.
        author: Who produced or reported the information.
        attributes: Producer-specific extras.
    """

    floorplan = ForeignKey(Floorplan, on_delete=CASCADE, related_name="source_pool")
    title = CharField(max_length=255, blank=True, default="")
    url = URLField(max_length=1000, blank=True, default="")
    file = ForeignKey("dashboard.Image", on_delete=SET_NULL, null=True, blank=True, related_name="floorplan_sources")
    note = TextField(blank=True, default="")
    author = CharField(max_length=255, blank=True, default="")
    attributes = JSONField(default=dict, blank=True)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_sources"

    def __str__(self) -> str:
        return self.title or self.author or self.url or f"source {self.pk}"


class FloorplanReference(abstract.FrontendDashboardModel):
    """A photo, PDF, video, document or model in a plan's reference pool.

    Attributes:
        floorplan: The plan whose pool this row belongs to.
        kind: Media kind, for icon/preview selection.
        title: Display label.
        url: External media URL, when not stored locally.
        image: A stored ``Image`` row (photos, scanned pages).
        description: What this media shows.
        attributes: Producer-specific extras.
    """

    floorplan = ForeignKey(Floorplan, on_delete=CASCADE, related_name="reference_pool")
    kind = CharField(max_length=16, choices=FloorplanReferenceKind.choices, default=FloorplanReferenceKind.OTHER)
    title = CharField(max_length=255, blank=True, default="")
    url = URLField(max_length=1000, blank=True, default="")
    image = ForeignKey("dashboard.Image", on_delete=CASCADE, null=True, blank=True, related_name="floorplan_references")
    description = TextField(blank=True, default="")
    attributes = JSONField(default=dict, blank=True)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_references"

    def __str__(self) -> str:
        return self.title or self.url or f"reference {self.pk}"


class FloorplanFloor(FloorplanItem):
    """One storey of the plan.

    Attributes:
        floorplan: The plan version this floor belongs to.
        level: Storey number - 0 is ground, negative below grade.
        name: Display label ("Ground floor", "Mezzanine").
        geometry: The storey's outline in world coordinates.
        elevation_meters: The walking surface's height above sea level.
        height_meters: Floor-to-ceiling height.
    """

    floorplan = ForeignKey(Floorplan, on_delete=CASCADE, related_name="floors")
    level = SmallIntegerField(default=0)
    name = CharField(max_length=255, blank=True, default="")
    geometry = GeometryField(srid=4326, null=True, blank=True)
    elevation_meters = FloatField(null=True, blank=True)
    height_meters = FloatField(null=True, blank=True)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_floors"
        ordering = ("level",)

    def __str__(self) -> str:
        return self.name or f"Level {self.level}"


class FloorplanRoom(FloorplanItem):
    """A named space on a floor.

    Attributes:
        floor: The floor this room is on.
        name: The room's name ("Ward B", "Boiler room").
        geometry: The room's outline in world coordinates.
        height_meters: Ceiling height inside this room, when it differs.
    """

    floor = ForeignKey(FloorplanFloor, on_delete=CASCADE, related_name="rooms")
    name = CharField(max_length=255, blank=True, default="")
    geometry = GeometryField(srid=4326, null=True, blank=True)
    height_meters = FloatField(null=True, blank=True)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_rooms"
        ordering = ("id",)

    def __str__(self) -> str:
        return self.name or f"room {self.pk}"


class FloorplanElement(FloorplanItem):
    """A physical thing on the plan: wall, window, door, stair, fixture, key.

    Attributes:
        floorplan: The plan this element belongs to.
        floor: The floor it stands on; null for plan-level elements (a key).
        kind: What it physically is.
        name: Display label ("North stair", "Vault door").
        geometry: Its shape or position, at whatever fidelity the producer
            works - Point, LineString or Polygon. Null for geometry-less
            elements (keys).
        material: What it is made of.
        room: The room it belongs to or opens into, when known.
        mounted_on: The surface element an opening sits in - a window's wall,
            a hatch's floor, a skylight's roof.
        base_elevation_meters: Bottom of the element above its floor's
            walking surface (a window's sill height).
        height_meters: The element's own height.
    """

    floorplan = ForeignKey(Floorplan, on_delete=CASCADE, related_name="elements")
    floor = ForeignKey(FloorplanFloor, on_delete=CASCADE, null=True, blank=True, related_name="elements")
    kind = CharField(max_length=16, choices=FloorplanElementKind.choices)
    name = CharField(max_length=255, blank=True, default="")
    geometry = GeometryField(srid=4326, null=True, blank=True)
    material = CharField(max_length=255, blank=True, default="")
    room = ForeignKey(FloorplanRoom, on_delete=SET_NULL, null=True, blank=True, related_name="elements")
    mounted_on = ForeignKey("self", on_delete=SET_NULL, null=True, blank=True, related_name="mounted_elements")
    base_elevation_meters = FloatField(null=True, blank=True)
    height_meters = FloatField(null=True, blank=True)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_elements"
        ordering = ("id",)

    def __str__(self) -> str:
        return self.name or f"{self.kind} {self.pk}"


class FloorplanLock(FloorplanItem):
    """One lock on an element; a door may carry many or none.

    Attributes:
        element: The element (usually a door) this lock secures.
        name: Free-form label/type ("padlock", "deadbolt", "chain").
        key_attributes: What opens it, as data a consumer can match keys
            against - producer-defined shape ("bitting", "brand", "keyway").
    """

    element = ForeignKey(FloorplanElement, on_delete=CASCADE, related_name="locks")
    name = CharField(max_length=255, blank=True, default="")
    key_attributes = JSONField(default=dict, blank=True)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_locks"
        ordering = ("id",)

    def __str__(self) -> str:
        return self.name or f"lock {self.pk}"
