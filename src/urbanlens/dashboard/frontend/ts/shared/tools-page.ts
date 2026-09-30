/**
 * The Tools pages (``pages/tools/index.html``, ``admin.html``): the section tabs, the export's type checkboxes
 * and progress, the import's file picker, and the admin page's manual backup. The photo-location scan has its
 * own entry.
 */

import { getCsrfToken } from "./csrf";
import { htmxDetail } from "./htmx-events";
import { installLeaveConfirmation } from "./leave-confirmation";

const EXPORT_FAILED = "Export failed. Please try again.";

function byId<T extends HTMLElement>(id: string, type: new () => T): T | null {
    const el = document.getElementById(id);
    return el instanceof type ? el : null;
}

export function installSectionTabs(): void {
    const tabs = Array.from(document.querySelectorAll<HTMLElement>(".tools-section-tab"));
    const panels = Array.from(document.querySelectorAll<HTMLElement>(".tools-section-panel"));
    for (const tab of tabs) {
        tab.addEventListener("click", () => {
            const section = tab.dataset.section;
            if (!section) return;
            for (const t of tabs) t.classList.toggle("is-active", t === tab);
            for (const p of panels) p.hidden = p.id !== `panel-${section}`;
        });
    }
}

/** Keep "Select all" checked, unchecked or indeterminate to match the type boxes, and drive them. */
export function installExportSelectAll(): void {
    const selectAll = byId("export-select-all", HTMLInputElement);
    const boxes = Array.from(document.querySelectorAll<HTMLInputElement>("#export-data-checkboxes .export-type-cb"));
    if (!selectAll) return;
    const sync = (): void => {
        const checked = boxes.filter((cb) => cb.checked).length;
        selectAll.checked = checked === boxes.length;
        selectAll.indeterminate = checked > 0 && checked < boxes.length;
    };
    selectAll.addEventListener("change", () => {
        for (const cb of boxes) cb.checked = selectAll.checked;
        selectAll.indeterminate = false;
    });
    for (const cb of boxes) cb.addEventListener("change", sync);
}

/** While the export is still polling. */
function exportInProgress(): boolean {
    return !!document.getElementById("export-status-poll")?.hasAttribute("hx-get");
}

let lastErrorToast = "";

function notifyExportError(message: string): void {
    const text = (message || EXPORT_FAILED).trim();
    if (!text || text === lastErrorToast) return;
    lastErrorToast = text;
    window.toastr?.error(text, "Export failed");
}

/** Stop polling and show *message* where the progress was, with a way to start again. */
export function showExportError(message: string): void {
    const poll = document.getElementById("export-status-poll");
    if (!poll) return;
    for (const attr of ["hx-get", "hx-trigger", "hx-target", "hx-swap"]) poll.removeAttribute(attr);
    const text = message || EXPORT_FAILED;

    const track = document.createElement("div");
    track.className = "export-progress-track";
    const bar = document.createElement("div");
    bar.className = "export-progress-bar export-progress-bar--error";
    bar.style.width = "100%";
    track.append(bar);

    const error = document.createElement("div");
    error.className = "export-error-msg";
    const icon = document.createElement("i");
    icon.className = "material-symbols-outlined";
    icon.textContent = "error";
    const words = document.createElement("span");
    words.textContent = text;
    error.append(icon, words);

    const actions = document.createElement("div");
    actions.className = "export-done-actions";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "btn btn--secondary";
    retry.toggleAttribute("data-reload", true);
    const retryIcon = document.createElement("i");
    retryIcon.className = "material-symbols-outlined";
    retryIcon.textContent = "refresh";
    retry.append(retryIcon, " Try again");
    actions.append(retry);

    poll.replaceChildren(track, error, actions);
    notifyExportError(text);
}

/** What an error response said, as text. */
type ErrorResponse = Pick<XMLHttpRequest, "responseText" | "status">;

export function responseMessage(xhr: ErrorResponse | null | undefined): string {
    if (!xhr?.responseText) return "";
    return xhr.responseText
        .replace(/<[^>]*>/g, " ")
        .replace(/\s+/g, " ")
        .trim();
}

