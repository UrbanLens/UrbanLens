/**
 * A trip's page (pages/trips/detail.html): the activity map, the activities panel's tabs and calendar, the add
 * and edit activity dialogs, section ordering, in-place title editing, and the onboarding tour.
 */

import { byId, formControlById } from "../shared/dom";
import Sortable from "sortablejs";

import { toast } from "../shared/dialogs";
import { delegateEditInPlace, type EditInPlaceOptions } from "../shared/edit-in-place";
import { escHtml } from "../shared/escape-html";
import { initOnboardingTour } from "../shared/onboarding-tour";
import { activitiesForTab, calendarHtml, tripMonths, type CalendarActivity } from "../shared/trip-calendar";

declare const L: typeof import("leaflet");

declare global {
    interface Window {
        tripHighlightMarker?: (activityId: string, on: boolean) => void;
    }
}

type ActivityTab = "upcoming" | "proposed" | "confirmed" | "completed";

const TABS: readonly ActivityTab[] = ["upcoming", "proposed", "confirmed", "completed"];
const CONTINENT_ZOOM = 3;
/** Past this zoom a drag is easy to make by accident, so moving a marker asks first. */
const DRAG_CONFIRM_ZOOM = 14;
const NEW_YORK: [number, number] = [40.7128, -74.006];

interface TripConfig {
    tripUuid: string;
    editUrl: string;
    mapDataUrl: string;
    positionUrl: string;
    moveUrl: string;
    childTripSearchUrl: string;
    localUrl: string;
    placesUrl: string;
    resolveUrl: string;
    profileUuid: string;
    layersStorageKey: string;
    defaultBase: string;
    darkMode: "light" | "dark" | "system";
    centerMode: string;
    serverCenter: [number, number] | null;
    gpsFallback: [number, number] | null;
    showOnboarding: boolean;
}

interface MapPoint {
    index: number | null;
    activity_id: number | null;
    label: string;
    lat: number;
    lng: number;
    status: string;
    scheduled_at: string | null;
    draggable: boolean;
    child_trip?: boolean;
}

interface ChildTripResult {
    uuid: string;
    name: string;
    start_date?: string | null;
    end_date?: string | null;
}

interface PickerIds {
    uuid: string;
    display: string;
    lat: string;
    lng: string;
    name: string;
    pin: string;
    childTrip?: string;
}

const ADD_PICKER: PickerIds = {
    uuid: "activity-location-uuid",
    display: "activity-location-display",
    lat: "activity-geocoded-lat",
    lng: "activity-geocoded-lng",
    name: "activity-geocoded-name",
    pin: "activity-pin-uuid",
    childTrip: "activity-child-trip-uuid",
};

const EDIT_PICKER: PickerIds = {
    uuid: "edit-activity-location-uuid",
    display: "edit-activity-location-display",
    lat: "edit-activity-geocoded-lat",
    lng: "edit-activity-geocoded-lng",
    name: "edit-activity-geocoded-name",
    pin: "edit-activity-pin-uuid",
};

function setValue(id: string, value: string | number): void {
    const el = formControlById(id);
    if (el) el.value = String(value);
}

function setHidden(id: string, hidden: boolean): void {
    const el = byId(id, HTMLElement);
    if (el) el.hidden = hidden;
}

function point(lat: string | undefined, lng: string | undefined): [number, number] | null {
    const la = Number.parseFloat(lat ?? "");
    const ln = Number.parseFloat(lng ?? "");
    return Number.isNaN(la) || Number.isNaN(ln) ? null : [la, ln];
}

function readConfig(root: HTMLElement): TripConfig {
    const d = root.dataset;
    const dark = d.darkMode;
    return {
        tripUuid: d.tripUuid ?? "",
        editUrl: d.editUrl ?? "",
        mapDataUrl: d.mapDataUrl ?? "",
        positionUrl: d.positionUrl ?? "",
        moveUrl: d.moveUrl ?? "",
        childTripSearchUrl: d.childTripSearchUrl ?? "",
        localUrl: d.localUrl ?? "",
        placesUrl: d.placesUrl ?? "",
        resolveUrl: d.resolveUrl ?? "",
        profileUuid: d.profileUuid ?? "",
        layersStorageKey: d.layersStorageKey ?? "",
        defaultBase: d.defaultBase ?? "",
        darkMode: dark === "dark" || dark === "system" ? dark : "light",
        centerMode: d.centerMode || "gps",
        serverCenter: point(d.centerLat, d.centerLng),
        gpsFallback: point(d.gpsLat, d.gpsLng),
        showOnboarding: d.showOnboarding === "1",
    };
}

/** A URL reversed with activity id 0, pointed at another activity. */
function forActivity(template: string, activityId: string | number): string {
    return template.replace("/0/", `/${activityId}/`);
}

function readStorage(key: string): string | null {
    try {
        return localStorage.getItem(key);
    } catch {
        return null;
    }
}

function writeStorage(key: string, value: string): void {
    try {
        localStorage.setItem(key, value);
    } catch {
        // Storage unavailable: the preference lasts for this page only.
    }
}

function todayIso(): string {
    return new Date().toISOString().slice(0, 10);
}

/** A freshly revealed date field starts at today rather than blank. */
function defaultDateToToday(id: string): void {
    const input = byId(id, HTMLInputElement);
    if (input && !input.value) input.value = todayIso();
}

function activitiesPanel(): HTMLElement | null {
    return byId("trip-activities-panel", HTMLElement);
}

// -- Map ---------------------------------------------------------------------------------

interface TripMarker {
    marker: L.Marker;
    status: string;
}

class TripMap {
    private map: L.Map | null = null;
    private markersGroup: L.LayerGroup | null = null;
    private markers = new Map<string, TripMarker>();
    private ctxMenu: HTMLElement | null = null;
    private readonly pastKey: string;
    showPast: boolean;

