import { afterAll, beforeEach, describe, expect, test } from "bun:test";

import { installExportSelectAll, installImportPicker, installManualBackup, installSectionTabs, responseMessage, showExportError } from "./tools-page";

const realToastr = window.toastr;
const realFetch = globalThis.fetch;
let errors: string[] = [];

afterAll(() => {
    window.toastr = realToastr;
    globalThis.fetch = realFetch;
});

beforeEach(() => {
    errors = [];
    window.toastr = { success: () => undefined, error: (m) => void errors.push(m), warning: () => undefined, info: () => undefined, clear: () => undefined };
});

describe("section tabs", () => {
    test("show only the chosen panel", () => {
        document.body.innerHTML = `
          <button class="tools-section-tab is-active" data-section="data">Data</button><button class="tools-section-tab" data-section="find">Find</button>
          <div class="tools-section-panel" id="panel-data"></div><div class="tools-section-panel" id="panel-find" hidden></div>`;
        installSectionTabs();
        document.querySelector<HTMLElement>('[data-section="find"]')?.click();
        expect(document.getElementById("panel-data")?.hidden).toBe(true);
        expect(document.getElementById("panel-find")?.hidden).toBe(false);
        expect(document.querySelector('[data-section="find"]')?.classList.contains("is-active")).toBe(true);
    });
});

describe("export", () => {
    test("Select all follows the type boxes, and drives them", () => {
        document.body.innerHTML = `<input type="checkbox" id="export-select-all" checked>
          <div id="export-data-checkboxes"><input type="checkbox" class="export-type-cb" checked><input type="checkbox" class="export-type-cb" checked></div>`;
        installExportSelectAll();
        const all = document.getElementById("export-select-all");
        const boxes = document.querySelectorAll<HTMLInputElement>(".export-type-cb");
        if (!(all instanceof HTMLInputElement) || !boxes[0]) throw new Error("bad fixture");
        boxes[0].checked = false;
        boxes[0].dispatchEvent(new Event("change"));
        expect([all.checked, all.indeterminate]).toEqual([false, true]);
        all.checked = true;
        all.dispatchEvent(new Event("change"));
        expect(Array.from(boxes, (b) => b.checked)).toEqual([true, true]);
        expect(all.indeterminate).toBe(false);
    });

    test("an error stops the poll and shows the server's words as text", () => {
        document.body.innerHTML = `<div id="export-status-poll" hx-get="/export/1/" hx-trigger="every 2s"></div>`;
        showExportError("<b>Export job not found</b>");
        const poll = document.getElementById("export-status-poll");
        expect(poll?.hasAttribute("hx-get")).toBe(false);
        expect(poll?.querySelector(".export-error-msg span")?.textContent).toBe("<b>Export job not found</b>");
        expect(poll?.querySelector("b")).toBeNull();
        expect(poll?.querySelector("button[data-reload]")?.className).toBe("btn btn--secondary");
        expect(errors).toEqual(["<b>Export job not found</b>"]);
    });

    test("an error page's markup is read as words", () => {
        expect(responseMessage({ responseText: "<html><h1>Server Error</h1>\n<p>Try later</p></html>", status: 500 })).toBe("Server Error Try later");
    });
});

describe("import picker", () => {
    test("a chosen file shows its name and enables Import; clearing undoes both", () => {
        document.body.innerHTML = `<label id="import-drop-zone"><span class="import-drop-label">Drop</span>
          <input type="file" id="import-file-input"><span id="import-file-chosen" style="display:none"><span id="import-file-name"></span><button id="import-file-clear">x</button></span></label>
          <button id="import-submit-btn" disabled>Import</button>`;
        installImportPicker();
        const input = document.getElementById("import-file-input");
        if (!(input instanceof HTMLInputElement)) throw new Error("no input");
        Object.defineProperty(input, "files", { value: [new File(["x"], "pins.kml")], configurable: true });
        input.dispatchEvent(new Event("change"));
        expect(document.getElementById("import-file-name")?.textContent).toBe("pins.kml");
        expect(document.querySelector<HTMLButtonElement>("#import-submit-btn")?.disabled).toBe(false);
        document.getElementById("import-file-clear")?.click();
        expect(document.getElementById("import-file-chosen")?.style.display).toBe("none");
        expect(document.querySelector<HTMLButtonElement>("#import-submit-btn")?.disabled).toBe(true);
    });
});

describe("manual backup", () => {
    async function run(response: Response): Promise<string | null | undefined> {
        globalThis.fetch = Object.assign(async () => response, realFetch);
        document.body.innerHTML = `<button id="manual-backup-btn" data-url="/tools/backup/">Run</button><div id="manual-backup-result"></div>`;
        installManualBackup();
        document.getElementById("manual-backup-btn")?.click();
        expect(document.getElementById("manual-backup-result")?.textContent).toBe("Queueing backup...");
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(document.querySelector<HTMLButtonElement>("#manual-backup-btn")?.disabled).toBe(false);
        return document.getElementById("manual-backup-result")?.textContent;
    }

    test("says what the server said", async () => {
        expect(await run(new Response(JSON.stringify({ message: "Backup queued as task 42." }), { status: 202 }))).toBe("Backup queued as task 42.");
    });

    test("a refusal without words reads as a failure", async () => {
        expect(await run(new Response("<h1>oops</h1>", { status: 500 }))).toBe("Backup request failed.");
    });
});
