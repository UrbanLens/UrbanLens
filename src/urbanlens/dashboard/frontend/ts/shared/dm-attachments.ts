/**
 * What the Messages composer will send with its next message: photos, a map, and a staged share, shown as chips.
 *
 * A share (an @pin/@trip/@friend pick) is staged like a photo rather than sent on the spot, so it goes out with
 * whatever the sender types, when they press Send.
 */

import { byId } from "./dom";
import { fetchJson } from "./fetch-json";
import { UUID_TOKEN } from "./e2ee-urls";
import { processingPlaceholder, watchProcessing } from "./photo-processing";

export interface PendingImage {
    id: number;
    /** Empty until its re-encode lands. */
    url: string;
    failed: boolean;
}

export type ShareKind = "pin" | "trip" | "friend";

export interface StagedShare {
    kind: ShareKind;
    /** The form field the share endpoint reads the value from, e.g. ``pin_slug``. */
    fieldName: string;
    value: string;
    label: string;
    lat: number | null;
    lng: number | null;
}

const SHARE_ICONS: Record<ShareKind, string> = { pin: "location_on", trip: "luggage", friend: "person_add" };

function shareKind(raw: string | undefined): ShareKind | null {
    return raw === "pin" || raw === "trip" || raw === "friend" ? raw : null;
}

function coordinate(raw: string | undefined): number | null {
    const value = raw ? Number.parseFloat(raw) : Number.NaN;
    return Number.isFinite(value) ? value : null;
}

/** The share a share-dialog pick button stands for, or null when the dialog does not say what it shares. */
export function shareFromPick(btn: HTMLElement): StagedShare | null {
    const dialog = btn.closest<HTMLElement>(".dm-share-dialog");
    const kind = shareKind(dialog?.dataset.kind);
    const fieldName = dialog?.dataset.fieldName;
    const value = btn.dataset.value;
    if (!kind || !fieldName || !value) return null;
    return { kind, fieldName, value, label: btn.dataset.label ?? "", lat: coordinate(btn.dataset.lat), lng: coordinate(btn.dataset.lng) };
}

/** A small map rendered by comment-map.js; removed before another takes its place. */
interface MapThumb {
    remove(): void;
}

export class PendingAttachments {
    images: PendingImage[] = [];
    mapUuid: string | null = null;
    share: StagedShare | null = null;

    private thumb: MapThumb | null = null;
    private thumbUuid: string | null = null;

    get isEmpty(): boolean {
        return !this.images.length && !this.mapUuid && !this.share;
    }

    reset(): void {
        this.images = [];
        this.mapUuid = null;
        this.share = null;
        this.render();
    }

    setMap(uuid: string | null): void {
        this.mapUuid = uuid;
        this.render();
    }

    setShare(share: StagedShare | null): void {
        this.share = share;
        this.render();
    }

    /** Add an uploaded photo, watching it until its re-encode lands when the upload says it is still processing. */
    addImage(image: PendingImage, processing: boolean): void {
        this.images.push(image);
        this.render();
        const statusUrl = document.getElementById("dm-messages")?.dataset.processingUrl;
        if (!processing || !statusUrl) return;
        watchProcessing(statusUrl, image.id, document.body, (item) => {
            if (item?.processing) return;
            image.url = item && !item.processing_failed ? String(item.thumb_url || item.url || "") : "";
            image.failed = !image.url;
            if (this.images.includes(image)) this.render();
        });
    }

    render(): void {
        this.renderImages();
        const mapChip = document.getElementById("dm-map-chip");
        if (mapChip) mapChip.hidden = !this.mapUuid;
        const mapField = byId("dm-markup-map-uuid", HTMLInputElement);
        if (mapField) mapField.value = this.mapUuid ?? "";
        this.renderMapThumb(this.mapUuid);
        const shareChip = document.getElementById("dm-share-chip");
        if (!shareChip) return;
        shareChip.hidden = !this.share;
        if (!this.share) return;
        const icon = document.getElementById("dm-share-chip-icon");
        const label = document.getElementById("dm-share-chip-label");
        if (icon) icon.textContent = SHARE_ICONS[this.share.kind];
        if (label) label.textContent = this.share.label;
    }

    private renderImages(): void {
        const wrap = document.getElementById("dm-composer-image-chips");
        if (!wrap) return;
        wrap.replaceChildren(
            ...this.images.map((image) => {
                const chip = document.createElement("span");
                chip.className = "dm-composer-attachment-chip";
                if (image.url) {
                    const img = document.createElement("img");
                    img.src = image.url;
                    img.alt = "";
                    chip.appendChild(img);
                } else {
                    chip.appendChild(processingPlaceholder("dm-composer-attachment-chip__thumb", image.failed));
                }
                const remove = document.createElement("button");
                remove.type = "button";
                remove.setAttribute("aria-label", "Remove photo");
                remove.textContent = "×";
                remove.addEventListener("click", () => {
                    this.images = this.images.filter((i) => i !== image);
                    this.render();
                });
                chip.appendChild(remove);
                return chip;
            }),
        );
    }

    /** The attached map's preview in its chip; unchanged when the map is. */
    private renderMapThumb(uuid: string | null): void {
        const el = document.getElementById("dm-map-chip-thumb");
        if (!el || uuid === this.thumbUuid) return;
        this.thumb?.remove();
        this.thumb = null;
        this.thumbUuid = uuid;
        el.replaceChildren();
        const template = document.getElementById("dm-thread")?.dataset.markupSnapshotUrlTemplate;
        const render = window._renderMapThumb;
        if (!uuid || !template || !render) return;
        fetchJson(template.replace(UUID_TOKEN, uuid), { reportsItsOwnErrors: true })
            .then((data) => {
                // A map changed or removed while this was loading has already moved on.
                if (data && uuid === this.thumbUuid) this.thumb = render(el, data, null);
            })
            .catch(() => undefined);
    }
}
