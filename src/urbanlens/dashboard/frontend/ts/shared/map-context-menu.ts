/**
 * Shared right-click menu for every Leaflet map: copy coordinates, Street View
 * when imagery exists, and directions to the clicked point.
 *
 * Page-specific actions (add a pin, create a child pin, edit a boundary) are
 * extra items passed by the caller, so every map starts from the same base
 * rather than each page reinventing a slightly different menu.
 *
 * Installed automatically by `createMapLayers` unless a map opts out (the main
 * map builds extra "Add Pin Here" items on top of `showMapContextMenu`; the
 * floorplan editor has its own specialised menu).
 */

declare const L: typeof import("leaflet");

import { toast } from "./dialogs";

/** Metadata endpoint used to hide Street View when Google has no coverage. */
export const STREETVIEW_CHECK_URL = "/dashboard/map/streetview-check/";

export interface ContextMenuAction {
    kind?: "button";
    icon: string;
    label: string;
    title?: string;
    className?: string;
    onClick: () => void;
}

export interface ContextMenuLink {
    kind: "link";
    icon: string;
    label: string;
    href: string;
    className?: string;
}

export interface ContextMenuSeparator {
    kind: "separator";
}

export type ContextMenuItem = ContextMenuAction | ContextMenuLink | ContextMenuSeparator | HTMLElement;

export interface ShowMapContextMenuOptions {
    lat: number;
    lng: number;
    clientX: number;
    clientY: number;
    /** Leaflet zoom; extra decimal places are kept when zoomed in. */
    zoom?: number;
    /** Place name or other heading above the coordinate row. */
    header?: string | HTMLElement;
    /** Extra block between the header and the shared actions (place details, etc.). */
    preamble?: HTMLElement;
    extraItems?: ContextMenuItem[];
    /** Called after the menu is removed, including outside-click dismiss. */
    onClose?: () => void;
    streetViewCheckUrl?: string;
}

export interface BindMapContextMenuOptions {
    extraItems?: (lat: number, lng: number) => ContextMenuItem[];
    shouldOpen?: (event: L.LeafletMouseEvent) => boolean;
    onOpen?: (lat: number, lng: number, event: L.LeafletMouseEvent) => void;
    onClose?: () => void;
    streetViewCheckUrl?: string;
}

const MENU_CLASS = "map-context-menu";

/** Fewest decimal places we have always copied (~11 cm). Never go below this. */
export const MIN_COORDINATE_COPY_DIGITS = 6;
/** Cap: sub-centimetre, beyond what a mouse click can specify even at max zoom. */
const MAX_COORDINATE_COPY_DIGITS = 8;
/** First zoom at which a pixel is fine enough to justify a 7th decimal (~1 cm). */
const ZOOM_FOR_7_DIGITS = 17;
/** First zoom at which a pixel is fine enough to justify an 8th decimal (~1 mm). */
const ZOOM_FOR_8_DIGITS = 19;

/**
 * Decimal places for a copied lat/lng, based on Leaflet zoom.
 *
 * A mouse click is a couple of pixels; at city/street zooms that is metres
 * wide, so six decimals (what we have always copied) is already more precise
 * than the click. Once the map is zoomed into a building or closer, extra
 * digits keep the click's true position instead of rounding it away.
 *
 * @param zoom - Leaflet zoom, or a non-finite stand-in when the map is unknown.
 * @returns An integer in ``[6, 8]``.
 */
export function coordinateCopyPrecision(zoom: number | undefined): number {
    if (!Number.isFinite(zoom)) return MIN_COORDINATE_COPY_DIGITS;
    if ((zoom as number) >= ZOOM_FOR_8_DIGITS) return MAX_COORDINATE_COPY_DIGITS;
    if ((zoom as number) >= ZOOM_FOR_7_DIGITS) return 7;
    return MIN_COORDINATE_COPY_DIGITS;
}

/** Formats ``lat, lng`` at the precision a click at *zoom* can realistically specify. */
export function formatCopiedCoordinates(lat: number, lng: number, zoom?: number): string {
    const digits = coordinateCopyPrecision(zoom);
    return `${lat.toFixed(digits)}, ${lng.toFixed(digits)}`;
}

let dismissHandler: ((event: MouseEvent) => void) | null = null;
let closeCallback: (() => void) | null = null;

function appendIconLabel(el: HTMLElement, icon: string, label: string): void {
    const glyph = document.createElement("i");
    glyph.className = "material-symbols-outlined";
    glyph.textContent = icon;
    const text = document.createElement("span");
    text.textContent = label;
    el.append(glyph, text);
}

