import { getCsrfToken } from "./csrf";
import { toast, confirmAction } from "./dialogs";

// See markup-engine.ts for why `L` is declared locally instead of imported.
declare const L: typeof import("leaflet");

/**
 * Shared map markup drawing/editing toolbar - lines, arrows, shapes, and text
 * labels drawn on a Leaflet map via window.MarkupEngine. Used identically by
 * the pin detail page, the Location wiki page, and the safety check-in map; a
 * fix here lands on all three.
 *
 * Ported from the old `_markup_toolbar_script.html` text fragment (which
 * relied on being spliced into the including page's own script scope to read
 * bare `map`/`_markupLayer` identifiers) into an explicit factory - the host
 * page now passes `map`/`markupLayer` and a config object instead. The host
 * must still expose the functions referenced by `_markup_toolbar_panel.html`
 * / `_markup_panel_dialog.html`'s inline `onclick=` attributes
 * (startMarkupDraw, startShapeDraw, startTextPlacement, toggleAddDetailMenu,
 * closeOrFinishDraw, deleteMarkupEdit) as `window` globals - see
 * ts/entries/map-annotations.ts and _safety_map_script.html for the two ways
 * that's done.
 */

export interface MarkupItem {
    uuid: string;
    markup_type: "text" | "line" | "arrow" | "square" | "polygon" | "circle";
    geometry: {
        type: string;
        coordinates: any;
        radius?: number;
        box_corner?: [number, number];
    };
    label?: string;
    color: string;
    border_color?: string | null;
    stroke_width?: number;
    fill_opacity?: number;
    border_opacity?: number;
    security_indicator?: string;
    _layers?: L.Layer[];
    _textMarker?: L.Marker;
    _arrowheadMarker?: L.Marker;
    _arrowheadDeg?: number;
}

export interface MarkupToolbarConfig {
    /** Static mode: markup already belongs to an existing pin/location/MarkupMap. */
    markupJsonUrl?: string;
    markupCreateUrl?: string;
    /** Edit endpoint template with a `00000000-0000-0000-0000-000000000000/` placeholder segment to strip. */
    markupEditUrlTemplate?: string;

    /** Lazy mode (safety check-in creation page): no MarkupMap exists yet. */
    markupMapCreateUrl?: string;
    markupMapUuid?: string | null;
    /** Templates with an `11111111-1111-1111-1111-111111111111` placeholder to replace with the new map's uuid. */
    markupMapJsonUrlTemplate?: string;
    markupMapMarkupUrlTemplate?: string;
    markupMapMarkupEditUrlTemplate?: string;
    /** id of the hidden input the new MarkupMap uuid should be written into. Defaults to 'markup-map-uuid-field'. */
    markupMapFieldId?: string;
    /** Read when lazily creating a MarkupMap on first draw (viewport at that moment). */
    getInitialView?: () => Record<string, unknown>;

    /** Profile defaults (0-100), matching the panel's own slider defaults. */
    markupFillOpacity?: number;
    markupBorderOpacity?: number;

    /** Whether the user has already dismissed (or opted out of) the one-time line-finish tip. */
    lineFinishTipDismissed?: () => boolean;

    /** Optional integration hooks with a host page's own detail-pin/layers panel. No-ops if omitted. */
    onBuildDetailList?: () => void;
    onClearDetailPinHighlight?: () => void;
    onCloseDetailPinPanel?: () => void;
}

export interface MarkupToolbar {
    loadMarkup: () => void;
    startMarkupDraw: (type: string) => void;
    startShapeDraw: (type: string) => void;
    startTextPlacement: () => void;
    toggleAddDetailMenu: () => void;
    closeMarkupPanel: () => void;
    /** The panel's single "Close" action: finishes a valid in-progress shape, or just closes. */
    closeOrFinishDraw: () => void;
    deleteMarkupEdit: () => Promise<void>;
    openMarkupEditDialog: (item: MarkupItem) => void;
    getMarkupItems: () => MarkupItem[];
    /** True while a draw tool is armed - hosts with their own map-click handlers
     * (detail-pin placement, the boundary editor's polygon-click menu) should
     * skip their own logic while this is true, so a click used to draw doesn't
     * also trigger them. */
    isDrawBusy: () => boolean;
}

const MARKUP_MAP_UUID_PLACEHOLDER = "11111111-1111-1111-1111-111111111111";