    constructor(
        private readonly cfg: TripConfig,
        private readonly currentTab: () => ActivityTab,
        private readonly onAddAt: (lat: number, lng: number) => void,
    ) {
        this.pastKey = `trip-show-past-activities-${cfg.tripUuid}`;
        this.showPast = readStorage(this.pastKey) === "1";
    }

    private get mapEl(): HTMLElement | null {
        return byId("trip-map", HTMLElement);
    }

    highlightMarker(activityId: string, on: boolean): void {
        this.markers.get(String(activityId))?.marker.getElement()?.classList.toggle("trip-map-marker--highlighted", on);
    }

    /** Markers mirror the list's tab; completed ones follow the "Past Activities" layer instead. */
    applyTabFilter(): void {
        const tab = this.currentTab();
        this.markers.forEach(({ marker, status }) => {
            const visible = status === "completed" ? this.showPast : tab === "upcoming" || tab === status;
            const el = marker.getElement();
            if (el) el.style.display = visible ? "" : "none";
        });
    }

    private removeCtxMenu(): void {
        this.ctxMenu?.remove();
        this.ctxMenu = null;
    }

    private showCtxMenu(latlng: L.LatLng, at: L.Point): void {
        this.removeCtxMenu();
        const menu = document.createElement("div");
        menu.className = "trip-map-ctx-menu";
        menu.style.left = `${at.x}px`;
        menu.style.top = `${at.y}px`;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "trip-map-ctx-item";
        btn.innerHTML = '<i class="material-symbols-outlined">add_location</i> Add activity here';
        btn.addEventListener("click", () => {
            this.removeCtxMenu();
            this.onAddAt(latlng.lat, latlng.lng);
        });
        menu.appendChild(btn);
        byId("trip-map-wrap", HTMLElement)?.appendChild(menu);
        this.ctxMenu = menu;
    }

    private defaultCenter(): [number, number] {
        return this.cfg.serverCenter ?? this.cfg.gpsFallback ?? NEW_YORK;
    }

    private withCenter(callback: (lat: number, lng: number) => void): void {
        if (this.cfg.centerMode === "gps" && navigator.geolocation) {
            navigator.geolocation.getCurrentPosition(
                (pos) => callback(pos.coords.latitude, pos.coords.longitude),
                () => callback(...this.defaultCenter()),
                { timeout: 8000, maximumAge: 300000 },
            );
            return;
        }
        callback(...this.defaultCenter());
    }

    private init(mapEl: HTMLElement): L.Map {
        const map = L.map(mapEl, { attributionControl: false });
        // The shared toolbar's screenshot tool reads it.
        window.map = map;
        window.MapLayers.create(map, {
            root: byId("trip-map-layers", HTMLElement),
            defaultBase: this.cfg.defaultBase || null,
            darkMode: this.cfg.darkMode,
            storageKey: this.cfg.layersStorageKey || null,
            onAttribution: window.MapLayers.setAttribution,
            custom: {
                past_activities: {
                    isActive: () => this.showPast,
                    toggle: () => {
                        this.showPast = !this.showPast;
                        writeStorage(this.pastKey, this.showPast ? "1" : "0");
                        this.load();
                    },
                },
            },
        });
        this.markersGroup = L.layerGroup().addTo(map);

        map.on("contextmenu", (e: L.LeafletMouseEvent) => {
            L.DomEvent.preventDefault(e.originalEvent);
            this.showCtxMenu(e.latlng, map.latLngToContainerPoint(e.latlng));
        });
        map.on("click", () => this.removeCtxMenu());
        map.on("movestart", () => this.removeCtxMenu());
        document.addEventListener("keydown", (ev) => {
            if (ev.key === "Escape") this.removeCtxMenu();
        });
        // Layout may still be settling when the map is built.
        setTimeout(() => map.invalidateSize(), 100);
        this.map = map;
        return map;
    }

    private showEmpty(): void {
        const mapEl = this.mapEl;
        if (!mapEl) return;
        setHidden("trip-map-wrap", false);
        mapEl.hidden = false;
        setHidden("trip-map-empty", false);
        if (this.map) {
            this.markersGroup?.clearLayers();
            this.markers.clear();
        } else {
            this.init(mapEl);
        }
        this.withCenter((lat, lng) => this.map?.setView([lat, lng], CONTINENT_ZOOM));
    }

    private marker(pt: MapPoint): L.Marker {
        const isChildTrip = !!pt.child_trip;
        const className = `trip-map-marker${pt.status === "proposed" ? " trip-map-marker--proposed" : ""}${isChildTrip ? " trip-map-marker--child-trip" : ""}`;
        const numHtml =
            pt.index != null
                ? `<span class="trip-map-marker-num">${escHtml(pt.index)}</span>`
                : '<span class="trip-map-marker-num trip-map-marker-num--ghost"><i class="material-icons" style="font-size:13px;line-height:1">link</i></span>';
        const icon = L.divIcon({ className, html: numHtml, iconSize: [28, 28], iconAnchor: [14, 14], popupAnchor: [0, -16] });

        // Built as nodes: the label is another member's text.
        const popup = document.createElement("div");
        const label = document.createElement("strong");
        label.textContent = pt.label;
        popup.appendChild(label);
        if (pt.scheduled_at) {
            const when = document.createElement("small");
            when.textContent = new Date(pt.scheduled_at).toLocaleString();
            popup.append(document.createElement("br"), when);
        }
        return L.marker([pt.lat, pt.lng], { icon, draggable: !isChildTrip && pt.draggable }).bindPopup(popup);
    }

