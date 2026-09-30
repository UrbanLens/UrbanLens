/**
 * A page's cover-photo hero: prev/next previews through the other eligible photos, and the live update after the
 * cover changes.
 *
 * The pin page's hero is ``#pin-detail-hero`` (``_page_hero.html``); any other key's is ``#<key>-cover-hero``. Its
 * candidates, every eligible photo but the cover, are ``{id, url}`` rows in ``#<key>-cover-candidates``. Previewing
 * stores nothing: the cycle wraps back through the true cover.
 */

interface HeroState {
    hero: HTMLElement;
    original: string;
    candidates: string[];
    /** -1 is the true cover. */
    index: number;
}

const states = new Map<string, HeroState>();

/** Where each key's hero goes when a first cover creates it. */
const INSERT_BEFORE: Record<string, string> = { wiki: ".page-tabs" };

function heroFor(key: string): HTMLElement | null {
    return document.getElementById(key === "pin" ? "pin-detail-hero" : `${key}-cover-hero`);
}

function candidateUrls(key: string): string[] {
    try {
        const rows: unknown = JSON.parse(document.getElementById(`${key}-cover-candidates`)?.textContent ?? "[]");
        if (!Array.isArray(rows)) return [];
        return rows.flatMap((row: unknown) => (typeof row === "object" && row !== null && "url" in row && typeof row.url === "string" ? [row.url] : []));
    } catch {
        return [];
    }
}

function stateFor(key: string): HeroState | null {
    const known = states.get(key);
    if (known?.hero.isConnected) return known;
    const hero = heroFor(key);
    if (!hero) return null;
    const state = { hero, original: hero.style.backgroundImage, candidates: candidateUrls(key), index: -1 };
    states.set(key, state);
    return state;
}

function backgroundOf(url: string): string {
    return `url(${JSON.stringify(url)})`;
}

export function coverHeroStep(key: string, step: number): void {
    const state = stateFor(key);
    if (!state?.candidates.length) return;
    const total = state.candidates.length + 1;
    state.index = ((state.index + 1 + step + total) % total) - 1;
    state.hero.style.backgroundImage = state.index < 0 ? state.original : backgroundOf(state.candidates[state.index] ?? "");
}

function heroButton(className: string, title: string, icon: string, data: Record<string, string>): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = className;
    btn.title = title;
    Object.assign(btn.dataset, data);
    const i = document.createElement("i");
    i.className = "material-symbols-outlined";
    i.textContent = icon;
    btn.appendChild(i);
    return btn;
}

/**
 * Show *url* as *key*'s cover, creating the hero for a first cover, or take the cover away when *url* is empty or
 * the viewer's preference hides it.
 */
export function updateCoverHero(key: string, url: string, enabled: boolean): void {
    if (!key) return;
    states.delete(key);
    const hero = heroFor(key);
    const show = !!url && enabled;
    if (key === "pin") {
        if (!hero) return;
        hero.classList.toggle("ul-page-hero--cover", show);
        if (!show) hero.classList.remove("is-adjusting-cover");
        hero.style.backgroundImage = show ? backgroundOf(url) : "";
        return;
    }
    if (!show) {
        hero?.remove();
        return;
    }
    if (hero) {
        hero.style.backgroundImage = backgroundOf(url);
        return;
    }
    const anchor = document.querySelector(INSERT_BEFORE[key] ?? "");
    if (!anchor) return;
    const created = document.createElement("div");
    created.className = "cover-hero";
    created.id = `${key}-cover-hero`;
    created.style.backgroundImage = backgroundOf(url);
    created.append(
        heroButton("cover-hero-nav cover-hero-prev", "Previous photo", "chevron_left", { coverHeroKey: key, coverHeroStep: "-1" }),
        heroButton("cover-hero-nav cover-hero-next", "Next photo", "chevron_right", { coverHeroKey: key, coverHeroStep: "1" }),
        heroButton("cover-hero-remove", "Remove cover photo", "close", { coverPhotoRemove: "" }),
    );
    anchor.before(created);
}

// -- Framing: the viewer's own zoom and focal point for a cover, kept in their browser -------------

const FRAMED = "[data-cover-state-key]";

interface Framing {
    zoom?: string;
    x?: number;
    y?: number;
}

function readFraming(hero: HTMLElement): Framing {
    let raw: unknown;
    try {
        raw = JSON.parse(localStorage.getItem(hero.dataset.coverStateKey ?? "") ?? "{}");
    } catch {
        return {};
    }
    if (!raw || typeof raw !== "object") return {};
    const zoom: unknown = Reflect.get(raw, "zoom");
    const number = (name: string): number | undefined => {
        const value = Number(Reflect.get(raw, name));
        return Reflect.get(raw, name) == null || !Number.isFinite(value) ? undefined : value;
    };
    return { zoom: typeof zoom === "string" || typeof zoom === "number" ? String(zoom) : undefined, x: number("x"), y: number("y") };
}

