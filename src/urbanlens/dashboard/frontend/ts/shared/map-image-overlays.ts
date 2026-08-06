/**
 * Georeferenced image overlays on a pin's or wiki's map.
 *
 * A user drops a historical map image - a Sanborn fire-insurance sheet, a site
 * plan, an old survey - onto the live map and drags its four corners until the
 * old streets sit on the real ones. Leaflet's own `L.ImageOverlay` only takes
 * axis-aligned bounds, which can't express a scan that is rotated, sheared, or
 * (as most flatbed scans of century-old paper are) slightly trapezoidal.
 *
 * So the image is drawn in a plain `<img>` positioned by a CSS `matrix3d`
 * computed from the four corner points: a full projective transform, i.e. the
 * homography mapping the image's own unit rectangle onto the four corners'
 * current pixel positions. Recomputed on every map move/zoom, since the pixel
 * positions change but the stored WGS-84 corners do not. This is the same
 * technique leaflet-distortableimage uses; it is ~70 lines of linear algebra
 * here rather than a dependency, and keeps the corner semantics ours.
 */

import type * as L from "leaflet";

/** One overlay as served by `MapImageOverlay.to_json`. */
export interface MapOverlayEntry {
    uuid: string;
    name: string;
    url: string;
    /** `[[lat, lng], ...]` for NW, NE, SE, SW - clockwise from the image's top-left. */
    corners: [number, number][];
    opacity: number;
    order: number;
    default_visible: boolean;
    locked: boolean;
    layer_uuid: string | null;
}

export interface MapOverlayOptions {
    /** Builds the POST url for one overlay's corner updates. */
    cornersUrl: (uuid: string) => string;
    /** CSRF token for those POSTs. */
    csrfToken: string;
    /** Called when a drag finishes and the server has been told. */
    onSaved?: (uuid: string, corners: [number, number][]) => void;
    /** Called when saving corners fails, so the page can toast it. */
    onError?: (message: string) => void;
}

/** Solve an 8x8 linear system by Gaussian elimination with partial pivoting.
 *
 * Returns null for a singular system, which happens when the user drags
 * corners into a degenerate shape (three of them collinear, or two coincident)
 * - the caller then leaves the previous transform in place rather than
 * applying a matrix full of NaN, which would make the overlay vanish with no
 * way to drag it back.
 */
function solve8(matrix: number[][], rhs: number[]): number[] | null {
    const size = 8;
    // Flat row-major storage of the augmented matrix: 8 unknowns plus the
    // constant column. A flat array keeps every access a plain number under
    // the project's strict index checks, rather than an array-of-maybe-arrays.
    const width = size + 1;
    const cells = new Float64Array(size * width);
    for (let row = 0; row < size; row++) {
        const source = matrix[row] ?? [];
        for (let col = 0; col < size; col++) cells[row * width + col] = source[col] ?? 0;
        cells[row * width + size] = rhs[row] ?? 0;
    }

    for (let col = 0; col < size; col++) {
        let pivot = col;
        for (let row = col + 1; row < size; row++) {
            if (Math.abs(cells[row * width + col]!) > Math.abs(cells[pivot * width + col]!)) pivot = row;
        }
        if (Math.abs(cells[pivot * width + col]!) < 1e-12) return null;
        if (pivot !== col) {
            for (let k = col; k < width; k++) {
                const swap = cells[col * width + k]!;
                cells[col * width + k] = cells[pivot * width + k]!;
                cells[pivot * width + k] = swap;
            }
        }
        const diagonal = cells[col * width + col]!;
        for (let row = 0; row < size; row++) {
            if (row === col) continue;
            const factor = cells[row * width + col]! / diagonal;
            if (!factor) continue;
            for (let k = col; k < width; k++) cells[row * width + k] = cells[row * width + k]! - factor * cells[col * width + k]!;
        }
    }

    // Full Gauss-Jordan above leaves a diagonal system, so each unknown is
    // just its row's constant over its own diagonal coefficient.
    const solution: number[] = [];
    for (let row = 0; row < size; row++) solution.push(cells[row * width + size]! / cells[row * width + row]!);
    return solution;
}

/**
 * The CSS `matrix3d(...)` mapping the unit square (0,0)-(1,1) onto four points.
 *
 * `points` are the destination pixel positions of the image's NW, NE, SE, SW
 * corners, relative to the element's own origin. Standard 8-unknown homography
 * solve: with the source corners fixed at the unit square, each destination
 * corner contributes two rows.
 */
