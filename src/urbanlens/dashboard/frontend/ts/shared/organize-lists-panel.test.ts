import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installOrgBulkToolbar } from "./organize-header";
import { installOrganizeListsPanel } from "./organize-lists-panel";

const realHtmx = window.htmx;
let requests: unknown[][] = [];

beforeAll(() => {
    installOrganizeListsPanel();
    installOrganizeListsPanel();
});

afterAll(() => {
    window.htmx = realHtmx;
});

beforeEach(() => {
    requests = [];
    window.htmx = {
        process: () => {},
        trigger: () => {},
        ajax: async (...args: unknown[]) => void requests.push(args),
    };
});

describe("Organize's Lists panel", () => {
    beforeEach(() => {
        document.body.innerHTML = `
          <div id="panel-lists">
            <input type="search" id="pin-lists-search">
            <select id="pin-lists-sort-select" data-panel-url="/dashboard/lists/?tab=lists">
              <option value="updated" selected>Recently updated</option><option value="name">Name</option>
            </select>
            <div class="pin-lists-grid" id="pin-lists-grid">
              <a class="pin-list-card" id="ohio" data-search="ohio road trip"></a>
              <a class="pin-list-card" id="mills" data-search="old mills"></a>
            </div>
          </div>`;
    });

    const shown = () => Array.from(document.querySelectorAll<HTMLElement>(".pin-list-card")).filter((c) => c.style.display !== "none").map((c) => c.id);

    test("the search box narrows the cards, ignoring case and edge spaces, and clearing it shows them all", () => {
        const search = document.getElementById("pin-lists-search") as HTMLInputElement;
        search.value = "  MILL ";
        search.dispatchEvent(new Event("input", { bubbles: true }));
        expect(shown()).toEqual(["mills"]);
        search.value = "";
        search.dispatchEvent(new Event("input", { bubbles: true }));
        expect(shown()).toEqual(["ohio", "mills"]);
    });

    test("choosing an order fetches the panel again in that order", () => {
        const sort = document.getElementById("pin-lists-sort-select") as HTMLSelectElement;
        sort.value = "name";
        sort.dispatchEvent(new Event("change", { bubbles: true }));
        expect(requests).toEqual([["GET", "/dashboard/lists/?tab=lists&sort=name", { target: "#panel-lists", swap: "innerHTML" }]]);
    });
});

describe("Organize's bulk bar", () => {
    test("each button runs the action the current tab registered, and none is a no-op", () => {
        document.body.innerHTML = `
          <div id="org-bulk-bar">
            <button type="button" id="deselect" data-org-bulk="deselect"><i id="deselect-icon">close</i></button>
            <button type="button" id="edit" data-org-bulk="edit">Edit</button>
            <button type="button" id="del" data-org-bulk="del">Delete</button>
          </div>`;
        installOrgBulkToolbar();
        const ran: string[] = [];
        window._orgBulk = { deselect: () => void ran.push("deselect"), edit: () => void ran.push("edit"), merge: null, del: null };

        document.getElementById("deselect-icon")!.click();
        document.getElementById("edit")!.click();
        document.getElementById("del")!.click();

        expect(ran).toEqual(["deselect", "edit"]);
    });
});
