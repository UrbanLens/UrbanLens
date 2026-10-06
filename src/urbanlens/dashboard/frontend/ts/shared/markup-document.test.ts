/**
 * The composer's drawing state: what undo and redo walk through, what the Undo and Clear buttons are
 * enabled by, what the Layers list is built from, and that saved maps - old ones included - load and
 * save unchanged.
 */
import { describe, expect, test } from "bun:test";
import { HISTORY_LIMIT, MarkupDocument, normalizeShape, type DocChange } from "./markup-document";
import type { ShapeSpec } from "./markup-engine";

const ARROW: ShapeSpec = { type: "arrow", latlngs: [[42.65, -73.75], [42.66, -73.74]], color: "#e74c3c", stroke_width: 3 };
const LINE: ShapeSpec = { type: "line", latlngs: [[42.6, -73.7], [42.61, -73.71], [42.62, -73.7]], color: "#3498db", stroke_width: 5 };
const TEXT: ShapeSpec = { type: "text", latlngs: [[42.65, -73.75]], color: "#000000", stroke_width: 16, label: "Gate", rotation: 30 };
const CIRCLE: ShapeSpec = { type: "circle", latlngs: [[42.65, -73.75], [42.651, -73.75]], color: "#2ecc71", stroke_width: 2, fill_opacity: 40, border_color: "#2ecc71" };

function docWith(...shapes: ShapeSpec[]): { doc: MarkupDocument; ids: string[] } {
    const doc = new MarkupDocument();
    const ids = shapes.map((shape) => doc.add(shape)!);
    return { doc, ids };
}

describe("MarkupDocument: what the Undo and Clear buttons follow", () => {
    test("a new document has nothing to undo, redo or clear", () => {
        const doc = new MarkupDocument();
        expect(doc.canUndo()).toBe(false);
        expect(doc.canRedo()).toBe(false);
        expect(doc.hasMarkup()).toBe(false);
    });

    test("drawing a shape makes it undoable and clearable; undoing it makes neither", () => {
        const { doc } = docWith(ARROW);
        expect(doc.canUndo()).toBe(true);
        expect(doc.hasMarkup()).toBe(true);
        expect(doc.undo()).toBe(true);
        expect(doc.hasMarkup()).toBe(false);
        expect(doc.canUndo()).toBe(false);
        expect(doc.canRedo()).toBe(true);
    });

    test("loading a saved map is not an edit: clearable, but nothing to undo", () => {
        const doc = new MarkupDocument();
        doc.load([ARROW, LINE]);
        expect(doc.hasMarkup()).toBe(true);
        expect(doc.canUndo()).toBe(false);
        expect(doc.undo()).toBe(false);
        expect(doc.items()).toHaveLength(2);
    });

    test("loading starts history over, dropping what came before", () => {
        const { doc } = docWith(ARROW, LINE);
        doc.undo();
        doc.load([TEXT]);
        expect(doc.canUndo()).toBe(false);
        expect(doc.canRedo()).toBe(false);
    });

    test("clear is undoable and brings every shape back in order", () => {
        const { doc } = docWith(ARROW, LINE, TEXT);
        expect(doc.clear()).toBe(true);
        expect(doc.hasMarkup()).toBe(false);
        expect(doc.canUndo()).toBe(true);
        expect(doc.clear()).toBe(false);
        doc.undo();
        expect(doc.items().map((item) => item.shape.type)).toEqual(["arrow", "line", "text"]);
    });
});

