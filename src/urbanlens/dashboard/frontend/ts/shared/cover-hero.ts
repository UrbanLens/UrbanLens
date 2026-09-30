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

let installed = false;

export function installGlobalCoverHero(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", (e) => {
        const btn = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-cover-hero-step]") : null;
        if (btn) coverHeroStep(btn.dataset.coverHeroKey ?? "", Number(btn.dataset.coverHeroStep));
    });
}
