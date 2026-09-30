/**
 * The shared photo lightbox (``partials/_photo_lightbox.html``): every full-size photo viewer on the site opens
 * through ``window.galleryOpenLightboxItem(list, index)``.
 *
 * Installed once from the core bundle. The page's lightbox is the ``dialog[data-photo-lightbox]``, whose data
 * attributes carry the endpoints and the page's options; the current cover lives there too, so it survives the
 * next open.
 */

import { updateCoverHero } from "./cover-hero";
import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";
import { fetchJson, HttpError } from "./fetch-json";
import { tileLayer } from "./map-layers";
import type { LightboxInput } from "./photo-tile";
import type { FetchInit } from "./site-runtime";

declare const L: typeof import("leaflet") | undefined;

const ID_TOKEN = "/0/";
const UUID_TOKEN = "00000000-0000-0000-0000-000000000000";
const PICKER_ID = "lightbox-picker-dialog";
const BROWSER_IMAGE = /\.(jpe?g|png|gif|webp|avif)(\?|$)/i;

type PickerMode = "pin" | "wiki" | "friends";

/** Each picker's title and search placeholder. */
const PICKER_TEXT: Record<PickerMode, readonly [string, string]> = {
    pin: ["File to a pin", "Search your pins by name…"],
    wiki: ["Send to a wiki", "Search wikis you have access to…"],
    friends: ["Share with a friend", "Search your friends…"],
};

function lightboxDialog(): HTMLDialogElement | null {
    const dialog = document.querySelector("dialog[data-photo-lightbox]");
    return dialog instanceof HTMLDialogElement ? dialog : null;
}

function el(id: string): HTMLElement | null {
    return document.getElementById(id);
}

function setLine(id: string, text: string): void {
    const line = el(id);
    if (!line) return;
    line.textContent = text;
    line.hidden = !text;
}

function withId(template: string, id: number): string {
    return template.replace(ID_TOKEN, `/${id}/`);
}

function load(url: string, target: string): void {
    void window.htmx?.ajax("GET", url, { target, swap: "innerHTML" });
}

function failureMessage(err: unknown, fallback: string): string {
    return err instanceof HttpError && !/^HTTP \d+$/.test(err.message) ? err.message : fallback;
}

/** A same-host URL as a path, so an ``http://`` absolute URL on an https page cannot be blocked as mixed content in the iframe. */
export function samePagePath(url: string): string {
    try {
        const parsed = new URL(url, window.location.href);
        if (parsed.host === window.location.host) return parsed.pathname + parsed.search + parsed.hash;
    } catch {
        /* Relative already, or unparseable: used as given. */
    }
    return url;
}

/**
 * The URL to show first: the full file when a browser can render it, else the thumbnail (a Wikimedia .tif, say).
 */
export function displayUrl(item: LightboxInput): string {
    if (item.viewUrl) return item.viewUrl;
    const full = item.url ?? "";
    const thumb = item.thumbUrl ?? "";
    return !BROWSER_IMAGE.test(full) && thumb ? thumb : full;
}

const REMOTE_COPY_RETRIES = 6;

function isRemoteCopy(url: string): boolean {
    return url.includes("/media-copy/");
}

/**
 * Shows this site's copy of a third-party image once it exists. The first request for one answers 503 while it is
 * downloaded and re-encoded, so the thumbnail stands in meanwhile.
 */
export function awaitRemoteCopy(img: HTMLImageElement, url: string, note: HTMLElement | null, attempt = 1): void {
    const standIn = img.src;
    window.setTimeout(() => {
        if (img.src !== standIn) return;
        const probe = new Image();
        probe.onload = () => {
            if (img.src !== standIn) return;
            img.classList.remove("lightbox-img--fallback");
            img.src = probe.src;
        };
        probe.onerror = () => {
            if (img.src !== standIn) return;
            if (attempt < REMOTE_COPY_RETRIES) awaitRemoteCopy(img, url, note, attempt + 1);
            else if (note) note.hidden = false;
        };
        probe.src = `${url}${url.includes("?") ? "&" : "?"}_r=${attempt}`;
    }, 2000 * attempt);
}

