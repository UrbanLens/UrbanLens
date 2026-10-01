/**
 * A Vault gallery page's uploads, deletes and lightbox, shared by every media kind.
 */

import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";
import type { LightboxInput } from "./photo-tile";
import { activeVaultSort, type VaultGrid, type VaultKind, vaultTileId, vaultTileSelector } from "./vault-media-grid";

export type VaultLightboxItem = LightboxInput & { url: string; imageId: number };

export interface StorageLimits {
    /** Largest single file the server accepts, or 0 for no limit. */
    readonly maxFileSize: number;
    /** Bytes the profile already stores. */
    readonly used: number;
    /** The profile's quota, or null for unlimited. */
    readonly quota: number | null;
}

export interface VaultUploaderOptions {
    readonly kind: VaultKind;
    /** The multipart field the kind's upload view reads. */
    readonly field: string;
    /** Whether a picked or dropped file belongs on this page at all. */
    readonly accepts: (file: File) => boolean;
    /** The warning for files that belong on another page instead. */
    readonly refusedMessage: (refused: File[]) => string;
    readonly uploadedMessage: (count: number) => string;
    readonly lightboxItem: (tile: HTMLElement) => VaultLightboxItem;
    /** Runs once per batch, after its last file has finished either way. */
    readonly afterUpload?: (uploaded: number) => void;
    /** Ids of elements besides the tile that show the item, removed along with it. */
    readonly companionIds?: (id: number) => string[];
}

const UNITS = ["bytes", "KB", "MB", "GB", "TB"];

export function formatBytes(bytes: number): string {
    let i = 0;
    let n = bytes;
    while (n >= 1024 && i < UNITS.length - 1) {
        n /= 1024;
        i++;
    }
    return `${i === 0 ? n : n.toFixed(1)} ${UNITS[i]}`;
}

/**
 * Why *file* would be refused, checked before the round-trip; the server's own checks are the enforcement.
 *
 * @param committed - Bytes of the same batch already counted against the quota, so a multi-file drop can't
 *     slip past the limit by checking every file against the same starting total.
 * @returns A user-facing reason, or null when the file is fine to send.
 */
export function precheckError(file: File, committed: number, limits: StorageLimits): string | null {
    if (limits.maxFileSize && file.size > limits.maxFileSize) {
        return `That file is too large (${formatBytes(file.size)}). The maximum upload size is ${formatBytes(limits.maxFileSize)}.`;
    }
    if (limits.quota !== null && limits.used + committed + file.size > limits.quota) {
        return `This upload would exceed your storage quota (${formatBytes(limits.used + committed)} of ${formatBytes(limits.quota)} used). Delete some files, or lower your image size in Settings → Storage.`;
    }
    return null;
}

/** POST one file under *field*, resolving to the created item's JSON or throwing the server's reason. */
export async function postUpload(url: string, field: string, file: File): Promise<Record<string, unknown>> {
    const csrf = getCsrfToken();
    const body = new FormData();
    body.append(field, file);
    body.append("csrfmiddlewaretoken", csrf);
    const response = await fetch(url, { method: "POST", body, headers: { "X-CSRFToken": csrf } });
    const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    if (!response.ok) throw new Error(typeof data.error === "string" && data.error ? data.error : `HTTP ${response.status}`);
    return data;
}

function capitalized(word: string): string {
    return word.charAt(0).toUpperCase() + word.slice(1);
}

function readLimits(root: HTMLElement): StorageLimits {
    return {
        maxFileSize: Number.parseInt(root.dataset.maxFileSize ?? "", 10) || 0,
        used: Number.parseInt(root.dataset.storageUsed ?? "", 10) || 0,
        quota: root.dataset.storageQuota ? Number.parseInt(root.dataset.storageQuota, 10) : null,
    };
}

export class VaultUploader {
    private readonly uploadUrl: string;
    private readonly actionBase: string;
    private limits: StorageLimits;
    private dragDepth = 0;
    private internalDrag = false;

