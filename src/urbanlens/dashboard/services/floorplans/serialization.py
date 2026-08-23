"""Floorplan documents: the JSON shape the editor and consumers speak.

One document carries a whole plan version - origin, floors, walls, the
openings cut into those walls, the locks fitted to those openings, room seeds
and markers. Coordinates are
plan-local metres throughout (see ``models.floorplans.model``), never degrees;
``services.floorplans.features`` is where they become WGS-84 for a map.

Saving is whole-document replacement: an item round-tripping a known uuid is
updated in place (keeping its identity, labels and references), an item the
document omits is deleted, an item without a uuid is created. That makes the
editor's job a single POST of what it currently holds, with no diffing.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction

if TYPE_CHECKING:
    from urbanlens.dashboard.models.floorplans.model import Floorplan, FloorplanFloor, FloorplanItem, FloorplanMarker
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: FloorplanFloor.level is a SmallIntegerField; past these a value reaches the
#: database as a DataError, which is a 500 rather than something a client can
#: act on.
_MIN_LEVEL, _MAX_LEVEL = -32_768, 32_767

#: Consumer-local data lives under this key inside ``attributes``, so a
#: producer that does not own the key round-trips it untouched.
LOCAL_NAMESPACE = "urbanlens"


def _item_out(row: FloorplanItem, source_uuids: dict, reference_uuids: dict) -> dict[str, Any]:
    """The item fields every floorplan row shares."""
    attributes = dict(row.attributes or {})
    labels = [str(label.uuid) for label in row.labels.all()]
    local = {key: value for key, value in (attributes.get(LOCAL_NAMESPACE) or {}).items() if key != "labels"}
    if labels:
        local["labels"] = labels
    if local:
        attributes[LOCAL_NAMESPACE] = local
    else:
        attributes.pop(LOCAL_NAMESPACE, None)

    return {
        "uuid": str(row.uuid),
        "description": row.description,
        "condition": row.condition,
        "built_date": row.built_date.isoformat() if row.built_date else None,
        "attributes": attributes,
        "source": source_uuids.get(row.source_id),
        "references": [reference_uuids[ref.pk] for ref in row.references.all() if ref.pk in reference_uuids],
    }


def _marker_dict(marker: FloorplanMarker, source_uuids: dict, reference_uuids: dict) -> dict[str, Any]:
    """One marker's document entry, deferring to its linked pin where it has one.

    A linked pin is the freshest copy of name/icon/color once it exists - it
    may have been renamed or restyled from the pin detail page since this
    marker was last saved here.
    """
    linked = marker.linked_pin
    return {
        **_item_out(marker, source_uuids, reference_uuids),
        "kind": marker.kind,
        "name": (linked.name if linked and linked.name else None) or marker.name,
        "icon": linked.effective_icon if linked else None,
        "color": linked.effective_color if linked else None,
        "x": marker.x,
        "y": marker.y,
        "facing_degrees": marker.facing_degrees,
        "connector_id": marker.connector_id,
    }


def document_for(floorplan: Floorplan) -> dict[str, Any]:
    """Assemble one plan version's full nested document.

    Bounded query count regardless of plan size - everything is prefetched
    per relation, never per row.

    Args:
        floorplan: The version to serialize.

    Returns:
        The document.
    """
    source_rows = list(floorplan.source_pool.all())
    reference_rows = list(floorplan.reference_pool.all())
    source_uuids = {row.pk: str(row.uuid) for row in source_rows}
    reference_uuids = {row.pk: str(row.uuid) for row in reference_rows}

    floors = list(
        floorplan.floors.prefetch_related(
            "references",
            "labels",
            "walls__references",
            "walls__labels",
            "walls__openings__references",
            "walls__openings__labels",
            "walls__openings__locks__references",
            "walls__openings__locks__labels",
            "rooms__references",
            "rooms__labels",
            "markers__references",
            "markers__labels",
            "markers__linked_pin",
            "markers__linked_pin__location",
        ),
    )

    def _wall_dict(wall) -> dict[str, Any]:
        return {
            **_item_out(wall, source_uuids, reference_uuids),
            "kind": wall.kind,
            "thickness": wall.thickness,
            "name": wall.name,
            "ax": wall.ax,
            "ay": wall.ay,
            "bx": wall.bx,
            "by": wall.by,
            "openings": [
                {
                    **_item_out(opening, source_uuids, reference_uuids),
                    "kind": opening.kind,
                    "t_start": opening.t_start,
                    "t_end": opening.t_end,
                    "swing": opening.swing,
                    "sill_meters": opening.sill_meters,
                    "locks": [
                        {
                            **_item_out(lock, source_uuids, reference_uuids),
                            "name": lock.name,
                            "state": lock.state,
                            "key_attributes": lock.key_attributes or {},
                        }
                        for lock in opening.locks.all()
                    ],
                }
                for opening in wall.openings.all()
            ],
        }

    return {
        **_item_out(floorplan, source_uuids, reference_uuids),
        "name": floorplan.name,
        # What this document was read at. A save sends it back, and a save
        # whose token no longer matches the row is a save built on a version
        # somebody else has already replaced - see save_document.
        "version_token": floorplan.updated.isoformat() if floorplan.updated else "",
        "building_ref": floorplan.building_ref,
        "building_name": floorplan.building_name,
        "valid_from": floorplan.valid_from.isoformat() if floorplan.valid_from else None,
        "floor_count": floorplan.floor_count,
        # Deliberately not "origin": resolution.py has long used that key for
        # provenance ("local" / "community" / "redata"), and the save view
        # merges that in over the document - so a coordinate anchor stored
        # under the same name is silently replaced by the string "local".
        "plan_origin": ({"lat": float(floorplan.origin_lat), "lng": float(floorplan.origin_lng)} if floorplan.origin_lat is not None and floorplan.origin_lng is not None else None),
        "rotation_degrees": floorplan.rotation_degrees,
        "source_pool": [{"uuid": str(row.uuid), "title": row.title, "url": row.url, "note": row.note, "author": row.author, "attributes": row.attributes or {}} for row in source_rows],
        "reference_pool": [
            {
                "uuid": str(row.uuid),
                "kind": row.kind,
                "title": row.title,
                "url": row.url,
                "description": row.description,
                "attributes": row.attributes or {},
                "image_uuid": str(row.image.uuid) if row.image is not None else None,
            }
            for row in reference_rows
        ],
        "floors": [
            {
                **_item_out(floor, source_uuids, reference_uuids),
                "level": floor.level,
                "designation": floor.designation,
                "name": floor.name,
                "elevation_meters": floor.elevation_meters,
                "height_meters": floor.height_meters,
                "walls": [_wall_dict(wall) for wall in floor.walls.all()],
                "rooms": [
                    {
                        **_item_out(room, source_uuids, reference_uuids),
                        "name": room.name,
                        "x": room.x,
                        "y": room.y,
                        "height_meters": room.height_meters,
                    }
                    for room in floor.rooms.all()
                ],
                "markers": [_marker_dict(marker, source_uuids, reference_uuids) for marker in floor.markers.all()],
            }
            for floor in floors
        ],
    }


def _int_in(raw, field: str) -> int | None:
    """An optional integer from a document payload.

    Raises:
        ValueError: Present but not a number - reported to the caller as a
            400 naming the field, rather than reaching the database and
            surfacing as a 500.
    """
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a whole number") from exc


def _float_in(raw, field: str) -> float | None:
    """An optional float from a document payload.

    Raises:
        ValueError: Present but not a number.
    """
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc


def _required_float_in(raw, field: str) -> float:
    """A coordinate the document must carry.

    Raises:
        ValueError: Absent or not a number. A wall missing an endpoint is not
            a wall, and defaulting it to zero would silently move it to the
            plan origin.
    """
    value = _float_in(raw, field)
    if value is None:
        raise ValueError(f"{field} is required")
    return value


def _date_in(raw) -> datetime.date | None:
    """Parse an ISO date or None.

    Raises:
        ValueError: Not an ISO date.
    """
    if not raw:
        return None
    return datetime.date.fromisoformat(str(raw))


#: Ceilings on how much one document may describe. Generous for any real
#: building - a 200-storey tower with 500 walls a floor fits - and small enough
#: that a malformed or hostile POST cannot ask one request to write a million
#: rows. Checked before anything is saved, so hitting one costs no work.
_MAX_FLOORS = 250
_MAX_WALLS_PER_FLOOR = 2_000
_MAX_OPENINGS_PER_WALL = 100
_MAX_ROOMS_PER_FLOOR = 2_000
_MAX_MARKERS_PER_FLOOR = 2_000


def _text_in(raw, field: str, limit: int) -> str:
    """Coerce a payload value to text this column can actually hold.

    Django does not enforce ``max_length`` on save, so an over-long string is
    not caught until Postgres refuses it - as a ``DataError``, which reaches the
    client as a 500 with nothing in it that says which field was wrong.

    Args:
        raw: The payload value.
        field: Field name, for the message.
        limit: The column's ``max_length``.

    Returns:
        The text, unchanged.

    Raises:
        ValueError: If it is longer than the column allows.
    """
    text = "" if raw is None else str(raw)
    if len(text) > limit:
        raise ValueError(f"{field} must be {limit} characters or fewer (got {len(text)})")
    return text


class StaleDocumentError(ValueError):
    """A save built on a version of the plan that has since been replaced.

    Separate from the other ValueErrors this module raises because it is not a
    malformed request: the document is perfectly valid, it is just no longer
    current. The caller answers it with 409 rather than 400, and the editor
    treats it as "somebody else got there first" rather than "you sent
    nonsense".
    """


def _reject_stale(floorplan: Floorplan, document: dict[str, Any]) -> None:
    """Refuse a save whose base version is no longer the current one.

    A whole-document save deletes by omission, so two tabs editing one plan do
    not merge - the second one to save silently discards everything the first
    did. Nothing detected that, because nothing recorded which version a
    document was read at.

    A document with no token at all is let through: it is a save from a client
    that predates this, or a deliberate fork (which carries no uuid either), and
    breaking those to catch a rarer problem is the wrong trade.

    Args:
        floorplan: The row about to be written.
        document: The incoming payload.

    Raises:
        StaleDocumentError: If the token names a different version than the row is at.
    """
    token = str(document.get("version_token") or "")
    if not token or floorplan.pk is None or floorplan.updated is None:
        return
    # The token describes the row the document was *read* from. Writing that
    # document into a different row is a copy, not an update - publish_to_wiki
    # does exactly this, and so does every deliberate fork - and the token says
    # nothing about the row being written. Only a same-row save can lose an
    # update, so only a same-row save is checked.
    if str(document.get("uuid") or "") != str(floorplan.uuid):
        return
    current = floorplan.updated.isoformat()
    if token != current:
        raise StaleDocumentError("This plan was changed somewhere else since you opened it. Reload to see the newer version.")


def _reject_oversized(document: dict[str, Any]) -> None:
    """Refuse a document that would write an unreasonable number of rows.

    Args:
        document: The whole payload.

    Raises:
        ValueError: If any collection is past its ceiling.
    """
    floors = document.get("floors")
    if not isinstance(floors, list):
        return
    if len(floors) > _MAX_FLOORS:
        raise ValueError(f"a plan cannot have more than {_MAX_FLOORS} floors (got {len(floors)})")
    for floor in floors:
        if not isinstance(floor, dict):
            continue
        for key, ceiling in (("walls", _MAX_WALLS_PER_FLOOR), ("rooms", _MAX_ROOMS_PER_FLOOR), ("markers", _MAX_MARKERS_PER_FLOOR)):
            items = floor.get(key)
            if isinstance(items, list) and len(items) > ceiling:
                raise ValueError(f"a floor cannot have more than {ceiling} {key} (got {len(items)})")
        walls = floor.get("walls")
        if not isinstance(walls, list):
            continue
        for wall in walls:
            if not isinstance(wall, dict):
                continue
            openings = wall.get("openings")
            if isinstance(openings, list) and len(openings) > _MAX_OPENINGS_PER_WALL:
                raise ValueError(f"a wall cannot have more than {_MAX_OPENINGS_PER_WALL} openings (got {len(openings)})")


def _choice_in(raw, choices, field: str, default: str) -> str:
    """One of an enum's values, defaulting when absent.

    Raises:
        ValueError: Present but unrecognised. Coercing an unknown value to the
            default is how a whole class of item quietly becomes the wrong
            thing while looking like it saved.
    """
    if raw is None or raw == "":
        return default
    if raw not in choices:
        raise ValueError(f"unknown {field} {raw!r} - expected one of {sorted(choices)}")
    return str(raw)


class _Pools:
    """The plan's source/reference pools during a save, keyed by payload uuid."""

    def __init__(self, floorplan: Floorplan) -> None:
        self.floorplan = floorplan
        self.sources: dict[str, Any] = {}
        self.references: dict[str, Any] = {}

    def sync(self, document: dict[str, Any]) -> None:
        """Reconcile both pools against the document's pool lists."""
        from urbanlens.dashboard.models.floorplans.model import FloorplanReference, FloorplanReferenceKind, FloorplanSource
        from urbanlens.dashboard.models.images.model import Image

        stale_sources = {str(row.uuid): row for row in self.floorplan.source_pool.all()}
        for payload in document.get("source_pool") or []:
            source = stale_sources.pop(str(payload.get("uuid") or ""), None) or FloorplanSource(floorplan=self.floorplan)
            source.floorplan = self.floorplan
            source.title = _text_in(payload.get("title"), "source.title", 255)
            source.url = _text_in(payload.get("url"), "source.url", 1000)
            source.note = payload.get("note") or ""
            source.author = _text_in(payload.get("author"), "source.author", 255)
            source.attributes = payload.get("attributes") or {}
            source.save()
            for name in (payload.get("uuid"), payload.get("key"), source.uuid):
                if name:
                    self.sources[str(name)] = source
        for stale_source in stale_sources.values():
            stale_source.delete()

        stale_references = {str(row.uuid): row for row in self.floorplan.reference_pool.all()}
        for payload in document.get("reference_pool") or []:
            reference = stale_references.pop(str(payload.get("uuid") or ""), None) or FloorplanReference(floorplan=self.floorplan)
            reference.floorplan = self.floorplan
            reference.kind = payload.get("kind") if payload.get("kind") in FloorplanReferenceKind.values else FloorplanReferenceKind.OTHER
            reference.title = _text_in(payload.get("title"), "reference.title", 255)
            reference.url = _text_in(payload.get("url"), "reference.url", 1000)
            reference.description = payload.get("description") or ""
            reference.attributes = payload.get("attributes") or {}
            reference.image = Image.objects.filter(uuid=payload["image_uuid"]).first() if payload.get("image_uuid") else None
            reference.save()
            for name in (payload.get("uuid"), payload.get("key"), reference.uuid):
                if name:
                    self.references[str(name)] = reference
        for stale_reference in stale_references.values():
            stale_reference.delete()


