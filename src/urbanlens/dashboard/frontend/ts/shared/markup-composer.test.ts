/**
 * The composer's controls kept in step with its drawing: Undo, Redo and Clear enabled only when they
 * would do something, the Layers list matching what is on the map, and the keys that select and
 * delete - never while the viewer is typing.
 *
 * Runs against the real dialog partial, so an id renamed in the template and not here fails.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { ShapeSpec } from "./markup-engine";
import { createMarkupComposer, type MarkupComposer } from "./markup-composer";

declare const L: typeof import("leaflet");

const DASHBOARD = join(import.meta.dir, "..", "..", "..");
const PARTIAL = readFileSync(join(DASHBOARD, "templates", "dashboard", "partials", "map", "_markup_composer_dialog.html"), "utf8");
/** The partial as the browser gets it, less what the template tags would have drawn. */
const DIALOG_HTML = PARTIAL.replace(/\{#[\s\S]*?#\}/g, "").replace(/\{%[\s\S]*?%\}/g, "");

const ARROW: ShapeSpec = { type: "arrow", latlngs: [[42.65, -73.75], [42.652, -73.748]], color: "#e74c3c", stroke_width: 3 };
const LINE: ShapeSpec = { type: "line", latlngs: [[42.651, -73.751], [42.652, -73.752]], color: "#3498db", stroke_width: 5 };
const TEXT: ShapeSpec = { type: "text", latlngs: [[42.6505, -73.7505]], color: "#000000", stroke_width: 16, label: "Gate" };

const realL = (globalThis as Record<string, unknown>).L;
let dialog: HTMLDialogElement;
let map: L.Map;
let composer: MarkupComposer;

function button(id: string): HTMLButtonElement {
    return document.getElementById(id) as HTMLButtonElement;
}

function states(): { undo: boolean; redo: boolean; clear: boolean } {
    return { undo: !button("cmc-undo").disabled, redo: !button("cmc-redo").disabled, clear: !button("cmc-clear").disabled };
}

function rows(): string[] {
    return [...document.querySelectorAll("#cmc-layers-list .cmc-layer-label")].map((node) => node.textContent ?? "");
}

function rowAction(label: string, action: string): HTMLButtonElement {
    const row = [...document.querySelectorAll<HTMLElement>("#cmc-layers-list .cmc-layer-row")].find((r) => r.querySelector(".cmc-layer-label")?.textContent === label);
    if (!row) throw new Error(`no layer row "${label}"`);
    return row.querySelector<HTMLButtonElement>(`button[data-layer-action="${action}"]`)!;
}

function press(target: EventTarget, key: string, init: KeyboardEventInit = {}): KeyboardEvent {
    const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
    target.dispatchEvent(event);
    return event;
}

beforeAll(async () => {
    (globalThis as Record<string, unknown>).L = (await import("leaflet")).default;
});

afterAll(() => {
    (globalThis as Record<string, unknown>).L = realL;
});

beforeEach(() => {
    document.body.innerHTML = DIALOG_HTML;
    dialog = document.getElementById("comment-map-composer") as HTMLDialogElement;
    dialog.setAttribute("open", "");
    const mapEl = document.getElementById("comment-map-composer-map")!;
    mapEl.style.width = "600px";
    mapEl.style.height = "400px";
    map = L.map(mapEl).setView([42.651, -73.75], 16);
    composer = createMarkupComposer(map, dialog);
});

afterEach(() => {
    composer.destroy();
    map.remove();
    document.body.innerHTML = "";
});

describe("the dialog template has every control the scripts look for", () => {
    const sources = [
        readFileSync(join(import.meta.dir, "markup-composer.ts"), "utf8"),
        // comment-map.js owns the dialog itself: its title, tabs, footer and download.
        readFileSync(join(DASHBOARD, "frontend", "static", "js", "comment-map.js"), "utf8"),
    ];
    const ids = new Set<string>();
    for (const source of sources) {
        for (const match of source.matchAll(/(?:byId<\w+>\(root, |getElementById\()["']((?:cmc|comment-map-composer)[\w-]*)["']\)/g)) ids.add(match[1]!);
    }

    test("found the ids to check", () => {
        expect(ids.size).toBeGreaterThan(25);
    });

    for (const id of [...ids].sort()) {
        test(`#${id}`, () => {
            expect(PARTIAL.includes(`id="${id}"`) || PARTIAL.includes(`panel_id="${id}"`)).toBe(true);
        });
    }
});

describe("Undo, Redo and Clear are enabled only when they would do something", () => {
    test("all three start disabled on an empty drawing", () => {
        expect(states()).toEqual({ undo: false, redo: false, clear: false });
    });

    test("they follow each edit as it happens", () => {
        composer.document.add(ARROW);
        expect(states()).toEqual({ undo: true, redo: false, clear: true });
        composer.document.undo();
        expect(states()).toEqual({ undo: false, redo: true, clear: false });
        composer.document.redo();
        expect(states()).toEqual({ undo: true, redo: false, clear: true });
    });

    test("a saved map loaded for editing can be cleared but not undone", () => {
        composer.load([ARROW, LINE]);
        expect(states()).toEqual({ undo: false, redo: false, clear: true });
    });

    test("the buttons do what they say", () => {
        composer.load([ARROW, LINE]);
        button("cmc-clear").click();
        expect(composer.document.hasMarkup()).toBe(false);
        expect(states()).toEqual({ undo: true, redo: false, clear: false });
        button("cmc-undo").click();
        expect(composer.shapes()).toEqual([ARROW, LINE]);
        button("cmc-redo").click();
        expect(composer.shapes()).toEqual([]);
    });

    test("deleting the last shape disables Clear; undoing the delete enables it again", () => {
        const id = composer.document.add(ARROW)!;
        composer.document.remove(id);
        expect(states().clear).toBe(false);
        composer.document.undo();
        expect(states().clear).toBe(true);
    });
});

describe("the Layers list matches the drawing", () => {
    test("lists every shape, topmost first, with a count, and hides the empty note", () => {
        expect(rows()).toEqual([]);
        expect(document.getElementById("cmc-layers-empty")!.hidden).toBe(false);
        composer.load([ARROW, LINE, TEXT]);
        expect(rows()).toEqual(["Gate", "Line 1", "Arrow 1"]);
        expect(document.getElementById("cmc-layers-count")!.textContent).toBe("3");
        expect(document.getElementById("cmc-layers-empty")!.hidden).toBe(true);
    });

    test("picking a row selects the shape, and picking it again lets go", () => {
        composer.load([ARROW, LINE]);
        rowAction("Arrow 1", "select").click();
        const arrowId = composer.document.items()[0]!.id;
        expect(composer.document.selectedId()).toBe(arrowId);
        expect(document.querySelector(".cmc-layer-row.is-selected .cmc-layer-label")?.textContent).toBe("Arrow 1");
        expect(document.getElementById("cmc-style-target")!.textContent).toBe("Arrow 1");
        expect(button("cmc-delete-selected").hidden).toBe(false);
        rowAction("Arrow 1", "select").click();
        expect(composer.document.selectedId()).toBeNull();
        expect(button("cmc-delete-selected").hidden).toBe(true);
    });

    test("selecting on the map shows in the list", () => {
        composer.load([ARROW, LINE]);
        composer.document.select(composer.document.items()[1]!.id);
        expect(document.querySelector(".cmc-layer-row.is-selected .cmc-layer-label")?.textContent).toBe("Line 1");
    });

    test("hiding a shape keeps it listed, leaves it out of what is saved, and says so", () => {
        composer.load([ARROW, LINE]);
        rowAction("Arrow 1", "visibility").click();
        expect(rows()).toEqual(["Line 1", "Arrow 1"]);
        expect(composer.shapes()).toEqual([LINE]);
        expect(document.getElementById("cmc-layers-hidden-note")!.hidden).toBe(false);
        expect(rowAction("Arrow 1", "select").disabled).toBe(true);
        rowAction("Arrow 1", "visibility").click();
        expect(composer.shapes()).toEqual([ARROW, LINE]);
        expect(document.getElementById("cmc-layers-hidden-note")!.hidden).toBe(true);
    });

    test("moving a row up or down reorders the drawing, and the ends cannot move past", () => {
        composer.load([ARROW, LINE]);
        expect(rowAction("Line 1", "up").disabled).toBe(true);
        expect(rowAction("Arrow 1", "down").disabled).toBe(true);
        rowAction("Arrow 1", "up").click();
        expect(rows()).toEqual(["Arrow 1", "Line 1"]);
        expect(composer.shapes()).toEqual([LINE, ARROW]);
    });

    test("deleting from the list removes the shape, and Undo brings it back", () => {
        composer.load([ARROW, LINE]);
        rowAction("Line 1", "delete").click();
        expect(rows()).toEqual(["Arrow 1"]);
        expect(states().undo).toBe(true);
        button("cmc-undo").click();
        expect(rows()).toEqual(["Line 1", "Arrow 1"]);
    });

    test("editing a label's words renames its row", () => {
        composer.load([TEXT]);
        const id = composer.document.items()[0]!.id;
        composer.document.select(id);
        const field = document.getElementById("cmc-text-label") as HTMLInputElement;
        expect(field.hidden).toBe(false);
        expect(field.value).toBe("Gate");
        field.value = "North gate";
        field.dispatchEvent(new Event("input", { bubbles: true }));
        expect(rows()).toEqual(["North gate"]);
    });
});

describe("keys", () => {
    test("Delete and Backspace remove the selected shape", () => {
        composer.load([ARROW, LINE]);
        const [arrow, line] = composer.document.items();
        composer.document.select(arrow!.id);
        expect(press(document.body, "Delete").defaultPrevented).toBe(true);
        expect(composer.document.get(arrow!.id)).toBeUndefined();
        composer.document.select(line!.id);
        press(document.body, "Backspace");
        expect(composer.document.hasMarkup()).toBe(false);
    });

    test("Delete with nothing selected does nothing, and leaves the key alone", () => {
        composer.load([ARROW]);
        expect(press(document.body, "Delete").defaultPrevented).toBe(false);
        expect(composer.document.hasMarkup()).toBe(true);
    });

    test("Backspace or Delete while typing edits the text, not the drawing", () => {
        composer.load([ARROW, TEXT]);
        const [arrow, text] = composer.document.items();
        composer.document.select(arrow!.id);
        const title = document.getElementById("cmc-title-input") as HTMLInputElement;
        expect(press(title, "Backspace").defaultPrevented).toBe(false);
        expect(press(title, "Delete").defaultPrevented).toBe(false);
        expect(composer.document.get(arrow!.id)).toBeDefined();
        // The selected label's own field included.
        composer.document.select(text!.id);
        const field = document.getElementById("cmc-text-label") as HTMLInputElement;
        expect(press(field, "Backspace").defaultPrevented).toBe(false);
        expect(composer.document.get(text!.id)).toBeDefined();
    });

    test("a style slider having focus does not stop Delete", () => {
        composer.load([ARROW]);
        const id = composer.document.items()[0]!.id;
        composer.document.select(id);
        press(document.getElementById("cmc-width")!, "Delete");
        expect(composer.document.get(id)).toBeUndefined();
    });

    test("Escape lets go of the selection", () => {
        composer.load([ARROW]);
        composer.document.select(composer.document.items()[0]!.id);
        expect(press(document.body, "Escape").defaultPrevented).toBe(true);
        expect(composer.document.selectedId()).toBeNull();
    });

    test("Escape that let go of something does not also close the dialog", () => {
        composer.load([ARROW]);
        composer.document.select(composer.document.items()[0]!.id);
        press(document.body, "Escape");
        const cancel = new Event("cancel", { cancelable: true });
        dialog.dispatchEvent(cancel);
        expect(cancel.defaultPrevented).toBe(true);
    });

    test("Escape with nothing selected is left for the dialog to close on", () => {
        const event = press(document.body, "Escape");
        expect(event.defaultPrevented).toBe(false);
    });

    test("Ctrl+Z undoes and Ctrl+Shift+Z redoes, but not while typing", () => {
        composer.document.add(ARROW);
        press(document.body, "z", { ctrlKey: true });
        expect(composer.document.hasMarkup()).toBe(false);
        press(document.body, "Z", { ctrlKey: true, shiftKey: true });
        expect(composer.document.hasMarkup()).toBe(true);
        press(document.getElementById("cmc-title-input")!, "z", { ctrlKey: true });
        expect(composer.document.hasMarkup()).toBe(true);
    });

    test("keys pressed outside the dialog are not the composer's", () => {
        composer.load([ARROW]);
        composer.document.select(composer.document.items()[0]!.id);
        const elsewhere = document.createElement("div");
        document.body.appendChild(elsewhere);
        press(elsewhere, "Delete");
        expect(composer.document.hasMarkup()).toBe(true);
    });

    test("keys do nothing once the dialog is closed", () => {
        composer.load([ARROW]);
        composer.document.select(composer.document.items()[0]!.id);
        dialog.removeAttribute("open");
        press(document.body, "Delete");
        expect(composer.document.hasMarkup()).toBe(true);
    });

    test("the Delete button removes the selection - the way to delete without a keyboard", () => {
        composer.load([ARROW]);
        composer.document.select(composer.document.items()[0]!.id);
        button("cmc-delete-selected").click();
        expect(composer.document.hasMarkup()).toBe(false);
    });
});

describe("closing", () => {
    test("deactivate lets go of the selection and closes an open gesture as one undo step", () => {
        composer.load([ARROW]);
        const id = composer.document.items()[0]!.id;
        composer.document.select(id);
        composer.document.begin();
        composer.document.update(id, (shape) => void (shape.color = "#00ff00"));
        composer.deactivate();
        expect(composer.document.selectedId()).toBeNull();
        expect(states().undo).toBe(true);
        composer.document.undo();
        expect(composer.shapes()).toEqual([ARROW]);
    });

    test("a map that cannot turn reports north up", () => {
        expect(composer.canRotate()).toBe(false);
        expect(composer.bearing()).toBe(0);
        composer.setBearing(90);
        expect(composer.bearing()).toBe(0);
    });
});
