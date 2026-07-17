/**
 * Tools page "Find Pins in a Photo Folder" card: scans a user-chosen local
 * directory (recursively) for photos/videos with GPS metadata entirely in
 * the browser - files never leave the device while scanning - clusters
 * nearby matches live, filters out locations the user already has a pin for
 * (best-effort, via the main map's cached pin store), and uploads the
 * resulting cluster summaries (lat/lng/dates/count) for review as
 * PinSuggestion rows. Photo files themselves are never uploaded unless the
 * user explicitly checks one in the opt-in picker before clicking upload -
 * everything else stays device-only.
 *
 * Uses the File System Access API (`showDirectoryPicker`) where available,
 * falling back to a `<input webkitdirectory>` file picker (Firefox/Safari).
 */
import exifr from "exifr";
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";
import { addHitToClusters, clusterHits, partitionByCachedPins, type PhotoCluster, type PhotoHit } from "../shared/photo-location-cluster";
import { readCachedPinLocations } from "../shared/pin-cache";

/** Candidate photos a user may opt into uploading per cluster - matches the server's MAX_SUGGESTION_PHOTOS cap. */
const MAX_SELECTABLE_PHOTOS: number = 3;

type UploadCluster = PhotoCluster & { id: string };

const IMAGE_EXTENSIONS = new Set(["jpg", "jpeg", "png", "heic", "heif", "tif", "tiff"]);
// Video GPS extraction is best-effort: exifr's QuickTime/MP4 support is partial,
// so a video with location data may simply yield no match - not a bug, a known limit.
const VIDEO_EXTENSIONS = new Set(["mp4", "mov"]);

function extensionOf(name: string): string {
    const dot = name.lastIndexOf(".");
    return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
}

function isCandidateFile(name: string): boolean {
    const ext = extensionOf(name);
    return IMAGE_EXTENSIONS.has(ext) || VIDEO_EXTENSIONS.has(ext);
}

function toIsoDate(value: unknown): string | undefined {
    if (value instanceof Date && !Number.isNaN(value.getTime())) {
        return value.toISOString().slice(0, 10);
    }
    return undefined;
}

/** Extract a GPS hit from one file via exifr, or null if no usable location was found. */
async function extractHit(file: File): Promise<PhotoHit | null> {
    try {
        const gps = await exifr.gps(file);
        if (!gps || typeof gps.latitude !== "number" || typeof gps.longitude !== "number") return null;
        let date: string | undefined;
        try {
            const meta = await exifr.parse(file, ["DateTimeOriginal", "CreateDate"]);
            date = toIsoDate(meta?.DateTimeOriginal) ?? toIsoDate(meta?.CreateDate);
        } catch {
            // Date extraction is best-effort - a location with no date is still useful.
        }
        return { lat: gps.latitude, lng: gps.longitude, date, file };
    } catch {
        return null;
    }
}

/** Recursively enumerate candidate files under a File System Access directory handle. */
async function* walkDirectoryHandle(dir: FileSystemDirectoryHandle, signal: AbortSignal): AsyncGenerator<File> {
    for await (const handle of dir.values()) {
        if (signal.aborted) return;
        if (handle.kind === "directory") {
            yield* walkDirectoryHandle(handle, signal);
        } else if (isCandidateFile(handle.name)) {
            try {
                yield await handle.getFile();
            } catch {
                // Unreadable file (permissions, race with external deletion) - skip it.
            }
        }
    }
}

class PhotoLocationScanApp {
    private readonly root: HTMLElement;
    private readonly startBtn: HTMLButtonElement;
    private readonly stopBtn: HTMLButtonElement;
    private readonly uploadBtn: HTMLButtonElement;
    private readonly fallbackInput: HTMLInputElement;
    private readonly progressWrap: HTMLElement;
    private readonly progressText: HTMLElement;
    private readonly progressCount: HTMLElement;
    private readonly progressBar: HTMLElement;
    private readonly resultsList: HTMLElement;
    private readonly emptyMsg: HTMLElement;
    private readonly uploadUrl: string;
    private readonly uploadPhotoUrl: string;
    private readonly profileUuid: string;

