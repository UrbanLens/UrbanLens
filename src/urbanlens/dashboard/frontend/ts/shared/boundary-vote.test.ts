import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

import { type BoundaryVoteDeps, installBoundaryVote } from "./boundary-vote";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
const DISMISS_KEY = "ul_boundary_vote_dismissed_old-mill";
let respond: () => Response | Promise<Response>;
let requests: { url: string; body: string; reported: unknown }[];
let drawn: string[];
let pending: { fn: () => void; ms: number }[];
let toasts: string[];

const flush = () => {
    const due = pending;
    pending = [];
    for (const { fn } of due) fn();
};
const settle = async () => {
    for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

const deps = (): BoundaryVoteDeps => ({
    drawMiniMap: (el, polygon) => drawn.push(`${el.id}:${JSON.stringify(polygon)}`),
    later: (fn, ms) => pending.push({ fn, ms }),
});

function render(attrs = ""): HTMLDialogElement {
    document.body.innerHTML = `
      <dialog id="boundary-vote-dialog" data-vote-url="/vote/" data-dismiss-key="${DISMISS_KEY}" ${attrs}>
        <div class="boundary-vote-option is-selected" data-boundary-id="7">
          <div class="boundary-vote-map" id="boundary-vote-map-7"></div>
          <button class="boundary-vote-choose-btn" data-boundary-id="7"><span class="boundary-vote-choose-text">Your choice</span></button>
        </div>
        <div class="boundary-vote-option" data-boundary-id="9">
          <div class="boundary-vote-map" id="boundary-vote-map-9"></div>
          <button class="boundary-vote-choose-btn" data-boundary-id="9"><span class="boundary-vote-choose-text">This one</span></button>
        </div>
        <button id="boundary-vote-not-now">Not now</button>
      </dialog>
      <script type="application/json" id="boundary-vote-options-data">[{"id": 7, "polygon": {"type": "Polygon"}}, {"id": 9, "polygon": null}]</script>`;
    const dialog = document.querySelector("dialog");
    if (!(dialog instanceof HTMLDialogElement)) throw new Error("no dialog");
    return dialog;
}

const choices = () => Array.from(document.querySelectorAll<HTMLElement>(".boundary-vote-option")).map((o) => `${o.dataset.boundaryId}:${o.classList.contains("is-selected")}:${o.querySelector(".boundary-vote-choose-text")?.textContent}`);

const realShowModal = HTMLDialogElement.prototype.showModal;

afterAll(() => {
    HTMLDialogElement.prototype.showModal = realShowModal;
});

beforeAll(() => {
    HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
    };
});

beforeEach(() => {
    requests = [];
    drawn = [];
    pending = [];
    toasts = [];
    localStorage.clear();
    respond = () => new Response(JSON.stringify({ ok: true, my_vote_id: 9, has_consensus: false }), { status: 200 });
    globalThis.fetch = Object.assign(
        async (input: RequestInfo | URL, init?: RequestInit) => {
            requests.push({ url: String(input), body: String(init?.body ?? ""), reported: init ? Reflect.get(init, "__ulReported") : undefined });
            return respond();
        },
        { preconnect: realFetch.preconnect },
    );
    window.toastr = Object.assign(Object.create(null), {
        success: (m: string) => toasts.push(`success:${m}`),
        error: (m: string) => toasts.push(`error:${m}`),
        info: (m: string) => toasts.push(`info:${m}`),
        warning: (m: string) => toasts.push(`warning:${m}`),
        clear: () => undefined,
    });
});

afterEach(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
});

test("opening draws each outlined option's map once, after the dialog is laid out", () => {
    const dialog = render();
    const vote = installBoundaryVote(dialog, deps());
    vote.open();
    expect(dialog.open).toBe(true);
    expect(drawn).toEqual([]);
    flush();
    expect(drawn).toEqual(['boundary-vote-map-7:{"type":"Polygon"}']);
    dialog.close();
    vote.open();
    flush();
    expect(drawn).toHaveLength(1);
});

test("a vote is posted, marked, thanked, remembered, and closes the dialog", async () => {
    const dialog = render();
    installBoundaryVote(dialog, deps()).open();
    flush();
    document.querySelector<HTMLElement>(".boundary-vote-choose-btn[data-boundary-id='9']")?.click();
    await settle();
    expect(requests).toEqual([{ url: "/vote/", body: "boundary_id=9", reported: true }]);
    expect(choices()).toEqual(["7:false:This one", "9:true:Your choice"]);
    expect(toasts).toEqual(["success:Thanks - your boundary vote was counted."]);
    expect(localStorage.getItem(DISMISS_KEY)).toBe("1");
    expect(dialog.open).toBe(true);
    flush();
    expect(dialog.open).toBe(false);
});

test("a refused vote shows the server's reason once and changes nothing", async () => {
    respond = () => new Response(JSON.stringify({ error: "That boundary isn't a valid option for this place." }), { status: 400 });
    const dialog = render();
    installBoundaryVote(dialog, deps()).open();
    document.querySelector<HTMLElement>(".boundary-vote-choose-btn[data-boundary-id='9']")?.click();
    await settle();
    expect(toasts).toEqual(["error:That boundary isn't a valid option for this place."]);
    expect(choices()).toEqual(["7:true:Your choice", "9:false:This one"]);
});

test("a failure without a reason is not called a network error", async () => {
    respond = () => new Response("<h1>Server Error</h1>", { status: 500 });
    installBoundaryVote(render(), deps()).open();
    document.querySelector<HTMLElement>(".boundary-vote-choose-btn[data-boundary-id='9']")?.click();
    await settle();
    expect(toasts).toEqual(["error:Failed to save your vote."]);

    toasts = [];
    respond = () => Promise.reject(new TypeError("Failed to fetch"));
    document.querySelector<HTMLElement>(".boundary-vote-choose-btn[data-boundary-id='9']")?.click();
    await settle();
    expect(toasts).toEqual(["error:Network error saving your vote."]);
});

test("Not now, or closing any other way, stops it opening by itself here", () => {
    const dialog = render();
    installBoundaryVote(dialog, deps()).open();
    document.getElementById("boundary-vote-not-now")?.click();
    expect(dialog.open).toBe(false);
    expect(localStorage.getItem(DISMISS_KEY)).toBe("1");

    localStorage.clear();
    const again = render();
    installBoundaryVote(again, deps()).open();
    again.close();
    expect(localStorage.getItem(DISMISS_KEY)).toBe("1");
});

test("it opens by itself only when asked to and not dismissed", () => {
    installBoundaryVote(render(), deps());
    expect(pending).toEqual([]);

    const asked = render("data-auto-open");
    installBoundaryVote(asked, deps());
    expect(pending.map((p) => p.ms)).toEqual([800]);
    flush();
    expect(asked.open).toBe(true);

    localStorage.setItem(DISMISS_KEY, "1");
    pending = [];
    installBoundaryVote(render("data-auto-open"), deps());
    expect(pending).toEqual([]);
});
