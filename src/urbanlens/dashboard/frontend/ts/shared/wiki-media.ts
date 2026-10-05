/**
 * A community wiki's Media section (``pages/location/wiki.html``): every provider's tiles in one grid, ranked by
 * the community's net vote, with a tab per source and a Manage tab holding the wiki's own photo gallery.
 *
 * Unlike the pin page's Media section (``pin-media-gallery.ts``), a thumb here is a vote, and a down-voted item
 * ranks lower rather than hiding. See ``controllers/wiki_media.py``.
 */

import { toast } from "./dialogs";
import { sendJson } from "./fetch-json";
import { htmxDetail } from "./htmx-events";

const TAB_LABELS: Record<string, string> = {
    photos: "Photos",
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
    historical_maps: "Historical Maps",
    redata_media: "Nearby Media",
    redata_aerial: "Aerial & Drone",
    redata_street_level: "Street-level",
};

/** How long every provider gets to produce a tile before the section says it found nothing. */
const EMPTY_AFTER_MS = 15000;

interface VoteResponse {
    materialize_error?: string;
    image_id?: number;
    image_url?: string;
    vote_score?: number;
}

type Vote = boolean | null;

function grid(): HTMLElement | null {
    return document.getElementById("wiki-media-grid");
}

function section(): HTMLElement | null {
    return document.getElementById("wiki-media-section");
}

function items(): HTMLElement[] {
    return Array.from(grid()?.querySelectorAll<HTMLElement>(".media-item") ?? []);
}

function score(item: HTMLElement): number {
    return Number.parseInt(item.dataset.voteScore ?? "0", 10) || 0;
}

function voteOf(item: HTMLElement): Vote {
    return item.dataset.mediaRelevant === "true" ? true : item.dataset.mediaRelevant === "false" ? false : null;
}

function showVote(item: HTMLElement, value: Vote): void {
    item.dataset.mediaRelevant = value === null ? "" : String(value);
    item.classList.toggle("media-item--relevant", value === true);
    item.classList.toggle("media-item--not-relevant", value === false);
    item.querySelector(".media-item-vote-up-btn")?.classList.toggle("is-active", value === true);
    item.querySelector(".media-item-vote-down-btn")?.classList.toggle("is-active", value === false);
}

function tabButton(tab: string, label: string, active: boolean, count?: number): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `card-tab media-tab${active ? " active" : ""}`;
    btn.dataset.tab = tab;
    btn.textContent = label;
    if (count !== undefined) {
        const badge = document.createElement("span");
        badge.className = "media-tab-count";
        badge.textContent = String(count);
        btn.appendChild(badge);
    }
    return btn;
}

export class WikiMedia {
    private activeTab = "all";
    private manageLoaded = false;

    constructor(private readonly voteUrl: string) {}

    install(): void {
        document.addEventListener("click", (event) => this.onClick(event));
        document.body.addEventListener("htmx:afterSwap", (event) => {
            const detail = htmxDetail(event);
            if (detail.target?.id !== "wiki-media-grid") return;
            // A provider still fetching polls itself; its placeholder swaps should not churn the tabs.
            if (detail.xhr?.getResponseHeader("UL-Panel-Pending")) return;
            this.refresh();
        });
        this.refresh();
        // No swap fires when every provider comes back empty, so nothing else would end the spinner.
        window.setTimeout(() => {
            if (items().length) return;
            const loading = document.getElementById("wiki-media-loading");
            if (loading) loading.hidden = true;
            const empty = document.getElementById("wiki-media-empty");
            if (empty) empty.hidden = false;
        }, EMPTY_AFTER_MS);
    }

    /** Re-rank the grid and rebuild its tabs after tiles arrive or a score changes. */
    refresh(): void {
        this.sortByVotes();
        this.rebuildTabs();
        this.applyFilter();
    }

    private sortByVotes(): void {
        const g = grid();
        if (!g) return;
        // Array.sort is stable, so equal scores keep the order the providers delivered them in.
        items()
            .sort((a, b) => score(b) - score(a))
            .forEach((item) => g.appendChild(item));
    }