    private wireActivityMarker(marker: L.Marker, pt: MapPoint, activityId: string): void {
        marker.on("mouseover", () => highlightActivity(activityId, true));
        marker.on("mouseout", () => highlightActivity(activityId, false));
        this.markers.set(activityId, { marker, status: pt.status });
        if (pt.child_trip || !pt.draggable) return;

        let unlocked = false;
        marker.on("dragstart", () => {
            if (!this.map || this.map.getZoom() < DRAG_CONFIRM_ZOOM || unlocked) return;
            marker.dragging?.disable();
            marker.setLatLng([pt.lat, pt.lng]);
            void window.confirmDialog?.({
                title: "Move activity?",
                message: "At this zoom level it is easy to move a marker by accident.\n\nDrag again after confirming to reposition it.",
                confirmLabel: "Enable move",
                danger: false,
            }).then((ok) => {
                if (!ok) return;
                unlocked = true;
                marker.dragging?.enable();
                toast.info("Drag the marker to its new location.");
            });
        });
        marker.on("dragend", () => {
            unlocked = false;
            const at = marker.getLatLng();
            // The marker has already moved on screen, so a refused save must say so.
            window.ulSendJson?.(forActivity(this.cfg.positionUrl, activityId), "POST", { lat: at.lat, lng: at.lng }, { headers: { "X-Requested-With": "XMLHttpRequest" } })
                .catch(() => toast.error("Could not save new position."));
        });
    }

    load(): void {
        const mapEl = this.mapEl;
        if (!mapEl) return;
        const url = this.cfg.mapDataUrl + (this.showPast ? "?include_past=1" : "");
        fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
            .then((r) => {
                if (!r.ok) throw new Error(`trip map data: HTTP ${r.status}`);
                return r.json() as Promise<{ points?: MapPoint[] }>;
            })
            .then((data) => {
                const points = data.points ?? [];
                if (!points.length) {
                    this.showEmpty();
                    return;
                }
                setHidden("trip-map-wrap", false);
                mapEl.hidden = false;
                setHidden("trip-map-empty", true);
                const map = this.map ?? this.init(mapEl);
                this.markersGroup?.clearLayers();
                this.markers.clear();

                const bounds: [number, number][] = [];
                for (const pt of points) {
                    const marker = this.marker(pt);
                    if (pt.activity_id) this.wireActivityMarker(marker, pt, String(pt.activity_id));
                    if (this.markersGroup) marker.addTo(this.markersGroup);
                    bounds.push([pt.lat, pt.lng]);
                }
                const [only] = bounds;
                if (bounds.length === 1 && only) map.setView(only, 14);
                else map.fitBounds(bounds, { padding: [32, 32] });
                this.applyTabFilter();
            })
            .catch(() => {
                // A failed load must not leave a blank panel with no explanation.
                this.showEmpty();
                toast.error("Could not load the trip map. Refresh to try again.");
            });
    }
}

function highlightActivity(activityId: string, on: boolean): void {
    byId(`trip-activity-${activityId}`, HTMLElement)?.classList.toggle("trip-activity--highlighted", on);
}

// -- Activities panel: tabs, view and calendar ---------------------------------------------------

class ActivitiesView {
    tab: ActivityTab = "upcoming";
    view = "list";
    private calOffset = 0;
    private readonly tabKey: string;

    constructor(
        private readonly cfg: TripConfig,
        private readonly onTabChange: () => void,
        private readonly openEdit: (li: HTMLElement) => void,
    ) {
        this.tabKey = `trip-active-tab-${cfg.tripUuid}`;
    }

    restoreTab(): void {
        const stored = readStorage(this.tabKey);
        this.setTab(stored && (TABS as readonly string[]).includes(stored) ? (stored as ActivityTab) : "upcoming");
    }

    /** Proposed/Confirmed/Completed are disabled when nothing is in them; Upcoming never is. */
    private updateTabAvailability(panel: HTMLElement): void {
        const counts: Record<string, number> = { proposed: 0, confirmed: 0, completed: 0 };
        panel.querySelectorAll<HTMLElement>(".trip-activity-item").forEach((li) => {
            const status = li.dataset.actStatus || "proposed";
            if (status in counts) counts[status]! += 1;
        });
        for (const tab of ["proposed", "confirmed", "completed"]) {
            const btn = panel.querySelector<HTMLButtonElement>(`.activity-tab[data-tab="${tab}"]`);
            if (btn) btn.disabled = counts[tab] === 0;
        }
    }

    private updateEmptyState(panel: HTMLElement): void {
        const list = panel.querySelector(".trip-activity-list");
        if (!list) return;
        const anyVisible = Array.from(list.querySelectorAll<HTMLElement>(".trip-activity-item")).some((li) => li.style.display !== "none");
        let allDoneVisible = false;
        const allDone = panel.querySelector<HTMLElement>(".trip-panel-empty--all-completed");
        if (allDone) {
            allDoneVisible = !anyVisible && panel.dataset.allCompleted === "true" && this.tab === "upcoming";
            allDone.hidden = !allDoneVisible;
        }
        const emptyTab = panel.querySelector<HTMLElement>(".trip-panel-empty--tab");
        if (emptyTab) emptyTab.hidden = anyVisible || allDoneVisible;
    }

    setTab(tab: ActivityTab): void {
        this.tab = tab;
        writeStorage(this.tabKey, tab);
        const panel = activitiesPanel();
        if (panel) {
            this.updateTabAvailability(panel);
            panel.querySelectorAll<HTMLElement>(".activity-tab").forEach((btn) => btn.classList.toggle("activity-tab--active", btn.dataset.tab === tab));
            panel.querySelectorAll<HTMLElement>(".trip-activity-item").forEach((li) => {
                const status = li.dataset.actStatus || "proposed";
                const visible = tab === "completed" ? status === "completed" : status !== "completed" && (tab === "upcoming" || tab === status);
                li.style.display = visible ? "" : "none";
            });
            // Drive-time legs only make sense between the full itinerary's stops.
            panel.querySelectorAll<HTMLElement>(".trip-activity-leg").forEach((li) => {
                li.style.display = tab === "upcoming" ? "" : "none";
            });
            this.updateEmptyState(panel);
            if (this.view === "calendar") this.renderCalendar();
        }
        this.onTabChange();
    }

