/**
 * The pin and Memories import wizard (pages/location/import/csv.html).
 *
 * The wizard arrives by htmx swap each time it is opened, but a module runs once per page, so this binds every
 * wizard it finds now and every one a later swap brings.
 */

import { escHtml } from "../shared/escape-html";
import { fetchResponse } from "../shared/fetch-json";

interface PreviewPin {
    name?: string;
    lat: number;
    lng: number;
    description?: string;
    cid?: string;
    maps_url?: string;
    needs_repair?: boolean;
    _selected?: boolean;
}

interface PreviewList {
    stem: string;
    pins: PreviewPin[];
    _selectedLabelIds: number[];
}

interface PreviewLabel {
    id: number;
    name: string;
    icon?: string;
    color?: string;
}

interface PreviewData {
    lists: PreviewList[];
    total: number;
    labels?: PreviewLabel[];
    history_summary?: string[];
    warnings?: string[];
    preview_id?: string;
}

interface ImportJob {
    job_id?: string;
    status_url: string;
    cancel_url?: string;
    total?: number;
    error?: string;
}

interface ImportResult {
    current?: number;
    total?: number;
    created?: number;
    exists?: number;
    skipped?: number;
    deferred?: number;
    outcome?: string;
    name?: string;
    history?: string;
    notices?: string[];
}

interface ImportState<R> {
    status: string;
    message?: string;
    progress?: number;
    result?: R;
}

type ProgressEvent =
    | { type: "start"; total?: number }
    | { type: "progress"; current: number; total?: number; percent?: number; created?: number; exists?: number; skipped?: number; outcome?: string; name?: string }
    | { type: "complete"; total?: number; created?: number; exists?: number; skipped?: number; deferred?: number }
    | { type: "deferred"; count: number }
    | { type: "error"; message?: string };

type StepName = "upload" | "parsing" | "preview" | "progress";
type MediaKind = "image" | "video" | null;

const IMAGE_EXT_RE = /\.(jpe?g|png|gif|webp|heic|heif|bmp|tiff?|avif)$/i;
const VIDEO_EXT_RE = /\.(mp4|mov|m4v|webm|avi|mkv|3gp)$/i;
const IMPORT_POLL_MS = 1000;
const IMPORT_POLL_GIVE_UP_AFTER = 120;
const MAX_LOG = 40;

function required<T extends HTMLElement>(id: string): T {
    const el = document.getElementById(id);
    if (!el) throw new Error(`import wizard: #${id} is missing`);
    return el as T;
}

function getCsrfToken(): string {
    const el = document.querySelector<HTMLInputElement>("[name=csrfmiddlewaretoken]");
    if (el) return el.value;
    const m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1]! : "";
}

function errorMessage(err: unknown, fallback: string): string {
    return err instanceof Error && err.message ? err.message : fallback;
}