describe("MarkupDocument: undo covers edits, not just drawing", () => {
    test("deleting a shape is undone, and the shape keeps its id and name", () => {
        const { doc, ids } = docWith(ARROW, ARROW);
        const second = doc.get(ids[1]!)!;
        expect(doc.label(second)).toBe("Arrow 2");
        doc.remove(ids[1]!);
        expect(doc.items()).toHaveLength(1);
        doc.undo();
        expect(doc.get(ids[1]!)?.shape.type).toBe("arrow");
        expect(doc.label(doc.get(ids[1]!)!)).toBe("Arrow 2");
    });

    test("a name restored by undo is not given to the next shape drawn", () => {
        const { doc, ids } = docWith(ARROW, ARROW);
        doc.remove(ids[1]!);
        doc.undo();
        const third = doc.add(ARROW)!;
        expect(doc.label(doc.get(third)!)).toBe("Arrow 3");
    });

    test("a style change is one undo step, and redo puts it back", () => {
        const { doc, ids } = docWith(ARROW);
        doc.update(ids[0]!, (shape) => void (shape.color = "#00ff00"));
        expect(doc.get(ids[0]!)!.shape.color).toBe("#00ff00");
        doc.undo();
        expect(doc.get(ids[0]!)!.shape.color).toBe("#e74c3c");
        doc.redo();
        expect(doc.get(ids[0]!)!.shape.color).toBe("#00ff00");
    });

    test("an update that changes nothing is not an undo step", () => {
        const { doc, ids } = docWith(ARROW, LINE);
        doc.undo();
        expect(doc.update(ids[0]!, (shape) => void (shape.color = "#e74c3c"))).toBe(false);
        // A real edit would have dropped the line's redo; a no-op leaves it.
        expect(doc.canRedo()).toBe(true);
        doc.undo();
        expect(doc.hasMarkup()).toBe(false);
    });

    test("an update producing something that is not a shape is refused", () => {
        const { doc, ids } = docWith(LINE);
        expect(doc.update(ids[0]!, (shape) => void (shape.latlngs = [[1, 2]]))).toBe(false);
        expect(doc.get(ids[0]!)!.shape.latlngs).toHaveLength(3);
    });

    test("a new edit drops what could have been redone", () => {
        const { doc } = docWith(ARROW, LINE);
        doc.undo();
        expect(doc.canRedo()).toBe(true);
        doc.add(TEXT);
        expect(doc.canRedo()).toBe(false);
    });

    test("hiding, showing and reordering are each undoable", () => {
        const { doc, ids } = docWith(ARROW, LINE, TEXT);
        doc.setHidden(ids[0]!, true);
        doc.move(ids[2]!, -2);
        expect(doc.items().map((item) => item.id)).toEqual([ids[2]!, ids[0]!, ids[1]!]);
        doc.undo();
        expect(doc.items().map((item) => item.id)).toEqual(ids);
        doc.undo();
        expect(doc.get(ids[0]!)!.hidden).toBe(false);
    });

    test("history keeps the most recent steps up to its limit", () => {
        const doc = new MarkupDocument();
        for (let i = 0; i < HISTORY_LIMIT + 5; i++) doc.add(ARROW);
        let undone = 0;
        while (doc.undo()) undone++;
        expect(undone).toBe(HISTORY_LIMIT);
        expect(doc.items()).toHaveLength(5);
    });
});

describe("MarkupDocument: a drag or a held slider is one undo step", () => {
    test("every update inside a transaction is undone together", () => {
        const { doc, ids } = docWith(ARROW);
        doc.begin();
        doc.update(ids[0]!, (shape) => void (shape.stroke_width = 4));
        doc.update(ids[0]!, (shape) => void (shape.stroke_width = 6));
        doc.update(ids[0]!, (shape) => void (shape.stroke_width = 9));
        doc.commit();
        expect(doc.get(ids[0]!)!.shape.stroke_width).toBe(9);
        doc.undo();
        expect(doc.get(ids[0]!)!.shape.stroke_width).toBe(3);
        // And the one undo before that removes the arrow: the drag was a single step.
        doc.undo();
        expect(doc.hasMarkup()).toBe(false);
    });

    test("Undo is enabled mid-gesture, before the transaction closes", () => {
        const doc = new MarkupDocument();
        doc.load([ARROW]);
        const id = doc.items()[0]!.id;
        doc.begin();
        expect(doc.canUndo()).toBe(false);
        doc.update(id, (shape) => void (shape.color = "#123456"));
        expect(doc.canUndo()).toBe(true);
    });

    test("undo mid-gesture closes the gesture and undoes it", () => {
        const doc = new MarkupDocument();
        doc.load([ARROW]);
        const id = doc.items()[0]!.id;
        doc.begin();
        doc.update(id, (shape) => void (shape.color = "#123456"));
        expect(doc.undo()).toBe(true);
        expect(doc.get(id)!.shape.color).toBe("#e74c3c");
    });

    test("a transaction that ends where it began leaves no step", () => {
        const doc = new MarkupDocument();
        doc.load([ARROW]);
        const id = doc.items()[0]!.id;
        doc.begin();
        doc.update(id, (shape) => void (shape.color = "#123456"));
        doc.update(id, (shape) => void (shape.color = "#e74c3c"));
        doc.commit();
        expect(doc.canUndo()).toBe(false);
    });

    test("cancel puts back the state the transaction began from", () => {
        const { doc, ids } = docWith(ARROW);
        doc.begin();
        doc.update(ids[0]!, (shape) => void (shape.latlngs = [[0, 0], [1, 1]]));
        doc.cancel();
        expect(doc.get(ids[0]!)!.shape.latlngs).toEqual(ARROW.latlngs);
        // Only the original draw is left to undo.
        doc.undo();
        expect(doc.hasMarkup()).toBe(false);
    });

    test("nested begin joins the open transaction", () => {
        const { doc, ids } = docWith(ARROW);
        doc.begin();
        doc.update(ids[0]!, (shape) => void (shape.stroke_width = 7));
        doc.begin();
        doc.update(ids[0]!, (shape) => void (shape.stroke_width = 8));
        doc.commit();
        expect(doc.canUndo()).toBe(true);
        doc.undo();
        expect(doc.get(ids[0]!)!.shape.stroke_width).toBe(3);
    });
});