def _apply_item(row: FloorplanItem, payload: dict[str, Any], pools: _Pools, profile: Profile | None) -> None:
    """Write the shared item surface from a payload; saves the row."""
    from urbanlens.dashboard.models.labels.model import Label

    row.description = payload.get("description") or ""
    row.condition = _text_in(payload.get("condition"), "condition", 255)
    try:
        row.built_date = _date_in(payload.get("built_date"))
    except ValueError as exc:
        raise ValueError(f"built_date: {exc}") from exc
    attributes = dict(payload.get("attributes") or {})
    local = dict(attributes.get(LOCAL_NAMESPACE) or {})
    label_uuids = local.pop("labels", None) or payload.get("labels") or []
    if local:
        attributes[LOCAL_NAMESPACE] = local
    else:
        attributes.pop(LOCAL_NAMESPACE, None)
    row.attributes = attributes
    row.source = pools.sources.get(str(payload.get("source") or ""))
    row.save()
    row.references.set([pools.references[key] for key in (payload.get("references") or []) if key in pools.references])
    if profile is not None:
        # Scoped to the saver's own labels: a document cannot attach somebody
        # else's label by guessing its uuid.
        row.labels.set(Label.objects.filter(uuid__in=label_uuids, profile=profile))


#: FloorplanMarkerKind -> PinType for a marker's detail-pin twin. Stair and
#: elevator earn their own PinType (see models.pin.model.PinType) precisely
#: so this mapping is not lossy; hazard already had DANGER to reuse.
_MARKER_KIND_TO_PIN_TYPE = {
    "hazard": "danger",
    "stair": "stair",
    "elevator": "elevator",
}


