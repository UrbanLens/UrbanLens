import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { WikiMedia } from "./wiki-media";

const realFetch = globalThis.fetch;
const realHtmx = window.htmx;
const realToastr = window.toastr;
let posted: Record<string, unknown>[] = [];
let respond: () => Response = () => new Response("{}", { status: 200 });
let triggered: string[] = [];
let errors: string[] = [];

function item(key: string, source: string, score: number, relevant = ""): string {
    return `<div class="media-item" data-media-source="${source}" data-media-key="${key}" data-media-url="/u/${key}" data-vote-score="${score}" data-media-relevant="${relevant}">
      <img class="media-item-thumb" src="/t/${key}">
      <button class="media-item-vote-up-btn" data-media-action="vote-up"><i>up</i></button>
      <span class="media-item-votes">${score}</span>
      <button class="media-item-vote-down-btn" data-media-action="vote-down"><i>down</i></button>
    </div>`;
}

function render(items: string): void {
    document.body.innerHTML = `
    <section id="wiki-media-section" data-media-view="all">
      <span id="wiki-media-count-badge" hidden></span>
      <div id="wiki-media-tabs"></div>
      <div class="media-view-panel" data-media-view-panel="all">
        <div id="wiki-media-loading"></div><div id="wiki-media-empty" hidden></div>
        <div id="wiki-media-grid">${items}</div>
      </div>
      <div class="media-view-panel" data-media-view-panel="manage" hidden><div id="wiki-gallery-panel"></div></div>
    </section>`;
}

const keys = () => Array.from(document.querySelectorAll<HTMLElement>("#wiki-media-grid .media-item"), (el) => el.dataset.mediaKey);
const tabs = () => Array.from(document.querySelectorAll<HTMLElement>("#wiki-media-tabs .media-tab"), (el) => el.textContent);
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const media = new WikiMedia("/wiki/x/media/vote/");

beforeAll(() => {
    render("");
    media.install();
    globalThis.fetch = Object.assign(async (_input: RequestInfo | URL, init?: RequestInit) => {
        posted.push(JSON.parse(String(init?.body)));
        return respond();
    }, realFetch);
    window.htmx = { process: () => undefined, trigger: (_el, name) => void triggered.push(String(name)), ajax: async () => undefined };
});

afterAll(() => {
    window.toastr = realToastr;
    globalThis.fetch = realFetch;
    window.htmx = realHtmx;
});

beforeEach(() => {
    posted = [];
    triggered = [];
    errors = [];
    respond = () => new Response("{}", { status: 200 });
    window.toastr = { success: () => undefined, error: (m) => void errors.push(m), warning: () => undefined, info: () => undefined, clear: () => undefined };
    render(item("a", "flickr", 1) + item("b", "wikimedia", 5) + item("c", "flickr", 3));
    media.refresh();
});

describe("tabs", () => {
    test("rank by score and count each source", () => {
        expect(keys()).toEqual(["b", "c", "a"]);
        expect(tabs()).toEqual(["All3", "Manage", "flickr2", "Wikimedia1"]);
        expect(document.getElementById("wiki-media-count-badge")?.textContent).toBe("3");
        expect(document.getElementById("wiki-media-loading")?.hidden).toBe(true);
    });

    test("a source tab filters the grid", () => {
        document.querySelector<HTMLElement>('#wiki-media-tabs [data-tab="flickr"]')?.click();
        const excluded = Array.from(document.querySelectorAll<HTMLElement>(".media-item.media-tab-excluded"), (el) => el.dataset.mediaKey);
        expect(excluded).toEqual(["b"]);
    });

    test("Manage shows the gallery and loads it once", () => {
        document.querySelector<HTMLElement>('#wiki-media-tabs [data-tab="manage"]')?.click();
        document.querySelector<HTMLElement>('#wiki-media-tabs [data-tab="manage"]')?.click();
        expect(document.getElementById("wiki-media-section")?.dataset.mediaView).toBe("manage");
        expect(document.querySelector<HTMLElement>('[data-media-view-panel="manage"]')?.hidden).toBe(false);
        expect(triggered).toEqual(["ul:load-manage"]);
    });

    test("a source name is text, not markup", () => {
        render(item("x", "<img src=x onerror=alert(1)>", 1));
        media.refresh();
        expect(document.querySelector("#wiki-media-tabs img")).toBeNull();
    });
});

describe("votes", () => {
    test("an upvote is sent, and the new score re-ranks the grid", async () => {
        respond = () => new Response(JSON.stringify({ vote_score: 9 }), { status: 200 });
        document.querySelector<HTMLElement>('.media-item[data-media-key="a"] [data-media-action="vote-up"] i')?.click();
        const a = document.querySelector<HTMLElement>('.media-item[data-media-key="a"]');
        expect(a?.dataset.mediaRelevant).toBe("true");
        await settle();
        expect(posted[0]).toMatchObject({ source: "flickr", item_key: "a", is_relevant: true });
        expect(a?.querySelector(".media-item-votes")?.textContent).toBe("9");
        expect(keys()).toEqual(["a", "b", "c"]);
    });

    test("clicking the active thumb clears the vote", async () => {
        render(item("a", "flickr", 1, "false"));
        media.refresh();
        document.querySelector<HTMLElement>('[data-media-action="vote-down"]')?.click();
        await settle();
        expect(posted[0]?.is_relevant).toBeNull();
    });

    test("a refused vote puts the thumb back and says so", async () => {
        respond = () => new Response("", { status: 500 });
        document.querySelector<HTMLElement>('.media-item[data-media-key="a"] [data-media-action="vote-up"]')?.click();
        await settle();
        const a = document.querySelector<HTMLElement>('.media-item[data-media-key="a"]');
        expect(a?.dataset.mediaRelevant).toBe("");
        expect(a?.querySelector('[data-media-action="vote-up"]')?.classList.contains("is-active")).toBe(false);
        expect(errors).toEqual(["Failed to save your vote."]);
    });
});