    private clusters: PhotoCluster[] = [];
    /**
     * Every raw GPS hit found so far, never merged or discarded - `clusters`
     * above is a live, order-dependent grouping kept only to render a
     * manageable-length list while scanning. The final grouping used for
     * upload is recomputed from this full set right before the request goes
     * out, so nothing is lost to the incremental display clustering.
     */
    private allHits: PhotoHit[] = [];
    /**
     * Photos the user has explicitly opted into uploading as a preview,
     * keyed by File object identity (not cluster identity) - `upload()`
     * recomputes clusters fresh from `allHits` right before submitting, so a
     * checkbox toggled against a File during live scanning must still be
     * identifiable against that freshly-reclustered result. File references
     * are stable across both since both derive from the same `allHits`.
     */
    private readonly selectedFiles = new Set<File>();
    /** Object URLs created for thumbnail previews, revoked before each re-render to avoid leaks. */
    private objectUrls: string[] = [];
    private abortController: AbortController | null = null;
    private scanning = false;
    /**
     * True while a throttled render is already queued via requestAnimationFrame
     * (see scheduleRender()) - a full re-render rebuilds every cluster's
     * thumbnails from scratch (revoking and recreating an object URL per
     * photo), so calling it once per GPS hit found rather than once per
     * frame turned a long scan with many results into an O(hits x clusters)
     * churn of DOM nodes and blob URLs, heavy enough to crash the tab.
     */
    private renderScheduled = false;

    constructor(root: HTMLElement) {
        this.root = root;
        this.startBtn = this.el<HTMLButtonElement>("photo-scan-start-btn");
        this.stopBtn = this.el<HTMLButtonElement>("photo-scan-stop-btn");
        this.uploadBtn = this.el<HTMLButtonElement>("photo-scan-upload-btn");
        this.progressWrap = this.el("photo-scan-progress");
        this.progressText = this.el("photo-scan-progress-text");
        this.progressCount = this.el("photo-scan-progress-count");
        this.progressBar = this.el("photo-scan-progress-bar");
        this.resultsList = this.el("photo-scan-results");
        this.emptyMsg = this.el("photo-scan-empty");
        this.uploadUrl = root.dataset.uploadUrl ?? "";
        this.uploadPhotoUrl = root.dataset.uploadPhotoUrl ?? "";
        this.profileUuid = root.dataset.profileUuid ?? "";

        this.fallbackInput = document.createElement("input");
        this.fallbackInput.type = "file";
        this.fallbackInput.multiple = true;
        this.fallbackInput.hidden = true;
        this.fallbackInput.setAttribute("webkitdirectory", "");
        root.appendChild(this.fallbackInput);

        this.startBtn.addEventListener("click", () => void this.start());
        this.stopBtn.addEventListener("click", () => this.stop());
        this.uploadBtn.addEventListener("click", () => void this.upload());
        this.fallbackInput.addEventListener("change", () => void this.scanFileList(this.fallbackInput.files));
    }

    private el<T extends HTMLElement = HTMLElement>(id: string): T {
        const found = this.root.querySelector<T>(`#${id}`) ?? (document.getElementById(id) as T | null);
        if (!found) throw new Error(`photo-location-scan: missing #${id}`);
        return found;
    }

    private async start(): Promise<void> {
        if (this.scanning) return;
        if ("showDirectoryPicker" in window) {
            let dirHandle: FileSystemDirectoryHandle;
            try {
                dirHandle = await window.showDirectoryPicker();
            } catch {
                return; // User cancelled the picker - not an error.
            }
            await this.scanDirectoryHandle(dirHandle);
        } else {
            this.fallbackInput.value = "";
            this.fallbackInput.click();
        }
    }

    private async scanFileList(files: FileList | null): Promise<void> {
        if (!files || files.length === 0) return;
        const candidates = Array.from(files).filter((file) => isCandidateFile(file.name));
        await this.runScan(candidates.length, (async function* () {
            for (const file of candidates) yield file;
        })());
    }

    private async scanDirectoryHandle(dirHandle: FileSystemDirectoryHandle): Promise<void> {
        this.abortController = new AbortController();
        // Scanning starts on the very first file the walk yields instead of
        // waiting for the whole tree to be enumerated first - a folder with a
        // lot of photos or nested subfolders used to sit on "Finding photos
        // and videos..." for as long as the full recursive walk took, with no
        // results appearing until every last file had been listed.
        await this.runScan(null, walkDirectoryHandle(dirHandle, this.abortController.signal));
    }

    /**
     * Scan a stream of candidate files, extracting GPS hits as they arrive.
     *
     * @param total - Known file count up front (the `<input webkitdirectory>`
     *   fallback path already has the full FileList), or `null` when the
     *   count isn't known ahead of time (the File System Access walk streams
     *   files one at a time) - the progress bar shows a running count instead
     *   of a percentage in that case.
     */
    private async runScan(total: number | null, files: AsyncGenerator<File>): Promise<void> {
        if (!this.abortController) this.abortController = new AbortController();
        this.setScanning(true);
        let scanned = 0;
        try {
            for await (const file of files) {
                if (this.abortController.signal.aborted) break;
                scanned += 1;
                this.setProgress(total != null ? `Scanning ${file.name}...` : `Scanning... (${scanned} file(s) checked so far)`, scanned, total ?? 0);
                const hit = await extractHit(file);
                if (hit) {
                    this.allHits.push(hit);
                    this.clusters = addHitToClusters(this.clusters, hit);
                    this.scheduleRender();
                }
            }
        } catch {
            toast.error("Could not fully read that folder. Showing what was found so far.");
        }
        this.renderResults(); // flush any pending throttled render so the final result set is always shown
        this.finishScan();
    }