function isAbort(err: unknown): boolean {
    return err instanceof Error && err.name === "AbortError";
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function readImportState<R>(url: string, signal: AbortSignal): Promise<ImportState<R> | null> {
    let res: Response;
    try {
        res = await fetchResponse(url, { signal });
    } catch (err) {
        if (isAbort(err)) throw err;
        return null;
    }
    if (res.status === 404) throw new Error("This import is no longer being tracked.");
    if (!res.ok) return null;
    try {
        return (await res.json()) as ImportState<R>;
    } catch {
        return null;
    }
}

function initImportWizard(dialog: HTMLElement): void {
    dialog.dataset.wizardReady = "1";
    const cfg = dialog.dataset;
    const acceptsMedia = cfg.acceptsMedia === "true";
    const canUploadVideos = cfg.canUploadVideos === "true";
    const titles: Record<StepName, string> = {
        upload: cfg.importTitle ?? "",
        parsing: cfg.importTitle ?? "",
        preview: cfg.reviewTitle ?? "",
        progress: "Importing...",
    };

    let parsedData: PreviewData | null = null;
    let selectedFiles: File[] = [];
    let abortCtrl: AbortController | null = null;
    let importing = false;
    let needsConfirm = false;
    let allSelected = true;
    let importJob: ImportJob | null = null;
    let openDropdown: HTMLElement | null = null;
    const logRows: HTMLElement[] = [];

    const $ = required;
    const steps: Record<StepName, HTMLElement> = {
        upload: $("iw-step-1"),
        parsing: $("iw-step-parsing"),
        preview: $("iw-step-2"),
        progress: $("iw-step-3"),
    };
    const dots = [$("iw-dot-1"), $("iw-dot-2"), $("iw-dot-3")];
    const closeButton = $<HTMLButtonElement>("closeImportDialog");

    function mediaKind(file: File): MediaKind {
        if (!acceptsMedia) return null;
        if ((file.type || "").startsWith("image/") || IMAGE_EXT_RE.test(file.name)) return "image";
        if ((file.type || "").startsWith("video/") || VIDEO_EXT_RE.test(file.name)) return "video";
        return null;
    }

    function showStep(name: StepName, dotIdx: number): void {
        Object.values(steps).forEach((el) => {
            el.hidden = true;
        });
        steps[name].hidden = false;
        dots.forEach((d, i) => {
            d.className = "iw-dot" + (i < dotIdx ? " done" : i === dotIdx ? " active" : "");
        });
        $("iw-title").textContent = titles[name];
    }

    function closeDialog(): void {
        if (importing) {
            if (!confirm("Import is still in progress. Leave and stop importing?")) return;
            if (importJob?.cancel_url) {
                void fetch(importJob.cancel_url, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, keepalive: true });
                importJob = null;
            }
            abortCtrl?.abort();
            abortCtrl = null;
            importing = false;
        } else if (needsConfirm) {
            if (!confirm("Leave the import wizard? Your progress will be lost.")) return;
        }
        abortCtrl?.abort();
        abortCtrl = null;
        document.getElementById("import-screen")?.remove();
        dialog.remove();
        window.removeEventListener("beforeunload", beforeUnloadHandler);
    }

    function beforeUnloadHandler(e: BeforeUnloadEvent): void {
        if (importing || needsConfirm) {
            e.preventDefault();
            e.returnValue = "";
        }
    }
    window.addEventListener("beforeunload", beforeUnloadHandler);

    $("import-screen").addEventListener("click", closeDialog);
    closeButton.addEventListener("click", closeDialog);

    $("iw-takeout-help-toggle").addEventListener("click", () => {
        const help = $("iw-takeout-help");
        const expanded = !help.hidden;
        help.hidden = expanded;
        $("iw-takeout-help-toggle").setAttribute("aria-expanded", String(!expanded));
    });

    const fileInput = $<HTMLInputElement>("iw-file-input");
    const fileZone = $("iw-file-zone");

    $("iw-browse-label").addEventListener("click", () => fileInput.click());
    fileZone.addEventListener("click", (e) => {
        const target = e.target;
        if (target === fileZone || (target instanceof Element && target.classList.contains("iw-file-zone-label"))) fileInput.click();
    });
    fileInput.addEventListener("change", () => handleFiles(Array.from(fileInput.files ?? [])));
    fileZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        fileZone.classList.add("drag-over");
    });
    fileZone.addEventListener("dragleave", () => fileZone.classList.remove("drag-over"));
    fileZone.addEventListener("drop", (e) => {
        e.preventDefault();
        fileZone.classList.remove("drag-over");
        handleFiles(Array.from(e.dataTransfer?.files ?? []));
    });

    function handleFiles(newFiles: File[]): void {
        newFiles.forEach((f) => {
            if (mediaKind(f) === "video" && !canUploadVideos) {
                toastr.warning(f.name + ": video uploads are not enabled for your account.", "Skipped");
                return;
            }
            if (!selectedFiles.find((x) => x.name === f.name && x.size === f.size)) selectedFiles.push(f);
        });
        renderFileChips();
    }

    function renderFileChips(): void {
        const container = $("iw-selected-files");
        container.innerHTML = "";
        selectedFiles.forEach((f, idx) => {
            const kind = mediaKind(f);
            const chip = document.createElement("div");
            chip.className = "iw-file-chip" + (kind ? " iw-media-chip" : "");
            const icon = kind === "image" ? "photo" : kind === "video" ? "videocam" : "insert_drive_file";
            chip.innerHTML =
                '<i class="material-symbols-outlined">' + icon + "</i>" +
                '<span class="iw-file-chip-name">' + escHtml(f.name) + (kind ? " <em>(added to your library)</em>" : "") + "</span>" +
                '<button class="iw-file-chip-remove" data-idx="' + escHtml(idx) + '" title="Remove">x</button>';
            container.appendChild(chip);
        });
        container.querySelectorAll<HTMLButtonElement>(".iw-file-chip-remove").forEach((btn) => {
            btn.addEventListener("click", () => {
                selectedFiles.splice(parseInt(btn.dataset.idx ?? "", 10), 1);
                renderFileChips();
            });
        });
        $<HTMLButtonElement>("iw-btn-upload").disabled = selectedFiles.length === 0;
    }

    // Uploaded through the same endpoint as Vault > Photos; the GPS visit matching files them against pins later.
    function uploadMediaFiles(files: File[]): void {
        const progress = $("iw-media-progress");
        const bar = $("iw-media-bar");
        const label = $("iw-media-progress-label");
        progress.hidden = false;
        const total = files.length;
        let done = 0;
        let ok = 0;
        label.textContent = "Uploading 0 of " + total + "...";

        files.forEach((file) => {
            const fd = new FormData();
            fd.append("image", file);
            fd.append("csrfmiddlewaretoken", getCsrfToken());
            fetchResponse(cfg.mediaUploadUrl ?? "", { method: "POST", body: fd, headers: { "X-CSRFToken": getCsrfToken() } })
                .then((r) =>
                    r
                        .json()
                        .catch(() => ({}))
                        .then((data: { error?: string }) => {
                            if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
                            return data;
                        }),
                )
                .then(() => {
                    ok++;
                })
                .catch((err: unknown) => {
                    toastr.error(file.name + ": " + errorMessage(err, "upload failed"));
                })
                .finally(() => {
                    done++;
                    bar.style.width = Math.round((done / total) * 100) + "%";
                    label.textContent = "Uploading " + done + " of " + total + "...";
                    if (done === total) {
                        label.textContent = ok + " of " + total + " added to your library.";
                        setTimeout(() => {
                            progress.hidden = true;
                            bar.style.width = "0";
                        }, 1200);
                        if (ok) toastr.success(ok + (ok === 1 ? " photo/video" : " photos/videos") + " added to your library.");
                    }
                });
        });
    }

    $("iw-btn-upload").addEventListener("click", async () => {
        if (selectedFiles.length === 0) return;

        const mediaFiles = selectedFiles.filter((f) => mediaKind(f) !== null);
        const locationFiles = selectedFiles.filter((f) => mediaKind(f) === null);

        if (mediaFiles.length > 0) uploadMediaFiles(mediaFiles);
        selectedFiles = locationFiles;
        renderFileChips();

        if (locationFiles.length === 0) return;

        showStep("parsing", 0);
        closeButton.disabled = true;

        const hasDocument = locationFiles.some((f) => /\.(txt|docx)$/i.test(f.name));
        $("iw-parsing-label").textContent = hasDocument ? "Reading files... AI is analyzing your document, this may take a moment." : "Reading files...";

        const formData = new FormData();
        formData.append("csrfmiddlewaretoken", getCsrfToken());
        locationFiles.forEach((f) => formData.append("upload_files", f));

        const ctrl = new AbortController();
        abortCtrl = ctrl;
        try {
            const res = await fetchResponse(cfg.previewUrl ?? "", { method: "POST", body: formData, signal: ctrl.signal });
            if (!res.ok) {
                if (res.status === 413) {
                    throw new Error("That upload is too large. Try removing some files or splitting them into smaller batches.");
                }
                const err = (await res.json().catch(() => ({}))) as { error?: string };
                throw new Error(err.error || "Server error " + res.status);
            }
            const job = (await res.json()) as ImportJob;
            closeButton.disabled = false;
            const data = await followPreview(job, ctrl.signal);
            if (!data) return;
            data.preview_id = job.job_id;
            parsedData = data;
            needsConfirm = true;
            closeButton.disabled = false;
            renderPreview(data);
            showStep("preview", 1);
            // Neutral title: per-file parse failures and notices about the preview itself (the pin cap) share it.
            (data.warnings || []).forEach((w) => toastr.warning(w, "Import warning"));
        } catch (err) {
            if (isAbort(err)) return;
            closeButton.disabled = false;
            showStep("upload", 0);
            toastr.error(errorMessage(err, "Could not read files"), "Could not read files");
        }
    });

    async function followPreview(job: ImportJob, signal: AbortSignal): Promise<PreviewData | null> {
        let failures = 0;
        for (;;) {
            await sleep(IMPORT_POLL_MS);
            if (signal.aborted) return null;
            const state = await readImportState<PreviewData>(job.status_url, signal);
            if (state === null) {
                failures += 1;
                if (failures >= IMPORT_POLL_GIVE_UP_AFTER) throw new Error("Lost track of reading your files. Please try again.");
                continue;
            }
            failures = 0;
            if (state.status === "done") return state.result ?? null;
            if (state.status === "error") throw new Error(state.message || "Could not read files");
        }
    }

    function renderPreview(data: PreviewData): void {
        const lists = data.lists;
        const labels = data.labels || [];
        allSelected = true;
        let alreadyOnMapCount = 0;

        const totalLabel =
            lists.length === 0
                ? "No pins"
                : data.total + " pin" + (data.total !== 1 ? "s" : "") + " across " + lists.length + " list" + (lists.length !== 1 ? "s" : "");

        const container = $("iw-lists-container");
        container.innerHTML = "";
        renderHistorySummary(container, data.history_summary || []);

        lists.forEach((lst, listIdx) => {
            const group = document.createElement("div");
            group.className = "iw-list-group";
            group.dataset.listIdx = String(listIdx);

            const header = document.createElement("div");
            header.className = "iw-list-header";
            header.innerHTML =
                '<i class="material-icons iw-list-toggle" id="iw-toggle-' + escHtml(listIdx) + '">expand_more</i>' +
                '<span class="iw-list-name">' + escHtml(lst.stem) + "</span>" +
                '<span class="iw-list-count" id="iw-list-count-' + escHtml(listIdx) + '">' + escHtml(lst.pins.length) + " pins</span>";
            group.appendChild(header);

            const meta = document.createElement("div");
            meta.className = "iw-list-meta";

            const catRow = document.createElement("div");
            catRow.className = "iw-meta-row";
            const catCheckbox = document.createElement("input");
            catCheckbox.type = "checkbox";
            catCheckbox.id = "iw-cat-" + listIdx;
            catCheckbox.dataset.list = String(listIdx);
            catCheckbox.checked = true;
            const catLabel = document.createElement("label");
            catLabel.append(catCheckbox, "Create category ");
            const catName = document.createElement("em");
            catName.textContent = '"' + lst.stem + '"';
            catLabel.appendChild(catName);
            catRow.appendChild(catLabel);
            meta.appendChild(catRow);

            // Always present so the new-category preview chip has somewhere to live.
            const labelRow = document.createElement("div");
            labelRow.className = "iw-meta-row";
            const labelInner = document.createElement("div");
            labelInner.className = "iw-label-row";
            labelInner.id = "iw-labels-" + listIdx;
            labelInner.innerHTML = '<span class="iw-label-row-text">Labels:</span>';

            const catPreview = document.createElement("span");
            catPreview.className = "iw-label-pill iw-label-pill-new";
            catPreview.title = "This category will be created when you import";
            catPreview.innerHTML = '<i class="material-icons">fiber_new</i><span>' + escHtml(lst.stem) + "</span>";
            catPreview.hidden = !catCheckbox.checked;
            labelInner.appendChild(catPreview);
            catCheckbox.addEventListener("change", () => {
                catPreview.hidden = !catCheckbox.checked;
            });

            if (labels.length > 0) {
                const addBtn = document.createElement("button");
                addBtn.className = "iw-label-add";
                addBtn.type = "button";
                addBtn.dataset.listIdx = String(listIdx);
                addBtn.innerHTML = '<i class="material-icons" style="font-size:.9rem">add</i> Add label';
                labelInner.appendChild(addBtn);
                addBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    openLabelDropdown(listIdx, labels, addBtn);
                });
            }

            labelRow.appendChild(labelInner);
            meta.appendChild(labelRow);
            lst._selectedLabelIds = [];
            group.appendChild(meta);

            const pinList = document.createElement("ul");
            pinList.className = "iw-pin-list";
            pinList.id = "iw-pins-" + listIdx;
            lst.pins.forEach((pin, pinIdx) => {
                // findLocalPinNear exists only on the main map page. A pin flagged needs_repair (TEMPORARY, legacy CID
                // coordinate repair - remove with services.apis.locations.legacy_cid_coordinate_fix) stays selected so
                // it reaches the server-side repair.
                const localMatch = !pin.needs_repair && window.findLocalPinNear ? window.findLocalPinNear(pin.lat, pin.lng) : null;
                pin._selected = !localMatch;
                if (localMatch) alreadyOnMapCount++;

                const row = document.createElement("li");
                row.className = "iw-pin-row" + (localMatch ? " deselected" : "");
                row.dataset.listIdx = String(listIdx);
                row.dataset.pinIdx = String(pinIdx);
                const cbId = escHtml("iw-pin-" + listIdx + "-" + pinIdx);
                row.innerHTML =
                    '<input type="checkbox" id="' + cbId + '"' + (localMatch ? "" : " checked") + ">" +
                    '<label for="' + cbId + '" class="iw-pin-name" title="' + escHtml(pin.name || "(unnamed)") + '">' +
                    escHtml(pin.name || "(unnamed)") + "</label>" +
                    (localMatch ? '<span class="iw-pin-existing-badge" title="A pin within 50m of these coordinates is already on your map">Already on your map</span>' : "");
                const cb = row.querySelector<HTMLInputElement>("input[type=checkbox]")!;
                cb.addEventListener("change", () => {
                    pin._selected = cb.checked;
                    row.classList.toggle("deselected", !cb.checked);
                    updateListCount(listIdx);
                    updateSelectedCount();
                });
                pinList.appendChild(row);
            });
            group.appendChild(pinList);
            updateListCount(listIdx);

            const toggleIcon = header.querySelector(".iw-list-toggle")!;
            header.addEventListener("click", (e) => {
                if (e.target instanceof Element && e.target.closest(".iw-label-add")) return;
                const collapsed = pinList.hidden;
                pinList.hidden = !collapsed;
                toggleIcon.classList.toggle("collapsed", !collapsed);
            });

            container.appendChild(group);
        });

        $("iw-preview-total-label").textContent =
            alreadyOnMapCount > 0 ? totalLabel + " · " + alreadyOnMapCount + " already on your map (deselected)" : totalLabel;
        updateSelectedCount();
    }

    // The lines are composed server-side.
    function renderHistorySummary(container: HTMLElement, lines: string[]): void {
        if (lines.length === 0) return;
        const group = document.createElement("div");
        group.className = "iw-list-group";
        const header = document.createElement("div");
        header.className = "iw-list-header";
        const label = document.createElement("label");
        label.className = "iw-list-name";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.id = "iw-history";
        checkbox.checked = true;
        checkbox.addEventListener("change", updateSelectedCount);
        label.appendChild(checkbox);
        label.appendChild(document.createTextNode(" Routes & history"));
        header.appendChild(label);
        group.appendChild(header);
        const list = document.createElement("ul");
        list.className = "iw-list-meta";
        lines.forEach((line) => {
            const item = document.createElement("li");
            item.textContent = line;
            list.appendChild(item);
        });
        group.appendChild(list);
        container.appendChild(group);
    }

    function historySelected(): boolean {
        const checkbox = document.getElementById("iw-history");
        return checkbox instanceof HTMLInputElement && checkbox.checked;
    }

    function openLabelDropdown(listIdx: number, labels: PreviewLabel[], anchor: HTMLElement): void {
        closeOpenDropdown();
        const lst = parsedData?.lists[listIdx];
        if (!lst) return;
        const dropdown = document.createElement("div");
        dropdown.className = "iw-label-dropdown";
        dropdown.id = "iw-bdrop-" + listIdx;

        labels.forEach((b) => {
            const selected = lst._selectedLabelIds.includes(b.id);
            const item = document.createElement("div");
            item.className = "iw-label-dropdown-item" + (selected ? " checked" : "");
            const iconHtml = b.icon
                ? '<i class="material-icons" style="font-size:1rem' + (b.color ? ";color:" + escHtml(b.color) : "") + '">' + escHtml(b.icon) + "</i>"
                : '<i class="material-icons" style="font-size:1rem;opacity:.3">label</i>';
            item.innerHTML = iconHtml + "<span>" + escHtml(b.name) + "</span>" + (selected ? '<i class="material-icons iw-label-dropdown-check">check</i>' : "");
            item.addEventListener("click", () => {
                toggleLabelOnList(listIdx, b);
                dropdown.remove();
                openDropdown = null;
            });
            dropdown.appendChild(item);
        });

        document.body.appendChild(dropdown);
        openDropdown = dropdown;

        const rect = anchor.getBoundingClientRect();
        dropdown.style.position = "fixed";
        dropdown.style.top = rect.bottom + 4 + "px";
        dropdown.style.left = rect.left + "px";

        setTimeout(() => document.addEventListener("click", closeOpenDropdown, { once: true }), 0);
    }

    function closeOpenDropdown(): void {
        openDropdown?.remove();
        openDropdown = null;
    }

    function toggleLabelOnList(listIdx: number, label: PreviewLabel): void {
        const lst = parsedData?.lists[listIdx];
        if (!lst) return;
        const ids = lst._selectedLabelIds;
        const labelRow = $("iw-labels-" + listIdx);
        const existIdx = ids.indexOf(label.id);
        if (existIdx >= 0) {
            ids.splice(existIdx, 1);
            labelRow.querySelector('[data-label-id="' + label.id + '"]')?.remove();
            return;
        }
        ids.push(label.id);
        const pill = document.createElement("span");
        pill.className = "iw-label-pill selected";
        pill.dataset.labelId = String(label.id);
        pill.title = "Click to remove";
        if (label.color) pill.style.setProperty("color", label.color);
        pill.innerHTML = (label.icon ? '<i class="material-icons" style="font-size:.85rem">' + escHtml(label.icon) + "</i>" : "") + "<span>" + escHtml(label.name) + "</span>";
        pill.addEventListener("click", () => toggleLabelOnList(listIdx, label));
        labelRow.insertBefore(pill, labelRow.querySelector(".iw-label-add"));
    }

    $("iw-toggle-all").addEventListener("click", () => {
        if (!parsedData) return;
        allSelected = !allSelected;
        $("iw-toggle-all").textContent = allSelected ? "Deselect all" : "Select all";
        parsedData.lists.forEach((lst, idx) => {
            lst.pins.forEach((pin) => {
                pin._selected = allSelected;
            });
            updateListCount(idx);
        });
        dialog.querySelectorAll(".iw-pin-row").forEach((row) => {
            const cb = row.querySelector<HTMLInputElement>("input[type=checkbox]");
            if (cb) cb.checked = allSelected;
            row.classList.toggle("deselected", !allSelected);
        });
        updateSelectedCount();
    });

    function updateListCount(listIdx: number): void {
        const lst = parsedData?.lists[listIdx];
        const el = document.getElementById("iw-list-count-" + listIdx);
        if (!lst || !el) return;
        const total = lst.pins.length;
        const selected = lst.pins.filter((p) => p._selected).length;
        el.textContent = selected === total ? total + " pin" + (total !== 1 ? "s" : "") : selected + " / " + total + " pins";
    }

    function updateSelectedCount(): void {
        let n = 0;
        parsedData?.lists.forEach((lst) => {
            n += lst.pins.filter((p) => p._selected).length;
        });
        $("iw-selected-count").textContent = n + " pin" + (n !== 1 ? "s" : "") + " selected" + (historySelected() ? " + routes & history" : "");
        $<HTMLButtonElement>("iw-btn-confirm").disabled = n === 0 && !historySelected();
    }

    $("iw-btn-back").addEventListener("click", () => {
        needsConfirm = false;
        showStep("upload", 0);
    });

    $("iw-btn-confirm").addEventListener("click", async () => {
        if (!parsedData) return;

        const payload = {
            auto_tag: $<HTMLInputElement>("iw-auto-tag").checked,
            preview_id: historySelected() ? parsedData.preview_id : null,
            lists: parsedData.lists
                .map((lst, idx) => {
                    const catEl = document.getElementById("iw-cat-" + idx);
                    return {
                        stem: lst.stem,
                        create_category: catEl instanceof HTMLInputElement ? catEl.checked : false,
                        label_ids: lst._selectedLabelIds || [],
                        pins: lst.pins
                            .filter((p) => p._selected)
                            .map((p) => ({ name: p.name, lat: p.lat, lng: p.lng, description: p.description, cid: p.cid, maps_url: p.maps_url, label_ids: [] })),
                    };
                })
                .filter((l) => l.pins.length > 0),
        };

        if (payload.lists.length === 0 && !payload.preview_id) {
            toastr.warning("Nothing selected.");
            return;
        }

        showStep("progress", 2);
        closeButton.disabled = true;
        importing = true;
        const ctrl = new AbortController();
        abortCtrl = ctrl;

        try {
            const res = await fetchResponse(cfg.confirmUrl ?? "", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify(payload),
                signal: ctrl.signal,
            });
            const job = (await res.json().catch(() => ({}))) as ImportJob;
            if (!res.ok) throw new Error(job.error || "Server returned " + res.status);

            importJob = job;
            handleProgressEvent({ type: "start", total: job.total });
            await followImport(job, ctrl.signal);
        } catch (err) {
            if (isAbort(err)) return;
            importing = false;
            closeButton.disabled = false;
            toastr.error(errorMessage(err, "Import failed"), "Import failed");
        }
    });

    async function followImport(job: ImportJob, signal: AbortSignal): Promise<void> {
        let shown = 0;
        let failures = 0;
        for (;;) {
            await sleep(IMPORT_POLL_MS);
            if (signal.aborted) return;
            const state = await readImportState<ImportResult>(job.status_url, signal);
            if (state === null) {
                failures += 1;
                if (failures >= IMPORT_POLL_GIVE_UP_AFTER) throw new Error("Lost track of the import. It may still finish - check your map shortly.");
                continue;
            }
            failures = 0;
            const r = state.result || {};
            // The pins' own names show while they import; before and after them, the phase the server is in.
            const pinsRunning = r.current && r.current !== r.total;
            if (state.status === "pending" || (state.status === "running" && !pinsRunning)) $("iw-current-name").textContent = state.message || "";
            if (r.current && r.current !== shown) {
                shown = r.current;
                handleProgressEvent({
                    type: "progress",
                    current: r.current,
                    total: r.total,
                    percent: state.progress,
                    created: r.created,
                    exists: r.exists,
                    skipped: r.skipped,
                    outcome: r.outcome,
                    name: r.name,
                });
            }
            if (state.status === "done") {
                importJob = null;
                handleProgressEvent({ type: "complete", total: r.total, created: r.created, exists: r.exists, skipped: r.skipped, deferred: r.deferred });
                if (r.deferred) handleProgressEvent({ type: "deferred", count: r.deferred });
                if (r.history) toastr.success(r.history, "Routes & history", { timeOut: 8000 });
                (r.notices || []).forEach((notice) => toastr.warning(notice, "Routes & history"));
                return;
            }
            if (state.status === "error" || state.status === "cancelled") {
                importJob = null;
                handleProgressEvent({ type: "error", message: state.message });
                return;
            }
        }
    }

    function setCounts(created: number | undefined, exists: number | undefined, skipped: number | undefined): void {
        $("iw-n-created").textContent = String(created ?? "");
        $("iw-n-exists").textContent = String(exists ?? "");
        $("iw-n-skipped").textContent = String(skipped ?? "");
    }

    function handleProgressEvent(evt: ProgressEvent): void {
        switch (evt.type) {
            case "start":
                $("iw-count").textContent = "0 of " + evt.total;
                break;

            case "progress":
                $("iw-bar").style.width = evt.percent + "%";
                $("iw-pct").textContent = evt.percent + "%";
                $("iw-count").textContent = evt.current + " of " + evt.total;
                $("iw-current-name").textContent = evt.name || "";
                setCounts(evt.created, evt.exists, evt.skipped);
                // Each row carries its own event's outcome, so finalising the previous row never reads another pin's.
                addLogRow(evt.name, evt.outcome);
                if (logRows.length > 1) finaliseLogRow(logRows[logRows.length - 2]!);
                break;

            case "complete": {
                $("iw-bar").style.width = "100%";
                $("iw-pct").textContent = "100%";
                $("iw-count").textContent = "Done";
                $("iw-current-name").textContent = "";
                setCounts(evt.created, evt.exists, evt.skipped);
                if (logRows.length > 0) finaliseLogRow(logRows[logRows.length - 1]!);
                const heading = $("iw-progress-header");
                heading.textContent = "Import complete";
                heading.style.color = "#43a047";
                importing = false;
                needsConfirm = false;
                closeButton.disabled = false;
                $("iw-btn-done").hidden = false;
                dialog.querySelector<HTMLElement>(".iw-step-dots")?.style.setProperty("display", "none");
                toastr.success(
                    evt.created + " created · " + evt.exists + " existed · " + evt.skipped + " skipped" + (evt.deferred ? " · " + evt.deferred + " fetching precise location..." : ""),
                    "Import complete",
                    { timeOut: 8000 },
                );
                if (evt.created) {
                    // The map page refreshes its pin store; any other page flags the cache for the next map visit.
                    if (window.forceRefreshPinCache) {
                        void window.forceRefreshPinCache();
                    } else {
                        try {
                            localStorage.setItem("ul_pins_dirty", "1");
                        } catch {
                            // Storage unavailable: the map reloads its pins on its own schedule.
                        }
                    }
                }
                break;
            }

            case "deferred":
                $("iw-deferred-note").hidden = false;
                $("iw-deferred-note-text").textContent =
                    evt.count + " pin" + (evt.count !== 1 ? "s" : "") + " need" + (evt.count === 1 ? "s" : "") +
                    " extra data from Google to place accurately - this can take a while. We’ll notify you " +
                    "when they’re ready. You can close this dialog and keep using the site in the meantime.";
                break;

            case "error":
                importing = false;
                closeButton.disabled = false;
                toastr.error(evt.message ?? "", "Import error");
                break;
        }
    }

    function addLogRow(name: string | undefined, outcome: string | undefined): void {
        const log = $("iw-item-log");
        const row = document.createElement("div");
        row.className = "iw-log-row";
        row.dataset.outcome = outcome || "";
        row.innerHTML =
            '<i class="material-icons iw-log-icon spin">sync</i>' +
            '<span class="iw-log-name">' + escHtml(name || "...") + "</span>" +
            '<span class="iw-log-status"></span>';
        log.appendChild(row);
        logRows.push(row);
        if (logRows.length > MAX_LOG) logRows.shift()?.remove();
        log.scrollTop = log.scrollHeight;
    }

    function finaliseLogRow(row: HTMLElement): void {
        const icon = row.querySelector(".iw-log-icon");
        const statusEl = row.querySelector(".iw-log-status");
        const [cls, mat, lbl] =
            row.dataset.outcome === "created"
                ? ["created", "check_circle", "new"]
                : row.dataset.outcome === "exists"
                  ? ["existed", "remove_circle_outline", "existed"]
                  : row.dataset.outcome === "deferred"
                    ? ["deferred", "cloud_sync", "fetching..."]
                    : ["skipped", "cancel", "skipped"];
        if (icon) {
            icon.className = "material-icons iw-log-icon " + cls;
            icon.textContent = mat;
        }
        if (statusEl) statusEl.textContent = lbl;
    }

    $("iw-btn-done").addEventListener("click", closeDialog);

    showStep("upload", 0);
}

function initAll(): void {
    document.querySelectorAll<HTMLElement>("#import-dialog:not([data-wizard-ready])").forEach(initImportWizard);
}

initAll();
document.addEventListener("htmx:load", initAll);
