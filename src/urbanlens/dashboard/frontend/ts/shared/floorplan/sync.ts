/**
 * Landing a save's server-assigned uuids back on the objects that were sent.
 */

import type { Floor, FloorplanDocument, ItemDetails, Lock, Marker, Opening, Reference, RoomSeed, Wall } from "./document";

/**
 * A frozen-order, same-object record of what a save's payload actually held,
 * taken at the moment it was sent - see snapshotForSend().
 */
export interface SentSnapshot {
    floor: Floor;
    /**
 * The floor's level *as sent*.
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
 * Record each floor's item arrays *in the order sent*, without copying the items themselves.
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
 */
function applyPoolIds(sent: SentDocument, saved: FloorplanDocument): void {
    const renamed = new Map<string, string>();
    (saved.reference_pool ?? []).forEach((row, index) => {
        const was = sent.pool[index];
        if (!was?.uuid || !row.uuid || was.uuid === row.uuid) return;
        renamed.set(was.uuid, row.uuid);
        was.uuid = row.uuid;
    });
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
