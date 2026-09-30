/**
 * The Private Pin page (pages/location/index.html): its external-data panels, the Media section, the edit
 * dialog, adding the pin to a list, in-place name and description editing, onboarding, and the staff debug
 * overlay. The map and its tools are entries/map-annotations.ts.
 */

import { installActionsFab } from "../shared/actions-fab";
import { byId } from "../shared/dom";
import { installVisitPhotos } from "../shared/visit-photos";
import { installAdaptivePagination } from "../shared/adaptive-pagination";
import { installAddToListPicker } from "../shared/add-to-list-picker";
import { deletePinCascade } from "../shared/confirm-dialog";
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";
import { delegateEditInPlace } from "../shared/edit-in-place";
import { escHtml } from "../shared/escape-html";
import { sendJson } from "../shared/fetch-json";
import { installExternalPanelFallbacks } from "../shared/external-panel-fallbacks";
import { initOnboardingTour } from "../shared/onboarding-tour";
import { PinMediaGallery } from "../shared/pin-media-gallery";
import { installPinShareDialog } from "../shared/pin-share-dialog";

declare global {
    interface Window {
        // Called by the staff dev toolbar.
        togglePinDebugOverlay?: () => void;
        clearPinResultCache?: () => void;
    }
}

interface PinConfig {
    pinId: string;
    pinUuid: string;
    pinName: string;
    editUrl: string;
    listItemsAddUrl: string;
    listCreateUrl: string;
    mapUrl: string;
    debugClearUrl: string;
    mediaRelevanceUrl: string;
    mediaSortUrl: string;
    mediaSendToWikiUrl: string;
    hasUsedAliases: boolean;
    showOnboarding: boolean;
}

/** The list URL is reversed with this placeholder in place of the list's slug. */
const LIST_PLACEHOLDER = "00000000-0000-0000-0000-000000000000";

function readConfig(root: HTMLElement): PinConfig {
    const d = root.dataset;
    return {
        pinId: d.pinId ?? "",
        pinUuid: d.pinUuid ?? "",
        pinName: d.pinName ?? "",
        editUrl: d.editUrl ?? "",
        listItemsAddUrl: d.listItemsAddUrl ?? "",
        listCreateUrl: d.listCreateUrl ?? "",
        mapUrl: d.mapUrl ?? "",
        debugClearUrl: d.debugClearUrl ?? "",
        mediaRelevanceUrl: d.mediaRelevanceUrl ?? "",
        mediaSortUrl: d.mediaSortUrl ?? "",
        mediaSendToWikiUrl: d.mediaSendToWikiUrl ?? "",
        hasUsedAliases: d.hasUsedAliases === "1",
        showOnboarding: d.showOnboarding === "1",
    };
}

async function postForm(url: string, fields: Record<string, string>): Promise<void> {
    const r = await fetch(url, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body: new URLSearchParams(fields) });
    if (!r.ok) throw new Error(String(r.status));
}

// -- Debug overlay (staff) -------------------------------------------------------------------

function bindDebugOverlay(cfg: PinConfig): void {
    const overlay = document.getElementById("pin-debug-overlay");
    const listEl = document.getElementById("pin-debug-overlay-list");
    if (!overlay || !listEl) return;
    const STORAGE_KEY = "ul_pin_debug_overlay_visible";

    const render = (): void => {
        const entries = Array.from(document.querySelectorAll<HTMLElement>("[data-debug-source]"));
        if (!entries.length) {
            listEl.innerHTML = '<div class="pin-debug-overlay__empty">No external API results loaded yet.</div>';
            return;
        }
        listEl.innerHTML = entries
            .map((el) => {
                const hit = el.dataset.debugCache === "hit";
                const count = Number.parseInt(el.dataset.debugCount ?? "", 10);
                return (
                    '<div class="pin-debug-overlay__row">' +
                    `<span class="pin-debug-overlay__source">${escHtml(el.dataset.debugSource ?? "")}</span>` +
                    `<span class="pin-debug-overlay__query">${escHtml(el.dataset.debugQuery || "(none)")}</span>` +
                    (Number.isNaN(count) ? "" : `<span class="pin-debug-overlay__count">${count}</span>`) +
                    `<span class="pin-debug-overlay__badge pin-debug-overlay__badge--${hit ? "hit" : "miss"}">${hit ? "CACHE" : "FRESH"}</span>` +
                    "</div>"
                );
            })
            .join("");
    };
    const setVisible = (visible: boolean): void => {
        overlay.hidden = !visible;
        try {
            sessionStorage.setItem(STORAGE_KEY, visible ? "1" : "0");
        } catch {
            // The overlay just forgets its state across loads.
        }
        if (visible) render();
    };

    window.togglePinDebugOverlay = () => setVisible(overlay.hidden !== false);
    window.clearPinResultCache = () => {
        fetch(cfg.debugClearUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken(), "HX-Request": "true" }, credentials: "same-origin" })
            .then((resp) => {
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                toast.success("Page API cache cleared. Reloading...");
                window.setTimeout(() => window.location.reload(), 350);
            })
            .catch(() => toast.error("Could not clear page API cache."));
    };
    document.body.addEventListener("htmx:afterSwap", () => {
        if (!overlay.hidden) render();
    });
    try {
        if (sessionStorage.getItem(STORAGE_KEY) === "1") setVisible(true);
    } catch {
        // No stored state.
    }
}