    setView(view: string): void {
        this.view = view;
        const panel = activitiesPanel();
        if (!panel) return;
        panel.querySelectorAll<HTMLElement>(".activity-view").forEach((el) => {
            el.hidden = el.dataset.view !== view;
        });
        panel.querySelectorAll<HTMLElement>(".view-toggle-btn").forEach((btn) => btn.classList.toggle("view-toggle-btn--active", btn.dataset.view === view));
        if (view === "calendar") this.renderCalendar();
    }

    pageCalendar(delta: number): void {
        this.calOffset += delta;
        this.renderCalendar();
    }

    private calendarActivities(panel: HTMLElement): CalendarActivity[] {
        const acts: CalendarActivity[] = [];
        panel.querySelectorAll<HTMLElement>(".trip-activity-item").forEach((li) => {
            if (!li.dataset.actDate || !li.dataset.actId) return;
            acts.push({
                id: li.dataset.actId,
                date: li.dataset.actDate,
                title: li.dataset.actTitle || li.querySelector(".trip-activity-name")?.textContent?.trim() || "Activity",
                index: li.querySelector(".trip-activity-num")?.textContent?.trim() ?? null,
                status: li.dataset.actStatus || "proposed",
                moveUrl: forActivity(this.cfg.moveUrl, li.dataset.actId),
            });
        });
        return acts;
    }

    private renderCalendar(): void {
        const panel = activitiesPanel();
        const calEl = panel?.querySelector<HTMLElement>(".activity-calendar-grid");
        if (!panel || !calEl) return;
        const months = tripMonths(panel.dataset.tripStart ?? "", panel.dataset.tripEnd ?? "", this.calOffset, new Date());
        calEl.innerHTML = calendarHtml(months, activitiesForTab(this.calendarActivities(panel), this.tab), new Date());
        this.bindCalendarDrag(calEl);
    }

    private bindCalendarDrag(calEl: HTMLElement): void {
        let dragMoveUrl: string | null = null;
        let wasDragging = false;

        calEl.querySelectorAll<HTMLElement>(".cal-event[draggable]").forEach((el) => {
            const actId = el.dataset.actId ?? "";
            el.addEventListener("dragstart", (e) => {
                dragMoveUrl = el.dataset.moveUrl ?? null;
                wasDragging = true;
                if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
                el.classList.add("cal-event--dragging");
            });
            el.addEventListener("dragend", () => {
                el.classList.remove("cal-event--dragging");
                // The click that follows a drop is not a request to edit.
                setTimeout(() => {
                    wasDragging = false;
                }, 150);
            });
            el.addEventListener("click", () => {
                if (wasDragging) return;
                const li = byId(`trip-activity-${actId}`, HTMLElement);
                if (li) this.openEdit(li);
            });
            el.addEventListener("mouseenter", () => {
                window.tripHighlightMarker?.(actId, true);
                highlightActivity(actId, true);
            });
            el.addEventListener("mouseleave", () => {
                window.tripHighlightMarker?.(actId, false);
                highlightActivity(actId, false);
            });
        });

        calEl.querySelectorAll<HTMLElement>(".cal-cell[data-date]").forEach((cell) => {
            cell.addEventListener("dragover", (e) => {
                e.preventDefault();
                if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
                cell.classList.add("cal-cell--drag-over");
            });
            cell.addEventListener("dragleave", () => cell.classList.remove("cal-cell--drag-over"));
            cell.addEventListener("drop", (e) => {
                e.preventDefault();
                cell.classList.remove("cal-cell--drag-over");
                if (!dragMoveUrl) return;
                void window.htmx?.ajax("POST", dragMoveUrl, { target: "#trip-activities-panel", swap: "outerHTML", values: { date: cell.dataset.date } });
                dragMoveUrl = null;
            });
        });
    }
}

// -- Add and edit activity dialogs -----------------------------------------------------------

function syncHideLabel(labelId: string, iconId: string, checked: boolean): void {
    byId(labelId, HTMLElement)?.classList.toggle("is-active", checked);
    const icon = byId(iconId, HTMLElement);
    if (icon) icon.textContent = checked ? "visibility_off" : "visibility";
}

function syncStatus(checkboxId: string, inputId: string): void {
    setValue(inputId, byId(checkboxId, HTMLInputElement)?.checked ? "proposed" : "confirmed");
}

function setChildTripToggle(open: boolean): void {
    const wrap = byId("edit-activity-child-trip-wrap", HTMLElement);
    const btn = byId("edit-activity-child-trip-toggle", HTMLElement);
    if (wrap) wrap.hidden = !open;
    if (btn) {
        btn.innerHTML = open
            ? '<i class="material-icons" style="font-size:.95rem;vertical-align:middle">remove</i> Remove child trip'
            : '<i class="material-icons" style="font-size:.95rem;vertical-align:middle">add</i> Add child trip';
    }
}

function clearEditChildTrip(): void {
    setValue("edit-activity-child-trip-uuid", "");
    const display = byId("edit-activity-child-trip-display", HTMLElement);
    if (display) display.textContent = "";
    const clear = byId("edit-activity-child-trip-clear", HTMLElement);
    if (clear) clear.style.display = "none";
}

function clearPicker(ids: PickerIds): void {
    for (const id of [ids.uuid, ids.pin, ids.lat, ids.lng, ids.name]) setValue(id, "");
    const display = byId(ids.display, HTMLElement);
    if (display) display.textContent = "";
}

