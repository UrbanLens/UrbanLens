/**
 * Every label tab organize/index.html renders must get a working OrgTabManager.
 */

import { afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { applyOrgFilter, ORG_FILTER_NAMESPACES } from "./organize-filter-engine";
import { createOrganizeHeader, installOrgBulkToolbar, orgHeader } from "./organize-header";
import type { OrgTabManagerConfig } from "./organize-tab-manager";
import { initOrganizeTabs, installOrgEditDialogOpener, organizeTabConfigs } from "./organize-tabs";

const TEMPLATES = join(import.meta.dir, "../../../templates/dashboard");
const INDEX = readFileSync(join(TEMPLATES, "pages/organize/index.html"), "utf8");
const MERGE_DIALOG = readFileSync(join(TEMPLATES, "partials/labels/organize_label_merge_dialog.html"), "utf8");
const BULK_EDIT_DIALOG = readFileSync(join(TEMPLATES, "partials/labels/organize_label_bulk_edit_dialog.html"), "utf8");

interface PanelInclude {
    ns: string;
    kind: string;
    rowsId: string;
    selectClass: string;
    editTarget: string | undefined;
}

function includeArg(include: string, name: string): string | undefined {
    return new RegExp(`\\b${name}='([^']*)'`).exec(include)?.[1];
}

function panelIncludes(): PanelInclude[] {
    return [...INDEX.matchAll(/\{% include 'dashboard\/partials\/labels\/organize_label_panel\.html' with ([^%]*)%\}/g)].map((m) => ({
        ns: includeArg(m[1]!, "ns")!,
        kind: includeArg(m[1]!, "kind")!,
        rowsId: includeArg(m[1]!, "rows_id")!,
        selectClass: includeArg(m[1]!, "select_class")!,
        editTarget: includeArg(m[1]!, "edit_target"),
    }));
}

function dialogIncludes(template: string): Array<Record<string, string>> {
    return [...INDEX.matchAll(new RegExp(`\\{% include 'dashboard/partials/labels/${template}' with ([^%]*)%\\}`, "g"))].map((m) =>
        Object.fromEntries([...m[1]!.matchAll(/(\w+)='([^']*)'/g)].map((a) => [a[1]!, a[2]!])),
    );
}

/** The element ids a dialog partial renders for one `ns`, read from its source. */
function templateIds(source: string, ns: string): Set<string> {
    const text = source.replaceAll("{{ ns }}", ns).replace(/ns\|add:"([^"]*)"/g, `"${ns}$1"`);
    return new Set([...text.matchAll(/\b(?:id|title_id|picker_id)="([^"{]+)"/g)].map((m) => m[1]!));
}

const MEDIA_ROWS = `
  <div class="organize-page" data-active-tab="media">
    <div id="media-label-rows" class="organize-label-rows" data-bulk-delete-url="/media/bulk-delete/" data-bulk-edit-url="/media/bulk-edit/" data-merge-url="/media/multi-merge/">
      ${[1, 2]
          .map(
              (id) => `
      <div class="tag-card" id="media-card-${id}" data-id="${id}" data-kind="media" data-media-id="${id}" data-media-name="Label ${id}" data-media-color="" data-media-icon="" data-media-pin-count="0" data-media-parents="">
        <label class="ul-checkbox-wrap"><input type="checkbox" class="media-sel-cb" data-media-id="${id}" data-id="${id}"></label>
      </div>`,
          )
          .join("")}
    </div>
  </div>
  <div id="org-bulk-bar"><span id="org-bulk-count"></span><button id="org-bulk-edit-btn"></button><button id="org-bulk-merge-btn"></button><button id="org-bulk-delete-btn"></button></div>`;

/** Stand-ins for every element a config's dialogs look up by id. */
function dialogMarkup(cfg: OrgTabManagerConfig): string {
    const d = cfg.bulkEditDialog;
    const m = cfg.mergeDialog;
    const inputs = [d.iconNochangeId, d.colorNochangeId, d.orderNochangeId, d.descNochangeId].map((id) => `<input type="checkbox" id="${id}" checked>`).join("");
    return `
      <dialog id="${d.dialogId}"><h2 id="${d.titleId}"></h2>${inputs}
        <input id="icon-value-${d.iconPickerId}"><div id="${d.colorPickerId}"></div><input id="${d.colorValueId}">
        <input id="${d.orderValueId}"><textarea id="${d.descValueId}"></textarea><button id="${d.confirmId}"></button>
      </dialog>
      <dialog id="${m.dialogId}"><h2 id="${m.titleId}"></h2><div id="${m.targetCardId}"></div><div id="${m.sourcesListId}"></div><button id="${m.confirmId}"></button></dialog>`;
}

function mediaConfig(): OrgTabManagerConfig {
    const cfg = organizeTabConfigs().find((c) => c.ns === "media");
    expect(cfg).toBeDefined();
    return cfg!;
}

describe("organize tab coverage", () => {
    test("the template is where we think it is", () => {
        expect(panelIncludes().map((p) => p.ns)).toContain("media");
    });

    test("every rendered label panel has a filter namespace", () => {
        const namespaces = new Set<string>(ORG_FILTER_NAMESPACES);
        for (const panel of panelIncludes()) {
            expect(namespaces).toContain(panel.ns);
        }
    });

    test("every rendered label panel gets a tab manager", () => {
        const panels = panelIncludes();
        document.body.innerHTML = panels.map((p) => `<div id="${p.rowsId}"></div>`).join("");
        const configs = organizeTabConfigs();
        for (const panel of panels) {
            const cfg = configs.find((c) => c.rowsId === panel.rowsId);
            expect<string | undefined>(cfg?.ns).toBe(panel.ns);
            expect(cfg?.checkboxSelector).toBe(`.${panel.selectClass}`);
        }
    });

    test("every rendered label panel has its merge and bulk-edit dialogs", () => {
        const merges = dialogIncludes("organize_label_merge_dialog.html").map((a) => a.ns);
        const bulkEdits = dialogIncludes("organize_label_bulk_edit_dialog.html").map((a) => a.ns);
        for (const panel of panelIncludes()) {
            expect(merges).toContain(panel.ns);
            expect(bulkEdits).toContain(panel.ns);
        }
    });

});

describe("media tab dialogs match their templates", () => {
    beforeEach(() => {
        document.body.innerHTML = MEDIA_ROWS;
    });

    test("every bulk-edit id the manager reads is rendered", () => {
        const d = mediaConfig().bulkEditDialog;
        const ids = templateIds(BULK_EDIT_DIALOG, "media");
        for (const id of [d.dialogId, d.titleId, d.confirmId, d.iconNochangeId, d.colorPickerId, d.colorValueId, d.colorNochangeId, d.orderValueId, d.orderNochangeId, d.descValueId, d.descNochangeId]) {
            expect(ids).toContain(id!);
        }
        const include = dialogIncludes("organize_label_bulk_edit_dialog.html").find((a) => a.ns === "media");
        expect(include?.picker_id).toBe(d.iconPickerId);
        expect(include?.candidates_from).toBe("media");
    });

    test("every merge id the manager reads is rendered", () => {
        const m = mediaConfig().mergeDialog;
        const ids = templateIds(MERGE_DIALOG, "media");
        for (const id of [m.dialogId, m.titleId, m.targetCardId, m.sourcesListId, m.confirmId]) {
            expect(ids).toContain(id!);
        }
    });

    test("media has no merge-time edit or kind conversion", () => {
        const cfg = mediaConfig();
        expect(cfg.supportsMergeEdit).toBe(false);
        expect(cfg.convertTargets).toEqual([]);
    });
});

describe("media tab behaviour", () => {
    const realFetch = globalThis.fetch;
    const realMatchMedia = window.matchMedia;
    let requests: Array<{ url: string; body: unknown }> = [];

    beforeAll(() => {
        installOrgEditDialogOpener();
    });

    beforeEach(() => {
        requests = [];
        globalThis.fetch = (async (url: string, init: RequestInit) => {
            requests.push({ url, body: JSON.parse(String(init.body)) });
            return new Response('<div class="tag-empty"></div>', { status: 200, headers: { "Content-Type": "text/html" } });
        }) as unknown as typeof fetch;
        // Another suite deletes the global; the header needs it for its narrow-viewport check.
        window.matchMedia = ((query: string) => ({ matches: false, media: query })) as unknown as typeof window.matchMedia;
        document.body.innerHTML = MEDIA_ROWS;
        document.body.insertAdjacentHTML("beforeend", dialogMarkup(mediaConfig()));
        installOrgBulkToolbar();
        createOrganizeHeader("media");
        initOrganizeTabs();
        orgHeader.init();
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
        window.matchMedia = realMatchMedia;
    });

    function check(id: number): void {
        document.querySelector<HTMLInputElement>(`.media-sel-cb[data-media-id="${id}"]`)!.click();
    }

    test("the header resolves the media tab to the media filter namespace", () => {
        expect(orgHeader.getFilterNs()).toBe("media");
    });

    test("checking a card selects it into the bulk toolbar", () => {
        check(1);
        expect(document.getElementById("org-bulk-bar")!.classList.contains("visible")).toBe(true);
        expect(document.getElementById("org-bulk-count")!.textContent).toBe("1 selected");
        expect(document.getElementById("media-card-1")!.classList.contains("tag-card--selected")).toBe(true);
    });

    test("bulk edit opens the media dialog and posts to the media endpoint", async () => {
        check(1);
        check(2);
        window._orgBulk.edit!();
        expect((document.getElementById("media-bulk-edit-dialog") as HTMLDialogElement).open).toBe(true);
        expect(document.getElementById("media-bulk-edit-title")!.textContent).toBe("Edit 2 Media Labels");

        const order = document.getElementById("media-bulk-order-value") as HTMLInputElement;
        order.value = "7";
        (document.getElementById("media-bulk-order-nochange") as HTMLInputElement).checked = false;
        document.getElementById("media-bulk-edit-confirm")!.click();
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(requests).toHaveLength(1);
        expect(requests[0]!.url).toBe("/media/bulk-edit/");
        expect(requests[0]!.body).toMatchObject({ ids: [1, 2], order: "7" });
    });

    test("merge opens the media merge dialog", () => {
        check(1);
        check(2);
        window._orgBulk.merge!();
        expect((document.getElementById("media-merge-dialog") as HTMLDialogElement).open).toBe(true);
        expect(document.getElementById("media-merge-dialog-title")!.textContent).toBe("Merge 2 Media Labels");
    });

    test("the filter search hides non-matching media cards", () => {
        document.body.insertAdjacentHTML("beforeend", '<input id="media-filter-search" value="label 2">');
        applyOrgFilter("media");
        expect(document.getElementById("media-card-1")!.style.display).toBe("none");
        expect(document.getElementById("media-card-2")!.style.display).toBe("");
        (document.getElementById("media-filter-search") as HTMLInputElement).value = "";
        applyOrgFilter("media");
    });

    test("a form swapped into the media edit body opens the media edit dialog", () => {
        const editTarget = panelIncludes().find((p) => p.ns === "media")!.editTarget!;
        const edit = dialogIncludes("organize_label_edit_dialog.html").find((a) => `#${a.body_id}` === editTarget);
        const dialogId = edit?.dialog_id ?? "";
        const bodyId = edit?.body_id ?? "";
        expect(dialogId).not.toBe("");
        document.body.insertAdjacentHTML("beforeend", `<dialog id="${dialogId}"><div id="${bodyId}"></div></dialog>`);

        const body = document.getElementById(bodyId)!;
        body.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true, detail: { target: body } }));

        expect((document.getElementById(dialogId) as HTMLDialogElement).open).toBe(true);
    });
});