def _sync_linked_pin(marker: FloorplanMarker, payload: dict[str, Any], floorplan: Floorplan) -> None:
    """Create, move, or restyle a marker's detail-pin twin to match it.

    A marker only ever gets one on a personal, pin-owned floorplan - the
    wiki-published copy (see :func:`services.floorplans.resolution.publish_to_wiki`)
    has no owning pin to parent a detail pin under, and its own markers stay
    unlinked rather than reaching across to another profile's private pin.

    Coordinates come from the payload's ``lat``/``lng`` (computed client-side
    from the marker's plan-local x/y via ``PlanProjection.toWorld`` - this
    module only knows plan-local metres, and duplicating that projection
    server-side would be a second implementation to keep in step with the
    first). Silently no-ops without them rather than raising: an older
    client, or a marker placed before this existed, should not block saving
    the rest of the document over a twin it doesn't know how to grow yet.

    Args:
        marker: The marker to link, already saved (has a pk).
        payload: This marker's document payload.
        floorplan: The plan version being saved.
    """
    parent_pin = floorplan.pin
    if parent_pin is None:
        return
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

    lat = _float_in(payload.get("lat"), "marker.lat")
    lng = _float_in(payload.get("lng"), "marker.lng")
    if lat is None or lng is None:
        return

    # get_exact_or_create, not resolve_child_pin_location: stacked floors
    # legitimately share a ground-plane point (a stairwell sits at the same
    # lat/lng on every storey it passes through), and that helper's "no two
    # of one profile's pins at the exact same point" rule exists for the
    # manual detail-pin dialog, not for this.
    location, _created = Location.objects.get_exact_or_create(lat, lng)

    linked = marker.linked_pin
    if linked is None:
        linked = Pin(parent_pin=parent_pin, profile=parent_pin.profile)
    linked.name = marker.name or None
    # Not an explicit rename any more than DetailPinPanelView's own creation
    # is - the name is prefilled from the marker kind, and should stay
    # eligible for later canonical-name cleanup.
    linked.name_is_user_provided = False
    linked.pin_type = _MARKER_KIND_TO_PIN_TYPE.get(marker.kind, "other")
    linked.pin_type_is_user_provided = True
    linked.location = location
    # Appearance is stored on the pin, never on the marker: the document reads
    # it back through linked.effective_icon/effective_color (see _marker_dict),
    # so a second copy on FloorplanMarker would be a second answer to the same
    # question. Only the write half was missing, which is why anything set in
    # the floorplan editor vanished on save.
    #
    # Blank means "no override", so the kind's own default returns - that is
    # how a marker is un-styled, and it has to be distinguishable from absent.
    if "icon" in payload:
        linked.icon = (payload.get("icon") or "").strip() or None
    if "color" in payload:
        linked.color = (payload.get("color") or "").strip() or None
    linked.save()
    if marker.linked_pin_id != linked.pk:
        marker.linked_pin = linked
        marker.save(update_fields=["linked_pin"])