/** Only the location picker shows at first; choosing a place or trip reveals the rest of the form. */
function revealAddActivity(title: string, kind: "place" | "trip"): void {
    setHidden("add-activity-location-picker", true);
    const icon = byId("activity-location-chip-icon", HTMLElement);
    if (icon) icon.textContent = kind === "trip" ? "link" : "place";
    const name = byId("activity-location-chip-name", HTMLElement);
    if (name) name.textContent = title;
    setHidden("activity-location-chip", false);
    setHidden("add-activity-more-fields", false);
    const submit = byId("add-activity-submit-btn", HTMLButtonElement);
    if (submit) submit.disabled = false;
}

function clearAddActivityLocation(): void {
    clearPicker(ADD_PICKER);
    setValue("activity-child-trip-uuid", "");
    setHidden("activity-location-chip", true);
    setHidden("add-activity-location-picker", false);
    const submit = byId("add-activity-submit-btn", HTMLButtonElement);
    if (submit) submit.disabled = true;
    const input = byId("activity-loc-search-input", HTMLInputElement);
    if (input) {
        input.value = "";
        input.focus();
    }
}

function resetAddActivityForm(): void {
    byId("add-activity-form", HTMLFormElement)?.reset();
    clearPicker(ADD_PICKER);
    for (const id of ["activity-child-trip-uuid", "activity-end-date", "activity-end-time"]) setValue(id, "");
    defaultDateToToday("activity-date");
    setHidden("activity-location-chip", true);
    setHidden("add-activity-location-picker", false);
    setHidden("add-activity-more-fields", true);
    const submit = byId("add-activity-submit-btn", HTMLButtonElement);
    if (submit) submit.disabled = true;
    setHidden("add-activity-end-date-wrap", true);
    setHidden("add-activity-end-date-toggle-row", false);
    const propose = byId("add-activity-propose-checkbox", HTMLInputElement);
    if (propose) propose.checked = false;
    setValue("add-activity-status-input", "confirmed");
    const hide = byId("add-activity-location-hidden", HTMLInputElement);
    if (hide) hide.checked = false;
    syncHideLabel("add-activity-hide-label", "add-activity-hide-icon", false);
    setHidden("add-activity-name-wrap", true);
    setHidden("add-activity-name-toggle", false);
}

function openAddActivityAt(lat: number, lng: number): void {
    setValue("activity-geocoded-lat", lat);
    setValue("activity-geocoded-lng", lng);
    setValue("activity-geocoded-name", "");
    setValue("activity-location-uuid", "");
    const display = byId("activity-location-display", HTMLElement);
    if (display) display.textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
    byId("add-activity-dialog", HTMLDialogElement)?.showModal();
}

function openEditActivity(li: HTMLElement): void {
    const form = byId("edit-activity-form", HTMLFormElement);
    if (form) {
        form.setAttribute("hx-post", li.dataset.editUrl ?? "");
        window.htmx?.process(form);
    }
    const d = li.dataset;

    const hasTitle = !!d.actTitle;
    setHidden("edit-activity-name-wrap", !hasTitle);
    setHidden("edit-activity-name-toggle", hasTitle);
    setValue("edit-activity-title", d.actTitle ?? "");
    setValue("edit-activity-notes", d.actNotes ?? "");
    setValue("edit-activity-date", d.actDate ?? "");
    setValue("edit-activity-time", d.actTime ?? "");

    const hasEnd = !!d.actEndDate;
    setValue("edit-activity-end-date", d.actEndDate ?? "");
    setValue("edit-activity-end-time", d.actEndTime ?? "");
    setHidden("edit-activity-end-date-wrap", !hasEnd);
    setHidden("edit-activity-end-date-toggle-row", hasEnd);

    // The row carries the location's slug, which the server resolves; it is not a UUID.
    clearPicker(EDIT_PICKER);
    setValue("edit-activity-location-uuid", d.actLocationRef ?? "");
    const display = byId("edit-activity-location-display", HTMLElement);
    if (display) display.textContent = d.actLocationName ?? "";
    setValue("edit-activity-loc-search-input", "");

    const childUuid = d.actChildTripUuid ?? "";
    setValue("edit-activity-child-trip-uuid", childUuid);
    const childDisplay = byId("edit-activity-child-trip-display", HTMLElement);
    if (childDisplay) childDisplay.textContent = d.actChildTripName ?? "";
    setValue("edit-activity-child-trip-search", "");
    const childClear = byId("edit-activity-child-trip-clear", HTMLElement);
    if (childClear) childClear.style.display = childUuid ? "inline-flex" : "none";
    setChildTripToggle(!!childUuid);

    const deleteBtn = byId("edit-activity-delete-btn", HTMLElement);
    if (deleteBtn) deleteBtn.dataset.deleteUrl = d.deleteUrl ?? "";

    const status = d.actStatus || "proposed";
    setValue("edit-activity-status-input", status);
    const propose = byId("edit-activity-propose-checkbox", HTMLInputElement);
    if (propose) propose.checked = status === "proposed";

    // Only the activity's adder or an organiser may hide its location.
    const hideWrap = byId("edit-activity-hide-wrap", HTMLElement);
    const hideInput = byId("edit-activity-location-hidden", HTMLInputElement);
    if (hideWrap && hideInput) {
        const canManage = d.actCanManage === "true";
        hideWrap.hidden = !canManage;
        if (canManage) hideInput.checked = d.actLocationHidden === "true";
        syncHideLabel("edit-activity-hide-label", "edit-activity-hide-icon", hideInput.checked);
    }

    byId("edit-activity-dialog", HTMLDialogElement)?.showModal();
}

async function deleteEditActivity(): Promise<void> {
    const deleteUrl = byId("edit-activity-delete-btn", HTMLElement)?.dataset.deleteUrl;
    if (!deleteUrl) return;
    if (!(await window.confirmDialog?.({ title: "Delete Activity", message: "Delete this activity? This cannot be undone.", confirmLabel: "Delete" }))) return;
    byId("edit-activity-dialog", HTMLDialogElement)?.close();
    void window.htmx?.ajax("DELETE", deleteUrl, { target: "#trip-activities-panel", swap: "outerHTML" });
}

