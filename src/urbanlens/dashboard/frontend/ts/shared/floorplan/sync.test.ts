import { describe, expect, test } from "bun:test";

import type { FloorplanDocument } from "./document";
import { applyServerIds, snapshotForSend } from "./sync";

/** A document whose floors are deliberately not in storey order. */
function unsortedDoc(): FloorplanDocument {
    return {
        rotation_degrees: 0,
        floors: [
            { level: 0, name: "Ground", walls: [{ kind: "exterior", thickness: "normal", ax: 0, ay: 0, bx: 4, by: 0, openings: [] }], rooms: [], markers: [] },
            { level: 1, name: "First", walls: [], rooms: [{ name: "Landing", x: 1, y: 1 }], markers: [] },
            { level: -1, name: "Basement", walls: [], rooms: [], markers: [{ kind: "stair", x: 2, y: 2 }] },
        ],
    } as unknown as FloorplanDocument;
}

/** What the server sends back: storey order, per FloorplanFloor.Meta.ordering. */
function savedInStoreyOrder(): FloorplanDocument {
    return {
        rotation_degrees: 0,
        floors: [
            { uuid: "srv-basement", level: -1, walls: [], rooms: [], markers: [{ uuid: "srv-stair" }] },
            { uuid: "srv-ground", level: 0, walls: [{ uuid: "srv-wall", openings: [] }], rooms: [], markers: [] },
            { uuid: "srv-first", level: 1, walls: [], rooms: [{ uuid: "srv-landing" }], markers: [] },
        ],
    } as unknown as FloorplanDocument;
}

