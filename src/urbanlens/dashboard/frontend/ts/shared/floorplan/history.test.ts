import { describe, expect, test } from "bun:test";

import { History } from "./history";

interface Doc {
    name: string;
    walls: number;
}

const clone = (d: Doc): Doc => ({ ...d });
const make = (limit?: number): History<Doc> => new History<Doc>(clone, limit);

describe("History", () => {
    test("starts with nothing to undo or redo", () => {
        const history = make();
        expect(history.canUndo).toBe(false);
        expect(history.canRedo).toBe(false);
        expect(history.undo({ name: "a", walls: 0 })).toBeNull();
        expect(history.redo({ name: "a", walls: 0 })).toBeNull();
    });

    test("one checkpoint per gesture means one undo per gesture", () => {
        const history = make();
        let doc: Doc = { name: "", walls: 0 };

        history.checkpoint(doc);
        doc = { ...doc, walls: 4 };
        history.checkpoint(doc);
        doc = { ...doc, walls: 8 };

        doc = history.undo(doc) as Doc;
        expect(doc.walls).toBe(4);
        doc = history.undo(doc) as Doc;
        expect(doc.walls).toBe(0);
        expect(history.canUndo).toBe(false);
    });

    test("redo replays exactly what undo took back", () => {
        const history = make();
        let doc: Doc = { name: "", walls: 0 };
        history.checkpoint(doc);
        doc = { ...doc, walls: 4 };

        doc = history.undo(doc) as Doc;
        expect(doc.walls).toBe(0);
        expect(history.canRedo).toBe(true);

        doc = history.redo(doc) as Doc;
        expect(doc.walls).toBe(4);
        expect(history.canRedo).toBe(false);
    });

    test("a new checkpoint discards the redo branch", () => {
        const history = make();
        let doc: Doc = { name: "", walls: 0 };
        history.checkpoint(doc);
        doc = { ...doc, walls: 4 };
        doc = history.undo(doc) as Doc;
        expect(history.canRedo).toBe(true);

        history.checkpoint(doc);

        expect(history.canRedo).toBe(false);
    });

    test("a group collapses a run of keystrokes into one step", () => {
        const history = make();
        let doc: Doc = { name: "", walls: 0 };

        for (const value of ["B", "Bo", "Boi", "Boiler"]) {
            history.checkpoint(doc, "room-name:1");
            doc = { ...doc, name: value };
        }

        expect(history.depth).toBe(1);
        doc = history.undo(doc) as Doc;
        expect(doc.name).toBe("");
    });

    test("a different group closes the previous one", () => {
        const history = make();
        let doc: Doc = { name: "", walls: 0 };
        history.checkpoint(doc, "room-name:1");
        doc = { ...doc, name: "Boiler" };
        history.checkpoint(doc, "room-name:2");
        doc = { ...doc, name: "Boiler/Store" };

        expect(history.depth).toBe(2);
        expect((history.undo(doc) as Doc).name).toBe("Boiler");
    });

    test("an ungrouped checkpoint closes an open group", () => {
        const history = make();
        let doc: Doc = { name: "", walls: 0 };
        history.checkpoint(doc, "room-name:1");
        doc = { ...doc, name: "Boiler" };
        history.checkpoint(doc);
        doc = { ...doc, walls: 4 };

        expect(history.depth).toBe(2);
        doc = history.undo(doc) as Doc;
        expect(doc).toEqual({ name: "Boiler", walls: 0 });
    });

    test("undoing reopens grouping, so typing after an undo records again", () => {
        const history = make();
        let doc: Doc = { name: "", walls: 0 };
        history.checkpoint(doc, "room-name:1");
        doc = { ...doc, name: "Boiler" };
        doc = history.undo(doc) as Doc;

        history.checkpoint(doc, "room-name:1");

        expect(history.depth).toBe(1);
    });

    test("snapshots do not alias the live document", () => {
        const history = make();
        const doc: Doc = { name: "", walls: 0 };
        history.checkpoint(doc);
        doc.walls = 99;

        expect((history.undo(doc) as Doc).walls).toBe(0);
    });

    test("the oldest step is dropped past the limit", () => {
        const history = make(3);
        let doc: Doc = { name: "", walls: 0 };
        for (let i = 1; i <= 5; i++) {
            history.checkpoint(doc);
            doc = { ...doc, walls: i };
        }

        expect(history.depth).toBe(3);
        // Five gestures, three retained: the earliest reachable state is the
        // one before the third, not the original.
        let last: Doc = doc;
        while (history.canUndo) last = history.undo(last) as Doc;
        expect(last.walls).toBe(2);
    });

    test("clear forgets both directions", () => {
        const history = make();
        let doc: Doc = { name: "", walls: 0 };
        history.checkpoint(doc);
        doc = { ...doc, walls: 4 };
        history.undo(doc);

        history.clear();

        expect(history.canUndo).toBe(false);
        expect(history.canRedo).toBe(false);
    });

    test("clear also closes any open group", () => {
        const history = make();
        const doc: Doc = { name: "", walls: 0 };
        history.checkpoint(doc, "room-name:1");
        history.clear();

        history.checkpoint(doc, "room-name:1");

        expect(history.depth).toBe(1);
    });
});