function openCompleteDialog(url: string, activityDate: string): void {
    const form = byId("trip-complete-form", HTMLFormElement);
    if (form) {
        form.setAttribute("hx-post", url);
        window.htmx?.process(form);
    }
    const today = todayIso();
    const dateInput = byId("trip-complete-date", HTMLInputElement);
    if (dateInput) {
        // A completion date cannot be in the future.
        dateInput.value = activityDate && activityDate <= today ? activityDate : today;
        dateInput.max = today;
    }
    byId("trip-complete-dialog", HTMLDialogElement)?.showModal();
}

function bindLocationSearch(cfg: TripConfig, prefix: string, ids: PickerIds, onPicked?: (title: string) => void): void {
    window.LocationSearchEngine.attach(prefix, {
        sources: { localPins: { url: cfg.localUrl }, googlePlaces: { url: cfg.placesUrl } },
        resolvePlaceUrl: cfg.resolveUrl,
        pinCacheProfileUuid: cfg.profileUuid,
        enableMyLocation: false,
        defaultZoom: 16,
        onSelect: (result) => {
            setValue(ids.uuid, "");
            setValue(ids.pin, "");
            if (ids.childTrip) setValue(ids.childTrip, "");
            if (result.type === "pin" && result.pinSlug) {
                setValue(ids.pin, result.pinSlug);
                for (const id of [ids.lat, ids.lng, ids.name]) setValue(id, "");
            } else {
                // Anything else (an address, coordinates, a place) becomes a geocoded point.
                setValue(ids.lat, result.lat);
                setValue(ids.lng, result.lng);
                setValue(ids.name, result.title || "");
            }
            const title = result.title || `${result.lat.toFixed(5)}, ${result.lng.toFixed(5)}`;
            const display = byId(ids.display, HTMLElement);
            if (display) display.textContent = title;
            onPicked?.(title);
        },
    });
}

async function searchChildTrips(cfg: TripConfig, q: string): Promise<ChildTripResult[]> {
    const r = await fetch(`${cfg.childTripSearchUrl}?q=${encodeURIComponent(q)}`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
    const data = (await r.json()) as { results?: ChildTripResult[] };
    return data.results ?? [];
}

function tripDates(t: ChildTripResult): string {
    return t.start_date ? ` (${t.start_date}${t.end_date ? ` - ${t.end_date}` : ""})` : "";
}

/**
 * Adds "Your Trips" to the add dialog's location suggestions, so picking one links it as a child trip.
 * The search engine rebuilds the box on every keystroke; this listener is bound after it, so it appends after.
 */
function bindAddChildTripSuggestions(cfg: TripConfig): void {
    const input = byId("activity-loc-search-input", HTMLInputElement);
    const box = byId("activity-loc-search-suggestions", HTMLElement);
    if (!input || !box) return;
    let seq = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const render = (mySeq: number, trips: ChildTripResult[]): void => {
        if (mySeq !== seq) return;
        box.querySelector(".activity-trip-suggestion-slot")?.remove();
        if (!trips.length) return;
        const slot = document.createElement("div");
        slot.className = "addr-source-slot activity-trip-suggestion-slot";
        const hdr = document.createElement("div");
        hdr.className = "addr-suggestion-group-hdr";
        hdr.textContent = "Your Trips";
        slot.appendChild(hdr);
        for (const t of trips) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "addr-suggestion addr-suggestion--trip";
            btn.innerHTML =
                '<i class="material-icons addr-suggestion-icon">link</i><span class="addr-suggestion-content">' +
                `<span class="addr-suggestion-title">${escHtml(t.name || "Untitled trip")}</span>` +
                `<span class="addr-suggestion-sub">Link as child trip${escHtml(tripDates(t))}</span></span>`;
            btn.addEventListener("mousedown", (e) => {
                e.preventDefault();
                box.hidden = true;
                clearPicker(ADD_PICKER);
                setValue("activity-child-trip-uuid", t.uuid);
                const display = byId(ADD_PICKER.display, HTMLElement);
                if (display) display.textContent = t.name;
                input.value = t.name;
                revealAddActivity(t.name, "trip");
            });
            slot.appendChild(btn);
        }
        box.appendChild(slot);
        box.hidden = false;
    };

    input.addEventListener("input", () => {
        const q = input.value.trim();
        clearTimeout(timer);
        const mySeq = ++seq;
        if (q.length < 2) return;
        timer = setTimeout(() => {
            searchChildTrips(cfg, q)
                .then((trips) => render(mySeq, trips))
                .catch(() => undefined);
        }, 300);
    });
}

function bindEditChildTripSearch(cfg: TripConfig): void {
    const searchInput = byId("edit-activity-child-trip-search", HTMLInputElement);
    const suggestions = byId("edit-activity-child-trip-suggestions", HTMLElement);
    if (!searchInput || !suggestions) return;
    const close = (): void => {
        suggestions.replaceChildren();
        suggestions.hidden = true;
    };
    let timer: ReturnType<typeof setTimeout> | undefined;

    searchInput.addEventListener("input", () => {
        clearTimeout(timer);
        const q = searchInput.value.trim();
        if (q.length < 2) {
            close();
            return;
        }
        timer = setTimeout(() => {
            void searchChildTrips(cfg, q).then((results) => {
                suggestions.replaceChildren();
                if (!results.length) {
                    const empty = document.createElement("li");
                    empty.className = "location-suggestion-item location-suggestion-empty";
                    empty.textContent = "No trips found";
                    suggestions.appendChild(empty);
                }
                for (const t of results) {
                    const li = document.createElement("li");
                    li.className = "location-suggestion-item";
                    li.textContent = t.name + tripDates(t);
                    li.addEventListener("click", () => {
                        setValue("edit-activity-child-trip-uuid", t.uuid);
                        const display = byId("edit-activity-child-trip-display", HTMLElement);
                        if (display) display.textContent = t.name;
                        const clear = byId("edit-activity-child-trip-clear", HTMLElement);
                        if (clear) clear.style.display = "inline-flex";
                        searchInput.value = "";
                        close();
                    });
                    suggestions.appendChild(li);
                }
                suggestions.hidden = false;
            });
        }, 300);
    });

    document.addEventListener("click", (e) => {
        const target = e.target instanceof Node ? e.target : null;
        if (!target || (!searchInput.contains(target) && !suggestions.contains(target))) close();
    });
}

