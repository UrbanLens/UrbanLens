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
 * The room's own are its partitions: interior walls, fences, anything that is
 * not the building's shell. Those travel with it. The shell does not - dragging
 * a room must never tear the side off the building, which is what a purely
 * topological rule did when it was tried.
 *
 * Bordering another face does not make a wall shared. In a planar subdivision
 * *every* interior partition borders two faces, so requiring otherwise left a
 * closet inside a building owning nothing at all - and a room with no walls of
 * its own cannot be moved, turned or deleted, which is precisely what those
 * three gestures decline to do. A partition between two rooms belongs to both,
 * and moving one room into its neighbour is an ordinary edit that leaves the
 * neighbour smaller.
 *
 * The one exception runs the other way. A face whose *entire* boundary is
 * exterior is a closed structure in its own right - a shed, or a building
 * nobody has subdivided yet - so its walls bound it and nothing else, and there
 * is no side of anything else to tear off.
 *
 * Args:
 *     face: The region the room sits in.
 *     walls: Every wall on the floor.
 *
 * Returns:
 *     The boundary, split.
 */
export function splitRoomBoundary(face: Face, walls: readonly Wall[]): RoomBoundary {
    const boundary = walls.filter((wall) => face.wallIds.includes(wallId(wall)));
    const standalone = boundary.length > 0 && boundary.every((wall) => wall.kind === "exterior");
    const unique = boundary.filter((wall) => standalone || wall.kind !== "exterior");
    const shared = boundary.filter((wall) => !unique.includes(wall));
    return { face, unique, shared };
}
