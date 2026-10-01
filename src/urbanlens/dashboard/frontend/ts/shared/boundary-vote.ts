/**
 * The wiki page's "Which boundary looks right?" dialog (``partials/wiki/_boundary_vote_dialog.html``): a satellite
 * mini map per candidate boundary, and a vote for one.
 */

import type { FetchInit } from "./site-runtime";

declare const L: typeof import("leaflet");

export interface BoundaryVoteDeps {
    drawMiniMap: (el: HTMLElement, polygon: GeoJSON.GeoJsonObject) => void;
    later: (fn: () => void, ms: number) => void;
}

export interface BoundaryVote {
    open(): void;
}

const LEAFLET_LAYOUT_MS = 50;
const AUTO_OPEN_MS = 800;
const CLOSE_AFTER_VOTE_MS = 600;

/** Satellite imagery makes "which outline hugs the real property?" answerable at a glance. */
function drawLeafletMiniMap(el: HTMLElement, polygon: GeoJSON.GeoJsonObject): void {
    const map = L.map(el, {
        zoomControl: false,
        attributionControl: false,
        dragging: false,
        scrollWheelZoom: false,
        doubleClickZoom: false,
        boxZoom: false,
        keyboard: false,
        touchZoom: false,
    });
    window.MapLayers.tileLayer("satellite").addTo(map);
    const layer = L.geoJSON(polygon, { style: { color: "#ffd54f", weight: 3, fillColor: "#ffd54f", fillOpacity: 0.18 } }).addTo(map);
    map.fitBounds(layer.getBounds().pad(0.35));
}

const DEFAULT_DEPS: BoundaryVoteDeps = { drawMiniMap: drawLeafletMiniMap, later: (fn, ms) => void setTimeout(fn, ms) };

function isGeoJson(value: unknown): value is GeoJSON.GeoJsonObject {
    return !!value && typeof value === "object" && typeof Reflect.get(value, "type") === "string";
}

function readOutlines(): { id: string; polygon: GeoJSON.GeoJsonObject }[] {
    let raw: unknown;
    try {
        raw = JSON.parse(document.getElementById("boundary-vote-options-data")?.textContent || "[]");
    } catch {
        return [];
    }
    if (!Array.isArray(raw)) return [];
    return raw.flatMap((option: unknown) => {
        if (!option || typeof option !== "object") return [];
        const id: unknown = Reflect.get(option, "id");
        const polygon: unknown = Reflect.get(option, "polygon");
        return (typeof id === "number" || typeof id === "string") && isGeoJson(polygon) ? [{ id: String(id), polygon }] : [];
    });
}

function remember(key: string): void {
    try {
        localStorage.setItem(key, "1");
    } catch {
        // Private mode: it asks again next visit.
    }
}

function dismissed(key: string): boolean {
    try {
        return localStorage.getItem(key) === "1";
    } catch {
        return false;
    }
}

class VoteRefused extends Error {}

async function castVote(url: string, boundaryId: string): Promise<string | number> {
    const init: FetchInit = {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": window.csrftoken ?? "" },
        body: new URLSearchParams({ boundary_id: boundaryId }),
        __ulReported: true,
    };
    const response = await fetch(url, init);
    const data: unknown = await response.json().catch(() => null);
    const field = (name: string): unknown => (data && typeof data === "object" ? Reflect.get(data, name) : undefined);
    const myVote = field("my_vote_id");
    if (response.ok && field("ok") === true && (typeof myVote === "number" || typeof myVote === "string")) return myVote;
    const reason = field("error");
    throw new VoteRefused(typeof reason === "string" && reason ? reason : "Failed to save your vote.");
}

export function installBoundaryVote(dialog: HTMLDialogElement, deps: BoundaryVoteDeps = DEFAULT_DEPS): BoundaryVote {
    const voteUrl = dialog.dataset.voteUrl ?? "";
    const dismissKey = dialog.dataset.dismissKey ?? "";
    let mapsDrawn = false;

    const drawMaps = (): void => {
        if (mapsDrawn) return;
        mapsDrawn = true;
        for (const { id, polygon } of readOutlines()) {
            const el = document.getElementById(`boundary-vote-map-${id}`);
            if (el) deps.drawMiniMap(el, polygon);
        }
    };
    const open = (): void => {
        if (dialog.open) return;
        dialog.showModal();
        // Leaflet sizes its tiles from the container, which has no size until the dialog is shown.
        deps.later(drawMaps, LEAFLET_LAYOUT_MS);
    };
    const markSelected = (boundaryId: string): void => {
        for (const card of dialog.querySelectorAll<HTMLElement>(".boundary-vote-option")) {
            const selected = card.dataset.boundaryId === boundaryId;
            card.classList.toggle("is-selected", selected);
            const text = card.querySelector(".boundary-vote-choose-text");
            if (text) text.textContent = selected ? "Your choice" : "This one";
        }
    };

    dialog.addEventListener("click", async (event) => {
        const button = event.target instanceof Element ? event.target.closest<HTMLElement>(".boundary-vote-choose-btn") : null;
        if (!button?.dataset.boundaryId) return;
        try {
            markSelected(String(await castVote(voteUrl, button.dataset.boundaryId)));
        } catch (error) {
            window.toastr?.error(error instanceof VoteRefused ? error.message : "Network error saving your vote.");
            return;
        }
        window.toastr?.success("Thanks - your boundary vote was counted.");
        remember(dismissKey);
        deps.later(() => dialog.open && dialog.close(), CLOSE_AFTER_VOTE_MS);
    });
    dialog.querySelector("#boundary-vote-not-now")?.addEventListener("click", () => dialog.close());
    // Any way of closing it counts as "stop opening this by itself here"; the map card's button still opens it.
    dialog.addEventListener("close", () => remember(dismissKey));

    // The server asks for this only while nobody else has voted.
    if (dialog.hasAttribute("data-auto-open") && !dismissed(dismissKey)) deps.later(open, AUTO_OPEN_MS);
    return { open };
}
