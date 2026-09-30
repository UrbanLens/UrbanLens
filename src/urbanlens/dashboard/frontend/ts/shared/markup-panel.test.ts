import { beforeEach, expect, test } from "bun:test";

import { wireMarkupPanel, wireMarkupTools } from "./markup-panel";

let calls: string[] = [];

beforeEach(() => {
    calls = [];
    document.body.innerHTML = `
      <div id="markup-panel">
        <button type="button" data-dialog-close>x</button>
        <input type="text" id="label" data-markup-live>
        <span id="width-val">3</span><input type="range" id="width" min="1" max="8" value="3" data-markup-live data-readout="width-val">
        <input type="hidden" id="color" value="#e53e3e">
        <select id="security" data-markup-live><option value="">None</option><option value="fence">Fence</option></select>
        <button type="button" data-markup-close>Close</button>
        <button type="button" data-markup-delete>Delete</button>
      </div>`;
    const panel = document.getElementById("markup-panel");
    if (!panel) throw new Error("fixture");
    wireMarkupPanel(panel, {
        liveApply: () => void calls.push("apply"),
        closePanel: () => void calls.push("close-panel"),
        closeOrFinish: () => void calls.push("close-or-finish"),
        deleteEdit: async () => void calls.push("delete"),
    });
});

function fire(id: string, type: "input" | "change", value?: string): void {
    const el = document.getElementById(id);
    if (!(el instanceof HTMLInputElement || el instanceof HTMLSelectElement)) throw new Error(id);
    if (value !== undefined) el.value = value;
    el.dispatchEvent(new Event(type, { bubbles: true }));
}

test("typing a label and dragging a slider apply live; the slider shows its value", () => {
    fire("label", "input", "Gate");
    fire("width", "input", "6");
    expect(calls).toEqual(["apply", "apply"]);
    expect(document.getElementById("width-val")?.textContent).toBe("6");
});

test("a select applies once per choice", () => {
    fire("security", "input", "fence");
    fire("security", "change");
    expect(calls).toEqual(["apply"]);
});

test("controls the panel does not mark are left alone", () => {
    fire("color", "input", "#000000");
    fire("color", "change");
    expect(calls).toEqual([]);
});

test("the header's X closes the panel; Close finishes a drawing; Delete deletes", () => {
    for (const selector of ["[data-dialog-close]", "[data-markup-close]", "[data-markup-delete]"]) document.querySelector<HTMLElement>(selector)?.click();
    expect(calls).toEqual(["close-panel", "close-or-finish", "delete"]);
});

test("each toolbar button starts its own tool", () => {
    document.body.innerHTML = ["line", "arrow", "freehand", "text", "square", "circle", "polygon"]
        .map((tool) => `<button class="map-btn-icon" data-markup-tool="${tool}"><i>${tool}</i></button>`)
        .join("");
    const started: string[] = [];
    const stop = wireMarkupTools({
        startMarkupDraw: (type) => void started.push(`draw:${type}`),
        startShapeDraw: (type) => void started.push(`shape:${type}`),
        startTextPlacement: () => void started.push("text"),
    });
    for (const icon of document.querySelectorAll<HTMLElement>("i")) icon.click();
    stop();
    expect(started).toEqual(["draw:line", "draw:arrow", "draw:freehand", "text", "shape:square", "shape:circle", "shape:polygon"]);
});
