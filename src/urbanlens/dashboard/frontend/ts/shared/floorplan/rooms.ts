/**
 * Which of a room's boundary walls belong to the room itself.
 */

import type { Face } from "./planar";
import { type Wall, wallId } from "./document";

/** A face's walls, split by whether they are the room's own. */
export interface RoomBoundary {
    face: Face;
    /** Walls this room alone relies on. These travel with it. */
    unique: Wall[];
    /** Everything else on the boundary, which stretches to keep up. */
    shared: Wall[];
}

/**
 * Split a face's boundary into the room's own walls and the rest.
 */
export function splitRoomBoundary(face: Face, walls: readonly Wall[]): RoomBoundary {
    const boundary = walls.filter((wall) => face.wallIds.includes(wallId(wall)));
    const standalone = boundary.length > 0 && boundary.every((wall) => wall.kind === "exterior");
    const unique = boundary.filter((wall) => standalone || wall.kind !== "exterior");
    const shared = boundary.filter((wall) => !unique.includes(wall));
    return { face, unique, shared };
}
