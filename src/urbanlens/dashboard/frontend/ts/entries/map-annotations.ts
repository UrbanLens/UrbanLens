/**
 * Shared map annotations page: markup drawing/editing, the unified detail-pin
 * side panel, the typed boundary editor (+ its context menu), the photo
 * layer, and the Details/Photos layers list panel. Used identically by the
 * pin detail page and the Location wiki page. The map's right-click menu is
 * the shared base (copy coordinates, Street View, directions) plus "Create
 * child pin here"; boundary polygons extend that same menu with Edit /
 * Convert / Delete.
 *
 * Config comes from data-* attributes on `#map` rather than being baked into
 * the script by the template (see templates/dashboard/pages/location/index.html
 * and wiki.html), which is what lets one compiled bundle serve both pages.
 */
import { getCsrfToken } from "../shared/csrf";
import { toast, confirmAction, htmxProcess } from "../shared/dialogs";
import type { CustomLayerToggle } from "../shared/map-layers";
import { createMapImageOverlays, wireManageOverlaysDialog, type MapOverlayEntry } from "../shared/map-image-overlays";
import { createMapLayers, MAP_MAX_ZOOM, MAP_MIN_ZOOM, tileLayer } from "../shared/map-layers";
import { bindMapContextMenu, showMapContextMenu, type ContextMenuItem } from "../shared/map-context-menu";
import { AdditiveSelectMemory, createPinClusterGroup, isAdditiveClick, reclusterOnDrag, returnToCluster } from "../shared/map-clusters";
import type { MarkupItem, MarkupToolbar } from "../shared/markup-toolbar";
import { createPhotoClusterGroup, makePhotoIcon, photoMarkerSize as sharedPhotoMarkerSize, tagPhotoMarker } from "../shared/photo-map";
import { createTemporalImagerySlider } from "../shared/temporal-imagery";

// See markup-engine.ts for why `L` is declared locally instead of imported.
declare const L: typeof import("leaflet");
// Triggers TS to pick up @types/leaflet-draw's `declare module "leaflet"`
// augmentation (L.Draw, L.Control.Draw, L.EditToolbar, ...) - erased at
// build time, no runtime import (leaflet-draw is loaded via CDN like Leaflet
// itself, only referenced here as an ambient global via `L`).
import type { } from "leaflet-draw";

interface DetailPinEntry {
    uuid: string;
    /** Slug of the child pin's own detail page (Pin-backed detail pins only). */
    slug?: string;
    /** URL of the child pin's own detail page (Pin-backed detail pins only). */
    url?: string;
    /** Name of the child pin this entry belongs to, when it came from a
     * descendant (the page-wide "show child pin details" toggle). Entries with
     * an owner are display-only here - they're edited from their own page. */
    owner_name?: string;
    name: string;
    pin_type: string;
    icon: string | null;
    color: string | null;
    bg_color: string;
    bg_opacity?: number;
    border_color: string;
    border_opacity?: number;
    description: string;
    added_by: string;
    is_mine: boolean;
    latitude: number;
    longitude: number;
    marker: L.Marker | null;
}

interface PhotoPanelItem {
    id: number;
    url: string;
    lat: number | null;
    lng: number | null;
    mine: boolean;
}

interface NearbyPinEntry {
    name: string;
    icon: string | null;
    url: string;
    latitude: number | null;
    longitude: number | null;
}

/** A Media-section tile's payload, as carried by its "text/media-item" drag. */
interface MediaDropItem {
    source: string;
    key: string;
    url: string;
    pageUrl: string;
    caption: string;
}

interface BuildingImportRow {
    selection_key: string;
    name: string;
    building_number: string;
    latitude: number | null;
    longitude: number | null;
    geometry: object | null;
}

/** One of the owner's own top-level pins standing inside the property - the
 * "organize" dialog's other kind of candidate, alongside buildings. See
 * controllers.pin_restructure._nestable_map_data. */
interface NestableImportRow {
    pk: number;
    name: string;
    latitude: number | null;
    longitude: number | null;
}

