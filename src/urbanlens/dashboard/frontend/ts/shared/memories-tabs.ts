/**
 * The Memories review tabs: Locations (``pages/memories/locations.html``) and Visits (``visits.html``), each a
 * select map over a card queue, and Maps (``maps.html``), where titles rename in place and cards delete.
 */

import { getCsrfToken } from "./csrf";
import { delegateEditInPlace } from "./edit-in-place";
import { bulkOutcomeOf, createPinSelectMap, reportBulkOutcome, type ItemId, type PinSelectItem } from "./pin-select-map";
import type { FetchInit } from "./site-runtime";

declare const L: typeof import("leaflet");

const MAP_UUID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000";

interface Suggestion extends PinSelectItem {
    id: number;
    name: string;
    hit_count: number;
    is_new_pin: boolean;
}

interface UnloggedPin extends PinSelectItem {
    id: string;
    name: string;
}

function numberField(raw: object, key: string): number | null {
    const value: unknown = Reflect.get(raw, key);
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringField(raw: object, key: string): string {
    const value: unknown = Reflect.get(raw, key);
    return typeof value === "string" ? value : "";
}

export function parseSuggestion(raw: unknown): Suggestion | null {
    if (!raw || typeof raw !== "object") return null;
    const id = numberField(raw, "id");
    const latitude = numberField(raw, "latitude");
    const longitude = numberField(raw, "longitude");
    if (id === null || latitude === null || longitude === null) return null;
    return { id, latitude, longitude, name: stringField(raw, "name"), hit_count: numberField(raw, "hit_count") ?? 0, is_new_pin: Reflect.get(raw, "is_new_pin") === true };
}

export function parseUnloggedPin(raw: unknown): UnloggedPin | null {
    if (!raw || typeof raw !== "object") return null;
    const id = stringField(raw, "id");
    const latitude = numberField(raw, "latitude");
    const longitude = numberField(raw, "longitude");
    if (!id || latitude === null || longitude === null) return null;
    return { id, latitude, longitude, name: stringField(raw, "name") };
}

function placeLabel(item: { name: string; latitude: number; longitude: number }): string {
    return item.name || `${item.latitude.toFixed(5)}, ${item.longitude.toFixed(5)}`;
}

export function suggestionTooltip(item: Suggestion): string {
    return `${placeLabel(item)} · ${item.hit_count} photo${item.hit_count === 1 ? "" : "s"}`;
}

function markerIcon(className: string, icon: string, selected: boolean): L.DivIcon {
    return L.divIcon({ className: `pin-select-marker${className}${selected ? " is-selected" : ""}`, html: `<i class="material-symbols-outlined">${icon}</i>`, iconSize: [30, 30], iconAnchor: [15, 15] });
}

/** A POST whose caller reports its own failure, so the site's fetch net stays quiet. */
function post(url: string, init: FetchInit = {}): Promise<Response> {
    const headers = { "X-CSRFToken": getCsrfToken(), ...(init.body ? { "Content-Type": "application/json" } : {}) };
    const request: FetchInit = { ...init, method: "POST", headers, __ulReported: true };
    return fetch(url, request);
}

function postJson(url: string, body: unknown): Promise<Response> {
    return post(url, { body: JSON.stringify(body) });
}

function scrollToCard(id: string): void {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/** Accept every suggestion, from the page or from the map's new-user onboarding (``data-onboarding="1"``). */
export function installAcceptAll(button: HTMLButtonElement, navigate: (url: string) => void = (url) => window.location.assign(url)): void {
    button.addEventListener("click", async () => {
        button.disabled = true;
        try {
            const outcome = await bulkOutcomeOf(await post(button.dataset.url ?? ""));
            reportBulkOutcome(outcome, "Accepted", "suggestion");
            if (button.dataset.onboarding === "1") navigate(button.dataset.doneUrl ?? "/");
            else window.location.reload();
        } catch {
            window.toastr?.error("Something went wrong. Please try again.");
            button.disabled = false;
        }
    });
}

/** Keeps each suggestion card's "N of M selected" caption and ticked photos in step with its checkboxes. */
export function installPhotoPickCaptions(wrap: HTMLElement): void {
    wrap.addEventListener("change", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement) || (target.name !== "image_ids" && target.name !== "asset_ids")) return;
        const card = target.closest(".pin-suggestion-card");
        if (!card) return;
        const boxes = Array.from(card.querySelectorAll<HTMLInputElement>('input[name="image_ids"], input[name="asset_ids"]'));
        for (const box of boxes) box.closest(".photo-pick")?.classList.toggle("photo-pick--selected", box.checked);
        const caption = card.querySelector("[data-photo-caption]");
        if (caption) caption.textContent = `${boxes.filter((box) => box.checked).length} of ${boxes.length} selected for the pin's gallery`;
    });
}