/** The HX-Trigger ``showToast`` a photo action answers with; a 200 can still be a refusal. */
function serverToast(response: Response): { level: string; message: string } {
    try {
        const triggers: unknown = JSON.parse(response.headers.get("HX-Trigger") ?? "");
        if (typeof triggers === "object" && triggers !== null && "showToast" in triggers) {
            const t = triggers.showToast;
            if (typeof t === "object" && t !== null) {
                const message = "message" in t && typeof t.message === "string" ? t.message : "";
                const level = "level" in t && typeof t.level === "string" ? t.level : "success";
                return { level, message };
            }
        }
    } catch {
        /* Absent or malformed: the caller's fallback stands. */
    }
    return { level: "success", message: "" };
}

function showServerToast(response: Response, fallback: string): string {
    const { level, message } = serverToast(response);
    const text = message || fallback;
    if (level === "error") toast.error(text);
    else if (level === "warning") toast.warning(text);
    else if (level === "info") toast.info(text);
    else toast.success(text);
    return level;
}

async function postForm(url: string, field: string, value: string): Promise<Response> {
    const init: FetchInit = {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
        body: `${field}=${encodeURIComponent(value)}`,
        // Each pick reports its own failure.
        __ulReported: true,
    };
    return fetch(url, init);
}

export class PhotoLightbox {
    private list: LightboxInput[] = [];
    private index = 0;
    private map: L.Map | null = null;
    private pickerMode: PickerMode | null = null;
    private pickerImageId: number | null = null;
    private pickerDebounce: ReturnType<typeof setTimeout> | undefined;

    get current(): LightboxInput | undefined {
        return this.list[this.index];
    }

    open(list: LightboxInput[], index: number): void {
        const dialog = lightboxDialog();
        if (!dialog) return;
        // showModal()'s top-layer promotion is void under a display:none ancestor, and a host partial can sit in a hidden tab panel.
        if (dialog.parentElement !== document.body) document.body.appendChild(dialog);
        this.list = list;
        this.index = index;
        this.show();
        if (!dialog.open) dialog.showModal();
    }

    step(delta: number): void {
        if (!this.list.length) return;
        this.index = (this.index + delta + this.list.length) % this.list.length;
        this.show();
    }

    private show(): void {
        const item = this.current;
        const dialog = lightboxDialog();
        if (!item || !dialog) return;
        if (item.mediaType === "document") this.showDocument(item);
        else {
            this.hideDocument();
            this.showImage(item);
        }
        let caption = item.caption ?? "";
        if (!caption && item.imageId) caption = el(`gallery-item-${item.imageId}`)?.querySelector(".gallery-caption")?.textContent ?? "";
        const captionEl = el("lightbox-caption");
        if (captionEl) captionEl.textContent = caption;
        this.showMeta(item, dialog);
        this.showLocation(item);
        this.loadStrips(item, dialog);
        const multi = this.list.length > 1;
        const prev = el("lightbox-prev-btn");
        const next = el("lightbox-next-btn");
        if (prev) prev.hidden = !multi;
        if (next) next.hidden = !multi;
    }

    private showDocument(item: LightboxInput): void {
        const img = el("lightbox-img");
        if (img) {
            img.removeAttribute("src");
            img.hidden = true;
        }
        const view = el("lightbox-document-view");
        const frame = el("lightbox-document-frame");
        if (!view || !(frame instanceof HTMLIFrameElement)) return;
        view.hidden = false;
        const url = samePagePath(item.url ?? "");
        frame.src = url;
        const name = el("lightbox-document-name");
        if (name) name.textContent = item.caption || "Untitled document";
        const openLink = el("lightbox-document-open");
        if (openLink instanceof HTMLAnchorElement) openLink.href = url;
    }

    private hideDocument(): void {
        const view = el("lightbox-document-view");
        if (view) view.hidden = true;
        const frame = el("lightbox-document-frame");
        if (frame instanceof HTMLIFrameElement) frame.src = "about:blank";
    }

