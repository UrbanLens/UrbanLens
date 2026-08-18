"""Floorplan documents: the JSON shape editors and consumers speak.

The document format is REData's, verbatim (see
``../REData/src/redata/parcels/services/floorplans.py``): geometry as GeoJSON,
``source`` as a uuid into the plan-level ``source_pool``, ``references`` as
uuids into ``reference_pool``, elements per floor plus plan-level, locks
nested under elements. One format means the editor parses a REData-origin
document and a local one identically, and a future push upstream is a POST of
the same payload (minus the local-only ``labels`` keys, which the upstream
validator would reject).

Saving is whole-document replacement, like REData's write side: items
round-tripping a known uuid are updated in place (labels and identity kept),
items the document omits are deleted, items without a uuid are created.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.gis.geos import GEOSGeometry
from django.db import transaction

if TYPE_CHECKING:
    from urbanlens.dashboard.models.floorplans.model import Floorplan, FloorplanItem
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: FloorplanFloor.level is a SmallIntegerField; past these a value reaches the
#: database as a DataError, which is a 500 rather than something a client can
#: act on.
_MIN_LEVEL, _MAX_LEVEL = -32_768, 32_767


def _geometry_out(geometry) -> dict | None:
    return json.loads(geometry.geojson) if geometry is not None else None


def _geometry_in(data: dict | None) -> GEOSGeometry | None:
    """A GeoJSON dict as GEOS geometry (SRID 4326), or None.

    Raises:
        ValueError: The dict is not a usable GeoJSON geometry.
    """
    if not data:
        return None
    try:
        return GEOSGeometry(json.dumps(data), srid=4326)
    except Exception as exc:
        raise ValueError(f"unusable geometry: {exc}") from exc


#: Consumer-local data lives under this key inside ``attributes``. REData
#: rejects unknown keys anywhere in a document - deliberately, so a typo like
#: "goemetry" fails loudly instead of silently dropping a wall - but
#: round-trips ``attributes`` verbatim and never interprets keys it does not
#: own. Namespacing keeps our labels portable *through* an upstream push
#: rather than being stripped before one.
LOCAL_NAMESPACE = "urbanlens"


def _item_out(row: FloorplanItem, source_uuids: dict, reference_uuids: dict) -> dict[str, Any]:
    """The shared item fields, exactly as REData emits them."""
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
        The document, in REData's shape plus local ``labels`` keys.
    """
    source_rows = list(floorplan.source_pool.all())
    reference_rows = list(floorplan.reference_pool.all())
    source_uuids = {row.pk: str(row.uuid) for row in source_rows}
    reference_uuids = {row.pk: str(row.uuid) for row in reference_rows}

    floors = list(floorplan.floors.prefetch_related("references", "labels", "rooms__references", "rooms__labels", "rooms__spans_floors"))
    elements = list(
        floorplan.elements.prefetch_related(
            "references", "labels", "locks__references", "locks__labels", "connects_rooms", "spans_floors",
        ),
    )
    element_uuids: dict[int | None, str] = {row.pk: str(row.uuid) for row in elements}
    room_uuids: dict[int | None, str] = {room.pk: str(room.uuid) for floor in floors for room in floor.rooms.all()}
    floor_uuids: dict[int | None, str] = {floor.pk: str(floor.uuid) for floor in floors}

    def _element_dict(row) -> dict[str, Any]:
        return {
            **_item_out(row, source_uuids, reference_uuids),
            "kind": row.kind,
            "name": row.name,
            "geometry": _geometry_out(row.geometry),
            "material": row.material,
            "room": room_uuids.get(row.room_id),
            "mounted_on": element_uuids.get(row.mounted_on_id),
            "base_elevation_meters": row.base_elevation_meters,
            "height_meters": row.height_meters,
            "thickness_meters": row.thickness_meters,
            "rotation_degrees": row.rotation_degrees,
            "connects_rooms": [room_uuids[room.pk] for room in row.connects_rooms.all() if room.pk in room_uuids],
            "spans_floors": [floor_uuids[floor.pk] for floor in row.spans_floors.all() if floor.pk in floor_uuids],
            "locks": [
                {**_item_out(lock, source_uuids, reference_uuids), "name": lock.name, "key_attributes": lock.key_attributes or {}}
                for lock in row.locks.all()
            ],
        }

    elements_by_floor: dict[int | None, list] = {}
    for row in elements:
        elements_by_floor.setdefault(row.floor_id, []).append(row)

    return {
        **_item_out(floorplan, source_uuids, reference_uuids),
        "name": floorplan.name,
        "building_ref": floorplan.building_ref,
        "building_name": floorplan.building_name,
        "valid_from": floorplan.valid_from.isoformat() if floorplan.valid_from else None,
        "floor_count": floorplan.floor_count,
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
                "image_uuid": str(row.image.uuid) if row.image_id else None,
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
                "geometry": _geometry_out(floor.geometry),
                "rooms": [
                    {
                        **_item_out(room, source_uuids, reference_uuids),
                        "name": room.name,
                        "geometry": _geometry_out(room.geometry),
                        "height_meters": room.height_meters,
                        "parent": room_uuids.get(room.parent_id),
                        "spans_floors": [floor_uuids[floor.pk] for floor in room.spans_floors.all() if floor.pk in floor_uuids],
                    }
                    for room in floor.rooms.all()
                ],
                "elements": [_element_dict(row) for row in elements_by_floor.get(floor.pk, [])],
            }
            for floor in floors
        ],
        "elements": [_element_dict(row) for row in elements_by_floor.get(None, [])],
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


