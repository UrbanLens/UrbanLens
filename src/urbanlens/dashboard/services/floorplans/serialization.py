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
    from urbanlens.dashboard.models.floorplans.model import Floorplan, FloorplanItem
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
        "building_ref": floorplan.building_ref,
        "building_name": floorplan.building_name,
        "valid_from": floorplan.valid_from.isoformat() if floorplan.valid_from else None,
        "floor_count": floorplan.floor_count,
        # Deliberately not "origin": resolution.py has long used that key for
        # provenance ("local" / "community" / "redata"), and the save view
        # merges that in over the document - so a coordinate anchor stored
        # under the same name is silently replaced by the string "local".
        "plan_origin": (
            {"lat": float(floorplan.origin_lat), "lng": float(floorplan.origin_lng)}
            if floorplan.origin_lat is not None and floorplan.origin_lng is not None
            else None
        ),
        "rotation_degrees": floorplan.rotation_degrees,
        "source_pool": [
            {"uuid": str(row.uuid), "title": row.title, "url": row.url, "note": row.note, "author": row.author, "attributes": row.attributes or {}}
            for row in source_rows
        ],
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
                "markers": [
                    {
                        **_item_out(marker, source_uuids, reference_uuids),
                        "kind": marker.kind,
                        "name": marker.name,
                        "x": marker.x,
                        "y": marker.y,
                        "facing_degrees": marker.facing_degrees,
                        "connector_id": marker.connector_id,
                    }
                    for marker in floor.markers.all()
                ],
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
            source.title = payload.get("title") or ""
            source.url = payload.get("url") or ""
            source.note = payload.get("note") or ""
            source.author = payload.get("author") or ""
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
            reference.title = payload.get("title") or ""
            reference.url = payload.get("url") or ""
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
    row.condition = payload.get("condition") or ""
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
        FloorplanMarker,
        FloorplanMarkerKind,
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

    floorplan.name = document.get("name") or ""
    floorplan.building_name = document.get("building_name") or floorplan.building_name
    if "building_ref" in document:
        floorplan.building_ref = document.get("building_ref") or floorplan.building_ref
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
        floor.name = payload.get("name") or ""
        floor.elevation_meters = _float_in(payload.get("elevation_meters"), "elevation_meters")
        floor.height_meters = _float_in(payload.get("height_meters"), "height_meters")
        return floor

    floors = _sync({str(f.uuid): f for f in floorplan.floors.all()}, document.get("floors"), build_floor, pools, profile)

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
                payload.get("thickness"), FloorplanWallThickness.values, "wall thickness", FloorplanWallThickness.NORMAL,
            )
            wall.name = payload.get("name") or ""
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

            openings = _sync({str(o.uuid): o for o in wall.openings.all()}, wall_payload.get("openings"), build_opening, pools, profile)

            for opening_payload, opening in openings:

                def build_lock(payload: dict, row: FloorplanLock | None, *, _opening=opening) -> FloorplanLock:
                    lock = row or FloorplanLock(opening=_opening)
                    lock.opening = _opening
                    lock.name = payload.get("name") or ""
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
            room.name = payload.get("name") or ""
            room.height_meters = _float_in(payload.get("height_meters"), "height_meters")
            return room

        _sync({str(r.uuid): r for r in floor.rooms.all()}, floor_payload.get("rooms"), build_room, pools, profile)

        def build_marker(payload: dict, row: FloorplanMarker | None, *, _floor=floor) -> FloorplanMarker:
            marker = row or FloorplanMarker(floor=_floor)
            marker.floor = _floor
            marker.x = _required_float_in(payload.get("x"), "marker.x")
            marker.y = _required_float_in(payload.get("y"), "marker.y")
            marker.kind = _choice_in(payload.get("kind"), FloorplanMarkerKind.values, "marker kind", FloorplanMarkerKind.NOTE)
            marker.name = payload.get("name") or ""
            marker.facing_degrees = _float_in(payload.get("facing_degrees"), "facing_degrees")
            marker.connector_id = payload.get("connector_id") or ""
            return marker

        _sync({str(m.uuid): m for m in floor.markers.all()}, floor_payload.get("markers"), build_marker, pools, profile)

    return floorplan