    private rebuildTabs(): void {
        const tabsEl = document.getElementById("wiki-media-tabs");
        if (!tabsEl) return;
        const counts = new Map<string, number>();
        const all = items();
        for (const item of all) {
            const source = item.dataset.mediaSource;
            if (source) counts.set(source, (counts.get(source) ?? 0) + 1);
        }
        const badge = document.getElementById("wiki-media-count-badge");
        if (badge) {
            badge.textContent = String(all.length);
            badge.hidden = all.length === 0;
        }
        // Zero tiles is "still loading" until the grace period says otherwise.
        if (all.length) {
            const loading = document.getElementById("wiki-media-loading");
            if (loading) loading.hidden = true;
            const empty = document.getElementById("wiki-media-empty");
            if (empty) empty.hidden = true;
        }
        const view = section()?.dataset.mediaView ?? "all";
        if (this.activeTab !== "all" && !counts.has(this.activeTab)) this.activeTab = "all";
        const buttons = [tabButton("all", "All", view === "all" && this.activeTab === "all", all.length), tabButton("manage", "Manage", view === "manage")];
        for (const source of Array.from(counts.keys()).sort()) {
            buttons.push(tabButton(source, TAB_LABELS[source] ?? source, view === "all" && this.activeTab === source, counts.get(source)));
        }
        tabsEl.replaceChildren(...buttons);
    }

    private applyFilter(): void {
        for (const item of items()) item.classList.toggle("media-tab-excluded", this.activeTab !== "all" && item.dataset.mediaSource !== this.activeTab);
    }

    private setView(view: string): void {
        const sec = section();
        if (!sec) return;
        sec.dataset.mediaView = view;
        sec.querySelectorAll<HTMLElement>(".media-view-panel").forEach((panel) => {
            panel.hidden = panel.dataset.mediaViewPanel !== view;
        });
    }

    private highlightTab(tab: string): void {
        document.querySelectorAll<HTMLElement>("#wiki-media-tabs .media-tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tab));
    }

    private selectTab(tab: string): void {
        if (tab === "manage") {
            this.setView("manage");
            this.highlightTab("manage");
            const panel = document.getElementById("wiki-gallery-panel");
            if (!this.manageLoaded && panel && window.htmx) {
                this.manageLoaded = true;
                window.htmx.trigger(panel, "ul:load-manage");
            }
            return;
        }
        this.setView("all");
        this.activeTab = tab;
        this.applyFilter();
        this.highlightTab(tab);
    }

    private onClick(event: MouseEvent): void {
        const target = event.target instanceof Element ? event.target : null;
        const tab = target?.closest<HTMLElement>("#wiki-media-tabs .media-tab");
        if (tab) {
            this.selectTab(tab.dataset.tab ?? "all");
            return;
        }
        const control = target?.closest<HTMLElement>('#wiki-media-grid [data-media-action="vote-up"], #wiki-media-grid [data-media-action="vote-down"]');
        const item = control?.closest<HTMLElement>(".media-item");
        if (!control || !item) return;
        event.stopPropagation();
        void this.vote(item, control.dataset.mediaAction === "vote-up");
    }

    /** Vote *item* up or down; the thumb already lit clears the vote. */
    async vote(item: HTMLElement, up: boolean): Promise<void> {
        const before = voteOf(item);
        const next: Vote = up ? (before === true ? null : true) : before === false ? null : false;
        showVote(item, next);
        let data: VoteResponse | null;
        try {
            data = await sendJson<VoteResponse>(
                this.voteUrl,
                "POST",
                {
                    source: item.dataset.mediaSource,
                    item_key: item.dataset.mediaKey,
                    url: item.dataset.mediaUrl,
                    is_relevant: next,
                    page_url: item.dataset.mediaPageUrl ?? "",
                    caption: item.dataset.mediaCaption ?? "",
                    image_id: item.dataset.imageId || null,
                },
                { reportsItsOwnErrors: true },
            );
        } catch {
            showVote(item, before);
            toast.error("Failed to save your vote.");
            return;
        }
        if (data?.materialize_error) toast.warning(`Vote saved, but couldn't save a local copy: ${data.materialize_error}`);
        if (data?.image_id) item.dataset.imageId = String(data.image_id);
        if (data?.image_url) {
            item.dataset.mediaUrl = data.image_url;
            item.dataset.mediaThumb = data.image_url;
            const thumb = item.querySelector(".media-item-thumb");
            if (thumb instanceof HTMLImageElement) thumb.src = data.image_url;
        }
        if (typeof data?.vote_score === "number") {
            item.dataset.voteScore = String(data.vote_score);
            const votes = item.querySelector(".media-item-votes");
            if (votes) votes.textContent = String(data.vote_score);
        }
        this.sortByVotes();
        this.applyFilter();
    }
}
