/**
 * The safety check-in pages' smaller pieces: growing textareas, the grace-period slider, the create form's
 * timing, delete buttons, live status and location updates, the archive countdown and unlock, and the
 * reference-map "New map" button.
 */

import { confirmAction } from "./dialogs";
import { fetchJson, fetchText } from "./fetch-json";
import { getCsrfToken } from "./csrf";
import { e2eeUrlsFromDataset } from "./e2ee-urls";

declare const L: typeof import("leaflet") | undefined;

const HOUR_MS = 3600000;
const ARCHIVE_RELOAD_DELAY_MS = 900;
const NEW_MAP_ZOOM = 13;

let leavingAllowed = false;

/** Stop warning about leaving the page, e.g. once the check-in is resolved. */
export function allowLeaving(): void {
    leavingAllowed = true;
}

export function isLeavingAllowed(): boolean {
    return leavingAllowed;
}

export function resetLeavingForTests(): void {
    leavingAllowed = false;
}

// -- Form widgets

/** Fit a textarea to its content. It reads 0 while hidden, so call it again once shown. */
export function autoExpand(el: HTMLTextAreaElement): void {
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
}

export function initAutoExpand(scope: ParentNode = document): void {
    scope.querySelectorAll<HTMLTextAreaElement>("textarea.safety-autoexpand").forEach((el) => {
        if (el.dataset.autoexpandInit) return;
        el.dataset.autoexpandInit = "1";
        autoExpand(el);
        el.addEventListener("input", () => autoExpand(el));
    });
}

/** "1 hour", "2.5 hours". */
export function formatGraceHours(value: string): string {
    const hours = Math.round(Number.parseFloat(value) * 100) / 100;
    return `${hours % 1 === 0 ? hours.toFixed(0) : String(hours)} hour${hours === 1 ? "" : "s"}`;
}

export function initGraceSliders(scope: ParentNode = document): void {
    scope.querySelectorAll<HTMLElement>('.safety-grace-slider[data-role="grace-slider"]').forEach((root) => {
        if (root.dataset.gracePickerInit) return;
        const range = root.querySelector('[data-role="range"]');
        const output = root.querySelector('[data-role="output"]');
        if (!(range instanceof HTMLInputElement) || !output) return;
        root.dataset.gracePickerInit = "1";
        const refresh = (): void => {
            output.textContent = formatGraceHours(range.value);
        };
        range.addEventListener("input", refresh);
        refresh();
    });
}

