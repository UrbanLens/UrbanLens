/**
 * Tools page "Find Pins in a Photo Folder" card: scans a user-chosen local
 * directory (recursively) for photos/videos with GPS metadata entirely in
 * the browser - files never leave the device while scanning - clusters
 * nearby matches live, filters out locations the user already has a pin for
 * (best-effort, via the main map's cached pin store), and uploads only the
 * resulting cluster summaries (lat/lng/dates/count - never the files
 * themselves) for review as PinSuggestion rows.
 *
 * Uses the File System Access API (`showDirectoryPicker`) where available,
 * falling back to a `<input webkitdirectory>` file picker (Firefox/Safari).
 */
import exifr from "exifr";
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";
import { addHitToClusters, clusterHits, partitionByCachedPins, type PhotoCluster, type PhotoHit } from "../shared/photo-location-cluster";
import { readCachedPinLocations } from "../shared/pin-cache";

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
        return { lat: gps.latitude, lng: gps.longitude, date, fileName: file.name };
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
    private abortController: AbortController | null = null;
    private scanning = false;

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
        this.setScanning(true);
        this.setProgress("Finding photos and videos...", 0, 0);

        const candidates: File[] = [];
        try {
            for await (const file of walkDirectoryHandle(dirHandle, this.abortController.signal)) {
                if (this.abortController.signal.aborted) break;
                candidates.push(file);
                this.setProgress(`Found ${candidates.length} file(s) so far...`, 0, 0);
            }
        } catch {
            toast.error("Could not fully read that folder. Showing what was found so far.");
        }

        if (this.abortController.signal.aborted) {
            this.finishScan();
            return;
        }
        await this.runScan(candidates.length, (async function* () {
            for (const file of candidates) yield file;
        })());
    }

    private async runScan(total: number, files: AsyncGenerator<File>): Promise<void> {
        if (!this.abortController) this.abortController = new AbortController();
        this.setScanning(true);
        let scanned = 0;
        for await (const file of files) {
            if (this.abortController.signal.aborted) break;
            scanned += 1;
            this.setProgress(`Scanning ${file.name}...`, scanned, total);
            const hit = await extractHit(file);
            if (hit) {
                this.allHits.push(hit);
                this.clusters = addHitToClusters(this.clusters, hit);
                this.renderResults();
            }
        }
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

    private renderResults(): void {
        const cachedPins = readCachedPinLocations(this.profileUuid).map((p) => ({ lat: p.latitude, lng: p.longitude }));
        const { fresh, existing } = partitionByCachedPins(this.clusters, cachedPins);

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
        return item;
    }

    private async upload(): Promise<void> {
        if (this.allHits.length === 0 || !this.uploadUrl) return;
        this.uploadBtn.disabled = true;
        try {
            // Regroup from the full, un-merged hit list rather than reusing the
            // live display clusters - the display grouping is order-dependent
            // (it locks in whichever cluster a hit met first while scanning),
            // so recomputing fresh from every raw coordinate gives a more
            // accurate final grouping now that the whole set is known.
            const finalClusters = clusterHits(this.allHits);
            const response = await fetch(this.uploadUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({
                    clusters: finalClusters.map((cluster) => ({
                        latitude: cluster.lat,
                        longitude: cluster.lng,
                        dates: cluster.dates,
                        count: cluster.count,
                        label: cluster.label,
                    })),
                }),
            });
            const data = (await response.json().catch(() => null)) as { error?: string; matched_suggestions?: number; new_pin_suggestions?: number; review_url?: string } | null;
            if (!response.ok || !data?.review_url) {
                toast.error(data?.error ?? "Could not upload results. Please try again.");
                this.uploadBtn.disabled = false;
                return;
            }
            const total = (data.matched_suggestions ?? 0) + (data.new_pin_suggestions ?? 0);
            toast.success(`Uploaded - found ${total} suggestion(s). Review them in Memories.`);
            this.clusters = [];
            this.allHits = [];
            this.renderResults();
            this.uploadBtn.hidden = true;
        } catch {
            toast.error("Could not upload results. Please check your connection and try again.");
            this.uploadBtn.disabled = false;
        }
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const root = document.getElementById("photo-scan-root");
    if (root) new PhotoLocationScanApp(root);
});