def _date_in(raw) -> datetime.date | None:
    """Parse an ISO date or None.

    Raises:
        ValueError: Not an ISO date.
    """
    if not raw:
        return None
    return datetime.date.fromisoformat(str(raw))


class _Registry:
    """Resolves a document's cross-references by uuid *or* document-local key.

    A client drawing a door on a wall it just drew has no uuid for either -
    both are new. REData solves this with a write-only ``key``: an item may
    name itself, and any reference may name that key instead of a uuid. Keys
    are never emitted on read (uuids are), so they exist only for the
    duration of one document.
    """

    def __init__(self) -> None:
        self._rows: dict[str, Any] = {}

    def register(self, payload: dict, row: Any) -> None:
        """Index a saved row under its payload uuid and its document key."""
        for name in (payload.get("uuid"), payload.get("key")):
            if name:
                self._rows[str(name)] = row
        self._rows[str(row.uuid)] = row

    def get(self, name: Any) -> Any:
        """The row a reference names, or None."""
        return self._rows.get(str(name)) if name else None

    def many(self, names: Any) -> list[Any]:
        """Every row a reference list names, skipping the unresolvable."""
        return [row for row in (self.get(name) for name in (names or [])) if row is not None]


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

        existing = {str(row.uuid): row for row in self.floorplan.source_pool.all()}
        for payload in document.get("source_pool") or []:
            row = existing.pop(str(payload.get("uuid") or ""), None) or FloorplanSource(floorplan=self.floorplan)
            row.floorplan = self.floorplan
            row.title = payload.get("title") or ""
            row.url = payload.get("url") or ""
            row.note = payload.get("note") or ""
            row.author = payload.get("author") or ""
            row.attributes = payload.get("attributes") or {}
            row.save()
            for name in (payload.get("uuid"), payload.get("key"), row.uuid):
                if name:
                    self.sources[str(name)] = row
        for orphan in existing.values():
            orphan.delete()

        existing = {str(row.uuid): row for row in self.floorplan.reference_pool.all()}
        for payload in document.get("reference_pool") or []:
            row = existing.pop(str(payload.get("uuid") or ""), None) or FloorplanReference(floorplan=self.floorplan)
            row.floorplan = self.floorplan
            row.kind = payload.get("kind") if payload.get("kind") in FloorplanReferenceKind.values else FloorplanReferenceKind.OTHER
            row.title = payload.get("title") or ""
            row.url = payload.get("url") or ""
            row.description = payload.get("description") or ""
            row.attributes = payload.get("attributes") or {}
            row.image = Image.objects.filter(uuid=payload["image_uuid"]).first() if payload.get("image_uuid") else None
            row.save()
            for name in (payload.get("uuid"), payload.get("key"), row.uuid):
                if name:
                    self.references[str(name)] = row
        for orphan in existing.values():
            orphan.delete()


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
    # Accept a top-level `labels` too: it is what this app emitted before the
    # namespaced form, and a document saved by an older client is still valid.
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


