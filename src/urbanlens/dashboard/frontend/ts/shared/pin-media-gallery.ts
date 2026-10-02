/**
 * The Private Pin page's Media section: each provider's loader appends into one grid as it answers, so a slow
 * provider never holds the others back. Tabs, sorting, relevance votes, multi-select and the All/Mine switch.
 *
 * Until the viewer pages through the grid, later results are folded into the images-first sort; after that
 * they only append, so the page being read is never reshuffled.
 */

import { toast } from "./dialogs";
import { escHtml } from "./escape-html";
import { initAdaptivePagination } from "./adaptive-pagination";
import { writePhotoIds } from "./photo-tile";

declare global {
    interface Window {
        // Used by the photo lightbox partial and by map-annotations.ts's drop-onto-map handler.
        _mediaApplyRelevanceState?: (itemEl: HTMLElement, value: boolean | null) => void;
        mediaApplyMaterializedDrop?: (itemEl: HTMLElement | undefined, data: Record<string, unknown>) => void;
        _mediaDragItemEl?: HTMLElement;
    }
}

export interface PinMediaGalleryConfig {
    relevanceUrl: string;
    sortUrl: string;
    sendToWikiUrl: string;
}

interface RelevanceResponse {
    materialize_error?: string;
    image_url?: string;
    image_id?: number | string;
    latitude?: number | null;
    longitude?: number | null;
    queued?: number;
}

const TAB_LABELS: Record<string, string> = {
    smithsonian: "Smithsonian",
    wikimedia: "Wikimedia",
    wikipedia_media: "Wikipedia",
    loc: "Library of Congress",
    yelp: "Yelp",
    google_images: "Google Images",
    google_maps: "Google Maps",
    searxng_images: "Web Images",
    internet_archive: "Internet Archive",
    digital_commonwealth: "Digital Commonwealth",
    loopnet: "LoopNet",
    cris_building: "NY Historic Preservation (CRIS)",
};

function section(): HTMLElement | null {
    return document.getElementById("media-gallery-section");
}

function grid(): HTMLElement | null {
    return document.getElementById("media-gallery-grid");
}

function mediaItems(root: ParentNode): HTMLElement[] {
    return Array.from(root.querySelectorAll<HTMLElement>(".media-item"));
}

function repaginate(): void {
    initAdaptivePagination(document, true);
}

export function applyRelevanceState(itemEl: HTMLElement, value: boolean | null): void {
    itemEl.dataset.mediaRelevant = value === true ? "true" : value === false ? "false" : "";
    itemEl.classList.toggle("media-item--relevant", value === true);
    itemEl.classList.toggle("media-item--not-relevant", value === false);
    itemEl.querySelector(".media-item-relevant-btn")?.classList.toggle("is-active", value === true);
    itemEl.querySelector(".media-item-not-relevant-btn")?.classList.toggle("is-active", value === false);
}

/** Swaps a tile over to its local copy once the server has materialized it. */
function useLocalCopy(itemEl: HTMLElement, data: RelevanceResponse): void {
    if (!data.image_url) return;
    itemEl.dataset.mediaUrl = data.image_url;
    itemEl.dataset.mediaThumb = data.image_url;
    if (data.image_id) itemEl.dataset.imageId = String(data.image_id);
    const thumb = itemEl.querySelector(".media-item-thumb");
    if (thumb instanceof HTMLImageElement) thumb.src = data.image_url;
}

export class PinMediaGallery {
    private pending: number;
    private activeTab = "all";
    private selecting = false;
    private readonly selectedKeys = new Set<string>();

    constructor(
        private readonly cfg: PinMediaGalleryConfig,
        loaderCount: number,
    ) {
        this.pending = loaderCount;
    }

