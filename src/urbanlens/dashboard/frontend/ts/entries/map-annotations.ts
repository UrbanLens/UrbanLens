/**
 * Shared map annotations page: markup drawing/editing, the unified detail-pin
 * side panel, the typed boundary editor (+ its context menu), the photo
 * layer, and the Details/Photos layers list panel. Used identically by the
 * pin detail page and the Location wiki page.
 *
 * Ported from `_map_annotations_script.html` + the spliced-in
 * `_markup_toolbar_script.html` fragment. Config previously baked into the
 * script via `{{ }}`/`{% %}` now comes from data-* attributes on `#map`
 * (see templates/dashboard/pages/location/index.html and wiki.html).
 */
import { getCsrfToken } from "../shared/csrf";
import { toast, confirmAction } from "../shared/dialogs";
import { createMapLayers } from "../shared/map-layers";
import type { MarkupItem, MarkupToolbar } from "../shared/markup-toolbar";

// See markup-engine.ts for why `L` is declared locally instead of imported.
declare const L: typeof import("leaflet");
// Triggers TS to pick up @types/leaflet-draw's `declare module "leaflet"`
// augmentation (L.Draw, L.Control.Draw, L.EditToolbar, ...) - erased at
// build time, no runtime import (leaflet-draw is loaded via CDN like Leaflet
// itself, only referenced here as an ambient global via `L`).
import type {} from "leaflet-draw";

