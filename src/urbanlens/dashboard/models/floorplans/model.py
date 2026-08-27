"""Interior floorplans: walls are the geometry, everything else hangs off them.

**Walls are the only stored shape.** A room is not a polygon - it is a named
point that binds, at render time, to whichever enclosed region of the wall
graph contains it (see ``frontend/ts/shared/floorplan/planar.ts``). That is
Revit's room model, and it is chosen for two properties this application needs:

- **One source of truth for a shared partition.** When two rooms sit either
  side of a wall, that wall exists once. Room-polygon models duplicate it, and
  the two copies drift apart the first time somebody drags one.
- **Geometry edits cannot destroy room identity.** Moving a wall changes which
  region a seed sits in, never whether the room, its name, its photos or its
  labels still exist. A seed left outside every enclosed region is a legible
  "not enclosed yet" state, not data loss.

**Doors and windows are intervals along a wall**, not free-standing objects
with their own coordinates. An opening cannot outlive the wall it is cut into,
moves when that wall moves, and cannot drift off it - all three are properties
of the schema here rather than rules some editor has to remember. **Locks hang
off the opening** for the same reason: which door is locked and what opens it
is field data worth keeping, and a lock outliving its door would be a fact
about nothing.

**Geometry is plan-local metres, not WGS-84.** A degree of longitude is ~74 km
at this latitude and ~111 km at the equator, so in degrees x and y are
different units: lengths, angles, right-angle snapping and area all need a
latitude correction applied at every step, and every place that forgets is a
subtly wrong drawing. Storing metres east/north of one per-plan origin makes
the arithmetic ordinary and makes the tolerances the editor reasons about
("within 0.15 m") expressible as themselves. The origin lives on the plan, not
the floor, because a shared origin across every floor is what lets storeys be
stacked and aligned at all. Conversion to WGS-84 happens at the edges, in
``services.floorplans.features`` for map rendering.

**Versioning is whole-document**: a layout change is a new ``Floorplan`` with a
later ``valid_from``. "The floorplan as of 1954" is the query.

**Sources and references are per-plan pools** every item points into, so ten
walls traced from one scanned drawing share one source row, and one photo can
evidence a wall and the door cut into it at once.

Floorplans are absent by default and never load with a building - most
buildings will never have one, and the common case pays nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import (
    CASCADE,
    PROTECT,
    SET_NULL,
    CheckConstraint,
    DateField,
    DecimalField,
    Deferrable,
    F,
    FloatField,
    ForeignKey,
    Index,
    ManyToManyField,
    OneToOneField,
    Q,
    TextChoices,
    UniqueConstraint,
    URLField,
)
from django.db.models.fields import CharField, IntegerField, PositiveIntegerField, SmallIntegerField, TextField
from django.db.models.fields.json import JSONField

from urbanlens.dashboard.models import abstract

from .queryset import FloorplanManager


class FloorplanWallKind(TextChoices):
    """What a wall segment represents.

    ``VIRTUAL`` is load-bearing for messy real buildings: it draws as a dashed
    hint and renders as nothing, but participates fully in enclosing a region.
    Without it a courtyard, a loading bay, a collapsed side or an unexplored
    boundary can only be recorded by drawing a wall that is not there.

    ``FENCE`` is a boundary rather than a building element - a yard, a
    compound, the line you have to get past before the structure. A *gap* in a
    fence is a ``VIRTUAL`` span rather than an opening, since it is a stretch
    where nothing is built; an opening is fitted into fabric that continues,
    which is what a ``GATE`` is.
    """

    EXTERIOR = "exterior", "Exterior wall"
    INTERIOR = "interior", "Interior wall"
    FENCE = "fence", "Fence"
    VIRTUAL = "virtual", "Virtual (open edge)"
    COLLAPSED = "collapsed", "Collapsed / ruined"


class FloorplanWallThickness(TextChoices):
    """Wall thickness, as presets rather than a measurement.

    Nobody documenting a derelict building from memory and photographs knows a
    wall is 340 mm. Three presets carry every distinction a drawing at this
    fidelity can honestly express.
    """

    THIN = "thin", "Thin"
    NORMAL = "normal", "Normal"
    THICK = "thick", "Thick"


class FloorplanOpeningKind(TextChoices):
    """What kind of gap is cut into a wall."""

    DOOR = "door", "Door"
    DOORWAY = "doorway", "Doorway (no door)"
    GATE = "gate", "Gate"
    WINDOW = "window", "Window"
    HATCH = "hatch", "Hatch"


class FloorplanOpeningSwing(TextChoices):
    """Which way a door opens, where it matters and is known."""

    NONE = "none", "Not known"
    LEFT = "left", "Left"
    RIGHT = "right", "Right"
    DOUBLE = "double", "Double"


class FloorplanLockState(TextChoices):
    """Whether a lock is presently securing its opening.

    Deliberately only the *engagement* axis. Physical integrity - broken,
    seized, missing - is what every floorplan item's ``condition`` already
    records, so folding it in here would give a producer two places to write
    one fact and a reader no rule for which wins. It would also erase the
    distinction that matters most on site: a broken lock may be hanging open
    or rusted shut, and "broken" alone does not say whether the door opens.
    """

    UNKNOWN = "unknown", "Not known"
    LOCKED = "locked", "Locked"
    UNLOCKED = "unlocked", "Unlocked"


class FloorplanMarkerKind(TextChoices):
    """What a point marker on a floor denotes.

    One table with a kind, rather than a tool and a table per concept: these
    differ in icon and meaning, not in schema.

    Trimmed to the kinds that earn their own icon: an entrance is already a
    door opening on a wall, and "photo"/"note"/"fixture" markers carried no
    information a label field didn't already say.
    """

    HAZARD = "hazard", "Hazard"
    STAIR = "stair", "Stair"
    ELEVATOR = "elevator", "Elevator / shaft"


class FloorplanReferenceKind(TextChoices):
    """The media type of a :class:`FloorplanReference`."""

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
        labels: The owner's labels.
    """

    #: Position within the document that wrote this item. Document order *is*
    #: the stored order, so a round-trip preserves how the author arranged
    #: things and a renderer gets a stable draw order.
    sort_order = PositiveIntegerField(default=0)
    description = TextField(blank=True, default="")
    condition = CharField(max_length=255, blank=True, default="")
    built_date = DateField(null=True, blank=True)
    attributes = JSONField(default=dict, blank=True)
    source = ForeignKey("dashboard.FloorplanSource", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    # The attachment path for photos of a wall, opening, room or marker: the
    # media lives once in the plan's reference pool and any number of items
    # point at it. Attachment is item-level, not a point on a wall - this
    # relation carries no geometry, and a photo's own position and heading live
    # on the linked Image (latitude/longitude/direction), whose coordinate
    # provenance is not yet separable; see models/images/model.py.
    references = ManyToManyField("dashboard.FloorplanReference", blank=True, related_name="%(class)ss")
    labels = ManyToManyField("dashboard.Label", blank=True, related_name="floorplan_%(class)ss")

    class Meta(abstract.FrontendDashboardModel.Meta):
        abstract = True


class Floorplan(FloorplanItem):
    """One dated version of one building's floorplan.

    Attributes:
        place: The building this plan describes. Null for a plan not tied to
            one identified structure - a sketch of somewhere the provider
            chain has no footprint for, which is most of what gets explored.
        pin: The pin it was authored from, when personal.
        profile: The local author; null for plans mirrored from upstream.
        wiki: Set when published to the place's community wiki.
        building_ref: REData's reconciliation ref for the building, when known.
        building_name: Free-text fallback naming the building.
        name: Version label ("As built", "After the 1962 fire").
        valid_from: The date this version takes effect; null is the original
            baseline, in force from the beginning of time.
        floor_count: What a source says the building has - may exceed the
            floors actually modelled.
        origin_lat: Latitude of the plan-local coordinate origin.
        origin_lng: Longitude of the plan-local coordinate origin.
        rotation_degrees: The drawing axis, clockwise from north. Buildings sit
            at arbitrary angles; aligning the axis once makes every subsequent
            right angle land square against the underlay.
    """

    #: Nullable so a plan can describe a place the provider chain has no
    #: footprint for. PROTECT so a place that *is* referenced cannot vanish
    #: from under one.
    place = ForeignKey("dashboard.Place", on_delete=PROTECT, null=True, blank=True, related_name="floorplans")
    pin = ForeignKey("dashboard.Pin", on_delete=SET_NULL, null=True, blank=True, related_name="floorplans")
    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, null=True, blank=True, related_name="floorplans")
    #: A community plan is visible to everyone who can see that wiki and
    #: editable by them, with each save recorded as a WikiEdit; a personal
    #: plan (wiki null) is its author's alone. Publishing is always explicit -
    #: a plan names doors and entrances.
    wiki = ForeignKey("dashboard.Wiki", on_delete=CASCADE, null=True, blank=True, related_name="floorplans")
    building_ref = CharField(max_length=255, blank=True, default="")
    building_name = CharField(max_length=255, blank=True, default="")
    name = CharField(max_length=255, blank=True, default="")
    valid_from = DateField(null=True, blank=True)
    floor_count = IntegerField(null=True, blank=True)
    #: Null until the plan has geometry - a version row can exist before
    #: anybody has drawn a wall.
    origin_lat = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    origin_lng = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    rotation_degrees = FloatField(default=0.0)

    objects = FloorplanManager()

    if TYPE_CHECKING:
        place_id: int | None
        profile_id: int | None
        wiki_id: int | None

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplans"
        # Newest first, ties broken by creation - so "the current plan" is a
        # single deterministic row rather than whichever the planner returned.
        ordering = [F("valid_from").desc(nulls_last=True), "-created"]
        indexes = [
            # The one lookup that matters: this user's plans for this building,
            # newest first (services.floorplans.resolution).
            Index(fields=["place", "profile", "valid_from"], name="idx_floorplan_place_owner_date"),
        ]

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

    #: Position in the plan's own list. Written from the payload index on every
    #: save, so the order a document is sent in is the order it comes back in -
    #: which is what lets the editor match a pool row it has just created to the
    #: row the server made for it.
    sort_order = PositiveIntegerField(default=0)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_sources"
        ordering = ("sort_order", "id")

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
    # SET_NULL, not CASCADE: deleting a photo from someone's media must not reach
    # into a floorplan and delete the citation that referred to it. The reference
    # keeps its url and caption and goes on describing what it described.
    image = ForeignKey("dashboard.Image", on_delete=SET_NULL, null=True, blank=True, related_name="floorplan_references")
    description = TextField(blank=True, default="")
    attributes = JSONField(default=dict, blank=True)

    #: As FloorplanSource.sort_order: the payload's own order, preserved.
    sort_order = PositiveIntegerField(default=0)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_references"
        ordering = ("sort_order", "id")

    def __str__(self) -> str:
        return self.title or self.url or f"reference {self.pk}"