    /** Relevant items first, then items with an image; stable within each group. */
    private sort(g: HTMLElement): void {
        const relevantFirst = (section()?.dataset.mediaSort || "relevant") === "relevant";
        const groups: HTMLElement[][] = [[], [], [], []];
        for (const item of mediaItems(g)) {
            const relevant = relevantFirst && item.classList.contains("media-item--relevant");
            const image = !!item.querySelector("img.media-item-thumb");
            groups[relevant ? (image ? 0 : 1) : image ? 2 : 3]!.push(item);
        }
        for (const item of groups.flat()) g.appendChild(item);
    }

    /**
     * All | Mine | one tab per source (when there are two or more) | Not Relevant. "Mine" is always there, so a
     * first-time visitor can still reach the upload flow.
     */
    private rebuildTabs(): void {
        const tabsEl = document.getElementById("media-tabs");
        if (!tabsEl) return;
        const g = grid();
        if (!g) {
            // A tablist with no tabs in it misleads a screen reader.
            tabsEl.removeAttribute("role");
            tabsEl.removeAttribute("aria-label");
            tabsEl.replaceChildren();
            return;
        }
        tabsEl.setAttribute("role", "tablist");
        tabsEl.setAttribute("aria-label", "Media view");
        const counts = new Map<string, number>();
        let notRelevant = 0;
        const items = mediaItems(g);
        for (const item of items) {
            const src = item.dataset.mediaSource;
            if (item.classList.contains("media-item--not-relevant")) notRelevant += 1;
            else if (src && src !== "photos") counts.set(src, (counts.get(src) ?? 0) + 1);
        }
        const badge = document.getElementById("media-count-badge");
        if (badge) {
            badge.textContent = String(items.length);
            badge.hidden = items.length === 0;
        }
        const sources = [...counts.keys()].sort();
        const showSourceTabs = sources.length >= 2;
        if (this.activeTab !== "not_relevant" && this.activeTab !== "all" && !(showSourceTabs && sources.includes(this.activeTab))) this.activeTab = "all";
        if (this.activeTab === "not_relevant" && !notRelevant) this.activeTab = "all";

        const view = section()?.dataset.mediaView || "all";
        const tab = (selected: boolean, key: string, label: string, count?: number): string =>
            `<button type="button" role="tab" aria-selected="${selected ? "true" : "false"}" class="card-tab media-tab${selected ? " active" : ""}" data-tab="${escHtml(key)}">` +
            `${escHtml(label)}${count === undefined ? "" : `<span class="media-tab-count">${count}</span>`}</button>`;

        let html = tab(view === "all" && this.activeTab === "all", "all", "All", items.length);
        html += tab(view === "mine", "mine", "Mine");
        if (showSourceTabs) {
            for (const src of sources) html += tab(view === "all" && this.activeTab === src, src, TAB_LABELS[src] ?? src, counts.get(src));
        }
        if (notRelevant) html += tab(view === "all" && this.activeTab === "not_relevant", "not_relevant", "Not Relevant", notRelevant);
        tabsEl.innerHTML = html;
        tabsEl.querySelectorAll<HTMLElement>(".media-tab").forEach((btn) => {
            btn.addEventListener("click", () => {
                if (btn.dataset.tab === "mine") {
                    this.setView("mine");
                    return;
                }
                if (view !== "all") this.setView("all");
                this.setTab(btn.dataset.tab ?? "all");
            });
        });
    }

