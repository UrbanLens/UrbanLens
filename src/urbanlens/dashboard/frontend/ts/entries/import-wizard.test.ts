import { afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
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

describe("an import that fails", () => {
    const realFetch = globalThis.fetch;
    const realToastr = Object.getOwnPropertyDescriptor(globalThis, "toastr");
    const PREVIEW = { lists: [{ stem: "pins", pins: [{ name: "Mill", lat: 42.5, lng: -71.2 }] }], total: 1 };
    let errors: string[];
    let confirm: () => Promise<Response>;

    beforeEach(() => {
        errors = [];
        const note = (): undefined => undefined;
        Object.defineProperty(globalThis, "toastr", { value: { success: note, info: note, warning: note, error: (m: string) => void errors.push(m), clear: note }, configurable: true, writable: true });
        globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
            const url = String(input);
            if (url === "/status/preview/") return Response.json({ status: "done", result: PREVIEW });
            if (url === "/status/import/") return Response.json({ status: "error", message: "The import worker stopped." });
            if (init?.body instanceof FormData) return Response.json({ job_id: "p1", status_url: "/status/preview/" });
            return confirm();
        }, realFetch);
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
        if (realToastr) Object.defineProperty(globalThis, "toastr", realToastr);
        else Reflect.deleteProperty(globalThis, "toastr");
    });

    const byId = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;

    /** Uploads a CSV and waits out the preview poll, leaving the wizard on its review step. */
    async function reachReview(): Promise<void> {
        mount("Import Pins");
        document.dispatchEvent(new Event("htmx:load"));
        const input = byId<HTMLInputElement>("iw-file-input");
        Object.defineProperty(input, "files", { value: [new File(["name,latitude,longitude\n"], "pins.csv", { type: "text/csv" })], configurable: true });
        input.dispatchEvent(new Event("change"));
        byId("iw-btn-upload").click();
        for (let i = 0; i < 30 && byId("iw-step-2").hidden; i++) await new Promise((resolve) => setTimeout(resolve, 100));
        expect(byId("iw-step-2").hidden).toBe(false);
    }

    test("a confirm that never reached the server goes back to the review step, ready to try again", async () => {
        await reachReview();
        confirm = async () => {
            throw new TypeError("Failed to fetch");
        };

        byId("iw-btn-confirm").click();
        for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));

        expect(byId("iw-step-2").hidden).toBe(false);
        expect(byId("iw-step-3").hidden).toBe(true);
        expect(byId("iw-title").textContent).not.toBe("Importing...");
        expect(byId<HTMLButtonElement>("iw-btn-confirm").disabled).toBe(false);
        expect(byId<HTMLButtonElement>("closeImportDialog").disabled).toBe(false);
        expect(errors).toHaveLength(1);
    });

    test("an import the server stopped part-way says it stopped, and can be closed", async () => {
        await reachReview();
        confirm = async () => Response.json({ job_id: "i1", status_url: "/status/import/", total: 1 });

        byId("iw-btn-confirm").click();
        for (let i = 0; i < 30 && byId("iw-btn-done").hidden; i++) await new Promise((resolve) => setTimeout(resolve, 100));

        expect(byId("iw-progress-header").textContent).toBe("Import stopped");
        expect(byId("iw-title").textContent).toBe("Import stopped");
        expect(byId("iw-btn-done").hidden).toBe(false);
        expect(byId<HTMLButtonElement>("closeImportDialog").disabled).toBe(false);
        expect(errors).toEqual(["The import worker stopped."]);
    });
});