// -- Add to list ------------------------------------------------------------------------------

function addToList(cfg: PinConfig, listRef: string, control: HTMLElement): void {
    postForm(cfg.listItemsAddUrl.replace(LIST_PLACEHOLDER, listRef), { pin_ids: cfg.pinId })
        .then(() => {
            control.closest("dialog")?.close();
            toast.success("Added to list.");
        })
        .catch(() => toast.error("Could not add pin to that list."));
}

function createListAndAdd(cfg: PinConfig, name: string, nameInput: HTMLInputElement, control: HTMLElement): void {
    // A duplicate name is a 409 whose plain-text body is the message to show.
    sendJson<{ uuid: string }>(cfg.listCreateUrl, "POST", { name }, { headers: { Accept: "application/json" } })
        .then((data) => {
            nameInput.value = "";
            if (data) addToList(cfg, data.uuid, control);
        })
        .catch((err: unknown) => toast.error((err instanceof Error && err.message) || "Could not create that list."));
}

// -- Edit dialog ------------------------------------------------------------------------------

/**
 * Snapshots the form when the dialog opens. A field still at its snapshot is dropped from the submit, so a
 * value another tab changed meanwhile is not overwritten with the stale one this page displayed.
 */
function openEditDialog(): void {
    const form = byId("pin-edit-form", HTMLFormElement);
    if (form) {
        const originals: Record<string, string> = {};
        for (const el of Array.from(form.elements)) {
            if ((el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement) && el.name) originals[el.name] = el.value;
        }
        form.dataset.originals = JSON.stringify(originals);
    }
    byId("pin-edit-dialog", HTMLDialogElement)?.showModal();
}

function dropUnchangedFields(event: Event): void {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.id !== "pin-edit-form" || !form.dataset.originals) return;
    const originals = JSON.parse(form.dataset.originals) as Record<string, string>;
    const params = (event as CustomEvent<{ parameters: Record<string, unknown> }>).detail.parameters;
    for (const [key, value] of Object.entries(originals)) {
        if (key !== "csrfmiddlewaretoken" && params[key] === value) delete params[key];
    }
}

/** The pin's own aliases, rendered with the page and filtered as the name is typed. */
function bindNameSuggestions(): void {
    const input = byId("pe-name", HTMLInputElement);
    const list = document.getElementById("pe-name-suggestions");
    if (!input || !list) return;
    input.addEventListener("focus", () => {
        list.hidden = false;
    });
    input.addEventListener("input", () => {
        const q = input.value.trim().toLowerCase();
        list.querySelectorAll<HTMLElement>("li").forEach((li) => {
            li.hidden = q.length > 0 && !(li.dataset.search ?? "").includes(q);
        });
        list.hidden = false;
    });
    list.addEventListener("click", (e) => {
        const btn = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-name-suggestion]") : null;
        if (!btn) return;
        input.value = btn.dataset.nameSuggestion ?? "";
        list.hidden = true;
    });
    document.addEventListener("click", (e) => {
        if (e.target instanceof Element && e.target.closest(".pin-edit-name-row")) return;
        list.hidden = true;
    });
}

async function deletePin(cfg: PinConfig): Promise<void> {
    const result = await deletePinCascade(cfg.pinUuid, cfg.pinName, getCsrfToken());
    if (result === true) window.location.href = cfg.mapUrl;
    else if (result === null) toast.error("Failed to delete pin");
}

function openImportPhotos(): void {
    const dialog = byId("pin-import-photos-dialog", HTMLDialogElement);
    dialog?.showModal();
    dialog?.querySelector<HTMLElement>(".import-photos-tab")?.click();
}

// -- Onboarding ---------------------------------------------------------------------------------

