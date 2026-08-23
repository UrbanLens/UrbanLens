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
        // Topologically `west` bounds only the west room, so a purely
        // topological rule hands it over and dragging the room tears the side
        // off the building. That was tried, and reverted.
        const { walls, west, faces } = splitShell();

        const boundary = splitRoomBoundary(west, walls, faces);

        expect(boundary.unique.map((w) => w.uuid)).toEqual([]);
    });

    test("a room owns a partition nothing else borders", () => {
        const { walls, west, faces } = splitShell();
        walls.push(wall("closet", "interior"));
        west.wallIds.push("closet");

        const boundary = splitRoomBoundary(west, walls, faces);

        expect(boundary.unique.map((w) => w.uuid)).toEqual(["closet"]);
    });

    test("a shared partition belongs to neither room", () => {
        const { walls, west, faces } = splitShell();

        const boundary = splitRoomBoundary(west, walls, faces);

        expect(boundary.shared.map((w) => w.uuid)).toContain("partition");
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

    test("a shed beside a building still owns only its own walls", () => {
        const { walls, west, east, faces } = splitShell();
        const shedWalls = ["shed-n", "shed-s", "shed-e", "shed-w"];
        for (const id of shedWalls) walls.push(wall(id, "exterior"));
        const shed = face(shedWalls);

        const boundary = splitRoomBoundary(shed, walls, [...faces, shed]);

        expect(boundary.unique.map((w) => w.uuid)).toEqual(shedWalls);
        // And the building's own rooms are unaffected by the shed existing.
        expect(splitRoomBoundary(east, walls, [west, east, shed]).unique).toEqual([]);
    });

    test("an exterior wall shared by two all-exterior faces stays shared", () => {
        // Two sheds built against each other: the party wall bounds both, so it
        // is nobody's to drag.
        const walls = [wall("party", "exterior"), wall("a1", "exterior"), wall("b1", "exterior")];
        const left = face(["party", "a1"]);
        const right = face(["party", "b1"]);

        const boundary = splitRoomBoundary(left, walls, [left, right]);

        expect(boundary.unique.map((w) => w.uuid)).toEqual(["a1"]);
        expect(boundary.shared.map((w) => w.uuid)).toEqual(["party"]);
    });

    test("a face with no walls on this floor splits to nothing", () => {
        expect(splitRoomBoundary(face(["gone"]), [], [])).toMatchObject({ unique: [], shared: [] });
    });
});