    /** Falls back to the thumbnail, with a note, when the full file will not load. */
    private showImage(item: LightboxInput): void {
        const img = el("lightbox-img");
        if (!(img instanceof HTMLImageElement)) return;
        const note = el("lightbox-fallback-note");
        img.hidden = false;
        img.classList.remove("lightbox-img--fallback");
        if (note) note.hidden = true;
        const thumb = item.thumbUrl ?? "";
        const display = displayUrl(item);
        img.onerror = () => {
            img.onerror = null;
            if (!thumb || thumb === display) return;
            img.classList.add("lightbox-img--fallback");
            img.src = thumb;
            if (isRemoteCopy(display)) awaitRemoteCopy(img, display, note);
            else if (note) note.hidden = false;
        };
        img.src = display;
    }

    private coverId(dialog: HTMLElement): number | null {
        const id = Number(dialog.dataset.currentCoverId);
        return id > 0 ? id : null;
    }

    private showMeta(item: LightboxInput, dialog: HTMLDialogElement): void {
        const d = dialog.dataset;
        setLine("lightbox-copied-from", item.copiedFromLabel ? `Copied from ${item.copiedFromLabel}’s wiki` : "");
        setLine("lightbox-source-name", item.sourceName ? `Source: ${item.sourceName}` : "");
        setLine("lightbox-author", item.author ? `By ${item.author}` : "");
        const taken = item.takenAt ? new Date(item.takenAt) : null;
        setLine("lightbox-taken-at", taken && !Number.isNaN(taken.getTime()) ? taken.toLocaleString() : "");
        setLine("lightbox-copyright", item.copyright ?? "");
        const source = el("lightbox-source-link");
        if (source instanceof HTMLAnchorElement) {
            if (item.sourceUrl) source.href = item.sourceUrl;
            else source.removeAttribute("href");
            source.hidden = !item.sourceUrl;
        }
        const relevance = el("lightbox-relevance-actions");
        if (relevance) {
            relevance.hidden = !item.canRelevance;
            el("lightbox-relevant-btn")?.classList.toggle("is-active", item.relevant === true);
            el("lightbox-not-relevant-btn")?.classList.toggle("is-active", item.relevant === false);
        }
        const coverActions = el("lightbox-cover-actions");
        if (coverActions) {
            coverActions.hidden = !(item.imageId && d.coverPhotoUrl);
            const isCover = !!item.imageId && item.imageId === this.coverId(dialog);
            const btn = el("lightbox-cover-btn");
            if (btn) {
                const icon = document.createElement("i");
                icon.className = "material-symbols-outlined";
                icon.textContent = isCover ? "hide_image" : "wallpaper";
                btn.replaceChildren(icon, isCover ? " Remove cover photo" : " Set as cover photo");
            }
        }
        const albums = el("albums-panel");
        const canAlbumCover = !!(item.imageId && albums?.dataset.editUrl && albums.dataset.albumSlug);
        const canShowOnMap = !!(item.imageId && item.mapHidden && window.gallerySetPhotoMapHidden);
        const canFloorplan = !!(item.imageId && d.floorplanOverlayUrl && el("lightbox-floorplan-overlay-btn"));
        // Absent isMine reads as the viewer's own; copying needs it to be definitely someone else's.
        const canShare = !!item.imageId && item.isMine !== false;
        const canCopyToPin = !!item.imageId && item.isMine === false && !!d.wikiCopyUrlTemplate;
        for (const [id, allowed] of [
            ["lightbox-album-cover-actions", canAlbumCover],
            ["lightbox-album-cover-action", canAlbumCover],
            ["lightbox-map-visibility-actions", canShowOnMap],
            ["lightbox-map-show-action", canShowOnMap],
            ["lightbox-share-action", canShare],
            ["lightbox-copy-to-pin-action", canCopyToPin],
        ] as const) {
            const target = el(id);
            if (target) target.hidden = !allowed;
        }
        const menu = el("lightbox-actions-menu");
        if (menu) {
            menu.hidden = !(canAlbumCover || canShowOnMap || canFloorplan || canShare || canCopyToPin);
            this.closeActions();
        }
    }