function saveFraming(hero: HTMLElement, framing: Framing): void {
    try {
        localStorage.setItem(hero.dataset.coverStateKey ?? "", JSON.stringify(framing));
    } catch {
        // Storage unavailable: the framing lasts until the page is left.
    }
}

const clamp = (value: number, min: number, max: number): number => Math.max(min, Math.min(max, value));

function applyFraming(hero: HTMLElement, framing: Framing): void {
    const zoom = clamp(Number.parseInt(framing.zoom ?? "100", 10) || 100, 100, 300);
    const x = clamp(Number(framing.x ?? 50), 0, 100);
    const y = clamp(Number(framing.y ?? 50), 0, 100);
    hero.style.backgroundSize = `${zoom}% auto`;
    hero.style.backgroundPosition = `${x}% ${y}%`;
    const input = hero.querySelector<HTMLInputElement>("[data-cover-zoom]");
    if (input) input.value = String(zoom);
}

function applyWithin(root: Element | Document): void {
    const heroes = root instanceof HTMLElement && root.matches(FRAMED) ? [root] : Array.from(root.querySelectorAll<HTMLElement>(FRAMED));
    for (const hero of heroes) applyFraming(hero, readFraming(hero));
}

/** Heroes mid-drag, so a second pointer cannot start another. */
const dragging = new WeakSet<HTMLElement>();

function installFraming(): void {
    document.addEventListener("click", (event) => {
        const toggle = event.target instanceof Element ? event.target.closest("[data-cover-adjust-toggle]") : null;
        const hero = toggle?.closest<HTMLElement>(FRAMED);
        if (!toggle || !hero) return;
        const on = !hero.classList.contains("is-adjusting-cover");
        hero.classList.toggle("is-adjusting-cover", on);
        toggle.classList.toggle("is-active", on);
        const controls = hero.querySelector<HTMLElement>("[data-cover-adjust-controls]");
        if (controls) controls.hidden = !on;
    });
    document.addEventListener("input", (event) => {
        const input = event.target instanceof HTMLInputElement && event.target.matches("[data-cover-zoom]") ? event.target : null;
        const hero = input?.closest<HTMLElement>(FRAMED);
        if (!input || !hero) return;
        const framing = { ...readFraming(hero), zoom: input.value };
        applyFraming(hero, framing);
        saveFraming(hero, framing);
    });
    document.addEventListener("pointerdown", (down) => {
        const target = down.target instanceof Element ? down.target : null;
        const hero = target?.closest<HTMLElement>(FRAMED);
        if (!target || !hero?.classList.contains("is-adjusting-cover") || dragging.has(hero)) return;
        if (target.closest("[data-cover-adjust-controls], [data-cover-adjust-toggle]")) return;
        dragging.add(hero);
        hero.setPointerCapture(down.pointerId);
        const start = readFraming(hero);
        const startX = Number(start.x ?? 50);
        const startY = Number(start.y ?? 50);
        const onMove = (move: PointerEvent): void => {
            if (move.pointerId !== down.pointerId) return;
            const moved = {
                ...readFraming(hero),
                x: clamp(startX + ((move.clientX - down.clientX) / hero.clientWidth) * 100, 0, 100),
                y: clamp(startY + ((move.clientY - down.clientY) / hero.clientHeight) * 100, 0, 100),
            };
            applyFraming(hero, moved);
            saveFraming(hero, moved);
        };
        const onUp = (up: PointerEvent): void => {
            if (up.pointerId !== down.pointerId) return;
            hero.removeEventListener("pointermove", onMove);
            hero.removeEventListener("pointerup", onUp);
            hero.removeEventListener("pointercancel", onUp);
            dragging.delete(hero);
        };
        hero.addEventListener("pointermove", onMove);
        hero.addEventListener("pointerup", onUp);
        hero.addEventListener("pointercancel", onUp);
    });
    // The pin page's hero is swapped out of band whenever its overview reloads.
    document.addEventListener("htmx:load", (event) => {
        if (event.target instanceof Element) applyWithin(event.target);
    });
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => applyWithin(document));
    else applyWithin(document);
}

let installed = false;

export function installGlobalCoverHero(): void {
    if (installed) return;
    installed = true;
    installFraming();
    document.addEventListener("click", (e) => {
        const btn = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-cover-hero-step]") : null;
        if (btn) coverHeroStep(btn.dataset.coverHeroKey ?? "", Number(btn.dataset.coverHeroStep));
    });
}