function initOnboarding(cfg: PinConfig): void {
    initOnboardingTour({
        prefix: "ul_onboarding_v1_pin_detail",
        hostSelector: "#pin-detail-onboarding",
        cards: [
            {
                id: "personal-detail-map",
                icon: "add_location_alt",
                target: "#markup-pin-button",
                eyebrow: "Personal layer",
                title: "Map entrances, hazards, routes, and notes for yourself",
                body: "The map toolbar lets you place private detail pins, shapes, arrows, and labels on this exact location map.",
                button: "Add a detail",
                watchSelector: "#markup-pin-button",
                action: () => {
                    const btn = document.getElementById("markup-pin-button");
                    btn?.scrollIntoView({ behavior: "smooth", block: "center" });
                    setTimeout(() => btn?.click(), 100);
                },
                ready: () => !!document.getElementById("markup-pin-button"),
            },
            {
                id: "photos",
                icon: "add_photo_alternate",
                target: "#pin-gallery-panel, #photo-gallery .gallery-upload-compact-btn",
                eyebrow: "Evidence & memories",
                title: "Attach photos without leaving your Private Pin",
                body: "Drag photos into the gallery or use the upload button - GPS-tagged photos can be positioned on your location map.",
                button: "Upload a photo",
                watchSelector: "#pin-gallery-panel .gallery-upload-compact-btn, #photo-gallery .gallery-upload-compact-btn",
                action: () => {
                    const btn = document.querySelector<HTMLElement>("#pin-gallery-panel .gallery-upload-compact-btn, #photo-gallery .gallery-upload-compact-btn");
                    if (btn) btn.click();
                    else document.getElementById("pin-gallery-panel")?.scrollIntoView({ behavior: "smooth", block: "center" });
                },
                ready: () => !!document.getElementById("pin-gallery-panel"),
            },
            {
                id: "aliases",
                icon: "label_important",
                target: "#pin-aliases-panel",
                eyebrow: "Your naming system",
                title: "Save private alternate names for this pin",
                body: "Aliases help you find places by local names, historic names, or project labels without changing the community wiki.",
                button: "Add an alias",
                watchSelector: "#pin-aliases-panel",
                action: () => {
                    const panel = document.getElementById("pin-aliases-panel");
                    panel?.scrollIntoView({ behavior: "smooth", block: "center" });
                    panel?.querySelector<HTMLElement>('input[type="text"], button')?.focus();
                },
                ready: () => !cfg.hasUsedAliases && !!document.getElementById("pin-aliases-panel"),
            },
            {
                id: "community-wiki",
                icon: "public",
                target: ".pin-location-name, .wiki-back-link",
                eyebrow: "Shared knowledge",
                title: "Open the community wiki when facts should help everyone",
                body: "The wiki holds shared history, access notes, security indicators, photos, and aliases visible to all users who pinned this location.",
                button: "Open community wiki",
                watchSelector: ".pin-location-name a, .wiki-back-link",
                action: () => {
                    const link = document.querySelector<HTMLElement>(".wiki-back-link") ?? document.querySelector<HTMLElement>(".pin-location-name a");
                    if (link) link.click();
                    else document.querySelector(".pin-location-name")?.scrollIntoView({ behavior: "smooth", block: "center" });
                },
                ready: () => !!document.querySelector(".pin-location-name"),
            },
        ],
    });
}

// -- Wiring -------------------------------------------------------------------------------------

function bind(cfg: PinConfig): void {
    bindDebugOverlay(cfg);
    const fab = document.getElementById("pin-actions-fab");
    if (fab) installActionsFab(fab);
    installVisitPhotos();
    if (cfg.showOnboarding) initOnboarding(cfg);
    installAdaptivePagination();
    installPinShareDialog();
    installExternalPanelFallbacks();
    const loaders = document.querySelectorAll(".media-provider-loader").length;
    new PinMediaGallery({ relevanceUrl: cfg.mediaRelevanceUrl, sortUrl: cfg.mediaSortUrl, sendToWikiUrl: cfg.mediaSendToWikiUrl }, loaders).install();
    bindNameSuggestions();
    document.body.addEventListener("htmx:configRequest", dropUnchangedFields);

    document.addEventListener("click", (e) => {
        const control = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-pin-action]") : null;
        if (!control) return;
        switch (control.dataset.pinAction) {
            case "edit":
                openEditDialog();
                break;
            case "delete":
                void deletePin(cfg);
                break;
            case "import-photos":
                openImportPhotos();
                break;
            case "debug-overlay":
                window.togglePinDebugOverlay?.();
                break;
        }
    });
    installAddToListPicker({
        add: (listRef, control) => addToList(cfg, listRef, control),
        create: (name, nameInput, control) => createListAndAdd(cfg, name, nameInput, control),
    });

    delegateEditInPlace(".pin-name-editable", () => ({
        dataKey: "rawName",
        inputClass: "pin-name-input",
        maxLength: 255,
        successMessage: "Pin renamed.",
        errorMessage: "Failed to rename pin.",
        save: (value) => postForm(cfg.editUrl, { name: value }),
        // The Details card shows the name too.
        onSaved: () => document.body.dispatchEvent(new Event("pinOverviewChanged")),
    }));
    delegateEditInPlace(".pin-description--editable", () => ({
        dataKey: "rawDescription",
        inputClass: "pin-description-input",
        maxLength: 50000,
        multiline: true,
        rows: 3,
        selectAll: true,
        autoGrow: true,
        allowEmpty: true,
        placeholder: "Add a description...",
        emptyClass: "pin-description--empty",
        successMessage: "Description updated.",
        errorMessage: "Failed to update description.",
        save: (value) => postForm(cfg.editUrl, { description: value }),
    }));
}

const root = document.querySelector<HTMLElement>(".location-detail-page");
if (root) bind(readConfig(root));
