/**
 * The map of pins two people have both saved (``pages/profile/common_pins.html``), from its ``#common-pins-map-data``
 * island: one marker per pin, framed to fit them all.
 */

declare const L: typeof import("leaflet");

export interface CommonPinPoint {
    latitude: number;
    longitude: number;
    name: string;
    url: string;
}

const CONTINENTAL_US: [number, number] = [39.8283, -98.5795];

const inRange = (value: unknown, limit: number): value is number => typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= limit;

export function commonPinPoints(raw: unknown): CommonPinPoint[] {
    if (!Array.isArray(raw)) return [];
    return raw.flatMap((item: unknown) => {
        if (!item || typeof item !== "object") return [];
        const latitude: unknown = Reflect.get(item, "latitude");
        const longitude: unknown = Reflect.get(item, "longitude");
        if (!inRange(latitude, 90) || !inRange(longitude, 180)) return [];
        const name: unknown = Reflect.get(item, "name");
        const url: unknown = Reflect.get(item, "url");
        return [{ latitude, longitude, name: typeof name === "string" ? name : "", url: typeof url === "string" ? url : "" }];
    });
}

/** Links only to a path on this site. */
const isSitePath = (url: string): boolean => url.startsWith("/") && !url.startsWith("//");

export function commonPinPopup(point: CommonPinPoint): HTMLElement {
    const popup = document.createElement("div");
    popup.className = "pin-popup";
    const title = document.createElement("div");
    title.className = "popup-title";
    title.textContent = point.name;
    popup.append(title);
    if (isSitePath(point.url)) {
        const actions = document.createElement("div");
        actions.className = "popup-actions";
        const link = document.createElement("a");
        link.className = "view-full-pin";
        link.href = point.url;
        link.textContent = "View Details";
        actions.append(link);
        popup.append(actions);
    }
    return popup;
}

export function installCommonPinsMap(el: HTMLElement, data: HTMLElement | null): void {
    let raw: unknown = [];
    try {
        raw = JSON.parse(data?.textContent || "[]");
    } catch {
        return;
    }
    const points = commonPinPoints(raw);
    const [first] = points;
    if (!first) return;
    const map = L.map(el, { attributionControl: false }).setView(CONTINENTAL_US, 4);
    window.map = map;
    window.MapLayers.create(map, { root: document.getElementById("common-pins-map-layers"), onAttribution: window.MapLayers.setAttribution });
    const markers = points.map((point) => L.marker([point.latitude, point.longitude]).bindPopup(commonPinPopup(point)).addTo(map));
    if (markers.length > 1) map.fitBounds(L.featureGroup(markers).getBounds().pad(0.15));
    else map.setView([first.latitude, first.longitude], 14);
    setTimeout(() => map.invalidateSize(), 0);
}