def _sync_markers(existing_by_uuid: dict, payloads: list[dict] | None, floor: FloorplanFloor, pools: _Pools, profile: Profile | None, floorplan: Floorplan) -> None:
    """Reconcile one floor's markers, keeping each one's detail-pin twin in step.

    A hand-rolled counterpart to :func:`_sync` rather than a parameter added
    to it: markers are the only floorplan item with a twin elsewhere on the
    site, and threading that through the generic helper would make every
    other caller (walls, openings, locks, rooms) carry a no-op it never uses.
    """
    from urbanlens.dashboard.models.floorplans.model import FloorplanMarker, FloorplanMarkerKind

    for index, payload in enumerate(payloads or []):
        marker = existing_by_uuid.pop(str(payload.get("uuid") or ""), None) or FloorplanMarker(floor=floor)
        marker.floor = floor
        marker.x = _required_float_in(payload.get("x"), "marker.x")
        marker.y = _required_float_in(payload.get("y"), "marker.y")
        marker.kind = _choice_in(payload.get("kind"), FloorplanMarkerKind.values, "marker kind", FloorplanMarkerKind.HAZARD)
        marker.name = _text_in(payload.get("name"), "marker.name", 255)
        marker.facing_degrees = _float_in(payload.get("facing_degrees"), "facing_degrees")
        marker.connector_id = payload.get("connector_id") or ""
        marker.sort_order = index
        _apply_item(marker, payload, pools, profile)
        _sync_linked_pin(marker, payload, floorplan)
    for orphan in existing_by_uuid.values():
        # A post_delete signal (models.floorplans.signals) removes the
        # linked pin, if any - the same path a floor or floorplan deletion
        # cascading down to this marker also goes through.
        orphan.delete()