interface DetailPinEntry {
    uuid: string;
    /** Slug of the child pin's own detail page (Pin-backed detail pins only). */
    slug?: string;
    /** URL of the child pin's own detail page (Pin-backed detail pins only). */
    url?: string;
    /** Name of the child pin this entry belongs to, when it came from a
     * descendant (the page-wide "show sub pin details" toggle). Entries with
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

function escHtml(s: string): string {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function readConfig(el: HTMLElement) {
    const d = el.dataset;
    return {
        mapCenterLat: Number.parseFloat(d.mapCenterLat ?? "0"),
        mapCenterLng: Number.parseFloat(d.mapCenterLng ?? "0"),
        pinSlug: d.pinSlug || "",
        locationSlug: d.locationSlug || "",
        defaultMapView: d.defaultMapView || "satellite",
        openweathermapApiKey: d.openweathermapApiKey || "",
        mainMarkerOwnerUuid: d.mainMarkerOwnerUuid || "",
        markupJsonUrl: d.markupJsonUrl || "",
        markupCreateUrl: d.markupCreateUrl || "",
        markupEditUrlTemplate: d.markupEditUrlTemplate || "",
        detailPinsJsonUrl: d.detailPinsJsonUrl || "",
        detailPinCreateUrl: d.detailPinCreateUrl || "",
        detailPinEditUrlTemplate: d.detailPinEditUrlTemplate || "",
        boundaryUrl: d.boundaryUrl || "",
        photoGalleryJsonUrl: d.photoGalleryJsonUrl || "",
        nearbyPinsJsonUrl: d.nearbyPinsJsonUrl || "",
        markupFillOpacity: d.markupFillOpacity ? Number.parseInt(d.markupFillOpacity, 10) : 87,
        markupBorderOpacity: d.markupBorderOpacity ? Number.parseInt(d.markupBorderOpacity, 10) : 100,
        showOnboardingTips: d.showOnboardingTips === "1",
    };
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
    const map = L.map("map", { scrollWheelZoom: false, attributionControl: false }).setView([mapCenterLat, mapCenterLng], 15);
    window.map = map;

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
        iconUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png",
        shadowUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png",
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
        mainMarker.on("dragend", () => {
            const pos = mainMarker.getLatLng();
            fetch(`/dashboard/rest/pins/${cfg.mainMarkerOwnerUuid}/`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ latitude: pos.lat.toFixed(6), longitude: pos.lng.toFixed(6) }),
            })
                .then((r) => {
                    if (!r.ok) throw new Error();
                    return r.json();
                })
                .then(() => {
                    mainMarkerLat = pos.lat;
                    mainMarkerLng = pos.lng;
                    toast.success("Pin moved.");
                })
                .catch(() => {
                    toast.error("Failed to save new position.");
                    mainMarker.setLatLng([mainMarkerLat, mainMarkerLng]);
                });
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
    const detailPinColors: Record<string, string> = { building: "#6b7280", entrance: "#16a34a", poi: "#d97706", danger: "#dc2626", other: "#7c3aed", location: "#2563eb" };
    const detailPinIcons: Record<string, string> = { building: "business", entrance: "door_front", poi: "star", danger: "warning", other: "info", location: "place" };
    // Both sub-layers live inside detailsLayer so one toggle shows/hides everything.
    const detailPinLayer = L.layerGroup();
    const markupLayer = L.layerGroup();
    const detailsLayer = L.layerGroup([detailPinLayer, markupLayer]).addTo(map);

    const photoLayer = L.layerGroup().addTo(map);

    // -- Nearby pins layer -----------------------------------------------------
    // This profile's other pins near the one being viewed. Off by default and
    // fetched lazily the first time the layer is turned on (mirrors the main
    // map's "Sub Pins" layer - see setChildPinsActive in pages/map/index.html).
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
                // main map's "Sub Pins" layer failure behavior.
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

    // Shared layers engine + panel - the exact same component as the main map
    // (see {% map_layers_panel %} in _map_annotations_panels.html). Details and
    // Photos are this page's own layer groups, registered as custom toggles.
    createMapLayers(map, {
        root: document.getElementById("detail-map-layers"),
        apiKey: cfg.openweathermapApiKey || null,
        defaultBase: cfg.defaultMapView,
        onAttribution: (text) => {
            const el = document.getElementById("page-footer-attribution-text");
            if (el) el.textContent = text;
        },
        custom: {
            details: {
                isActive: () => map.hasLayer(detailsLayer),
                toggle: () => (map.hasLayer(detailsLayer) ? map.removeLayer(detailsLayer) : detailsLayer.addTo(map)),
            },
            photos: {
                isActive: () => map.hasLayer(photoLayer),
                toggle: () => (map.hasLayer(photoLayer) ? map.removeLayer(photoLayer) : photoLayer.addTo(map)),
            },
            nearby: {
                isActive: () => nearbyActive,
                toggle: () => setNearbyActive(!nearbyActive),
            },
        },
    });

    // URL base for detail pin edit/delete: strip the placeholder UUID off the end.
    const dpEditBase = cfg.detailPinEditUrlTemplate.replace("00000000-0000-0000-0000-000000000000/", "");

    let detailPins: DetailPinEntry[] = [];
    let highlightedDpUuid: string | null = null;
    let photoPanelItems: PhotoPanelItem[] = [];
    const photoMarkers: Record<number, { marker: L.Marker; url: string; lat: number; lng: number }> = {};

    function hexToRgb(hex: string): string {
        const r = Number.parseInt(hex.slice(1, 3), 16);
        const g = Number.parseInt(hex.slice(3, 5), 16);
        const b = Number.parseInt(hex.slice(5, 7), 16);
        return `${r},${g},${b}`;
    }

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

        return L.divIcon({
            className: "",
            html: `<span style="position:relative;display:inline-flex;align-items:center;justify-content:center;border-radius:50%;${bgStyle}${bdStyle}padding:${pad}px;">${ring}<span class="material-icons detail-map-icon" style="color:${color};font-size:${size}px;line-height:1;">${icon}</span></span>`,
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
        if (countLabel) countLabel.textContent = `${total} Layer${total === 1 ? "" : "s"}`;
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
            li.innerHTML = `
                <span class="material-icons detail-pin-list-item-icon" style="color:${escHtml(item.color)}">${escHtml(markupIcon[item.markup_type] || "edit")}</span>
                <span class="detail-pin-list-item-name">${escHtml(displayName)}</span>
                ${ownerMeta}
                ${item.owner_name ? "" : `<button type="button" class="detail-pin-list-item-delete" title="Delete"><i class="material-symbols-outlined">close</i></button>`}`;
            li.addEventListener("click", (e) => {
                if ((e.target as HTMLElement).closest(".detail-pin-list-item-delete")) return;
                if (item.owner_name) return; // child-pin markup is edited from its own page
                toolbar.openMarkupEditDialog(item);
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
        const iframe = slide.querySelector<HTMLIFrameElement>(".sv-embed");
        const staticImg = slide.querySelector<HTMLImageElement>(".sv-img--fallback");
        if (iframe) iframe.hidden = true;
        if (staticImg) staticImg.hidden = false;
        btn.hidden = true;
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

    // Popup shown when a child pin's marker is clicked: name, which sub pin it
    // belongs to (for nested entries), and a link to that pin's own detail
    // page - plus Edit/promote-to-parent shortcuts for this pin's own direct
    // children (no hover tooltip - the click popup already covers this, and a
    // separate hover tooltip here was previously unreadable in dark mode).
    function detailPinPopupContent(entry: DetailPinEntry): HTMLElement {
        const el = document.createElement("div");
        el.className = "pin-popup child-pin-popup";
        const owner = entry.owner_name ? `<div class="popup-child-parent"><i class="material-symbols-outlined">subdirectory_arrow_right</i> Inside ${escHtml(entry.owner_name)}</div>` : "";
        el.innerHTML = `
            <div class="popup-title">${escHtml(entry.name || "Sub pin")}</div>
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
            editBtn.title = "Edit sub pin";
            editBtn.innerHTML = '<i class="material-symbols-outlined">edit</i>';
            editBtn.addEventListener("click", () => {
                map.closePopup();
                openDetailPinEditDialog(entry);
            });
            actions.appendChild(editBtn);
        }
        return el;
    }

    function loadDetailPins(): void {
        fetch(cfg.detailPinsJsonUrl)
            .then((r) => r.json())
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
                    // owner/actions, and a separate hover tooltip here was previously
                    // unreadable in dark mode (dark text on a dark background).
                    const marker = L.marker([dp.latitude, dp.longitude], { icon: detailIcon(entry), draggable: !entry.owner_name });
                    if (entry.url) {
                        marker.bindPopup(detailPinPopupContent(entry));
                    } else {
                        // Wiki child markers have no personal detail page - keep the
                        // direct click-to-edit behavior there.
                        marker.on("click", () => openDetailPinEditDialog(entry));
                    }
                    // Select-mode click toggles selection instead of opening the popup
                    // or the editor - mirrors the main map's marker click handling.
                    marker.on("click", (e) => {
                        if (!detailSelectMode || entry.owner_name) return;
                        marker.closePopup();
                        L.DomEvent.stop(e);
                        toggleDpSelection(entry.uuid);
                    });
                    marker.on("dragend", () => {
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
                            })
                            .catch(() => {
                                toast.error("Failed to save new position.");
                                marker.setLatLng([entry.latitude, entry.longitude]);
                            });
                    });
                    marker.addTo(detailPinLayer);
                    entry.marker = marker;
                    detailPins.push(entry);
                });
                buildDetailList();
            })
            .catch((err) => console.warn("Could not load detail pins:", err));
    }

    // -- Detail pin multi-select: promote or delete several sub pins at once --
    // Pin-only (cfg.pinSlug is empty on the wiki page, which shares this module
    // but has no reparentable Pin-backed detail pins to act on) - the button is
    // removed there. Nested entries (entry.owner_name set) are display-only and
    // never selectable, matching their existing non-draggable/non-editable state.
    let detailSelectMode = false;
    const selectedDpUuids = new Set<string>();
    let dpDragSelectRect: L.Rectangle | null = null;

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
        btn.setAttribute("data-tooltip", hasSelectable ? "Select multiple sub pins to promote or delete" : "This pin has no sub pins to select");
        if (!hasSelectable && detailSelectMode) exitDetailPinSelectMode();
    }

    function toggleDetailPinSelectMode(): void {
        if (detailSelectMode) exitDetailPinSelectMode();
        else enterDetailPinSelectMode();
    }
    window.toggleDetailPinSelectMode = toggleDetailPinSelectMode;

    function enterDetailPinSelectMode(): void {
        if (detailSelectMode || !detailSelectableEntries().length) return;
        detailSelectMode = true;
        document.getElementById("select-detail-pins-button")?.classList.add("active");
        document.getElementById("map")?.classList.add("select-mode");
        map.dragging.disable();
    }

    function exitDetailPinSelectMode(): void {
        if (!detailSelectMode) return;
        detailSelectMode = false;
        document.getElementById("select-detail-pins-button")?.classList.remove("active");
        document.getElementById("map")?.classList.remove("select-mode");
        map.dragging.enable();
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
                      promote: doPromoteSelectedDp,
                      delete: doDeleteSelectedDp,
                      deselect: clearDpSelection,
                  }
                : {},
        );
    }

    async function doPromoteSelectedDp(): Promise<void> {
        const uuids = Array.from(selectedDpUuids);
        if (!uuids.length) return;
        const n = uuids.length;
        if (!(await confirmAction({ title: "Promote sub pins?", message: `Promote ${n} sub pin${n === 1 ? "" : "s"} to top-level pins on your main map?`, confirmLabel: "Promote" }))) return;
        const results = await Promise.all(
            uuids.map((uuid) => {
                const slug = detailPins.find((d) => d.uuid === uuid)?.slug || uuid;
                return fetch(`/dashboard/map/pin/${encodeURIComponent(slug)}/detach-parent/`, {
                    method: "POST",
                    headers: { "X-CSRFToken": getCsrfToken() },
                }).then((r) => r.ok);
            }),
        );
        const promoted = results.filter(Boolean).length;
        if (promoted) toast.success(`${promoted} pin${promoted === 1 ? "" : "s"} promoted.`);
        if (promoted < n) toast.warning(`${n - promoted} pin${n - promoted === 1 ? "" : "s"} could not be promoted (location conflict).`);
        clearDpSelection();
        loadDetailPins();
    }

    async function doDeleteSelectedDp(): Promise<void> {
        const uuids = Array.from(selectedDpUuids);
        if (!uuids.length) return;
        const n = uuids.length;
        if (!(await confirmAction({ title: "Delete sub pins?", message: `Delete ${n} sub pin${n === 1 ? "" : "s"}? This also removes reviews, visit history, and notes.`, confirmLabel: "Delete" }))) return;
        const results = await Promise.all(uuids.map((uuid) => fetch(`${dpEditBase}${uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } }).then((r) => r.ok)));
        const deleted = results.filter(Boolean).length;
        if (deleted) toast.success(`${deleted} pin${deleted === 1 ? "" : "s"} deleted.`);
        if (deleted < n) toast.warning(`${n - deleted} pin${n - deleted === 1 ? "" : "s"} could not be deleted.`);
        clearDpSelection();
        loadDetailPins();
    }

    // Rectangle drag-select over detail-pin markers, mirroring the main map's
    // multi-select tool (_initSelectDragRectangle in pages/map/index.html).
    (function initDetailPinDragSelect() {
        mapEl.addEventListener("mousedown", (e: MouseEvent) => {
            if (!detailSelectMode || e.button !== 0) return;
            const startLL = map.mouseEventToLatLng(e);
            const startX = e.clientX;
            const startY = e.clientY;
            let dragging = false;

            function onMove(ev: MouseEvent): void {
                if (!dragging && Math.hypot(ev.clientX - startX, ev.clientY - startY) < 6) return;
                dragging = true;
                if (dpDragSelectRect) map.removeLayer(dpDragSelectRect);
                dpDragSelectRect = L.rectangle(L.latLngBounds(startLL, map.mouseEventToLatLng(ev)), {
                    color: "#1E88E5",
                    weight: 2,
                    fillOpacity: 0.08,
                    dashArray: "4 4",
                    interactive: false,
                }).addTo(map);
            }
            function onUp(ev: MouseEvent): void {
                document.removeEventListener("mousemove", onMove);
                if (dpDragSelectRect) {
                    map.removeLayer(dpDragSelectRect);
                    dpDragSelectRect = null;
                }
                if (!dragging) return;
                const bounds = L.latLngBounds(startLL, map.mouseEventToLatLng(ev));
                detailSelectableEntries().forEach((dp) => {
                    if (dp.marker && !selectedDpUuids.has(dp.uuid) && bounds.contains(dp.marker.getLatLng())) toggleDpSelection(dp.uuid);
                });
            }
            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", onUp, { once: true });
        });
    })();

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
    });

    window.startMarkupDraw = toolbar.startMarkupDraw;
    window.startShapeDraw = toolbar.startShapeDraw;
    window.startTextPlacement = toolbar.startTextPlacement;
    window.closeMarkupPanel = toolbar.closeMarkupPanel;
    window._closeMarkupDraw = toolbar.closeOrFinishDraw;
    window.deleteMarkupEdit = toolbar.deleteMarkupEdit;
    window.openMarkupEditDialog = toolbar.openMarkupEditDialog;
    window.loadMarkup = toolbar.loadMarkup;

    loadDetailPins();

    // -- Photo panel -----------------------------------------------------------
    function makePhotoIcon(url: string, size: number, highlighted?: boolean): L.DivIcon {
        const shadow = highlighted ? "0 0 0 3px #2563eb, 0 3px 10px rgba(0,0,0,.45)" : "0 2px 6px rgba(0,0,0,.35)";
        return L.divIcon({
            className: "",
            html: `<img src="${url}" class="photo-marker-img" style="width:${size}px;height:${size}px;object-fit:cover;border-radius:5px;border:2px solid #fff;box-shadow:${shadow};display:block;transition:transform .15s,box-shadow .15s;">`,
            iconSize: [size, size],
            iconAnchor: [size / 2, size / 2],
        });
    }

    function addPhotoMarker(imgId: number, url: string, lat: number, lng: number, ownerName?: string): void {
        if (photoMarkers[imgId]) photoLayer.removeLayer(photoMarkers[imgId]!.marker);
        // Photos belonging to a child pin (ownerName) are display-only on this
        // map - they're repositioned from their own pin's page.
        const marker = L.marker([lat, lng], { icon: makePhotoIcon(url, 44, false), draggable: !ownerName });
        if (ownerName) marker.bindTooltip(`Photo from ${ownerName}`, { permanent: false, direction: "top", className: "detail-pin-tooltip" });
        marker.on("dragend", () => {
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
        marker.addTo(photoLayer);
        photoMarkers[imgId] = { marker, url, lat, lng };
    }

    window._galleryAddMarker = (img) => {
        if (!photoPanelItems.find((p) => p.id === img.id)) photoPanelItems.push({ id: img.id, url: img.url, lat: img.latitude, lng: img.longitude, mine: true });
        if (img.latitude != null && img.longitude != null) addPhotoMarker(img.id, img.url, img.latitude, img.longitude);
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
            const sz = on ? 56 : 44;
            entry.marker.setIcon(makePhotoIcon(entry.url, sz, on));
            if (on) map.panTo([entry.lat, entry.lng]);
        }
        document.querySelectorAll<HTMLElement>(".photo-panel-item").forEach((li) => {
            li.classList.toggle("is-highlighted", +(li.dataset.id ?? "") === imgId && !!on);
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
            li.innerHTML = `
                <div class="photo-panel-thumb-wrap">
                    <img src="${img.url}" class="photo-panel-thumb" alt="" draggable="false">
                    <span class="photo-panel-coord-badge ${hasCoords ? "has-gps" : "no-gps"}" title="${hasCoords ? "Has GPS" : "No GPS"}">
                        <i class="material-icons">${hasCoords ? "place" : "location_off"}</i>
                    </span>
                </div>`;
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
    }

    // Drop photo onto map to assign coordinates.
    mapEl.addEventListener("dragover", (e) => {
        if (!e.dataTransfer?.types.includes("text/photoid")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        mapEl.classList.add("photo-drop-target");
    });
    mapEl.addEventListener("dragleave", () => mapEl.classList.remove("photo-drop-target"));
    mapEl.addEventListener("drop", (e) => {
        mapEl.classList.remove("photo-drop-target");
        const idStr = e.dataTransfer?.getData("text/photoid");
        if (!idStr) return;
        e.preventDefault();
        const imgId = Number.parseInt(idStr, 10);
        const rect = mapEl.getBoundingClientRect();
        const latlng = map.containerPointToLatLng([e.clientX - rect.left, e.clientY - rect.top]);
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
                if (img.latitude != null && img.longitude != null) addPhotoMarker(img.id, img.url, img.latitude, img.longitude, img.child_pin_name);
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
            if (entry.polygon) addGeoJSONPolygons(detailBuildingItems, entry.polygon, DETAIL_BUILDING_STYLE, "Building boundary (from a sub pin)");
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
    function fetchBoundaries(attempt: number): void {
        fetch(boundaryApiUrl)
            .then((r) => r.json())
            .then((data) => {
                applyBoundaryPayload(data);
                if (data.pending && attempt < 30) {
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
                        m.off("contextmenu.rcdelete" as never);
                        m.on("contextmenu.rcdelete" as never, (e: L.LeafletMouseEvent) => {
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

    function saveBoundary(options: { type?: BoundaryType; exitEdit?: boolean; quiet?: boolean } = {}): void {
        const type = options.type || editingBoundaryType;
        if (!type) return;
        const layers = boundaryGroups[type].getLayers() as Array<L.Layer & { toGeoJSON: () => any }>;
        const geometry = layers.length === 0 ? null : { type: "MultiPolygon", coordinates: layers.map((l) => l.toGeoJSON().geometry.coordinates) };
        fetch(boundaryApiUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify({ boundary_type: type, polygon: geometry }),
        })
            .then(async (r) => {
                if (!r.ok) {
                    let msg = `HTTP ${r.status}`;
                    try {
                        msg = (await r.json()).error || msg;
                    } catch {
                        /* keep default */
                    }
                    throw new Error(msg);
                }
                return r.json();
            })
            .then((data) => {
                const exiting = options.exitEdit !== false;
                if (exiting) exitBoundaryEdit();
                // The server responds with the full refreshed payload (the clear
                // path falls back down the resolution chain server-side); only
                // redraw from it outside active editing so in-progress vertex
                // edits aren't clobbered.
                if (exiting || !boundaryDrawControl) applyBoundaryPayload(data);
                if (data.pending) fetchBoundaries(0);
                if (!options.quiet) toast.success(geometry ? "Boundary saved." : "Boundary reset to the default.");
            })
            .catch((err) => toast.error(`Failed to save boundary: ${err.message}`));
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
        return {
            name: (document.getElementById("dp-name") as HTMLInputElement).value.trim(),
            description: (document.getElementById("dp-description") as HTMLInputElement).value.trim(),
            pin_type: (document.getElementById("dp-type") as HTMLInputElement).value,
            icon: (document.getElementById("dp-icon") as HTMLInputElement).value || null,
            color: (document.getElementById("dp-color") as HTMLInputElement).value || null,
            bg_color: (document.getElementById("dp-bg-color") as HTMLInputElement).value || null,
            bg_opacity: Number.parseInt((document.getElementById("dp-bg-opacity") as HTMLInputElement).value, 10),
            border_color: (document.getElementById("dp-border-color") as HTMLInputElement).value || null,
            border_opacity: Number.parseInt((document.getElementById("dp-border-opacity") as HTMLInputElement).value, 10),
            latitude: (document.getElementById("dp-lat") as HTMLInputElement).value,
            longitude: (document.getElementById("dp-lon") as HTMLInputElement).value,
        };
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
            .then(() => undefined)
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

    function openAddPinDialog(): void {
        // Only one map side-panel open at a time - closing markup autosaves first.
        toolbar.closeMarkupPanel();

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
    }

    function openDetailPinEditDialog(dp: DetailPinEntry): void {
        // Only one map side-panel open at a time - closing markup autosaves first.
        toolbar.closeMarkupPanel();

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
    document.getElementById("dp-type")?.addEventListener("change", updateDpMarkerIcon);
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
                toast.success("Detail pin deleted.");
            })
            .catch(() => toast.error("Failed to delete detail pin."));
    });

    // -- Boundaries: click a polygon for an Edit/Delete context menu ------------
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
        openBoundaryCtxMenu(e.target as L.Layer, e.latlng);
    }
    function attachBoundaryClickHandlers(): void {
        (["property", "building"] as BoundaryType[]).forEach((type) => {
            boundaryGroups[type].eachLayer((layer) => {
                layer.off("click", onBoundaryLayerClick);
                layer.on("click", onBoundaryLayerClick);
            });
        });
    }

    // Outside-click handler for the currently-open boundary context menu, so it
    // behaves like a normal context menu: any interaction elsewhere on the page
    // (another shape, a toolbar button, a plain map click) dismisses it. Tracked
    // so a re-opened menu doesn't pile up duplicate listeners.
    let boundaryCtxOutsideHandler: ((e: MouseEvent) => void) | null = null;

    function openBoundaryCtxMenu(layer: L.Layer, latlng: L.LatLng): void {
        if (boundaryCtxOutsideHandler) {
            document.removeEventListener("click", boundaryCtxOutsideHandler, true);
            boundaryCtxOutsideHandler = null;
        }

        const content = document.createElement("div");
        content.className = "boundary-ctx-menu";

        const layerType = boundaryTypeOfLayer(layer);

        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "boundary-ctx-menu__item";
        editBtn.innerHTML = '<i class="material-symbols-outlined">edit</i> Edit';
        editBtn.addEventListener("click", () => {
            map.closePopup();
            if (layerType) startEditBoundary(layerType);
        });

        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "boundary-ctx-menu__item boundary-ctx-menu__item--danger";
        delBtn.innerHTML = '<i class="material-symbols-outlined">delete_outline</i> Delete';
        delBtn.addEventListener("click", async () => {
            map.closePopup();
            if (!layerType) return;
            if (!(await confirmAction({ title: "Delete Boundary", message: "Delete this boundary polygon?", confirmLabel: "Delete" }))) return;
            boundaryGroups[layerType].removeLayer(layer);
            if (layerType === "property" && boundaryGroups.property.getLayers().length === 0) setMainMarkerVisible(true);
            saveBoundary({ exitEdit: false, type: layerType });
        });

        content.append(editBtn, delBtn);
        L.popup({ closeButton: false, className: "boundary-ctx-menu-popup", offset: [0, -2] }).setLatLng(latlng).setContent(content).openOn(map);

        // The click that opened this menu already had propagation stopped (see
        // onBoundaryLayerClick), so it's safe to attach this immediately - it
        // only fires on the *next* click anywhere in the document.
        boundaryCtxOutsideHandler = (e: MouseEvent) => {
            document.removeEventListener("click", boundaryCtxOutsideHandler!, true);
            boundaryCtxOutsideHandler = null;
            if (content.contains(e.target as Node)) return; // the button's own handler drives this close
            map.closePopup();
        };
        document.addEventListener("click", boundaryCtxOutsideHandler, true);
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}

interface GalleryImage {
    id: number;
    url: string;
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

        // "Take a screenshot" toolbar button (_map_annotations_panels.html) -
        // opens the shared standalone map composer pre-scoped to this pin/wiki.
        _openMapScreenshot: () => void;

        // Detail-pin/boundary functions, exposed for this page's own template onclick= attributes.
        _toggleDetailPinListPanel: () => void;
        toggleDetailPinSelectMode: () => void;
        openAddPinDialog: () => void;
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
        galleryOpenLightbox?: (imgId: number, opts: { url: string }) => void;
        _galleryAddMarker: (img: GalleryImage) => void;
        _galleryRemoveMarker: (imgId: number) => void;
        _galleryHighlightMarker: (imgId: number, on: boolean) => void;
    }
}