export function createMarkupToolbar(map: L.Map, markupLayer: L.LayerGroup, config: MarkupToolbarConfig): MarkupToolbar {
    let markupJsonUrl = config.markupJsonUrl ?? "";
    let markupPostUrl = config.markupCreateUrl ?? "";
    let markupEditBase = (config.markupEditUrlTemplate ?? "").replace("00000000-0000-0000-0000-000000000000/", "");

    const markupMapCreateUrl = config.markupMapCreateUrl ?? "";
    let markupMapCreatePromise: Promise<void> | null = null;

    function applyMarkupMapUuid(uuid: string): void {
        markupJsonUrl = (config.markupMapJsonUrlTemplate ?? "").replaceAll(MARKUP_MAP_UUID_PLACEHOLDER, uuid);
        markupPostUrl = (config.markupMapMarkupUrlTemplate ?? "").replaceAll(MARKUP_MAP_UUID_PLACEHOLDER, uuid);
        markupEditBase = (config.markupMapMarkupEditUrlTemplate ?? "").replaceAll(MARKUP_MAP_UUID_PLACEHOLDER, uuid).replace("00000000-0000-0000-0000-000000000000/", "");
    }

    // Resolves once markup endpoints exist, creating the draft MarkupMap on
    // first use. Static-URL pages (pin detail, wiki, check-in detail) resolve
    // immediately; only lazy mode ever hits the network here.
    function ensureMarkupTarget(): Promise<void> {
        if (markupPostUrl) return Promise.resolve();
        if (!markupMapCreateUrl) return Promise.reject(new Error("No markup endpoints configured"));
        if (!markupMapCreatePromise) {
            const initialView = config.getInitialView ? config.getInitialView() : {};
            markupMapCreatePromise = fetch(markupMapCreateUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify(initialView),
            })
                .then((r) => {
                    if (!r.ok) throw new Error();
                    return r.json();
                })
                .then((data) => {
                    applyMarkupMapUuid(data.uuid);
                    const field = document.getElementById(config.markupMapFieldId ?? "markup-map-uuid-field") as HTMLInputElement | null;
                    if (field) field.value = data.uuid;
                    document.dispatchEvent(new CustomEvent("ul:markup-map-created", { detail: { uuid: data.uuid } }));
                })
                .catch((err) => {
                    // Allow a retry on the next draw rather than caching the failure.
                    markupMapCreatePromise = null;
                    throw err;
                });
        }
        return markupMapCreatePromise;
    }

    const markupDefaultFillOpacity = (config.markupFillOpacity ?? 87) / 100;
    const markupDefaultBorderOpacity = (config.markupBorderOpacity ?? 100) / 100;
    // Referenced only to keep the profile-default constants from being flagged
    // unused - the actual per-item opacity always falls back to these same
    // 87/100 defaults inline (see _shapeOptions/_renderMarkupItem below),
    // matching the original file's behavior of computing them but only ever
    // consulting the item's own stored value.
    void markupDefaultFillOpacity;
    void markupDefaultBorderOpacity;

    let markupItems: MarkupItem[] = [];
    let markupDrawType: string | null = null;
    let editingMarkupItem: MarkupItem | null = null;

    const markupPalette = ["#e53e3e", "#1d4ed8", "#16a34a", "#d97706", "#7c3aed", "#0f172a", "#f8fafc"];
    // A shape/arrow/line's own Border Color is restricted to black/white/none - it's
    // just an outline for legibility, not a design choice like a dedicated Text
    // item's fully user-controlled color+background.
    const borderOnlyPalette = ["#0f172a", "#f8fafc"];

    function arrowheadSize(): number {
        return window.MarkupEngine.arrowheadSize(map.getZoom());
    }

    function escapeMarkupLabel(s: string): string {
        const div = document.createElement("div");
        div.textContent = s || "";
        return div.innerHTML;
    }

    function textFontSize(item: MarkupItem): number {
        const base = item.stroke_width || 16;
        const z = map.getZoom();
        const scale = 2 ** ((z - 16) * 0.5);
        return Math.max(8, Math.min(72, Math.round(base * scale)));
    }

    function textBackground(item: MarkupItem): string {
        if (item.border_color === "none") return "transparent";
        if (item.border_color) return item.border_color;
        return "rgba(255,255,255,0.94)";
    }

    function textBoxPixelRect(item: MarkupItem): { w: number; h: number; anchorX: number; anchorY: number } | null {
        const bc = item.geometry.box_corner;
        if (!bc) return null;
        const c1 = item.geometry.coordinates;
        const p1 = map.latLngToLayerPoint([c1[1], c1[0]]);
        const p2 = map.latLngToLayerPoint([bc[1], bc[0]]);
        return {
            w: Math.max(24, Math.abs(p2.x - p1.x)),
            h: Math.max(18, Math.abs(p2.y - p1.y)),
            anchorX: p2.x < p1.x ? Math.abs(p2.x - p1.x) : 0,
            anchorY: p2.y < p1.y ? Math.abs(p2.y - p1.y) : 0,
        };
    }

    function textLabelHtml(item: MarkupItem, overrideLabel?: string): string {
        const label = overrideLabel !== undefined ? overrideLabel : item.label || "";
        const bg = textBackground(item);
        const rect = textBoxPixelRect(item);
        // The Font Size slider (item.stroke_width) always drives the rendered size -
        // for a drag-created box, the box just defines a fixed wrap/clip region
        // around that text instead of the box height dictating the font size.
        const sz = textFontSize(item);
        if (rect) {
            return `<span class="map-text-label map-text-label--box" style="color:${item.color};background:${bg};` + `width:${rect.w}px;height:${rect.h}px;font-size:${sz}px;">${escapeMarkupLabel(label) || "&nbsp;"}</span>`;
        }
        return `<span class="map-text-label" style="color:${item.color};font-size:${sz}px;background:${bg}">${escapeMarkupLabel(label) || "&nbsp;"}</span>`;
    }

    function textIcon(item: MarkupItem): L.DivIcon {
        const rect = textBoxPixelRect(item);
        return rect
            ? L.divIcon({ className: "", html: textLabelHtml(item), iconSize: [rect.w, rect.h], iconAnchor: [rect.anchorX, rect.anchorY] })
            : L.divIcon({ className: "", html: textLabelHtml(item), iconSize: undefined, iconAnchor: [0, 0] });
    }

    function shapeOptions(item: MarkupItem): L.PathOptions {
        const bc = item.border_color;
        const strokeColor = bc && bc !== "none" ? bc : "white";
        const hasBorder = !!(bc && bc !== "none");
        const fillOp = (item.fill_opacity != null ? item.fill_opacity : 87) / 100;
        const borderOp = (item.border_opacity != null ? item.border_opacity : 100) / 100;
        return {
            pane: "markupPane",
            color: strokeColor,
            fillColor: item.color,
            fillOpacity: fillOp,
            weight: hasBorder ? item.stroke_width || 2 : 0,
            opacity: borderOp,
        };
    }

    function renderMarkupItem(item: MarkupItem): void {
        const layers: L.Layer[] = [];
        const type = item.markup_type;

        if (type === "text") {
            const c = item.geometry.coordinates;
            const textMarker = L.marker([c[1], c[0]], { icon: textIcon(item) });
            layers.push(textMarker);
            item._textMarker = textMarker;
        } else if (type === "line" || type === "arrow") {
            const latlngs: L.LatLng[] = item.geometry.coordinates.map((c: [number, number]) => L.latLng(c[1], c[0]));
            const w = item.stroke_width || 3;
            const isArrow = type === "arrow";
            const fillOp = (item.fill_opacity != null ? item.fill_opacity : 87) / 100;
            const borderOp = (item.border_opacity != null ? item.border_opacity : 100) / 100;
            const outlineColor = item.border_color && item.border_color !== "none" ? item.border_color : "white";

            if (isArrow) {
                layers.push(L.polyline(latlngs, { pane: "markupPane", color: outlineColor, weight: w + 4, opacity: borderOp * 0.75, interactive: false }));
            } else if (item.border_color && item.border_color !== "none") {
                layers.push(L.polyline(latlngs, { pane: "markupPane", color: item.border_color, weight: w + 3, opacity: borderOp * 0.7, interactive: false }));
            }

            layers.push(L.polyline(latlngs, { pane: "markupPane", color: item.color, weight: isArrow ? w + 2 : w, opacity: fillOp }));

            if (isArrow && latlngs.length >= 2) {
                const deg = window.MarkupEngine.bearing(latlngs[latlngs.length - 2]!, latlngs[latlngs.length - 1]!);
                const sz = arrowheadSize();
                const arrowMarker = L.marker(latlngs[latlngs.length - 1]!, {
                    icon: L.divIcon({ className: "", html: window.MarkupEngine.arrowheadSvg(item.color, deg, sz, fillOp), iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2] }),
                    interactive: false,
                });
                layers.push(arrowMarker);
                item._arrowheadMarker = arrowMarker;
                item._arrowheadDeg = deg;
            }

            if (item.label) {
                const mid = latlngs[Math.floor(latlngs.length / 2)]!;
                layers.push(
                    L.marker(mid, {
                        // Shape/arrow/line names always render as black-on-white, regardless
                        // of the shape's own color - unlike a dedicated Text markup item
                        // (which the user fully controls), this is just a readable caption.
                        icon: L.divIcon({ className: "", iconSize: undefined, iconAnchor: [0, 0], html: `<span class="map-text-label map-text-label--line">${escapeMarkupLabel(item.label)}</span>` }),
                        interactive: false,
                    }),
                );
            }
        } else if (type === "square" || type === "polygon") {
            const rings: L.LatLng[][] = item.geometry.coordinates.map((ring: [number, number][]) => ring.map((c) => L.latLng(c[1], c[0])));
            const polygon = L.polygon(rings, shapeOptions(item));
            layers.push(polygon);
            if (item.label) {
                const center = polygon.getBounds().getCenter();
                layers.push(
                    L.marker(center, {
                        icon: L.divIcon({ className: "", iconSize: undefined, iconAnchor: [0, 0], html: `<span class="map-text-label">${escapeMarkupLabel(item.label)}</span>` }),
                        interactive: false,
                    }),
                );
            }
        } else if (type === "circle") {
            const [lng, lat] = item.geometry.coordinates as [number, number];
            const circle = L.circle([lat, lng], { radius: item.geometry.radius, ...shapeOptions(item) });
            layers.push(circle);
        }

        layers.forEach((l) => l.addTo(markupLayer));
        item._layers = layers;

        // Clicking any interactive layer opens the edit dialog; also bind a
        // tooltip showing the label (if any) on hover.
        layers.forEach((l) => {
            const interactive = l as L.Layer & { on?: L.Evented["on"]; bindTooltip?: L.Layer["bindTooltip"] };
            if (!interactive.on) return;
            interactive.on!("click", () => openMarkupEditDialog(item));
            if (item.label && interactive.bindTooltip) {
                interactive.bindTooltip!(item.label, { permanent: false, direction: "top", className: "detail-pin-tooltip" });
            }
        });
    }

    function loadMarkup(): void {
        if (!markupJsonUrl) return; // lazy mode - nothing to load until the map is created
        fetch(markupJsonUrl)
            .then((r) => r.json())
            .then((data) => {
                markupLayer.clearLayers();
                markupItems = [];
                (data.markup_items || []).forEach((item: MarkupItem) => {
                    renderMarkupItem(item);
                    markupItems.push(item);
                });
                config.onBuildDetailList?.();
            })
            .catch((err) => console.warn("Could not load markup:", err));
    }

    // -- One-time tip: how to finish a multi-point line/arrow -------------------
    // Shown once ever (not per-page) the first time a user places a 2nd point on
    // a line/arrow - non-blocking (pointer-events: none on the wrapper) so it
    // never steals the click the user is mid-gesture on.
    const lineFinishTipKey = "ul_onboarding_v1_markup_line_finish_tip_dismissed";
    function lineFinishTipDismissed(): boolean {
        if (config.lineFinishTipDismissed?.()) return true;
        try {
            return localStorage.getItem(lineFinishTipKey) === "1";
        } catch {
            return true;
        }
    }
    function dismissLineFinishTip(): void {
        try {
            localStorage.setItem(lineFinishTipKey, "1");
        } catch {
            /* storage unavailable - ignore */
        }
        document.getElementById("markup-line-finish-tip")?.remove();
    }
    function maybeShowLineFinishTip(): void {
        if (lineFinishTipDismissed() || document.getElementById("markup-line-finish-tip")) return;
        const wrapper = document.querySelector(".map-wrapper") || document.querySelector(".safety-map-wrapper");
        if (!wrapper) return;
        const el = document.createElement("div");
        el.id = "markup-line-finish-tip";
        el.className = "markup-line-finish-tip";
        el.innerHTML =
            '<i class="material-icons markup-line-finish-tip__icon">gesture</i>'
            + '<span class="markup-line-finish-tip__text">Click that <strong>last point</strong> again to finish the shape</span>'
            + '<button type="button" class="markup-line-finish-tip__close" aria-label="Got it, don\'t show this again">'
            + '<i class="material-symbols-outlined">close</i></button>';
        el.querySelector(".markup-line-finish-tip__close")?.addEventListener("click", dismissLineFinishTip);
        wrapper.appendChild(el);
        setTimeout(dismissLineFinishTip, 8000);
    }

    // -- Markup draw session ---------------------------------------------------
    const drawSession = window.MarkupEngine.createDrawSession(map, {
        getColor: () => (document.getElementById("markup-panel-color") as HTMLInputElement).value,
        getWidth: () => Number.parseInt((document.getElementById("markup-panel-width") as HTMLInputElement).value || "3", 10),
        getTextLabel: () => (document.getElementById("markup-panel-label") as HTMLInputElement).value.trim(),
        onCommit: onDrawCommit,
        onHintChange: (hint) => {
            const el = document.getElementById("markup-panel-hint-text");
            if (el) el.textContent = hint;
        },
        onPointCountChange: (tool, n) => {
            // The "click the last point again to finish" gesture only becomes
            // live once a 2nd point is down - that's the moment to teach it.
            if ((tool === "line" || tool === "arrow") && n === 2) maybeShowLineFinishTip();
        },
        onToolChange: (tool) => {
            markupDrawType = tool;
            if (!tool && !editingMarkupItem) {
                (document.getElementById("markup-panel") as HTMLElement).style.display = "none";
            }
        },
    });

    // -- "Add Detail" dropdown -------------------------------------------------
    let addDetailOpen = false;

    function toggleAddDetailMenu(): void {
        addDetailOpen = !addDetailOpen;
        (document.getElementById("add-detail-menu") as HTMLElement).style.display = addDetailOpen ? "" : "none";
        document.querySelector(".add-detail-chevron")?.classList.toggle("open", addDetailOpen);
    }
    function closeAddDetailMenu(): void {
        addDetailOpen = false;
        (document.getElementById("add-detail-menu") as HTMLElement).style.display = "none";
        document.querySelector(".add-detail-chevron")?.classList.remove("open");
    }
    document.addEventListener("click", (e) => {
        if (addDetailOpen && !document.getElementById("add-detail-wrap")?.contains(e.target as Node)) {
            closeAddDetailMenu();
        }
    });

    // -- Markup drawing - engine-backed -----------------------------------------
    // One panel (#markup-panel) is used for both drawing a new item and editing
    // an existing one - only its title, hint, and action row change between the
    // two modes, so the user only ever learns a single set of controls.
    const MARKUP_TOOL_TITLES: Record<string, string> = {
        line: "Draw Line",
        arrow: "Draw Arrow",
        text: "Add Text Label",
        rect: "Draw Square",
        circle: "Draw Circle",
        polygon: "Draw Polygon",
    };

    function configureMarkupPanelForTool(tool: string): void {
        // Only one map side-panel open at a time.
        config.onCloseDetailPinPanel?.();

        const isText = tool === "text";
        editingMarkupItem = null;

        document.getElementById("markup-panel-title")!.textContent = MARKUP_TOOL_TITLES[tool] ?? "Draw";
        document.getElementById("markup-panel-label-caption")!.textContent = isText ? "Text" : "Label";
        document.getElementById("markup-panel-color-label")!.textContent = isText ? "Text Color" : "Fill Color";
        document.getElementById("markup-panel-border-label")!.textContent = isText ? "Background" : "Border Color";
        document.getElementById("markup-panel-fill-opacity-label-text")!.textContent = isText ? "Text Opacity" : "Fill Opacity";
        document.getElementById("markup-panel-width-label-text")!.textContent = isText ? "Font Size" : "Width";
        (document.getElementById("markup-panel-security-row") as HTMLElement).hidden = isText;
        rebuildEditSwatch("markup-panel-border-swatches", "markup-panel-border", true, isText ? markupPalette : borderOnlyPalette);

        const widthEl = document.getElementById("markup-panel-width") as HTMLInputElement;
        widthEl.min = isText ? "10" : "1";
        widthEl.max = isText ? "48" : "8";
        widthEl.value = isText ? "16" : "3";
        document.getElementById("markup-panel-width-label")!.textContent = widthEl.value;
        (document.getElementById("markup-panel-label") as HTMLInputElement).value = "";

        (document.getElementById("markup-panel-hint") as HTMLElement).hidden = false;
        (document.getElementById("markup-panel-draw-actions") as HTMLElement).hidden = false;
        (document.getElementById("markup-panel-edit-actions") as HTMLElement).hidden = true;
        (document.getElementById("markup-panel") as HTMLElement).style.display = "";
    }

    function createMarkupItem(markupType: string, geometry: MarkupItem["geometry"]): void {
        const label = (document.getElementById("markup-panel-label") as HTMLInputElement).value.trim();
        const color = (document.getElementById("markup-panel-color") as HTMLInputElement).value;
        const border_color = (document.getElementById("markup-panel-border") as HTMLInputElement).value;
        const stroke_width = Number.parseInt((document.getElementById("markup-panel-width") as HTMLInputElement).value, 10);
        const fill_opacity = Number.parseInt((document.getElementById("markup-panel-fill-opacity") as HTMLInputElement).value, 10);
        const border_opacity = Number.parseInt((document.getElementById("markup-panel-border-opacity") as HTMLInputElement).value, 10);
        const security_indicator = markupType === "text" ? "" : (document.getElementById("markup-panel-security") as HTMLInputElement).value;
        ensureMarkupTarget()
            .then(() =>
                fetch(markupPostUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                    body: JSON.stringify({ markup_type: markupType, geometry, label, color, stroke_width, border_color, fill_opacity, border_opacity, security_indicator }),
                }),
            )
            .then((r) => {
                if (!r.ok) throw new Error();
                return r.json();
            })
            .then((data) => reloadMarkupAndOpenEdit(data.uuid))
            .catch(() => toast.error("Failed to save markup."));
    }

    function onDrawCommit(type: string, latlngs: [number, number][], extras: Record<string, unknown>): void {
        // Deactivate tool first - hides the panel via onToolChange, unless we're
        // about to immediately re-show it in edit mode once the save completes.
        drawSession.deactivate();

        if (type === "line" || type === "arrow") {
            const coords = latlngs.map((ll) => [ll[1], ll[0]]);
            createMarkupItem(type, { type: "LineString", coordinates: coords });
            return;
        }
        if (type === "circle") {
            const center = L.latLng(latlngs[0]![0], latlngs[0]![1]);
            const edge = L.latLng(latlngs[1]![0], latlngs[1]![1]);
            createMarkupItem("circle", { type: "Circle", coordinates: [latlngs[0]![1], latlngs[0]![0]], radius: center.distanceTo(edge) });
            return;
        }
        if (type === "rect") {
            const [n, w] = [latlngs[0]![0], latlngs[0]![1]];
            const [s, e] = [latlngs[1]![0], latlngs[1]![1]];
            createMarkupItem("square", {
                type: "Polygon",
                coordinates: [
                    [
                        [w, n],
                        [e, n],
                        [e, s],
                        [w, s],
                        [w, n],
                    ],
                ],
            });
            return;
        }
        if (type === "polygon") {
            const coords = latlngs.map((ll) => [ll[1], ll[0]]);
            coords.push(coords[0]!);
            createMarkupItem("polygon", { type: "Polygon", coordinates: [coords] });
            return;
        }
        if (type === "text") {
            const ll = L.latLng(latlngs[0]![0], latlngs[0]![1]);
            const geometry: MarkupItem["geometry"] = { type: "Point", coordinates: [ll.lng, ll.lat] };
            // A drag-created text commits a second point (the opposite box corner) -
            // store it so the renderer can size/wrap the label to fit that box.
            if (latlngs.length > 1) {
                const corner = L.latLng(latlngs[1]![0], latlngs[1]![1]);
                geometry.box_corner = [corner.lng, corner.lat];
            }
            createMarkupItem("text", geometry);
        }
    }

    function startMarkupDraw(type: string): void {
        closeAddDetailMenu();
        configureMarkupPanelForTool(type);
        drawSession.startTool(type);
    }

    function reloadMarkupAndOpenEdit(newUuid: string): Promise<void> {
        return fetch(markupJsonUrl)
            .then((r) => r.json())
            .then((markupData) => {
                markupLayer.clearLayers();
                markupItems = [];
                (markupData.markup_items || []).forEach((item: MarkupItem) => {
                    renderMarkupItem(item);
                    markupItems.push(item);
                });
                config.onBuildDetailList?.();
                const newItem = markupItems.find((i) => i.uuid === newUuid);
                if (newItem) openMarkupEditDialog(newItem);
            });
    }

    // -- Shape drawing (square, circle, polygon) --------------------------------
    function startShapeDraw(type: string): void {
        closeAddDetailMenu();
        // Engine uses 'rect' for both 'square' and 'rect'.
        const tool = type === "square" ? "rect" : type;
        configureMarkupPanelForTool(tool);
        drawSession.startTool(tool);
    }

    function closeMarkupPanel(): void {
        flushMarkupAutoSave(); // don't lose an edit made just before closing
        if (drawSession?.getCurrentTool()) {
            drawSession.deactivate(); // triggers onToolChange(null), which hides the panel
        } else {
            (document.getElementById("markup-panel") as HTMLElement).style.display = "none";
        }
        editingMarkupItem = null;
    }

    // Single "Close" action for draw mode: finishes a valid in-progress shape
    // (the old Finish button), or just abandons the tool if there's nothing
    // to finish yet (the old Cancel button) - autosave means there's never a
    // saved-but-unconfirmed state to explicitly "cancel" once a shape exists.
    function closeOrFinishDraw(): void {
        if (drawSession?.canFinish()) {
            drawSession.finishCurrent();
        } else {
            closeMarkupPanel();
        }
    }

    function rebuildEditSwatch(containerId: string, inputId: string, withNone: boolean, palette?: string[]): void {
        const cont = document.getElementById(containerId) as HTMLElement;
        const input = document.getElementById(inputId) as HTMLInputElement;
        cont.innerHTML = "";
        if (withNone) {
            const nb = document.createElement("button");
            nb.type = "button";
            nb.title = "None";
            nb.className = `markup-color-swatch markup-color-swatch--none${!input.value || input.value === "none" ? " markup-color-swatch--active" : ""}`;
            nb.style.cssText = "background:transparent;border:1px solid #cbd5e1;position:relative;";
            nb.innerHTML = '<span style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.65rem;color:#9ca3af">∅</span>';
            nb.addEventListener("click", () => {
                cont.querySelectorAll(".markup-color-swatch").forEach((b) => b.classList.remove("markup-color-swatch--active"));
                nb.classList.add("markup-color-swatch--active");
                input.value = "none";
                liveApplyMarkupEdit();
            });
            cont.appendChild(nb);
        }
        (palette ?? markupPalette).forEach((color) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = `markup-color-swatch${color === input.value ? " markup-color-swatch--active" : ""}`;
            btn.style.cssText = `background:${color};${color === "#f8fafc" ? "border:1px solid #cbd5e1;" : ""}`;
            btn.addEventListener("click", () => {
                cont.querySelectorAll(".markup-color-swatch").forEach((b) => b.classList.remove("markup-color-swatch--active"));
                btn.classList.add("markup-color-swatch--active");
                input.value = color;
                liveApplyMarkupEdit();
            });
            cont.appendChild(btn);
        });
    }

    // Applies every edit-panel field to the map instantly, then schedules a
    // debounced save - so annotations update in realtime as the user drags a
    // slider or picks a color, without needing an explicit Save button, and
    // without hammering the server on every single input event.
    function liveApplyMarkupEdit(): void {
        if (!editingMarkupItem) return;
        const item = editingMarkupItem;
        const isText = item.markup_type === "text";

        item.label = (document.getElementById("markup-panel-label") as HTMLInputElement).value.trim();
        item.color = (document.getElementById("markup-panel-color") as HTMLInputElement).value;
        item.border_color = (document.getElementById("markup-panel-border") as HTMLInputElement).value;
        item.stroke_width = Number.parseInt((document.getElementById("markup-panel-width") as HTMLInputElement).value, 10);
        item.fill_opacity = Number.parseInt((document.getElementById("markup-panel-fill-opacity") as HTMLInputElement).value, 10);
        item.border_opacity = Number.parseInt((document.getElementById("markup-panel-border-opacity") as HTMLInputElement).value, 10);
        item.security_indicator = isText ? "" : (document.getElementById("markup-panel-security") as HTMLInputElement).value;

        // Re-render in place: the item object's identity is preserved (same
        // reference in markupItems / editingMarkupItem), only its layers change,
        // so this reuses renderMarkupItem as the single source of truth for how
        // every shape type looks rather than hand-rolling per-type restyle logic.
        item._layers?.forEach((l) => markupLayer.removeLayer(l));
        renderMarkupItem(item);

        scheduleMarkupAutoSave(item);
    }

    let markupAutoSaveTimer: ReturnType<typeof setTimeout> | undefined;
    let markupAutoSaveItem: MarkupItem | null = null;
    function scheduleMarkupAutoSave(item: MarkupItem): void {
        clearTimeout(markupAutoSaveTimer);
        markupAutoSaveItem = item;
        markupAutoSaveTimer = setTimeout(flushMarkupAutoSave, 500);
    }
    function flushMarkupAutoSave(): void {
        clearTimeout(markupAutoSaveTimer);
        const item = markupAutoSaveItem;
        markupAutoSaveItem = null;
        if (!item) return;
        fetch(`${markupEditBase}${item.uuid}/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify({
                label: item.label,
                color: item.color,
                border_color: item.border_color,
                stroke_width: item.stroke_width,
                fill_opacity: item.fill_opacity,
                border_opacity: item.border_opacity,
                security_indicator: item.security_indicator,
            }),
        }).catch(() => toast.error("Failed to save annotation changes."));
    }

    function openMarkupEditDialog(item: MarkupItem): void {
        // Only one map side-panel open at a time.
        config.onCloseDetailPinPanel?.();

        editingMarkupItem = item;
        const isText = item.markup_type === "text";

        document.getElementById("markup-panel-title")!.textContent = `Edit ${isText ? "Text Label" : "Annotation"}`;
        document.getElementById("markup-panel-label-caption")!.textContent = isText ? "Text" : "Label";
        (document.getElementById("markup-panel-label") as HTMLInputElement).value = item.label || "";
        const widthEl = document.getElementById("markup-panel-width") as HTMLInputElement;
        widthEl.min = isText ? "10" : "1";
        widthEl.max = isText ? "48" : "8";
        widthEl.value = String(item.stroke_width || (isText ? 16 : 3));
        document.getElementById("markup-panel-width-label")!.textContent = String(item.stroke_width || (isText ? 16 : 3));
        document.getElementById("markup-panel-width-label-text")!.textContent = isText ? "Font Size" : "Width";
        document.getElementById("markup-panel-color-label")!.textContent = isText ? "Text Color" : "Fill Color";
        document.getElementById("markup-panel-border-label")!.textContent = isText ? "Background" : "Border Color";
        document.getElementById("markup-panel-fill-opacity-label-text")!.textContent = isText ? "Text Opacity" : "Fill Opacity";
        const fillOpEl = document.getElementById("markup-panel-fill-opacity") as HTMLInputElement;
        const fillOpVal = item.fill_opacity != null ? item.fill_opacity : 87;
        fillOpEl.value = String(fillOpVal);
        document.getElementById("markup-panel-fill-opacity-val")!.textContent = String(fillOpVal);
        const borderOpEl = document.getElementById("markup-panel-border-opacity") as HTMLInputElement;
        const borderOpVal = item.border_opacity != null ? item.border_opacity : 100;
        borderOpEl.value = String(borderOpVal);
        document.getElementById("markup-panel-border-opacity-val")!.textContent = String(borderOpVal);

        (document.getElementById("markup-panel-color") as HTMLInputElement).value = item.color || "#e53e3e";
        (document.getElementById("markup-panel-border") as HTMLInputElement).value = item.border_color || "";
        rebuildEditSwatch("markup-panel-color-swatches", "markup-panel-color", false);
        rebuildEditSwatch("markup-panel-border-swatches", "markup-panel-border", true, isText ? markupPalette : borderOnlyPalette);
        (document.getElementById("markup-panel-security-row") as HTMLElement).hidden = isText;
        (document.getElementById("markup-panel-security") as HTMLInputElement).value = item.security_indicator || "";

        (document.getElementById("markup-panel-hint") as HTMLElement).hidden = true;
        (document.getElementById("markup-panel-draw-actions") as HTMLElement).hidden = true;
        (document.getElementById("markup-panel-edit-actions") as HTMLElement).hidden = false;
        (document.getElementById("markup-panel") as HTMLElement).style.display = "";
        if (isText) (document.getElementById("markup-panel-label") as HTMLInputElement).focus();
    }

    async function deleteMarkupEdit(): Promise<void> {
        if (!editingMarkupItem) return;
        if (!(await confirmAction({ title: "Delete Annotation", message: "Delete this annotation?", confirmLabel: "Delete" }))) return;
        // Drop any pending autosave for this item - it no longer exists to save.
        if (markupAutoSaveItem === editingMarkupItem) {
            clearTimeout(markupAutoSaveTimer);
            markupAutoSaveItem = null;
        }
        fetch(`${markupEditBase}${editingMarkupItem.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } })
            .then((r) => {
                if (!r.ok) throw new Error();
                closeMarkupPanel();
                loadMarkup();
                toast.success("Annotation deleted.");
            })
            .catch(() => toast.error("Failed to delete annotation."));
    }

    // Rescale arrowheads and text labels when the user zooms in/out.
    map.on("zoomend", () => {
        const sz = arrowheadSize();
        markupItems.forEach((item) => {
            if (item._arrowheadMarker) {
                const itemOp = (item.fill_opacity != null ? item.fill_opacity : 87) / 100;
                item._arrowheadMarker.setIcon(L.divIcon({ className: "", html: window.MarkupEngine.arrowheadSvg(item.color, item._arrowheadDeg!, sz, itemOp), iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2] }));
            }
            if (item._textMarker) {
                item._textMarker.setIcon(textIcon(item));
            }
        });
    });

    // -- Text placement ----------------------------------------------------------
    function startTextPlacement(): void {
        closeAddDetailMenu();
        configureMarkupPanelForTool("text");
        drawSession.startTool("text");
    }

    // Initial render + swatch setup, matching the original file's top-level calls.
    rebuildEditSwatch("markup-panel-color-swatches", "markup-panel-color", false);
    rebuildEditSwatch("markup-panel-border-swatches", "markup-panel-border", true, borderOnlyPalette);
    loadMarkup();

    void markupDrawType; // read only via closures above; kept for parity with the original state var

    return {
        loadMarkup,
        startMarkupDraw,
        startShapeDraw,
        startTextPlacement,
        toggleAddDetailMenu,
        closeMarkupPanel,
        closeOrFinishDraw,
        deleteMarkupEdit,
        openMarkupEditDialog,
        getMarkupItems: () => markupItems,
        isDrawBusy: () => drawSession.isBusy(),
    };
}
