/**
 * Marker icons and popups for a pin list's read-only overview map, in the main map's markup.
 */

import { safeColor } from "./color-safety";
import { escHtml } from "./escape-html";

export interface OverviewTag {
    name: string;
    color?: string | null;
}

/** One entry of the ``pin-list-items-map-data`` payload (``_pin_map_marker_data``). */
export interface OverviewPoint {
    uuid: string;
    name: string;
    url: string;
    icon: string | null;
    color: string | null;
    rating: number | null;
    address: string;
    description: string;
    last_visited: string;
    latitude: number;
    longitude: number;
    tags_data: OverviewTag[];
}

function normalizeHexColor(hex: string): string {
    let h = hex.replace("#", "").toLowerCase();
    if (h.length === 3)
        h = h
            .split("")
            .map((c) => c + c)
            .join("");
    return h;
}

function hexToRgba(hex: string, alpha: number): string {
    const h = normalizeHexColor(hex);
    const r = Number.parseInt(h.slice(0, 2), 16);
    const g = Number.parseInt(h.slice(2, 4), 16);
    const b = Number.parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${alpha})`;
}

export interface OverviewIcon {
    html: string;
    /** Drawn inside a tinted circle, which needs the larger icon box. */
    circled: boolean;
}

/**
 * The divIcon markup for a point, or ``null`` when it has no icon and keeps Leaflet's default marker.
 */
export function overviewIcon(pt: OverviewPoint): OverviewIcon | null {
    if (!pt.icon) return null;
    const color = safeColor(pt.color);
    let iconHtml: string;
    if (/^[a-z_]+$/.test(pt.icon)) {
        iconHtml = `<i class="material-icons map-pin-icon"${color ? "" : ' style="color:#555555;"'}>${escHtml(pt.icon)}</i>`;
    } else if (/^(https?:\/\/|\/)/.test(pt.icon)) {
        iconHtml = `<img src="${escHtml(pt.icon)}" class="map-pin-custom-img" alt="">`;
    } else {
        iconHtml = `<span class="map-pin-emoji">${escHtml(pt.icon)}</span>`;
    }
    if (!color) return { html: iconHtml, circled: false };
    const html = `<span class="map-pin-with-circle"><span class="map-pin-color-circle map-pin-color-circle--${escHtml(normalizeHexColor(color))}" style="background:${escHtml(hexToRgba(color, 0.8))};"></span><span class="map-pin-icon-layer">${iconHtml}</span></span>`;
    return { html, circled: true };
}

export function overviewPopupHtml(pt: OverviewPoint): string {
    const rating = Number(pt.rating) || 0;
    const stars = [1, 2, 3, 4, 5].map((i) => `<span class="popup-star ${i <= rating ? "popup-star--on" : ""}">★</span>`).join("");
    const chips = (pt.tags_data || [])
        .map((t) => {
            const tagColor = safeColor(t.color);
            const bg = tagColor ? `${tagColor}22` : "rgba(100,120,160,0.18)";
            const border = tagColor ? `${tagColor}55` : "rgba(100,120,160,0.3)";
            return `<span class="popup-tag-chip" style="background:${escHtml(bg)};border-color:${escHtml(border)};">${escHtml(t.name)}</span>`;
        })
        .join("");
    return (
        `<div class="pin-popup" data-uuid="${escHtml(pt.uuid)}">` +
        `<div class="popup-title">${escHtml(pt.name)}</div>` +
        (pt.address ? `<div class="popup-address"><i class="material-icons" style="font-size:.7rem;vertical-align:middle;opacity:.6;">location_on</i> ${escHtml(pt.address)}</div>` : "") +
        (pt.description ? `<div class="popup-desc">${escHtml(pt.description)}</div>` : "") +
        (pt.last_visited && pt.last_visited !== "Never"
            ? `<div class="popup-meta"><span class="popup-visited"><i class="material-icons" style="font-size:.75rem;vertical-align:middle;">schedule</i> ${escHtml(pt.last_visited)}</span></div>`
            : "") +
        `<div class="popup-stars" style="cursor:default;">${stars}</div>` +
        (chips ? `<div class="popup-tags">${chips}</div>` : "") +
        `<div class="popup-actions"><a href="${escHtml(pt.url)}" class="view-full-pin">View Details</a></div>` +
        "</div>"
    );
}