    constructor(
        root: HTMLElement,
        private readonly grid: VaultGrid | null,
        private readonly options: VaultUploaderOptions,
    ) {
        this.uploadUrl = root.dataset.uploadUrl ?? "";
        this.actionBase = root.dataset.actionBase ?? "";
        this.limits = readLimits(root);
    }

    private get kind(): VaultKind {
        return this.options.kind;
    }

    private byId(suffix: string): HTMLElement | null {
        return document.getElementById(`${this.kind.plural}-${suffix}`);
    }

    /** Wire the file picker and make the whole page a drop target. */
    bindInputs(): void {
        const input = this.byId("file-input");
        if (input instanceof HTMLInputElement) {
            input.addEventListener("change", () => {
                void this.handleFiles(input.files);
                input.value = "";
            });
        }
        const overlay = this.byId("drop-overlay");
        const hideOverlay = () => {
            if (overlay) overlay.hidden = true;
        };
        // A drag that starts in the page (a tile's img) carries that image's file; an OS file drag never fires dragstart here.
        document.addEventListener("dragstart", () => {
            this.internalDrag = true;
        });
        document.addEventListener("dragend", () => {
            this.internalDrag = false;
        });
        const isFilesDrag = (e: DragEvent) => !this.internalDrag && !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
        document.addEventListener("dragenter", (e) => {
            if (!isFilesDrag(e)) return;
            this.dragDepth++;
            if (overlay) overlay.hidden = false;
        });
        document.addEventListener("dragleave", (e) => {
            if (!isFilesDrag(e) && this.dragDepth === 0) return;
            this.dragDepth = Math.max(0, this.dragDepth - 1);
            if (this.dragDepth === 0) hideOverlay();
        });
        document.addEventListener("drop", () => {
            this.dragDepth = 0;
            this.internalDrag = false;
            hideOverlay();
        });
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && overlay && !overlay.hidden) {
                this.dragDepth = 0;
                overlay.hidden = true;
            }
        });
        overlay?.addEventListener("dragover", (e) => e.preventDefault());
        overlay?.addEventListener("drop", (e) => {
            e.preventDefault();
            e.stopPropagation();
            this.dragDepth = 0;
            hideOverlay();
            if (this.internalDrag) {
                this.internalDrag = false;
                return;
            }
            void this.handleFiles(e.dataTransfer?.files ?? null);
        });
    }

    /** Upload *files*, one request each, after dropping what this page doesn't take or the server would refuse. */
    async handleFiles(files: FileList | File[] | null): Promise<void> {
        let batch = Array.from(files ?? []);
        if (!batch.length) return;

        const refused = batch.filter((f) => !this.options.accepts(f));
        batch = batch.filter((f) => this.options.accepts(f));
        if (refused.length) toast.warning(this.options.refusedMessage(refused));
        if (!batch.length) return;

        let committed = 0;
        const accepted: File[] = [];
        for (const file of batch) {
            const error = precheckError(file, committed, this.limits);
            if (error) {
                toast.error(`${file.name}: ${error}`);
                continue;
            }
            committed += file.size;
            accepted.push(file);
        }
        if (!accepted.length) return;

        const progress = this.byId("upload-progress");
        const bar = this.byId("upload-bar");
        if (progress) progress.hidden = false;
        let done = 0;
        let ok = 0;
        await Promise.all(
            accepted.map(async (file) => {
                try {
                    const item = await postUpload(this.uploadUrl, this.options.field, file);
                    ok++;
                    this.limits = { ...this.limits, used: this.limits.used + file.size };
                    this.prependTile(item);
                } catch (err) {
                    toast.error(`${file.name}: ${(err as Error).message || "upload failed"}`);
                } finally {
                    done++;
                    if (bar) bar.style.width = `${Math.round((done / accepted.length) * 100)}%`;
                }
            }),
        );
        this.finish(ok);
    }

    /** Delete one item after confirming, removing its tile and anything else showing it. */
    async remove(id: number): Promise<void> {
        const noun = this.kind.kind;
        if (!window.confirm(`Delete this ${noun}? This cannot be undone.`)) return;
        try {
            const response = await fetch(`${this.actionBase}${id}/delete/`, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() } });
            if (!response.ok) {
                toast.error(`Could not delete that ${noun}.`);
                return;
            }
        } catch {
            toast.error(`Could not delete that ${noun}.`);
            return;
        }
        const tile = document.getElementById(vaultTileId(this.kind, id));
        if (tile) {
            tile.remove();
            this.adjustCount(-1);
        }
        for (const companion of this.options.companionIds?.(id) ?? []) document.getElementById(companion)?.remove();
        toast.info(`${capitalized(noun)} deleted.`);
    }

    /** Open the shared lightbox over every settled tile in the grid, at *id*. */
    openLightbox(id: number): void {
        const tiles = document.querySelectorAll<HTMLElement>(`${vaultTileSelector(this.kind)}:not([data-processing])`);
        const items = Array.from(tiles, (tile) => this.options.lightboxItem(tile));
        const idx = items.findIndex((item) => item.imageId === id);
        window.galleryOpenLightboxItem?.(items, Math.max(idx, 0));
    }

    private finish(ok: number): void {
        const progress = this.byId("upload-progress");
        const bar = this.byId("upload-bar");
        setTimeout(() => {
            if (progress) progress.hidden = true;
            if (bar) bar.style.width = "0";
        }, 700);
        if (ok) toast.success(this.options.uploadedMessage(ok));
        // Where a fresh upload lands under any other sort is the server's call, so re-fetch once per batch.
        if (ok && activeVaultSort(this.kind) !== "recent") this.grid?.refresh();
        this.options.afterUpload?.(ok);
    }

    private prependTile(raw: Record<string, unknown>): void {
        if (!this.grid) {
            window.location.reload();
            return;
        }
        this.adjustCount(1);
        if (activeVaultSort(this.kind) !== "recent") return;
        const tile = this.grid.renderTile(raw);
        if (tile) this.grid.element.insertBefore(tile, this.grid.element.firstChild);
    }

    /** Keep the gallery's count badge, and the total the grid pages against, in step with a local add or delete. */
    private adjustCount(delta: number): void {
        const grid = this.grid?.element;
        if (grid) grid.dataset.photoCount = String(Math.max(0, (Number.parseInt(grid.dataset.photoCount ?? "", 10) || 0) + delta));
        const head = grid?.closest("section")?.querySelector<HTMLElement>(".photos-gallery-head");
        if (!head) return;
        let badge = head.querySelector<HTMLElement>(":scope > .badge");
        const next = Math.max(0, (badge ? Number.parseInt(badge.textContent ?? "", 10) || 0 : 0) + delta);
        if (next === 0) {
            badge?.remove();
            return;
        }
        if (!badge) {
            badge = document.createElement("span");
            badge.className = "badge";
            head.appendChild(badge);
        }
        badge.textContent = String(next);
    }
}

/**
 * Re-upload a file picked on a "Couldn't upload" card, then dismiss the card.
 *
 * Delegated from *root*, since the cards arrive and leave with the organize queue's htmx swaps.
 */
export function bindUploadRetry(root: HTMLElement, defaultUrl: string, field: string): void {
    root.addEventListener("change", (e) => {
        const input = e.target;
        if (!(input instanceof HTMLInputElement) || !input.getAttribute("data-retry-failure")) return;
        const file = input.files?.[0];
        const url = input.getAttribute("data-retry-url") || defaultUrl;
        const dismissUrl = input.getAttribute("data-dismiss-url");
        const card = input.closest(".photos-issue-card");
        input.value = "";
        if (!file) return;
        postUpload(url, field, file)
            .then(() => {
                toast.success(`${file.name} uploaded.`);
                if (dismissUrl && window.htmx && card) window.htmx.ajax("POST", dismissUrl, { target: card, swap: "outerHTML" });
            })
            .catch((err: Error) => toast.error(`${file.name}: ${err.message || "upload failed"}`));
    });
}