describe("MarkupDocument: what the Layers list is built from", () => {
    test("items are kept bottom first, named per type in the order drawn", () => {
        const { doc } = docWith(ARROW, LINE, ARROW, CIRCLE);
        expect(doc.items().map((item) => doc.label(item))).toEqual(["Arrow 1", "Line 1", "Arrow 2", "Circle 1"]);
    });

    test("a text label is listed by its own words, shortened when long", () => {
        const { doc, ids } = docWith(TEXT, { ...TEXT, label: "x".repeat(60) }, { ...TEXT, label: "   " });
        expect(doc.label(doc.get(ids[0]!)!)).toBe("Gate");
        expect(doc.label(doc.get(ids[1]!)!)).toBe(`${"x".repeat(39)}…`);
        expect(doc.label(doc.get(ids[2]!)!)).toBe("Text 3");
    });

    test("every change is announced, with what changed", () => {
        const doc = new MarkupDocument();
        const seen: DocChange[] = [];
        doc.subscribe((change) => seen.push(change));
        const id = doc.add(ARROW)!;
        doc.update(id, (shape) => void (shape.color = "#000000"));
        doc.select(id);
        doc.select(id);
        doc.remove(id);
        expect(seen).toEqual([{ kind: "structure" }, { kind: "items", ids: [id] }, { kind: "selection" }, { kind: "structure" }]);
    });

    test("unsubscribing stops the announcements", () => {
        const doc = new MarkupDocument();
        let count = 0;
        const off = doc.subscribe(() => count++);
        doc.add(ARROW);
        off();
        doc.add(ARROW);
        expect(count).toBe(1);
    });

    test("deleting or hiding the selected shape lets go of it", () => {
        const { doc, ids } = docWith(ARROW, LINE);
        doc.select(ids[0]!);
        doc.setHidden(ids[0]!, true);
        expect(doc.selectedId()).toBeNull();
        doc.select(ids[1]!);
        doc.remove(ids[1]!);
        expect(doc.selectedId()).toBeNull();
    });

    test("a hidden shape cannot be selected, and an unknown id selects nothing", () => {
        const { doc, ids } = docWith(ARROW);
        doc.setHidden(ids[0]!, true);
        doc.select(ids[0]!);
        expect(doc.selectedId()).toBeNull();
        doc.select("nope");
        expect(doc.selectedId()).toBeNull();
    });

    test("undoing back past a shape lets go of it if it was selected", () => {
        const { doc, ids } = docWith(ARROW);
        doc.select(ids[0]!);
        doc.undo();
        expect(doc.selectedId()).toBeNull();
    });

    test("moving stops at either end of the stack", () => {
        const { doc, ids } = docWith(ARROW, LINE);
        expect(doc.move(ids[1]!, 1)).toBe(false);
        expect(doc.move(ids[0]!, -1)).toBe(false);
        expect(doc.move(ids[0]!, 5)).toBe(true);
        expect(doc.items().map((item) => item.id)).toEqual([ids[1]!, ids[0]!]);
    });
});