/** Keeps a position:fixed menu inside the viewport. */
export function placeFloatingMenu(menu: HTMLElement, clientX: number, clientY: number): void {
    const margin = 8;
    menu.style.maxWidth = `${window.innerWidth - margin * 2}px`;
    menu.style.maxHeight = `${window.innerHeight - margin * 2}px`;
    menu.style.overflowY = "auto";
    menu.style.left = "0px";
    menu.style.top = "0px";
    if (!menu.isConnected) document.body.appendChild(menu);
    const { width, height } = menu.getBoundingClientRect();
    const left = clientX + width + margin > window.innerWidth ? clientX - width : clientX;
    const top = clientY + height + margin > window.innerHeight ? clientY - height : clientY;
    const maxLeft = Math.max(margin, window.innerWidth - width - margin);
    const maxTop = Math.max(margin, window.innerHeight - height - margin);
    menu.style.left = `${Math.min(Math.max(left, margin), maxLeft)}px`;
    menu.style.top = `${Math.min(Math.max(top, margin), maxTop)}px`;
}

/** Viewport anchor for a marker click, falling back to the marker's on-screen point. */
export function markerMenuAnchor(map: L.Map, event: L.LeafletMouseEvent, lat: number, lng: number): { x: number; y: number } {
    const src = event.originalEvent;
    if (src && Number.isFinite(src.clientX) && Number.isFinite(src.clientY)) {
        return { x: src.clientX, y: src.clientY };
    }
    const rect = map.getContainer().getBoundingClientRect();
    const pt = map.latLngToContainerPoint([lat, lng]);
    return { x: rect.left + pt.x, y: rect.top + pt.y };
}

export function closeMapContextMenus(): void {
    if (dismissHandler) {
        document.removeEventListener("click", dismissHandler);
        dismissHandler = null;
    }
    document.querySelectorAll(`.${MENU_CLASS}`).forEach((menu) => menu.remove());
    const onClose = closeCallback;
    closeCallback = null;
    onClose?.();
}

