/**
 * Landing a save's server-assigned uuids back on the objects that were sent.
 *
 * Extracted from the editor so the matching rules can be tested directly: they
 * are the difference between an item keeping its identity across a save and an
 * item quietly acquiring a different row's uuid, which the next save then
 * overwrites.
 */

import type { Floor, FloorplanDocument, ItemDetails, Lock, Marker, Opening, Reference, RoomSeed, Wall } from "./document";

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
    /** Each opening's locks, in the order sent. Indexed [wall][opening]. */
    openingLocks: Lock[][][];
    rooms: RoomSeed[];
    markers: Marker[];
}

/** Everything one save sent, frozen at the moment it went. */
export interface SentDocument {
    floors: SentSnapshot[];
    /** The reference pool as sent, so its rows can be renamed on the way back. */
    pool: Reference[];
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
export function snapshotForSend(doc: FloorplanDocument): SentDocument {
    const floors = doc.floors.map((floor) => ({
        floor,
        level: floor.level,
        walls: [...floor.walls],
        wallOpenings: floor.walls.map((wall) => [...wall.openings]),
        openingLocks: floor.walls.map((wall) => wall.openings.map((opening) => [...(opening.locks ?? [])])),
        rooms: [...floor.rooms],
        markers: [...floor.markers],
    }));
    return { floors, pool: [...(doc.reference_pool ?? [])] };
}

/** Every item on a floor that can carry details, the floor itself included. */
function itemsOf(entry: SentSnapshot): ItemDetails[] {
    const items: ItemDetails[] = [entry.floor];
    for (let wallIndex = 0; wallIndex < entry.walls.length; wallIndex++) {
        items.push(entry.walls[wallIndex] as ItemDetails);
        for (let openingIndex = 0; openingIndex < (entry.wallOpenings[wallIndex]?.length ?? 0); openingIndex++) {
            items.push(entry.wallOpenings[wallIndex]?.[openingIndex] as ItemDetails);
            for (const lock of entry.openingLocks[wallIndex]?.[openingIndex] ?? []) items.push(lock);
        }
    }
    items.push(...entry.rooms, ...entry.markers);
    return items;
}

/**
 * Give the pool's rows their real uuids, and repoint what cites them.
 *
 * Matched on the image each row stands for rather than by position: the pool
 * has no declared ordering, so the order it comes back in is whatever the
 * database felt like. A row with no image - a reference added by URL - has no
 * key to match on and keeps whatever it had.
 *
 * This matters because _Pools looks the existing pool up by real uuid: a second
 * save still carrying "local-3" matches nothing, creates a second row and
 * deletes the first as stale. The citation follows, so nothing visible breaks -
 * the row is simply destroyed and rebuilt on every autosave.
 *
 * Args:
 *     sent: What snapshotForSend recorded.
 *     saved: The document the server returned.
 */
function applyPoolIds(sent: SentDocument, saved: FloorplanDocument): void {
    const savedByImage = new Map<string, string>();
    for (const row of saved.reference_pool ?? []) {
        if (row.image_uuid && row.uuid) savedByImage.set(row.image_uuid, row.uuid);
    }
    const renamed = new Map<string, string>();
    for (const row of sent.pool) {
        const real = row.image_uuid ? savedByImage.get(row.image_uuid) : undefined;
        if (!real || !row.uuid || row.uuid === real) continue;
        renamed.set(row.uuid, real);
        row.uuid = real;
    }
    if (!renamed.size) return;
    for (const entry of sent.floors) {
        for (const item of itemsOf(entry)) {
            if (!item.references?.length) continue;
            item.references = item.references.map((uuid) => renamed.get(uuid) ?? uuid);
        }
    }
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
 * locks, rooms and markers all order by ``sort_order`` first. Only orphans - items
 * already absent from the payload - are missing from the response.
 *
 * Args:
 *     sent: What snapshotForSend() recorded for this request.
 *     saved: The document the server returned for it.
 */
export function applyServerIds(sent: SentDocument, saved: FloorplanDocument): void {
    applyPoolIds(sent, saved);
    const byLevel = new Map<number, SentSnapshot>();
    for (const entry of sent.floors) byLevel.set(entry.level, entry);

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
                // Locks are rows too, matched by uuid within their own opening.
                // One left holding a client-only id is deleted as an orphan on
                // the next save and recreated under a new one, taking whatever
                // had been attached to it along with it.
                (savedOpening.locks || []).forEach((savedLock, lockIndex) => {
                    const lock = entry.openingLocks[wallIndex]?.[openingIndex]?.[lockIndex];
                    if (lock) lock.uuid = savedLock.uuid;
                });
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
