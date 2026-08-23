/**
 * Landing a save's server-assigned uuids back on the objects that were sent.
 *
 * Extracted from the editor so the matching rules can be tested directly: they
 * are the difference between an item keeping its identity across a save and an
 * item quietly acquiring a different row's uuid, which the next save then
 * overwrites.
 */

import type { Floor, FloorplanDocument, Marker, Opening, RoomSeed, Wall } from "./document";

/**
 * A frozen-order, same-object record of what a save's payload actually held,
 * taken at the moment it was sent - see snapshotForSend().
 */
export interface SentSnapshot {
    floor: Floor;
    /**
     * The floor's level *as sent*. Deleting a floor renumbers every floor above
     * it, and the editor does that immediately - so by the time a response
     * arrives the live object's level may no longer be the one it was saved
     * under, and reading it back off the object would misfile the uuid.
     */
    level: number;
    walls: Wall[];
    wallOpenings: Opening[][];
    rooms: RoomSeed[];
    markers: Marker[];
}

/**
 * Record each floor's item arrays *in the order sent*, without copying the
 * items themselves.
 *
 * Editing - including deleting something - can continue on the live document
 * while a save is in flight, and applyServerIds() must still land each returned
 * uuid on the exact object that was actually sent, not on whatever a live array
 * index happens to point at once the response arrives.
 *
 * Args:
 *     doc: The document being sent.
 *
 * Returns:
 *     One entry per floor, holding that floor's arrays as they were.
 */
export function snapshotForSend(doc: FloorplanDocument): SentSnapshot[] {
    return doc.floors.map((floor) => ({
        floor,
        level: floor.level,
        walls: [...floor.walls],
        wallOpenings: floor.walls.map((wall) => [...wall.openings]),
        rooms: [...floor.rooms],
        markers: [...floor.markers],
    }));
}

/**
 * Copy the server's real per-item uuids back onto the objects a save sent.
 *
 * Not matched by uuid, because the client's own new items do not have a real
 * one yet, and not by live array position, because the document may have moved
 * on (see snapshotForSend()).
 *
 * Floors are matched by the ``level`` each was *sent* under. A document cannot
 * hold two floors at the same storey - the server rejects that outright - so
 * level is a real key, and it is the only one both sides agree on before the
 * uuids exist. Position would
 * not do: ``FloorplanFloor.Meta.ordering`` is ``("level", "sort_order", "id")``,
 * so the server returns floors in storey order regardless of the order they
 * were sent in. That happens to match today only because the editor sorts its
 * floors by level on every change; a single append that skipped it would have
 * handed each floor its neighbour's uuid.
 *
 * Items *within* a floor are matched positionally, which is sound: ``_sync``
 * assigns ``sort_order`` from the payload's array index, and walls, openings,
 * rooms and markers all order by ``sort_order`` first. Only orphans - items
 * already absent from the payload - are missing from the response.
 *
 * Args:
 *     sent: What snapshotForSend() recorded for this request.
 *     saved: The document the server returned for it.
 */
export function applyServerIds(sent: SentSnapshot[], saved: FloorplanDocument): void {
    const byLevel = new Map<number, SentSnapshot>();
    for (const entry of sent) byLevel.set(entry.level, entry);

    for (const savedFloor of saved.floors || []) {
        const entry = byLevel.get(savedFloor.level);
        if (!entry) continue;
        entry.floor.uuid = savedFloor.uuid;
        (savedFloor.walls || []).forEach((savedWall, wallIndex) => {
            const wall = entry.walls[wallIndex];
            if (!wall) return;
            wall.uuid = savedWall.uuid;
            (savedWall.openings || []).forEach((savedOpening, openingIndex) => {
                const opening = entry.wallOpenings[wallIndex]?.[openingIndex];
                if (opening) opening.uuid = savedOpening.uuid;
            });
        });
        (savedFloor.rooms || []).forEach((savedRoom, roomIndex) => {
            const room = entry.rooms[roomIndex];
            if (room) room.uuid = savedRoom.uuid;
        });
        (savedFloor.markers || []).forEach((savedMarker, markerIndex) => {
            const marker = entry.markers[markerIndex];
            if (marker) marker.uuid = savedMarker.uuid;
        });
    }
}