    /**
     * The "where was this taken" map, rebuilt per photo. Its marker drags only for a real photo on a page that can
     * save the move (``window.galleryRepositionImage``); anywhere else it is read-only rather than failing to save.
     */
    private showLocation(item: LightboxInput): void {
        this.map?.remove();
        this.map = null;
        const section = el("lightbox-location");
        const mapEl = el("lightbox-location-map");
        if (!section || !mapEl) return;
        const { latitude: lat, longitude: lng } = item;
        if (typeof lat !== "number" || typeof lng !== "number" || Number.isNaN(lat) || Number.isNaN(lng) || typeof L === "undefined") {
            section.hidden = true;
            return;
        }
        section.hidden = false;
        const reposition = window.galleryRepositionImage;
        const imageId = item.imageId;
        const editable = !!imageId && !!reposition;
        const hint = el("lightbox-location-hint");
        if (hint) hint.hidden = !editable;
        const map = L.map(mapEl, { attributionControl: false, zoomControl: false, dragging: false, scrollWheelZoom: false }).setView([lat, lng], 16);
        tileLayer("street").addTo(map);
        const marker = L.marker([lat, lng], { draggable: editable }).addTo(map);
        if (editable && imageId && reposition) {
            marker.on("dragend", () => {
                const pos = marker.getLatLng();
                const [prevLat, prevLng] = [item.latitude, item.longitude];
                reposition(imageId, pos.lat, pos.lng, () => {
                    if (typeof prevLat === "number" && typeof prevLng === "number") marker.setLatLng([prevLat, prevLng]);
                });
                item.latitude = pos.lat;
                item.longitude = pos.lng;
            });
        }
        this.map = map;
        // The dialog is not yet showing the first time, so Leaflet measured nothing.
        setTimeout(() => this.map?.invalidateSize(), 60);
    }

    /** The side panel's lazily loaded strips, each empty (a 204) for anything but the viewer's own photo. */
    private loadStrips(item: LightboxInput, dialog: HTMLDialogElement): void {
        const d = dialog.dataset;
        const imageId = item.imageId ?? null;
        const strip = (id: string, url: string | null): void => {
            const target = el(id);
            if (!target) return;
            target.replaceChildren();
            if (url) load(url, `#${id}`);
        };
        strip("lightbox-custom-fields", imageId && d.customFieldsUrl ? withId(d.customFieldsUrl, imageId) : null);
        strip("lightbox-media-labels", item.uuid && item.isMine !== false && d.labelImageUrl ? `${d.labelImageUrl.replace(UUID_TOKEN, item.uuid)}?embed=lightbox` : null);
        strip("lightbox-attachment-points", imageId && d.attachmentPointsUrl ? withId(d.attachmentPointsUrl, imageId) : null);
        this.loadAssociations(imageId);
    }

    private loadAssociations(imageId: number | null): void {
        const target = el("lightbox-associations");
        const url = lightboxDialog()?.dataset.associationsUrl;
        if (!target) return;
        target.replaceChildren();
        if (imageId && url) load(withId(url, imageId), "#lightbox-associations");
    }

    closeActions(): void {
        const list = el("lightbox-actions-list");
        if (list) list.hidden = true;
        el("lightbox-actions-toggle")?.setAttribute("aria-expanded", "false");
    }

    private toggleActions(): void {
        const list = el("lightbox-actions-list");
        if (!list) return;
        const open = list.hidden;
        list.hidden = !open;
        el("lightbox-actions-toggle")?.setAttribute("aria-expanded", String(open));
    }

    // -- Cover photo ---------------------------------------------------------------------------------