describe("applyServerIds", () => {
    test("floors keep their own uuid when the response is in a different order", () => {
        // FloorplanFloor orders by level, so the server always answers in
        // storey order however the payload was arranged. Matching positionally
        // handed each floor its neighbour's uuid, and the next save then
        // overwrote the wrong row.
        const doc = unsortedDoc();
        applyServerIds(snapshotForSend(doc), savedInStoreyOrder());

        expect(doc.floors.map((floor) => floor.uuid)).toEqual(["srv-ground", "srv-first", "srv-basement"]);
    });

    test("items land on the floor they belong to", () => {
        const doc = unsortedDoc();
        applyServerIds(snapshotForSend(doc), savedInStoreyOrder());

        expect(doc.floors[0]?.walls[0]?.uuid).toBe("srv-wall");
        expect(doc.floors[1]?.rooms[0]?.uuid).toBe("srv-landing");
        expect(doc.floors[2]?.markers[0]?.uuid).toBe("srv-stair");
    });

    test("a floor the server did not return is left alone", () => {
        const doc = unsortedDoc();
        const saved = savedInStoreyOrder();
        saved.floors = saved.floors.filter((floor) => floor.level !== 1);
        applyServerIds(snapshotForSend(doc), saved);

        expect(doc.floors[1]?.uuid).toBeUndefined();
        expect(doc.floors[0]?.uuid).toBe("srv-ground");
    });

    test("openings land on their own wall", () => {
        const doc = unsortedDoc();
        const ground = doc.floors[0];
        if (!ground) throw new Error("no floor");
        ground.walls[0]?.openings.push({ kind: "door", t_start: 0.4, t_end: 0.6, swing: "left" });
        ground.walls.push({ kind: "interior", thickness: "thin", ax: 4, ay: 0, bx: 4, by: 3, openings: [{ kind: "window", t_start: 0.1, t_end: 0.3, swing: "none" }] });

        const saved = savedInStoreyOrder();
        const savedGround = saved.floors[1];
        if (!savedGround) throw new Error("no saved floor");
        savedGround.walls = [
            { uuid: "srv-wall", openings: [{ uuid: "srv-door" }] },
            { uuid: "srv-wall-2", openings: [{ uuid: "srv-window" }] },
        ] as unknown as typeof savedGround.walls;
        applyServerIds(snapshotForSend(doc), saved);

        expect(ground.walls[0]?.openings[0]?.uuid).toBe("srv-door");
        expect(ground.walls[1]?.openings[0]?.uuid).toBe("srv-window");
    });

    test("renumbering the stack mid-save does not misfile the uuids", () => {
        // Deleting a floor renumbers every floor above it, and the editor does
        // that the moment the button is pressed - which can land while a save
        // is still in flight. The level a floor has when the response arrives
        // is then not the level it was sent under, so the key has to be the one
        // recorded at send time, not read back off the live object.
        const doc = unsortedDoc();
        const sent = snapshotForSend(doc);
        // The basement is deleted; ground becomes the new basement, and so on.
        doc.floors = doc.floors.filter((floor) => floor.level !== -1);
        for (const floor of doc.floors) floor.level -= 1;

        applyServerIds(sent, savedInStoreyOrder());

        expect(doc.floors.map((floor) => floor.uuid)).toEqual(["srv-ground", "srv-first"]);
    });

    test("a door's locks get their real ids back too", () => {
        // A lock left holding a client-only id is deleted as an orphan on the
        // next save and recreated under a new one, taking anything attached to
        // it with it - and the whole point of a lock record is that it
        // accumulates notes about the same physical lock.
        const doc = unsortedDoc();
        const ground = doc.floors[0];
        if (!ground) throw new Error("no floor");
        const wall = ground.walls[0];
        if (!wall) throw new Error("no wall");
        wall.openings.push({ kind: "door", t_start: 0.4, t_end: 0.6, swing: "left", locks: [{ state: "locked", name: "padlock" }] });

        const saved = savedInStoreyOrder();
        const savedGround = saved.floors[1];
        if (!savedGround) throw new Error("no saved floor");
        savedGround.walls = [{ uuid: "srv-wall", openings: [{ uuid: "srv-door", locks: [{ uuid: "srv-lock" }] }] }] as unknown as typeof savedGround.walls;

        applyServerIds(snapshotForSend(doc), saved);

        expect(wall.openings[0]?.uuid).toBe("srv-door");
        expect(wall.openings[0]?.locks?.[0]?.uuid).toBe("srv-lock");
    });

    test("an opening with no locks is not a problem", () => {
        const doc = unsortedDoc();
        expect(() => applyServerIds(snapshotForSend(doc), savedInStoreyOrder())).not.toThrow();
    });

    test("a pool row takes its real uuid, and what cites it follows", () => {
        // _Pools looks the existing pool up by real uuid, so a second save
        // still carrying "local-1" matches nothing, creates a second row and
        // deletes the first as stale - the row destroyed and rebuilt on every
        // autosave, with nothing visible to show for it.
        const doc = unsortedDoc();
        const ground = doc.floors[0];
        if (!ground) throw new Error("no floor");
        (ground.walls[0] as { references?: string[] }).references = ["local-1"];
        doc.reference_pool = [{ uuid: "local-1", kind: "photo", image_uuid: "image-a" }];

        const saved = savedInStoreyOrder();
        saved.reference_pool = [{ uuid: "srv-ref", kind: "photo", image_uuid: "image-a" }];

        applyServerIds(snapshotForSend(doc), saved);

        expect(doc.reference_pool?.[0]?.uuid).toBe("srv-ref");
        expect((ground.walls[0] as { references?: string[] }).references).toEqual(["srv-ref"]);
    });

    test("the pool is matched by position, in the order it was sent", () => {
        // Safe because _Pools writes sort_order from the payload index and both
        // pool models order by it, so the list comes back as it went out.
        const doc = unsortedDoc();
        doc.reference_pool = [
            { uuid: "local-1", image_uuid: "image-a" },
            { uuid: "local-2", image_uuid: "image-b" },
        ];
        const saved = savedInStoreyOrder();
        saved.reference_pool = [
            { uuid: "srv-a", image_uuid: "image-a" },
            { uuid: "srv-b", image_uuid: "image-b" },
        ];

        applyServerIds(snapshotForSend(doc), saved);

        expect(doc.reference_pool?.map((row) => row.uuid)).toEqual(["srv-a", "srv-b"]);
    });

    test("a reference with no image gets its real id too", () => {
        // Added by URL rather than from a photo. Matching on the image it stood
        // for could not name this one at all, so it kept a client-side id and
        // was destroyed and rebuilt on every save.
        const doc = unsortedDoc();
        doc.reference_pool = [{ uuid: "local-1", url: "https://example.test/plan.pdf" }];
        const saved = savedInStoreyOrder();
        saved.reference_pool = [{ uuid: "srv-1", url: "https://example.test/plan.pdf" }];

        applyServerIds(snapshotForSend(doc), saved);

        expect(doc.reference_pool?.[0]?.uuid).toBe("srv-1");
    });

    test("a lock's citation is repointed too", () => {
        const doc = unsortedDoc();
        const ground = doc.floors[0];
        if (!ground) throw new Error("no floor");
        const wall = ground.walls[0];
        if (!wall) throw new Error("no wall");
        wall.openings.push({ kind: "door", t_start: 0.4, t_end: 0.6, swing: "none", locks: [{ state: "locked", references: ["local-1"] }] });
        doc.reference_pool = [{ uuid: "local-1", image_uuid: "image-a" }];
        const saved = savedInStoreyOrder();
        saved.reference_pool = [{ uuid: "srv-ref", image_uuid: "image-a" }];

        applyServerIds(snapshotForSend(doc), saved);

        expect(wall.openings[0]?.locks?.[0]?.references).toEqual(["srv-ref"]);
    });

    test("a snapshot is immune to edits made while the save is in flight", () => {
        // The whole reason the snapshot exists: deleting the first wall before
        // the response lands must not shift every uuid onto its neighbour.
        const doc = unsortedDoc();
        const ground = doc.floors[0];
        if (!ground) throw new Error("no floor");
        const original = ground.walls[0];
        const sent = snapshotForSend(doc);
        ground.walls.splice(0, 1);

        applyServerIds(sent, savedInStoreyOrder());

        expect(original?.uuid).toBe("srv-wall");
    });
});