function statusMessage(xhr: ErrorResponse | null | undefined): string {
    if (xhr?.status === 403) return "Could not verify export ownership. Please start a new export.";
    if (xhr?.status === 404) return "Export job not found or expired. Please start a new export.";
    return responseMessage(xhr) || "Export status could not be checked. Please try again.";
}

export function installExportProgress(): void {
    installLeaveConfirmation({
        isBlocked: exportInProgress,
        message: "An export is still in progress. Leaving now will stop the export and you may lose your download.",
        confirmLabel: "Leave anyway",
    });
    // On document, like every other htmx listener here: htmx dispatches these from the element, bubbling.
    document.addEventListener("htmx:afterSwap", (event) => {
        const id = htmxDetail(event).target?.id;
        if (id === "export-status-area") {
            const form = document.getElementById("export-form-wrap");
            if (form) form.style.display = "none";
        }
        if (id === "export-status-area" || id === "export-status-poll") {
            const error = document.querySelector("#export-status-poll .export-error-msg span");
            if (error) notifyExportError(error.textContent ?? "");
        }
        if (id === "import-status-area") {
            const form = document.getElementById("import-form-wrap");
            if (form) form.style.display = "none";
        }
    });
    document.addEventListener("htmx:responseError", (event) => {
        const { elt, xhr } = htmxDetail(event);
        if (elt?.id === "export-status-poll") showExportError(statusMessage(xhr));
    });
    document.addEventListener("htmx:sendError", (event) => {
        if (htmxDetail(event).elt?.id === "export-status-poll") showExportError("Network error while checking export status. Please check your connection and try again.");
    });
}

/** The import's file box: pick or drop one file, see its name, clear it. */
export function installImportPicker(): void {
    const input = byId("import-file-input", HTMLInputElement);
    const chosen = document.getElementById("import-file-chosen");
    const name = document.getElementById("import-file-name");
    const submit = byId("import-submit-btn", HTMLButtonElement);
    const label = document.querySelector<HTMLElement>(".import-drop-label");
    const zone = document.getElementById("import-drop-zone");
    if (!input || !chosen || !name || !submit) return;

    const setFile = (file: File): void => {
        name.textContent = file.name;
        chosen.style.display = "flex";
        if (label) label.style.display = "none";
        submit.disabled = false;
    };
    const clearFile = (): void => {
        input.value = "";
        chosen.style.display = "none";
        if (label) label.style.display = "";
        submit.disabled = true;
    };

    input.addEventListener("change", () => {
        const file = input.files?.[0];
        if (file) setFile(file);
        else clearFile();
    });
    document.getElementById("import-file-clear")?.addEventListener("click", (event) => {
        // It sits inside the drop label; the click must not reopen the file chooser.
        event.preventDefault();
        event.stopPropagation();
        clearFile();
    });
    if (!zone) return;
    zone.addEventListener("dragover", (event) => {
        event.preventDefault();
        zone.classList.add("drag-over");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
    zone.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("drag-over");
        const file = event.dataTransfer?.files[0];
        if (!file) return;
        const transfer = new DataTransfer();
        transfer.items.add(file);
        input.files = transfer.files;
        setFile(file);
    });
}

/** The admin page's "Run Backup": queue one and say what the server said. */
export function installManualBackup(): void {
    const btn = byId("manual-backup-btn", HTMLButtonElement);
    const result = document.getElementById("manual-backup-result");
    if (!btn || !result) return;
    btn.addEventListener("click", async () => {
        btn.disabled = true;
        result.textContent = "Queueing backup...";
        try {
            const response = await fetch(btn.dataset.url ?? "", { method: "POST", headers: { "X-CSRFToken": getCsrfToken() } });
            const data: unknown = await response.json().catch(() => null);
            const message = data && typeof data === "object" && "message" in data && typeof data.message === "string" ? data.message : "";
            result.textContent = message || (response.status === 202 ? "Backup queued." : "Backup request failed.");
        } catch {
            result.textContent = "Backup request failed.";
        } finally {
            btn.disabled = false;
        }
    });
}