function escHtml(s: string): string {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function readConfig(el: HTMLElement) {
    const d = el.dataset;
    return {
        mapCenterLat: Number.parseFloat(d.mapCenterLat ?? "0"),
        mapCenterLng: Number.parseFloat(d.mapCenterLng ?? "0"),
        markerIconUrl: d.markerIconUrl || "",
        markerShadowUrl: d.markerShadowUrl || "",
        pinSlug: d.pinSlug || "",
        locationSlug: d.locationSlug || "",
        defaultMapView: d.defaultMapView || "satellite",
        profileUuid: d.profileUuid || "",
        openweathermapApiKey: d.openweathermapApiKey || "",
        mainMarkerOwnerUuid: d.mainMarkerOwnerUuid || "",
        markupJsonUrl: d.markupJsonUrl || "",
        markupCreateUrl: d.markupCreateUrl || "",
        markupEditUrlTemplate: d.markupEditUrlTemplate || "",
        detailPinsJsonUrl: d.detailPinsJsonUrl || "",
        detailPinCreateUrl: d.detailPinCreateUrl || "",
        detailPinEditUrlTemplate: d.detailPinEditUrlTemplate || "",
        overlayCornersUrlTemplate: d.overlayCornersUrlTemplate || "",
        detailPinsBulkEditUrl: d.detailPinsBulkEditUrl || "",
        pinShareDialogUrl: d.pinShareDialogUrl || "",
        detailPinsSendToWikiUrl: d.detailPinsSendToWikiUrl || "",
        boundaryUrl: d.boundaryUrl || "",
        photoGalleryJsonUrl: d.photoGalleryJsonUrl || "",
        nearbyPinsJsonUrl: d.nearbyPinsJsonUrl || "",
        mediaRelevanceUrl: d.mediaRelevanceUrl || "",
        markupFillOpacity: d.markupFillOpacity ? Number.parseInt(d.markupFillOpacity, 10) : 87,
        markupBorderOpacity: d.markupBorderOpacity ? Number.parseInt(d.markupBorderOpacity, 10) : 100,
        showOnboardingTips: d.showOnboardingTips === "1",
        // Beta-only time slider (see shared/temporal-imagery.ts) - empty unless
        // the viewer has SiteFeature.BETA_FEATURES and this location has dated
        // OHM coverage nearby (temporal_slider_years() decides both server-side).
        temporalYears: (d.temporalYears || "").split(",").filter(Boolean).map(Number),
        temporalImageryUrlTemplate: d.temporalImageryUrlTemplate || "",
    };
}

interface CustomLayerEntry {
    uuid: string;
    name: string;
    color: string;
    icon: string;
    default_visible: boolean;
}

// Same json_script-embedded-<script> pattern as #building-import-map-data
// above - lives next to #map rather than on #map-annotations-config's
// dataset since it's a list, not a scalar attribute.
function readCustomLayers(): CustomLayerEntry[] {
    try {
        return JSON.parse(document.getElementById("custom-layers-data")?.textContent || "[]");
    } catch {
        return [];
    }
}

// Georeferenced image overlays, embedded the same way (see
// _map_annotations_panels.html and shared/map-image-overlays.ts).
function readMapOverlays(): MapOverlayEntry[] {
    try {
        const parsed = JSON.parse(document.getElementById("map-overlays-data")?.textContent || "[]");
        return Array.isArray(parsed) ? parsed : [];
    } catch {
        return [];
    }
}

/**
 * Wire a rubber-band rectangle selection gesture onto a map's container.
 *
 * Pointer events serve mouse, touch and pen from one code path, so the
 * gesture is reachable on a phone as well as with a mouse.
 *
 * @param element - The map's container element.
 * @param map - The Leaflet map the rectangle is drawn on.
 * @param isActive - Whether the caller's select mode is currently on.
 * @param onSelect - Receives the dragged rectangle's bounds once the drag ends.
 */
function initMapRectangleSelect(element: HTMLElement, map: L.Map, isActive: () => boolean, onSelect: (bounds: L.LatLngBounds) => void): void {
    // A finger's contact point drifts further than a mouse cursor's, so a
    // coarse pointer needs more slop before a tap reads as a drag.
    const MOUSE_DRAG_THRESHOLD_PX = 6;
    const COARSE_DRAG_THRESHOLD_PX = 12;

    let rect: L.Rectangle | null = null;
    element.addEventListener("pointerdown", (event: PointerEvent) => {
        // A second finger belongs to a pinch-zoom, not to a second rectangle.
        if (!isActive() || !event.isPrimary || event.button !== 0) return;
        const startLL = map.mouseEventToLatLng(event);
        const pointerId = event.pointerId;
        const threshold = event.pointerType === "mouse" ? MOUSE_DRAG_THRESHOLD_PX : COARSE_DRAG_THRESHOLD_PX;
        const restoreDragging = map.dragging.enabled();
        map.dragging.disable();
        let dragging = false;

        function finish(): void {
            element.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", onUp);
            window.removeEventListener("pointercancel", onCancel);
            if (rect) {
                map.removeLayer(rect);
                rect = null;
            }
            if (restoreDragging) map.dragging.enable();
        }

        function onCancel(cancelEvent: PointerEvent): void {
            if (cancelEvent.pointerId !== pointerId) return;
            finish();
        }

        function onMove(moveEvent: PointerEvent): void {
            // A pinch's second finger would otherwise redraw the rectangle to
            // its position, and its release would commit those bounds.
            if (moveEvent.pointerId !== pointerId) return;
            if (!dragging && Math.hypot(moveEvent.clientX - event.clientX, moveEvent.clientY - event.clientY) < threshold) return;
            if (!dragging) {
                // Capturing only once the gesture is a drag keeps a plain tap
                // reaching the marker or shape underneath, while still
                // delivering moves that leave the map's bounds.
                element.setPointerCapture(moveEvent.pointerId);
                dragging = true;
            }
            if (rect) map.removeLayer(rect);
            rect = L.rectangle(L.latLngBounds(startLL, map.mouseEventToLatLng(moveEvent)), {
                color: "#1E88E5",
                weight: 2,
                fillOpacity: 0.08,
                dashArray: "4 4",
                interactive: false,
            }).addTo(map);
        }

        function onUp(upEvent: PointerEvent): void {
            if (upEvent.pointerId !== pointerId) return;
            const dragged = dragging;
            finish();
            if (!dragged) return;
            onSelect(L.latLngBounds(startLL, map.mouseEventToLatLng(upEvent)));
        }

        element.addEventListener("pointermove", onMove);
        // The terminators go on window, not the element: capture is only taken
        // once the drag threshold is crossed, so a press that ends before that
        // (a flick off the map edge) would otherwise never be delivered here -
        // leaving these listeners attached and map dragging disabled, and
        // letting the next gesture fire a stale onUp with the previous bounds.
        window.addEventListener("pointerup", onUp);
        window.addEventListener("pointercancel", onCancel);
    });
}

function init(): void {
    const mapEl = document.getElementById("map");
    // Config lives on a dedicated element rather than #map itself: #map is
    // rendered by _map_annotations_panels.html (included from page content,
    // before the {% url %} endpoint variables this page defines further down
    // in {% block scripts %} exist), while #map-annotations-config sits right
    // next to this entry's own <script> tag where those URLs are in scope.
    const configEl = document.getElementById("map-annotations-config");
    if (!mapEl || !configEl) return;
    const cfg = readConfig(configEl);

    // -- Map setup ---------------------------------------------------------
    const mapCenterLat = cfg.mapCenterLat;
    const mapCenterLng = cfg.mapCenterLng;
    // Expose for the comment map composer's default center.
    window._commentMapDefaultLat = mapCenterLat;
    window._commentMapDefaultLng = mapCenterLng;

    // "Take a screenshot" toolbar button - opens the shared standalone map
    // composer (base.html) scoped to whichever of pin/wiki this page is.
    window._openMapScreenshot = function () {
        const context = cfg.pinSlug ? { pinSlug: cfg.pinSlug } : cfg.locationSlug ? { locationSlug: cfg.locationSlug } : null;
        const center = map.getCenter();
        window._openCommentMapComposer({ context, initialView: { lat: center.lat, lng: center.lng, zoom: map.getZoom() } });
    };

    // attributionControl: false - required attribution renders in the page
    // footer instead (show_map_footer=True; see createMapLayers' onAttribution below).
    // maxZoom is explicit because the cluster groups below are added before the
    // first tile layer, and leaflet.markercluster throws on an infinite
    // getMaxZoom() - see MAP_MAX_ZOOM.
    const map = L.map("map", { scrollWheelZoom: false, attributionControl: false, maxZoom: MAP_MAX_ZOOM, minZoom: MAP_MIN_ZOOM }).setView(
        [mapCenterLat, mapCenterLng],
        15,
    );
    window.map = map;

    // -- Selectable parcel-building import dialog ---------------------------
    // Its body is loaded by HTMX. Per the template convention, the dialog is
    // opened before Leaflet is initialized so the map has measurable bounds.
    let buildingImportMap: L.Map | null = null;

    // Map select tool (top-right toolbar button) - lets the user click, or
    // drag a box over, buildings on the preview map to toggle their checkbox,
    // mirroring the main map's/pin detail map's select-mode tools. The dialog
    // body (and so the map + button) is HTMX-swapped fresh on every open, so
    // the mode itself and its DOM side effects are tracked separately: the
    // former survives across opens (reset below), the latter is rebound each
    // time via `applyBuildingSelectMode`.
    let buildingSelectMode = false;
    let applyBuildingSelectMode: ((active: boolean) => void) | null = null;
    window.toggleBuildingImportSelectMode = function (): void {
        buildingSelectMode = !buildingSelectMode;
        applyBuildingSelectMode?.(buildingSelectMode);
    };

    function initBuildingImportDialog(): void {
        const dialog = document.getElementById("building-import-dialog") as HTMLDialogElement | null;
        const mapElement = document.getElementById("building-import-map");
        const dataElement = document.getElementById("building-import-map-data");
        const nestableDataElement = document.getElementById("building-import-nestable-map-data");
        const form = dialog?.querySelector<HTMLFormElement>(".building-import-form");
        if (!dialog || !mapElement || !dataElement || !form) return;

        let buildings: BuildingImportRow[];
        try {
            buildings = JSON.parse(dataElement.textContent || "[]") as BuildingImportRow[];
        } catch {
            buildings = [];
        }
        let nestablePins: NestableImportRow[];
        try {
            nestablePins = JSON.parse(nestableDataElement?.textContent || "[]") as NestableImportRow[];
        } catch {
            nestablePins = [];
        }

        buildingImportMap?.remove();
        const previewMap = L.map(mapElement, { scrollWheelZoom: false, attributionControl: false, maxZoom: MAP_MAX_ZOOM, minZoom: MAP_MIN_ZOOM }).setView([mapCenterLat, mapCenterLng], 16);
        buildingImportMap = previewMap;
        tileLayer("street").addTo(previewMap);

        // Buildings and existing-pin candidates share one selection key space
        // (a building's selection_key hash vs. a pin's stringified pk never
        // collide in practice) so select-all/count/rectangle-select treat both
        // uniformly; only the per-row "child pin vs merge" mode toggle below is
        // pin-specific.
        const checkboxes = Array.from(form.querySelectorAll<HTMLInputElement>('input[name="building_keys"], input[name="nest_keys"]'));
        const checkboxByKey = new Map(checkboxes.map((checkbox) => [checkbox.value, checkbox] as const));
        const rowByKey = new Map<string, HTMLElement>();
        checkboxes.forEach((checkbox) => {
            const row = checkbox.closest<HTMLElement>(".building-import-item");
            if (row) rowByKey.set(checkbox.value, row);
        });
        const selectedCount = form.querySelector<HTMLElement>("[data-building-selected-count]");
        const selectAll = form.querySelector<HTMLButtonElement>("[data-building-select-all]");
        const submit = form.querySelector<HTMLButtonElement>("[data-building-import-submit]");
        const submitLabel = form.querySelector<HTMLElement>("[data-building-submit-label]");
        const isRestructure = form.dataset.restructure === "1";

        const pathsByKey = new Map<string, L.Path[]>();
        const boundsByKey = new Map<string, L.LatLngBounds>();
        const previewBounds = L.latLngBounds([]);
        const selectedStyle: L.PathOptions = { color: "#2563eb", weight: 3, fillColor: "#3b82f6", fillOpacity: 0.45, opacity: 1 };
        const unselectedStyle: L.PathOptions = { color: "#64748b", weight: 2, fillColor: "#94a3b8", fillOpacity: 0.12, opacity: 0.5 };
        const selectedPinStyle: L.PathOptions = { color: "#7c3aed", weight: 3, fillColor: "#a78bfa", fillOpacity: 0.55, opacity: 1 };
        const unselectedPinStyle: L.PathOptions = { color: "#64748b", weight: 2, fillColor: "#94a3b8", fillOpacity: 0.12, opacity: 0.5 };
        const hoverStyle: L.PathOptions = { color: "#f97316", weight: 4 };
        const pinKeys = new Set(nestablePins.map((candidate) => String(candidate.pk)));
        const styleForKey = (key: string): L.PathOptions => {
            const checked = checkboxByKey.get(key)?.checked ?? false;
            if (pinKeys.has(key)) return checked ? selectedPinStyle : unselectedPinStyle;
            return checked ? selectedStyle : unselectedStyle;
        };

        // Mode toggle ("Child pin" vs "Merge into this pin") shows/hides that
        // row's conflict picker (or plain merge warning) - both start hidden
        // server-side unless this candidate was resubmitted with "Merge"
        // already chosen (see _nestable_rows' default_merge).
        form.querySelectorAll<HTMLElement>(".building-import-pin-row").forEach((row) => {
            const modeInputs = Array.from(row.querySelectorAll<HTMLInputElement>('input[type="radio"][name^="nest_mode__"]'));
            const detail = row.querySelector<HTMLElement>(".building-import-merge-conflicts, .building-import-merge-note");
            if (!detail) return;
            const sync = (): void => {
                const merging = modeInputs.some((input) => input.checked && input.value === "merge");
                detail.hidden = !merging;
            };
            modeInputs.forEach((input) => input.addEventListener("change", sync));
            sync();
        });

        // Bidirectional hover sync between the row list and the map preview -
        // row/shape pairs share `selection_key` via rowByKey/pathsByKey.
        let hoveredKey: string | null = null;
        const setBuildingHover = (key: string | null): void => {
            if (hoveredKey === key) return;
            if (hoveredKey) {
                rowByKey.get(hoveredKey)?.classList.remove("is-hovered");
                pathsByKey.get(hoveredKey)?.forEach((path) => path.setStyle(styleForKey(hoveredKey!)));
            }
            hoveredKey = key;
            if (key) {
                rowByKey.get(key)?.classList.add("is-hovered");
                pathsByKey.get(key)?.forEach((path) => {
                    path.setStyle(hoverStyle);
                    path.bringToFront();
                });
            }
        };

        const buildingCheckboxes = checkboxes.filter((checkbox) => !pinKeys.has(checkbox.value));

        const syncSelection = (): void => {
            let buildingsChecked = 0;
            let totalChecked = 0;
            checkboxes.forEach((checkbox) => {
                if (!checkbox.checked) return;
                totalChecked += 1;
                if (!pinKeys.has(checkbox.value)) buildingsChecked += 1;
            });
            pathsByKey.forEach((paths, key) => {
                if (key === hoveredKey) return;
                paths.forEach((path) => path.setStyle(styleForKey(key)));
            });
            // "N of M selected" / "(Un)check all" only ever describe the
            // buildings list - the denominator the template renders
            // (building_count) is buildings-only, and pins get their own
            // per-row controls instead of a bulk toggle.
            if (selectedCount) selectedCount.textContent = String(buildingsChecked);
            if (selectAll) selectAll.textContent = buildingsChecked === buildingCheckboxes.length ? "Uncheck all" : "Check all";
            if (submit) submit.disabled = totalChecked === 0;
            if (submitLabel && !isRestructure) submitLabel.textContent = `Add ${buildingsChecked} building${buildingsChecked === 1 ? "" : "s"}`;
        };

        const toggleBuildingKey = (key: string): void => {
            const checkbox = checkboxByKey.get(key);
            if (!checkbox) return;
            checkbox.checked = !checkbox.checked;
            syncSelection();
        };

        buildings.forEach((building) => {
            const paths: L.Path[] = [];
            let preview: L.Layer | null = null;
            if (building.geometry) {
                const geoJson = L.geoJSON(building.geometry as Parameters<typeof L.geoJSON>[0], {
                    style: selectedStyle,
                    onEachFeature: (_feature, layer) => {
                        if (layer instanceof L.Path) paths.push(layer);
                    },
                }).addTo(previewMap);
                preview = geoJson;
                const bounds = geoJson.getBounds();
                if (bounds.isValid()) {
                    previewBounds.extend(bounds);
                    boundsByKey.set(building.selection_key, bounds);
                }
            } else if (building.latitude != null && building.longitude != null) {
                const point = L.circleMarker([building.latitude, building.longitude], { ...selectedStyle, radius: 8 }).addTo(previewMap);
                preview = point;
                paths.push(point);
                previewBounds.extend(point.getLatLng());
                boundsByKey.set(building.selection_key, L.latLngBounds(point.getLatLng(), point.getLatLng()));
            }
            preview?.bindTooltip(building.name || (building.building_number ? `Building ${building.building_number}` : "Unnamed building"));
            preview?.on("mouseover", () => setBuildingHover(building.selection_key));
            preview?.on("mouseout", () => setBuildingHover(null));
            preview?.on("click", () => {
                if (buildingSelectMode) toggleBuildingKey(building.selection_key);
            });
            pathsByKey.set(building.selection_key, paths);
        });

        // Existing-pin candidates get a plain point marker (no footprint data
        // to draw) in a visually distinct color from buildings - see the
        // legend in _building_import_dialog_body.html.
        nestablePins.forEach((candidate) => {
            if (candidate.latitude == null || candidate.longitude == null) return;
            const key = String(candidate.pk);
            const point = L.circleMarker([candidate.latitude, candidate.longitude], { ...selectedPinStyle, radius: 8 }).addTo(previewMap);
            point.bindTooltip(candidate.name || "Unnamed pin");
            point.on("mouseover", () => setBuildingHover(key));
            point.on("mouseout", () => setBuildingHover(null));
            point.on("click", () => {
                if (buildingSelectMode) toggleBuildingKey(key);
            });
            previewBounds.extend(point.getLatLng());
            boundsByKey.set(key, L.latLngBounds(point.getLatLng(), point.getLatLng()));
            pathsByKey.set(key, [point]);
        });

        if (previewBounds.isValid()) previewMap.fitBounds(previewBounds.pad(0.18), { maxZoom: 18 });

        rowByKey.forEach((row, key) => {
            row.addEventListener("mouseenter", () => setBuildingHover(key));
            row.addEventListener("mouseleave", () => setBuildingHover(null));
        });

        checkboxes.forEach((checkbox) => checkbox.addEventListener("change", syncSelection));
        selectAll?.addEventListener("click", () => {
            const shouldCheck = buildingCheckboxes.some((checkbox) => !checkbox.checked);
            buildingCheckboxes.forEach((checkbox) => {
                checkbox.checked = shouldCheck;
            });
            syncSelection();
        });
        syncSelection();

        // Fresh dialog open always starts out of select mode; wire this open's
        // button/map/dragging up to the (persistent) toggle above.
        buildingSelectMode = false;
        const selectBtn = document.getElementById("select-building-import-button") as HTMLButtonElement | null;
        applyBuildingSelectMode = (active: boolean): void => {
            selectBtn?.classList.toggle("active", active);
            mapElement.classList.toggle("select-mode", active);
            // Disabling dragging makes Leaflet hand touch panning back to the
            // browser, which would scroll the page instead of letting the
            // rubber band consume the gesture.
            mapElement.style.touchAction = active ? "none" : "";
            if (active) previewMap.dragging.disable();
            else previewMap.dragging.enable();
        };

        // Rectangle drag-select over building shapes, sharing the detail-pin
        // panel's multi-select gesture (initMapRectangleSelect).
        initMapRectangleSelect(
            mapElement,
            previewMap,
            () => buildingSelectMode,
            (bounds) => {
                boundsByKey.forEach((buildingBounds, key) => {
                    if (bounds.intersects(buildingBounds)) toggleBuildingKey(key);
                });
            },
        );

        requestAnimationFrame(() => buildingImportMap?.invalidateSize());
    }

    window.openBuildingImportDialog = function (): void {
        const dialog = document.getElementById("building-import-dialog") as HTMLDialogElement | null;
        if (!dialog) return;
        dialog.showModal();
        requestAnimationFrame(initBuildingImportDialog);
    };

    // A merge conflict keeps the dialog open and re-renders just its body
    // (#building-import-dialog-body, outerHTML) with fresh checkboxes/radios
    // and a fresh #building-import-map placeholder - none of it wired up yet.
    // Scanning the document on every swap (rather than trying to single out
    // that one target) mirrors initAdaptivePagination's own afterSwap handler
    // above; initBuildingImportDialog() already no-ops when its elements
    // aren't present, so this is a safe, idempotent rebuild either way -
    // including the redundant call this causes on the dialog's initial open,
    // which openBuildingImportDialog() above also triggers directly.
    document.body.addEventListener("htmx:afterSwap", () => {
        if (document.getElementById("building-import-map")) initBuildingImportDialog();
    });

    // Dedicated panes keep markup shapes clickable even when a boundary
    // polygon visually overlaps them - without this, both layer groups share
    // the default overlayPane and whichever one's SVG was appended to the DOM
    // last (boundaries, since they load after markup) silently swallows
    // clicks meant for the markup shape underneath. Both stay below the
    // default markerPane (600) so our own arrowhead/text/label markers still
    // render on top of their own shapes, but above the default overlayPane
    // (400) that any unrelated vector layer would otherwise share.
    map.createPane("markupPane")!.style.zIndex = "550";
    map.createPane("boundaryPane")!.style.zIndex = "540";

    // Enable scroll-wheel zoom only after the user has hovered over the map
    // for a moment, so normal page scrolling is not hijacked by a mouse that's
    // merely passing over the map on its way down the page. 750ms erred too
    // far the other way though: a user who actually paused on the map to zoom
    // still had to wait most of a second, past when their scroll gesture had
    // already been read as a page scroll - 350ms is enough to reject a quick
    // pass-through while responding promptly to real zoom intent.
    const SCROLL_ZOOM_ENABLE_DELAY_MS = 350;
    let scrollEnableTimer: ReturnType<typeof setTimeout> | undefined;
    mapEl.addEventListener("mouseenter", () => {
        scrollEnableTimer = setTimeout(() => map.scrollWheelZoom.enable(), SCROLL_ZOOM_ENABLE_DELAY_MS);
    });
    mapEl.addEventListener("mouseleave", () => {
        clearTimeout(scrollEnableTimer);
        map.scrollWheelZoom.disable();
    });

    const markerIcon = L.icon({
        iconUrl: cfg.markerIconUrl,
        shadowUrl: cfg.markerShadowUrl,
        iconSize: [25, 41],
        shadowSize: [41, 41],
        iconAnchor: [12, 41],
        shadowAnchor: [12, 41],
        popupAnchor: [1, -34],
    });
    L.Marker.prototype.options.icon = markerIcon;

    // Main center marker - hidden whenever a boundary polygon exists.
    // Draggable (and self-saving) only when mainMarkerOwnerUuid is provided -
    // Locations don't support relocating their canonical coordinates by
    // dragging.
    let mainMarkerLat = mapCenterLat;
    let mainMarkerLng = mapCenterLng;
    const mainMarker = L.marker([mapCenterLat, mapCenterLng], { draggable: !!cfg.mainMarkerOwnerUuid }).addTo(map);
    if (cfg.mainMarkerOwnerUuid) {
        const savePosition = (lat: number, lng: number, confirmWikiLoss: boolean): Promise<Response> =>
            fetch(`/dashboard/rest/pins/${cfg.mainMarkerOwnerUuid}/`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({
                    latitude: lat.toFixed(6),
                    longitude: lng.toFixed(6),
                    ...(confirmWikiLoss ? { confirm_wiki_loss: true } : {}),
                }),
            });

        mainMarker.on("dragend", () => {
            const pos = mainMarker.getLatLng();
            void (async () => {
                try {
                    let response = await savePosition(pos.lat, pos.lng, false);
                    // 409: the move would drop this pin out of a place whose
                    // community wiki the owner can currently see, and no other
                    // pin of theirs keeps it open. Ask before letting that
                    // happen - the access is silent to lose and easy to miss.
                    if (response.status === 409) {
                        const payload = (await response.json()) as { wikis?: { name: string }[] };
                        const names = (payload.wikis ?? []).map((w) => w.name).join(", ");
                        const confirmed = await confirmAction({
                            title: "Move this pin?",
                            message: names
                                ? `You'll no longer see the community page for ${names}. Moving the pin back inside will restore it.`
                                : "You'll no longer see this place's community page. Moving the pin back inside will restore it.",
                            confirmLabel: "Move anyway",
                            cancelLabel: "Cancel",
                        });
                        if (!confirmed) {
                            mainMarker.setLatLng([mainMarkerLat, mainMarkerLng]);
                            return;
                        }
                        response = await savePosition(pos.lat, pos.lng, true);
                    }
                    if (!response.ok) throw new Error();
                    mainMarkerLat = pos.lat;
                    mainMarkerLng = pos.lng;
                    toast.success("Pin moved.");
                } catch {
                    toast.error("Failed to save new position.");
                    mainMarker.setLatLng([mainMarkerLat, mainMarkerLng]);
                }
            })();
        });
    }

    setTimeout(() => map.invalidateSize(), 300);

    // Re-validate map size on resize and orientation change (important on mobile).
    (() => {
        let resizeTimer: ReturnType<typeof setTimeout> | undefined;
        function onResize(): void {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => map.invalidateSize(), 150);
        }
        window.addEventListener("resize", onResize);
        window.addEventListener("orientationchange", () => setTimeout(() => map.invalidateSize(), 300));
    })();

    // -- Map resize handle (pin detail page only - wiki page has no handle in ---
    // its DOM, so this is a silent no-op there). Dragging the bottom border
    // saves the new height (see PinController.set_map_height) so every pin
    // detail page's map opens at that height going forward. Bounds must match
    // the server's own clamp (_MAP_HEIGHT_MIN_PX/_MAP_HEIGHT_MAX_PX in
    // controllers/pin.py) - the server re-clamps regardless, this is just to
    // avoid a jarring snap once the save round-trips.
    (() => {
        const wrapper = document.getElementById("pin-detail-map-wrapper");
        const handle = document.getElementById("pin-detail-map-resize-handle");
        if (!wrapper || !handle) return;

        const MIN_HEIGHT_PX = 320;
        const MAX_HEIGHT_PX = 1200;
        let startY = 0;
        let startHeight = 0;

        function onPointerMove(e: PointerEvent): void {
            const delta = e.clientY - startY;
            const newHeight = Math.max(MIN_HEIGHT_PX, Math.min(MAX_HEIGHT_PX, startHeight + delta));
            wrapper!.style.height = `${newHeight}px`;
            map.invalidateSize();
        }

        function onPointerUp(): void {
            handle!.classList.remove("is-dragging");
            document.removeEventListener("pointermove", onPointerMove);
            document.removeEventListener("pointerup", onPointerUp);
            const finalHeight = Math.round(wrapper!.getBoundingClientRect().height);
            fetch("/dashboard/map/pin/map-height/", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ height: finalHeight }),
            }).catch(() => {
                toast.error("Failed to save map size.");
            });
        }

        handle.addEventListener("pointerdown", (e: PointerEvent) => {
            e.preventDefault();
            startY = e.clientY;
            startHeight = wrapper!.getBoundingClientRect().height;
            handle!.classList.add("is-dragging");
            document.addEventListener("pointermove", onPointerMove);
            document.addEventListener("pointerup", onPointerUp);
        });
    })();

    // -- Detail pins layer ---------------------------------------------------
    const detailPinColors: Record<string, string> = { parcel: "#0f766e", building: "#6b7280", entrance: "#16a34a", poi: "#d97706", danger: "#dc2626", stair: "#6b7280", elevator: "#6b7280", other: "#7c3aed", location: "#2563eb" };
    const detailPinIcons: Record<string, string> = { parcel: "crop_free", building: "business", entrance: "door_front", poi: "star", danger: "warning", stair: "stairs", elevator: "elevator", other: "info", location: "place" };
    // Cluster group is added to the map directly (not nested in a LayerGroup) -
    // MarkerClusterGroup misses zoom events when it is only a child of another
    // group. Markup stays a sibling; the Details toggle shows/hides both.
    const detailPinLayer = createPinClusterGroup({}, map);
    const markupLayer = L.layerGroup();
    detailPinLayer.addTo(map);
    markupLayer.addTo(map);

    const photoLayer = createPhotoClusterGroup(map).addTo(map);

    function detailsVisible(): boolean {
        return map.hasLayer(detailPinLayer);
    }
    function toggleDetails(): void {
        if (detailsVisible()) {
            map.removeLayer(detailPinLayer);
            map.removeLayer(markupLayer);
        } else {
            detailPinLayer.addTo(map);
            markupLayer.addTo(map);
        }
    }

    // -- Nearby pins layer -----------------------------------------------------
    // This profile's other pins near the one being viewed. Off by default and
    // fetched lazily the first time the layer is turned on (mirrors the main
    // map's "Child Pins" layer - see setChildPinsActive in pages/map/index.html).
    const nearbyLayer = L.layerGroup();
    let nearbyActive = false;
    let nearbyFetchPromise: Promise<void> | null = null;

    function buildNearbyMarker(pin: NearbyPinEntry): L.Marker | null {
        if (pin.latitude == null || pin.longitude == null) return null;
        const iconName = pin.icon || "place";
        const inner = /^[a-z_]+$/.test(iconName) ? `<i class="material-icons nearby-pin-icon">${escHtml(iconName)}</i>` : `<span class="nearby-pin-emoji">${escHtml(iconName)}</span>`;
        const marker = L.marker([pin.latitude, pin.longitude], {
            icon: L.divIcon({ className: "nearby-pin-marker-wrap", html: `<span class="nearby-pin-marker">${inner}</span>`, iconSize: [26, 26], iconAnchor: [13, 13] }),
        });
        marker.bindPopup(`
            <div class="pin-popup nearby-pin-popup">
                <div class="popup-title">${escHtml(pin.name || "Pin")}</div>
                <div class="popup-actions"><a href="${escHtml(pin.url || "#")}" class="view-full-pin">View Details</a></div>
            </div>`);
        return marker;
    }

    function loadNearbyPins(): Promise<void> {
        if (!cfg.nearbyPinsJsonUrl) return Promise.resolve();
        nearbyFetchPromise = fetch(cfg.nearbyPinsJsonUrl, { headers: { "X-Requested-With": "XMLHttpRequest" } })
            .then((r) => (r.ok ? r.json() : { pins: [] }))
            .then((data: { pins?: NearbyPinEntry[] }) => {
                nearbyLayer.clearLayers();
                (data.pins || []).forEach((pin) => {
                    const m = buildNearbyMarker(pin);
                    if (m) nearbyLayer.addLayer(m);
                });
            })
            .catch(() => {
                // Silently ignore - the layer just stays empty, matching the
                // main map's "Child Pins" layer failure behavior.
            });
        return nearbyFetchPromise;
    }

    function setNearbyActive(on: boolean): void {
        if (on === nearbyActive) return;
        nearbyActive = on;
        if (on) {
            nearbyLayer.addTo(map);
            if (!nearbyFetchPromise) loadNearbyPins();
        } else {
            map.removeLayer(nearbyLayer);
        }
    }

    // -- Custom layers: user-created groupings of markup items (e.g. "Tunnels"), --
    // independently toggleable from the base Markup layer. One Leaflet LayerGroup
    // per CustomLayer row, keyed by uuid so markup-toolbar.ts's layerGroupFor hook
    // can route each item's shapes into the right one. See templatetags/
    // map_components.py's custom_layer_button() for the matching "layer-<uuid>"
    // button key rendered into the layers panel.
    const customLayers = readCustomLayers();
    const customLayerGroups = new Map<string, L.LayerGroup>();
    const customLayerToggles: Record<string, CustomLayerToggle> = {};
    customLayers.forEach((layer) => {
        const group = L.layerGroup();
        if (layer.default_visible) group.addTo(map);
        customLayerGroups.set(layer.uuid, group);
        customLayerToggles[`layer-${layer.uuid}`] = {
            isActive: () => map.hasLayer(group),
            toggle: () => (map.hasLayer(group) ? map.removeLayer(group) : group.addTo(map)),
        };
    });

    // Shared layers engine + panel - the exact same component as the main map
    // (see {% map_layers_panel %} in _map_annotations_panels.html). Details and
    // Photos are this page's own layer groups, registered as custom toggles.
    const mapLayersInstance = createMapLayers(map, {
        root: document.getElementById("detail-map-layers"),
        apiKey: cfg.openweathermapApiKey || null,
        defaultBase: cfg.defaultMapView,
        // Same per-profile key the main map, trip and Memories maps use, so the
        // remembered layer is one site-wide choice. Null without a uuid: that
        // makes defaultBase "remember" degrade to street rather than sharing one
        // unscoped bucket between accounts on a shared browser.
        storageKey: cfg.profileUuid ? `ul_layers_v1_${cfg.profileUuid}` : null,
        // Bound below with "Create child pin here" once those helpers exist.
        contextMenu: false,
        onAttribution: (text) => {
            const el = document.getElementById("page-footer-attribution-text");
            if (el) el.textContent = text;
        },
        custom: {
            details: {
                isActive: () => detailsVisible(),
                toggle: toggleDetails,
            },
            photos: {
                isActive: () => map.hasLayer(photoLayer),
                toggle: () => (map.hasLayer(photoLayer) ? map.removeLayer(photoLayer) : photoLayer.addTo(map)),
            },
            nearby: {
                isActive: () => nearbyActive,
                toggle: () => setNearbyActive(!nearbyActive),
            },
            ...customLayerToggles,
        },
    });

    // -- Beta time slider: OpenHistoricalMap overlay ------------------------
    // Below the map, not in .map-bottom-controls (which overlays the tiles).
    // Absent from the DOM entirely unless the viewer has BETA_FEATURES and
    // this location has dated OHM coverage nearby (see readConfig() above and
    // _temporal_imagery_slider.html's server-side gate).
    if (cfg.temporalYears.length > 0) {
        const temporalSliderContainer = document.getElementById("temporal-imagery-slider");
        if (temporalSliderContainer) {
            createTemporalImagerySlider(map, {
                container: temporalSliderContainer,
                years: cfg.temporalYears,
                urlTemplate: cfg.temporalImageryUrlTemplate,
                onError: (message) => toast.error?.(message),
            });
        }
    }

    // Manage Layers dialog changes (create/rename/recolor/reorder/delete) fire
    // this event with the fresh layer list (see custom_layers.py's
    // _render_layer_list HX-Trigger). Re-sync the panel buttons, this map's
    // LayerGroups, and the toggle engine in place so a change made in the
    // dialog shows up immediately - no page reload needed. customLayers is
    // mutated (not reassigned) since the markup layer-assignment dropdown
    // above closes over this same array reference.
    const customLayersMenu = document.querySelector<HTMLElement>("#detail-map-layers [data-layers-menu]");
    const customLayersManageBtn = customLayersMenu?.querySelector<HTMLElement>(".map-layers-manage-btn") ?? null;

    function customLayerButtonLabel(layer: CustomLayerEntry): string {
        const tint = layer.color ? ` style="background:rgba(${hexToRgb(layer.color)},.18)"` : "";
        return `<span class="map-layer-thumb map-layer-thumb--icon"${tint}><i class="material-symbols-outlined">${escHtml(layer.icon || "layers")}</i></span><span>${escHtml(layer.name)}</span>`;
    }

    function syncCustomLayers(fresh: CustomLayerEntry[]): void {
        const freshUuids = new Set(fresh.map((layer) => layer.uuid));

        customLayers.filter((layer) => !freshUuids.has(layer.uuid)).forEach((layer) => {
            const group = customLayerGroups.get(layer.uuid);
            if (group && map.hasLayer(group)) map.removeLayer(group);
            customLayerGroups.delete(layer.uuid);
            customLayersMenu?.querySelector(`[data-map-layer="layer-${layer.uuid}"]`)?.remove();
            customLayers.splice(customLayers.indexOf(layer), 1);
        });

        fresh.forEach((layer) => {
            const key = `layer-${layer.uuid}`;
            const existing = customLayers.find((l) => l.uuid === layer.uuid);
            if (existing) {
                Object.assign(existing, layer);
            } else {
                const group = L.layerGroup();
                if (layer.default_visible) group.addTo(map);
                customLayerGroups.set(layer.uuid, group);
                mapLayersInstance.registerToggle(key, {
                    isActive: () => map.hasLayer(group),
                    toggle: () => (map.hasLayer(group) ? map.removeLayer(group) : group.addTo(map)),
                });
                customLayers.push(layer);
            }

            if (!customLayersMenu) return;
            let btn = customLayersMenu.querySelector<HTMLButtonElement>(`[data-map-layer="${key}"]`);
            if (!btn) {
                btn = document.createElement("button");
                btn.type = "button";
                btn.className = "map-layer-btn";
                btn.dataset.mapLayer = key;
                btn.dataset.layerKind = "custom";
                btn.addEventListener("click", () => mapLayersInstance.toggleCustom(key));
            }
            btn.innerHTML = customLayerButtonLabel(layer);
            btn.setAttribute("aria-label", `Show or hide ${layer.name}`);
            btn.setAttribute("data-tooltip", layer.name);
            btn.setAttribute("data-tooltip-float", "true");
            btn.setAttribute("data-tooltip-pos", "top");
            // Re-insert in server order, right before the "Manage Layers" entry.
            customLayersMenu.insertBefore(btn, customLayersManageBtn);
        });

        mapLayersInstance.syncButtons();
    }

    document.body.addEventListener("ul:custom-layers-changed", (e) => {
        syncCustomLayers((e as CustomEvent).detail?.layers || []);
    });

    // -- Georeferenced image overlays -------------------------------------
    // Historical sheets (Sanborn maps, site plans) the user has aligned onto
    // this map by dragging their four corners. Each gets its own toggle in the
    // layers panel unless it was assigned to a custom layer, in which case it
    // shows and hides with that layer's other markup instead.
    const overlayCornersTemplate = cfg.overlayCornersUrlTemplate || "";
    const imageOverlays = overlayCornersTemplate
        ? createMapImageOverlays(L, map, {
            cornersUrl: (uuid) => overlayCornersTemplate.replace("00000000-0000-0000-0000-000000000000", uuid),
            csrfToken: getCsrfToken(),
            onError: (message) => toast.error?.(message),
        })
        : null;

    function overlayToggleKey(uuid: string): string {
        return `overlay-${uuid}`;
    }

    function syncMapOverlays(entries: MapOverlayEntry[]): void {
        if (!imageOverlays) return;
        const standalone = entries.filter((entry) => !entry.layer_uuid);
        imageOverlays.sync(entries);

        // An overlay assigned to a custom layer follows that layer's toggle
        // rather than carrying one of its own. The layer's own LayerGroup holds
        // markup, not this <img> (it lives in the overlay pane), so visibility
        // is mirrored from whether that group is on the map.
        entries
            .filter((entry) => entry.layer_uuid)
            .forEach((entry) => {
                const group = customLayerGroups.get(entry.layer_uuid as string);
                imageOverlays.setVisible(entry.uuid, !!group && map.hasLayer(group));
            });

        // An overlay inside a custom layer follows that layer's toggle; the
        // rest need one of their own so they can be turned off individually.
        standalone.forEach((entry) => {
            const key = overlayToggleKey(entry.uuid);
            mapLayersInstance.registerToggle(key, {
                isActive: () => imageOverlays.isVisible(entry.uuid),
                toggle: () => imageOverlays.setVisible(entry.uuid, !imageOverlays.isVisible(entry.uuid)),
            });
            if (!customLayersMenu) return;
            let btn = customLayersMenu.querySelector<HTMLButtonElement>(`[data-map-layer="${key}"]`);
            if (!btn) {
                btn = document.createElement("button");
                btn.type = "button";
                btn.className = "map-layer-btn";
                btn.dataset.mapLayer = key;
                btn.dataset.layerKind = "custom";
                btn.addEventListener("click", () => mapLayersInstance.toggleCustom(key));
                customLayersMenu.insertBefore(btn, customLayersManageBtn);
            }
            btn.innerHTML = `<span class="map-layer-thumb map-layer-thumb--icon"><i class="material-symbols-outlined">image</i></span><span>${escHtml(entry.name || "Image overlay")}</span>`;
            btn.setAttribute("aria-label", `Show or hide ${entry.name || "image overlay"}`);
        });

        // Buttons for overlays that no longer exist (or moved into a layer).
        const liveKeys = new Set(standalone.map((entry) => overlayToggleKey(entry.uuid)));
        customLayersMenu?.querySelectorAll<HTMLElement>('[data-map-layer^="overlay-"]').forEach((btn) => {
            if (!liveKeys.has(btn.dataset.mapLayer || "")) btn.remove();
        });
        mapLayersInstance.syncButtons();
    }

    // Keep layer-assigned overlays in step when their layer is toggled. Bound
    // to the map rather than to each toggle button so it also catches a layer
    // turned on from the Manage Layers dialog or by default_visible.
    function syncOverlaysToLayers(): void {
        if (!imageOverlays) return;
        customLayerGroups.forEach((group, layerUuid) => {
            const visible = map.hasLayer(group);
            imageOverlays.uuidsInLayer(layerUuid).forEach((uuid) => imageOverlays.setVisible(uuid, visible));
        });
    }

    map.on("layeradd layerremove", syncOverlaysToLayers);

    syncMapOverlays(readMapOverlays());

    document.body.addEventListener("ul:map-overlays-changed", (e) => {
        syncMapOverlays((e as CustomEvent).detail?.overlays || []);
    });

    // Hooks the manage-overlays dialog calls by name (it is server-rendered
    // HTML, so it can't import from this module) - shared with the floorplan
    // editor's own copy of this dialog, see wireManageOverlaysDialog's docstring.
    if (imageOverlays) {
        wireManageOverlaysDialog({
            map,
            control: imageOverlays,
            onAlignStart: () => (document.getElementById("map-overlays-dialog") as HTMLDialogElement | null)?.close(),
        });
    }

    // URL base for detail pin edit/delete: strip the placeholder UUID off the end.
    const dpEditBase = cfg.detailPinEditUrlTemplate.replace("00000000-0000-0000-0000-000000000000/", "");

    let detailPins: DetailPinEntry[] = [];
    let highlightedDpUuid: string | null = null;
    let photoPanelItems: PhotoPanelItem[] = [];
    const photoMarkers: Record<number, { marker: L.Marker; url: string; lat: number; lng: number; highlighted: boolean }> = {};

    function hexToRgb(hex: string): string {
        const r = Number.parseInt(hex.slice(1, 3), 16);
        const g = Number.parseInt(hex.slice(3, 5), 16);
        const b = Number.parseInt(hex.slice(5, 7), 16);
        return `${r},${g},${b}`;
    }

    // Mirrors the `is_material_icon` Django filter (dashboard_tags.py): a bare
    // ligature name renders fine inside a material-icons span, but an
    // uploaded custom icon's URL rendered the same way shows up as literal
    // text - far wider than one glyph, which stretched the fixed-size
    // flex circle into an oval and threw off iconAnchor's centered-square
    // math. A URL gets an <img> sized to match instead; anything else
    // (an emoji) is plain text, un-fonted so it isn't mistaken for a glyph.
    const MATERIAL_ICON_NAME = /^[a-z0-9_]+$/;
    const ICON_URL = /^(?:https?:)?\//;

    function detailIcon(dp: Partial<DetailPinEntry>, highlighted?: boolean): L.DivIcon {
        const pinType = dp.pin_type || "location";
        const color = dp.color || detailPinColors[pinType] || "#2563eb";
        const icon = dp.icon || detailPinIcons[pinType] || "place";
        const bgColor = dp.bg_color || null;
        const bgOp = bgColor ? (dp.bg_opacity != null ? dp.bg_opacity : 80) / 100 : 0;
        const bdColor = dp.border_color || null;
        const bdOp = bdColor ? (dp.border_opacity != null ? dp.border_opacity : 100) / 100 : 0;

        const hasCircle = !!(bgColor || bdColor);
        const size = highlighted ? 32 : 24;
        const pad = hasCircle ? 5 : 0;
        const total = size + pad * 2;

        const bgStyle = bgColor ? `background:rgba(${hexToRgb(bgColor)},${bgOp});` : "";
        const bdStyle = bdColor ? `border:2px solid rgba(${hexToRgb(bdColor)},${bdOp});` : "";
        const ring = highlighted ? `<span style="position:absolute;inset:-5px;border:2.5px solid ${color};border-radius:50%;opacity:.55;pointer-events:none;"></span>` : "";

        const iconHtml = ICON_URL.test(icon)
            ? `<img class="detail-map-icon-img" src="${icon}" alt="" style="width:${size}px;height:${size}px;">`
            : MATERIAL_ICON_NAME.test(icon)
              ? `<span class="material-icons detail-map-icon" style="color:${color};font-size:${size}px;line-height:1;">${icon}</span>`
              : `<span class="detail-map-icon" style="font-size:${size}px;line-height:1;">${icon}</span>`;

        return L.divIcon({
            className: "",
            html: `<span style="position:relative;display:inline-flex;align-items:center;justify-content:center;border-radius:50%;${bgStyle}${bdStyle}padding:${pad}px;">${ring}${iconHtml}</span>`,
            iconSize: [total, total],
            iconAnchor: [total / 2, total],
            popupAnchor: [0, -total - 2],
            tooltipAnchor: [0, -total / 2],
        });
    }

    function highlightDetailPin(uuid: string): void {
        clearDetailPinHighlight();
        highlightedDpUuid = uuid;
        const dp = detailPins.find((d) => d.uuid === uuid);
        if (!dp?.marker) return;
        dp.marker.setIcon(detailIcon(dp, true));
        map.panTo(dp.marker.getLatLng());
        document.querySelectorAll<HTMLElement>(".detail-pin-list-item").forEach((li) => {
            li.classList.toggle("is-highlighted", li.dataset.uuid === uuid);
        });
    }

    function clearDetailPinHighlight(): void {
        if (highlightedDpUuid) {
            const dp = detailPins.find((d) => d.uuid === highlightedDpUuid);
            if (dp?.marker) dp.marker.setIcon(detailIcon(dp, false));
            highlightedDpUuid = null;
        }
        document.querySelectorAll(".detail-pin-list-item").forEach((li) => li.classList.remove("is-highlighted"));
    }

    function refreshPanelHeader(): void {
        const handle = document.getElementById("detail-pin-list-handle");
        const countLabel = document.getElementById("detail-pin-count-label");
        const total = detailPins.length + toolbar.getMarkupItems().length + photoPanelItems.length;
        if (countLabel) countLabel.textContent = `${total} Item${total === 1 ? "" : "s"}`;
        // Nothing to show yet (brand-new pin: no detail pins, markup, or photos) -
        // hide the edge handle entirely rather than exposing an empty sidebar.
        if (handle) handle.style.display = total ? "" : "none";
        refreshDetailPinSelectButton();
    }

    function buildDetailList(): void {
        const ul = document.getElementById("detail-pin-list-ul");
        if (!ul) return;
        refreshPanelHeader();
        ul.innerHTML = "";

        // -- Pin items --------------------------------------------------------
        detailPins.forEach((dp) => {
            const color = dp.color || detailPinColors[dp.pin_type] || "#2563eb";
            const icon = dp.icon || detailPinIcons[dp.pin_type] || "place";
            const li = document.createElement("li");
            li.className = "detail-pin-list-item";
            li.dataset.uuid = dp.uuid;
            li.dataset.kind = "pin";
            // Nested entries (from a child pin) are display-only: no delete,
            // clicking highlights on the map instead of opening the editor.
            const meta = dp.owner_name ? `<span class="detail-pin-list-item-meta">in ${escHtml(dp.owner_name)}</span>` : dp.added_by ? `<span class="detail-pin-list-item-meta">by ${dp.is_mine ? "you" : escHtml(dp.added_by)}</span>` : "";
            li.innerHTML = `
                <span class="material-icons detail-pin-list-item-icon" style="color:${escHtml(color)}">${escHtml(icon)}</span>
                <span class="detail-pin-list-item-name">${escHtml(dp.name)}</span>
                ${meta}
                ${dp.owner_name ? "" : `<button type="button" class="detail-pin-list-item-delete" title="Delete pin"><i class="material-symbols-outlined">close</i></button>`}`;
            li.addEventListener("click", (e) => {
                if ((e.target as HTMLElement).closest(".detail-pin-list-item-delete")) return;
                highlightDetailPin(dp.uuid);
                if (!dp.owner_name) openDetailPinEditDialog(dp);
            });
            li.querySelector(".detail-pin-list-item-delete")?.addEventListener("click", async (e) => {
                e.stopPropagation();
                if (!(await confirmAction({ title: "Delete Pin", message: `Delete "${dp.name}"?`, confirmLabel: "Delete" }))) return;
                fetch(`${dpEditBase}${dp.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } })
                    .then((r) => {
                        if (!r.ok) throw new Error();
                    })
                    .then(() => {
                        toast.success("Detail pin deleted.");
                        loadDetailPins();
                        fetchBoundaries(0);
                    })
                    .catch(() => toast.error("Failed to delete detail pin."));
            });
            ul.appendChild(li);
        });

        // -- Markup items -------------------------------------------------------
        const markupIcon: Record<string, string> = { line: "show_chart", arrow: "arrow_forward", text: "title", square: "crop_square", circle: "circle", polygon: "format_shapes" };
        toolbar.getMarkupItems().forEach((item) => {
            const li = document.createElement("li");
            li.className = "detail-pin-list-item";
            li.dataset.uuid = item.uuid;
            li.dataset.kind = "markup";
            const displayName = item.label || item.markup_type.charAt(0).toUpperCase() + item.markup_type.slice(1);
            const ownerMeta = item.owner_name ? `<span class="detail-pin-list-item-meta">in ${escHtml(item.owner_name)}</span>` : "";
            // Inline layer picker - the "move onto/off a layer without recreating
            // it" affordance: changing it calls toolbar.setItemLayer, which just
            // updates PinMarkup.layer and re-renders in place.
            const layerPicker =
                !item.owner_name && customLayers.length
                    ? `<select class="detail-pin-list-item-layer" title="Layer" aria-label="Layer">
                        <option value=""${item.layer_uuid ? "" : " selected"}>No layer</option>
                        ${customLayers.map((layer) => `<option value="${escHtml(layer.uuid)}"${item.layer_uuid === layer.uuid ? " selected" : ""}>${escHtml(layer.name)}</option>`).join("")}
                       </select>`
                    : "";
            li.innerHTML = `
                <span class="material-icons detail-pin-list-item-icon" style="color:${escHtml(item.color)}">${escHtml(markupIcon[item.markup_type] || "edit")}</span>
                <span class="detail-pin-list-item-name">${escHtml(displayName)}</span>
                ${ownerMeta}
                ${layerPicker}
                ${item.owner_name ? "" : `<button type="button" class="detail-pin-list-item-delete" title="Delete"><i class="material-symbols-outlined">close</i></button>`}`;
            li.addEventListener("click", (e) => {
                if ((e.target as HTMLElement).closest(".detail-pin-list-item-delete, .detail-pin-list-item-layer")) return;
                if (item.owner_name) return; // child-pin markup is edited from its own page
                toolbar.openMarkupEditDialog(item);
            });
            li.querySelector(".detail-pin-list-item-layer")?.addEventListener("click", (e) => e.stopPropagation());
            li.querySelector(".detail-pin-list-item-layer")?.addEventListener("change", (e) => {
                toolbar.setItemLayer(item.uuid, (e.target as HTMLSelectElement).value || null);
            });
            li.querySelector(".detail-pin-list-item-delete")?.addEventListener("click", async (e) => {
                e.stopPropagation();
                if (!(await confirmAction({ title: "Delete Item", message: `Delete this ${item.markup_type}?`, confirmLabel: "Delete" }))) return;
                fetch(`${cfg.markupEditUrlTemplate.replace("00000000-0000-0000-0000-000000000000/", "")}${item.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } })
                    .then((r) => {
                        if (!r.ok) throw new Error();
                    })
                    .then(() => {
                        toast.success("Markup deleted.");
                        toolbar.loadMarkup();
                    })
                    .catch(() => toast.error("Failed to delete markup."));
            });
            ul.appendChild(li);
        });
    }

    // Detail pin list sidebar toggle - same collapse/expand mechanic as the
    // main map's #pin-list-panel/.pin-list-handle (window._togglePinListPanel).
    function toggleDetailPinListPanel(): void {
        const panel = document.getElementById("detail-pin-list-panel");
        const handle = document.getElementById("detail-pin-list-handle");
        if (!panel) return;
        const isOpen = panel.classList.toggle("open");
        if (handle) {
            handle.classList.toggle("open", isOpen);
            handle.setAttribute("aria-expanded", String(isOpen));
            const icon = handle.querySelector(".material-symbols-outlined, .material-icons");
            if (icon) icon.textContent = isOpen ? "chevron_left" : "chevron_right";
        }
    }
    window._toggleDetailPinListPanel = toggleDetailPinListPanel;

    // Satellite/street-view carousel controls (satellite_view.html / street_view.html
    // fragments, HTMX-swapped into this page). Defined here - not inside those
    // fragments' own <script> tags - because HTMX inserts a swapped fragment's DOM
    // (including <img> tags, which start loading immediately) before it executes any
    // <script> tags found within that same fragment: a fast-failing image (cached
    // 404, empty src, ...) can fire its onerror before a same-fragment <script>
    // defining the handler has run, throwing "X is not defined". Defining these
    // globals here (this module loads and runs on page load, well before any panel
    // fragment can be swapped in) guarantees they exist before any swap can happen.
    // Remembers which provider's slide the user last flipped to (by its
    // display-name `source`, e.g. "Esri World Imagery"), so the next pin
    // detail page's satellite carousel opens on that same provider instead
    // of always starting over at the default order - see _satShowRemembered.
    const SAT_LAST_SOURCE_KEY = "ul_sat_last_source";

    function _satRememberSource(source: string): void {
        if (!source) return;
        try {
            localStorage.setItem(SAT_LAST_SOURCE_KEY, source);
        } catch {
            /* private browsing / storage disabled - just don't remember it */
        }
    }

    function _satLastSource(): string | null {
        try {
            return localStorage.getItem(SAT_LAST_SOURCE_KEY);
        } catch {
            return null;
        }
    }

    let _satIdx = 0;
    function _satSlides(): HTMLElement[] {
        const c = document.getElementById("sat-carousel");
        return c ? Array.from(c.querySelectorAll<HTMLElement>(".sat-slide")) : [];
    }
    function _satShow(idx: number): void {
        const slides = _satSlides();
        if (!slides.length) return;
        _satIdx = ((idx % slides.length) + slides.length) % slides.length;
        slides.forEach((s, i) => s.classList.toggle("is-active", i === _satIdx));
        const active = slides[_satIdx];
        if (!active) return;
        const source = document.querySelector<HTMLElement>("#sat-carousel .sat-source");
        const date = document.querySelector<HTMLElement>("#sat-carousel .sat-date");
        const detail = document.querySelector<HTMLElement>("#sat-carousel .sat-detail");
        if (source) source.textContent = active.dataset.source || "";
        if (date) date.textContent = active.dataset.date || "";
        if (detail) detail.textContent = active.dataset.detail || "";
        _satRememberSource(active.dataset.source || "");
        _satRebuildDots(slides.length);
    }
    function _satRebuildDots(count: number): void {
        // Prev/next only make sense with more than one slide - the server
        // already omits them from the initial render when there's just one,
        // but a broken image can drop the count further at runtime
        // (_satRemoveSlide), so hide them here too if that happens.
        const prev = document.querySelector<HTMLElement>("#sat-carousel .sat-prev");
        const next = document.querySelector<HTMLElement>("#sat-carousel .sat-next");
        if (prev) prev.hidden = count <= 1;
        if (next) next.hidden = count <= 1;
        const el = document.getElementById("sat-dots");
        if (!el) return;
        el.innerHTML = "";
        for (let i = 0; i < count; i++) {
            const dot = document.createElement("button");
            dot.type = "button";
            dot.className = "sat-dot" + (i === _satIdx ? " is-active" : "");
            dot.setAttribute("aria-label", `Slide ${i + 1}`);
            dot.addEventListener("click", () => _satShow(i));
            el.appendChild(dot);
        }
    }
    window._satRemoveSlide = function (img: HTMLImageElement): void {
        const slide = img.closest<HTMLElement>(".sat-slide");
        if (!slide) return;
        const wasActive = slide.classList.contains("is-active");
        slide.remove();
        const slides = _satSlides();
        if (!slides.length) {
            const c = document.getElementById("sat-carousel");
            if (c) {
                c.innerHTML =
                    '<div class="view-unavailable"><i class="material-symbols-outlined">broken_image</i>' +
                    "<span>No satellite imagery available for this location.</span></div>";
            }
            return;
        }
        if (wasActive) _satIdx = Math.max(0, Math.min(_satIdx, slides.length - 1));
        _satShow(_satIdx);
    };
    window._satPrev = function () {
        _satShow(_satIdx - 1);
    };
    window._satNext = function () {
        _satShow(_satIdx + 1);
    };
    window._satShowRemembered = function (): void {
        const slides = _satSlides();
        if (!slides.length) return;
        const lastSource = _satLastSource();
        const idx = lastSource ? slides.findIndex((s) => s.dataset.source === lastSource) : -1;
        _satShow(idx >= 0 ? idx : 0);
    };
    window._satShow = _satShow;

    // The interactive embed (see street_view.html's .sv-embed) is a cross-origin
    // iframe: Google renders its own "no imagery here" state (a blank/black scene)
    // inside it, which our JS has no way to read to detect - there's no success
    // signal either, so this can only be a manually-triggered swap
    // (.sv-embed-fallback-btn below), never an automatic one on a timer with
    // nothing to cancel it on success.
    function _svSwapToStatic(slide: HTMLElement): void {
        const iframe = slide.querySelector<HTMLIFrameElement>(".sv-embed");
        const staticImg = slide.querySelector<HTMLImageElement>(".sv-img--fallback");
        const btn = slide.querySelector<HTMLButtonElement>(".sv-embed-fallback-btn");
        if (iframe) iframe.hidden = true;
        if (staticImg) staticImg.hidden = false;
        if (btn) btn.hidden = true;
    }

    let _svIdx = 0;
    function _svSlides(): HTMLElement[] {
        const c = document.getElementById("sv-carousel");
        return c ? Array.from(c.querySelectorAll<HTMLElement>(".sv-slide")) : [];
    }
    function _svShow(idx: number): void {
        const slides = _svSlides();
        if (!slides.length) return;
        _svIdx = ((idx % slides.length) + slides.length) % slides.length;
        slides.forEach((s, i) => s.classList.toggle("is-active", i === _svIdx));
        const active = slides[_svIdx];
        if (!active) return;
        const source = document.querySelector<HTMLElement>("#sv-carousel .sv-source");
        const date = document.querySelector<HTMLElement>("#sv-carousel .sv-date");
        const heading = document.querySelector<HTMLElement>("#sv-carousel .sv-heading");
        if (source) source.textContent = active.dataset.source || "";
        if (date) date.textContent = active.dataset.date || "";
        if (heading) heading.textContent = active.dataset.heading !== undefined ? `⇨ ${active.dataset.heading}°` : "";
        _svRebuildDots(slides.length);
    }
    function _svRebuildDots(count: number): void {
        // Prev/next only make sense with more than one slide - the server
        // already omits them from the initial render when there's just one,
        // but a broken image can drop the count further at runtime
        // (_svRemoveSlide), so hide them here too if that happens.
        const prev = document.querySelector<HTMLElement>("#sv-carousel .sv-prev");
        const next = document.querySelector<HTMLElement>("#sv-carousel .sv-next");
        if (prev) prev.hidden = count <= 1;
        if (next) next.hidden = count <= 1;
        const el = document.getElementById("sv-dots");
        if (!el) return;
        el.innerHTML = "";
        for (let i = 0; i < count; i++) {
            const dot = document.createElement("button");
            dot.type = "button";
            dot.className = "sv-dot" + (i === _svIdx ? " is-active" : "");
            dot.setAttribute("aria-label", `Slide ${i + 1}`);
            dot.addEventListener("click", () => _svShow(i));
            el.appendChild(dot);
        }
    }
    window._svShowStaticFallback = function (btn: HTMLButtonElement): void {
        const slide = btn.closest<HTMLElement>(".sv-slide");
        if (!slide) return;
        _svSwapToStatic(slide);
    };
    window._svRemoveSlide = function (img: HTMLImageElement): void {
        const slide = img.closest<HTMLElement>(".sv-slide");
        if (!slide) return;
        const wasActive = slide.classList.contains("is-active");
        slide.remove();
        const slides = _svSlides();
        if (!slides.length) {
            const c = document.getElementById("sv-carousel");
            if (c) {
                c.innerHTML =
                    '<div class="view-unavailable"><i class="material-symbols-outlined">broken_image</i>' +
                    "<span>No street-level imagery available for this location.</span></div>";
            }
            return;
        }
        if (wasActive) _svIdx = Math.max(0, Math.min(_svIdx, slides.length - 1));
        _svShow(_svIdx);
    };
    window._svPrev = function () {
        _svShow(_svIdx - 1);
    };
    window._svNext = function () {
        _svShow(_svIdx + 1);
    };
    window._svShow = _svShow;

    // Promotes a direct child pin to take this pin's place as the parent -
    // the child becomes the parent, and this pin becomes its child. Only
    // ever offered for Pin-backed direct children (entry.slug set, no
    // owner_name), same gating as the Edit button below.
    async function promotePinToParent(entry: DetailPinEntry): Promise<void> {
        if (!entry.slug || !entry.url) return;
        if (!(await confirmAction({ title: "Make this the parent pin?", message: `"${entry.name || "This pin"}" will become the parent, and the current pin will become its child. Everything else - name, notes, reviews, photos, visit history - stays with each pin.`, confirmLabel: "Swap" }))) {
            return;
        }
        fetch(`/dashboard/map/pin/${encodeURIComponent(entry.slug)}/swap-parent/`, {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken() },
        })
            .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok) {
                    toast.error(data.error || "Could not swap these pins.");
                    return;
                }
                toast.success("Pins swapped - taking you to the new parent pin.");
                // The pin this popup was on is no longer the top of this
                // hierarchy - land on the new parent's own detail page.
                window.location.href = entry.url as string;
            })
            .catch(() => toast.error("Could not swap these pins."));
    }

    // Popup shown when a child pin's marker is clicked: name, which child pin it
    // belongs to (for nested entries), and a link to that pin's own detail
    // page - plus Edit/promote-to-parent shortcuts for this pin's own direct
    // children (no hover tooltip - the click popup already covers this, and a
    // separate hover tooltip here renders unreadably in dark mode).
    function detailPinPopupContent(entry: DetailPinEntry): HTMLElement {
        const el = document.createElement("div");
        el.className = "pin-popup child-pin-popup";
        const owner = entry.owner_name ? `<div class="popup-child-parent"><i class="material-symbols-outlined">subdirectory_arrow_right</i> Inside ${escHtml(entry.owner_name)}</div>` : "";
        el.innerHTML = `
            <div class="popup-title">${escHtml(entry.name || "Child pin")}</div>
            ${owner}
            ${entry.description ? `<div class="popup-desc">${escHtml(entry.description)}</div>` : ""}
            <div class="popup-actions">
                ${entry.url ? `<a href="${escHtml(entry.url)}" class="view-full-pin">View Details</a>` : ""}
            </div>`;
        if (!entry.owner_name) {
            const actions = el.querySelector(".popup-actions")!;
            const promoteBtn = document.createElement("button");
            promoteBtn.type = "button";
            promoteBtn.className = "promote-pin-button";
            promoteBtn.title = "Make this the parent pin";
            promoteBtn.innerHTML = '<i class="material-symbols-outlined">swap_vert</i>';
            promoteBtn.addEventListener("click", () => {
                map.closePopup();
                void promotePinToParent(entry);
            });
            actions.appendChild(promoteBtn);

            const editBtn = document.createElement("button");
            editBtn.type = "button";
            editBtn.className = "edit-pin-button";
            editBtn.title = "Edit child pin";
            editBtn.innerHTML = '<i class="material-symbols-outlined">edit</i>';
            editBtn.addEventListener("click", () => {
                map.closePopup();
                openDetailPinEditDialog(entry);
            });
            actions.appendChild(editBtn);

            const deleteBtn = document.createElement("button");
            deleteBtn.type = "button";
            deleteBtn.className = "delete-button";
            deleteBtn.title = "Delete child pin";
            deleteBtn.innerHTML = '<i class="material-symbols-outlined">delete</i>';
            deleteBtn.addEventListener("click", async () => {
                map.closePopup();
                if (!(await confirmAction({ title: "Delete Pin", message: `Delete "${entry.name || "this pin"}"?`, confirmLabel: "Delete" }))) return;
                fetch(`${dpEditBase}${entry.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } })
                    .then((r) => {
                        if (!r.ok) throw new Error();
                    })
                    .then(() => {
                        toast.success("Detail pin deleted.");
                        loadDetailPins();
                        fetchBoundaries(0);
                    })
                    .catch(() => toast.error("Failed to delete detail pin."));
            });
            actions.appendChild(deleteBtn);
        }
        return el;
    }

    function loadDetailPins(): void {
        fetch(cfg.detailPinsJsonUrl)
            .then((r) => {
                // Without this, a server error whose body still parses as
                // JSON (or one with no "detail_pins" key) fell through to
                // the success branch below, which unconditionally clears
                // the existing layer - a transient failure wiped every pin
                // already on the map rather than leaving them alone.
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then((data) => {
                detailPinLayer.clearLayers();
                highlightedDpUuid = null;
                detailPins = [];
                (data.detail_pins || []).forEach((dp: any) => {
                    if (!dp.latitude || !dp.longitude) return;
                    const entry: DetailPinEntry = {
                        uuid: dp.uuid,
                        slug: dp.slug,
                        url: dp.url,
                        owner_name: dp.owner_name,
                        name: dp.name,
                        pin_type: dp.pin_type,
                        icon: dp.icon,
                        color: dp.color,
                        bg_color: dp.bg_color || "",
                        bg_opacity: dp.bg_opacity,
                        border_color: dp.border_color || "",
                        border_opacity: dp.border_opacity,
                        description: dp.description || "",
                        added_by: dp.added_by || "",
                        is_mine: !!dp.is_mine,
                        latitude: dp.latitude,
                        longitude: dp.longitude,
                        marker: null,
                    };
                    // Nested entries (owner_name set) belong to a child pin and are
                    // display-only here - not draggable, edited on their own page.
                    // No hover tooltip - the click popup below already covers name/
                    // owner/actions, and a separate hover tooltip here renders
                    // unreadably in dark mode (dark text on a dark background).
                    const marker = L.marker([dp.latitude, dp.longitude], { icon: detailIcon(entry), draggable: !entry.owner_name && !detailSelectMode });
                    if (entry.url) {
                        marker.bindPopup(detailPinPopupContent(entry));
                    } else {
                        // Wiki child markers have no personal detail page - keep the
                        // direct click-to-edit behavior there.
                        marker.on("click", () => openDetailPinEditDialog(entry));
                    }
                    // Select-mode click toggles selection instead of opening the popup
                    // or the editor. Ctrl/cmd-click on a second pin of this type
                    // *enters* select mode with both selected, matching the main map.
                    marker.on("click", (e) => {
                        handleDetailPinSelectClick(entry, marker, e);
                    });
                    if (!entry.owner_name) {
                        reclusterOnDrag(marker, detailPinLayer, map, () => detailSelectMode);
                    }
                    marker.on("dragend", () => {
                        returnToCluster(marker, detailPinLayer, map);
                        const pos = marker.getLatLng();
                        fetch(`${dpEditBase}${dp.uuid}/`, {
                            method: "POST",
                            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                            body: JSON.stringify({ latitude: pos.lat.toFixed(6), longitude: pos.lng.toFixed(6) }),
                        })
                            .then((r) => {
                                if (!r.ok) throw new Error();
                                return r.json();
                            })
                            .then(() => {
                                entry.latitude = pos.lat;
                                entry.longitude = pos.lng;
                                toast.success("Pin moved.");
                                fetchBoundaries(0);
                            })
                            .catch(() => {
                                toast.error("Failed to save new position.");
                                marker.setLatLng([entry.latitude, entry.longitude]);
                                returnToCluster(marker, detailPinLayer, map);
                            });
                    });
                    marker.addTo(detailPinLayer);
                    entry.marker = marker;
                    detailPins.push(entry);
                });
                buildDetailList();
                syncDpSelectionClasses();
            })
            .catch((err) => {
                console.warn("Could not load detail pins:", err);
                toast.error("Could not load your pins.");
            });
    }

    // -- Detail pin multi-select: act on several child pins at once ------------
    // Pin-only (cfg.pinSlug is empty on the wiki page, which shares this module
    // but has no reparentable Pin-backed detail pins to act on) - the button is
    // removed there. Nested entries (entry.owner_name set) are display-only and
    // never selectable, matching their existing non-draggable/non-editable state.
    let detailSelectMode = false;
    const selectedDpUuids = new Set<string>();
    const dpSelectMemory = new AdditiveSelectMemory();

    function canDetailMultiSelect(): boolean {
        return !!cfg.pinSlug && detailSelectableEntries().length > 0;
    }

    function setDetailPinDragging(enabled: boolean): void {
        detailSelectableEntries().forEach((dp) => {
            if (enabled) dp.marker?.dragging?.enable();
            else dp.marker?.dragging?.disable();
        });
    }

    function syncDpSelectionClasses(): void {
        if (!selectedDpUuids.size) return;
        selectedDpUuids.forEach((uuid) => {
            detailPins.find((d) => d.uuid === uuid)?.marker?.getElement()?.classList.add("is-selected");
        });
    }
    detailPinLayer.on("animationend spiderfied unspiderfied layeradd", syncDpSelectionClasses);
    map.on("zoomend moveend", () => {
        requestAnimationFrame(syncDpSelectionClasses);
    });

    /**
     * Consume a marker click for multi-select when appropriate.
     *
     * Returns true when the click should not also open a popup / editor:
     * already in select mode, or a ctrl/cmd-click that just entered it.
     */
    function handleDetailPinSelectClick(entry: DetailPinEntry, marker: L.Marker, event: L.LeafletMouseEvent): boolean {
        if (entry.owner_name || !canDetailMultiSelect()) return false;
        const additive = isAdditiveClick(event);
        if (detailSelectMode) {
            marker.closePopup();
            L.DomEvent.stop(event);
            toggleDpSelection(entry.uuid);
            return true;
        }
        if (additive) {
            marker.closePopup();
            L.DomEvent.stop(event);
            map.closePopup();
            enterDetailPinSelectMode();
            dpSelectMemory.idsForAdditiveStart(entry.uuid).forEach((uuid) => {
                if (!selectedDpUuids.has(uuid) && detailPins.some((d) => d.uuid === uuid && !d.owner_name)) toggleDpSelection(uuid);
            });
            dpSelectMemory.clear();
            return true;
        }
        dpSelectMemory.remember(entry.uuid);
        return false;
    }

    function detailSelectableEntries(): DetailPinEntry[] {
        return detailPins.filter((d) => !d.owner_name);
    }

    function refreshDetailPinSelectButton(): void {
        const btn = document.getElementById("select-detail-pins-button") as HTMLButtonElement | null;
        if (!btn) return;
        if (!cfg.pinSlug) {
            btn.remove();
            return;
        }
        const hasSelectable = detailSelectableEntries().length > 0;
        btn.disabled = !hasSelectable;
        btn.setAttribute("data-tooltip", hasSelectable ? "Select multiple child pins. Ctrl+click a second pin to start." : "This pin has no child pins to select");
        if (!hasSelectable && detailSelectMode) exitDetailPinSelectMode();
    }

    function toggleDetailPinSelectMode(): void {
        if (detailSelectMode) exitDetailPinSelectMode();
        else enterDetailPinSelectMode();
    }
    window.toggleDetailPinSelectMode = toggleDetailPinSelectMode;

    function enterDetailPinSelectMode(): void {
        if (detailSelectMode || !canDetailMultiSelect()) return;
        detailSelectMode = true;
        document.getElementById("select-detail-pins-button")?.classList.add("active");
        document.getElementById("map")?.classList.add("select-mode");
        map.dragging.disable();
        setDetailPinDragging(false);
        // Disabling dragging makes Leaflet hand touch panning back to the
        // browser, which would scroll the page instead of letting the rubber
        // band consume the gesture.
        map.getContainer().style.touchAction = "none";
    }

    function exitDetailPinSelectMode(): void {
        if (!detailSelectMode) return;
        detailSelectMode = false;
        document.getElementById("select-detail-pins-button")?.classList.remove("active");
        document.getElementById("map")?.classList.remove("select-mode");
        map.dragging.enable();
        setDetailPinDragging(true);
        map.getContainer().style.touchAction = "";
        dpSelectMemory.clear();
        clearDpSelection();
    }

    function toggleDpSelection(uuid: string): void {
        if (selectedDpUuids.has(uuid)) selectedDpUuids.delete(uuid);
        else selectedDpUuids.add(uuid);
        const dp = detailPins.find((d) => d.uuid === uuid);
        dp?.marker?.getElement()?.classList.toggle("is-selected", selectedDpUuids.has(uuid));
        renderDetailBulkToolbar();
    }

    function clearDpSelection(): void {
        selectedDpUuids.forEach((uuid) => {
            detailPins.find((d) => d.uuid === uuid)?.marker?.getElement()?.classList.remove("is-selected");
        });
        selectedDpUuids.clear();
        window.ulBulkToolbar?.clear("detailpins");
    }

    function renderDetailBulkToolbar(): void {
        const n = selectedDpUuids.size;
        window.ulBulkToolbar?.sync(
            "detailpins",
            n,
            n
                ? {
                    ...(cfg.detailPinsBulkEditUrl ? { edit: openSelectedDpBulkEditDialog } : {}),
                    promote: doPromoteSelectedDp,
                    // "Share" and "Send to wiki" are pin-only - the wiki page shares
                    // this same module for its own (community) child-wiki toolbar,
                    // which has neither concept.
                    ...(cfg.pinShareDialogUrl ? { share: doShareSelectedDp } : {}),
                    ...(cfg.detailPinsSendToWikiUrl ? { wiki: doSendSelectedDpToWiki } : {}),
                    delete: doDeleteSelectedDp,
                    deselect: clearDpSelection,
                }
                : {},
        );
    }

    function resetSelectedDpBulkEditDialog(): void {
        document.querySelectorAll<HTMLElement>("[data-dp-bulk-picker]").forEach((picker) => {
            delete picker.dataset.dpBulkValue;
            picker.querySelectorAll(".dp-icon-btn--active, .dp-color-swatch--active").forEach((button) => {
                button.classList.remove("dp-icon-btn--active", "dp-color-swatch--active");
            });
        });
        for (const [kind, defaultValue] of [
            ["bg", "80"],
            ["border", "100"],
        ] as const) {
            const enabled = document.getElementById(`detail-pin-bulk-${kind}-opacity-enabled`) as HTMLInputElement | null;
            const range = document.getElementById(`detail-pin-bulk-${kind}-opacity`) as HTMLInputElement | null;
            if (enabled) enabled.checked = false;
            if (range) {
                range.value = defaultValue;
                range.disabled = true;
            }
            const value = document.getElementById(`detail-pin-bulk-${kind}-opacity-value`);
            if (value) value.textContent = defaultValue;
        }
    }

    function openSelectedDpBulkEditDialog(): void {
        const dialog = document.getElementById("detail-pin-bulk-edit-dialog") as HTMLDialogElement | null;
        if (!dialog || !selectedDpUuids.size) return;
        resetSelectedDpBulkEditDialog();
        const title = document.getElementById("detail-pin-bulk-edit-title");
        if (title) title.textContent = `Edit ${selectedDpUuids.size} child pin${selectedDpUuids.size === 1 ? "" : "s"}`;
        dialog.showModal();
    }

    document.querySelectorAll<HTMLElement>("[data-dp-bulk-picker]").forEach((picker) => {
        picker.addEventListener("click", (event) => {
            const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-dp-bulk-value]");
            if (!button || !picker.contains(button)) return;
            picker.dataset.dpBulkValue = button.dataset.dpBulkValue ?? "";
            picker.querySelectorAll(".dp-icon-btn, .dp-color-swatch").forEach((candidate) => {
                candidate.classList.toggle("dp-icon-btn--active", candidate === button && candidate.classList.contains("dp-icon-btn"));
                candidate.classList.toggle("dp-color-swatch--active", candidate === button && candidate.classList.contains("dp-color-swatch"));
            });
        });
    });

    for (const kind of ["bg", "border"] as const) {
        const enabled = document.getElementById(`detail-pin-bulk-${kind}-opacity-enabled`) as HTMLInputElement | null;
        const range = document.getElementById(`detail-pin-bulk-${kind}-opacity`) as HTMLInputElement | null;
        const value = document.getElementById(`detail-pin-bulk-${kind}-opacity-value`);
        enabled?.addEventListener("change", () => {
            if (range) range.disabled = !enabled.checked;
        });
        range?.addEventListener("input", () => {
            if (value) value.textContent = range.value;
        });
    }

    document.getElementById("detail-pin-bulk-edit-submit")?.addEventListener("click", async function (this: HTMLButtonElement) {
        const uuids = Array.from(selectedDpUuids);
        if (!uuids.length || !cfg.detailPinsBulkEditUrl) return;

        const payload: Record<string, unknown> = { uuids };
        document.querySelectorAll<HTMLElement>("[data-dp-bulk-picker]").forEach((picker) => {
            const field = picker.dataset.dpBulkField;
            if (field && Object.hasOwn(picker.dataset, "dpBulkValue")) payload[field] = picker.dataset.dpBulkValue || null;
        });
        for (const kind of ["bg", "border"] as const) {
            const enabled = document.getElementById(`detail-pin-bulk-${kind}-opacity-enabled`) as HTMLInputElement | null;
            const range = document.getElementById(`detail-pin-bulk-${kind}-opacity`) as HTMLInputElement | null;
            if (enabled?.checked && range) payload[`${kind}_opacity`] = Number.parseInt(range.value, 10);
        }
        if (Object.keys(payload).length === 1) {
            toast.info("Choose at least one style to change.");
            return;
        }

        const saved = this.innerHTML;
        this.disabled = true;
        this.innerHTML = '<i class="material-symbols-outlined spin">progress_activity</i> Saving...';
        try {
            const response = await fetch(cfg.detailPinsBulkEditUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify(payload),
            });
            if (!response.ok) throw new Error((await response.text()) || "Update failed.");
            (document.getElementById("detail-pin-bulk-edit-dialog") as HTMLDialogElement | null)?.close();
            toast.success(`${uuids.length} child pin${uuids.length === 1 ? "" : "s"} updated.`);
            clearDpSelection();
            loadDetailPins();
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to update child pins.");
        } finally {
            this.disabled = false;
            this.innerHTML = saved;
        }
    });

    async function doPromoteSelectedDp(): Promise<void> {
        const uuids = Array.from(selectedDpUuids);
        if (!uuids.length) return;
        const n = uuids.length;
        if (!(await confirmAction({ title: "Promote child pins?", message: `Promote ${n} child pin${n === 1 ? "" : "s"} to top-level pins on your main map?`, confirmLabel: "Promote" }))) return;
        // `.catch(() => false)` matters as much as the `.ok`: without it a single
        // network failure rejects the whole Promise.all, so this function throws
        // and the user gets no toast, no cleared selection and no refreshed list
        // after confirming a bulk promote - see doDeleteSelectedDp() below, which
        // needed the same fix for the same reason.
        const results = await Promise.all(
            uuids.map((uuid) => {
                const slug = detailPins.find((d) => d.uuid === uuid)?.slug || uuid;
                return fetch(`/dashboard/map/pin/${encodeURIComponent(slug)}/detach-parent/`, {
                    method: "POST",
                    headers: { "X-CSRFToken": getCsrfToken() },
                })
                    .then((r) => r.ok)
                    .catch(() => false);
            }),
        );
        const promoted = results.filter(Boolean).length;
        if (promoted) toast.success(`${promoted} pin${promoted === 1 ? "" : "s"} promoted.`);
        if (promoted < n) toast.warning(`${n - promoted} pin${n - promoted === 1 ? "" : "s"} could not be promoted (location conflict).`);
        clearDpSelection();
        loadDetailPins();
        fetchBoundaries(0);
    }

    async function doShareSelectedDp(): Promise<void> {
        if (!cfg.pinShareDialogUrl) return;
        const uuids = Array.from(selectedDpUuids);
        if (!uuids.length) return;
        const dialog = document.getElementById("pin-share-dialog") as HTMLDialogElement | null;
        if (!dialog) return;
        const url = `${cfg.pinShareDialogUrl}?children=${uuids.map(encodeURIComponent).join(",")}`;
        const html = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
            .then((r) => (r.ok ? r.text() : ""))
            .catch(() => "");
        if (!html) {
            toast.error("Failed to open the share dialog.");
            return;
        }
        dialog.innerHTML = html;
        htmxProcess(dialog);
        dialog.showModal();
        clearDpSelection();
    }

    async function doSendSelectedDpToWiki(): Promise<void> {
        if (!cfg.detailPinsSendToWikiUrl) return;
        const uuids = Array.from(selectedDpUuids);
        if (!uuids.length) return;
        const body = new URLSearchParams();
        uuids.forEach((uuid) => body.append("child_pin_uuids", uuid));
        const response = await fetch(cfg.detailPinsSendToWikiUrl, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": getCsrfToken() },
            body,
        }).catch(() => null);
        if (response?.ok) {
            const trigger = response.headers.get("HX-Trigger");
            if (trigger) {
                try {
                    const parsed = JSON.parse(trigger) as { showToast?: { level: string; message: string } };
                    if (parsed.showToast) toast[parsed.showToast.level as "success" | "info" | "warning" | "error"]?.(parsed.showToast.message);
                } catch {
                    /* malformed trigger header - nothing to show */
                }
            }
        } else {
            toast.error("Failed to send child pins to the wiki.");
        }
        clearDpSelection();
    }

    async function doDeleteSelectedDp(): Promise<void> {
        const uuids = Array.from(selectedDpUuids);
        if (!uuids.length) return;
        const n = uuids.length;
        if (!(await confirmAction({ title: "Delete child pins?", message: `Delete ${n} child pin${n === 1 ? "" : "s"}? This also removes reviews, visit history, and notes.`, confirmLabel: "Delete" }))) return;
        // `.catch(() => false)` matters as much as the `.ok`: without it a single
        // network failure rejects the whole Promise.all, so this function throws
        // and the user gets no toast, no cleared selection and no refreshed list
        // after confirming a bulk delete. Counting it as "not deleted" instead
        // routes it into the warning below, which already says the right thing.
        const results = await Promise.all(
            uuids.map((uuid) =>
                fetch(`${dpEditBase}${uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } })
                    .then((r) => r.ok)
                    .catch(() => false),
            ),
        );
        const deleted = results.filter(Boolean).length;
        if (deleted) toast.success(`${deleted} pin${deleted === 1 ? "" : "s"} deleted.`);
        if (deleted < n) toast.warning(`${n - deleted} pin${n - deleted === 1 ? "" : "s"} could not be deleted.`);
        clearDpSelection();
        loadDetailPins();
        fetchBoundaries(0);
    }

    // Rectangle drag-select over detail-pin markers, mirroring the main map's
    // multi-select tool (_initSelectDragRectangle in pages/map/index.html).
    initMapRectangleSelect(
        mapEl,
        map,
        () => detailSelectMode,
        (bounds) => {
            detailSelectableEntries().forEach((dp) => {
                if (dp.marker && !selectedDpUuids.has(dp.uuid) && bounds.contains(dp.marker.getLatLng())) toggleDpSelection(dp.uuid);
            });
        },
    );

    // -- Markup toolbar (shared factory - see ts/shared/markup-toolbar.ts) --
    const toolbar: MarkupToolbar = window.createMarkupToolbar(map, markupLayer, {
        markupJsonUrl: cfg.markupJsonUrl,
        markupCreateUrl: cfg.markupCreateUrl,
        markupEditUrlTemplate: cfg.markupEditUrlTemplate,
        markupFillOpacity: cfg.markupFillOpacity,
        markupBorderOpacity: cfg.markupBorderOpacity,
        lineFinishTipDismissed: () => !cfg.showOnboardingTips,
        onBuildDetailList: () => buildDetailList(),
        onClearDetailPinHighlight: () => clearDetailPinHighlight(),
        onCloseDetailPinPanel: () => closeDetailPinPanel(),
        layerGroupFor: (item) => (item.layer_uuid && customLayerGroups.get(item.layer_uuid)) || markupLayer,
    });

    window.startMarkupDraw = toolbar.startMarkupDraw;
    window.startShapeDraw = toolbar.startShapeDraw;
    window.startTextPlacement = toolbar.startTextPlacement;
    window.closeMarkupPanel = toolbar.closeMarkupPanel;
    window._liveApplyMarkupEdit = toolbar.liveApplyMarkupEdit;
    window._closeMarkupDraw = toolbar.closeOrFinishDraw;
    window.deleteMarkupEdit = toolbar.deleteMarkupEdit;
    window.openMarkupEditDialog = toolbar.openMarkupEditDialog;
    window.loadMarkup = toolbar.loadMarkup;

    loadDetailPins();
    document.body.addEventListener("pinDetailPinsChanged", () => {
        loadDetailPins();
        fetchBoundaries(0);
    });

    // -- Photo panel -----------------------------------------------------------
    // Icon and sizing come from shared/photo-map so this map and an album's map
    // render a photo identically.
    function photoMarkerSize(highlighted?: boolean): number {
        return sharedPhotoMarkerSize(map.getZoom(), highlighted);
    }

    function addPhotoMarker(imgId: number, url: string, lat: number, lng: number, ownerName?: string): void {
        if (photoMarkers[imgId]) photoLayer.removeLayer(photoMarkers[imgId]!.marker);
        // Photos belonging to a child pin (ownerName) are display-only on this
        // map - they're repositioned from their own pin's page.
        const marker = L.marker([lat, lng], { icon: makePhotoIcon(url, photoMarkerSize(false), false), draggable: !ownerName });
        tagPhotoMarker(marker, url, imgId);
        if (ownerName) marker.bindTooltip(`Photo from ${ownerName}`, { permanent: false, direction: "top", className: "detail-pin-tooltip" });
        if (!ownerName) reclusterOnDrag(marker, photoLayer, map);
        marker.on("dragend", () => {
            returnToCluster(marker, photoLayer, map);
            const pos = marker.getLatLng();
            const prevLat = photoMarkers[imgId]!.lat;
            const prevLng = photoMarkers[imgId]!.lng;
            photoMarkers[imgId]!.lat = pos.lat;
            photoMarkers[imgId]!.lng = pos.lng;
            const item = photoPanelItems.find((p) => p.id === imgId);
            if (item) {
                item.lat = pos.lat;
                item.lng = pos.lng;
            }
            if (window.galleryRepositionImage) {
                window.galleryRepositionImage(imgId, pos.lat, pos.lng, () => {
                    // Server rejected the move - snap back to the last known-good position.
                    marker.setLatLng([prevLat, prevLng]);
                    photoMarkers[imgId]!.lat = prevLat;
                    photoMarkers[imgId]!.lng = prevLng;
                    if (item) {
                        item.lat = prevLat;
                        item.lng = prevLng;
                    }
                    returnToCluster(marker, photoLayer, map);
                    buildPhotoPanel();
                });
            }
            buildPhotoPanel();
        });
        marker.on("mouseover", () => window._galleryHighlightMarker?.(imgId, true));
        marker.on("mouseout", () => window._galleryHighlightMarker?.(imgId, false));
        // Open the photo in the gallery lightbox. The url is passed as a
        // fallback because the gallery grid is paginated - this photo may not
        // be on the currently rendered gallery page.
        marker.on("click", () => window.galleryOpenLightbox?.(imgId, { url }));
        marker.on("contextmenu", (event: L.LeafletMouseEvent) => {
            L.DomEvent.stop(event);
            showMapContextMenu({
                lat: event.latlng.lat,
                lng: event.latlng.lng,
                zoom: map.getZoom(),
                clientX: event.originalEvent.clientX,
                clientY: event.originalEvent.clientY,
                extraItems: [
                    {
                        icon: "visibility_off",
                        label: "Hide from map",
                        onClick: () => {
                            if (typeof window.gallerySetPhotoMapHidden === "function") {
                                window.gallerySetPhotoMapHidden(imgId, true);
                            }
                        },
                    },
                ],
            });
        });
        marker.addTo(photoLayer);
        photoMarkers[imgId] = { marker, url, lat, lng, highlighted: false };
    }

    // Rescale photo thumbnails when the user zooms in/out so they don't cover
    // a disproportionate area of the map when zoomed far out.
    map.on("zoomend", () => {
        Object.values(photoMarkers).forEach((entry) => {
            entry.marker.setIcon(makePhotoIcon(entry.url, photoMarkerSize(entry.highlighted), entry.highlighted));
        });
    });

    window._galleryAddMarker = (img) => {
        if (!photoPanelItems.find((p) => p.id === img.id)) photoPanelItems.push({ id: img.id, url: img.url, lat: img.latitude, lng: img.longitude, mine: true });
        if (img.latitude != null && img.longitude != null) addPhotoMarker(img.id, img.marker_thumb_url || img.url, img.latitude, img.longitude);
        buildPhotoPanel();
        refreshPanelHeader();
    };

    window._galleryRemoveMarker = (imgId) => {
        photoPanelItems = photoPanelItems.filter((p) => p.id !== imgId);
        if (photoMarkers[imgId]) {
            photoLayer.removeLayer(photoMarkers[imgId]!.marker);
            delete photoMarkers[imgId];
        }
        buildPhotoPanel();
        refreshPanelHeader();
    };

    window._galleryHighlightMarker = (imgId, on) => {
        const entry = photoMarkers[imgId];
        if (entry) {
            entry.highlighted = !!on;
            entry.marker.setIcon(makePhotoIcon(entry.url, photoMarkerSize(entry.highlighted), entry.highlighted));
        }
        document.querySelectorAll<HTMLElement>(".photo-panel-item").forEach((li) => {
            li.classList.toggle("is-highlighted", +(li.dataset.id ?? "") === imgId && !!on);
        });
    };

    // -- Tap-to-place ----------------------------------------------------------
    // HTML5 drag-and-drop never fires on touch, so drag-onto-the-map leaves a
    // photo with no coordinates unplaceable from a phone. An armed item hands
    // the next map click to the same placement path a drop takes.
    type PendingPlacement = { kind: "photo"; photoId: number } | { kind: "media"; itemEl: HTMLElement; item: MediaDropItem };
    let pendingPlacement: PendingPlacement | null = null;
    const PLACEMENT_HINT = "Tap the map to place this photo, or press Escape to cancel.";

    function syncPlacementAffordance(): void {
        const armedPhotoId = pendingPlacement?.kind === "photo" ? String(pendingPlacement.photoId) : null;
        document.querySelectorAll<HTMLElement>(".photo-panel-item").forEach((li) => {
            const armed = armedPhotoId != null && li.dataset.id === armedPhotoId;
            li.classList.toggle("is-placing", armed);
            li.querySelector(".photo-panel-place-btn")?.setAttribute("aria-pressed", String(armed));
        });
        const armedMediaEl = pendingPlacement?.kind === "media" ? pendingPlacement.itemEl : null;
        document.querySelectorAll<HTMLElement>(".media-item.is-placing").forEach((el) => {
            if (el !== armedMediaEl) el.classList.remove("is-placing");
        });
        armedMediaEl?.classList.add("is-placing");
    }

    function disarmPlacement(): void {
        if (!pendingPlacement) return;
        map.off("click", onPlacementMapClick);
        map.getContainer().classList.remove("photo-drop-target");
        pendingPlacement = null;
        syncPlacementAffordance();
    }

    function onPlacementMapClick(event: L.LeafletMouseEvent): void {
        const pending = pendingPlacement;
        disarmPlacement();
        if (!pending) return;
        if (pending.kind === "photo") placePhotoAt(pending.photoId, event.latlng);
        else placeMediaItemAt(pending.itemEl, pending.item, event.latlng);
    }

    function armPlacement(pending: PendingPlacement): void {
        disarmPlacement();
        pendingPlacement = pending;
        map.once("click", onPlacementMapClick);
        map.getContainer().classList.add("photo-drop-target");
        syncPlacementAffordance();
        toast.info(PLACEMENT_HINT);
    }

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        if (document.querySelector("dialog[open]")) return;
        if (detailSelectMode) {
            exitDetailPinSelectMode();
            return;
        }
        disarmPlacement();
    });

    // Media tiles are server-rendered (partials/pins/pin_media_items.html), so
    // their affordance calls in here instead of being bound from this module.
    window.mediaPlaceOnMap = function (itemEl: HTMLElement): void {
        if (!cfg.mediaRelevanceUrl) return;
        if (pendingPlacement?.kind === "media" && pendingPlacement.itemEl === itemEl) {
            disarmPlacement();
            return;
        }
        armPlacement({
            kind: "media",
            itemEl,
            item: {
                source: itemEl.dataset.mediaSource ?? "",
                key: itemEl.dataset.mediaKey ?? "",
                url: itemEl.dataset.mediaUrl ?? "",
                pageUrl: itemEl.dataset.mediaPageUrl ?? "",
                caption: itemEl.dataset.mediaCaption ?? "",
            },
        });
    };

    function buildPhotoPanel(): void {
        const ul = document.getElementById("photo-panel-list");
        if (!ul) return;
        ul.innerHTML = "";
        // Update badge on Photos tab button.
        const photoTab = document.getElementById("map-panel-tab-photos");
        if (photoTab) {
            let badge = photoTab.querySelector(".map-panel-tab-badge");
            if (photoPanelItems.length) {
                if (!badge) {
                    badge = document.createElement("span");
                    badge.className = "map-panel-tab-badge";
                    photoTab.appendChild(badge);
                }
                badge.textContent = String(photoPanelItems.length);
            } else if (badge) {
                badge.remove();
            }
        }
        if (!photoPanelItems.length) {
            const empty = document.createElement("li");
            empty.className = "photo-panel-empty";
            empty.innerHTML = '<i class="material-symbols-outlined">photo_camera</i><span>No photos yet</span>';
            ul.appendChild(empty);
            return;
        }
        photoPanelItems.forEach((img) => {
            const hasCoords = img.lat != null && img.lng != null;
            const li = document.createElement("li");
            li.className = "photo-panel-item";
            li.dataset.id = String(img.id);
            li.draggable = true;
            li.title = "Click to view";
            // The place button carries its own styling: .photo-panel-item is a
            // bare thumbnail tile with no button treatment to inherit.
            li.innerHTML = `
                <div class="photo-panel-thumb-wrap">
                    <img src="${escHtml(img.url)}" class="photo-panel-thumb" alt="" draggable="false">
                    <button type="button" class="photo-panel-place-btn" draggable="false" aria-pressed="false"
                            title="${hasCoords ? "Move on map" : "Place on map"}" aria-label="${hasCoords ? "Move on map" : "Place on map"}"
                            style="position:absolute;top:3px;right:3px;display:flex;align-items:center;justify-content:center;width:28px;height:28px;padding:0;border:0;border-radius:4px;background:rgba(0,0,0,.52);color:#fff;cursor:pointer">
                        <i class="material-icons" style="font-size:1rem">add_location_alt</i>
                    </button>
                    <span class="photo-panel-coord-badge ${hasCoords ? "has-gps" : "no-gps"}" title="${hasCoords ? "Has GPS" : "No GPS"}">
                        <i class="material-icons">${hasCoords ? "place" : "location_off"}</i>
                    </span>
                </div>`;
            li.querySelector(".photo-panel-place-btn")?.addEventListener("click", (event) => {
                // The tile's own click pans and opens the lightbox.
                event.stopPropagation();
                if (pendingPlacement?.kind === "photo" && pendingPlacement.photoId === img.id) disarmPlacement();
                else armPlacement({ kind: "photo", photoId: img.id });
            });
            li.addEventListener("mouseenter", () => window._galleryHighlightMarker?.(img.id, true));
            li.addEventListener("mouseleave", () => window._galleryHighlightMarker?.(img.id, false));
            li.addEventListener("dragstart", (e) => {
                e.dataTransfer?.setData("text/photoid", String(img.id));
                if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
                li.classList.add("is-dragging");
            });
            li.addEventListener("dragend", () => li.classList.remove("is-dragging"));
            li.addEventListener("click", () => {
                if (hasCoords) map.panTo([img.lat!, img.lng!]);
                window.galleryOpenLightbox?.(img.id, { url: img.url });
            });
            ul.appendChild(li);
        });
        syncPlacementAffordance();
    }

    function placePhotoAt(imgId: number, latlng: L.LatLng): void {
        const item = photoPanelItems.find((p) => p.id === imgId);
        if (!item) return;
        const prevLat = item.lat;
        const prevLng = item.lng;
        item.lat = latlng.lat;
        item.lng = latlng.lng;
        addPhotoMarker(imgId, item.url, latlng.lat, latlng.lng);
        if (window.galleryRepositionImage) {
            window.galleryRepositionImage(imgId, latlng.lat, latlng.lng, () => {
                // Server rejected the move - snap back, or remove the marker
                // entirely if the photo had no prior coordinates.
                item.lat = prevLat;
                item.lng = prevLng;
                if (prevLat != null && prevLng != null) {
                    addPhotoMarker(imgId, item.url, prevLat, prevLng);
                } else if (photoMarkers[imgId]) {
                    photoLayer.removeLayer(photoMarkers[imgId]!.marker);
                    delete photoMarkers[imgId];
                }
                buildPhotoPanel();
            });
        }
        buildPhotoPanel();
        refreshPanelHeader();
    }

    // Drop photo onto map to assign coordinates.
    mapEl.addEventListener("dragover", (e) => {
        if (!e.dataTransfer?.types.includes("text/photoid")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        map.getContainer().classList.add("photo-drop-target");
    });
    mapEl.addEventListener("dragleave", () => {
        if (!pendingPlacement) mapEl.classList.remove("photo-drop-target");
    });
    mapEl.addEventListener("drop", (e) => {
        map.getContainer().classList.remove("photo-drop-target");
        const idStr = e.dataTransfer?.getData("text/photoid");
        if (!idStr) return;
        e.preventDefault();
        // A completed drop resolves whatever the user had armed for a tap.
        disarmPlacement();
        const rect = mapEl.getBoundingClientRect();
        placePhotoAt(Number.parseInt(idStr, 10), map.containerPointToLatLng([e.clientX - rect.left, e.clientY - rect.top]));
    });

    // Drop a Media-section item (external provider result, not yet a real
    // Image row - see PinController.media_relevance) onto the map: this
    // materializes it locally (downloads + saves, same as clicking
    // "relevant") and sets its coordinates in one request, then adds it to
    // the photo layer exactly like a real gallery photo. Only wired when the
    // page actually has a Media section (cfg.mediaRelevanceUrl - the wiki
    // page, which shares this module, has none).
    function placeMediaItemAt(itemEl: HTMLElement | undefined, item: MediaDropItem, latlng: L.LatLng): void {
        fetch(cfg.mediaRelevanceUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify({
                source: item.source,
                item_key: item.key,
                url: item.url,
                is_relevant: true,
                page_url: item.pageUrl,
                caption: item.caption,
                latitude: latlng.lat,
                longitude: latlng.lng,
            }),
        })
            .then((r) => {
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then((data) => {
                window.mediaApplyMaterializedDrop?.(itemEl, data);
                if (data.image_id && data.latitude != null && data.longitude != null) {
                    window._galleryAddMarker({ id: data.image_id, url: data.image_url, latitude: data.latitude, longitude: data.longitude });
                } else if (data.materialize_error) {
                    toast.warning(`Couldn't save a local copy: ${data.materialize_error}`);
                }
            })
            .catch(() => toast.error("Failed to save photo location."));
    }

    mapEl.addEventListener("dragover", (e) => {
        if (!cfg.mediaRelevanceUrl || !e.dataTransfer?.types.includes("text/media-item")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        map.getContainer().classList.add("photo-drop-target");
    });
    mapEl.addEventListener("dragleave", () => {
        if (!pendingPlacement) mapEl.classList.remove("photo-drop-target");
    });
    mapEl.addEventListener("drop", (e) => {
        const raw = e.dataTransfer?.getData("text/media-item");
        if (!cfg.mediaRelevanceUrl || !raw) return;
        e.preventDefault();
        // A completed drop resolves whatever the user had armed for a tap.
        disarmPlacement();
        map.getContainer().classList.remove("photo-drop-target");
        const itemEl = window._mediaDragItemEl;
        window._mediaDragItemEl = undefined;
        let item: MediaDropItem;
        try {
            item = JSON.parse(raw);
        } catch {
            return;
        }
        const rect = mapEl.getBoundingClientRect();
        placeMediaItemAt(itemEl, item, map.containerPointToLatLng([e.clientX - rect.left, e.clientY - rect.top]));
    });

    // Tab switching.
    document.querySelectorAll<HTMLElement>(".map-panel-tab").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".map-panel-tab").forEach((b) => b.classList.remove("is-active"));
            btn.classList.add("is-active");
            const tab = btn.dataset.tab;
            (document.getElementById("map-panel-details") as HTMLElement).hidden = tab !== "details";
            (document.getElementById("map-panel-photos") as HTMLElement).hidden = tab !== "photos";
        });
    });

    // Load gallery photos on page load.
    fetch(cfg.photoGalleryJsonUrl)
        .then((r) => r.json())
        .then((data) => {
            photoPanelItems = [];
            (data.images || []).forEach((img: any) => {
                photoPanelItems.push({ id: img.id, url: img.url, lat: img.latitude, lng: img.longitude, mine: img.is_mine });
                if (img.latitude != null && img.longitude != null) addPhotoMarker(img.id, img.marker_thumb_url || img.url, img.latitude, img.longitude, img.child_pin_name);
            });
            buildPhotoPanel();
            refreshPanelHeader();
        })
        .catch((err) => console.warn("Could not load gallery photos for panel:", err));

    // -- Boundary editor (property + building) ----------------------------------
    // Two typed boundaries render in different colors: the property boundary
    // (parcel/grounds, red) and the building boundary (footprint, blue). Each
    // is fetched, drawn, and edited independently against the same endpoint.
    const boundaryApiUrl = cfg.boundaryUrl;
    type BoundaryType = "property" | "building";
    const BOUNDARY_STYLES: Record<BoundaryType, L.PathOptions> = {
        property: { pane: "boundaryPane", color: "#cc2200", fillColor: "#ff4422", fillOpacity: 0.2, weight: 2 },
        building: { pane: "boundaryPane", color: "#1d4ed8", fillColor: "#3b82f6", fillOpacity: 0.22, weight: 2 },
    };
    // The synthesized default circle is display-only context, not real
    // geometry - dashed and faint, with the main marker left visible.
    const CIRCLE_STYLE: L.PathOptions = { ...BOUNDARY_STYLES.property, dashArray: "6 6", fillOpacity: 0.06 };
    // Building boundaries drawn on this pin's detail pins (display-only here).
    const DETAIL_BUILDING_STYLE: L.PathOptions = { ...BOUNDARY_STYLES.building, dashArray: "4 4", fillOpacity: 0.12 };
    const boundaryGroups: Record<BoundaryType, L.FeatureGroup> = {
        property: new L.FeatureGroup().addTo(map),
        building: new L.FeatureGroup().addTo(map),
    };
    const detailBuildingItems = new L.FeatureGroup().addTo(map);
    let boundaryDrawControl: L.Control.Draw | null = null;
    let editingBoundaryType: BoundaryType | null = null;
    const savedBoundaries: Record<BoundaryType, any> = { property: null, building: null }; // GeoJSON as last returned by the server
    const boundarySources: Record<BoundaryType, string | null> = { property: null, building: null }; // pin|wiki|inherited|generated|circle|null
    let boundaryBoundsFitted = false;

    // Clicking an already-active draw-toolbar tool cancels it instead of no-op
    // re-enabling it. Prototype-patched dynamically like the original script;
    // `any` here is deliberate - the patch's whole point is to be generic
    // across Leaflet.Draw's incompatible per-tool return types.
    if (!window._boundaryDrawToggleWired) {
        window._boundaryDrawToggleWired = true;
        ([L.Draw.Polygon, L.EditToolbar.Edit] as any[]).forEach((Ctor) => {
            const origEnable = Ctor.prototype.enable;
            Ctor.prototype.enable = function (this: { _enabled?: boolean; disable: () => void }) {
                if (this._enabled) {
                    this.disable();
                    return this;
                }
                return origEnable.call(this);
            };
        });
    }

    function setMainMarkerVisible(visible: boolean): void {
        if (visible && !map.hasLayer(mainMarker)) {
            mainMarker.addTo(map);
        } else if (!visible && map.hasLayer(mainMarker)) {
            map.removeLayer(mainMarker);
        }
    }

    function addGeoJSONPolygons(group: L.FeatureGroup, geojson: any, style: L.PathOptions, label?: string): void {
        // Split MultiPolygon into individual L.Polygon layers so Leaflet.Draw
        // can edit each sub-polygon independently.
        const rings: [number, number][][][] | null = geojson.type === "MultiPolygon" ? geojson.coordinates : geojson.type === "Polygon" ? [geojson.coordinates] : null;
        const bindLabel = (layer: L.Layer) => {
            if (label) layer.bindTooltip(label, { sticky: true, direction: "top", className: "boundary-tooltip" });
            return layer;
        };
        if (rings) {
            rings.forEach((ringSet) => {
                // GeoJSON coords are [lng, lat]; Leaflet wants [lat, lng].
                group.addLayer(bindLabel(L.polygon(ringSet.map((ring) => ring.map((c) => [c[1], c[0]] as [number, number])), style)));
            });
        } else {
            // FeatureCollection fallback.
            L.geoJSON(geojson, { style }).eachLayer((l) => group.addLayer(bindLabel(l)));
        }
    }

    function loadBoundary(type: BoundaryType, geojson: any, source: string | null): void {
        const group = boundaryGroups[type];
        group.clearLayers();
        savedBoundaries[type] = geojson || null;
        boundarySources[type] = geojson ? source || null : null;
        if (!geojson) return;
        const isCircle = type === "property" && source === "circle";
        const style = isCircle ? CIRCLE_STYLE : BOUNDARY_STYLES[type];
        const label = type === "property" ? (isCircle ? "Approximate property area" : "Property boundary") : "Building boundary";
        addGeoJSONPolygons(group, geojson, style, label);
    }

    function boundaryHasRealPolygon(type: BoundaryType): boolean {
        return Boolean(savedBoundaries[type]) && boundarySources[type] !== "circle";
    }

    function applyBoundaryPayload(data: any): void {
        const boundaries = data.boundaries || {};
        (["property", "building"] as BoundaryType[]).forEach((type) => {
            const entry = boundaries[type] || {};
            loadBoundary(type, entry.polygon || null, entry.source || null);
        });
        // Buildings drawn on detail pins keep the building layer meaningful even
        // when this pin has no building boundary of its own. When neither
        // exists, no building layer is shown at all ("no known building here").
        detailBuildingItems.clearLayers();
        (data.detail_buildings || []).forEach((entry: any) => {
            if (entry.polygon) addGeoJSONPolygons(detailBuildingItems, entry.polygon, DETAIL_BUILDING_STYLE, "Building boundary (from a child pin)");
        });
        // The center marker stays visible unless a real (non-circle) property
        // polygon marks the place's extent.
        setMainMarkerVisible(!boundaryHasRealPolygon("property"));
        if (!boundaryBoundsFitted) {
            const fitGroup = boundaryHasRealPolygon("property") ? boundaryGroups.property : boundaryHasRealPolygon("building") ? boundaryGroups.building : null;
            if (fitGroup && fitGroup.getLayers().length) {
                map.fitBounds(fitGroup.getBounds().pad(0.25));
                boundaryBoundsFitted = true;
            }
        }
        map.invalidateSize();
        attachBoundaryClickHandlers();
    }

    // Boundary generation happens in a background task on first view (see
    // services/external_data.py) - while the server reports pending, poll
    // until the generated polygons land rather than blocking the page load.
    // A previously-generated boundary also goes stale after a while (see
    // SiteSettings.boundary_cache_days) - the server serves the last-known
    // geometry immediately (already applied below) while refreshing it in
    // the background, reporting that as "refreshing" rather than "pending"
    // since there's already something on the map. Poll the same way in both
    // cases so an already-open page redraws with the newer geometry once it
    // lands, without ever blocking on it.
    function fetchBoundaries(attempt: number): void {
        fetch(boundaryApiUrl)
            .then((r) => r.json())
            .then((data) => {
                applyBoundaryPayload(data);
                if ((data.pending || data.refreshing) && attempt < 30) {
                    setTimeout(() => fetchBoundaries(attempt + 1), 2000);
                }
            })
            .catch((err) => console.warn("Could not load boundaries:", err));
    }
    fetchBoundaries(0);

    function attachEditRightClickDelete(): void {
        // Walk the edit handler's marker group and fire a click (delete) on right-click.
        setTimeout(() => {
            if (!editingBoundaryType) return;
            boundaryGroups[editingBoundaryType].eachLayer((layer) => {
                const editableLayer = layer as L.Layer & { editing?: { _markerGroup?: L.LayerGroup } };
                if (editableLayer.editing?._markerGroup) {
                    editableLayer.editing._markerGroup.eachLayer((m) => {
                        // Leaflet's event system has no jQuery-style dot
                        // namespacing - "contextmenu.rcdelete" was a distinct
                        // event type nothing ever fires, so right-click
                        // delete never worked despite the toast advertising
                        // it. .off() with no listener removes every
                        // "contextmenu" handler, which is what keeps repeat
                        // calls (this runs on every EDITSTART) from stacking.
                        m.off("contextmenu");
                        m.on("contextmenu", (e: L.LeafletMouseEvent) => {
                            L.DomEvent.stopPropagation(e);
                            m.fire("click");
                        });
                    });
                }
            });
        }, 100);
    }

    // `visible` names the normal (not-editing) state - true hides the boundary
    // save controls, false shows them. A boundary edit session is started from
    // the boundary's own right-click context menu (see openBoundaryCtxMenu),
    // since boundaries have no dedicated toolbar button.
    function setBoundaryEditButtonsVisible(visible: boolean): void {
        const controls = document.getElementById("boundary-save-controls");
        if (controls) controls.style.display = visible ? "none" : "";
    }

    function startEditBoundary(type: BoundaryType): void {
        if (boundaryDrawControl || !boundaryGroups[type]) return;
        editingBoundaryType = type;
        // Editing a boundary is its own exclusive map-interaction mode - close
        // whichever side panel happens to be open (autosave makes this safe).
        toolbar.closeMarkupPanel();
        closeDetailPinPanel();
        disarmPlacement();
        // While actively editing, boundary polygons need to catch clicks/drags
        // ahead of markup shapes - temporarily swap the pane stacking order for that.
        map.getPane("boundaryPane")!.style.zIndex = "560";

        const group = boundaryGroups[type];
        // The dashed default circle is display-only context, not editable
        // geometry - drop it so the user draws their real boundary from scratch.
        if (type === "property" && boundarySources.property === "circle") group.clearLayers();

        boundaryDrawControl = new L.Control.Draw({
            draw: {
                polygon: { allowIntersection: false, drawError: { color: "#ffcc00", message: "Boundaries cannot intersect!" }, shapeOptions: BOUNDARY_STYLES[type], showArea: true },
                marker: false,
                circle: false,
                rectangle: false,
                polyline: false,
                circlemarker: false,
            },
            edit: { featureGroup: group, remove: false },
        });
        map.addControl(boundaryDrawControl);
        map.on((L.Draw.Event as any).CREATED, (e: any) => {
            // Add the new polygon to the existing set - never clear others.
            group.addLayer(e.layer);
            saveBoundary({ exitEdit: false });
        });
        // Re-attach right-click delete whenever edit mode is activated by toolbar click.
        map.on((L.Draw.Event as any).EDITSTART, attachEditRightClickDelete);
        map.on((L.Draw.Event as any).EDITED, () => saveBoundary({ exitEdit: false }));
        map.on((L.Draw.Event as any).DELETED, () => saveBoundary({ exitEdit: false }));
        group.eachLayer((layer) => layer.on("edit", scheduleBoundaryAutoSave));

        // Auto-activate the right tool immediately after the control renders.
        setTimeout(() => {
            const control = boundaryDrawControl as any;
            if (group.getLayers().length > 0) {
                control._toolbars.edit._modes.edit.handler.enable();
                attachEditRightClickDelete();
                toast.info("Drag vertices to reshape, click a vertex to delete it, or right-click to delete.");
            } else {
                control._toolbars.draw._modes.polygon.handler.enable();
            }
        }, 50);

        setBoundaryEditButtonsVisible(false);
    }

    let boundaryAutoSaveTimer: ReturnType<typeof setTimeout> | undefined;

    function scheduleBoundaryAutoSave(): void {
        if (!boundaryDrawControl) return;
        clearTimeout(boundaryAutoSaveTimer);
        boundaryAutoSaveTimer = setTimeout(() => saveBoundary({ exitEdit: false, quiet: true }), 600);
    }

    function boundaryTypeOfLayer(layer: L.Layer): BoundaryType | null {
        if (boundaryGroups.property.hasLayer(layer)) return "property";
        if (boundaryGroups.building.hasLayer(layer)) return "building";
        return null;
    }

    function boundaryGeometryOf(type: BoundaryType): { type: string; coordinates: unknown[] } | null {
        const layers = boundaryGroups[type].getLayers() as Array<L.Layer & { toGeoJSON: () => { geometry: { coordinates: unknown[] } } }>;
        return layers.length === 0 ? null : { type: "MultiPolygon", coordinates: layers.map((l) => l.toGeoJSON().geometry.coordinates) };
    }

    async function postBoundary(type: BoundaryType, geometry: { type: string; coordinates: unknown[] } | null): Promise<any> {
        const response = await fetch(boundaryApiUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify({ boundary_type: type, polygon: geometry }),
        });
        if (!response.ok) {
            let msg = `HTTP ${response.status}`;
            try {
                msg = (await response.json()).error || msg;
            } catch {
                /* keep default */
            }
            throw new Error(msg);
        }
        return response.json();
    }

    function saveBoundary(options: { type?: BoundaryType; exitEdit?: boolean; quiet?: boolean } = {}): void {
        const type = options.type || editingBoundaryType;
        if (!type) return;
        const geometry = boundaryGeometryOf(type);
        postBoundary(type, geometry)
            .then((data) => {
                const exiting = options.exitEdit !== false;
                if (exiting) exitBoundaryEdit();
                // The server responds with the full refreshed payload (the clear
                // path falls back down the resolution chain server-side); only
                // redraw from it outside active editing so in-progress vertex
                // edits aren't clobbered.
                if (exiting || !boundaryDrawControl) applyBoundaryPayload(data);
                if (data.pending || data.refreshing) fetchBoundaries(0);
                if (!options.quiet) toast.success(geometry ? "Boundary saved." : "Boundary reset to the default.");
            })
            .catch((err) => toast.error(`Failed to save boundary: ${err.message}`));
    }

    async function convertBoundary(layer: L.Layer, from: BoundaryType): Promise<void> {
        const to: BoundaryType = from === "property" ? "building" : "property";
        boundaryGroups[from].removeLayer(layer);
        const path = layer as L.Path;
        path.setStyle(BOUNDARY_STYLES[to]);
        layer.unbindTooltip();
        layer.bindTooltip(to === "property" ? "Property boundary" : "Building boundary", {
            sticky: true,
            direction: "top",
            className: "boundary-tooltip",
        });
        boundaryGroups[to].addLayer(layer);
        attachBoundaryClickHandlers();
        try {
            await postBoundary(to, boundaryGeometryOf(to));
            const data = await postBoundary(from, boundaryGeometryOf(from));
            applyBoundaryPayload(data);
            if (data.pending || data.refreshing) fetchBoundaries(0);
            toast.success(to === "building" ? "Converted to a building boundary." : "Converted to a parcel boundary.");
        } catch (err) {
            toast.error(`Failed to convert boundary: ${err instanceof Error ? err.message : String(err)}`);
            fetchBoundaries(0);
        }
    }

    async function clearBoundary(): Promise<void> {
        if (!editingBoundaryType) return;
        if (!(await confirmAction({ title: "Clear Boundary", message: "Reset this boundary to its default?", confirmLabel: "Clear" }))) return;
        boundaryGroups[editingBoundaryType].clearLayers();
        saveBoundary();
    }

    function exitBoundaryEdit(): void {
        if (boundaryDrawControl) {
            map.removeControl(boundaryDrawControl);
            boundaryDrawControl = null;
        }
        map.off((L.Draw.Event as any).CREATED);
        map.off((L.Draw.Event as any).EDITED);
        map.off((L.Draw.Event as any).DELETED);
        if (editingBoundaryType) {
            boundaryGroups[editingBoundaryType].eachLayer((layer) => layer.off("edit", scheduleBoundaryAutoSave));
        }
        editingBoundaryType = null;
        map.getPane("boundaryPane")!.style.zIndex = "540";
        setBoundaryEditButtonsVisible(true);
        attachBoundaryClickHandlers();
    }

    function cancelBoundaryEdit(): void {
        const type = editingBoundaryType;
        exitBoundaryEdit();
        if (type) loadBoundary(type, savedBoundaries[type], boundarySources[type]);
    }

    function finishBoundaryEdit(): void {
        // Edits already autosave as they happen - just flush any pending debounced
        // save so the very last tweak isn't dropped, then leave edit mode.
        clearTimeout(boundaryAutoSaveTimer);
        saveBoundary();
    }

    window.startEditBoundary = startEditBoundary;
    window.saveBoundary = saveBoundary;
    window.clearBoundary = clearBoundary;
    window.cancelBoundaryEdit = cancelBoundaryEdit;
    window.finishBoundaryEdit = finishBoundaryEdit;

    // -- Circle-style swatch builder (bg/border for detail pins) ----------------
    const circlePalette = ["#e53e3e", "#1d4ed8", "#16a34a", "#d97706", "#7c3aed", "#0f172a", "#f8fafc", "#ffffff"];

    function buildCircleSwatches(containerId: string, inputId: string, currentVal: string, onChange?: (value: string) => void): void {
        const container = document.getElementById(containerId);
        if (!container) return;
        container.innerHTML = "";
        const nb = document.createElement("button");
        nb.type = "button";
        nb.title = "None";
        nb.className = `dp-color-swatch markup-color-swatch--none${!currentVal ? " dp-color-swatch--active" : ""}`;
        nb.style.cssText = "background:transparent;border:1px dashed #cbd5e1;position:relative;";
        nb.innerHTML = '<span style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.65rem;color:#9ca3af">∅</span>';
        nb.addEventListener("click", () => {
            container.querySelectorAll(".dp-color-swatch").forEach((b) => b.classList.remove("dp-color-swatch--active"));
            nb.classList.add("dp-color-swatch--active");
            (document.getElementById(inputId) as HTMLInputElement).value = "";
            onChange?.("");
        });
        container.appendChild(nb);
        circlePalette.forEach((color) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = `dp-color-swatch${color === currentVal ? " dp-color-swatch--active" : ""}`;
            btn.style.cssText = `background:${color};${color === "#f8fafc" || color === "#ffffff" ? "border:1px solid #cbd5e1;" : ""}`;
            btn.addEventListener("click", () => {
                container.querySelectorAll(".dp-color-swatch").forEach((b) => b.classList.remove("dp-color-swatch--active"));
                btn.classList.add("dp-color-swatch--active");
                (document.getElementById(inputId) as HTMLInputElement).value = color;
                onChange?.(color);
            });
            container.appendChild(btn);
        });
    }

    // -- Unified detail-pin panel (add + edit) -----------------------------------
    // Same panel/fields for both; instead of a second embedded map, placing or
    // moving the pin happens by clicking/dragging directly on the main map -
    // the map stays interactive and in view the whole time.
    let editingDp: DetailPinEntry | null = null;
    let dpMode: "add" | "edit" | null = null;
    let dpActiveMarker: L.Marker | null = null;
    let dpCreatedUuid: string | null = null;
    let dpAutoSaveTimer: ReturnType<typeof setTimeout> | undefined;
    let dpAutoSaveUuid: string | null = null;
    // Whether the user picked a Type in this panel session. The select's first
    // option is "Auto", meaning "work out whether this is a building from the
    // footprint under it" (see services.locations.site_scope) - so pin_type is
    // only ever submitted when it was deliberately chosen. Submitting it
    // regardless would mark every autosave as a user decision and freeze (or
    // overwrite) an automatic classification the server may have just made.
    let dpTypeTouched = false;

    function currentDpIcon(): L.DivIcon {
        return detailIcon({
            pin_type: (document.getElementById("dp-type") as HTMLInputElement).value,
            icon: (document.getElementById("dp-icon") as HTMLInputElement).value || null,
            color: (document.getElementById("dp-color") as HTMLInputElement).value || null,
            bg_color: (document.getElementById("dp-bg-color") as HTMLInputElement).value || "",
            bg_opacity: Number.parseInt((document.getElementById("dp-bg-opacity") as HTMLInputElement).value || "80", 10),
            border_color: (document.getElementById("dp-border-color") as HTMLInputElement).value || "",
            border_opacity: Number.parseInt((document.getElementById("dp-border-opacity") as HTMLInputElement).value || "100", 10),
        });
    }

    function updateDpMarkerIcon(): void {
        dpActiveMarker?.setIcon(currentDpIcon());
        scheduleDpAutoSave();
    }

    function collectDpFormData(): Record<string, unknown> {
        const data: Record<string, unknown> = {
            name: (document.getElementById("dp-name") as HTMLInputElement).value.trim(),
            description: (document.getElementById("dp-description") as HTMLInputElement).value.trim(),
            icon: (document.getElementById("dp-icon") as HTMLInputElement).value || null,
            color: (document.getElementById("dp-color") as HTMLInputElement).value || null,
            bg_color: (document.getElementById("dp-bg-color") as HTMLInputElement).value || null,
            bg_opacity: Number.parseInt((document.getElementById("dp-bg-opacity") as HTMLInputElement).value, 10),
            border_color: (document.getElementById("dp-border-color") as HTMLInputElement).value || null,
            border_opacity: Number.parseInt((document.getElementById("dp-border-opacity") as HTMLInputElement).value, 10),
            latitude: (document.getElementById("dp-lat") as HTMLInputElement).value,
            longitude: (document.getElementById("dp-lon") as HTMLInputElement).value,
        };
        if (dpTypeTouched) data.pin_type = (document.getElementById("dp-type") as HTMLInputElement).value;
        return data;
    }

    // Persists the new detail pin the instant it's first placed, so it survives
    // even if the user navigates away without explicitly finishing the dialog.
    function createDpImmediately(lat: number, lng: number): void {
        const data = collectDpFormData();
        data.latitude = lat.toFixed(6);
        data.longitude = lng.toFixed(6);
        fetch(cfg.detailPinCreateUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify(data),
        })
            .then((r) => r.json().then((resp) => {
                if (!r.ok || resp.ok === false) throw resp;
                return resp;
            }))
            .then((resp) => {
                dpCreatedUuid = resp.uuid;
                fetchBoundaries(0);
            })
            .catch((resp) => toast.error((resp && resp.error) || "Failed to save detail pin."));
    }

    // Every subsequent field/position change while still in 'add' mode patches the
    // already-created pin instead of waiting for an explicit save action.
    function scheduleDpAutoSave(): void {
        if (dpMode !== "add" || !dpCreatedUuid) return;
        dpAutoSaveUuid = dpCreatedUuid;
        clearTimeout(dpAutoSaveTimer);
        dpAutoSaveTimer = setTimeout(flushDpAutoSave, 500);
    }

    function flushDpAutoSave(): Promise<void> {
        clearTimeout(dpAutoSaveTimer);
        const uuid = dpAutoSaveUuid;
        dpAutoSaveUuid = null;
        if (!uuid) return Promise.resolve();
        return fetch(`${dpEditBase}${uuid}/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify(collectDpFormData()),
        })
            .then((r) => {
                // fetch only rejects on a network failure - a validation
                // error (400) resolved here and was swallowed as success,
                // so the edit looked saved while the server had discarded it.
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
            })
            .catch(() => toast.error("Failed to save detail pin changes."));
    }

    function setDpLocation(lat: number, lng: number): void {
        (document.getElementById("dp-lat") as HTMLInputElement).value = lat.toFixed(6);
        (document.getElementById("dp-lon") as HTMLInputElement).value = lng.toFixed(6);
        (document.getElementById("detail-pin-submit-btn") as HTMLButtonElement).disabled = false;
        document.getElementById("detail-pin-place-hint")?.classList.add("is-placed");
        document.getElementById("detail-pin-place-hint-text")!.textContent = dpMode === "edit" ? "Drag the pin to move it." : "Drag the pin, or click elsewhere to move it.";
    }

    function onDpMarkerDragEnd(): void {
        const pos = dpActiveMarker!.getLatLng();
        setDpLocation(pos.lat, pos.lng);
        scheduleDpAutoSave();
    }

    function onMainMapClickForDp(e: L.LeafletMouseEvent): void {
        // In edit mode the pin already exists on the map and is draggable/self-saving
        // (see loadDetailPins) - clicking elsewhere should pan/interact with the map
        // as normal, not silently relocate an existing pin.
        if (dpMode === "edit") return;
        const { lat, lng } = e.latlng;
        if (dpActiveMarker) {
            dpActiveMarker.setLatLng([lat, lng]);
            setDpLocation(lat, lng);
            scheduleDpAutoSave();
        } else {
            dpActiveMarker = L.marker([lat, lng], { icon: currentDpIcon(), draggable: true }).addTo(map);
            dpActiveMarker.on("dragend", onDpMarkerDragEnd);
            setDpLocation(lat, lng);
            createDpImmediately(lat, lng);
        }
    }

    function resetDpForm(): void {
        (document.getElementById("detail-pin-form") as HTMLFormElement).reset();
        dpTypeTouched = false;
        (document.getElementById("dp-lat") as HTMLInputElement).value = "";
        (document.getElementById("dp-lon") as HTMLInputElement).value = "";
        (document.getElementById("dp-icon") as HTMLInputElement).value = "";
        (document.getElementById("dp-color") as HTMLInputElement).value = "";
        (document.getElementById("dp-bg-color") as HTMLInputElement).value = "";
        (document.getElementById("dp-border-color") as HTMLInputElement).value = "";
        (document.getElementById("dp-bg-opacity") as HTMLInputElement).value = "80";
        (document.getElementById("dp-border-opacity") as HTMLInputElement).value = "100";
        document.getElementById("dp-bg-opacity-val")!.textContent = "80";
        document.getElementById("dp-border-opacity-val")!.textContent = "100";
        document.querySelectorAll("#dp-icon-picker .dp-icon-btn").forEach((b) => b.classList.remove("dp-icon-btn--active"));
        document.querySelectorAll("#dp-color-picker .dp-color-swatch").forEach((s) => s.classList.remove("dp-color-swatch--active"));
        buildCircleSwatches("dp-bg-swatches", "dp-bg-color", "", updateDpMarkerIcon);
        buildCircleSwatches("dp-border-swatches", "dp-border-color", "", updateDpMarkerIcon);
    }

    function openAddPinDialog(lat?: number, lng?: number): void {
        // Only one map side-panel open at a time - closing markup autosaves first.
        toolbar.closeMarkupPanel();
        // These modes claim the next map click too.
        disarmPlacement();

        dpMode = "add";
        editingDp = null;
        dpCreatedUuid = null;
        resetDpForm();

        document.getElementById("detail-pin-panel-title")!.textContent = "Add Detail Pin";
        document.getElementById("detail-pin-submit-btn")!.textContent = "Close";
        (document.getElementById("detail-pin-submit-btn") as HTMLButtonElement).disabled = true;
        (document.getElementById("detail-pin-delete-btn") as HTMLElement).hidden = true;
        document.getElementById("detail-pin-place-hint")?.classList.remove("is-placed");
        document.getElementById("detail-pin-place-hint-text")!.textContent = "Click anywhere on the map to place the pin.";
        (document.getElementById("detail-pin-panel") as HTMLElement).style.display = "";
        map.on("click", onMainMapClickForDp);
        if (lat != null && lng != null) {
            onMainMapClickForDp({ latlng: L.latLng(lat, lng) } as L.LeafletMouseEvent);
        }
    }

    function openDetailPinEditDialog(dp: DetailPinEntry): void {
        // Only one map side-panel open at a time - closing markup autosaves first.
        toolbar.closeMarkupPanel();
        // These modes claim the next map click too.
        disarmPlacement();

        dpMode = "edit";
        editingDp = dp;
        resetDpForm();

        document.getElementById("detail-pin-panel-title")!.textContent = "Edit Detail Pin";
        document.getElementById("detail-pin-submit-btn")!.textContent = "Save Changes";
        (document.getElementById("detail-pin-submit-btn") as HTMLButtonElement).disabled = false;
        (document.getElementById("detail-pin-delete-btn") as HTMLElement).hidden = false;
        document.getElementById("detail-pin-place-hint")?.classList.add("is-placed");
        document.getElementById("detail-pin-place-hint-text")!.textContent = "Drag the pin to move it.";

        (document.getElementById("dp-name") as HTMLInputElement).value = dp.name || "";
        (document.getElementById("dp-description") as HTMLInputElement).value = dp.description || "";
        (document.getElementById("dp-type") as HTMLInputElement).value = dp.pin_type || "poi";
        (document.getElementById("dp-icon") as HTMLInputElement).value = dp.icon || "";
        (document.getElementById("dp-color") as HTMLInputElement).value = dp.color || "";
        (document.getElementById("dp-lat") as HTMLInputElement).value = String(dp.latitude);
        (document.getElementById("dp-lon") as HTMLInputElement).value = String(dp.longitude);

        document.querySelectorAll<HTMLElement>("#dp-icon-picker .dp-icon-btn").forEach((b) => {
            b.classList.toggle("dp-icon-btn--active", b.dataset.icon === dp.icon);
        });
        document.querySelectorAll<HTMLElement>("#dp-color-picker .dp-color-swatch").forEach((s) => {
            s.classList.toggle("dp-color-swatch--active", s.dataset.color === dp.color);
        });

        const bgOpacity = dp.bg_opacity != null ? dp.bg_opacity : 80;
        (document.getElementById("dp-bg-color") as HTMLInputElement).value = dp.bg_color || "";
        (document.getElementById("dp-bg-opacity") as HTMLInputElement).value = String(bgOpacity);
        document.getElementById("dp-bg-opacity-val")!.textContent = String(bgOpacity);
        buildCircleSwatches("dp-bg-swatches", "dp-bg-color", dp.bg_color || "", updateDpMarkerIcon);

        const bdOpacity = dp.border_opacity != null ? dp.border_opacity : 100;
        (document.getElementById("dp-border-color") as HTMLInputElement).value = dp.border_color || "";
        (document.getElementById("dp-border-opacity") as HTMLInputElement).value = String(bdOpacity);
        document.getElementById("dp-border-opacity-val")!.textContent = String(bdOpacity);
        buildCircleSwatches("dp-border-swatches", "dp-border-color", dp.border_color || "", updateDpMarkerIcon);

        (document.getElementById("detail-pin-panel") as HTMLElement).style.display = "";

        // Manipulate the pin's real marker directly rather than a stand-in - it's
        // already draggable and self-saving (see loadDetailPins); this just keeps
        // the panel's hidden lat/lon fields in sync with it while open.
        dpActiveMarker = dp.marker;
        dp.marker?.on("dragend", onDpMarkerDragEnd);
        map.on("click", onMainMapClickForDp);
    }

    function closeDetailPinPanel(): void {
        (document.getElementById("detail-pin-panel") as HTMLElement).style.display = "none";
        map.off("click", onMainMapClickForDp);
        // A pin created via 'add' mode is already persisted (see createDpImmediately) -
        // swap the provisional local marker for the fully-wired one loadDetailPins builds
        // (autosaving drag, click-to-edit, sidebar list entry) instead of discarding it.
        const wasAdding = dpMode === "add" && dpCreatedUuid;
        if (dpActiveMarker) {
            dpActiveMarker.off("dragend", onDpMarkerDragEnd);
            if (dpMode === "add") map.removeLayer(dpActiveMarker);
        }
        dpActiveMarker = null;
        dpMode = null;
        editingDp = null;
        dpCreatedUuid = null;
        if (wasAdding) Promise.resolve(flushDpAutoSave()).finally(loadDetailPins);
    }

    window.openAddPinDialog = openAddPinDialog;
    window.closeDetailPinPanel = closeDetailPinPanel;

    document.getElementById("dp-icon-picker")?.addEventListener("click", function (this: HTMLElement, e) {
        const btn = (e.target as HTMLElement).closest<HTMLElement>(".dp-icon-btn");
        if (!btn) return;
        this.querySelectorAll(".dp-icon-btn").forEach((b) => b.classList.remove("dp-icon-btn--active"));
        btn.classList.add("dp-icon-btn--active");
        (document.getElementById("dp-icon") as HTMLInputElement).value = btn.dataset.icon ?? "";
        updateDpMarkerIcon();
    });
    document.getElementById("dp-color-picker")?.addEventListener("click", function (this: HTMLElement, e) {
        const sw = (e.target as HTMLElement).closest<HTMLElement>(".dp-color-swatch");
        if (!sw) return;
        this.querySelectorAll(".dp-color-swatch").forEach((s) => s.classList.remove("dp-color-swatch--active"));
        sw.classList.add("dp-color-swatch--active");
        (document.getElementById("dp-color") as HTMLInputElement).value = sw.dataset.color ?? "";
        updateDpMarkerIcon();
    });
    document.getElementById("dp-bg-opacity")?.addEventListener("input", function (this: HTMLInputElement) {
        document.getElementById("dp-bg-opacity-val")!.textContent = this.value;
        updateDpMarkerIcon();
    });
    document.getElementById("dp-border-opacity")?.addEventListener("input", function (this: HTMLInputElement) {
        document.getElementById("dp-border-opacity-val")!.textContent = this.value;
        updateDpMarkerIcon();
    });
    document.getElementById("dp-type")?.addEventListener("change", () => {
        dpTypeTouched = true;
        updateDpMarkerIcon();
    });
    document.getElementById("dp-name")?.addEventListener("input", scheduleDpAutoSave);
    document.getElementById("dp-description")?.addEventListener("input", scheduleDpAutoSave);

    document.getElementById("detail-pin-form")?.addEventListener("submit", (e) => {
        e.preventDefault();
        if (dpMode === "add") {
            // Already saved incrementally as each change was made (see
            // createDpImmediately/scheduleDpAutoSave) - this button just closes
            // the panel; closeDetailPinPanel flushes any pending debounced save.
            closeDetailPinPanel();
            return;
        }
        const lat = (document.getElementById("dp-lat") as HTMLInputElement).value;
        const lon = (document.getElementById("dp-lon") as HTMLInputElement).value;
        if (!lat || !lon) {
            toast.warning("Click a point on the map to set the location first.");
            return;
        }
        const submitBtn = document.getElementById("detail-pin-submit-btn") as HTMLButtonElement;
        submitBtn.disabled = true;
        const data = collectDpFormData();
        fetch(`${dpEditBase}${editingDp!.uuid}/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify(data),
        })
            .then((r) => r.json().then((resp) => {
                if (!r.ok || resp.ok === false) throw resp;
                return resp;
            }))
            .then(() => {
                toast.success("Detail pin updated.");
                closeDetailPinPanel();
                loadDetailPins();
                fetchBoundaries(0);
            })
            .catch((resp) => {
                toast.error((resp && resp.error) || "Failed to save detail pin.");
                submitBtn.disabled = false;
            });
    });

    document.getElementById("detail-pin-delete-btn")?.addEventListener("click", async () => {
        if (!editingDp) return;
        if (!(await confirmAction({ title: "Delete Pin", message: `Delete "${editingDp.name}"?`, confirmLabel: "Delete" }))) return;
        fetch(`${dpEditBase}${editingDp.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } })
            .then((r) => {
                if (!r.ok) throw new Error();
                closeDetailPinPanel();
                loadDetailPins();
                fetchBoundaries(0);
                toast.success("Detail pin deleted.");
            })
            .catch(() => toast.error("Failed to delete detail pin."));
    });

    // -- Boundaries: click or right-click a polygon for Edit / Convert / Delete --
    // Leaflet's event system has no jQuery-style dot-namespacing - a listener
    // registered for the literal string 'click.openEditor' never matches a real
    // click, which Leaflet always fires as plain 'click'. Bind/unbind a named
    // handler under the real event name instead.
    function onBoundaryLayerClick(e: L.LeafletMouseEvent): void {
        if (boundaryDrawControl) return;
        // Don't hijack a click that's actually meant to draw a shape onto (or drop
        // a detail pin inside) this boundary - without this, clicking a boundary
        // while a tool is armed always opened the context menu instead of placing
        // the point, making it impossible to draw into a boundary polygon at all.
        // Not stopping propagation here lets the click keep bubbling to the map's
        // own click handler (the draw session / detail-pin placement listener).
        if (toolbar.isDrawBusy() || dpMode === "add") return;
        L.DomEvent.stopPropagation(e);
        openBoundaryCtxMenu(e.target as L.Layer, e);
    }
    function onBoundaryLayerContextMenu(e: L.LeafletMouseEvent): void {
        if (boundaryDrawControl || toolbar.isDrawBusy() || dpMode === "add") return;
        L.DomEvent.stopPropagation(e);
        openBoundaryCtxMenu(e.target as L.Layer, e);
    }
    function attachBoundaryClickHandlers(): void {
        (["property", "building"] as BoundaryType[]).forEach((type) => {
            boundaryGroups[type].eachLayer((layer) => {
                layer.off("click", onBoundaryLayerClick);
                layer.on("click", onBoundaryLayerClick);
                layer.off("contextmenu", onBoundaryLayerContextMenu);
                layer.on("contextmenu", onBoundaryLayerContextMenu);
            });
        });
    }

    function childPinMenuItems(lat: number, lng: number): ContextMenuItem[] {
        return [
            {
                icon: "add_location",
                label: "Create child pin here",
                onClick: () => openAddPinDialog(lat, lng),
            },
        ];
    }

    function openBoundaryCtxMenu(layer: L.Layer, event: L.LeafletMouseEvent): void {
        const layerType = boundaryTypeOfLayer(layer);
        const extraItems: ContextMenuItem[] = [...childPinMenuItems(event.latlng.lat, event.latlng.lng)];
        if (layerType) {
            extraItems.push({
                icon: "edit",
                label: "Edit boundary",
                onClick: () => startEditBoundary(layerType),
            });
            if (boundarySources[layerType] !== "circle") {
                extraItems.push({
                    icon: "swap_horiz",
                    label: layerType === "property" ? "Convert to building boundary" : "Convert to parcel boundary",
                    onClick: () => {
                        void convertBoundary(layer, layerType);
                    },
                });
            }
            extraItems.push({
                icon: "delete_outline",
                label: "Delete boundary",
                className: "map-context-menu__item--danger",
                onClick: () => {
                    void (async () => {
                        if (!(await confirmAction({ title: "Delete Boundary", message: "Delete this boundary polygon?", confirmLabel: "Delete" }))) return;
                        boundaryGroups[layerType].removeLayer(layer);
                        if (layerType === "property" && boundaryGroups.property.getLayers().length === 0) setMainMarkerVisible(true);
                        saveBoundary({ exitEdit: false, type: layerType });
                    })();
                },
            });
        }
        showMapContextMenu({
            lat: event.latlng.lat,
            lng: event.latlng.lng,
            zoom: map.getZoom(),
            clientX: event.originalEvent.clientX,
            clientY: event.originalEvent.clientY,
            extraItems,
        });
    }

    bindMapContextMenu(map, {
        extraItems: (lat, lng) => childPinMenuItems(lat, lng),
        shouldOpen: () => !toolbar.isDrawBusy() && dpMode !== "add" && !boundaryDrawControl && !pendingPlacement,
    });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}

interface GalleryImage {
    id: number;
    url: string;
    /** Tiny map-marker preview; absent for a row that hasn't been generated one yet. */
    marker_thumb_url?: string;
    latitude: number | null;
    longitude: number | null;
}

declare global {
    interface Window {
        // Read by base.html's comment map composer as its default center.
        _commentMapDefaultLat: number;
        _commentMapDefaultLng: number;
        map: L.Map;

        // Markup toolbar functions, exposed for the top-right toolbar's
        // markup_* buttons / _markup_panel_dialog.html's inline onclick= attributes.
        startMarkupDraw: (type: string) => void;
        startShapeDraw: (type: string) => void;
        startTextPlacement: () => void;
        closeMarkupPanel: () => void;
        _closeMarkupDraw: () => void;
        deleteMarkupEdit: () => Promise<void>;
        openMarkupEditDialog: (item: MarkupItem) => void;
        loadMarkup: () => void;
        // Applies the edit panel's fields (label/width/opacity/security/layer) to
        // the item being edited - called by _markup_panel_dialog.html's inline
        // oninput=/onchange= attributes on every field in that panel.
        _liveApplyMarkupEdit: () => void;

        // "Take a screenshot" toolbar button (_map_annotations_panels.html) -
        // opens the shared standalone map composer pre-scoped to this pin/wiki.
        _openMapScreenshot: () => void;
        openBuildingImportDialog: () => void;
        toggleBuildingImportSelectMode: () => void;

        // Detail-pin/boundary functions, exposed for this page's own template onclick= attributes.
        _toggleDetailPinListPanel: () => void;
        toggleDetailPinSelectMode: () => void;
        openAddPinDialog: (lat?: number, lng?: number) => void;
        closeDetailPinPanel: () => void;
        startEditBoundary: (type: "property" | "building") => void;
        saveBoundary: (options?: { type?: "property" | "building"; exitEdit?: boolean; quiet?: boolean }) => void;
        clearBoundary: () => Promise<void>;
        cancelBoundaryEdit: () => void;
        finishBoundaryEdit: () => void;
        _boundaryDrawToggleWired?: boolean;

        // Satellite/street-view carousel controls, exposed for satellite_view.html /
        // street_view.html's onclick=/onerror= attributes - see their definitions
        // above for why they live here instead of in those fragments' own scripts.
        _satRemoveSlide: (img: HTMLImageElement) => void;
        _satPrev: () => void;
        _satNext: () => void;
        _satShow: (idx: number) => void;
        _satShowRemembered: () => void;
        _svRemoveSlide: (img: HTMLImageElement) => void;
        _svShowStaticFallback: (btn: HTMLButtonElement) => void;
        _svPrev: () => void;
        _svNext: () => void;
        _svShow: (idx: number) => void;

        // External photo-gallery integration hooks (gallery.ts, out of scope for
        // this migration) - this page calls out to them and also implements the
        // three the gallery calls back into.
        galleryRepositionImage?: (imgId: number, lat: number, lng: number, onRejected: () => void) => void;
        gallerySetPhotoMapHidden?: (imgId: number, hidden: boolean, onRejected?: () => void) => void;
        galleryOpenLightbox?: (imgId: number, opts: { url: string }) => void;
        _galleryAddMarker: (img: GalleryImage) => void;
        // Optional to match the ambient declaration in types/globals.d.ts: the
        // gallery partial calls it defensively on pages where this entry never ran.
        _galleryRemoveMarker?: (imgId: number) => void;
        _galleryHighlightMarker: (imgId: number, on: boolean) => void;

        // Media-section drag-onto-map integration (pages/location/index.html).
        // The dragged tile's own element, stashed by mediaItemDragStart so the
        // drop handler above can update its visual state - HTML5 drag-and-drop
        // only carries string data through dataTransfer, not element refs.
        _mediaDragItemEl?: HTMLElement;
        mediaApplyMaterializedDrop?: (itemEl: HTMLElement | undefined, data: Record<string, unknown>) => void;
        // Touch counterpart to that drag: arms the tile so the next map tap
        // places it. Called from the tile's own "place on map" control.
        mediaPlaceOnMap?: (itemEl: HTMLElement) => void;
    }
}