class FloorplanFloor(FloorplanItem):
    """One storey of the plan.

    Carries no outline of its own: the storey's shape is whatever its walls
    enclose, so an outline stored here would be a second, divergent answer to
    the same question.

    A storey carries three facts that are easy to conflate and must not be:
    where it sits in the stack (``level``), what people call it on a lift
    button (``designation``), and any nickname the author gives it (``name``).
    Skipping a thirteenth floor by designation while the levels stay
    contiguous is exactly the case that needs them apart.

    Attributes:
        floorplan: The plan version this floor belongs to.
        level: Storey number - 0 is ground, negative below grade. Contiguous
            within a plan: stacking, the floor-below underlay and stair/lift
            connectors all read adjacency off it.
        designation: The lift-button code ("G", "14", "4A", "B2", "M"). Blank
            means derive it from the level, which is the ordinary case.
        name: Optional nickname ("Boiler level"). Never affects numbering.
        elevation_meters: The walking surface's height above sea level.
        height_meters: Floor-to-ceiling height.
    """

    floorplan = ForeignKey(Floorplan, on_delete=CASCADE, related_name="floors")
    level = SmallIntegerField(default=0)
    designation = CharField(max_length=8, blank=True, default="")
    name = CharField(max_length=255, blank=True, default="")
    elevation_meters = FloatField(null=True, blank=True)
    height_meters = FloatField(null=True, blank=True)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_floors"
        ordering = ("level", "sort_order", "id")
        constraints = [
            # DEFERRED is load-bearing, not decoration. save_document writes
            # floors one row at a time inside a single transaction, so a
            # reorder that swaps two levels, or a mid-stack delete that
            # renumbers 3 down to 2, necessarily collides part-way through.
            # Checked at commit, those are fine; checked per statement, they
            # are an IntegrityError.
            UniqueConstraint(fields=["floorplan", "level"], name="floorplan_floor_unique_level", deferrable=Deferrable.DEFERRED),
        ]

    def __str__(self) -> str:
        return self.name or self.designation or f"Level {self.level}"


