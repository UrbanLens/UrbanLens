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


def _item_out(row: FloorplanItem, source_uuids: dict, reference_uuids: dict) -> dict[str, Any]:
    """The shared item fields, exactly as REData emits them, plus local labels."""
    return {
        "uuid": str(row.uuid),
        "description": row.description,
        "condition": row.condition,
        "built_date": row.built_date.isoformat() if row.built_date else None,
        "attributes": row.attributes or {},
        "source": source_uuids.get(row.source_id),
        "references": [reference_uuids[ref.pk] for ref in row.references.all() if ref.pk in reference_uuids],
        "labels": [str(label.uuid) for label in row.labels.all()],
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

    floors = list(floorplan.floors.prefetch_related("references", "labels", "rooms__references", "rooms__labels"))
    elements = list(floorplan.elements.prefetch_related("references", "labels", "locks__references", "locks__labels"))
    element_uuids: dict[int | None, str] = {row.pk: str(row.uuid) for row in elements}
    room_uuids: dict[int | None, str] = {room.pk: str(room.uuid) for floor in floors for room in floor.rooms.all()}

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
                    }
                    for room in floor.rooms.all()
                ],
                "elements": [_element_dict(row) for row in elements_by_floor.get(floor.pk, [])],
            }
            for floor in floors
        ],
        "elements": [_element_dict(row) for row in elements_by_floor.get(None, [])],
    }


def _date_in(raw) -> datetime.date | None:
    """Parse an ISO date or None.

    Raises:
        ValueError: Not an ISO date.
    """
    if not raw:
        return None
    return datetime.date.fromisoformat(str(raw))


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
            self.sources[str(payload.get("uuid") or row.uuid)] = row
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
            self.references[str(payload.get("uuid") or row.uuid)] = row
        for orphan in existing.values():
            orphan.delete()


def _apply_item(row: FloorplanItem, payload: dict[str, Any], pools: _Pools, profile: Profile | None) -> None:
    """Write the shared item surface from a payload; saves the row."""
    from urbanlens.dashboard.models.labels.model import Label

    row.description = payload.get("description") or ""
    row.condition = payload.get("condition") or ""
    row.built_date = _date_in(payload.get("built_date"))
    row.attributes = payload.get("attributes") or {}
    row.source = pools.sources.get(str(payload.get("source") or ""))
    row.save()
    row.references.set([pools.references[key] for key in (payload.get("references") or []) if key in pools.references])
    if profile is not None:
        # Scoped to the saver's own labels: a document cannot attach somebody
        # else's label by guessing its uuid.
        row.labels.set(Label.objects.filter(uuid__in=payload.get("labels") or [], profile=profile))


def _sync(existing_by_uuid: dict, payloads: list[dict], build, pools: _Pools, profile: Profile | None) -> list[tuple[dict, Any]]:
    """Reconcile one collection: update by uuid, create the new, delete the omitted."""
    kept: list[tuple[dict, Any]] = []
    for payload in payloads or []:
        row = existing_by_uuid.pop(str(payload.get("uuid") or ""), None)
        row = build(payload, row)
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
    floorplan.floor_count = document.get("floor_count")
    _apply_item(floorplan, document, pools, profile)

    def build_floor(payload: dict, row: FloorplanFloor | None) -> FloorplanFloor:
        floor = row or FloorplanFloor(floorplan=floorplan)
        floor.floorplan = floorplan
        floor.level = int(payload.get("level") or 0)
        floor.name = payload.get("name") or ""
        floor.geometry = _geometry_in(payload.get("geometry"))
        floor.elevation_meters = payload.get("elevation_meters")
        floor.height_meters = payload.get("height_meters")
        return floor

    floors = _sync({str(f.uuid): f for f in floorplan.floors.all()}, document.get("floors"), build_floor, pools, profile)

    rooms_by_uuid: dict[str, FloorplanRoom] = {}
    for floor_payload, floor in floors:

        def build_room(payload: dict, row: FloorplanRoom | None, *, _floor=floor) -> FloorplanRoom:
            room = row or FloorplanRoom(floor=_floor)
            room.floor = _floor
            room.name = payload.get("name") or ""
            room.geometry = _geometry_in(payload.get("geometry"))
            room.height_meters = payload.get("height_meters")
            return room

        for payload, room in _sync({str(r.uuid): r for r in floor.rooms.all()}, floor_payload.get("rooms"), build_room, pools, profile):
            rooms_by_uuid[str(payload.get("uuid") or room.uuid)] = room

    # Elements are reconciled plan-wide (they live per floor *and* plan-level),
    # in two passes so `mounted_on` can point at an element defined later in
    # the document.
    existing_elements = {str(e.uuid): e for e in floorplan.elements.all()}
    element_payloads: list[tuple[dict, FloorplanFloor | None]] = [(payload, None) for payload in document.get("elements") or []]
    for floor_payload, floor in floors:
        element_payloads.extend((payload, floor) for payload in floor_payload.get("elements") or [])

    elements_by_uuid: dict[str, FloorplanElement] = {}
    kept_elements: list[tuple[dict, FloorplanElement]] = []
    for payload, floor in element_payloads:
        element = existing_elements.pop(str(payload.get("uuid") or ""), None) or FloorplanElement(floorplan=floorplan)
        element.floorplan = floorplan
        element.floor = floor
        element.kind = payload.get("kind") if payload.get("kind") in FloorplanElementKind.values else FloorplanElementKind.OTHER
        element.name = payload.get("name") or ""
        element.geometry = _geometry_in(payload.get("geometry"))
        element.material = payload.get("material") or ""
        element.room = rooms_by_uuid.get(str(payload.get("room") or ""))
        element.mounted_on = None  # second pass
        element.base_elevation_meters = payload.get("base_elevation_meters")
        element.height_meters = payload.get("height_meters")
        _apply_item(element, payload, pools, profile)
        elements_by_uuid[str(payload.get("uuid") or element.uuid)] = element
        kept_elements.append((payload, element))
    for orphan in existing_elements.values():
        orphan.delete()

    for payload, element in kept_elements:
        mounted = elements_by_uuid.get(str(payload.get("mounted_on") or ""))
        if mounted is not None and mounted.pk != element.pk:
            element.mounted_on = mounted
            element.save(update_fields=["mounted_on"])

        def build_lock(lock_payload: dict, row: FloorplanLock | None, *, _element=element) -> FloorplanLock:
            lock = row or FloorplanLock(element=_element)
            lock.element = _element
            lock.name = lock_payload.get("name") or ""
            lock.key_attributes = lock_payload.get("key_attributes") or {}
            return lock

        _sync({str(lk.uuid): lk for lk in element.locks.all()}, payload.get("locks"), build_lock, pools, profile)

    return floorplan