// -- Title and description, edited in place -------------------------------------------------

type InlineField = Omit<EditInPlaceOptions, "save">;

const INLINE_FIELDS: Record<"name" | "description", InlineField> = {
    name: { dataKey: "rawName", inputClass: "trip-title-input", maxLength: 255, successMessage: "Trip renamed.", errorMessage: "Failed to rename trip." },
    description: {
        dataKey: "rawDescription",
        inputClass: "trip-description-input",
        multiline: true,
        allowEmpty: true,
        placeholder: "Add a description...",
        maxLength: 50000,
        successMessage: "Description updated.",
        errorMessage: "Failed to update description.",
    },
};

/** The edit view answers with a partial, not JSON; only the status matters. */
async function saveTripField(cfg: TripConfig, field: "name" | "description", value: string): Promise<void> {
    const r = await fetch(cfg.editUrl, { method: "POST", headers: { "X-CSRFToken": window.csrftoken }, body: new URLSearchParams({ [field]: value }) });
    if (!r.ok) throw new Error(String(r.status));
}

/** Delegated: an activity date change re-renders the hero out of band, which would drop direct listeners. */
function bindInlineEditing(cfg: TripConfig): void {
    for (const [selector, field] of [
        [".trip-title-editable", "name"],
        [".trip-description-editable", "description"],
    ] as const) {
        delegateEditInPlace(selector, () => ({ ...INLINE_FIELDS[field], save: (value) => saveTripField(cfg, field, value) }));
    }
}

// -- Sections ------------------------------------------------------------------------------

function bindSectionOrder(cfg: TripConfig): void {
    const container = byId("trip-sections", HTMLElement);
    if (!container) return;
    const orderKey = `trip-section-order-${cfg.tripUuid}`;
    try {
        const saved: unknown = JSON.parse(readStorage(orderKey) ?? "null");
        if (Array.isArray(saved)) {
            for (const section of saved) {
                const el = container.querySelector(`.trip-section[data-section="${CSS.escape(String(section))}"]`);
                if (el) container.appendChild(el);
            }
        }
    } catch {
        // A corrupt saved order falls back to the page's own.
    }
    Sortable.create(container, {
        animation: 150,
        handle: ".trip-section-grip",
        ghostClass: "trip-section--ghost",
        chosenClass: "trip-section--chosen",
        onEnd: () => {
            const order = Array.from(container.querySelectorAll<HTMLElement>(".trip-section")).map((el) => el.dataset.section);
            writeStorage(orderKey, JSON.stringify(order));
        },
    });
}

function initOnboarding(): void {
    initOnboardingTour({
        prefix: "ul_onboarding_v1_trip",
        hostSelector: "#trip-onboarding",
        cards: [
            {
                id: "activities",
                icon: "playlist_add",
                target: "#trip-activities-panel, .trip-detail-activities",
                eyebrow: "Itinerary",
                title: "Activities are proposed or confirmed stops",
                body: "Add locations, times, notes, secret stops, or even a child trip. Votes let the group weigh in on what makes the final plan.",
                button: "Add an activity",
                watchSelector: '#trip-activities-panel [type="button"]',
                action: () => {
                    const add = document.querySelector<HTMLElement>('#trip-activities-panel .add-activity-btn, #trip-activities-panel [data-action="add"]');
                    if (add) add.click();
                    else activitiesPanel()?.scrollIntoView({ behavior: "smooth", block: "center" });
                },
                // Once the trip has an activity, the feature has been found.
                ready: () => {
                    const panel = activitiesPanel();
                    return !!panel && !panel.querySelector(".trip-activity-item");
                },
            },
            {
                id: "members-rsvp",
                icon: "groups",
                target: "#trip-members-panel",
                eyebrow: "Coordination",
                title: "Members can RSVP and organizers can manage planning permissions",
                body: "Use the Members panel to see who is coming, promote organizers, or invite the right collaborators.",
                button: "View members",
                watchSelector: '#trip-members-panel [type="button"]',
                action: () => byId("trip-members-panel", HTMLElement)?.scrollIntoView({ behavior: "smooth", block: "center" }),
                ready: () => !!byId("trip-members-panel", HTMLElement),
            },
            {
                id: "section-layout",
                icon: "drag_indicator",
                target: "#trip-sections .trip-section-grip, #trip-sections",
                eyebrow: "Personal layout",
                title: "Reorder trip sections for the way you plan",
                body: "Drag the grip handles to put activities, members, comments, or weather where you need them. The order saves automatically.",
                button: "Show sections",
                watchSelector: ".trip-section-grip",
                watchEvent: "pointerdown",
                action: () => byId("trip-sections", HTMLElement)?.scrollIntoView({ behavior: "smooth", block: "start" }),
                ready: () => !!byId("trip-sections", HTMLElement),
            },
        ],
    });
}

// -- Wiring --------------------------------------------------------------------------------