describe("MarkupDocument: saving and loading", () => {
    test("shapes round-trip unchanged through load and visibleShapes", () => {
        const doc = new MarkupDocument();
        const shapes = [ARROW, LINE, TEXT, CIRCLE, { type: "pin", latlngs: [[42.65, -73.75]], color: "#e74c3c" } as ShapeSpec];
        doc.load(JSON.parse(JSON.stringify(shapes)));
        expect(doc.visibleShapes()).toEqual(shapes);
        const again = new MarkupDocument();
        again.load(doc.visibleShapes());
        expect(again.visibleShapes()).toEqual(shapes);
    });

    test("hidden shapes are left out of what is saved", () => {
        const { doc, ids } = docWith(ARROW, LINE);
        doc.setHidden(ids[0]!, true);
        expect(doc.visibleShapes()).toEqual([LINE]);
    });

    test("what is saved does not share arrays with the live drawing", () => {
        const { doc, ids } = docWith(ARROW);
        const saved = doc.visibleShapes();
        saved[0]!.latlngs[0]![0] = 0;
        expect(doc.get(ids[0]!)!.shape.latlngs[0]![0]).toBe(42.65);
    });

    test("an older map's shapes load: weight for width, {lat, lng} points, no rotation", () => {
        const doc = new MarkupDocument();
        const dropped = doc.load([
            { type: "line", latlngs: [{ lat: 42.6, lng: -73.7 }, { lat: 42.61, lng: -73.71 }], color: "#3498db", weight: 4 },
            { type: "text", latlngs: [[42.65, -73.75]], color: "#000000", stroke_width: 14, label: "Old label" },
            { type: "rect", latlngs: [[42.6, -73.7], [42.7, -73.6]], color: "#3498db", stroke_width: 3, fill_opacity: 20 },
        ]);
        expect(dropped).toBe(0);
        expect(doc.visibleShapes()).toEqual([
            { type: "line", latlngs: [[42.6, -73.7], [42.61, -73.71]], color: "#3498db", stroke_width: 4 },
            { type: "text", latlngs: [[42.65, -73.75]], color: "#000000", stroke_width: 14, label: "Old label" },
            { type: "rect", latlngs: [[42.6, -73.7], [42.7, -73.6]], color: "#3498db", stroke_width: 3, fill_opacity: 20 },
        ]);
    });

    test("entries that are not shapes are dropped and counted", () => {
        const doc = new MarkupDocument();
        const dropped = doc.load([
            null,
            "arrow",
            { type: "hexagon", latlngs: [[1, 2], [3, 4]] },
            { type: "line", latlngs: [[1, 2]] },
            { type: "line", latlngs: [[1, 2], [95, 4]] },
            { type: "line", latlngs: [[1, 2], [Number.NaN, 4]] },
            { type: "polygon", latlngs: [[1, 2], [3, 4]] },
            ARROW,
        ]);
        expect(dropped).toBe(7);
        expect(doc.items()).toHaveLength(1);
    });

    test("a null or missing markup list loads as empty", () => {
        const doc = new MarkupDocument();
        expect(doc.load(null)).toBe(0);
        expect(doc.load(undefined)).toBe(0);
        expect(doc.hasMarkup()).toBe(false);
    });
});

describe("normalizeShape", () => {
    test("keeps a text label's turn, and drops a zero turn", () => {
        expect(normalizeShape(TEXT)?.rotation).toBe(30);
        expect(normalizeShape({ ...TEXT, rotation: 0 })).not.toHaveProperty("rotation");
    });

    test("gives a text shape without words an empty label", () => {
        expect(normalizeShape({ type: "text", latlngs: [[1, 2]] })?.label).toBe("");
    });

    test("drops a turn on shapes other than text, and fields it does not know", () => {
        const shape = normalizeShape({ ...ARROW, rotation: 45, onclick: "alert(1)" });
        expect(shape).toEqual(ARROW);
    });

    test("ignores non-numeric widths and non-string colours", () => {
        expect(normalizeShape({ type: "line", latlngs: ARROW.latlngs, color: 5, stroke_width: "3" })).toEqual({ type: "line", latlngs: ARROW.latlngs });
    });
});
