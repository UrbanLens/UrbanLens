import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installWikiEditForm, installWikiRename, rememberRecentWiki, type RecentWiki } from "./wiki-page";

const realFetch = globalThis.fetch;
const realHtmx = window.htmx;
const realToastr = window.toastr;
let sent: Record<string, unknown>[] = [];
let respond: () => Response = () => new Response("{}", { status: 200 });
let triggered: string[] = [];
let toasts: { success: string[]; error: string[] };

beforeAll(() => {
    globalThis.fetch = Object.assign(async (_input: RequestInfo | URL, init?: RequestInit) => {
        sent.push(JSON.parse(String(init?.body)));
        return respond();
    }, realFetch);
    window.htmx = { process: () => undefined, trigger: (el, name) => void triggered.push(`${el.id}:${name}`), ajax: async () => undefined };
    installWikiRename();
});

afterAll(() => {
    window.toastr = realToastr;
    globalThis.fetch = realFetch;
    window.htmx = realHtmx;
});

beforeEach(() => {
    sent = [];
    triggered = [];
    toasts = { success: [], error: [] };
    window.toastr = {
        success: (m) => void toasts.success.push(m),
        error: (m) => void toasts.error.push(m),
        warning: () => undefined,
        info: () => undefined,
        clear: () => undefined,
    };
    document.body.innerHTML = `
      <h1 class="wiki-title">Old Mill</h1>
      <div class="wiki-body"><div id="wiki-about-card">old about</div></div>
      <div id="wiki-tab-content"></div>
      <dialog id="wiki-edit-dialog" open>
        <form id="wiki-edit-form">
          <input type="hidden" name="csrfmiddlewaretoken" value="tok">
          <input type="hidden" name="base_revision_id" value="7">
          <input id="wiki-name" name="name" value="New Mill">
        </form>
      </dialog>`;
    installWikiEditForm(document.getElementById("wiki-edit-form"), "/wiki/x/edit/");
});

function submit(): Promise<void> {
    document.getElementById("wiki-edit-form")?.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe("suggest-edits form", () => {
    test("a saved edit updates the page in place", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, revision: 8, about_html: '<div id="wiki-about-card">new about</div>' }), { status: 200 });
        await submit();

        expect(sent[0]).toEqual({ base_revision_id: "7", name: "New Mill" });
        expect(document.querySelector<HTMLInputElement>('[name="base_revision_id"]')?.value).toBe("8");
        expect(toasts.success).toEqual(["Changes saved."]);
        expect(document.querySelector<HTMLDialogElement>("#wiki-edit-dialog")?.open).toBe(false);
        expect(document.getElementById("wiki-about-card")?.textContent).toBe("new about");
        expect(document.querySelector(".wiki-title")?.textContent).toBe("New Mill");
        expect(triggered).toContain("wiki-tab-content:load");
    });

    test("a first description adds the About card", async () => {
        document.getElementById("wiki-about-card")?.remove();
        respond = () => new Response(JSON.stringify({ ok: true, revision: 8, about_html: '<div id="wiki-about-card">first</div>' }), { status: 200 });
        await submit();
        expect(document.querySelector(".wiki-body")?.firstElementChild?.id).toBe("wiki-about-card");
    });

    test("nothing changed says so", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, message: "No changes detected.", revision: 7 }), { status: 200 });
        await submit();
        expect(toasts.success).toEqual(["No changes detected."]);
    });

    test("a conflict keeps the dialog open and gives the server's reason", async () => {
        respond = () => new Response(JSON.stringify({ error: "Someone else changed this page since you opened it." }), { status: 409 });
        await submit();
        expect(toasts.error).toEqual(["Someone else changed this page since you opened it."]);
        expect(document.querySelector<HTMLDialogElement>("#wiki-edit-dialog")?.open).toBe(true);
        expect(document.getElementById("wiki-about-card")?.textContent).toBe("old about");
    });
});

describe("rememberRecentWiki", () => {
    test("puts this wiki first, once, and keeps ten", () => {
        const key = "ul_recent_wikis_v1_test";
        const older = Array.from({ length: 12 }, (_, i) => ({ slug: `s${i}`, title: `T${i}`, subtitle: "", url: `/w/${i}/` }));
        localStorage.setItem(key, JSON.stringify([...older.slice(0, 3), { slug: "mill", title: "stale", subtitle: "", url: "/w/mill/" }, ...older.slice(3)]));
        rememberRecentWiki(key, { slug: "mill", title: "Old Mill", subtitle: "Main St", url: "/w/mill/" });
        const list: RecentWiki[] = JSON.parse(localStorage.getItem(key) ?? "[]");
        expect(list).toHaveLength(10);
        expect(list[0]).toEqual({ slug: "mill", title: "Old Mill", subtitle: "Main St", url: "/w/mill/" });
        expect(list.filter((w) => w.slug === "mill")).toHaveLength(1);
    });

    test("a corrupt entry starts the list over", () => {
        localStorage.setItem("k", "{not json");
        rememberRecentWiki("k", { slug: "a", title: "A", subtitle: "", url: "/a/" });
        expect(JSON.parse(localStorage.getItem("k") ?? "[]")).toHaveLength(1);
    });
});