function bind(cfg: TripConfig): void {
    if (cfg.showOnboarding) initOnboarding();

    let view: ActivitiesView | null = null;
    const map = new TripMap(cfg, () => view?.tab ?? "upcoming", openAddActivityAt);
    view = new ActivitiesView(cfg, () => map.applyTabFilter(), openEditActivity);
    const activities = view;
    window.tripHighlightMarker = (id, on) => map.highlightMarker(id, on);
    map.load();
    window.ulHtmxActions?.register("trip-refresh-map", () => map.load());

    bindSectionOrder(cfg);
    bindLocationSearch(cfg, "activity-loc", ADD_PICKER, (title) => revealAddActivity(title, "place"));
    bindLocationSearch(cfg, "edit-activity-loc", EDIT_PICKER);
    bindAddChildTripSuggestions(cfg);
    bindEditChildTripSearch(cfg);
    bindInlineEditing(cfg);

    // The trip's start date is filled in server-side; an undated trip starts today.
    defaultDateToToday("activity-date");
    window.ulHtmxActions?.register("trip-add-activity", () => {
        byId("add-activity-dialog", HTMLDialogElement)?.close();
        resetAddActivityForm();
    });
    // Every way out of the dialog resets it, so reopening never shows a stale draft.
    byId("add-activity-dialog", HTMLElement)?.addEventListener("close", resetAddActivityForm);

    const onChange = (id: string, handler: (input: HTMLInputElement) => void): void => {
        const input = byId(id, HTMLInputElement);
        input?.addEventListener("change", () => handler(input));
    };
    onChange("add-activity-propose-checkbox", () => syncStatus("add-activity-propose-checkbox", "add-activity-status-input"));
    onChange("edit-activity-propose-checkbox", () => syncStatus("edit-activity-propose-checkbox", "edit-activity-status-input"));
    onChange("add-activity-location-hidden", (input) => syncHideLabel("add-activity-hide-label", "add-activity-hide-icon", input.checked));
    onChange("edit-activity-location-hidden", (input) => syncHideLabel("edit-activity-hide-label", "edit-activity-hide-icon", input.checked));

    document.addEventListener("click", (e) => {
        const control = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-trip-action]") : null;
        if (!control) return;
        const d = control.dataset;
        switch (d.tripAction) {
            case "set-tab":
                if ((TABS as readonly string[]).includes(d.tab ?? "")) activities.setTab(d.tab as ActivityTab);
                break;
            case "set-view":
                activities.setView(d.view ?? "list");
                break;
            case "cal-nav":
                activities.pageCalendar(Number(d.delta) || 0);
                break;
            case "edit-activity": {
                const li = control.closest<HTMLElement>("li");
                if (li) openEditActivity(li);
                break;
            }
            case "complete":
                openCompleteDialog(d.completeUrl ?? "", d.activityDate ?? "");
                break;
            case "toggle-rsvp": {
                const popup = byId(d.popupId ?? "", HTMLElement);
                if (popup) popup.hidden = !popup.hidden;
                break;
            }
            case "clear-add-location":
                clearAddActivityLocation();
                break;
            case "reveal-add-name":
                setHidden("add-activity-name-wrap", false);
                setHidden("add-activity-name-toggle", true);
                break;
            case "reveal-edit-name":
                setHidden("edit-activity-name-wrap", false);
                setHidden("edit-activity-name-toggle", true);
                break;
            case "reveal-add-end":
                setHidden("add-activity-end-date-wrap", false);
                setHidden("add-activity-end-date-toggle-row", true);
                defaultDateToToday("activity-end-date");
                byId("activity-end-date", HTMLElement)?.focus();
                break;
            case "reveal-edit-end":
                setHidden("edit-activity-end-date-wrap", false);
                setHidden("edit-activity-end-date-toggle-row", true);
                defaultDateToToday("edit-activity-end-date");
                byId("edit-activity-end-date", HTMLElement)?.focus();
                break;
            case "clear-fields":
                for (const id of (d.fields ?? "").split(" ").filter(Boolean)) setValue(id, "");
                break;
            case "clear-edit-location":
                clearPicker(EDIT_PICKER);
                setValue("edit-activity-loc-search-input", "");
                break;
            case "toggle-edit-child-trip": {
                const opening = byId("edit-activity-child-trip-wrap", HTMLElement)?.hidden === true;
                setChildTripToggle(opening);
                if (!opening) clearEditChildTrip();
                break;
            }
            case "clear-edit-child-trip":
                clearEditChildTrip();
                break;
            case "delete-edit-activity":
                void deleteEditActivity();
                break;
        }
    });

    document.addEventListener("mouseover", (e) => hoverActivity(e, true));
    document.addEventListener("mouseout", (e) => hoverActivity(e, false));

    // A weather panel that comes back hidden takes its section with it.
    document.body.addEventListener("htmx:afterSettle", (e) => {
        const weather = byId("trip-weather-panel", HTMLElement);
        const section = weather?.closest<HTMLElement>(".trip-section");
        if (weather && section) section.hidden = weather.hidden;

        const panel = activitiesPanel();
        const target = e.target instanceof Node ? e.target : null;
        const activitiesSection = document.querySelector(".trip-detail-activities");
        if (target && (target === panel || activitiesSection?.contains(target))) activities.setTab(activities.tab);
    });

    const activitiesSection = document.querySelector(".trip-detail-activities");
    activitiesSection?.addEventListener("htmx:afterSwap", () => {
        map.load();
        if (activities.view !== "list") activities.setView(activities.view);
        activities.restoreTab();
    });
}

/** Rows with a map marker highlight it while hovered. */
function hoverActivity(e: MouseEvent, on: boolean): void {
    const target = e.target instanceof Element ? e.target : null;
    const li = target?.closest<HTMLElement>(".trip-activity-item[data-marker-highlight]");
    if (!li) return;
    const related = e.relatedTarget instanceof Node ? e.relatedTarget : null;
    if (related && li.contains(related)) return;
    window.tripHighlightMarker?.(li.dataset.actId ?? "", on);
}

const root = document.querySelector<HTMLElement>(".trip-detail-page");
if (root) bind(readConfig(root));