export function matrix3dForCorners(points: { x: number; y: number }[], width: number, height: number): string | null {
    const src = [
        { x: 0, y: 0 },
        { x: width, y: 0 },
        { x: width, y: height },
        { x: 0, y: height },
    ];
    if (points.length !== 4) return null;
    const a: number[][] = [];
    const b: number[] = [];
    for (let i = 0; i < 4; i++) {
        const s = src[i]!;
        const d = points[i];
        if (!d) return null;
        a.push([s.x, s.y, 1, 0, 0, 0, -s.x * d.x, -s.y * d.x]);
        b.push(d.x);
        a.push([0, 0, 0, s.x, s.y, 1, -s.x * d.y, -s.y * d.y]);
        b.push(d.y);
    }
    const h = solve8(a, b);
    if (!h || h.some((value) => !Number.isFinite(value))) return null;
    // CSS matrix3d is column-major; the homography's third row/column are the
    // identity's since this is a 2D projective map lifted into 3D.
    const m = [h[0]!, h[3]!, 0, h[6]!, h[1]!, h[4]!, 0, h[7]!, 0, 0, 1, 0, h[2]!, h[5]!, 0, 1];
    return `matrix3d(${m.map((v) => (Number.isFinite(v) ? v : 0)).join(",")})`;
}

interface LiveOverlay {
    entry: MapOverlayEntry;
    img: HTMLImageElement;
    handles: HTMLElement[];
    aligning: boolean;
}

/**
 * Attach the image-overlay renderer to a map.
 *
 * @param leaflet The Leaflet namespace (passed in rather than imported so this
 *   shares the single instance the host entry already created).
 * @param map The Leaflet map to draw on.
 * @param options Endpoints and callbacks - see {@link MapOverlayOptions}.
 */
