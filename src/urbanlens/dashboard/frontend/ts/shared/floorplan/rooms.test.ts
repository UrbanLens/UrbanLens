import { describe, expect, test } from "bun:test";

import type { Wall } from "./document";
import type { Face } from "./planar";
import { splitRoomBoundary } from "./rooms";

function wall(uuid: string, kind: Wall["kind"]): Wall {
    return { uuid, kind, thickness: "normal", ax: 0, ay: 0, bx: 1, by: 0, openings: [] };
}

function face(ids: string[]): Face {
    return { wallIds: ids, ring: [], area: 10 } as unknown as Face;
}

/** An 8x4 shell split by one partition: two rooms, west and east. */
function splitShell(): { walls: Wall[]; west: Face; east: Face; faces: Face[] } {
    const walls = [
        wall("north", "exterior"),
        wall("south", "exterior"),
        wall("west", "exterior"),
        wall("east", "exterior"),
        wall("partition", "interior"),
    ];
    const west = face(["north", "south", "west", "partition"]);
    const east = face(["north", "south", "east", "partition"]);
    return { walls, west, east, faces: [west, east] };
}

describe("splitRoomBoundary", () => {
    test("a room does not own the building's side", () => {
        // The reverted bug: topologically `west` bounds only the west room, so
        // a purely topological rule hands it over and dragging the room tears
        // the side off the building.
        const { walls, west, faces } = splitShell();

        const boundary = splitRoomBoundary(west, walls, faces);

        expect(boundary.shared.map((w) => w.uuid)).toEqual(["north", "south", "west"]);
        expect(boundary.unique.map((w) => w.uuid)).not.toContain("west");
    });

    test("a room owns the partitions on its boundary", () => {
        // In a planar subdivision every interior partition borders two faces,
        // so requiring "borders nothing else" left a closet inside a building
        // owning nothing at all - and a room with no walls of its own declines
        // to be moved, turned or deleted.
        const { walls, west, faces } = splitShell();

        const boundary = splitRoomBoundary(west, walls, faces);

        expect(boundary.unique.map((w) => w.uuid)).toEqual(["partition"]);
    });

    test("a partition between two rooms belongs to both", () => {
        // Moving one room into its neighbour is an ordinary edit; the neighbour
        // gets smaller.
        const { walls, west, east, faces } = splitShell();

        expect(splitRoomBoundary(west, walls, faces).unique.map((w) => w.uuid)).toContain("partition");
        expect(splitRoomBoundary(east, walls, faces).unique.map((w) => w.uuid)).toContain("partition");
    });

    test("a closet built into a corner owns its two partitions", () => {
        const walls = [wall("north", "exterior"), wall("west", "exterior"), wall("p1", "interior"), wall("p2", "interior")];
        const closet = face(["north", "west", "p1", "p2"]);
        const rest = face(["north", "west", "p1", "p2"]);

        const boundary = splitRoomBoundary(closet, walls, [closet, rest]);

        expect(boundary.unique.map((w) => w.uuid)).toEqual(["p1", "p2"]);
        expect(boundary.shared.map((w) => w.uuid)).toEqual(["north", "west"]);
    });

    test("a fence counts as the room's own, not as shell", () => {
        const walls = [wall("north", "exterior"), wall("paddock", "fence")];
        const yard = face(["north", "paddock"]);

        expect(splitRoomBoundary(yard, walls, [yard]).unique.map((w) => w.uuid)).toEqual(["paddock"]);
    });

    test("a structure bounded only by exterior wall owns all of it", () => {
        // A shed, or a building nobody has subdivided. Its walls bound it and
        // nothing else, so there is no side of anything else to tear off - and
        // without this such a room could be named but never moved or deleted.
        const walls = [wall("n", "exterior"), wall("s", "exterior"), wall("e", "exterior"), wall("w", "exterior")];
        const shed = face(["n", "s", "e", "w"]);

        const boundary = splitRoomBoundary(shed, walls, [shed]);

        expect(boundary.unique.map((w) => w.uuid)).toEqual(["n", "s", "e", "w"]);
        expect(boundary.shared).toEqual([]);
    });

    test("a shed beside a building leaves the building's sides alone", () => {
        const { walls, east, faces } = splitShell();
        const shedWalls = ["shed-n", "shed-s", "shed-e", "shed-w"];
        for (const id of shedWalls) walls.push(wall(id, "exterior"));
        const shed = face(shedWalls);

        expect(splitRoomBoundary(shed, walls, [...faces, shed]).unique.map((w) => w.uuid)).toEqual(shedWalls);
        expect(splitRoomBoundary(east, walls, [...faces, shed]).shared.map((w) => w.uuid)).toEqual(["north", "south", "east"]);
    });

    test("a face with no walls on this floor splits to nothing", () => {
        expect(splitRoomBoundary(face(["gone"]), [], [])).toMatchObject({ unique: [], shared: [] });
    });
});