class FloorplanWall(FloorplanItem):
    """One straight wall segment, in plan-local metres.

    Always a single segment. A drawn polyline is stored as consecutive walls
    sharing an endpoint, which is what lets one span of a chain be re-typed,
    deleted or given a door without splitting anything first.

    Attributes:
        floor: The storey this wall stands on.
        ax: Start point, metres east of the plan origin.
        ay: Start point, metres north of the plan origin.
        bx: End point, metres east of the plan origin.
        by: End point, metres north of the plan origin.
        kind: Exterior, interior, virtual or collapsed.
        thickness: Preset thickness for rendering.
        name: Optional label ("Party wall").
    """

    floor = ForeignKey(FloorplanFloor, on_delete=CASCADE, related_name="walls")
    ax = FloatField()
    ay = FloatField()
    bx = FloatField()
    by = FloatField()
    kind = CharField(max_length=16, choices=FloorplanWallKind.choices, default=FloorplanWallKind.INTERIOR)
    thickness = CharField(max_length=8, choices=FloorplanWallThickness.choices, default=FloorplanWallThickness.NORMAL)
    name = CharField(max_length=255, blank=True, default="")

    if TYPE_CHECKING:
        floor_id: int

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_walls"
        ordering = ("sort_order", "id")
        indexes = [
            # How walls are always asked for: every wall on one storey, to
            # rebuild that storey's enclosed regions.
            Index(fields=["floor", "kind"], name="idx_floorplan_wall_kind"),
        ]

    def __str__(self) -> str:
        return self.name or f"{self.kind} wall {self.pk}"


