import { afterEach, beforeEach, expect, test } from "bun:test";

import { installHomeOverview, renderRecentStrips } from "./home-overview";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
const realOpen = window.galleryOpenLightboxItem;
let toasts: string[];
let posts: { url: string; body: string; reported: unknown }[];
let respond: () => Response | Promise<Response>;
let navigations: number;
let uninstall: (() => void) | null = null;

const settle = async () => {
    for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

beforeEach(() => {
    localStorage.clear();
    toasts = [];
    posts = [];
    navigations = 0;
    respond = () => new Response(JSON.stringify({ enabled_keys: ["stats"] }), { status: 200 });
    globalThis.fetch = Object.assign(
        async (input: RequestInfo | URL, init?: RequestInit) => {
            posts.push({ url: String(input), body: String(init?.body ?? ""), reported: init ? Reflect.get(init, "__ulReported") : undefined });
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
    uninstall?.();
    uninstall = null;
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
    window.galleryOpenLightboxItem = realOpen;
});

const STRIP = `
  <section id="home-recent-pins-strip" data-recent-key="ul_recent_pins_v1_abc" data-recent-icon="push_pin" hidden>
    <div data-recent-list></div>
  </section>`;

test("a recently-viewed strip lists what its key holds, as text, and appears", () => {
    document.body.innerHTML = STRIP;
    const entries = [
        { name: "<b>Old Mill</b>", url: "/dashboard/map/pin/old-mill/" },
        { title: "Asylum", subtitle: "Albany, NY", url: "/dashboard/location/x/wiki/" },
        ...Array.from({ length: 6 }, (_, i) => ({ name: `Extra ${i}`, url: `/p/${i}/` })),
    ];
    localStorage.setItem("ul_recent_pins_v1_abc", JSON.stringify(entries));
    renderRecentStrips(document);
    const cards = Array.from(document.querySelectorAll<HTMLAnchorElement>(".home-mini-card"));
    expect(cards).toHaveLength(6);
    expect(cards.map((c) => [c.querySelector("i")?.textContent, c.querySelector("strong")?.textContent, c.querySelector("span")?.textContent, c.getAttribute("href")]).slice(0, 2)).toEqual([
        ["push_pin", "<b>Old Mill</b>", "Recently viewed", "/dashboard/map/pin/old-mill/"],
        ["push_pin", "Asylum", "Albany, NY", "/dashboard/location/x/wiki/"],
    ]);
    expect(document.querySelector(".home-mini-card b")).toBeNull();
    expect(document.getElementById("home-recent-pins-strip")?.hidden).toBe(false);
});

test("an entry whose link leaves this site is dropped", () => {
    document.body.innerHTML = STRIP;
    localStorage.setItem(
        "ul_recent_pins_v1_abc",
        JSON.stringify([
            { name: "script", url: "javascript:alert(1)" },
            { name: "elsewhere", url: "//evil.example/pin/" },
            { name: "absolute", url: "https://evil.example/" },
            { name: "no link", slug: "old-mill" },
            { name: "kept", url: "/dashboard/map/pin/kept/" },
        ]),
    );
    renderRecentStrips(document);
    expect(Array.from(document.querySelectorAll(".home-mini-card strong")).map((s) => s.textContent)).toEqual(["kept"]);
});

test("an empty or unreadable history leaves the strip hidden", () => {
    document.body.innerHTML = STRIP;
    localStorage.setItem("ul_recent_pins_v1_abc", "{not json");
    renderRecentStrips(document);
    expect(document.getElementById("home-recent-pins-strip")?.hidden).toBe(true);
    localStorage.setItem("ul_recent_pins_v1_abc", JSON.stringify([{ name: "x", url: "javascript:void 0" }]));
    renderRecentStrips(document);
    expect(document.getElementById("home-recent-pins-strip")?.hidden).toBe(true);
});

test("a recent photo opens the lightbox on the ready photos, at the one clicked", () => {
    document.body.innerHTML = `
      <ul class="home-photo-strip">
        <li class="photo-tile" data-id="4" data-url="/m/4.jpg" data-caption="Stairs" data-taken-at="2026-01-02"><button class="photo-tile-btn">4</button></li>
        <li class="photo-tile" data-id="5" data-url="/m/5.jpg" data-processing="pending"><button class="photo-tile-btn">5</button></li>
        <li class="photo-tile" data-id="6" data-url="/m/6.jpg" data-author="Jo"><button class="photo-tile-btn" id="six">6</button></li>
      </ul>`;
    const opened: { ids: (number | null)[]; index: number; caption: string }[] = [];
    window.galleryOpenLightboxItem = (list, index) => opened.push({ ids: list.map((i) => i.imageId ?? null), index, caption: list[0]?.caption ?? "" });
    uninstall = installHomeOverview(document, { reload: () => navigations++ });
    document.getElementById("six")?.click();
    expect(opened).toEqual([{ ids: [4, 6], index: 1, caption: "Stairs" }]);
});

test("saving the layout posts the chosen widgets and reloads; a failure says so once", async () => {
    document.body.innerHTML = `
      <input type="hidden" id="home-widget-priority-hidden" value="stats,recent_pins">
      <button type="button" data-home-action="save-layout" data-save-url="/dashboard/home/widgets/">Save</button>`;
    uninstall = installHomeOverview(document, { reload: () => navigations++ });
    const button = document.querySelector<HTMLButtonElement>("[data-home-action='save-layout']");
    button?.click();
    expect(button?.disabled).toBe(true);
    await settle();
    expect(posts).toEqual([{ url: "/dashboard/home/widgets/", body: JSON.stringify({ enabled_keys: ["stats", "recent_pins"] }), reported: true }]);
    expect(navigations).toBe(1);

    respond = () => new Response("", { status: 500 });
    if (button) button.disabled = false;
    button?.click();
    await settle();
    expect(toasts).toEqual(["error:Could not save your homepage layout."]);
    expect(button?.disabled).toBe(false);
    expect(navigations).toBe(1);
});