def _sync(existing_by_uuid: dict, payloads: list[dict] | None, build, pools: _Pools, profile: Profile | None) -> list[tuple[dict, Any]]:
    """Reconcile one collection: update by uuid, create the new, delete the omitted."""
    kept: list[tuple[dict, Any]] = []
    for index, payload in enumerate(payloads or []):
        row = existing_by_uuid.pop(str(payload.get("uuid") or ""), None)
        row = build(payload, row)
        # Position in the document is the stored order, so re-arranging items
        # in an editor survives the round-trip.
        row.sort_order = index
        _apply_item(row, payload, pools, profile)
        kept.append((payload, row))
    for orphan in existing_by_uuid.values():
        orphan.delete()
    return kept


def _reject_duplicate_levels(payload: Any) -> None:
    """Refuse a document whose floors would share a storey.

    Args:
        payload: The document's ``floors`` value, whatever the client sent.

    Raises:
        ValueError: If two floors claim the same level.
    """
    if not isinstance(payload, list):
        return
    seen: set[int] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        level = _int_in(entry.get("level"), "level") or 0
        if level in seen:
            raise ValueError(f"two floors cannot share level {level}")
        seen.add(level)


@transaction.atomic
def save_document(floorplan: Floorplan, document: dict[str, Any], *, profile: Profile | None) -> Floorplan:
    """Fully replace one floorplan version's contents from a document.

    Args:
        floorplan: The version being written.
        document: The document (see :func:`document_for`).
        profile: Whose labels label-uuids may resolve against.

    Returns:
        The saved floorplan.

    Raises:
        ValueError: Something in the document is unusable, named in the message
            so the caller can turn it into a 400 a client can act on.
    """
    from urbanlens.dashboard.models.floorplans.model import (
        FloorplanFloor,
        FloorplanLock,
        FloorplanLockState,
        FloorplanOpening,
        FloorplanOpeningKind,
        FloorplanOpeningSwing,
        FloorplanRoomSeed,
        FloorplanWall,
        FloorplanWallKind,
        FloorplanWallThickness,
    )

    pools = _Pools(floorplan)
    pools.sync(document)

    # Before anything is written, which the placement is load-bearing for.
    # _apply_item below saves the floorplan row, and `updated` is auto_now - so
    # a staleness check made after it would be comparing the token against a
    # timestamp this very request had just moved, and could never match.
    #
    # The unique constraint on (floorplan, level) is DEFERRED - it has to be,
    # since a reorder or a mid-stack renumber necessarily collides part-way
    # through a save that writes one row at a time - so a genuinely duplicated
    # level would not surface until the outer commit, as an IntegrityError the
    # view cannot turn into a useful message. Rejecting it here makes it a 400.
    _reject_stale(floorplan, document)
    _reject_duplicate_levels(document.get("floors"))
    _reject_oversized(document)

    floorplan.name = _text_in(document.get("name"), "name", 255)
    floorplan.building_name = _text_in(document.get("building_name"), "building_name", 255) or floorplan.building_name
    if "building_ref" in document:
        floorplan.building_ref = _text_in(document.get("building_ref"), "building_ref", 255) or floorplan.building_ref
    floorplan.valid_from = _date_in(document.get("valid_from"))
    floorplan.floor_count = _int_in(document.get("floor_count"), "floor_count")
    plan_origin = document.get("plan_origin") or {}
    if plan_origin:
        floorplan.origin_lat = _float_in(plan_origin.get("lat"), "plan_origin.lat")
        floorplan.origin_lng = _float_in(plan_origin.get("lng"), "plan_origin.lng")
    floorplan.rotation_degrees = _float_in(document.get("rotation_degrees"), "rotation_degrees") or 0.0
    _apply_item(floorplan, document, pools, profile)

    def build_floor(payload: dict, row: FloorplanFloor | None) -> FloorplanFloor:
        floor = row or FloorplanFloor(floorplan=floorplan)
        floor.floorplan = floorplan
        level = _int_in(payload.get("level"), "level") or 0
        if not _MIN_LEVEL <= level <= _MAX_LEVEL:
            raise ValueError(f"level must be between {_MIN_LEVEL} and {_MAX_LEVEL}")
        floor.level = level
        designation = str(payload.get("designation") or "").strip()
        # Refused rather than truncated, matching this module's 400-not-500
        # style: a designation silently cut to "4A2345678" is worse than a
        # client being told it sent something it cannot have meant.
        if len(designation) > 8:
            raise ValueError("designation must be 8 characters or fewer")
        floor.designation = designation
        floor.name = _text_in(payload.get("name"), "floor.name", 255)
        floor.elevation_meters = _float_in(payload.get("elevation_meters"), "elevation_meters")
        floor.height_meters = _float_in(payload.get("height_meters"), "height_meters")
        return floor

    # Prefetched exactly like document_for() reads it: without this, every
    # existing wall/opening/lock/room/marker triggers its own query the
    # moment its parent's .all() is called below, and this whole function
    # runs on every autosave tick.

    existing_floors = floorplan.floors.prefetch_related("walls__openings__locks", "rooms", "markers")

    # An opening can change wall - dragging a door round a corner is an
    # ordinary edit - so openings are matched by uuid across the whole plan
    # rather than only within the wall they used to sit on. Matched per wall,
    # a move reads as "deleted from one, unknown to the other": the row is
    # destroyed and a new one created, so the opening loses its identity and
    # its locks go with it (FloorplanLock cascades from the opening).
    existing_openings: dict[str, FloorplanOpening] = {}
    for existing_floor in existing_floors:
        for existing_wall in existing_floor.walls.all():
            for existing_opening in existing_wall.openings.all():
                existing_openings[str(existing_opening.uuid)] = existing_opening
    surviving_openings: set[str] = set()

    floors = _sync({str(f.uuid): f for f in existing_floors}, document.get("floors"), build_floor, pools, profile)

    for floor_payload, floor in floors:

        def build_wall(payload: dict, row: FloorplanWall | None, *, _floor=floor) -> FloorplanWall:
            wall = row or FloorplanWall(floor=_floor)
            wall.floor = _floor
            wall.ax = _required_float_in(payload.get("ax"), "wall.ax")
            wall.ay = _required_float_in(payload.get("ay"), "wall.ay")
            wall.bx = _required_float_in(payload.get("bx"), "wall.bx")
            wall.by = _required_float_in(payload.get("by"), "wall.by")
            wall.kind = _choice_in(payload.get("kind"), FloorplanWallKind.values, "wall kind", FloorplanWallKind.INTERIOR)
            wall.thickness = _choice_in(
                payload.get("thickness"),
                FloorplanWallThickness.values,
                "wall thickness",
                FloorplanWallThickness.NORMAL,
            )
            wall.name = _text_in(payload.get("name"), "wall.name", 255)
            return wall

        walls = _sync({str(w.uuid): w for w in floor.walls.all()}, floor_payload.get("walls"), build_wall, pools, profile)

        for wall_payload, wall in walls:

            def build_opening(payload: dict, row: FloorplanOpening | None, *, _wall=wall) -> FloorplanOpening:
                opening = row or FloorplanOpening(wall=_wall)
                opening.wall = _wall
                opening.kind = _choice_in(payload.get("kind"), FloorplanOpeningKind.values, "opening kind", FloorplanOpeningKind.DOOR)
                t_start = _required_float_in(payload.get("t_start"), "opening.t_start")
                t_end = _required_float_in(payload.get("t_end"), "opening.t_end")
                # Checked here as well as by the database constraint: reaching
                # the constraint is an IntegrityError (a 500), while this is a
                # 400 that names what is wrong.
                if not 0 <= t_start < t_end <= 1:
                    raise ValueError(f"opening must satisfy 0 <= t_start < t_end <= 1, got {t_start} and {t_end}")
                opening.t_start = t_start
                opening.t_end = t_end
                opening.swing = _choice_in(payload.get("swing"), FloorplanOpeningSwing.values, "opening swing", FloorplanOpeningSwing.NONE)
                opening.sill_meters = _float_in(payload.get("sill_meters"), "sill_meters")
                return opening

            # Only the rows this wall's payload actually claims, so _sync's
            # own orphan sweep cannot delete one that has moved to another
            # wall. What is genuinely gone is collected after every wall has
            # been seen, below.
            opening_payloads = wall_payload.get("openings") or []
            claimed: dict[str, FloorplanOpening] = {}
            if isinstance(opening_payloads, list):
                for opening_payload in opening_payloads:
                    if not isinstance(opening_payload, dict):
                        continue
                    opening_uuid = str(opening_payload.get("uuid") or "")
                    if opening_uuid and opening_uuid in existing_openings:
                        claimed[opening_uuid] = existing_openings[opening_uuid]
            openings = _sync(claimed, opening_payloads, build_opening, pools, profile)
            for _, kept_opening in openings:
                surviving_openings.add(str(kept_opening.uuid))

            for opening_payload, opening in openings:

                def build_lock(payload: dict, row: FloorplanLock | None, *, _opening=opening) -> FloorplanLock:
                    lock = row or FloorplanLock(opening=_opening)
                    lock.opening = _opening
                    lock.name = _text_in(payload.get("name"), "lock.name", 255)
                    lock.state = _choice_in(payload.get("state"), FloorplanLockState.values, "lock state", FloorplanLockState.UNKNOWN)
                    key_attributes = payload.get("key_attributes") or {}
                    if not isinstance(key_attributes, dict):
                        # Free-form, not shapeless: a list or string here reaches
                        # the database happily and then breaks every reader that
                        # expects an object, far from the save that caused it.
                        # ValueError rather than the TypeError ruff wants, because
                        # every caller in controllers/floorplans.py turns a
                        # ValueError from this module into a 400 naming the field.
                        raise ValueError("key_attributes must be an object")  # noqa: TRY004
                    lock.key_attributes = key_attributes
                    return lock

                _sync({str(lk.uuid): lk for lk in opening.locks.all()}, opening_payload.get("locks"), build_lock, pools, profile)

        def build_room(payload: dict, row: FloorplanRoomSeed | None, *, _floor=floor) -> FloorplanRoomSeed:
            room = row or FloorplanRoomSeed(floor=_floor)
            room.floor = _floor
            room.x = _required_float_in(payload.get("x"), "room.x")
            room.y = _required_float_in(payload.get("y"), "room.y")
            room.name = _text_in(payload.get("name"), "room.name", 255)
            room.height_meters = _float_in(payload.get("height_meters"), "height_meters")
            return room

        _sync({str(r.uuid): r for r in floor.rooms.all()}, floor_payload.get("rooms"), build_room, pools, profile)

        _sync_markers({str(m.uuid): m for m in floor.markers.all()}, floor_payload.get("markers"), floor, pools, profile, floorplan)

    # Deleted last, once every wall has had its say: an opening missing from
    # the wall it used to be on may simply have moved to another one.
    for opening_uuid, orphan_opening in existing_openings.items():
        if opening_uuid not in surviving_openings:
            orphan_opening.delete()

    return floorplan
