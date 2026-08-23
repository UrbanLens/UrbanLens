/**
 * Which of a room's boundary walls belong to the room itself.
 *
 * The wall-first model stores no rooms, only walls - a room is a face the walls
 * happen to enclose - so "move this room" and "delete this room" have to decide
 * which walls travel with it and which merely bound it.
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
 *
 * A wall is the room's own when it bounds no other face - with one exclusion
 * that is load-bearing and easy to talk yourself out of. Topologically an
 * exterior wall usually does bound exactly one room: in a shell split by a
 * single partition, the west wall bounds only the west room. Handing that wall
 * to the room means dragging the room tears the side off the building, which is
 * what a purely topological rule did.
 *
 * The exclusion lifts when the face's *entire* boundary is exterior. Such a face
 * is a closed structure in its own right - a shed, or a building nobody has
 * subdivided yet - so its walls bound it and nothing else, and moving or
 * deleting them affects only that structure. Without this, a shed someone had
 * deliberately named was a room that could not be moved or deleted, which is a
 * room in name only.
 *
 * Args:
 *     face: The region the room sits in.
 *     walls: Every wall on the floor.
 *     faces: Every derived face on the floor, used to tell "bounds this room"
 *         from "bounds this room and its neighbour".
 *
 * Returns:
 *     The boundary, split.
 */
export function splitRoomBoundary(face: Face, walls: readonly Wall[], faces: readonly Face[]): RoomBoundary {
    const boundary = walls.filter((wall) => face.wallIds.includes(wallId(wall)));
    const standalone = boundary.length > 0 && boundary.every((wall) => wall.kind === "exterior");
    const bordersAnother = (wall: Wall): boolean => faces.some((other) => other !== face && other.wallIds.includes(wallId(wall)));
    const unique = boundary.filter((wall) => (standalone || wall.kind !== "exterior") && !bordersAnother(wall));
    const shared = boundary.filter((wall) => !unique.includes(wall));
    return { face, unique, shared };
}