    /** Marks items outside the tab excluded, without moving the viewer off the page they are on. */
    private applyTabFilter(tab: string): void {
        const g = grid();
        if (!g) return;
        g.closest(".media-gallery")?.classList.toggle("media-gallery--tab-not-relevant", tab === "not_relevant");
        for (const item of mediaItems(g)) {
            const notRelevant = item.classList.contains("media-item--not-relevant");
            const excluded = tab === "all" ? false : tab === "not_relevant" ? !notRelevant : notRelevant || item.dataset.mediaSource !== tab;
            item.classList.toggle("media-tab-excluded", excluded);
        }
        document.querySelectorAll<HTMLElement>("#media-tabs .media-tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tab));
    }

    setTab(tab: string): void {
        this.activeTab = tab;
        this.applyTabFilter(tab);
        const card = grid()?.closest<HTMLElement>(".card");
        if (card) card.dataset.adaptivePaginationCurrentPage = "1";
        repaginate();
    }

    /** "All" is the combined grid; "Mine" shows the pin's own gallery panel, which is always loaded. */
    setView(view: string): void {
        const sec = section();
        if (sec) sec.dataset.mediaView = view;
        document.querySelectorAll<HTMLElement>("[data-media-view-panel]").forEach((panel) => {
            panel.hidden = panel.dataset.mediaViewPanel !== view;
        });
        const settingsBtn = document.getElementById("media-settings-btn");
        if (settingsBtn) settingsBtn.hidden = view !== "all";
        this.rebuildTabs();
        if (view === "all") repaginate();
    }

    private refresh(): void {
        const g = grid();
        if (!g || !mediaItems(g).length) return;
        if (g.closest<HTMLElement>(".card")?.dataset.userPaginated !== "1") this.sort(g);
        for (const item of mediaItems(g)) item.setAttribute("data-adaptive-pagination-item", "");
        this.rebuildTabs();
        if (this.activeTab !== "all") this.applyTabFilter(this.activeTab);
        repaginate();
    }

    private finishLoader(el: Element): void {
        el.remove();
        this.pending -= 1;
        if (this.pending > 0) return;
        document.getElementById("media-gallery-loading")?.remove();
        const sec = section();
        if (sec) sec.hidden = false;
        // Tiles, not the hidden debug markers every provider response also carries.
        const empty = document.getElementById("media-gallery-empty");
        const g = grid();
        if (empty) empty.hidden = !!(g && mediaItems(g).length);
    }

    setRelevance(itemEl: HTMLElement, isRelevant: boolean): void {
        const was = itemEl.dataset.mediaRelevant;
        // Pressing the active vote again clears it.
        const next = isRelevant && was === "true" ? null : !isRelevant && was === "false" ? null : isRelevant;
        applyRelevanceState(itemEl, next);
        this.rebuildTabs();
        this.applyTabFilter(this.activeTab);
        repaginate();
        // Applied optimistically, so a refusal has to be reported or it silently reverts on the next load.
        window.ulSendJson?.<RelevanceResponse>(this.cfg.relevanceUrl, "POST", {
            source: itemEl.dataset.mediaSource,
            item_key: itemEl.dataset.mediaKey,
            url: itemEl.dataset.mediaUrl,
            is_relevant: next,
            page_url: itemEl.dataset.mediaPageUrl || "",
            caption: itemEl.dataset.mediaCaption || "",
        })
            .then((data) => {
                if (data?.materialize_error) toast.warning(`Marked relevant, but couldn't save a local copy: ${data.materialize_error}`);
                if (data) useLocalCopy(itemEl, data);
            })
            .catch(() => toast.error("Failed to save."));
    }

    applyMaterializedDrop(itemEl: HTMLElement | undefined, data: RelevanceResponse): void {
        if (data.materialize_error) toast.warning(`Coordinates saved, but couldn't save a local copy: ${data.materialize_error}`);
        if (!itemEl || !data.image_url) return;
        applyRelevanceState(itemEl, true);
        useLocalCopy(itemEl, data);
        if (data.latitude != null) itemEl.dataset.lat = String(data.latitude);
        if (data.longitude != null) itemEl.dataset.lng = String(data.longitude);
        this.rebuildTabs();
        this.applyTabFilter(this.activeTab);
    }

    toggleSelectMode(): void {
        this.selecting = !this.selecting;
        this.selectedKeys.clear();
        section()?.classList.toggle("media-gallery--selecting", this.selecting);
        document.getElementById("media-select-btn")?.classList.toggle("is-active", this.selecting);
        document.querySelectorAll<HTMLElement>("#media-gallery-grid .media-item").forEach((item) => {
            item.classList.remove("is-selected");
            const check = item.querySelector<HTMLElement>(".media-item-select-check");
            if (check) check.hidden = !this.selecting;
        });
        window.ulBulkToolbar?.clear("media");
    }

    /** The one Select button drives whichever view is showing. */
    dispatchSelect(): void {
        if (section()?.dataset.mediaView === "mine" && window.photosToggleSelectMode) {
            window.photosToggleSelectMode();
            return;
        }
        this.toggleSelectMode();
    }

    private toggleSelected(item: HTMLElement): void {
        const key = `${item.dataset.mediaSource}:${item.dataset.mediaKey}`;
        const selected = !item.classList.contains("is-selected");
        item.classList.toggle("is-selected", selected);
        if (selected) this.selectedKeys.add(key);
        else this.selectedKeys.delete(key);
        window.ulBulkToolbar?.sync("media", this.selectedKeys.size, {
            relevant: () => this.bulkSetRelevance(true),
            not_relevant: () => this.bulkSetRelevance(false),
            wiki: () => void this.bulkSendToWiki(),
            deselect: () => this.toggleSelectMode(),
        });
    }

    private selected(): HTMLElement[] {
        return Array.from(document.querySelectorAll<HTMLElement>("#media-gallery-grid .media-item.is-selected"));
    }

    private bulkSetRelevance(isRelevant: boolean): void {
        for (const item of this.selected()) this.setRelevance(item, isRelevant);
    }

    private async bulkSendToWiki(): Promise<void> {
        const items = this.selected();
        if (!items.length) return;
        const ok = await (window.confirmDialog?.({
            title: "Send to wiki",
            message: `Send ${items.length} photo${items.length === 1 ? "" : "s"} to the community wiki for this location?`,
            confirmLabel: "Send",
            danger: false,
        }) ?? Promise.resolve(false));
        if (ok !== true) return;
        const payload = items.map((item) => ({ source: item.dataset.mediaSource, url: item.dataset.mediaUrl, page_url: item.dataset.mediaPageUrl, caption: item.dataset.mediaCaption }));
        try {
            const data = await window.ulSendJson?.<RelevanceResponse>(this.cfg.sendToWikiUrl, "POST", { items: payload });
            // Queued, not stored: the downloads finish in the background.
            toast.success(`${data?.queued ?? items.length} photo(s) queued for the wiki - they'll appear shortly.`);
            this.toggleSelectMode();
        } catch {
            toast.error("Failed to send photos to the wiki.");
        }
    }

    private toggleSettingsMenu(): void {
        const menu = document.getElementById("media-settings-menu");
        if (!menu) return;
        menu.hidden = !menu.hidden;
        document.getElementById("media-settings-btn")?.classList.toggle("is-active", !menu.hidden);
    }

    private setVisibility(btn: HTMLElement): void {
        const sec = section();
        if (!sec) return;
        sec.classList.remove("media-gallery--show-hidden", "media-gallery--only-relevant");
        const mode = btn.dataset.mediaVisibility;
        if (mode === "show-hidden") sec.classList.add("media-gallery--show-hidden");
        if (mode === "only-relevant") sec.classList.add("media-gallery--only-relevant");
        document.querySelectorAll<HTMLElement>("[data-media-visibility]").forEach((b) => b.classList.toggle("active", b === btn));
        repaginate();
    }

    private setSort(btn: HTMLElement): void {
        const sort = btn.dataset.mediaSortOption ?? "relevant";
        const sec = section();
        if (sec) sec.dataset.mediaSort = sort;
        document.querySelectorAll<HTMLElement>("[data-media-sort-option]").forEach((b) => b.classList.toggle("active", b === btn));
        const g = grid();
        if (g) {
            this.sort(g);
            repaginate();
        }
        // Already applied locally; only its persistence can fail.
        window.ulSendJson?.(this.cfg.sortUrl, "POST", { sort }).catch(() => toast.warning("Couldn't save your sort preference."));
    }

    private onClick(event: MouseEvent): void {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;

        const menu = document.getElementById("media-settings-menu");
        const settingsBtn = document.getElementById("media-settings-btn");
        if (menu && !menu.hidden && !menu.contains(target) && !settingsBtn?.contains(target)) {
            menu.hidden = true;
            settingsBtn?.classList.remove("is-active");
        }

        const control = target.closest<HTMLElement>("[data-media-action], [data-media-visibility], [data-media-sort-option]");
        if (!control) return;
        if (control.dataset.mediaVisibility) {
            this.setVisibility(control);
            return;
        }
        if (control.dataset.mediaSortOption) {
            this.setSort(control);
            return;
        }
        const item = control.closest<HTMLElement>(".media-item");
        switch (control.dataset.mediaAction) {
            case "settings-menu":
                this.toggleSettingsMenu();
                break;
            case "dispatch-select":
                this.dispatchSelect();
                break;
            case "select":
                if (item) this.toggleSelected(item);
                break;
            case "relevant":
            case "not-relevant":
                if (item) this.setRelevance(item, control.dataset.mediaAction === "relevant");
                break;
        }
    }

    /** HTML5 drag-and-drop carries only strings, so the tile itself is parked for the map's drop handler. */
    private onDragStart(event: DragEvent): void {
        const item = event.target instanceof Element ? event.target.closest<HTMLElement>('.media-item[draggable="true"]') : null;
        if (!item || !event.dataTransfer) return;
        // One of the viewer's own photos already has a row; the map moves it rather than saving a copy.
        if (item.dataset.mediaSource === "photos") {
            const id = Number.parseInt(item.dataset.imageId ?? "", 10);
            if (id) writePhotoIds(event.dataTransfer, [id]);
            return;
        }
        event.dataTransfer.effectAllowed = "copy";
        window._mediaDragItemEl = item;
        event.dataTransfer.setData(
            "text/media-item",
            JSON.stringify({
                source: item.dataset.mediaSource,
                key: item.dataset.mediaKey,
                url: item.dataset.mediaUrl,
                pageUrl: item.dataset.mediaPageUrl || "",
                caption: item.dataset.mediaCaption || "",
            }),
        );
    }

    private isLoader(el: Element | undefined): el is Element {
        return !!el?.classList.contains("media-provider-loader");
    }

    install(): void {
        this.rebuildTabs();
        document.body.addEventListener("htmx:afterOnLoad", (event) => {
            const { elt, xhr } = (event as CustomEvent<{ elt?: Element; xhr?: XMLHttpRequest }>).detail ?? {};
            if (!this.isLoader(elt)) return;
            // The provider is still fetching: the body is a replacement loader that polls again.
            if (xhr?.getResponseHeader("UL-Panel-Pending") === "1") return;
            if (xhr?.status === 200) {
                const sec = section();
                if (sec) sec.hidden = false;
                this.refresh();
            }
            this.finishLoader(elt);
        });
        const onFailure = (event: Event): void => {
            const { elt } = (event as CustomEvent<{ elt?: Element }>).detail ?? {};
            if (!this.isLoader(elt)) return;
            event.stopImmediatePropagation();
            this.finishLoader(elt);
        };
        for (const name of ["htmx:responseError", "htmx:sendError", "htmx:timeout"]) document.body.addEventListener(name, onFailure, true);

        document.addEventListener("click", (event) => this.onClick(event));
        document.addEventListener("dragstart", (event) => this.onDragStart(event));

        const sort = section()?.dataset.mediaSort ?? "relevant";
        document.querySelectorAll<HTMLElement>("[data-media-sort-option]").forEach((b) => b.classList.toggle("active", b.dataset.mediaSortOption === sort));
        document.querySelector('[data-media-visibility="default"]')?.classList.add("active");

        window._mediaApplyRelevanceState = applyRelevanceState;
        window.mediaApplyMaterializedDrop = (itemEl, data) => this.applyMaterializedDrop(itemEl, data as RelevanceResponse);
    }
}