function appendItem(menu: HTMLElement, item: ContextMenuItem, close: () => void): void {
    if (item instanceof HTMLElement) {
        menu.appendChild(item);
        return;
    }
    if (item.kind === "separator") {
        const sep = document.createElement("div");
        sep.className = `${MENU_CLASS}__sep`;
        menu.appendChild(sep);
        return;
    }
    if (item.kind === "link") {
        const link = document.createElement("a");
        link.className = [`${MENU_CLASS}__item`, `${MENU_CLASS}__item--link`, item.className].filter(Boolean).join(" ");
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.href = item.href;
        appendIconLabel(link, item.icon, item.label);
        link.addEventListener("click", () => close());
        menu.appendChild(link);
        return;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = [`${MENU_CLASS}__item`, item.className].filter(Boolean).join(" ");
    if (item.title) button.title = item.title;
    appendIconLabel(button, item.icon, item.label);
    button.addEventListener("click", () => {
        item.onClick();
        close();
    });
    menu.appendChild(button);
}

/**
 * Opens the shared map menu at a viewport point.
 *
 * @returns The menu element, so callers that later grow its contents (place
 *     details arriving asynchronously) can re-place it.
 */
export function showMapContextMenu(options: ShowMapContextMenuOptions): HTMLElement {
    closeMapContextMenus();
    closeCallback = options.onClose ?? null;

    const coordText = formatCopiedCoordinates(options.lat, options.lng, options.zoom);
    const menu = document.createElement("div");
    menu.className = MENU_CLASS;

    const close = (): void => closeMapContextMenus();

    if (options.header) {
        if (typeof options.header === "string") {
            const header = document.createElement("div");
            header.className = `${MENU_CLASS}__header`;
            header.textContent = options.header;
            menu.appendChild(header);
        } else {
            menu.appendChild(options.header);
        }
        const sep = document.createElement("div");
        sep.className = `${MENU_CLASS}__sep`;
        menu.appendChild(sep);
    }
    if (options.preamble) {
        menu.appendChild(options.preamble);
        const sep = document.createElement("div");
        sep.className = `${MENU_CLASS}__sep`;
        menu.appendChild(sep);
    }

    const coordItem = document.createElement("button");
    coordItem.type = "button";
    coordItem.className = `${MENU_CLASS}__item ${MENU_CLASS}__coords`;
    coordItem.title = "Click to copy coordinates";
    appendIconLabel(coordItem, "gps_fixed", coordText);
    coordItem.addEventListener("click", () => {
        navigator.clipboard
            .writeText(coordText)
            .then(() => toast.success("Coordinates copied to clipboard"))
            .catch(() => toast.error("Could not copy coordinates"));
        close();
    });
    menu.appendChild(coordItem);

    const svItem = document.createElement("a");
    svItem.className = `${MENU_CLASS}__item ${MENU_CLASS}__item--link ${MENU_CLASS}__streetview`;
    svItem.target = "_blank";
    svItem.rel = "noopener noreferrer";
    svItem.href = `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${options.lat},${options.lng}`;
    appendIconLabel(svItem, "streetview", "Street View");
    svItem.style.display = "none";
    menu.appendChild(svItem);

    const checkUrl = options.streetViewCheckUrl || STREETVIEW_CHECK_URL;
    fetch(`${checkUrl}?lat=${options.lat}&lng=${options.lng}`)
        .then((response) => response.json())
        .then((data: { available?: boolean }) => {
            // parentNode is gone after dismiss; don't resurrect a closed menu
            // when the coverage check lands late.
            if (!data.available || !menu.parentNode) return;
            svItem.style.display = "";
            placeFloatingMenu(menu, options.clientX, options.clientY);
        })
        .catch(() => {
            /* coverage check is best-effort; Directions still works */
        });

    const dirItem = document.createElement("a");
    dirItem.className = `${MENU_CLASS}__item ${MENU_CLASS}__item--link`;
    dirItem.target = "_blank";
    dirItem.rel = "noopener noreferrer";
    dirItem.href = `https://www.google.com/maps/dir/?api=1&destination=${options.lat},${options.lng}`;
    appendIconLabel(dirItem, "directions", "Directions here");
    menu.appendChild(dirItem);

    (options.extraItems || []).forEach((item) => appendItem(menu, item, close));

    placeFloatingMenu(menu, options.clientX, options.clientY);

    const dismiss = (event: MouseEvent): void => {
        if (!menu.contains(event.target as Node)) close();
    };
    dismissHandler = dismiss;
    // Deferred so the click that opened the menu doesn't immediately dismiss
    // it. closeMapContextMenus() can run first (a second right-click, an item
    // click, a route change), and it nulls dismissHandler - so re-check that
    // this menu is still the open one instead of registering a stale handler.
    setTimeout(() => {
        if (dismissHandler === dismiss) document.addEventListener("click", dismiss);
    }, 0);

    return menu;
}

function defaultShouldOpen(map: L.Map, event: L.LeafletMouseEvent): boolean {
    const target = event.originalEvent.target as Element | null;
    if (target?.closest?.(".leaflet-control, .leaflet-popup, .map-context-menu")) return false;
    // Leaflet.Draw (and similar tools) set a crosshair while armed; a draw
    // gesture's right-click is not a request for this menu.
    if (map.getContainer().classList.contains("leaflet-crosshair")) return false;
    return true;
}

/**
 * Binds the shared menu to a map's `contextmenu` event.
 *
 * @returns An unbind function.
 */
export function bindMapContextMenu(map: L.Map, options: BindMapContextMenuOptions = {}): () => void {
    const onContextMenu = (event: L.LeafletMouseEvent): void => {
        const shouldOpen = options.shouldOpen ?? ((e) => defaultShouldOpen(map, e));
        if (!shouldOpen(event)) return;
        map.closePopup();
        const { lat, lng } = event.latlng;
        options.onOpen?.(lat, lng, event);
        showMapContextMenu({
            lat,
            lng,
            zoom: map.getZoom(),
            clientX: event.originalEvent.clientX,
            clientY: event.originalEvent.clientY,
            extraItems: options.extraItems?.(lat, lng) ?? [],
            onClose: options.onClose,
            streetViewCheckUrl: options.streetViewCheckUrl,
        });
    };
    map.on("contextmenu", onContextMenu);
    return () => {
        map.off("contextmenu", onContextMenu);
    };
}

export const MapContextMenu = {
    show: showMapContextMenu,
    close: closeMapContextMenus,
    place: placeFloatingMenu,
    bind: bindMapContextMenu,
    markerAnchor: markerMenuAnchor,
};

/** Publishes the menu on window for classic inline template scripts. */
export function installGlobalMapContextMenu(): void {
    window.MapContextMenu = MapContextMenu;
}

declare global {
    interface Window {
        MapContextMenu: typeof MapContextMenu;
    }
}