/** A ``datetime-local`` value for *date* on the user's own clock. */
export function toLocalInputValue(date: Date): string {
    const pad = (n: number): string => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * The create form's expected check-in time. The picker has no time zone and the server reads a naive time as
 * UTC, so the picked time is mirrored into a hidden UTC field.
 */
export function installCheckinTiming(form: HTMLFormElement, picker: HTMLInputElement, utc: HTMLInputElement): void {
    const syncUtc = (): void => {
        const picked = new Date(picker.value);
        utc.value = Number.isNaN(picked.getTime()) ? "" : picked.toISOString();
    };
    const now = new Date();
    picker.min = toLocalInputValue(now);
    // An hour out: "now" would already be past by the time the page has loaded.
    if (!picker.value) picker.value = toLocalInputValue(new Date(now.getTime() + HOUR_MS));
    syncUtc();

    const grace = form.querySelector('.safety-grace-slider [data-role="range"]');
    const endHint = document.getElementById("safety-grace-end-hint");
    const refreshEndHint = (): void => {
        if (!(grace instanceof HTMLInputElement) || !endHint || !picker.value) return;
        const checkinBy = new Date(picker.value);
        if (Number.isNaN(checkinBy.getTime())) return;
        const endsAt = new Date(checkinBy.getTime() + Number.parseFloat(grace.value) * HOUR_MS);
        endHint.textContent = `Contacts will be notified at ${endsAt.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
    };
    grace?.addEventListener("input", refreshEndHint);
    picker.addEventListener("input", () => {
        syncUtc();
        refreshEndHint();
        picker.setCustomValidity("");
    });
    refreshEndHint();

    form.addEventListener("submit", (event) => {
        const checkinBy = new Date(picker.value);
        if (Number.isNaN(checkinBy.getTime()) || checkinBy <= new Date()) {
            event.preventDefault();
            picker.setCustomValidity("Expected check-in time must be in the future.");
            picker.reportValidity();
        } else {
            picker.setCustomValidity("");
        }
    });
}

/** The safety defaults' "Never" auto-delete box: it blanks and disables the day count. */
export function installAutoDeleteNever(onChange: () => void): void {
    const days = document.getElementById("auto_delete_after_days");
    const never = document.getElementById("auto_delete_never");
    if (!(days instanceof HTMLInputElement) || !(never instanceof HTMLInputElement)) return;
    never.addEventListener("change", () => {
        days.disabled = never.checked;
        if (!never.checked && !days.value) days.value = "30";
        onChange();
    });
    days.addEventListener("input", () => {
        if (days.value && never.checked) never.checked = false;
    });
}

// -- Actions

async function deleteCheckin(btn: HTMLElement): Promise<void> {
    const ok = await confirmAction({
        title: "Delete this check-in?",
        message:
            btn.dataset.contactsNotified === "1"
                ? "Your emergency contacts were already notified about this trip - deleting won't un-notify them. This can't be undone."
                : "This can't be undone.",
        confirmLabel: "Delete",
    });
    if (!ok) return;
    const redirect = btn.dataset.redirectUrl;
    try {
        // The view answers with a redirect to the safety home page; only the status matters.
        await fetchText(btn.dataset.deleteUrl ?? "", { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, reportsItsOwnErrors: true });
    } catch {
        window.toastr?.error("Could not delete this check-in.");
        return;
    }
    if (redirect) {
        // Only once it is gone: a failed delete leaves the check-in, and its warning, in place.
        allowLeaving();
        window.autosaveGuard?.allowNavigation();
        window.location.href = redirect;
        return;
    }
    document.getElementById(btn.dataset.cardId ?? "")?.remove();
    window.toastr?.success("Check-in deleted.");
}

async function createReferenceMap(btn: HTMLElement): Promise<void> {
    const body: Record<string, number> = {};
    if (btn.dataset.lat) {
        body.center_lat = Number.parseFloat(btn.dataset.lat);
        body.center_lng = Number.parseFloat(btn.dataset.lng ?? "");
        body.zoom = NEW_MAP_ZOOM;
    }
    try {
        const data = await fetchJson<{ ok?: boolean; uuid?: string }>(btn.dataset.createUrl ?? "", {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/json" },
            body: JSON.stringify(body),
            reportsItsOwnErrors: true,
        });
        if (!data?.ok || !data.uuid) throw new Error("create failed");
        await window.htmx?.ajax("POST", btn.dataset.attachUrl ?? "", { target: "#safety-attached-maps", swap: "outerHTML", values: { map_uuid: data.uuid } });
    } catch {
        window.toastr?.error("Could not create a new map.");
    }
}

function onClick(event: MouseEvent): void {
    const target = event.target instanceof Element ? event.target : null;
    const del = target?.closest<HTMLElement>(".safety-checkin-delete-btn, #safety-delete-btn");
    if (del) {
        void deleteCheckin(del);
        return;
    }
    const newMap = target?.closest<HTMLElement>(".safety-new-map-btn");
    if (newMap) {
        void createReferenceMap(newMap);
        return;
    }
    const attach = target?.closest<HTMLElement>("[data-safety-map-picker-open]");
    if (attach) {
        const search = document.getElementById("safety-map-picker-search");
        if (search instanceof HTMLInputElement) search.value = "";
        const dialog = document.getElementById("safety-map-picker-dialog");
        if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
    }
}

async function onSubmit(event: SubmitEvent): Promise<void> {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-safety-resolve")) return;
    const question = form.dataset.confirm;
    if (question && !form.dataset.confirmed) {
        event.preventDefault();
        if (!(await confirmAction({ title: question, message: form.dataset.confirmMessage, confirmLabel: form.dataset.confirmLabel, cancelLabel: form.dataset.cancelLabel }))) return;
        form.dataset.confirmed = "1";
        allowLeaving();
        form.requestSubmit(event.submitter instanceof HTMLElement ? event.submitter : undefined);
        return;
    }
    allowLeaving();
}

// -- Live updates

interface StatusUpdate {
    status: string;
    status_display: string;
    is_resolved: boolean;
}

interface LocationUpdate {
    latitude: number | null;
    longitude: number | null;
    updated_at: string | null;
}

function detail<T>(event: Event): T | null {
    return event instanceof CustomEvent ? event.detail : null;
}

function onStatusUpdate(event: Event): void {
    const data = detail<StatusUpdate>(event);
    if (!data) return;
    const badge = document.getElementById("safety-status-badge");
    if (badge) {
        badge.className = `safety-checkin-status-badge safety-checkin-status-badge--${data.status}`;
        badge.textContent = data.status_display;
    }
    const actions = document.getElementById("safety-resolve-actions");
    if (actions) actions.hidden = !!data.is_resolved;
    if (data.is_resolved) allowLeaving();
}

function numberOrNull(raw: string | undefined): number | null {
    if (raw === undefined || raw === "") return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
}

/** The live position, on the map and in words; with none, the status goes back to its ``data-idle-text``. */
export function installLiveLocationMarker(card: HTMLElement): void {
    let marker: L.CircleMarker | null = null;
    // Unset, not just undefined, on a page without Leaflet.
    const leaflet = typeof L === "undefined" ? undefined : L;
    const show = (data: LocationUpdate): void => {
        const status = document.getElementById("safety-live-location-status");
        const map = leaflet?.Map && window.map instanceof leaflet.Map ? window.map : null;
        if (data.latitude === null || data.longitude === null) {
            if (marker && map) {
                map.removeLayer(marker);
                marker = null;
            }
            if (status) status.textContent = status.dataset.idleText ?? "";
            return;
        }
        if (map) {
            if (marker) marker.setLatLng([data.latitude, data.longitude]);
            else if (leaflet) marker = leaflet.circleMarker([data.latitude, data.longitude], { radius: 8, color: "#2563eb", fillColor: "#3b82f6", fillOpacity: 0.9 }).addTo(map);
        }
        if (status) {
            const updated = new Date(data.updated_at ?? "");
            const when = Number.isNaN(updated.getTime()) ? "just now" : updated.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
            status.textContent = `Live location updated ${when}.`;
        }
    };
    document.body.addEventListener("safetyLocationUpdate", (event) => {
        const data = detail<LocationUpdate>(event);
        if (data) show({ latitude: data.latitude ?? null, longitude: data.longitude ?? null, updated_at: data.updated_at ?? null });
    });
    // The last known position, so a phone that died mid-trip still shows where it was.
    show({ latitude: numberOrNull(card.dataset.liveLat), longitude: numberOrNull(card.dataset.liveLng), updated_at: card.dataset.liveUpdatedAt || null });
}

// -- Archive

function initArchiveCountdown(): void {
    const el = document.getElementById("safety-archive-countdown");
    const text = document.getElementById("safety-archive-countdown-text");
    if (!el || !text || el.dataset.countdownInit) return;
    el.dataset.countdownInit = "1";
    const tick = (): void => {
        const remaining = new Date(el.dataset.archiveAt ?? "").getTime() - Date.now();
        if (Number.isNaN(remaining) || remaining <= 0) {
            text.textContent = "shortly";
            return;
        }
        const minutes = Math.floor(remaining / 60000);
        const seconds = Math.floor((remaining % 60000) / 1000);
        text.textContent = `in ${minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`}`;
    };
    document.body.addEventListener("safetyArchiveScheduled", (event) => {
        const at = detail<{ archive_at?: string }>(event)?.archive_at;
        if (!at) return;
        el.dataset.archiveAt = at;
        tick();
    });
    tick();
    window.ulStartPoller?.(tick, { intervalMs: 1000, element: el });
}

export function initArchiveUnlock(): void {
    const btn = document.getElementById("safety-archive-unlock-btn");
    if (!(btn instanceof HTMLButtonElement) || btn.dataset.unlockInit) return;
    btn.dataset.unlockInit = "1";
    btn.addEventListener("click", async () => {
        const e2ee = window.UrbanLensE2EE;
        if (!e2ee) return;
        btn.disabled = true;
        let data: Record<string, unknown> | null;
        try {
            e2ee.init({ selfSlug: btn.dataset.selfSlug || null, loginIdentifier: btn.dataset.loginIdentifier ?? "", urls: e2eeUrlsFromDataset(btn.dataset) });
            data = await e2ee.decryptSafetyArchive(btn.dataset.sealedKey ?? "", btn.dataset.ciphertext ?? "", btn.dataset.nonce ?? "");
        } catch {
            window.toastr?.error("Could not unlock this check-in on this device.");
            return;
        } finally {
            btn.disabled = false;
        }
        if (!data) {
            window.toastr?.error("Could not unlock this check-in on this device - try a device that's already unlocked your messages.");
            return;
        }
        const text = (key: string, fallback: string): string => {
            const value = data?.[key];
            return typeof value === "string" && value ? value : fallback;
        };
        const set = (id: string, value: string): void => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        set("safety-archive-title", text("title", "(untitled)"));
        set("safety-archive-plan", text("plan_details", "(no plan recorded)"));
        set("safety-archive-resolved-by", text("resolved_by_label", "unknown"));
        const unlocked = document.getElementById("safety-archive-unlocked");
        if (unlocked) unlocked.hidden = false;
        btn.hidden = true;
    });
}

function onArchived(): void {
    document.querySelectorAll(".safety-page .card").forEach((card) => card.classList.add("safety-dissolving"));
    allowLeaving();
    window.setTimeout(() => window.location.reload(), ARCHIVE_RELOAD_DELAY_MS);
}

let installed = false;

/** Everything here that finds its own elements; safe on any safety page. */
export function installSafetyPage(): void {
    if (installed) return;
    installed = true;
    initAutoExpand();
    initGraceSliders();
    initArchiveCountdown();
    initArchiveUnlock();
    document.addEventListener("click", onClick);
    document.addEventListener("submit", (event) => void onSubmit(event));
    document.body.addEventListener("safetyStatusUpdate", onStatusUpdate);
    document.body.addEventListener("safetyCheckinArchived", onArchived);
}