    private stop(): void {
        this.abortController?.abort();
    }

    private setScanning(scanning: boolean): void {
        this.scanning = scanning;
        this.startBtn.hidden = scanning;
        this.stopBtn.hidden = !scanning;
        if (scanning) this.progressWrap.hidden = false;
        this.uploadBtn.hidden = scanning || this.clusters.length === 0;
        this.updateEmptyMessage();
    }

    private finishScan(): void {
        const stopped = this.abortController?.signal.aborted ?? false;
        this.setScanning(false);
        this.startBtn.textContent = "";
        this.startBtn.innerHTML = '<i class="material-symbols-outlined">folder_open</i><span>Scan another folder</span>';
        this.uploadBtn.disabled = this.clusters.length === 0;
        this.setProgress(`${stopped ? "Stopped" : "Done"} - found ${this.clusters.length} location(s).`, 1, 1);
    }

    private setProgress(text: string, current: number, total: number): void {
        this.progressText.textContent = text;
        this.progressCount.textContent = total > 0 ? `${current} / ${total}` : "";
        const pct = total > 0 ? Math.round((current / total) * 100) : 0;
        this.progressBar.style.width = `${pct}%`;
    }

    private updateEmptyMessage(): void {
        this.emptyMsg.hidden = !this.scanning || this.clusters.length > 0;
    }

    /** Coalesce renderResults() calls to at most once per animation frame - see renderScheduled. */
    private scheduleRender(): void {
        if (this.renderScheduled) return;
        this.renderScheduled = true;
        requestAnimationFrame(() => {
            this.renderScheduled = false;
            this.renderResults();
        });
    }

    private renderResults(): void {
        const cachedPins = readCachedPinLocations(this.profileUuid).map((p) => ({ lat: p.latitude, lng: p.longitude }));
        const { fresh, existing } = partitionByCachedPins(this.clusters, cachedPins);

        for (const url of this.objectUrls) URL.revokeObjectURL(url);
        this.objectUrls = [];
        this.resultsList.innerHTML = "";
        this.updateEmptyMessage();

        for (const cluster of fresh) this.resultsList.appendChild(this.renderCard(cluster, false));
        if (existing.length > 0) {
            const details = document.createElement("details");
            const summary = document.createElement("summary");
            summary.textContent = `${existing.length} location(s) you already have a pin for`;
            details.appendChild(summary);
            for (const cluster of existing) details.appendChild(this.renderCard(cluster, true));
            this.resultsList.appendChild(details);
        }
        this.uploadBtn.disabled = this.clusters.length === 0;
    }

    private renderCard(cluster: PhotoCluster, alreadyHavePin: boolean): HTMLElement {
        const item = document.createElement("li");
        item.className = `photo-scan-result-item${alreadyHavePin ? " photo-scan-result-item--existing" : ""}`;

        const main = document.createElement("div");
        main.className = "photo-scan-result-main";
        const coords = document.createElement("span");
        coords.className = "photo-scan-result-coords";
        coords.textContent = `${cluster.lat.toFixed(5)}, ${cluster.lng.toFixed(5)}`;
        const meta = document.createElement("span");
        meta.className = "photo-scan-result-meta";
        const dateRange = cluster.dates.length > 0 ? cluster.dates.slice().sort().join(", ") : "unknown date";
        meta.textContent = `${cluster.count} photo${cluster.count === 1 ? "" : "s"} - ${dateRange}`;
        main.append(coords, meta);

        const badge = document.createElement("span");
        badge.className = "photo-scan-result-badge";
        badge.textContent = alreadyHavePin ? "Already have a pin" : "New";

        item.append(main, badge);
        if (cluster.photos.length > 0) item.appendChild(this.renderPicker(cluster));
        return item;
    }