class FloorplanOpening(FloorplanItem):
    """A door or window, as an interval along one wall.

    Stored as two parameters along the wall rather than its own coordinates:
    an opening then cannot outlive its wall, cannot fail to move with it, and
    cannot drift off it. All three would otherwise be invariants some editor
    had to maintain by hand.

    An opening never breaks the enclosure - a room with a door in it is still
    a room - so this table is invisible to region detection by construction.

    Attributes:
        wall: The wall this opening is cut into.
        kind: Door, doorway, window or hatch.
        t_start: Where the opening begins along the wall, 0 at ``a``, 1 at ``b``.
        t_end: Where it ends. Always greater than ``t_start``.
        swing: Which way a door opens, when it matters and is known.
        sill_meters: Height of the opening's bottom above the walking surface.
    """

    wall = ForeignKey(FloorplanWall, on_delete=CASCADE, related_name="openings")
    kind = CharField(max_length=16, choices=FloorplanOpeningKind.choices, default=FloorplanOpeningKind.DOOR)
    t_start = FloatField()
    t_end = FloatField()
    swing = CharField(max_length=8, choices=FloorplanOpeningSwing.choices, default=FloorplanOpeningSwing.NONE)
    sill_meters = FloatField(null=True, blank=True)

    if TYPE_CHECKING:
        wall_id: int

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_openings"
        ordering = ("sort_order", "id")
        constraints = [
            # An opening outside its wall, or inside out, is not a drawing
            # defect to render oddly - it is meaningless. Rejected by the
            # database so no write path can produce one.
            CheckConstraint(
                condition=Q(t_start__gte=0) & Q(t_end__lte=1) & Q(t_start__lt=F("t_end")),
                name="floorplan_opening_within_wall",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} on wall {self.wall_id}"


class FloorplanLock(FloorplanItem):
    """One lock on an opening; a door may carry several, or none.

    Hangs off the opening rather than the wall, because that is the thing a
    lock actually secures: it cannot then outlive the door it is fitted to,
    and a wall's locks would have nothing to say about which of its three
    doors each one belongs to.

    ``key_attributes`` is free-form on purpose. What identifies a key -
    bitting, keyway, brand, "the one on the ring in the office" - differs per
    building and per person recording it, and any column set fixed here would
    only push the actual note into a description field instead.

    Attributes:
        opening: The door or hatch this lock secures.
        name: Free-form type or label ("padlock", "deadbolt", "chain").
        state: Whether it is presently locked, so far as anyone has seen.
        key_attributes: What opens it, in whatever shape the recorder used.
    """

    opening = ForeignKey(FloorplanOpening, on_delete=CASCADE, related_name="locks")
    name = CharField(max_length=255, blank=True, default="")
    state = CharField(max_length=16, choices=FloorplanLockState.choices, default=FloorplanLockState.UNKNOWN)
    key_attributes = JSONField(default=dict, blank=True)

    if TYPE_CHECKING:
        opening_id: int

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_locks"
        ordering = ("sort_order", "id")

    def __str__(self) -> str:
        return self.name or f"lock {self.pk}"


class FloorplanRoomSeed(FloorplanItem):
    """A named room, stored as the point that identifies it.

    Not a polygon. The room's shape is whichever enclosed region of the wall
    graph contains this point, resolved at render time - so the walls and the
    room can never disagree about where the room is, and editing walls can
    never delete a room's name, photos or labels.

    A seed that lands outside every enclosed region is a room the drawing does
    not close yet. That is shown as such, and is recoverable by drawing the
    missing wall; it is not an error and not data loss.

    Attributes:
        floor: The storey this room is on.
        x: Metres east of the plan origin.
        y: Metres north of the plan origin.
        name: The room's name ("Ward B", "Boiler room").
        height_meters: Ceiling height inside this room, when it differs.
    """

    floor = ForeignKey(FloorplanFloor, on_delete=CASCADE, related_name="rooms")
    x = FloatField()
    y = FloatField()
    name = CharField(max_length=255, blank=True, default="")
    height_meters = FloatField(null=True, blank=True)

    if TYPE_CHECKING:
        floor_id: int

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_room_seeds"
        ordering = ("sort_order", "id")

    def __str__(self) -> str:
        return self.name or f"room {self.pk}"


class FloorplanMarker(FloorplanItem):
    """A point of interest on a storey: photo, hazard, entrance, stair, note.

    Attributes:
        floor: The storey this marker is on.
        x: Metres east of the plan origin.
        y: Metres north of the plan origin.
        kind: What the marker denotes.
        name: Display label.
        facing_degrees: Which way a photo was taken, clockwise from north.
        connector_id: Ties a stair or shaft to the same shaft on other
            storeys. Markers sharing one are the same vertical connection,
            which is what makes a stack of floors read as one building rather
            than unrelated drawings. Free-form rather than an FK: it is
            authored client-side while both ends may still be unsaved. Has no
            ``linked_pin`` equivalent - a detail pin has no notion of "storey"
            to link across.
        linked_pin: The detail pin (``Pin.parent_pin``-child) this marker
            *is*, elsewhere on the site - the pin detail page, its map, its
            own edit/delete UI. Null only for markers on a wiki-published
            copy of a plan (see ``services.floorplans.serialization``),
            which has no owning pin to parent one under. ``CASCADE`` so
            deleting the detail pin (from the pin page) removes the marker
            with it, rather than leaving a marker pointing at nothing.
    """

    floor = ForeignKey(FloorplanFloor, on_delete=CASCADE, related_name="markers")
    x = FloatField()
    y = FloatField()
    kind = CharField(max_length=16, choices=FloorplanMarkerKind.choices, default=FloorplanMarkerKind.HAZARD)
    name = CharField(max_length=255, blank=True, default="")
    facing_degrees = FloatField(null=True, blank=True)
    connector_id = CharField(max_length=64, blank=True, default="")
    linked_pin = OneToOneField("dashboard.Pin", on_delete=CASCADE, null=True, blank=True, related_name="floorplan_marker")

    if TYPE_CHECKING:
        floor_id: int
        linked_pin_id: int | None

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_floorplan_markers"
        ordering = ("sort_order", "id")
        indexes = [
            # Linking a stair to its counterpart on the floor above.
            Index(fields=["connector_id"], name="idx_floorplan_marker_connector"),
        ]

    def __str__(self) -> str:
        return self.name or f"{self.kind} {self.pk}"