export function installLocationsTab(mapEl: HTMLElement): void {
    const acceptAll = document.getElementById("pin-suggestions-accept-all-btn");
    if (acceptAll instanceof HTMLButtonElement) installAcceptAll(acceptAll);
    const banner = document.getElementById("pin-suggestions-onboarding-banner");
    banner?.querySelector(".pin-suggestions-onboarding-banner__close")?.addEventListener("click", () => banner.remove());
    const wrap = document.getElementById("pin-suggestions-wrap");
    if (wrap) installPhotoPickCaptions(wrap);

    const bulk = (action: "accept" | "reject") => async (ids: ItemId[]) => {
        const outcome = await bulkOutcomeOf(await postJson(mapEl.dataset[action === "accept" ? "bulkAcceptUrl" : "bulkRejectUrl"] ?? "", { suggestion_ids: ids }));
        reportBulkOutcome(outcome, action === "accept" ? "Accepted" : "Dismissed", "suggestion");
    };
    createPinSelectMap(mapEl, {
        dataUrl: mapEl.dataset.mapDataUrl ?? "",
        itemsKey: "suggestions",
        parse: parseSuggestion,
        idOf: (item) => item.id,
        icon: (item, selected) => (item.is_new_pin ? markerIcon("", "add_location", selected) : markerIcon(" pin-select-marker--alt", "event_available", selected)),
        tooltip: suggestionTooltip,
        cardEl: (id) => document.getElementById(`pin-suggestion-card-${id}`),
        wrapEl: wrap,
        checkboxSelector: ".pin-select-cb",
        checkboxIdAttr: "suggestionId",
        layersPanelId: "pin-suggestions-map-layers",
        selectToggleBtnId: "pin-suggestions-select-toggle",
        namespace: "pin_suggestions",
        bulkActions: { accept: bulk("accept"), reject: bulk("reject") },
        onMarkerClick: (item) => scrollToCard(`pin-suggestion-card-${item.id}`),
    });
}

/** The pin slugs to log or unmark, with the bulk bar's date for a log. */
export function unloggedBulkBody(action: "log" | "unmark", ids: ItemId[]): Record<string, unknown> {
    const body: Record<string, unknown> = { pin_slugs: ids };
    const date = document.querySelector("#ul-bulk-bar-unlogged_visits [data-bulk-date]");
    if (action === "log" && date instanceof HTMLInputElement && date.value) body.visited_date = date.value;
    return body;
}

export function installVisitsTab(mapEl: HTMLElement): void {
    const bulk = (action: "log" | "unmark") => async (ids: ItemId[]) => {
        const outcome = await bulkOutcomeOf(await postJson(mapEl.dataset[action === "log" ? "bulkLogUrl" : "bulkUnmarkUrl"] ?? "", unloggedBulkBody(action, ids)));
        reportBulkOutcome(outcome, action === "log" ? "Logged" : "Unmarked", "visit");
    };
    createPinSelectMap(mapEl, {
        dataUrl: mapEl.dataset.mapDataUrl ?? "",
        itemsKey: "pins",
        parse: parseUnloggedPin,
        idOf: (item) => item.id,
        icon: (_item, selected) => markerIcon("", "location_on", selected),
        tooltip: placeLabel,
        cardEl: (id) => document.getElementById(`unlogged-card-${id}`),
        wrapEl: document.getElementById("memories-unlogged-band"),
        checkboxSelector: ".pin-select-cb",
        checkboxIdAttr: "pinSlug",
        cardSelector: ".unlogged-card",
        layersPanelId: "unlogged-visits-map-layers",
        selectToggleBtnId: "unlogged-visits-select-toggle",
        namespace: "unlogged_visits",
        bulkActions: { log: bulk("log"), unmark: bulk("unmark") },
        onMarkerClick: (item) => scrollToCard(`unlogged-card-${item.id}`),
    });
}

/** What deleting a map asks, naming everything it is attached to. */
export function mapDeleteMessage(labels: string[]): string {
    if (!labels.length) return "Delete this map and all its annotations?";
    return `Deleting this map will remove it from:\n\n${labels.map((label) => `• ${label}`).join("\n")}\n\nAny text there will stay, but a "map removed" notice will show in its place. Delete it?`;
}

export function installMapsTab(page: HTMLElement): void {
    const template = page.dataset.viewStateUrlTemplate ?? "";
    delegateEditInPlace(".memories-map-card-title--editable", (el) => ({
        dataKey: "rawTitle",
        inputClass: "memories-map-card-title-input",
        maxLength: 200,
        allowEmpty: true,
        placeholder: "Untitled map",
        successMessage: "Map renamed.",
        errorMessage: "Failed to rename map.",
        // The editor's own autosave endpoint: a title alone leaves the viewport and layers as they are.
        save: async (title) => {
            const response = await postJson(template.replace(MAP_UUID_PLACEHOLDER, el.dataset.mapUuid ?? ""), { title });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
        },
    }));

    page.addEventListener("click", async (event) => {
        const button = event.target instanceof Element ? event.target.closest<HTMLElement>(".memories-map-delete-btn") : null;
        if (!button) return;
        const labels = button.dataset.attachmentLabels ? button.dataset.attachmentLabels.split("||") : [];
        const confirmed = await window.confirmDialog?.({ title: "Delete map", message: mapDeleteMessage(labels), confirmLabel: "Delete" });
        if (confirmed !== true) return;
        try {
            const response = await post(button.dataset.deleteUrl ?? "");
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            document.getElementById(button.dataset.cardId ?? "")?.remove();
            window.toastr?.success("Map deleted.");
        } catch {
            window.toastr?.error("Failed to delete map.");
        }
    });
}