    /**
     * Opt-in thumbnail picker for one cluster: unchecked by default, and
     * visually unambiguous about which photos are selected to upload as a
     * preview - a filled, accent-colored badge and border for selected
     * thumbnails, a faint outline for unselected ones, plus a running
     * "N of 3 selected" caption so the state is never ambiguous at a glance.
     */
    private renderPicker(cluster: PhotoCluster): HTMLElement {
        const wrap = document.createElement("div");
        wrap.className = "photo-scan-picker";

        const caption = document.createElement("span");
        caption.className = "photo-scan-picker-caption";

        const thumbs = document.createElement("div");
        thumbs.className = "photo-scan-thumbs";

        const thumbEntries: { el: HTMLElement; checkbox: HTMLInputElement; file: File }[] = [];

        const refresh = (): void => {
            const selectedCount = cluster.photos.filter((file) => this.selectedFiles.has(file)).length;
            caption.textContent = `${selectedCount} of ${MAX_SELECTABLE_PHOTOS} photo${MAX_SELECTABLE_PHOTOS === 1 ? "" : "s"} selected for your review queue`;
            for (const entry of thumbEntries) {
                const isSelected = this.selectedFiles.has(entry.file);
                entry.el.classList.toggle("photo-scan-thumb--selected", isSelected);
                const disable = !isSelected && selectedCount >= MAX_SELECTABLE_PHOTOS;
                entry.el.classList.toggle("photo-scan-thumb--disabled", disable);
                entry.checkbox.disabled = disable;
            }
        };

        for (const file of cluster.photos) {
            const thumbEl = document.createElement("label");
            thumbEl.className = "photo-scan-thumb";

            const url = URL.createObjectURL(file);
            this.objectUrls.push(url);
            const img = document.createElement("img");
            img.src = url;
            img.alt = "";

            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.addEventListener("change", () => {
                if (checkbox.checked) this.selectedFiles.add(file);
                else this.selectedFiles.delete(file);
                refresh();
            });

            const thumbBadge = document.createElement("span");
            thumbBadge.className = "photo-scan-thumb-badge";
            thumbBadge.innerHTML = '<i class="material-symbols-outlined">check_circle</i>';

            thumbEl.append(img, checkbox, thumbBadge);
            thumbs.appendChild(thumbEl);
            thumbEntries.push({ el: thumbEl, checkbox, file });
        }

        wrap.append(caption, thumbs);
        refresh();
        return wrap;
    }

    private async upload(): Promise<void> {
        if (this.allHits.length === 0 || !this.uploadUrl) return;
        this.uploadBtn.disabled = true;
        try {
            // Regroup from the full, un-merged hit list rather than reusing the
            // live display clusters - the display grouping is order-dependent
            // (it locks in whichever cluster a hit met first while scanning),
            // so recomputing fresh from every raw coordinate gives a more
            // accurate final grouping now that the whole set is known. Each
            // cluster gets a fresh client-side id so the response can report
            // back which PinSuggestion it became, for the opt-in photo upload
            // below.
            const finalClusters: UploadCluster[] = clusterHits(this.allHits).map((cluster) => ({ ...cluster, id: crypto.randomUUID() }));
            const response = await fetch(this.uploadUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({
                    clusters: finalClusters.map((cluster) => ({
                        id: cluster.id,
                        latitude: cluster.lat,
                        longitude: cluster.lng,
                        dates: cluster.dates,
                        count: cluster.count,
                    })),
                }),
            });
            const data = (await response.json().catch(() => null)) as {
                error?: string;
                matched_suggestions?: number;
                new_pin_suggestions?: number;
                review_url?: string;
                suggestion_ids?: Record<string, number>;
            } | null;
            if (!response.ok || !data?.review_url) {
                toast.error(data?.error ?? "Could not upload results. Please try again.");
                this.uploadBtn.disabled = false;
                return;
            }
            const total = (data.matched_suggestions ?? 0) + (data.new_pin_suggestions ?? 0);
            toast.success(`Uploaded - found ${total} suggestion(s). Review them in Memories.`);

            await this.uploadSelectedPhotos(finalClusters, data.suggestion_ids ?? {});

            this.clusters = [];
            this.allHits = [];
            this.selectedFiles.clear();
            this.renderResults();
            this.uploadBtn.hidden = true;
        } catch {
            toast.error("Could not upload results. Please check your connection and try again.");
            this.uploadBtn.disabled = false;
        }
    }

    /**
     * Upload any opted-in preview photos, tagged to the PinSuggestion each
     * cluster resolved to. Best-effort: a failed photo upload doesn't undo
     * the location results already saved above, so failures are reported
     * separately rather than blocking the main success toast.
     */
    private async uploadSelectedPhotos(finalClusters: UploadCluster[], suggestionIds: Record<string, number>): Promise<void> {
        if (!this.uploadPhotoUrl) return;
        let failures = 0;
        for (const cluster of finalClusters) {
            const suggestionId = suggestionIds[cluster.id];
            if (!suggestionId) continue;
            const selected = cluster.photos.filter((file) => this.selectedFiles.has(file));
            for (const file of selected) {
                try {
                    const body = new FormData();
                    body.append("suggestion_id", String(suggestionId));
                    body.append("image", file);
                    const res = await fetch(this.uploadPhotoUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body });
                    if (!res.ok) failures += 1;
                } catch {
                    failures += 1;
                }
            }
        }
        if (failures > 0) {
            toast.error(`${failures} preview photo${failures === 1 ? "" : "s"} could not be uploaded, but your location results were saved.`);
        }
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const root = document.getElementById("photo-scan-root");
    if (root) new PhotoLocationScanApp(root);
});