def _sync(existing_by_uuid: dict, payloads: list[dict], build, pools: _Pools, profile: Profile | None) -> list[tuple[dict, Any]]:
    """Reconcile one collection: update by uuid, create the new, delete the omitted."""
    kept: list[tuple[dict, Any]] = []
    for index, payload in enumerate(payloads or []):
        row = existing_by_uuid.pop(str(payload.get("uuid") or ""), None)
        row = build(payload, row)
        # Position in the document is the stored order, so re-arranging items
        # in an editor survives the round-trip (REData does the same).
        row.sort_order = index
        _apply_item(row, payload, pools, profile)
        kept.append((payload, row))
    for orphan in existing_by_uuid.values():
        orphan.delete()
    return kept


def _would_cycle(room, parent) -> bool:
    """Whether making ``parent`` this room's parent closes a loop.

    REData rejects cycles at write time, so a well-formed document has none -
    but a consumer that walks the chain has to survive one anyway, the same
    way ``parcel_buildings._tree_ordered`` survives a cyclic ``parent_ref``.
    """
    seen = {room.pk}
    walker = parent
    while walker is not None:
        if walker.pk in seen:
            return True
        seen.add(walker.pk)
        walker = walker.parent
    return False


@transaction.atomic
def save_document(floorplan: Floorplan, document: dict[str, Any], *, profile: Profile | None) -> Floorplan:
    """Fully replace one floorplan version's contents from a document.

    Args:
        floorplan: The version being written.
        document: The document (REData's shape; see :func:`document_for`).
        profile: Whose labels label-uuids may resolve against.

    Returns:
        The saved floorplan.

    Raises:
        ValueError: A geometry or date in the document is unusable.
    """
    from urbanlens.dashboard.models.floorplans.model import (
        FloorplanElement,
        FloorplanElementKind,
        FloorplanFloor,
        FloorplanLock,
        FloorplanRoom,
    )

    pools = _Pools(floorplan)
    pools.sync(document)

    floorplan.name = document.get("name") or ""
    floorplan.building_name = document.get("building_name") or floorplan.building_name
    if "building_ref" in document:
        floorplan.building_ref = document.get("building_ref") or floorplan.building_ref
    floorplan.valid_from = _date_in(document.get("valid_from"))
    floorplan.floor_count = _int_in(document.get("floor_count"), "floor_count")
    _apply_item(floorplan, document, pools, profile)

    def build_floor(payload: dict, row: FloorplanFloor | None) -> FloorplanFloor:
        floor = row or FloorplanFloor(floorplan=floorplan)
        floor.floorplan = floorplan
        level = _int_in(payload.get("level"), "level") or 0
        if not _MIN_LEVEL <= level <= _MAX_LEVEL:
            raise ValueError(f"level must be between {_MIN_LEVEL} and {_MAX_LEVEL}")
        floor.level = level
        floor.name = payload.get("name") or ""
        floor.geometry = _geometry_in(payload.get("geometry"))
        floor.elevation_meters = _float_in(payload.get("elevation_meters"), "elevation_meters")
        floor.height_meters = _float_in(payload.get("height_meters"), "height_meters")
        return floor

    floors = _sync({str(f.uuid): f for f in floorplan.floors.all()}, document.get("floors"), build_floor, pools, profile)

    floor_registry = _Registry()
    for floor_payload, floor in floors:
        floor_registry.register(floor_payload, floor)

    room_registry = _Registry()
    room_payloads: list[tuple[dict, FloorplanRoom]] = []
    for floor_payload, floor in floors:

        def build_room(payload: dict, row: FloorplanRoom | None, *, _floor=floor) -> FloorplanRoom:
            room = row or FloorplanRoom(floor=_floor)
            room.floor = _floor
            room.name = payload.get("name") or ""
            room.geometry = _geometry_in(payload.get("geometry"))
            room.height_meters = _float_in(payload.get("height_meters"), "height_meters")
            return room

        for payload, room in _sync({str(r.uuid): r for r in floor.rooms.all()}, floor_payload.get("rooms"), build_room, pools, profile):
            room_registry.register(payload, room)
            room_payloads.append((payload, room))

    # Second pass: a room's parent may be defined later in the document than
    # the room itself, and spans_floors is a many-to-many that needs saved rows.
    for payload, room in room_payloads:
        parent = room_registry.get(payload.get("parent"))
        if parent is not None and not _would_cycle(room, parent):
            room.parent = parent
            room.save(update_fields=["parent"])
        room.spans_floors.set(floor_registry.many(payload.get("spans_floors")))

    # Elements are reconciled plan-wide (they live per floor *and* plan-level),
    # in two passes so `mounted_on` can point at an element defined later in
    # the document.
    existing_elements = {str(e.uuid): e for e in floorplan.elements.all()}
    element_payloads: list[tuple[dict, FloorplanFloor | None]] = [(payload, None) for payload in document.get("elements") or []]
    for floor_payload, floor in floors:
        element_payloads.extend((payload, floor) for payload in floor_payload.get("elements") or [])

    element_registry = _Registry()
    kept_elements: list[tuple[dict, FloorplanElement]] = []
    for order, (payload, floor) in enumerate(element_payloads):
        element = existing_elements.pop(str(payload.get("uuid") or ""), None) or FloorplanElement(floorplan=floorplan)
        element.floorplan = floorplan
        element.floor = floor
        # Loud, not coerced: silently turning an unrecognised kind into
        # "other" is how a whole class of imported element becomes wrong while
        # looking like it worked. A kind we don't know is a document we can't
        # honestly store.
        kind = payload.get("kind") or FloorplanElementKind.OTHER
        if kind not in FloorplanElementKind.values:
            raise ValueError(f"unknown element kind {kind!r} - expected one of {sorted(FloorplanElementKind.values)}")
        element.kind = kind
        element.name = payload.get("name") or ""
        element.geometry = _geometry_in(payload.get("geometry"))
        element.material = payload.get("material") or ""
        element.room = room_registry.get(payload.get("room"))
        element.mounted_on = None  # second pass
        element.base_elevation_meters = _float_in(payload.get("base_elevation_meters"), "base_elevation_meters")
        element.height_meters = _float_in(payload.get("height_meters"), "height_meters")
        element.thickness_meters = _float_in(payload.get("thickness_meters"), "thickness_meters")
        element.rotation_degrees = _float_in(payload.get("rotation_degrees"), "rotation_degrees")
        element.sort_order = order
        _apply_item(element, payload, pools, profile)
        element_registry.register(payload, element)
        kept_elements.append((payload, element))
    for orphan in existing_elements.values():
        orphan.delete()

    for payload, element in kept_elements:
        mounted = element_registry.get(payload.get("mounted_on"))
        if mounted is not None and mounted.pk != element.pk:
            element.mounted_on = mounted
            element.save(update_fields=["mounted_on"])
        element.connects_rooms.set(room_registry.many(payload.get("connects_rooms")))
        element.spans_floors.set(floor_registry.many(payload.get("spans_floors")))

        def build_lock(lock_payload: dict, row: FloorplanLock | None, *, _element=element) -> FloorplanLock:
            lock = row or FloorplanLock(element=_element)
            lock.element = _element
            lock.name = lock_payload.get("name") or ""
            lock.key_attributes = lock_payload.get("key_attributes") or {}
            return lock

        _sync({str(lk.uuid): lk for lk in element.locks.all()}, payload.get("locks"), build_lock, pools, profile)

    return floorplan