export function createMapImageOverlays(leaflet: typeof L, map: L.Map, options: MapOverlayOptions) {
    const pane = map.getPane("overlayPane");
    const live = new Map<string, LiveOverlay>();
    const container = document.createElement("div");
    // leaflet-zoom-hide makes Leaflet hide the whole container for the duration
    // of a zoom animation. Without it the overlay is positioned in layer-point
    // space while the pane is simultaneously being CSS-transformed by the
    // animation, so it visibly drifts away from the map and snaps back at the
    // end. Hiding through the animation and redrawing on zoomend is what
    // Leaflet's own vector renderer does short of implementing _animateZoom.
    container.className = "ul-map-overlay-container leaflet-zoom-hide";
    pane?.appendChild(container);

    function pixelFor(corner: [number, number]) {
        const point = map.latLngToLayerPoint(leaflet.latLng(corner[0], corner[1]));
        return { x: point.x, y: point.y };
    }

    function redraw(item: LiveOverlay): void {
        const { img, entry } = item;
        if (!img.naturalWidth || !img.naturalHeight) return;
        const points = entry.corners.map(pixelFor);
        // Everything is positioned in layer-point space, the same coordinates
        // Leaflet's own overlay pane uses - so the element's origin is the map
        // origin and there's no per-element offset bookkeeping. (This holds
        // only between zoom animations; see leaflet-zoom-hide above.)
        const matrix = matrix3dForCorners(points, img.naturalWidth, img.naturalHeight);
        if (!matrix) return;
        img.style.transform = matrix;
        img.style.opacity = String(entry.opacity / 100);
        item.handles.forEach((handle, index) => {
            const point = points[index];
            if (!point) return;
            handle.style.transform = `translate(${point.x}px, ${point.y}px)`;
            handle.hidden = entry.locked || !item.aligning;
        });
    }

    function redrawAll(): void {
        live.forEach(redraw);
    }

    async function saveCorners(item: LiveOverlay): Promise<void> {
        const body = new FormData();
        body.append("corners", JSON.stringify(item.entry.corners));
        body.append("csrfmiddlewaretoken", options.csrfToken);
        try {
            const response = await fetch(options.cornersUrl(item.entry.uuid), { method: "POST", body, headers: { "X-CSRFToken": options.csrfToken } });
            if (!response.ok) {
                options.onError?.("Could not save the overlay's position.");
                return;
            }
            options.onSaved?.(item.entry.uuid, item.entry.corners);
        } catch {
            options.onError?.("Could not save the overlay's position.");
        }
    }

    function makeHandle(item: LiveOverlay, index: number): HTMLElement {
        const handle = document.createElement("div");
        handle.className = "ul-map-overlay-handle";
        handle.title = "Drag to align this corner";
        handle.hidden = true;
        handle.addEventListener("pointerdown", (event) => {
            event.preventDefault();
            event.stopPropagation();
            handle.setPointerCapture(event.pointerId);
            // The map's own drag handler would otherwise pan the whole map
            // while the user is trying to move one corner.
            map.dragging.disable();
            const move = (moveEvent: PointerEvent) => {
                const rect = map.getContainer().getBoundingClientRect();
                const containerPoint = leaflet.point(moveEvent.clientX - rect.left, moveEvent.clientY - rect.top);
                const latLng = map.containerPointToLatLng(containerPoint);
                item.entry.corners[index] = [latLng.lat, latLng.lng];
                redraw(item);
            };
            const up = () => {
                handle.removeEventListener("pointermove", move);
                handle.removeEventListener("pointerup", up);
                map.dragging.enable();
                void saveCorners(item);
            };
            handle.addEventListener("pointermove", move);
            handle.addEventListener("pointerup", up);
        });
        return handle;
    }

    function add(entry: MapOverlayEntry): void {
        const img = document.createElement("img");
        img.className = "ul-map-overlay-image";
        img.alt = entry.name || "Map overlay";
        img.draggable = false;
        img.src = entry.url;

        const item: LiveOverlay = { entry, img, handles: [], aligning: false };
        item.handles = entry.corners.map((_corner, index) => makeHandle(item, index));

        container.appendChild(img);
        item.handles.forEach((handle) => container.appendChild(handle));
        img.addEventListener("load", () => redraw(item));
        live.set(entry.uuid, item);
        setVisible(entry.uuid, entry.default_visible);
        redraw(item);
    }

    function remove(uuid: string): void {
        const item = live.get(uuid);
        if (!item) return;
        item.img.remove();
        item.handles.forEach((handle) => handle.remove());
        live.delete(uuid);
    }

    function setVisible(uuid: string, visible: boolean): void {
        const item = live.get(uuid);
        if (!item) return;
        item.img.hidden = !visible;
        item.handles.forEach((handle) => {
            handle.hidden = !visible || item.entry.locked || !item.aligning;
        });
    }

    // `zoom` deliberately not included: it fires continuously *during* the
    // animation, when layer points are momentarily inconsistent with the pane's
    // own transform (see leaflet-zoom-hide above).
    map.on("move moveend viewreset zoomend", redrawAll);

    return {
        /** Replace the whole overlay set (after an HTMX manage-dialog action). */
        sync(entries: MapOverlayEntry[]): void {
            const fresh = new Set(entries.map((entry) => entry.uuid));
            [...live.keys()].filter((uuid) => !fresh.has(uuid)).forEach(remove);
            entries.forEach((entry) => {
                const existing = live.get(entry.uuid);
                if (!existing) {
                    add(entry);
                    return;
                }
                const wasVisible = !existing.img.hidden;
                const urlChanged = existing.entry.url !== entry.url;
                existing.entry = entry;
                if (urlChanged) existing.img.src = entry.url;
                setVisible(entry.uuid, wasVisible);
                redraw(existing);
            });
        },
        /** Show the drag handles for one overlay, hiding every other set. */
        startAlign(uuid: string): void {
            live.forEach((item, key) => {
                item.aligning = key === uuid;
                redraw(item);
            });
        },
        /** Hide every overlay's drag handles. */
        stopAlign(): void {
            live.forEach((item) => {
                item.aligning = false;
                redraw(item);
            });
        },
        /** Live-preview an opacity change from the manage dialog's slider. */
        previewOpacity(uuid: string, opacity: number): void {
            const item = live.get(uuid);
            if (!item) return;
            item.entry.opacity = opacity;
            item.img.style.opacity = String(opacity / 100);
        },
        setVisible,
        /** Whether an overlay is currently drawn. */
        isVisible(uuid: string): boolean {
            const item = live.get(uuid);
            return !!item && !item.img.hidden;
        },
        /** The uuids currently rendered, in draw order. */
        uuids(): string[] {
            return [...live.keys()];
        },
        /** The uuids of overlays belonging to one custom layer. */
        uuidsInLayer(layerUuid: string): string[] {
            return [...live.values()].filter((item) => item.entry.layer_uuid === layerUuid).map((item) => item.entry.uuid);
        },
    };
}
