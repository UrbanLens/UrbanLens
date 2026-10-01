import { beforeAll, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const TEMPLATE = readFileSync(join(import.meta.dir, "../../../templates/dashboard/pages/location/import/csv.html"), "utf8");

/** The wizard's markup as the server renders it for the Memories variant, with the template tags resolved by hand. */
function wizardMarkup(title: string): string {
    const start = TEMPLATE.indexOf('<div class="dialog-overlay" id="import-screen">');
    const end = TEMPLATE.indexOf("<script");
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    return TEMPLATE.slice(start, end)
        .replace(/\{%[^%]*%\}/g, "")
        .replace(/\{\{[^}]*\}\}/g, "")
        .replace('data-import-title=""', `data-import-title="${title}"`);
}

function mount(title: string): void {
    document.body.innerHTML = '<div id="importPinsModal">' + wizardMarkup(title) + "</div>";
}

describe("import wizard binding", () => {
    beforeAll(async () => {
        mount("First");
        await import("./import-wizard");
    });

    test("a wizard on the page when the bundle loads is bound", () => {
        expect(document.getElementById("import-dialog")?.dataset.wizardReady).toBe("1");
        expect(document.getElementById("iw-step-1")?.hidden).toBe(false);
        expect(document.getElementById("iw-step-2")?.hidden).toBe(true);
    });

    test("a wizard htmx swaps in later is bound too, since the module does not run again", () => {
        mount("Second");
        expect(document.getElementById("import-dialog")?.dataset.wizardReady).toBeUndefined();

        document.dispatchEvent(new Event("htmx:load"));

        expect(document.getElementById("import-dialog")?.dataset.wizardReady).toBe("1");
        expect(document.getElementById("iw-title")?.textContent).toBe("Second");
    });

    test("a chosen file is listed by name, escaped, and enables the upload", () => {
        const input = document.getElementById("iw-file-input") as HTMLInputElement;
        const file = new File(["name,latitude,longitude\n"], "<b>pins</b>.csv", { type: "text/csv" });
        Object.defineProperty(input, "files", { value: [file], configurable: true });

        input.dispatchEvent(new Event("change"));

        const chip = document.querySelector(".iw-file-chip-name");
        expect(chip?.textContent).toBe("<b>pins</b>.csv");
        expect(chip?.querySelector("b")).toBeNull();
        expect((document.getElementById("iw-btn-upload") as HTMLButtonElement).disabled).toBe(false);
    });
});