    /** Set (or with null, clear) the page's cover photo, then update the hero and this panel in place. */
    async setCover(imageId: number | null): Promise<void> {
        const dialog = lightboxDialog();
        const url = dialog?.dataset.coverPhotoUrl;
        if (!dialog || !url) return;
        try {
            const data = await fetchJson(url, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ image_id: imageId }),
                reportsItsOwnErrors: true,
            });
            dialog.dataset.currentCoverId = imageId ? String(imageId) : "";
            const cover = typeof data === "object" && data !== null && "cover_photo" in data && typeof data.cover_photo === "string" ? data.cover_photo : "";
            updateCoverHero(dialog.dataset.coverHeroKey ?? "", cover, dialog.dataset.coverHeroEnabled === "true");
            const item = this.current;
            if (item) this.showMeta(item, dialog);
            toast.success(imageId ? "Cover photo updated." : "Cover photo removed.");
        } catch (err) {
            toast.error(failureMessage(err, "Failed to update cover photo."));
        }
    }

    private toggleCover(): void {
        const item = this.current;
        const dialog = lightboxDialog();
        if (!item?.imageId || !dialog) return;
        void this.setCover(item.imageId === this.coverId(dialog) ? null : item.imageId);
    }

    private async setAlbumCover(): Promise<void> {
        const item = this.current;
        const url = el("albums-panel")?.dataset.editUrl;
        if (!item?.imageId || !url) return;
        try {
            await fetchJson(url, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ cover_image_id: item.imageId }),
                reportsItsOwnErrors: true,
            });
            toast.success("Album cover updated.");
        } catch (err) {
            toast.error(failureMessage(err, "Could not set album cover."));
        }
    }

    private showOnMap(): void {
        const item = this.current;
        const setHidden = window.gallerySetPhotoMapHidden;
        const dialog = lightboxDialog();
        if (!item?.imageId || !setHidden || !dialog) return;
        setHidden(item.imageId, false, () => toast.error("Could not show this photo on the map."));
        item.mapHidden = false;
        this.showMeta(item, dialog);
    }

    private async useAsFloorplanOverlay(button: HTMLElement): Promise<void> {
        const item = this.current;
        const url = lightboxDialog()?.dataset.floorplanOverlayUrl;
        if (!item?.imageId || !url) return;
        const body = new FormData();
        body.append("csrfmiddlewaretoken", getCsrfToken());
        body.append("image_id", String(item.imageId));
        if (item.caption) body.append("name", item.caption);
        if (button instanceof HTMLButtonElement) button.disabled = true;
        try {
            const data = await fetchJson(url, { method: "POST", headers: { Accept: "application/json", "X-CSRFToken": getCsrfToken() }, body, reportsItsOwnErrors: true });
            const floorplan = typeof data === "object" && data !== null && "floorplan_url" in data && typeof data.floorplan_url === "string" ? data.floorplan_url : "";
            if (floorplan) {
                window.location.href = floorplan;
                return;
            }
            toast.success("Added as a map overlay.");
            this.closeActions();
        } catch (err) {
            toast.error(failureMessage(err, "Could not add this photo as an overlay."));
        } finally {
            if (button instanceof HTMLButtonElement) button.disabled = false;
        }
    }

    private async copyToPin(button: HTMLElement): Promise<void> {
        const item = this.current;
        const template = lightboxDialog()?.dataset.wikiCopyUrlTemplate;
        if (!item?.imageId || !template) return;
        if (button instanceof HTMLButtonElement) button.disabled = true;
        try {
            const data = await fetchJson(withId(template, item.imageId), { method: "POST", headers: { Accept: "application/json", "X-CSRFToken": getCsrfToken() }, reportsItsOwnErrors: true });
            const row = typeof data === "object" && data !== null ? data : {};
            const pinName = "pin_name" in row && typeof row.pin_name === "string" ? row.pin_name : "";
            const already = "already_copied" in row && row.already_copied === true;
            toast.success(already ? `Already on your pin "${pinName}".` : `Copied to your pin "${pinName}".`);
            this.closeActions();
        } catch (err) {
            toast.error(failureMessage(err, "Could not copy this photo to your pin."));
        } finally {
            if (button instanceof HTMLButtonElement) button.disabled = false;
        }
    }

    /**
     * Mark a Media-gallery item relevant or not (again to clear), keeping its grid tile in step. Marking relevant
     * saves a local copy server-side, which then replaces the provider's URL here and on the tile.
     */
    private async setRelevance(isRelevant: boolean): Promise<void> {
        const item = this.current;
        const dialog = lightboxDialog();
        const url = dialog?.dataset.pinMediaRelevanceUrl;
        if (!item?.canRelevance || !url || !dialog) return;
        const next = item.relevant === isRelevant ? null : isRelevant;
        item.relevant = next;
        this.showMeta(item, dialog);
        const tile = Array.from(document.querySelectorAll<HTMLElement>("#media-gallery-grid .media-item")).find((t) => t.dataset.mediaSource === item.mediaSource && t.dataset.mediaKey === item.mediaKey);
        if (tile) window._mediaApplyRelevanceState?.(tile, next);
        try {
            const data = await fetchJson(url, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ source: item.mediaSource, item_key: item.mediaKey, url: item.url, is_relevant: next, page_url: item.sourceUrl || "", caption: item.caption || "" }),
                reportsItsOwnErrors: true,
            });
            const row = typeof data === "object" && data !== null ? data : {};
            if ("materialize_error" in row && typeof row.materialize_error === "string" && row.materialize_error) toast.warning(`Marked relevant, but couldn't save a local copy: ${row.materialize_error}`);
            const imageUrl = "image_url" in row && typeof row.image_url === "string" ? row.image_url : "";
            const imageId = "image_id" in row && typeof row.image_id === "number" ? row.image_id : null;
            if (!imageUrl) return;
            item.url = imageUrl;
            item.thumbUrl = imageUrl;
            if (imageId) item.imageId = imageId;
            if (this.current === item) this.showImage(item);
            if (!tile) return;
            tile.dataset.mediaUrl = imageUrl;
            tile.dataset.mediaThumb = imageUrl;
            if (imageId) tile.dataset.imageId = String(imageId);
            const thumb = tile.querySelector(".media-item-thumb");
            if (thumb instanceof HTMLImageElement) thumb.src = imageUrl;
        } catch {
            toast.error("Failed to save.");
        }
    }

    // -- Picker: file to a pin, send to a wiki, share with a friend ---------------------------------

    /**
     * Opens the shared picker for the photo showing now. Its picks act on that photo even if the lightbox behind
     * it moves on, since the two dialogs are not coupled.
     */
    private openPicker(mode: PickerMode): void {
        const item = this.current;
        const dialog = el(PICKER_ID);
        const search = el("lightbox-picker-search");
        const results = el("lightbox-picker-results");
        if (!item?.imageId || !(dialog instanceof HTMLDialogElement) || !(search instanceof HTMLInputElement) || !results) return;
        clearTimeout(this.pickerDebounce);
        this.pickerMode = mode;
        this.pickerImageId = item.imageId;
        const [title, placeholder] = PICKER_TEXT[mode];
        const titleEl = el("lightbox-picker-title");
        if (titleEl) titleEl.textContent = title;
        search.hidden = false;
        search.placeholder = placeholder;
        search.value = "";
        results.replaceChildren();
        this.closeActions();
        dialog.showModal();
        if (mode === "friends") {
            const url = lightboxDialog()?.dataset.shareFriendsUrl;
            if (url) load(`${url}?image_id=${item.imageId}`, "#lightbox-picker-results");
        } else {
            search.focus();
        }
    }

    /** Pins and wikis search the server as the query changes; friends, a short list, filter in place. */
    private onPickerInput(search: HTMLInputElement): void {
        const imageId = this.pickerImageId;
        const d = lightboxDialog()?.dataset;
        if (!imageId || !d) return;
        if (this.pickerMode === "friends") {
            const q = search.value.trim().toLowerCase();
            document.querySelectorAll<HTMLElement>("#lightbox-picker-results .photo-share-friend-item").forEach((li) => {
                li.hidden = q.length > 0 && !(li.dataset.search ?? "").includes(q);
            });
            return;
        }
        const url = this.pickerMode === "pin" ? `${d.pinSearchUrl ?? ""}?q=${encodeURIComponent(search.value)}&image_id=${imageId}&lightbox=1` : `${d.wikiSearchUrl ?? ""}?q=${encodeURIComponent(search.value)}&image_id=${imageId}`;
        clearTimeout(this.pickerDebounce);
        this.pickerDebounce = setTimeout(() => {
            if (imageId === this.pickerImageId) load(url, "#lightbox-picker-results");
        }, 300);
    }

    private closePicker(): void {
        const dialog = el(PICKER_ID);
        if (dialog instanceof HTMLDialogElement) dialog.close();
    }

    private async pick(action: "pin" | "wiki" | "friend", button: HTMLElement): Promise<void> {
        const imageId = this.pickerImageId;
        const d = lightboxDialog()?.dataset;
        const spec = {
            pin: { template: d?.pinActionUrl, field: "pin_slug", value: button.dataset.pinSlug, ok: "Filed to pin.", fail: "Could not file this photo." },
            wiki: { template: d?.wikiActionUrl, field: "location_slug", value: button.dataset.locationSlug, ok: "Sent to wiki.", fail: "Could not send this photo to that wiki." },
            friend: { template: d?.shareActionUrl, field: "friend_slug", value: button.dataset.friendSlug, ok: `Shared with ${button.dataset.username || "your friend"}.`, fail: "Could not share this photo." },
        }[action];
        if (!imageId || !spec.template || !spec.value) return;
        if (action === "friend" && button instanceof HTMLButtonElement) button.disabled = true;
        try {
            const response = await postForm(withId(spec.template, imageId), spec.field, spec.value);
            if (!response.ok) {
                toast.error(spec.fail);
                return;
            }
            const level = showServerToast(response, spec.ok);
            if (action !== "friend") this.loadAssociations(imageId);
            if (level !== "error") this.closePicker();
        } catch {
            toast.error(spec.fail);
        } finally {
            if (button instanceof HTMLButtonElement) button.disabled = false;
        }
    }

    // -- Wiring --------------------------------------------------------------------------------------

    private onAction(action: string, button: HTMLElement, event: Event): void {
        switch (action) {
            case "prev":
                return this.step(-1);
            case "next":
                return this.step(1);
            case "toggle-actions":
                event.stopPropagation();
                return this.toggleActions();
            case "cover":
                return this.toggleCover();
            case "album-cover":
                return void this.setAlbumCover();
            case "show-on-map":
                return this.showOnMap();
            case "floorplan-overlay":
                return void this.useAsFloorplanOverlay(button);
            case "copy-to-pin":
                return void this.copyToPin(button);
            case "relevant":
                return void this.setRelevance(true);
            case "not-relevant":
                return void this.setRelevance(false);
            case "pin-picker":
                return this.openPicker("pin");
            case "wiki-picker":
                return this.openPicker("wiki");
            case "share":
                return this.openPicker("friends");
            case "pick-pin":
                return void this.pick("pin", button);
            case "pick-wiki":
                return void this.pick("wiki", button);
            case "pick-friend":
                return void this.pick("friend", button);
        }
    }

    install(): void {
        window.galleryOpenLightboxItem = (list, index) => this.open(list, index);
        document.addEventListener("click", (e) => {
            const target = e.target instanceof Element ? e.target : null;
            const actionEl = target?.closest<HTMLElement>("[data-lightbox-action]");
            if (actionEl) this.onAction(actionEl.dataset.lightboxAction ?? "", actionEl, e);
            else if (target?.closest("[data-cover-photo-remove]")) void this.setCover(null);
            const menu = el("lightbox-actions-menu");
            if (menu && !menu.hidden && !(target && menu.contains(target))) this.closeActions();
        });
        document.addEventListener("input", (e) => {
            if (e.target instanceof HTMLInputElement && e.target.id === "lightbox-picker-search") this.onPickerInput(e.target);
        });
        document.addEventListener("keydown", (e) => {
            if (!lightboxDialog()?.open) return;
            if (e.key === "ArrowLeft") this.step(-1);
            else if (e.key === "ArrowRight") this.step(1);
        });
        // close does not bubble.
        document.addEventListener(
            "close",
            (e) => {
                if (e.target instanceof HTMLDialogElement && e.target.matches("[data-photo-lightbox]")) {
                    this.map?.remove();
                    this.map = null;
                    this.closeActions();
                } else if (e.target instanceof HTMLElement && e.target.id === PICKER_ID) {
                    clearTimeout(this.pickerDebounce);
                    this.pickerImageId = null;
                    this.pickerMode = null;
                }
            },
            true,
        );
    }
}

let installed = false;

export function installGlobalPhotoLightbox(): void {
    if (installed) return;
    installed = true;
    new PhotoLightbox().install();
}
