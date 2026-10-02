/**
 * The logged-in home page (``pages/home/overview.html``): the "Recently viewed" strips, the recent-photos widget's
 * lightbox, and saving the Customize dialog's widget layout.
 */

import { lightboxItemFromTile, tileFromElement } from "./photo-tile";
import { sendForText } from "./fetch-json";

const STRIP_LIMIT = 6;

export interface HomeOverviewDeps {
    reload: () => void;
}

interface RecentEntry {
    title: string;
    subtitle: string;
    url: string;
}

/** Only a path on this site: the list is read back from storage any script on the origin can write. */
function sitePath(url: unknown): string | null {
    return typeof url === "string" && /^\/(?![/\\])/.test(url) ? url : null;
}

function readRecent(key: string): RecentEntry[] {
    let raw: unknown;
    try {
        raw = JSON.parse(localStorage.getItem(key) ?? "[]");
    } catch {
        return [];
    }
    if (!Array.isArray(raw)) return [];
    return raw.flatMap((item: unknown) => {
        if (!item || typeof item !== "object") return [];
        const url = sitePath(Reflect.get(item, "url"));
        if (!url) return [];
        const text = (name: string): string => {
            const value: unknown = Reflect.get(item, name);
            return typeof value === "string" ? value : "";
        };
        return [{ title: text("title") || text("name") || "Untitled", subtitle: text("subtitle") || "Recently viewed", url }];
    });
}

function card(entry: RecentEntry, icon: string): HTMLAnchorElement {
    const link = document.createElement("a");
    link.className = "home-mini-card";
    link.href = entry.url;
    const glyph = document.createElement("i");
    glyph.className = "material-symbols-outlined";
    glyph.textContent = icon;
    const title = document.createElement("strong");
    title.textContent = entry.title;
    const subtitle = document.createElement("span");
    subtitle.textContent = entry.subtitle;
    link.append(glyph, title, subtitle);
    return link;
}

/** Fill each ``[data-recent-key]`` widget from the history other pages keep under that key. */
export function renderRecentStrips(root: ParentNode): void {
    for (const strip of root.querySelectorAll<HTMLElement>("[data-recent-key]")) {
        const list = strip.querySelector("[data-recent-list]");
        const entries = readRecent(strip.dataset.recentKey ?? "").slice(0, STRIP_LIMIT);
        if (!list || !entries.length) continue;
        list.replaceChildren(...entries.map((entry) => card(entry, strip.dataset.recentIcon ?? "history")));
        strip.hidden = false;
    }
}

function openPhoto(button: Element): void {
    const strip = button.closest(".home-photo-strip");
    const clicked = button.closest<HTMLElement>(".photo-tile");
    if (!strip || !clicked || !window.galleryOpenLightboxItem) return;
    const tiles = Array.from(strip.querySelectorAll<HTMLElement>(".photo-tile:not([data-processing])"));
    const items = tiles.flatMap((el) => {
        const tile = tileFromElement(el);
        return tile ? [lightboxItemFromTile(tile)] : [];
    });
    window.galleryOpenLightboxItem(items, Math.max(tiles.indexOf(clicked), 0));
}

async function saveLayout(button: HTMLButtonElement, deps: HomeOverviewDeps): Promise<void> {
    const hidden = document.getElementById("home-widget-priority-hidden");
    const value = hidden instanceof HTMLInputElement ? hidden.value : "";
    button.disabled = true;
    try {
        await sendForText(button.dataset.saveUrl ?? "", "POST", { enabled_keys: value ? value.split(",") : [] }, { reportsItsOwnErrors: true });
        // Every widget renders server-side, so the new layout needs the page again.
        deps.reload();
    } catch {
        window.toastr?.error("Could not save your homepage layout.");
        button.disabled = false;
    }
}

export function installHomeOverview(root: Document, deps: HomeOverviewDeps = { reload: () => window.location.reload() }): () => void {
    renderRecentStrips(root);
    const strip = root.querySelector<HTMLElement>(".home-photo-strip");
    const stopObserving = strip && window.urbanlensObserveProcessingTiles ? window.urbanlensObserveProcessingTiles(strip, (el, item) => window.urbanlensSettleProcessingThumb?.(el, item)) : null;
    const onClick = (event: MouseEvent): void => {
        const target = event.target instanceof Element ? event.target : null;
        const photo = target?.closest(".home-photo-strip .photo-tile-btn");
        if (photo) {
            openPhoto(photo);
            return;
        }
        const save = target?.closest<HTMLButtonElement>("button[data-home-action='save-layout']");
        if (save) void saveLayout(save, deps);
    };
    root.addEventListener("click", onClick);
    return () => {
        root.removeEventListener("click", onClick);
        stopObserving?.();
    };
}
