/**
 * MapLibre-native counterpart to `markup-engine.ts`'s `renderShape` - draws the same stored
 * `ShapeSpec` shapes (comment/DM map attachments, pin/wiki annotations) but as MapLibre GL JS
 * sources/layers/markers instead of Leaflet vectors, for maps rendered by the MapLibre engine
 * (PL8 item 2's WebGL2 path). Reuses `markup-engine.ts`'s own pure HTML builders
 * (`arrowheadSvg`, `textLabelHtml`) rather than re-deriving them, so an arrow/text label looks
 * identical on either engine.
 *
 * `circle` has one deliberate, known visual approximation: Leaflet's `L.circle` recomputes a
 * screen-space ellipse every frame from the current projection (`Circle._project()` in Leaflet's
 * own source), which MapLibre's GeoJSON-layer model has no equivalent of. This module instead
 * builds a fixed 64-point polygon approximating the true geodesic circle (matching Leaflet's own
 * `distance()` - haversine, `R = 6371000`, the same value `L.CRS.Earth.R` uses), which MapLibre
 * then projects correctly like any other polygon. The two are visually indistinguishable at the
 * radii and zoom levels this app's shapes are actually drawn at, but they are not the same
 * algorithm, and the difference grows at extreme radii or high latitude.
 */
import { arrowheadSize, arrowheadSvg, bearing, safeColor, safeNumber, textLabelHtml, type LatLngTuple, type ShapeSpec } from "./markup-engine";

// maplibregl is loaded globally via a CDN <script> tag (maplibregl_js in vendor_assets.py), never bundled here - same pattern as `L`.
declare const maplibregl: typeof import("maplibre-gl");

type LngLat = [number, number];

/** Mirrors `L.CRS.Earth.R` - Leaflet's own mean-Earth-radius constant, so distances/bearings match what Leaflet already draws. */
const EARTH_RADIUS_M = 6371000;

function toRad(deg: number): number {
    return (deg * Math.PI) / 180;
}

function toDeg(rad: number): number {
    return (rad * 180) / Math.PI;
}

/** Great-circle distance in meters - the same haversine formula `L.LatLng.distanceTo` uses. */
function haversineDistance(a: LatLngTuple, b: LatLngTuple): number {
    const [lat1, lng1] = a;
    const [lat2, lng2] = b;
    const sinDLat = Math.sin(toRad(lat2 - lat1) / 2);
    const sinDLng = Math.sin(toRad(lng2 - lng1) / 2);
    const h = sinDLat * sinDLat + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * sinDLng * sinDLng;
    return 2 * EARTH_RADIUS_M * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
}

/** The point `distanceM` meters from `center` along `bearingDeg` (0 = north), via the standard spherical direct formula. */
function destinationPoint(center: LatLngTuple, distanceM: number, bearingDeg: number): LatLngTuple {
    const angularDist = distanceM / EARTH_RADIUS_M;
    const bearingRad = toRad(bearingDeg);
    const lat1 = toRad(center[0]);
    const lng1 = toRad(center[1]);
    const lat2 = Math.asin(Math.sin(lat1) * Math.cos(angularDist) + Math.cos(lat1) * Math.sin(angularDist) * Math.cos(bearingRad));
    const lng2 = lng1 + Math.atan2(Math.sin(bearingRad) * Math.sin(angularDist) * Math.cos(lat1), Math.cos(angularDist) - Math.sin(lat1) * Math.sin(lat2));
    return [toDeg(lat2), toDeg(lng2)];
}

function circleRing(center: LatLngTuple, radiusM: number, steps = 64): LngLat[] {
    const ring: LngLat[] = [];
    for (let i = 0; i <= steps; i++) {
        const [lat, lng] = destinationPoint(center, radiusM, (360 * i) / steps);
        ring.push([lng, lat]);
    }
    return ring;
}

function boundsRing(a: LatLngTuple, b: LatLngTuple): LngLat[] {
    const minLat = Math.min(a[0], b[0]);
    const maxLat = Math.max(a[0], b[0]);
    const minLng = Math.min(a[1], b[1]);
    const maxLng = Math.max(a[1], b[1]);
    return [
        [minLng, minLat],
        [maxLng, minLat],
        [maxLng, maxLat],
        [minLng, maxLat],
        [minLng, minLat],
    ];
}

function polygonRing(latlngs: LatLngTuple[]): LngLat[] {
    const ring: LngLat[] = latlngs.map(([lat, lng]) => [lng, lat]);
    const first = ring[0]!;
    const last = ring[ring.length - 1]!;
    if (first[0] !== last[0] || first[1] !== last[1]) ring.push(first);
    return ring;
}

/** Handle returned by `renderShapeGroup` - removes every source/layer/marker it added. */
export interface ShapeGroupHandle {
    remove(): void;
}

function markerAt(latlng: LatLngTuple, html: string, anchor: "center" | "top-left" | "bottom"): InstanceType<typeof maplibregl.Marker> {
    const el = document.createElement("div");
    el.innerHTML = html;
    return new maplibregl.Marker({ element: el.firstElementChild as HTMLElement, anchor }).setLngLat([latlng[1], latlng[0]]);
}

/**
 * Renders `shapes` onto `map` as MapLibre sources/layers/markers - the MapLibre-side equivalent
 * of calling `markup-engine.ts`'s `renderShape` once per shape into a Leaflet `L.layerGroup()`.
 * @param idPrefix - Namespaces this group's MapLibre source/layer ids so multiple groups (or
 *   repeated renders) on the same map never collide.
 */
export function renderShapeGroup(map: InstanceType<typeof maplibregl.Map>, shapes: ShapeSpec[], idPrefix: string, zoom?: number): ShapeGroupHandle {
    const sourceIds: string[] = [];
    const layerIds: string[] = [];
    const markers: InstanceType<typeof maplibregl.Marker>[] = [];

    function addLine(id: string, coords: LngLat[], color: string, weight: number, opacity: number): void {
        const sourceId = `${id}-src`;
        map.addSource(sourceId, { type: "geojson", data: { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: coords } } });
        sourceIds.push(sourceId);
        const layerId = `${id}-line`;
        map.addLayer({ id: layerId, type: "line", source: sourceId, paint: { "line-color": color, "line-width": weight, "line-opacity": opacity } });
        layerIds.push(layerId);
    }

    function addFilledPolygon(id: string, ring: LngLat[], fillColor: string, fillOpacity: number, strokeColor: string, strokeWeight: number, strokeOpacity: number): void {
        const sourceId = `${id}-src`;
        map.addSource(sourceId, { type: "geojson", data: { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [ring] } } });
        sourceIds.push(sourceId);
        const fillLayerId = `${id}-fill`;
        map.addLayer({ id: fillLayerId, type: "fill", source: sourceId, paint: { "fill-color": fillColor, "fill-opacity": fillOpacity } });
        layerIds.push(fillLayerId);
        const lineLayerId = `${id}-outline`;
        map.addLayer({ id: lineLayerId, type: "line", source: sourceId, paint: { "line-color": strokeColor, "line-width": strokeWeight, "line-opacity": strokeOpacity } });
        layerIds.push(lineLayerId);
    }

    shapes.forEach((s, i) => {
        const id = `${idPrefix}-${i}`;
        const color = safeColor(s.color, "#e74c3c");
        const weight = safeNumber(s.stroke_width != null ? s.stroke_width : s.weight, 1, 50, 3);
        const fillOp = safeNumber(s.fill_opacity != null ? s.fill_opacity : 87, 0, 100, 87) / 100;
        const borderOp = safeNumber(s.border_opacity != null ? s.border_opacity : 100, 0, 100, 100) / 100;
        const bc = s.border_color && s.border_color !== "none" ? safeColor(s.border_color, color) : null;
        const hasBorder = !!bc;
        const strokeC = hasBorder ? bc! : color;

        switch (s.type) {
            case "line":
                addLine(id, s.latlngs.map(([lat, lng]): LngLat => [lng, lat]), color, weight, fillOp);
                break;
            case "arrow": {
                addLine(id, s.latlngs.map(([lat, lng]): LngLat => [lng, lat]), color, weight, fillOp);
                if (s.latlngs.length >= 2) {
                    const n = s.latlngs.length;
                    const deg = bearing(s.latlngs[n - 2]!, s.latlngs[n - 1]!);
                    const sz = arrowheadSize(zoom);
                    markers.push(markerAt(s.latlngs[n - 1]!, arrowheadSvg(color, deg, sz, fillOp), "center").addTo(map));
                }
                break;
            }
            case "circle": {
                const center = s.latlngs[0]!;
                const radiusM = haversineDistance(center, s.latlngs[1]!);
                addFilledPolygon(id, circleRing(center, radiusM), color, fillOp, strokeC, hasBorder ? weight : 2, borderOp);
                break;
            }
            case "rect":
                addFilledPolygon(id, boundsRing(s.latlngs[0]!, s.latlngs[1]!), color, fillOp, strokeC, hasBorder ? weight : 2, borderOp);
                break;
            case "polygon":
                addFilledPolygon(id, polygonRing(s.latlngs), color, fillOp, strokeC, hasBorder ? weight : 2, borderOp);
                break;
            case "text":
                markers.push(markerAt(s.latlngs[0]!, textLabelHtml(s), "top-left").addTo(map));
                break;
            case "pin": {
                const sz = 32;
                const html = `<span class="material-symbols-outlined" style="font-size:${sz}px;color:${color};text-shadow:0 1px 3px rgba(0,0,0,.4)">location_on</span>`;
                markers.push(markerAt(s.latlngs[0]!, html, "bottom").addTo(map));
                break;
            }
        }
    });

    return {
        remove(): void {
            for (const marker of markers) marker.remove();
            for (const layerId of layerIds) if (map.getLayer(layerId)) map.removeLayer(layerId);
            for (const sourceId of sourceIds) if (map.getSource(sourceId)) map.removeSource(sourceId);
        },
    };
}

export const MaplibreMarkup = { renderShapeGroup };

/** Publishes the renderer on window for the classic inline template scripts and hand-written vanilla JS (e.g. `comment-map.js`). */
export function installGlobalMaplibreMarkup(): void {
    window.MaplibreMarkup = MaplibreMarkup;
}

declare global {
    interface Window {
        MaplibreMarkup: typeof MaplibreMarkup;
    }
}
