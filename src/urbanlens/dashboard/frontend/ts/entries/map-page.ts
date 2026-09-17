/**
 * The map page's behaviour. Bundled as an ESM entry (see bin/build-frontend.ts) so the browser caches
 * it: inline, it was 275 KB of every map view. Everything the server knows arrives in the
 * #map-page-config element.
 */
import type { HtmxApi } from "../types/globals";
import { deletePinCascade } from "../shared/confirm-dialog";
import { confirmAction } from "../shared/dialogs";
import { fetchJson, sendJson, type FetchJsonOptions } from "../shared/fetch-json";
import { createPinClusterGroup, isAdditiveClick as sharedIsAdditiveClick } from "../shared/map-clusters";
import { PIN_CACHE_VERSION, pinCacheKey, purgeForeignPinCaches } from "../shared/pin-cache";
import { createChipPicker, createFilterPicker, type ChipPickerApi, type FilterPickerApi, type LabelGroup } from "../shared/label-picker";
import { MapContextMenu } from "../shared/map-context-menu";
import { MapLayers, type MapDarkMode, type MapLayersInstance } from "../shared/map-layers";
import { LocationSearchEngine, type LocationSearchAttachOptions } from "../shared/location-search-engine";

declare const L: typeof import("leaflet");
declare const htmx: HtmxApi;

declare global {
    interface Window {
        UrbanLensDualRangeSlider?: {
            resetAll: (form: HTMLFormElement) => void;
            sync: (root: HTMLElement) => void;
        };
        // The live Leaflet map instance, exposed for debugging from the console.
        map: L.Map;
        // Debug-only leftover, never read - always assigned null.
        pin: null;
        // Set when a saved-filter deep link (label_groups) arrives via URL params, so the
        // first filtered pin batch can zoom to fit rather than leaving the default view.
        _fitToFilteredPinsOnce?: boolean;
        _addPinCustomIconFile: File | null;
        // IconPicker, pickColor and openAddPinDialog are each also declared (with a
        // different but assignable signature) by shared/icon-picker.ts,
        // shared/color-picker.ts and entries/map-annotations.ts respectively - those
        // other pages' bundles never load here, but `tsc` type-checks every entry
        // point as one program, so this page's richer functions are assigned to
        // those ambient names rather than re-declaring (and conflicting with) them.
        // _refreshAllPins is likewise already declared (optional) by shared/undo-bar.ts.
        addMarker: typeof addMarker;
        updatePinCounter: typeof updatePinCounter;
        invalidatePinCache: () => void;
        forceRefreshPinCache: () => Promise<number>;
        updateCachedPin: (pinData: PinData) => void;
        findLocalPinNear: (lat: number, lng: number, thresholdMeters?: number) => PinData | null;
        applyMapDarkMode: (mode: string) => void;
        closePinPopupMenus: typeof closePinPopupMenus;
        togglePinPopupMenu: typeof togglePinPopupMenu;
        deletePin: typeof deletePin;
        promoteChildPins: typeof promoteChildPins;
        toggleSelectMode: typeof toggleSelectMode;
        exitSelectMode: typeof exitSelectMode;
        _exportSelection: typeof _exportSelection;
        openBulkDeleteDialog: typeof openBulkDeleteDialog;
        _undoBulkDelete: typeof _undoBulkDelete;
        openBulkMergeDialog: typeof openBulkMergeDialog;
        openBulkEditDialog: typeof openBulkEditDialog;
        toggleFilterPanel: typeof toggleFilterPanel;
        _togglePinListPanel: typeof _togglePinListPanel;
        _flyToPinFromList: typeof _flyToPinFromList;
        openAddToListDialog: typeof openAddToListDialog;
        filterAddToListResults: typeof filterAddToListResults;
        addPinsToList: typeof addPinsToList;
        createListAndAddPins: typeof createListAndAddPins;
        resetFilters: typeof resetFilters;
        applySavedFilter: typeof applySavedFilter;
        toggleToolbarSavedFilter: typeof toggleToolbarSavedFilter;
        sfToolbarPage: typeof sfToolbarPage;
        _resetVisits: typeof _resetVisits;
        _resetVisitDates: typeof _resetVisitDates;
        _resetCreatedDates: typeof _resetCreatedDates;
        _dateBuiltRange: typeof _dateBuiltRange;
        _dateAbandonedRange: typeof _dateAbandonedRange;
        _lastViewedRange: typeof _lastViewedRange;
        _exitFilterMode: typeof _exitFilterMode;
        _apdlgClearCustomizations: typeof _apdlgClearCustomizations;
        _apdlgDeletePin: typeof _apdlgDeletePin;
        openEditPinDialog: typeof openEditPinDialog;
        closeAddPinDialog: typeof closeAddPinDialog;
    }
}

// -- Server-provided page config --------------------------------------------

interface MapPageUrls {
    labelCreateCategory: string;
    labelCreateStatus: string;
    labelCreateTag: string;
    listsCreate: string;
    listsItemsAdd: string;
    mapAutocompleteEmpty: string;
    mapAutocompleteLocal: string;
    mapAutocompletePlaces: string;
    mapDocument: string;
    mapGeolocationVisits: string;
    mapInfrastructure: string;
    mapPinJson: string;
    mapPins: string;
    mapPinsChildren: string;
    mapPinsList: string;
    mapPinsMeta: string;
    mapPlacesDetails: string;
    mapPlacesNearby: string;
    mapResolvePlace: string;
    pinAdd: string;
    pinBulkDelete: string;
    pinBulkEdit: string;
    pinBulkEditLabelOptions: string;
    pinBulkMerge: string;
    pinBulkUndo: string;
    pinParentSearch: string;
    savedFiltersCounts: string;
    settingsSaveMapDarkMode: string;
    settingsSaveMapPosition: string;
}

interface MapPageConfig {
    urls: MapPageUrls;
    assets: {
        leafletMarkerIcon: string;
        leafletMarkerShadow: string;
    };
    csrfToken: string;
    profileId: number;
    profileUuid: string;
    appUuid: string;
    openweathermapApiKey: string;
    pinCount: number;
    clusterRadius: number | null;
    showOnboardingTips: boolean;
    showPinCount: boolean;
    showFilteredPinCount: boolean;
    showPlacesLayer: boolean;
    usePinCache: boolean;
    mapCenterMode: string;
    mapCenterLat: number | null;
    mapCenterLng: number | null;
    gpsFallbackLat: number | null;
    gpsFallbackLng: number | null;
    geolocationTrackingAllowed: boolean;
    mapDefaultZoom: number;
    defaultMapView: string;
    mapDarkMode: string;
}

const MAP_CFG: MapPageConfig = JSON.parse(document.getElementById("map-page-config")!.textContent!);

// -- HTML-building safety ---------------------------------------------------
// This script builds markup by interpolation in a lot of places. The two
// contexts need different escaping and are easy to mix up: a quote ends an
// attribute but is inert in text, so a text escaper used on an attribute value
// looks like protection and is not. (There is a second, partial pair of these
// further down, local to the location-conflict dialog - these are the ones the
// rest of the script can see.)
function _ulEscText(value: unknown): string {
    return String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}
function _ulEscAttr(value: unknown): string {
    return _ulEscText(value).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
// Anything reaching href=/src= also needs its *scheme* checked - escaping quotes
// does nothing about `javascript:`. Relative paths and http(s) only.
function _ulSafeUrl(value: unknown): string {
    const url = String(value == null ? "" : value).trim();
    return /^(?:https?:\/\/|\/(?!\/))/i.test(url) ? url : "";
}

const _PROFILE_UUID = MAP_CFG.profileUuid;
const _APP_UUID = MAP_CFG.appUuid; // server instance UUID - changes on DB wipe
const _SHOW_FILTERED_PIN_COUNT = MAP_CFG.showFilteredPinCount;
const _SHOW_PIN_COUNT = MAP_CFG.showPinCount;
const _USE_CACHE = MAP_CFG.usePinCache;
// Keys/version now come from shared/pin-cache.ts (the only reader) instead of
// duplicated literals - see pin-cache.contract.test.ts for why that mattered
// when this lived in a classic script that could not import it.
const _CACHE_KEY = pinCacheKey(_PROFILE_UUID);
const _CACHE_MAX = 180 * 24 * 60 * 60 * 1000; // 6 months: space reclaim for inactive users
const _POLL_INTERVAL = 2 * 60 * 1000; // 2 min: background poll for pin changes
const _TILE_SIZE = 0.5; // degrees per tile edge (~55 km at mid-latitudes)
const _TILE_PAD = 1.0; // degrees of prefetch padding around the viewport
const _MAX_TILES = 200; // threshold above which we just fetch the whole world

// -- Pin data shape ----------------------------------------------------------
// The map endpoints' pin payload. Kept permissive (most fields optional) since
// different endpoints (tiles, document stream, quick-edit refresh, cache) send
// different subsets, and the cache itself only persists _CACHE_FIELDS.
interface LabelDictEntry {
    id?: number | string;
    kind?: string;
    name: string;
    color?: string;
    icon?: string;
}
type LabelDict = Record<string, LabelDictEntry>;

/** A label as the server's label-list/label-options endpoints send it (numeric PK). */
interface LabelCandidate {
    id: number;
    name: string;
    icon?: string;
    color?: string;
    kind?: string;
}

interface PinTagLike {
    id?: number | string;
    name: string;
    color?: string;
    icon?: string;
    kind?: string;
}

interface PinData {
    uuid: string;
    id?: number;
    name?: string;
    icon?: string;
    color?: string;
    own_icon?: string;
    own_custom_icon_url?: string;
    own_color?: string;
    latitude?: string | number;
    longitude?: string | number;
    description?: string;
    last_visited?: string;
    rating?: number | string;
    label_ids?: Array<number | string>;
    tags?: Array<string | PinTagLike>;
    tags_data?: PinTagLike[];
    viewLocationUrl?: string;
    address?: string;
    cover_photo_url?: string;
    child_count?: number;
    slug?: string;
    parent_url?: string;
    parent_name?: string;
    url?: string;
    [key: string]: unknown;
}

interface PinPage {
    pins: PinData[];
    next_cursor: string | null;
    total: number | null;
}

// -- Shared state ----------------------------------------------------------
const _pinStore = new Map<string, PinData>(); // uuid → pin data
// id (as a string) → {id, kind, name, color, icon}. A pin names its labels
// by id; every map response carries whatever ids it uses.
let _labelDict: LabelDict = {};
// Entries that moved since a refresh last acted on them. A label edit shows
// up here rather than in any pin, so this is the only thing that can say a
// marker needs rebuilding for one.
const _changedLabelIds = new Set<string>();
const _fetchedTiles = new Set<string>(); // tile keys already loaded from server
const _markerMap = new Map<string, L.Marker>(); // uuid → L.Marker
let _filterMode = false; // true while search/filter results are shown
let _viewportTid: ReturnType<typeof setTimeout> | null = null; // debounce timer for viewport changes
let _pinListTid: ReturnType<typeof setTimeout> | null = null; // debounce timer for pin-list-sidebar viewport refresh
let _cacheTid: ReturnType<typeof setTimeout> | null = null; // debounce timer for cache writes
let _lastKnownUpdated: string | null = null; // server last_updated from most recent meta poll
let pinsVisible = true;
// Server-authoritative total pin count (updated after a full refresh).
let _totalPins = MAP_CFG.pinCount;
let _isOffline = false; // true when the meta poll returns an error/network failure
let _searchMarker: L.Marker | null = null; // temporary Leaflet marker placed on search-result jump
let _userLocationMarker: L.Marker | null = null; // persistent "you are here" dot from the most recent GPS fix
let _contextMenuMarker: L.Marker | null = null; // temporary marker while the map right-click menu is open

// -- Multi-select tool -------------------------------------------------------
let _selectMode = false; // true while the select tool is active
const _selectedPinUuids = new Set<string>();
let _dragSelectRect: L.Rectangle | null = null; // Leaflet rectangle preview during an active drag-select gesture
// Last plain-clicked pin, so a subsequent ctrl/cmd-click can enter select
// mode with both that pin and the new one selected.
let _lastClickedPinUuid: string | null = null;

function _isAdditiveClick(e: L.LeafletMouseEvent): boolean {
    return sharedIsAdditiveClick(e);
}

function _handleSelectablePinClick(e: L.LeafletMouseEvent, uuid: string, marker: L.Marker): void {
    const additive = _isAdditiveClick(e);
    if (_selectMode) {
        marker.closePopup();
        L.DomEvent.stop(e);
        _togglePinSelection(uuid);
        return;
    }
    if (additive) {
        marker.closePopup();
        L.DomEvent.stop(e);
        map.closePopup();
        enterSelectMode();
        if (_lastClickedPinUuid && _lastClickedPinUuid !== uuid && !_selectedPinUuids.has(_lastClickedPinUuid) && _anyMarker(_lastClickedPinUuid)) {
            _togglePinSelection(_lastClickedPinUuid);
        }
        if (!_selectedPinUuids.has(uuid)) _togglePinSelection(uuid);
        _lastClickedPinUuid = null;
        return;
    }
    _lastClickedPinUuid = uuid;
}

// -- Map -------------------------------------------------------------------
const _MAP_CENTER_MODE = MAP_CFG.mapCenterMode;
const _SERVER_CENTER_LAT = MAP_CFG.mapCenterLat;
const _SERVER_CENTER_LNG = MAP_CFG.mapCenterLng;
// Pin-cluster centroid - used as fallback when GPS permission is denied.
const _GPS_FALLBACK_LAT = MAP_CFG.gpsFallbackLat;
const _GPS_FALLBACK_LNG = MAP_CFG.gpsFallbackLng;
const _defaultZoom = MAP_CFG.mapDefaultZoom;
const _DEFAULT_MAP_VIEW = MAP_CFG.defaultMapView;
let _MAP_DARK_MODE = MAP_CFG.mapDarkMode;
// Per-profile key for remembering active layers across sessions.
const _LAYER_CACHE_KEY = `ul_layers_v1_${_PROFILE_UUID}`;
const _GEOLOCATION_VISIT_URL = MAP_CFG.urls.mapGeolocationVisits;
// GPS is still used to center the map (client-side only) even when this is
// false; it just stops that fix from being relayed to the server at all.
const _GEOLOCATION_TRACKING_ALLOWED = MAP_CFG.geolocationTrackingAllowed;

// -- Browser-side user location cache (never sent to server) --------------
const _USER_LOC_KEY = "ul_user_location_v1";
// Kept long so a stale-but-plausible last-known position beats the generic
// fallback center on the very common case of returning >5 min after the last visit.
// A fresh fix is still requested on every load and silently replaces this center.
const _USER_LOC_MAX_AGE = 7 * 24 * 60 * 60 * 1000; // 7 days: used for initial map centre

interface CachedUserLocation {
    lat: number;
    lng: number;
}

function _getCachedUserLocation(): CachedUserLocation | null {
    try {
        const raw = localStorage.getItem(_USER_LOC_KEY);
        if (!raw) return null;
        const { lat, lng, ts } = JSON.parse(raw) as { lat: number; lng: number; ts: number };
        if (lat != null && lng != null && Date.now() - ts < _USER_LOC_MAX_AGE) return { lat, lng };
    } catch {
        /* corrupt/unavailable - fall through to null */
    }
    return null;
}

function _cacheUserLocation(lat: number, lng: number): void {
    try {
        localStorage.setItem(_USER_LOC_KEY, JSON.stringify({ lat, lng, ts: Date.now() }));
    } catch {
        /* private mode / quota - best effort only */
    }
}

function _recordGeolocationVisit(lat: number, lng: number): void {
    fetch(_GEOLOCATION_VISIT_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": MAP_CFG.csrfToken },
        body: JSON.stringify({ latitude: lat, longitude: lng }),
        keepalive: true,
    }).catch(() => {
        // Visit tracking is best-effort and should never block map use.
    });
}

// For GPS mode, prefer a recently cached user position over the generic fallback
// so the map opens at the right place without waiting for the geolocation API.
const _cachedUserLoc = _MAP_CENTER_MODE === "gps" ? _getCachedUserLocation() : null;
const _serverCenter: [number, number] =
    _SERVER_CENTER_LAT !== null && _SERVER_CENTER_LNG !== null
        ? [_SERVER_CENTER_LAT, _SERVER_CENTER_LNG]
        : _cachedUserLoc
          ? [_cachedUserLoc.lat, _cachedUserLoc.lng]
          : _GPS_FALLBACK_LAT !== null && _GPS_FALLBACK_LNG !== null
            ? [_GPS_FALLBACK_LAT, _GPS_FALLBACK_LNG]
            : [40.7128, -74.006];

// -- Map viewport URL sync (lat/lng/zoom query params) ---------------------
const _MAP_VIEW_PARAM_KEYS = new Set(["lat", "lng", "zoom"]);

interface MapView {
    center: [number, number];
    zoom: number;
}

function _parseMapViewFromUrl(search: string = location.search): MapView | null {
    const params = new URLSearchParams(search);
    const lat = Number.parseFloat(params.get("lat") ?? "");
    const lng = Number.parseFloat(params.get("lng") ?? "");
    const zoom = Number.parseInt(params.get("zoom") ?? "", 10);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    return {
        center: [lat, lng],
        zoom: Number.isFinite(zoom) && zoom >= 2 && zoom <= 19 ? zoom : _defaultZoom,
    };
}

function _mapViewSearchParams(): { lat: string; lng: string; zoom: string } {
    const c = map.getCenter();
    return {
        lat: c.lat.toFixed(6),
        lng: c.lng.toFixed(6),
        zoom: String(map.getZoom()),
    };
}

function _urlWithMapView(params?: URLSearchParams | string): string {
    const p = params instanceof URLSearchParams ? new URLSearchParams(params) : new URLSearchParams(params ?? "");
    const mv = _mapViewSearchParams();
    p.set("lat", mv.lat);
    p.set("lng", mv.lng);
    p.set("zoom", mv.zoom);
    const qs = p.toString();
    return location.pathname + (qs ? "?" + qs : "");
}

function _mapViewKey(): string {
    const c = map.getCenter();
    return `${c.lat.toFixed(5)},${c.lng.toFixed(5)},${map.getZoom()}`;
}

const _urlMapView = _parseMapViewFromUrl();
const _initialCenter: [number, number] = _urlMapView ? _urlMapView.center : _serverCenter;
const _initialZoom = _urlMapView ? _urlMapView.zoom : _defaultZoom;

let _urlSyncReady = false;
let _programmaticMove = true;
let _urlSyncTid: ReturnType<typeof setTimeout> | null = null;
let _lastSyncedViewKey: string | null = _urlMapView
    ? `${_urlMapView.center[0].toFixed(5)},${_urlMapView.center[1].toFixed(5)},${_urlMapView.zoom}`
    : null;

function _syncMapViewToUrl({ push = false }: { push?: boolean } = {}): void {
    const key = _mapViewKey();
    if (key === _lastSyncedViewKey) return;
    _lastSyncedViewKey = key;
    const params = new URLSearchParams(location.search);
    _MAP_VIEW_PARAM_KEYS.forEach((k) => params.delete(k));
    const mv = _mapViewSearchParams();
    params.set("lat", mv.lat);
    params.set("lng", mv.lng);
    params.set("zoom", mv.zoom);
    const newUrl = location.pathname + (params.toString() ? "?" + params.toString() : "");
    const state = { mapView: true };
    if (push) history.pushState(state, "", newUrl);
    else history.replaceState(state, "", newUrl);
}

function _applyMapViewFromUrl(search: string = location.search): boolean {
    const view = _parseMapViewFromUrl(search);
    if (!view) return false;
    _programmaticMove = true;
    map.setView(view.center, view.zoom);
    _lastSyncedViewKey = `${view.center[0].toFixed(5)},${view.center[1].toFixed(5)},${view.zoom}`;
    return true;
}

function _onMapViewUrlChange(): void {
    if (_programmaticMove) {
        _programmaticMove = false;
        _syncMapViewToUrl({ push: false });
        return;
    }
    if (!_urlSyncReady) return;
    if (_urlSyncTid) clearTimeout(_urlSyncTid);
    _urlSyncTid = setTimeout(() => _syncMapViewToUrl({ push: true }), 400);
}

window.addEventListener("popstate", () => {
    if (_urlSyncTid) clearTimeout(_urlSyncTid);
    _applyMapViewFromUrl(location.search);
});

// minZoom: prevent zooming out past the point where the world-tile fills the container
// maxZoom is higher than any layer's native tile depth; layers set maxNativeZoom so their
// tiles upscale past that depth (Google-like) instead of the layer disappearing.
const map = L.map("map", { maxZoom: 21, minZoom: 2, attributionControl: false }).setView(_initialCenter, _initialZoom);
window.map = map;
window.pin = null;

// Show the "you are here" dot immediately from a cached fix (if any) rather
// than waiting up to 8s for the live read below to resolve; that live read
// then relocates this same marker once it comes back.
//
// Deliberately independent of _MAP_CENTER_MODE: knowing where you are is
// useful on every map, not only one that centres on you. Gating this on
// GPS mode meant a user who centres on their last position - or a custom
// point - had shared their location and still had no idea where they were
// standing relative to their pins.
if (_cachedUserLoc) {
    _showUserLocationMarker(_cachedUserLoc.lat, _cachedUserLoc.lng);
}

// With permission already granted, keep that dot current without prompting
// and without moving the map. Permission is checked first precisely so this
// never turns into a prompt for somebody who hasn't opted in; GPS mode does
// its own (map-moving) read below, so it is excluded here.
if (_MAP_CENTER_MODE !== "gps" && navigator.geolocation && navigator.permissions?.query) {
    navigator.permissions
        .query({ name: "geolocation" })
        .then((status) => {
            if (status.state !== "granted") return;
            navigator.geolocation.getCurrentPosition(
                (pos) => {
                    _cacheUserLocation(pos.coords.latitude, pos.coords.longitude);
                    _showUserLocationMarker(pos.coords.latitude, pos.coords.longitude);
                },
                () => {
                    /* a refused or failed passive read is not worth surfacing */
                },
                { maximumAge: 300000, timeout: 8000 },
            );
        })
        .catch(() => {
            /* Permissions API unavailable - stay with the cached dot */
        });
}

// GPS mode: refine the initial centre with a live geolocation read.
// If we already used a cached position, the map won't visibly jump; we just
// silently refresh the cache so the next load is accurate. If we had no cached
// position to seed the initial centre with, _needsGeoFix below holds the
// cold-start overlay open (briefly) so the fallback centre is never shown.
// Skip when the URL already specifies a viewport (shareable / back-button link).
const _needsGeoFix = _MAP_CENTER_MODE === "gps" && !_cachedUserLoc && !_urlMapView && !!navigator.geolocation;

// Resolves once we have a fresh GPS fix (or fail) or 1s has elapsed, whichever is
// first - the cold-start overlay (see _suppressPinOverlay/hideLoadingMessage below)
// is held open until this fires, so a cache-less first load never flashes the map
// at the wrong place; it shows the loading indicator for at most 1s instead.
let _geoReady = !_needsGeoFix;
const _geoReadyWaiters: Array<() => void> = [];
function _markGeoReady(): void {
    if (_geoReady) return;
    _geoReady = true;
    _geoReadyWaiters.splice(0).forEach((fn) => fn());
}
if (_needsGeoFix) setTimeout(_markGeoReady, 1000);

if (_MAP_CENTER_MODE === "gps" && navigator.geolocation && !_urlMapView) {
    // UL-221: a cached position already seeded _initialCenter above, so the
    // map must NOT visibly jump once this fresh fix resolves - only the
    // cache gets updated (for next load's accuracy). Without this guard
    // every returning GPS-mode user saw the map load at their last known
    // position and then silently snap to the fresh one a moment later,
    // regardless of the comment above already promising it wouldn't.
    const _hadCachedLocation = !!_cachedUserLoc;
    navigator.geolocation.getCurrentPosition(
        (pos) => {
            _cacheUserLocation(pos.coords.latitude, pos.coords.longitude);
            _showUserLocationMarker(pos.coords.latitude, pos.coords.longitude);
            if (_GEOLOCATION_TRACKING_ALLOWED) {
                _recordGeolocationVisit(pos.coords.latitude, pos.coords.longitude);
            }
            if (!_hadCachedLocation) {
                _programmaticMove = true;
                map.setView([pos.coords.latitude, pos.coords.longitude], _defaultZoom);
            }
            _markGeoReady();
        },
        () => {
            // Only apply the pin-centroid fallback when we had nothing better to show.
            if (!_cachedUserLoc && _GPS_FALLBACK_LAT !== null && _GPS_FALLBACK_LNG !== null) {
                _programmaticMove = true;
                map.setView([_GPS_FALLBACK_LAT, _GPS_FALLBACK_LNG], _defaultZoom);
            }
            _markGeoReady();
        },
        { timeout: 8000, maximumAge: 300000 },
    );
}

// -- Cluster layer (replaces plain LayerGroup) -----------------------------
// P92: this used to hand-roll its own L.markerClusterGroup(), including its
// own copy of the numbered-badge iconCreateFunction - a second, driftable
// copy of exactly what shared/map-clusters.ts's createPinClusterGroup()
// already draws for every other map. The main map's only real differences
// from that helper's defaults are its own maxClusterRadius policy and the
// chunked-loading options a multi-thousand-pin account needs; both are
// passed through as overrides instead of re-implemented.
const _userClusterRadius = MAP_CFG.clusterRadius;
const clusterGroup = createPinClusterGroup(
    {
        maxClusterRadius: _userClusterRadius !== null ? _userClusterRadius : (zoom: number) => (zoom <= 10 ? 60 : zoom <= 13 ? 30 : 10),
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
        animate: true,
        animateAddingMarkers: false,
        chunkedLoading: true,
        chunkSize: 400,
        chunkInterval: 60,
    },
    map,
);
clusterGroup.addTo(map);
const pinsLayerGroup = clusterGroup; // backward-compat alias

// -- UI helpers ------------------------------------------------------------
const _tfi = document.getElementById("tile-fetch-indicator");
let _fetching = 0;
function _setFetching(on: boolean, message?: string): void {
    _fetching += on ? 1 : -1;
    if (!_tfi) return;
    // Suppress the small indicator while the full-screen cold-start overlay is active
    const overlay = document.getElementById("pin-load-overlay");
    const overlayActive = overlay && !overlay.classList.contains("hidden");
    _tfi.classList.toggle("visible", _fetching > 0 && !overlayActive);
    if (on && message) {
        const txt = document.getElementById("tfi-text");
        if (txt) txt.textContent = message;
    }
}

function hideLoadingMessage(): void {
    // Hold the overlay open until we have a GPS fix (or 1s has passed) so the
    // map underneath is never revealed centred on the wrong place.
    if (!_geoReady) {
        _geoReadyWaiters.push(hideLoadingMessage);
        return;
    }
    const loadingMessageEl = document.getElementById("loading-pins-message");
    if (loadingMessageEl) loadingMessageEl.style.display = "none";
    // Fade out and then fully hide the cold-start pin load overlay.
    const overlay = document.getElementById("pin-load-overlay");
    if (!overlay || overlay.classList.contains("hidden")) return;
    overlay.classList.add("fading");
    setTimeout(() => overlay.classList.add("hidden"), 460);
}

// On cache hit the overlay is unnecessary - suppress it immediately, unless we're
// still waiting (up to 1s) on a GPS fix for the initial centre.
function _suppressPinOverlay(): void {
    if (!_geoReady) {
        _geoReadyWaiters.push(_suppressPinOverlay);
        return;
    }
    const overlay = document.getElementById("pin-load-overlay");
    if (overlay) overlay.classList.add("hidden");
}

function updatePinCounter(): void {
    const counter = document.getElementById("pin-counter");
    if (!counter) return;
    if (_filterMode && _SHOW_FILTERED_PIN_COUNT) {
        // VIP users see the count of currently visible filtered pins
        const n = clusterGroup.getLayers().length;
        counter.style.display = "";
        document.getElementById("pin-counter-value")!.textContent = String(n);
        document.getElementById("pin-counter-label")!.textContent = n === 1 ? "pin shown" : "pins shown";
    } else if (_SHOW_PIN_COUNT) {
        // Admin users see the total pin count always
        counter.style.display = "";
        const count = _fetchedTiles.has("*") ? _pinStore.size : _totalPins;
        document.getElementById("pin-counter-value")!.textContent = String(count);
        document.getElementById("pin-counter-label")!.textContent = count === 1 ? "pin" : "pins";
    } else {
        counter.style.display = "none";
    }
}
window.updatePinCounter = updatePinCounter;

// -- Tile math -------------------------------------------------------------
function _tileKey(lat: number, lng: number): string {
    return `${(Math.floor(lat / _TILE_SIZE) * _TILE_SIZE).toFixed(1)},${(Math.floor(lng / _TILE_SIZE) * _TILE_SIZE).toFixed(1)}`;
}
function _tilesInBbox(s: number, w: number, n: number, e: number): string[] {
    const keys: string[] = [];
    for (let lat = Math.floor(s / _TILE_SIZE) * _TILE_SIZE; lat < n + 1e-9; lat += _TILE_SIZE) {
        for (let lng = Math.floor(w / _TILE_SIZE) * _TILE_SIZE; lng < e + 1e-9; lng += _TILE_SIZE) {
            keys.push(_tileKey(lat, lng));
        }
    }
    return keys;
}
function _newTilesForBounds(bounds: L.LatLngBounds): string[] {
    const s = Math.max(-90, bounds.getSouth() - _TILE_PAD);
    const w = Math.max(-180, bounds.getWest() - _TILE_PAD);
    const n = Math.min(90, bounds.getNorth() + _TILE_PAD);
    const e = Math.min(180, bounds.getEast() + _TILE_PAD);
    const lats = Math.ceil((n - s) / _TILE_SIZE);
    const lngs = Math.ceil((e - w) / _TILE_SIZE);
    if (lats * lngs > _MAX_TILES) {
        // Viewport covers huge area - request everything once
        return _fetchedTiles.has("*") ? [] : ["*"];
    }
    return _tilesInBbox(s, w, n, e).filter((k) => !_fetchedTiles.has(k));
}
function _bboxFromKeys(keys: string[]): { s: number; w: number; n: number; e: number } {
    let s = Infinity,
        w = Infinity,
        n = -Infinity,
        e = -Infinity;
    for (const key of keys) {
        const parts = key.split(",").map(Number);
        const lat = parts[0]!;
        const lng = parts[1]!;
        if (lat < s) s = lat;
        if (lng < w) w = lng;
        if (lat + _TILE_SIZE > n) n = lat + _TILE_SIZE;
        if (lng + _TILE_SIZE > e) e = lng + _TILE_SIZE;
    }
    return { s, w, n, e };
}
// _newTilesForBounds is part of this page's viewport-tiling design, kept
// alongside its sibling helpers even though the "fetch everything once" path
// (see _loadViewport) is what's actually wired up today.
void _newTilesForBounds;

// -- LocalStorage cache ----------------------------------------------------
// Only the fields needed for marker rendering and popup display are cached.
// Dropping address/city/state/country/priority/profile/statuses cuts the
// stored payload by ~40%, keeping it under the 5 MB quota.
const _CACHE_FIELDS = new Set([
    "uuid",
    "id",
    "name",
    "icon",
    "color",
    "own_icon",
    "own_custom_icon_url",
    "own_color",
    "latitude",
    "longitude",
    "description",
    "last_visited",
    "rating",
    "label_ids",
    "viewLocationUrl",
    "address",
    "cover_photo_url",
]);
function _slimPin(pin: PinData): Partial<PinData> {
    const slim: Partial<PinData> = {};
    for (const k of _CACHE_FIELDS) {
        if (k in pin) (slim as Record<string, unknown>)[k] = pin[k];
    }
    return slim;
}

interface CachedPinBlob {
    v: number;
    ts: number;
    profileUuid: string;
    appUuid: string;
    lastUpdated: string | null;
    tiles: string[];
    labels: LabelDict;
    pins: Record<string, PinData>;
}

function _readCache(): CachedPinBlob | null {
    if (!_USE_CACHE) return null;
    try {
        const raw = localStorage.getItem(_CACHE_KEY);
        if (!raw) return null;
        const c = JSON.parse(raw) as CachedPinBlob;
        // Explicitly remove (not just ignore) an outdated/foreign-profile blob -
        // otherwise it sits under this same key forever, re-parsed and rejected
        // on every load, until a fresh _writeCache() happens to overwrite it
        // (which isn't guaranteed - e.g. if the next fetch fails, or the tab
        // closes before a scheduled write fires).
        if (c.v !== PIN_CACHE_VERSION || c.profileUuid !== _PROFILE_UUID) {
            localStorage.removeItem(_CACHE_KEY);
            return null;
        }
        if (Date.now() - c.ts > _CACHE_MAX) {
            localStorage.removeItem(_CACHE_KEY);
            return null;
        }
        // App UUID mismatch = DB was wiped or app was redeployed with a fresh database.
        // Clear cache so ghost pins from the old DB don't appear on the map.
        if (_APP_UUID && c.appUuid && c.appUuid !== _APP_UUID) {
            console.log("[UL] App UUID mismatch - clearing stale cache from previous deployment");
            localStorage.removeItem(_CACHE_KEY);
            return null;
        }
        return c;
    } catch {
        return null;
    }
}

interface QuotaLikeError {
    name?: string;
    code?: number;
}

function _isQuotaError(err: unknown): boolean {
    // Firefox uses its own legacy name; some engines only set the numeric code.
    const e = err as QuotaLikeError | null;
    return !!e && (e.name === "QuotaExceededError" || e.name === "NS_ERROR_DOM_QUOTA_REACHED" || e.code === 22);
}

function _updateCacheStatus(ts: number): void {
    const el = document.getElementById("cache-status");
    if (!el || !ts) return;
    const d = new Date(ts);
    const str = d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    el.textContent = `cache: ${str}`;
    console.log(`[UrbanLens] Pin cache last refreshed: ${d.toLocaleString()}`);
}

function _writeCache(): void {
    if (!_USE_CACHE) return;
    if (!_pinStore.size) return; // nothing to cache yet
    const ts = Date.now();
    const payload = JSON.stringify({
        v: PIN_CACHE_VERSION,
        ts,
        profileUuid: _PROFILE_UUID,
        appUuid: _APP_UUID,
        lastUpdated: _lastKnownUpdated,
        tiles: [..._fetchedTiles],
        labels: _labelDict,
        pins: Object.fromEntries([..._pinStore.entries()].map(([k, v]) => [k, _slimPin(v)])),
    });
    try {
        localStorage.setItem(_CACHE_KEY, payload);
    } catch (err) {
        if (!_isQuotaError(err)) {
            const e = err as QuotaLikeError | null;
            console.warn("[UL] Cache write failed (%s) - pins will reload next visit", (e && e.name) || err);
            try {
                localStorage.removeItem(_CACHE_KEY);
            } catch {
                /* nothing more we can do */
            }
            return;
        }
        // Out of quota: drop this profile's previous blob and any orphans, then
        // try exactly once more. Without the retry the session ends uncached even
        // though the removals just freed the space the write needed.
        try {
            localStorage.removeItem(_CACHE_KEY);
        } catch {
            /* best effort */
        }
        _purgeOrphanCaches();
        try {
            localStorage.setItem(_CACHE_KEY, payload);
        } catch (retryErr) {
            const re = retryErr as QuotaLikeError | null;
            console.warn("[UL] Cache too large for localStorage (%s) - pins will reload next visit", (re && re.name) || retryErr);
            try {
                localStorage.removeItem(_CACHE_KEY);
            } catch {
                /* best effort */
            }
            const el = document.getElementById("cache-status");
            if (el) el.textContent = "cache: too large";
            return;
        }
    }
    _updateCacheStatus(ts);
    console.log("[UL] Cache written - %d pins, %dKB", _pinStore.size, Math.round(payload.length / 1024));
}
function _scheduleCache(): void {
    if (!_USE_CACHE) return;
    if (_cacheTid) clearTimeout(_cacheTid);
    _cacheTid = setTimeout(_writeCache, 1500);
}
// Flush the cache when the user navigates away so it's always saved
window.addEventListener("beforeunload", _writeCache);

// Delegates to shared/pin-cache.ts, imported directly now that this is a
// module rather than a classic script that had to reach it via a
// window-installed global. Reclaiming space is an optimization, never a
// correctness requirement, so failures are swallowed either way.
function _purgeOrphanCaches(): number {
    try {
        const removed = purgeForeignPinCaches(_CACHE_KEY);
        if (removed) console.log("[UL] Reclaimed %d orphaned pin cache blob(s)", removed);
        return removed;
    } catch {
        return 0;
    }
}
// Reclaim every pin-cache blob that is not this profile's current one: retired
// versions, pre-v5 PK-shaped keys, and other accounts' blobs on a shared
// browser. Nothing else ever reads those, so _readCache's expiry can't free
// them, and a stale multi-MB orphan is what pushes a write over the ~5 MB
// origin quota (see UL-355 - clearing site data by hand "fixed" that report).
_purgeOrphanCaches();

// Call after any pin mutation (add/delete) to force a fresh fetch next time.
// Always clears any existing localStorage entry regardless of _USE_CACHE,
// so disabling and re-enabling caching starts fresh.
window.invalidatePinCache = function (): void {
    try {
        localStorage.removeItem(_CACHE_KEY);
    } catch {
        /* best effort */
    }
    _lastKnownUpdated = null;
};

// Dev toolbar: wipe localStorage + in-memory pin state, then reload from server.
window.forceRefreshPinCache = async function (): Promise<number> {
    console.log("[UL] Force clearing pin cache and refreshing from server");
    window.invalidatePinCache!();
    if (_PROFILE_UUID)
        try {
            localStorage.removeItem(`ul_pins_v4_${_PROFILE_UUID}`);
        } catch {
            /* best effort */
        }
    _pinStore.clear();
    _fetchedTiles.clear();
    clusterGroup.clearLayers();
    _markerMap.clear();
    if (_filterMode) _exitFilterMode();
    _totalPins = 0;
    updatePinCounter();
    const csi = document.getElementById("cache-status");
    if (csi) csi.textContent = "cache: refreshing...";
    await _refreshAllPins();
    await _pollForUpdates();
    if (_cacheTid) {
        clearTimeout(_cacheTid);
        _cacheTid = null;
    }
    _writeCache();
    return _pinStore.size;
};

// Update a single pin in the local store + map without touching any other pin.
// Call this after editing a pin on the detail page so the map marker refreshes
// immediately without a full server round-trip for all pins.
window.updateCachedPin = function (pinData: PinData): void {
    if (!pinData || !pinData.uuid) return;
    const uuid = pinData.uuid;
    _pinStore.set(uuid, pinData);
    if (_filterMode) {
        // The visible marker set in filter mode comes from server-side filter
        // criteria, not _pinStore - re-run the active filter so the map reflects
        // whether the edited pin still matches it, instead of just removing the
        // marker and leaving it gone until filters are cleared or the page reloads.
        htmx.trigger(document.getElementById("filter-form")!, "change");
    } else {
        if (_markerMap.has(uuid)) {
            clusterGroup.removeLayer(_markerMap.get(uuid)!);
            _markerMap.delete(uuid);
        }
        const m = _buildMarker(pinData);
        if (m) {
            _markerMap.set(uuid, m);
            clusterGroup.addLayer(m);
        }
    }
    _totalPins = _pinStore.size;
    updatePinCounter();
    _scheduleCache();
    _refreshPinList();
};

// Look up a pin already in the local store within threshold_meters of a
// coordinate, using a stricter threshold than Pin.objects.get_nearby_or_create's default
// 50m "same location" threshold server-side. Used by the import wizard
// to pre-flag rows that would just come back as "existed" anyway, without
// needing a round trip. Only ever populated/accurate on this page (the
// main map) - other pages that reuse the import dialog (e.g. Memories)
// simply won't find this global and skip the pre-flagging step.
window.findLocalPinNear = function (lat: number, lng: number, thresholdMeters = 5): PinData | null {
    lat = Number(lat);
    lng = Number(lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    const R = 6371000; // Earth radius in meters
    const rad = Math.PI / 180;
    for (const pin of _pinStore.values()) {
        const plat = Number(pin.latitude),
            plng = Number(pin.longitude);
        if (!Number.isFinite(plat) || !Number.isFinite(plng)) continue;
        // Cheap bounding-box reject before the trig-heavy haversine calc.
        if (Math.abs(plat - lat) > 0.01 || Math.abs(plng - lng) > 0.01) continue;
        const dLat = (plat - lat) * rad;
        const dLng = (plng - lng) * rad;
        const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat * rad) * Math.cos(plat * rad) * Math.sin(dLng / 2) ** 2;
        const dist = 2 * R * Math.asin(Math.sqrt(a));
        if (dist <= thresholdMeters) return pin;
    }
    return null;
};

// -- Background pin refresh (non-disruptive) -------------------------------
// Fetches all pins, then adds new ones, updates changed markers, removes
// deleted ones - without altering map position or zoom.
async function _refreshAllPins(): Promise<void> {
    console.log("[UL] Pin data changed on server - running full background refresh");
    const csi = document.getElementById("cache-status");
    if (csi) csi.classList.add("refreshing");
    _setFetching(true, "Refreshing pins...");
    try {
        const data = await _fetchEveryPin("Refreshing pins...");

        if (Number.isFinite(data.total)) _totalPins = data.total as number;
        const freshUuids = new Set((data.pins || []).map((p) => p.uuid));
        const batch: L.Marker[] = [];

        for (const pin of data.pins || []) {
            const existing = _pinStore.get(pin.uuid);
            _pinStore.set(pin.uuid, pin);
            if (!_filterMode) {
                if (!existing) {
                    const m = _buildMarker(pin);
                    if (m) {
                        _markerMap.set(pin.uuid, m);
                        batch.push(m);
                    }
                } else {
                    const changed =
                        existing.icon !== pin.icon ||
                        existing.color !== pin.color ||
                        _labelsDiffer(existing, pin) ||
                        existing.rating !== pin.rating ||
                        existing.name !== pin.name ||
                        existing.latitude !== pin.latitude ||
                        existing.longitude !== pin.longitude;
                    if (changed && _markerMap.has(pin.uuid)) {
                        clusterGroup.removeLayer(_markerMap.get(pin.uuid)!);
                        _markerMap.delete(pin.uuid);
                        const m = _buildMarker(pin);
                        if (m) {
                            _markerMap.set(pin.uuid, m);
                            batch.push(m);
                        }
                    }
                }
            }
        }

        // Remove markers for pins that no longer exist server-side
        for (const uuid of [..._pinStore.keys()]) {
            if (!freshUuids.has(uuid)) {
                _pinStore.delete(uuid);
                if (_markerMap.has(uuid)) {
                    clusterGroup.removeLayer(_markerMap.get(uuid)!);
                    _markerMap.delete(uuid);
                }
            }
        }

        if (batch.length) (clusterGroup as unknown as { addLayers: (layers: L.Marker[]) => void }).addLayers(batch);
        // Every pin has now been compared against them, so a label edit does
        // not go on rebuilding its markers on every later refresh.
        _changedLabelIds.clear();
        _fetchedTiles.clear();
        _fetchedTiles.add("*");
        _totalPins = _pinStore.size;
        updatePinCounter();
        _scheduleCache();
        console.log("[UL] Full refresh complete - %d pins total", _pinStore.size);
    } catch (err) {
        console.warn("[UrbanLens] Background pin refresh failed:", err);
    } finally {
        _setFetching(false);
        if (csi) csi.classList.remove("refreshing");
    }
}
window._refreshAllPins = _refreshAllPins;

// -- Server connectivity ---------------------------------------------------
function _setOffline(offline: boolean): void {
    if (_isOffline === offline) return;
    _isOffline = offline;
    const el = document.getElementById("offline-indicator");
    if (el) el.hidden = !offline;
    if (offline) console.warn("[UL] Server appears to be offline");
    else console.log("[UL] Server connectivity restored");
}

// -- Temporary place markers (search jump + right-click context menu) -----
// isSearch=true uses amber styling so search markers are visually distinct from
// permanent pins and from the blue context-menu marker. Both variants get a
// pulsing halo at the anchor point so the temporary marker is unmissable at
// any zoom level and on any tile style.
function _createTempPlaceMarker(lat: number, lng: number, title = "", isSearch = false): L.Marker {
    const modifier = isSearch ? " search-result-marker-icon--search" : "";
    const haloModifier = isSearch ? " search-result-marker-halo--search" : "";
    return L.marker([lat, lng], {
        icon: L.divIcon({
            className: "search-result-marker-wrap search-result-marker-wrap--temp",
            html: `<span class="search-result-marker-halo${haloModifier}"></span><i class="material-icons search-result-marker-icon${modifier}">place</i>`,
            iconSize: [44, 52],
            iconAnchor: [22, 48],
        }),
        zIndexOffset: 2000,
        title,
    });
}

// -- "You are here" marker --------------------------------------------------
// Unlike _searchMarker (a temp place-pin cleared on the next search), this
// persists across pans/searches once a GPS fix has been shown - it's
// updated in place rather than recreated, so repeated fixes (initial load,
// background refresh, "jump to my location") just relocate the same dot.
function _showUserLocationMarker(lat: number, lng: number): void {
    if (_userLocationMarker) {
        _userLocationMarker.setLatLng([lat, lng]);
        return;
    }
    _userLocationMarker = L.marker([lat, lng], {
        icon: L.divIcon({
            className: "user-location-marker-wrap",
            html: '<span class="user-location-marker-halo"></span><span class="user-location-marker-dot"></span>',
            iconSize: [22, 22],
            iconAnchor: [11, 11],
        }),
        zIndexOffset: 3000,
        title: "Your location",
    }).bindPopup("Your approximate location");
    _userLocationMarker.addTo(map);
}

function _clearContextMenuMarker(): void {
    if (!_contextMenuMarker) return;
    map.removeLayer(_contextMenuMarker);
    _contextMenuMarker = null;
}

function _showContextMenuMarker(lat: number, lng: number): void {
    _clearContextMenuMarker();
    _clearSearchMarker();
    _contextMenuMarker = _createTempPlaceMarker(lat, lng);
    _contextMenuMarker.addTo(map);
}

// Context menus are position:fixed, so they take viewport coordinates and
// have to be kept inside it - a long-press near the right or bottom edge
// opens the menu where nothing can scroll it back into view otherwise.
function _placeFloatingMenu(menu: HTMLElement, clientX: number, clientY: number): void {
    MapContextMenu.place(menu, clientX, clientY);
}

// Marker menus open at the point that was clicked, in viewport coordinates;
// a marker reached by keyboard reports none, so fall back to where the
// marker itself sits on screen.
function _markerMenuAnchor(e: L.LeafletMouseEvent | undefined, lat: number, lng: number): { x: number; y: number } {
    const src = e && e.originalEvent;
    if (src && Number.isFinite(src.clientX) && Number.isFinite(src.clientY)) {
        return { x: src.clientX, y: src.clientY };
    }
    const rect = map.getContainer().getBoundingClientRect();
    const pt = map.latLngToContainerPoint([lat, lng]);
    return { x: rect.left + pt.x, y: rect.top + pt.y };
}

function _clearSearchMarker(): void {
    if (!_searchMarker) return;
    map.removeLayer(_searchMarker);
    _searchMarker = null;
}

function _showSearchMarker(lat: number, lng: number, title?: string): void {
    _clearSearchMarker();
    _clearContextMenuMarker();
    _searchMarker = _createTempPlaceMarker(lat, lng, title || "", true /* isSearch */);
    _searchMarker.on("click", function (e) {
        map.closePopup();
        const anchor = _markerMenuAnchor(e, lat, lng);
        MapContextMenu.show({
            lat: lat,
            lng: lng,
            zoom: map.getZoom(),
            clientX: anchor.x,
            clientY: anchor.y,
            extraItems: [
                {
                    icon: "add_location",
                    label: "Create pin",
                    onClick: function () {
                        _clearSearchMarker();
                        openAddPinDialog(lat, lng, { defaultName: title || "" });
                    },
                },
                {
                    icon: "close",
                    label: "Dismiss marker",
                    onClick: function () {
                        _clearSearchMarker();
                    },
                },
            ],
        });
    });
    _searchMarker.addTo(map);
}

// -- Background polling ----------------------------------------------------
// Checks whether any pin has been updated since the cached timestamp.
// Also detects mid-session DB wipes via app_uuid mismatch.
// Only triggers a full refresh when the server signals a change.
async function _pollForUpdates(): Promise<void> {
    try {
        if (localStorage.getItem("ul_pins_dirty") === "1") {
            localStorage.removeItem("ul_pins_dirty");
            console.log("[UL] Pin cache flagged dirty by another tab/page - forcing refresh");
            window.invalidatePinCache!();
            await _refreshAllPins();
            return;
        }
        const resp = await fetch(MAP_CFG.urls.mapPinsMeta, { headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (!resp.ok) {
            _setOffline(true);
            return;
        }
        _setOffline(false);
        const data = (await resp.json()) as { app_uuid?: string; fingerprint?: string; last_updated?: string };
        // App UUID changed mid-session (DB wiped while user had the map open).
        if (_APP_UUID && data.app_uuid && data.app_uuid !== _APP_UUID) {
            console.log("[UL] App UUID changed mid-session - clearing cache and reloading pins");
            window.invalidatePinCache!();
            await _refreshAllPins();
            return;
        }
        // fingerprint, not last_updated: deleting any pin other than the most
        // recently updated one leaves the timestamp exactly where it was, so a
        // pin deleted in another tab used to stay on this map until the cache
        // expired. Falls back for a response from an older server.
        const stamp = data.fingerprint || data.last_updated;
        if (stamp) {
            if (_lastKnownUpdated !== null && stamp !== _lastKnownUpdated) {
                _lastKnownUpdated = stamp;
                await _refreshAllPins();
            } else {
                _lastKnownUpdated = stamp;
            }
        }
    } catch {
        _setOffline(true);
    }
}

// -- Marker builder --------------------------------------------------------
// Merge a response's label dictionary in rather than replacing it: a paged
// fetch sends one per page, and the quick-edit endpoints send none at all.
// Records which entries actually moved, because a renamed or recoloured
// label changes what a pin draws without changing anything in the pin.
function _mergeLabels(labels: LabelDict | null | undefined): void {
    if (!labels || typeof labels !== "object") return;
    const merged: LabelDict = Object.assign({}, _labelDict);
    for (const [id, entry] of Object.entries(labels)) {
        const before = merged[id];
        if (!before || JSON.stringify(before) !== JSON.stringify(entry)) _changedLabelIds.add(String(id));
        merged[id] = entry;
    }
    _labelDict = merged;
}

// Whether this pin draws differently than the store's copy of it does,
// given what the dictionary just learned.
function _labelsDiffer(before: PinData | undefined, after: PinData | undefined): boolean {
    const ids = Array.isArray(after?.label_ids) ? after.label_ids : [];
    const previous = Array.isArray(before?.label_ids) ? before.label_ids : [];
    if (ids.join(",") !== previous.join(",")) return true;
    return ids.some((id) => _changedLabelIds.has(String(id)));
}

// A pin's labels as objects, whatever wrote the entry. The map endpoints
// send `label_ids` and the dictionary to resolve them against. `tags` is
// still what the quick-edit and pin-list endpoints return, `tags_data` was
// the filter panel's separate shape, and a comma-joined string was the same
// paths' `tags` before they were unified.
function _pinTagObjects(pin: PinData): PinTagLike[] {
    if (Array.isArray(pin.label_ids)) {
        return pin.label_ids.map((id) => _labelDict[String(id)]).filter((x): x is LabelDictEntry => Boolean(x));
    }
    if (Array.isArray(pin.tags) && pin.tags.length) {
        return pin.tags.map((t) => (typeof t === "object" && t !== null ? t : { name: String(t) }));
    }
    if (Array.isArray(pin.tags_data) && pin.tags_data.length) return pin.tags_data;
    return String(pin.tags || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
        .map((n) => ({ name: n }));
}

function _normalizeHexColor(hex: unknown): string {
    let h = String(hex).replace("#", "").toLowerCase();
    if (h.length === 3)
        h = h
            .split("")
            .map((c) => c + c)
            .join("");
    return h;
}

function _hexToRgba(hex: unknown, alpha: number): string {
    const h = _normalizeHexColor(hex);
    const r = Number.parseInt(h.slice(0, 2), 16);
    const g = Number.parseInt(h.slice(2, 4), 16);
    const b = Number.parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${alpha})`;
}

// Mirrors shared/color-safety.ts's safeColor, which this file cannot import.
// The colour is interpolated into style="..." strings handed to innerHTML, so
// a stored value that is not a colour is an attribute breakout - and rows
// predating the column's own coercion are still served. Validated rather
// than escaped: escaping stops the breakout and still permits url(...) and
// friends inside the style value.
const _UL_HEX_COLOR = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

function _safePinColor(value: unknown): string | null {
    return value && _UL_HEX_COLOR.test(String(value)) ? String(value) : null;
}

function _resolvePinColor(pin: PinData): string | null {
    // Server sends icon-only color in pin.color; do not fall back to other tags.
    return _safePinColor(pin.color);
}

// "2026-09-17T00:00:00+00:00" -> "Sep 17, 2026" in the viewer's own locale.
// Absolute rather than relative ("3 days ago"): this can sit in the client
// pin cache for a while, and a relative phrase read days after it was
// fetched would go quietly wrong in a way an absolute date does not.
function _humanizeVisitedDate(iso: string): string {
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return iso;
    return parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function _buildMarker(pin: PinData): L.Marker | null {
    if (!pin.latitude || !pin.longitude) return null;
    const coords: [number, number] = [Number.parseFloat(String(pin.latitude)), Number.parseFloat(String(pin.longitude))];
    const marker = L.marker(coords, { draggable: !_selectMode });

    if (pin.icon && pin.icon !== "undefined" && pin.icon !== "null") {
        const resolvedColor = _resolvePinColor(pin);
        const hasColor = !!resolvedColor;
        const color = hasColor ? resolvedColor! : "#555555";
        let iconHtml: string;
        if (/^[a-z_]+$/.test(pin.icon)) {
            const iconColorStyle = hasColor ? "" : ` style="color:${color};"`;
            iconHtml = `<i class="material-icons map-pin-icon"${iconColorStyle}>${pin.icon}</i>`;
        } else if (/^(https?:\/\/|\/)/.test(pin.icon)) {
            iconHtml = `<img src="${_ulEscAttr(pin.icon)}" class="map-pin-custom-img" alt="">`;
        } else {
            iconHtml = `<span class="map-pin-emoji">${pin.icon}</span>`;
        }
        if (hasColor) {
            const circleColorClass = `map-pin-color-circle--${_normalizeHexColor(color)}`;
            iconHtml = `<span class="map-pin-with-circle"><span class="map-pin-color-circle ${circleColorClass}" style="background:${_hexToRgba(color, 0.8)};"></span><span class="map-pin-icon-layer">${iconHtml}</span></span>`;
        }
        const iconSize: [number, number] = hasColor ? [36, 36] : [28, 28];
        const iconAnchor: [number, number] = hasColor ? [18, 18] : [14, 14];
        marker.setIcon(L.divIcon({ className: "map-pin-icon-wrap", html: iconHtml, iconSize, iconAnchor }));
    }

    const tagChipsData = _pinTagObjects(pin);
    const tagChips = tagChipsData
        .map((t) => {
            const bg = t.color ? `${t.color}22` : "rgba(100,120,160,0.18)";
            const border = t.color ? `${t.color}55` : "rgba(100,120,160,0.3)";
            let icon = "";
            if (t.icon) {
                if (/^https?:\/\/|^\//.test(t.icon)) {
                    icon = `<img src="${_escHtml(t.icon)}" alt="" style="width:14px;height:14px;object-fit:contain;vertical-align:middle;margin-right:3px;border-radius:2px;">`;
                } else {
                    icon = `<span style="font-size:.85em;vertical-align:middle;margin-right:2px;">${_escHtml(t.icon)}</span>`;
                }
            }
            return `<span class="popup-tag-chip" style="background:${bg};border-color:${border};">${icon}${_escHtml(t.name)}</span>`;
        })
        .join("");

    const rating = Number.parseInt(String(pin.rating)) || 0;
    const starDisplay = [1, 2, 3, 4, 5].map((i) => `<span class="popup-star ${i <= rating ? "popup-star--on" : ""}" data-val="${i}">★</span>`).join("");
    // Hidden radio inputs for save-on-click (popupopen handler uses these)
    const starInputs = [1, 2, 3, 4, 5]
        .map((i) => `<input type="radio" name="star" class="star-${i}" id="star-${i}-${pin.uuid}" value="${i}" ${rating === i ? "checked" : ""} style="display:none">`)
        .join("");

    const popupContent = `
            <div class="pin-popup" data-uuid="${pin.uuid}" data-id="${pin.id || ""}">
                ${pin.cover_photo_url ? `<img class="popup-thumb" src="${_escHtml(pin.cover_photo_url)}" alt="" loading="lazy">` : ""}
                <a class="popup-title" href="${_escHtml(pin.viewLocationUrl ?? "")}" title="Open this pin's details">${_escHtml(pin.name || "")}</a>
                ${pin.address ? `<div class="popup-address"><i class="material-icons" style="font-size:.7rem;vertical-align:middle;opacity:.6;">location_on</i> ${_escHtml(pin.address)}</div>` : ""}
                ${pin.description ? `<div class="popup-desc">${_escHtml(pin.description)}</div>` : ""}
                ${
                    pin.last_visited && pin.last_visited !== "never"
                        ? `
                <div class="popup-meta">
                    <span class="popup-visited"><i class="material-icons" style="font-size:.75rem;vertical-align:middle;">schedule</i> ${_escHtml(_humanizeVisitedDate(pin.last_visited))}</span>
                </div>`
                        : ""
                }
                <div class="popup-stars" data-pin-id="${pin.id || ""}" data-pin-uuid="${pin.uuid}">
                    ${starDisplay}
                    <div class="stars rating-${rating}" style="display:none">${starInputs}</div>
                </div>
                ${tagChips ? `<div class="popup-tags">${tagChips}</div>` : ""}
                <div class="popup-actions">
                    <div class="popup-menu">
                        <button type="button" class="popup-menu-toggle" aria-haspopup="true" aria-expanded="false"
                                title="More actions" aria-label="More actions for this pin"
                                onclick="togglePinPopupMenu(this)"><i class="material-symbols-outlined">more_vert</i></button>
                        <div class="popup-menu-items" role="menu" hidden>
                            ${pin.id ? `<button type="button" role="menuitem" class="add-to-list-button" onclick="event.stopPropagation(); closePinPopupMenus(); openAddToListDialog(${pin.id})"><i class="material-symbols-outlined">playlist_add</i> Add to list</button>` : ""}
                            ${pin.child_count && pin.child_count > 0 ? `<button type="button" role="menuitem" class="promote-children-button" onclick="event.stopPropagation(); closePinPopupMenus(); promoteChildPins('${pin.slug || pin.uuid}', ${pin.child_count})"><i class="material-symbols-outlined">move_up</i> Promote child pins</button>` : ""}
                            <button type="button" role="menuitem" class="edit-pin-button" onclick="closePinPopupMenus(); openEditPinDialog('${pin.uuid}')"><i class="material-symbols-outlined">edit</i> Edit pin</button>
                            <button type="button" role="menuitem" class="delete-button" onclick="deletePin(this)"><i class="material-symbols-outlined">delete</i> Delete pin</button>
                        </div>
                    </div>
                </div>
            </div>`;
    marker.bindPopup(popupContent);

    // Select-mode click toggles selection instead of opening the popup. The
    // popup already opened by the time this runs (Leaflet's own popup-click
    // handler fires first), so close it back out immediately. Ctrl/cmd-click
    // on a second pin enters select mode with both selected.
    marker.on("click", function (e) {
        _handleSelectablePinClick(e, pin.uuid, marker);
    });

    // Right-clicking an existing pin should behave exactly like left-
    // clicking it (open its popup), not fall through to the map's own
    // contextmenu handler (the blank-area "Add Pin Here" menu) - stopping
    // the event here keeps it from bubbling up to that handler.
    marker.on("contextmenu", function (e) {
        L.DomEvent.stop(e);
        if (_selectMode) {
            _togglePinSelection(pin.uuid);
            return;
        }
        marker.openPopup();
    });

    // Merge dialog map->list hover sync (see "-- Merge --" below) - a
    // no-op whenever the dialog isn't open, so this costs nothing on the
    // vast majority of markers/hovers.
    marker.on("mouseover", function () {
        if (_mergeDialogIsOpen()) _mergeHoverMiniCard(pin.uuid, true);
    });
    marker.on("mouseout", function () {
        if (_mergeDialogIsOpen()) _mergeHoverMiniCard(pin.uuid, false);
    });

    if (pin.id) {
        let _savedLat = Number.parseFloat(String(pin.latitude));
        let _savedLng = Number.parseFloat(String(pin.longitude));

        // Return the marker to the cluster group, wherever it currently sits.
        const _reclusterMarker = function (): void {
            if (map.hasLayer(marker)) map.removeLayer(marker);
            clusterGroup.addLayer(marker);
        };

        // Drag freely; the position is only committed once the user confirms on drop.
        marker.on("dragstart", function () {
            if (_selectMode) return;
            clusterGroup.removeLayer(marker);
            marker.addTo(map);
        });
        marker.on("dragend", function () {
            const pos = marker.getLatLng();
            // Ignore an incidental click-drag that left the pin where it started.
            if (pos.lat.toFixed(6) === _savedLat.toFixed(6) && pos.lng.toFixed(6) === _savedLng.toFixed(6)) {
                _reclusterMarker();
                return;
            }
            void confirmAction({
                title: "Move pin here?",
                message: "Save this pin at its new location?",
                confirmLabel: "Move pin",
            }).then(function (ok) {
                if (!ok) {
                    marker.setLatLng([_savedLat, _savedLng]);
                    _reclusterMarker();
                    return;
                }
                fetch(`/dashboard/rest/pins/${pin.uuid}/`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": MAP_CFG.csrfToken },
                    body: JSON.stringify({ latitude: pos.lat.toFixed(6), longitude: pos.lng.toFixed(6) }),
                })
                    .then((r) => {
                        if (!r.ok) throw new Error();
                        return r.json();
                    })
                    .then(() => {
                        _savedLat = pos.lat;
                        _savedLng = pos.lng;
                        if (_pinStore.has(pin.uuid)) {
                            const cached = _pinStore.get(pin.uuid)!;
                            cached.latitude = pos.lat.toFixed(6);
                            cached.longitude = pos.lng.toFixed(6);
                            _scheduleCache();
                        }
                        toastr.success("Pin moved.");
                    })
                    .catch(() => {
                        toastr.error("Failed to save new position.");
                        marker.setLatLng([_savedLat, _savedLng]);
                    })
                    .finally(_reclusterMarker);
            });
        });
    }

    return marker;
}

function addMarker(pin: PinData): void {
    if (!pin.latitude || !pin.longitude || _markerMap.has(pin.uuid)) return;
    const m = _buildMarker(pin);
    if (!m) return;
    _markerMap.set(pin.uuid, m);
    clusterGroup.addLayer(m);
}
window.addMarker = addMarker;

// -- Gentle onboarding ---------------------------------------------------
// Small, dismissible nudges teach powerful map actions in context without
// blocking map use. Dismissals are stored per profile so users stay in control.
const _ONBOARDING_PREFIX = `ul_onboarding_v1_${_PROFILE_UUID}`;
const _ONBOARDING_SESSION_KEY = `${_ONBOARDING_PREFIX}_later`;
// Declared (as null) here rather than where it's actually built further down
// this script (MapLayers.create(...)), so the 'map-layers' card's
// shouldShow()/action() below - which read it via closure - see `null`
// instead of hitting the temporal dead zone. A `let` declared later would
// throw "Cannot access '_mapLayers' before initialization" if the onboarding
// trigger ever ran before that later line executes (e.g. if something else
// earlier in this very long script throws first and the assignment is never
// reached) - null is a safe, checkable placeholder instead of a crash.
let _mapLayers: MapLayersInstance | null = null;

interface OnboardingCard {
    id: string;
    icon: string;
    eyebrow: string;
    title: string;
    body: string;
    button: string;
    target: string;
    shouldShow: () => boolean;
    watchSelector: string;
    action: () => void;
}

const _onboardingCards: OnboardingCard[] = [
    {
        id: "import-pins",
        icon: "upload",
        eyebrow: "Fast start",
        title: "Bring your saved places into UrbanLens",
        body: "Import pins from another app so your map feels useful immediately. Support for Google, Multiplottr, and more.",
        button: "Import pins",
        target: "import-pins-button",
        shouldShow: () => _totalPins < 20,
        watchSelector: "#import-pins-button",
        action: () => document.getElementById("import-pins-button")?.click(),
    },
    {
        id: "add-first-pin",
        icon: "add_location",
        eyebrow: "Build your map",
        title: "Drop a pin anywhere you discover",
        body: "Use Add Pin, or right-click the map, to save a location without leaving the map.",
        button: "Add a pin",
        target: "add-pin-button",
        shouldShow: () => _totalPins < 1,
        watchSelector: "#add-pin-button",
        action: () => openAddPinDialog(),
    },
    {
        id: "search-filters",
        icon: "search",
        eyebrow: "Find things faster",
        title: "Filter by rating, visits, labels, and more",
        body: "Search & Filter keeps your map tidy as your collection grows.",
        button: "Open filters",
        target: "search-pins-button",
        shouldShow: () => _totalPins >= 5,
        watchSelector: "#search-pins-button",
        action: () => {
            if (!document.getElementById("filter-panel")?.classList.contains("open")) toggleFilterPanel();
        },
    },
    {
        id: "map-layers",
        icon: "layers",
        eyebrow: "Map context",
        title: "Switch views when terrain or imagery matters",
        body: "Topographic, satellite, and weather overlays can reveal access, terrain, and conditions.",
        button: "Show satellite",
        target: "satellite-button",
        shouldShow: () => !!_mapLayers && _mapLayers.baseKey() !== "satellite",
        watchSelector: "#satellite-button",
        action: () => {
            if (_mapLayers && _mapLayers.baseKey() !== "satellite") _mapLayers.setBase("satellite");
        },
    },
];

function _onboardingKey(id: string): string {
    return `${_ONBOARDING_PREFIX}_${id}_dismissed`;
}
function _onboardingDismissed(id: string): boolean {
    try {
        return localStorage.getItem(_onboardingKey(id)) === "1";
    } catch {
        return false;
    }
}
function _setOnboardingDismissed(id: string): void {
    try {
        localStorage.setItem(_onboardingKey(id), "1");
    } catch {
        /* best effort */
    }
}
function _setOnboardingLater(): void {
    try {
        sessionStorage.setItem(_ONBOARDING_SESSION_KEY, "1");
    } catch {
        /* best effort */
    }
}
function _onboardingLater(): boolean {
    try {
        return sessionStorage.getItem(_ONBOARDING_SESSION_KEY) === "1";
    } catch {
        return false;
    }
}
function _clearOnboarding(): void {
    document.getElementById("map-onboarding")?.replaceChildren();
    document.querySelectorAll(".map-btn-icon.onboarding-highlight").forEach((el) => el.classList.remove("onboarding-highlight"));
}
function _escapeHtml(value: unknown): string {
    return String(value || "").replace(/[&<>'"]/g, (ch) => (({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }) as Record<string, string>)[ch]!);
}
function _showOnboardingCard(card: OnboardingCard): void {
    const host = document.getElementById("map-onboarding");
    if (!host) return;
    _clearOnboarding();
    const target = document.getElementById(card.target);
    target?.classList.add("onboarding-highlight");
    const el = document.createElement("section");
    el.className = "map-onboarding-card";
    el.dataset.onboardingId = card.id;
    el.innerHTML = `
            <div class="map-onboarding-card__icon"><i class="material-icons">${_escapeHtml(card.icon)}</i></div>
            <div class="map-onboarding-card__content">
                <div class="map-onboarding-card__eyebrow">${_escapeHtml(card.eyebrow)}</div>
                <h2>${_escapeHtml(card.title)}</h2>
                <p>${_escapeHtml(card.body)}</p>
                <div class="map-onboarding-card__actions">
                    <button type="button" class="btn btn--primary map-onboarding-primary">${_escapeHtml(card.button)}</button>
                    <button type="button" class="btn btn--ghost map-onboarding-later">Later</button>
                    <button type="button" class="map-onboarding-dismiss">Don't show again</button>
                </div>
            </div>
            <button type="button" class="map-onboarding-x" aria-label="Close onboarding tip"><i class="material-symbols-outlined">close</i></button>`;
    host.appendChild(el);
    el.querySelector(".map-onboarding-primary")?.addEventListener("click", () => {
        _setOnboardingDismissed(card.id);
        _clearOnboarding();
        card.action();
    });
    el.querySelector(".map-onboarding-later")?.addEventListener("click", () => {
        _setOnboardingLater();
        _clearOnboarding();
    });
    el.querySelector(".map-onboarding-dismiss")?.addEventListener("click", () => {
        _setOnboardingDismissed(card.id);
        _clearOnboarding();
    });
    el.querySelector(".map-onboarding-x")?.addEventListener("click", () => {
        _setOnboardingLater();
        _clearOnboarding();
    });
}
function _registerAutoDissmiss(card: OnboardingCard): void {
    if (_onboardingDismissed(card.id) || !card.watchSelector) return;
    document.querySelectorAll(card.watchSelector).forEach((el) => {
        el.addEventListener("click", () => _setOnboardingDismissed(card.id), { once: true });
    });
}

function initMapOnboarding(): void {
    if (!MAP_CFG.showOnboardingTips) return;
    _onboardingCards.forEach(_registerAutoDissmiss);
    if (_onboardingLater()) return;
    const card = _onboardingCards.find((c) => c.shouldShow() && !_onboardingDismissed(c.id));
    if (card) setTimeout(() => _showOnboardingCard(card), 450);
}

// -- Fetch tiles from server -----------------------------------------------
// Thin shim over shared/fetch-json.ts's fetchJson (imported directly now that
// this is a module), kept so the timeout stays a positional argument at the
// call sites below. The map endpoints below never actually answer 204, so a
// null body is treated as a fetch failure rather than threaded through as an
// optional result everywhere it's used.
async function _fetchJson<T>(url: string, options?: FetchJsonOptions, timeoutMs?: number): Promise<T> {
    const result = await fetchJson<T>(url, { ...(options || {}), timeoutMs: timeoutMs || 120000 });
    if (result === null) throw new Error(`Empty response from ${url}`);
    return result;
}

/** sendJson resolves null only for a 204; every endpoint this page posts to answers with a body. */
async function _sendJson<T>(url: string, method: "POST" | "PUT" | "PATCH" | "DELETE", body?: unknown): Promise<T> {
    const result = await sendJson<T>(url, method, body);
    if (result === null) throw new Error(`Empty response from ${url}`);
    return result;
}

function _setLoadProgress(text: string): void {
    const overlayText = document.getElementById("plo-text");
    if (overlayText && !document.getElementById("pin-load-overlay")?.classList.contains("hidden")) overlayText.textContent = text;
    const tfiText = document.getElementById("tfi-text");
    if (tfiText) tfiText.textContent = text;
}

interface MapDocumentLine {
    t: "head" | "labels" | "pin" | "end";
    mode?: string;
    total?: number;
    labels?: LabelDict;
    p?: PinData;
    sent?: number;
}

// One request instead of twenty. Returns null - not an error - whenever the
// paged path should be used instead: an account over the server's ceiling, a
// browser without streaming, or a document that arrived without its end line.
async function _fetchMapDocument(message?: string): Promise<PinPage | null> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    try {
        const resp = await fetch(MAP_CFG.urls.mapDocument, {
            headers: { "X-Requested-With": "XMLHttpRequest" },
            signal: controller.signal,
        });
        if (!resp.ok || !resp.body || !resp.body.getReader) return null;
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        const pins: PinData[] = [];
        let buffer = "",
            head: MapDocumentLine | null = null,
            ended = false;
        for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let nl: number;
            while ((nl = buffer.indexOf("\n")) >= 0) {
                const line = buffer.slice(0, nl);
                buffer = buffer.slice(nl + 1);
                if (!line) continue;
                let obj: MapDocumentLine;
                try {
                    obj = JSON.parse(line);
                } catch {
                    return null;
                }
                if (obj.t === "head") {
                    head = obj;
                    if (obj.mode !== "document") return null;
                    _mergeLabels(obj.labels);
                } else if (obj.t === "labels") {
                    _mergeLabels(obj.labels);
                } else if (obj.t === "pin") {
                    if (obj.p) pins.push(obj.p);
                    if (pins.length % 500 === 0) {
                        _setLoadProgress(`${message || "Loading pins..."} ${pins.length} / ${head ? head.total : "?"}`);
                        await new Promise((resolve) => setTimeout(resolve, 0));
                    }
                } else if (obj.t === "end") {
                    ended = obj.sent === pins.length;
                }
            }
        }
        // Against the end line's own count, not head.total: a pin created
        // mid-stream makes those differ without the transfer being short, and
        // the next meta poll picks that up. A short read is what this catches.
        if (!head || !ended) return null;
        return { pins, next_cursor: null, total: head.total ?? null };
    } finally {
        clearTimeout(timer);
    }
}

async function _fetchEveryPin(message?: string): Promise<PinPage> {
    try {
        const doc = await _fetchMapDocument(message);
        if (doc) return doc;
    } catch (err) {
        console.warn("[UL] map document unavailable, falling back to paged fetch", err);
    }
    return _fetchAllPinPages(message);
}

interface PinPageResponse {
    pins?: PinData[];
    labels?: LabelDict;
    next_cursor?: string | null;
    total?: number;
}

async function _fetchAllPinPages(message?: string): Promise<PinPage> {
    const pins: PinData[] = [];
    let cursor: string | null = null;
    let total: number | null = null;
    let page = 0;
    do {
        const params = new URLSearchParams({ limit: "500" });
        if (cursor) params.set("cursor", cursor);
        if (page === 0) params.set("include_total", "1");
        const data = await _fetchJson<PinPageResponse>(
            `${MAP_CFG.urls.mapPins}?${params.toString()}`,
            {
                headers: { "X-Requested-With": "XMLHttpRequest" },
            },
            45000,
        );
        pins.push(...(data.pins || []));
        _mergeLabels(data.labels);
        cursor = data.next_cursor || null;
        total = Number.isFinite(data.total) ? (data.total ?? null) : total;
        page += 1;
        _setLoadProgress(total ? `${message || "Loading pins..."} ${Math.min(pins.length, total)} / ${total}` : `${message || "Loading pins..."} ${pins.length}`);
        await new Promise((resolve) => setTimeout(resolve, 0));
    } while (cursor);
    return { pins, next_cursor: null, total };
}

async function _fetchTiles(tileKeys: string[]): Promise<number> {
    if (!tileKeys.length) return 0;
    console.log("[UL] Fetching %d tile(s) from server: %s", tileKeys.length, tileKeys.join(", "));
    tileKeys.forEach((k) => _fetchedTiles.add(k));
    const msg = tileKeys.length === 1 && tileKeys[0] === "*" ? "Loading pins..." : "Loading pins...";
    _setFetching(true, msg);
    try {
        const data: PinPage =
            tileKeys.length === 1 && tileKeys[0] === "*"
                ? await _fetchEveryPin("Loading pins...")
                : await _fetchJson<PinPage>(
                      `${MAP_CFG.urls.mapPins}?bbox=${encodeURIComponent(
                          (() => {
                              const b = _bboxFromKeys(tileKeys);
                              return `${b.s},${b.w},${b.n},${b.e}`;
                          })(),
                      )}`,
                      { headers: { "X-Requested-With": "XMLHttpRequest" } },
                      45000,
                  );
        if (Number.isFinite(data.total)) _totalPins = data.total as number;
        const batch: L.Marker[] = [];
        for (const pin of data.pins || []) {
            const existing = _pinStore.get(pin.uuid);
            if (existing) {
                // Refresh marker when display-relevant fields changed (tag edits, rating, etc.)
                if (existing.icon !== pin.icon || existing.color !== pin.color || _labelsDiffer(existing, pin) || existing.rating !== pin.rating) {
                    _pinStore.set(pin.uuid, pin);
                    if (!_filterMode && _markerMap.has(pin.uuid)) {
                        clusterGroup.removeLayer(_markerMap.get(pin.uuid)!);
                        _markerMap.delete(pin.uuid);
                        const m = _buildMarker(pin);
                        if (m) {
                            _markerMap.set(pin.uuid, m);
                            batch.push(m);
                        }
                    }
                }
                continue;
            }
            _pinStore.set(pin.uuid, pin);
            if (!_filterMode) {
                const m = _buildMarker(pin);
                if (m) {
                    _markerMap.set(pin.uuid, m);
                    batch.push(m);
                }
            }
        }
        if (batch.length) {
            (clusterGroup as unknown as { addLayers: (layers: L.Marker[]) => void }).addLayers(batch);
            updatePinCounter();
            _scheduleCache();
        }
        if (tileKeys.length === 1 && tileKeys[0] === "*") _changedLabelIds.clear();
        console.log("[UL] Server returned %d new pin(s) for tile(s): %s", batch.length, tileKeys.join(", "));
        return batch.length;
    } catch (err) {
        tileKeys.forEach((k) => _fetchedTiles.delete(k));
        throw err;
    } finally {
        _setFetching(false);
    }
}

// -- Viewport-based loading ------------------------------------------------
// Always fetch the full dataset ('*') rather than bbox subsets.
// Once _fetchedTiles has '*', all viewport changes are served from
// _pinStore with zero network cost - panning never hits the server again.
async function _loadViewport(): Promise<void> {
    if (_filterMode) return;
    if (_fetchedTiles.has("*")) return;
    await _fetchTiles(["*"]);
}

function _onViewportChange(): void {
    if (_viewportTid) clearTimeout(_viewportTid);
    _viewportTid = setTimeout(() => {
        _loadViewport().catch((err) => console.warn("Viewport load failed:", err));
    }, 220);
}

// The pin-list sidebar shows only pins within the map's current viewport
// (see _refreshPinList) - unlike marker loading above, this always needs a
// fresh server request per pan/zoom (no client-side cache to fall back on),
// but _refreshPinList itself is already a no-op while the panel is closed.
function _onPinListViewportChange(): void {
    if (_pinListTid) clearTimeout(_pinListTid);
    _pinListTid = setTimeout(_refreshPinList, 220);
}

map.on("moveend", _onViewportChange);
map.on("zoomend", _onViewportChange);

map.on("moveend", _onPinListViewportChange);
map.on("zoomend", _onPinListViewportChange);

map.on("moveend", _onMapViewUrlChange);
map.on("zoomend", _onMapViewUrlChange);
map.once("moveend", () => {
    _urlSyncReady = true;
});

// -- Remember last position ------------------------------------------------
if (_MAP_CENTER_MODE === "remember") {
    let _rememberTid: ReturnType<typeof setTimeout> | null = null;
    let _pendingPositionBody: URLSearchParams | null = null;
    const _positionBody = (): URLSearchParams => {
        const c = map.getCenter();
        const z = map.getZoom();
        return new URLSearchParams({
            lat: c.lat.toFixed(6),
            lng: c.lng.toFixed(6),
            zoom: String(z),
            csrfmiddlewaretoken: (document.querySelector('[name=csrfmiddlewaretoken]') as HTMLInputElement | null)?.value || "",
        });
    };
    const _saveMapPosition = (): void => {
        _rememberTid = null;
        _pendingPositionBody = null;
        const body = _positionBody();
        fetch(MAP_CFG.urls.settingsSaveMapPosition, {
            method: "POST",
            headers: { "X-CSRFToken": body.get("csrfmiddlewaretoken") ?? "", "Content-Type": "application/x-www-form-urlencoded" },
            body: body.toString(),
        }).catch(() => {});
    };
    const _onRememberChange = (): void => {
        if (_rememberTid) clearTimeout(_rememberTid);
        _pendingPositionBody = _positionBody();
        _rememberTid = setTimeout(_saveMapPosition, 800);
    };
    map.on("moveend", _onRememberChange);
    map.on("zoomend", _onRememberChange);

    // The 800ms debounce above can still be pending when the user
    // navigates away (closes the tab, follows a link) - fetch() isn't
    // guaranteed to finish once the page starts unloading, so the very
    // last pan/zoom was silently never saved. sendBeacon is designed
    // for exactly this: the browser keeps it alive across the
    // navigation. Both events are registered since pagehide doesn't
    // fire reliably in every browser for every kind of navigation.
    const _flushPendingPosition = (): void => {
        if (!_pendingPositionBody || _rememberTid === null) return;
        clearTimeout(_rememberTid);
        navigator.sendBeacon(MAP_CFG.urls.settingsSaveMapPosition, new Blob([_pendingPositionBody.toString()], { type: "application/x-www-form-urlencoded" }));
        _rememberTid = null;
        _pendingPositionBody = null;
    };
    window.addEventListener("pagehide", _flushPendingPosition);
    window.addEventListener("beforeunload", _flushPendingPosition);
}

// -- Filter mode -----------------------------------------------------------
// The filter POST answers with identifiers rather than payloads whenever this
// page can prove its store is the account's current pin set, which turns an
// 11.45MB document for a 10,000-pin account into ~360KB of uuids and removes
// the payload build from a request the user is waiting on. The proof is the
// fingerprint map.pins.meta serves: the server re-derives it and only trusts
// a claim that still matches. _filterStoreTrusted is this side's veto - it
// drops the claim after an identifier the store could not resolve, until the
// next full refresh re-establishes it.
let _filterStoreTrusted = true;

function _syncFilterStoreClaim(): void {
    const field = document.getElementById("fp-store-fingerprint") as HTMLInputElement | null;
    if (!field) return;
    const usable = _filterStoreTrusted && _fetchedTiles.has("*") && _lastKnownUpdated;
    field.value = usable ? (_lastKnownUpdated ?? "") : "";
}

function _pushFilterStateToUrl(): void {
    const form = document.getElementById("filter-form") as HTMLFormElement | null;
    if (!form) return;
    const params = new URLSearchParams(new FormData(form) as unknown as Record<string, string>);
    const clean = new URLSearchParams();
    for (const [k, v] of params.entries()) {
        if (v !== "" && k !== "store_fingerprint") clean.append(k, v);
    }
    history.replaceState({ filter: clean.toString() }, "", _urlWithMapView(clean));
}
// Wired to the pushState UX for filter-panel navigation, kept alongside its
// sibling URL-sync helpers even though today's filter flow reads state back
// from the URL on load (see _restoreFiltersFromUrl) rather than calling this
// on every change.
void _pushFilterStateToUrl;

function _applyFilterMeta(meta: { truncated?: boolean; shown?: number; total?: number } | null | undefined): void {
    const note = document.getElementById("fp-truncation-note");
    if (!note) return;
    if (meta && meta.truncated) {
        note.textContent = `Showing ${meta.shown} of ${meta.total} matching pins. Narrow the filter to see the rest.`;
        note.hidden = false;
    } else {
        note.hidden = true;
    }
}
// Invoked by the filter-results partial's own inline script (data.html) via
// this file's module-scope binding once bundled - kept here rather than
// duplicated so the truncation note's markup has exactly one owner.
void _applyFilterMeta;

// An identifier the store cannot resolve means the claim was wrong - the
// store is behind what the server just filtered. Ask again for payloads
// rather than drawing a map that is quietly missing pins.
//
// Bounded at one recovery, because this reaches the server by re-submitting
// the form the identifier response came from: a store that stays behind
// would otherwise turn one filter change into an unbounded series of them,
// which is the shape of cost this programme exists to remove. A second miss
// means the handshake is wrong rather than the store stale, so trust is not
// restored again and this page asks for payloads from then on.
let _filterStoreMisses = 0;

function _refilterWithPayloads(missing: number): void {
    _filterStoreMisses += 1;
    if (_filterStoreMisses > 2) return;
    console.warn("[UL] %d filtered pin(s) missing from the store - refetching payloads", missing);
    _filterStoreTrusted = false;
    _syncFilterStoreClaim();
    const form = document.getElementById("filter-form");
    if (form) htmx.trigger(form, "change");
    if (_filterStoreMisses === 1) {
        _refreshAllPins()
            .then(function () {
                _filterStoreTrusted = true;
                _syncFilterStoreClaim();
            })
            .catch(function () {});
    }
}
// Called by the filter-results partial's inline script when the server
// reports identifiers this store couldn't resolve.
void _refilterWithPayloads;

// Called when search/filter results arrive (see data.html).
// Called by resetFilters() to restore the full pin set from the store.
function _exitFilterMode(): void {
    if (!_filterMode) return;
    _filterMode = false;
    // Clear filter params from URL but keep map viewport
    history.replaceState({}, "", _urlWithMapView(new URLSearchParams()));
    clusterGroup.clearLayers();
    _markerMap.clear();
    const layers: L.Marker[] = [];
    for (const pin of _pinStore.values()) {
        const m = _buildMarker(pin);
        if (m) {
            _markerMap.set(pin.uuid, m);
            layers.push(m);
        }
    }
    if (layers.length) (clusterGroup as unknown as { addLayers: (layers: L.Marker[]) => void }).addLayers(layers);
    updatePinCounter();
    if (_childPinsActive) _loadChildPins();
}
window._exitFilterMode = _exitFilterMode;

// -- Initialization --------------------------------------------------------
(async () => {
    // Other pages (e.g. the bulk pin importer) can't reach this page's cache
    // directly, so they flag it dirty instead. Honor that by invalidating the
    // cache *before* reading it, so a completed import's new pins show up on
    // this load rather than the ~2 minute background poll catching up later.
    try {
        if (localStorage.getItem("ul_pins_dirty") === "1") {
            console.log("[UL] Pin cache flagged dirty (e.g. after an import) - forcing refresh");
            window.invalidatePinCache!();
            localStorage.removeItem("ul_pins_dirty");
        }
    } catch {
        /* best effort */
    }
    const cache = _readCache();
    if (cache?.pins && Object.keys(cache.pins).length) {
        // Restore from cache immediately - no server round-trip, no loading indicator.
        const ageSeconds = Math.round((Date.now() - cache.ts) / 1000);
        console.log("[UL] Cache HIT - %d pins, age %ds, app_uuid=%s", Object.keys(cache.pins).length, ageSeconds, cache.appUuid || "none");
        // Before the markers: a chip is drawn by resolving the pin's
        // label ids, so a marker built against an empty dictionary loses them.
        _mergeLabels(cache.labels);
        const layers: L.Marker[] = [];
        for (const [uuid, pin] of Object.entries(cache.pins)) {
            _pinStore.set(uuid, pin);
            const m = _buildMarker(pin);
            if (m) {
                _markerMap.set(uuid, m);
                layers.push(m);
            }
        }
        if (cache.tiles) cache.tiles.forEach((k) => _fetchedTiles.add(k));
        if (layers.length) (clusterGroup as unknown as { addLayers: (layers: L.Marker[]) => void }).addLayers(layers);
        // Every marker was just built against this dictionary, so nothing in
        // it is outstanding. Without this the first merge - which sees every
        // entry as new - would make the next refresh rebuild all of them.
        _changedLabelIds.clear();
        updatePinCounter();
        _suppressPinOverlay(); // cache hit - overlay is not needed
        hideLoadingMessage();
        _lastKnownUpdated = cache.lastUpdated || null;
        _updateCacheStatus(cache.ts);

        // Trigger a silent background refresh when:
        //   • Cache has no lastUpdated baseline (old format) - can't validate freshness
        //   • Cache used partial tile keys instead of '*' - panning would hit server
        // _refreshAllPins() fetches all pins, marks _fetchedTiles as '*', and
        // reschedules the cache write with the upgraded format.
        if (!_lastKnownUpdated || !_fetchedTiles.has("*")) {
            const reason = !_lastKnownUpdated ? "no lastUpdated baseline" : "partial tile cache";
            console.log("[UL] Cache needs upgrade (%s) - triggering silent refresh", reason);
            _refreshAllPins().catch(() => {});
        }
    } else {
        // Cold start: cache disabled, empty, or expired.
        // Fetch the full pin set in one request so the cache becomes complete
        // and future viewport pans never need a server round-trip.
        if (!_USE_CACHE) {
            console.log("[UL] Pin cache disabled by user preference - fetching from server");
        } else {
            console.log("[UL] Cache MISS - fetching all pins from server");
        }
        try {
            await _fetchTiles(["*"]);
            hideLoadingMessage();
        } catch (e) {
            console.warn("Initial pin load failed:", e);
            const txt = document.getElementById("plo-text");
            if (txt) txt.textContent = "Could not load pins. Try refreshing.";
        }
    }
    // Deferred to a macrotask so the loading indicator has a chance to clear
    // and other post-load UI settles first. _mapLayers may not be built yet
    // at this point (it's assigned further down this script) - the
    // 'map-layers' card's shouldShow()/action() guard against that (see
    // where _mapLayers is declared, above _onboardingCards) instead of
    // assuming ordering here.
    setTimeout(initMapOnboarding, 0);
    // Begin background polling.  First call compares cache.lastUpdated to the
    // server's value and triggers _refreshAllPins() only when they differ.
    _pollForUpdates();
    setInterval(_pollForUpdates, _POLL_INTERVAL);
})();

// -- Child pins (child pins) layer ---------------------------------------------
// Pins nested inside another pin (merged pins, child pins) are hidden from
// the main map by default; this layer renders them as smaller markers with
// a link back to their parent. Loaded lazily the first time it's turned on,
// and re-fetched with the active filter criteria whenever filters change.
// Markers are added directly into the shared clusterGroup (not a separate
// plain layer group) so child pins cluster together with regular pins when
// zoomed out instead of always rendering as loose individual markers.
const _childPinStore = new Map<string, PinData>(); // uuid → child pin data
const _childMarkerMap = new Map<string, L.Marker>(); // uuid → L.Marker
let _childPinsActive = false;
let _childPinsFetchPromise: Promise<void> | null = null;

function _buildChildMarker(pin: PinData): L.Marker | null {
    if (!pin.latitude || !pin.longitude) return null;
    const color = pin.color || "#7c3aed";
    const iconName = pin.icon || "place";
    let inner: string;
    if (/^[a-z_]+$/.test(iconName)) {
        inner = `<i class="material-icons child-pin-icon" style="color:${_escHtml(color)}">${_escHtml(iconName)}</i>`;
    } else if (/^(https?:\/\/|\/)/.test(iconName)) {
        inner = `<img src="${_escHtml(iconName)}" class="child-pin-img" alt="">`;
    } else {
        inner = `<span class="child-pin-emoji">${_escHtml(iconName)}</span>`;
    }
    const marker = L.marker([Number.parseFloat(String(pin.latitude)), Number.parseFloat(String(pin.longitude))], {
        icon: L.divIcon({ className: "child-pin-marker-wrap", html: `<span class="child-pin-marker">${inner}</span>`, iconSize: [26, 26], iconAnchor: [13, 13] }),
    });
    const parentLink = pin.parent_url
        ? `<div class="popup-child-parent"><i class="material-symbols-outlined">subdirectory_arrow_right</i> Child pin of <a href="${_escHtml(pin.parent_url)}">${_escHtml(pin.parent_name || "parent pin")}</a></div>`
        : "";
    marker.bindPopup(`
            <div class="pin-popup child-pin-popup" data-uuid="${pin.uuid}">
                <div class="popup-title">${_escHtml(pin.name || "Child pin")}</div>
                ${parentLink}
                ${pin.description ? `<div class="popup-desc">${_escHtml(pin.description)}</div>` : ""}
                <div class="popup-actions">
                    <a href="${_escHtml(pin.url || "#")}" class="view-full-pin">View Details</a>
                    ${pin.child_count && pin.child_count > 0 ? `<button class="btn btn--icon promote-children-button" onclick="event.stopPropagation(); promoteChildPins('${pin.slug || pin.uuid}', ${pin.child_count})" title="Promote all child pins up one level"><i class="material-symbols-outlined">move_up</i></button>` : ""}
                </div>
            </div>`);

    // Select-mode click toggles selection instead of opening the popup,
    // matching the root-marker behavior in _buildMarker. Ctrl/cmd-click
    // on a second pin enters select mode with both selected.
    marker.on("click", function (e) {
        _handleSelectablePinClick(e, pin.uuid, marker);
    });

    // Right-click behaves like left-click, matching _buildMarker - see its
    // own comment for why this must stop propagation to the map's own
    // contextmenu handler.
    marker.on("contextmenu", function (e) {
        L.DomEvent.stop(e);
        if (_selectMode) {
            _togglePinSelection(pin.uuid);
            return;
        }
        marker.openPopup();
    });
    return marker;
}

function _activeFilterQueryString(): string {
    if (!_hasActiveFilters()) return "";
    const form = document.getElementById("filter-form") as HTMLFormElement;
    const params = new URLSearchParams(new FormData(form) as unknown as Record<string, string>);
    params.delete("csrfmiddlewaretoken");
    // Only map.search answers differently for it; every other consumer of
    // these criteria would just carry it around.
    params.delete("store_fingerprint");
    return params.toString();
}

function _loadChildPins(): Promise<void> {
    const qs = _activeFilterQueryString();
    const url = MAP_CFG.urls.mapPinsChildren + (qs ? `?${qs}` : "");
    _setFetching(true, "Loading child pins...");
    _childPinsFetchPromise = _fetchJson<{ pins?: PinData[] }>(url, { headers: { "X-Requested-With": "XMLHttpRequest" } }, 30000)
        .then(function (data) {
            if (_childMarkerMap.size) (clusterGroup as unknown as { removeLayers: (layers: L.Marker[]) => void }).removeLayers(Array.from(_childMarkerMap.values()));
            _childPinStore.clear();
            _childMarkerMap.clear();
            const newMarkers: L.Marker[] = [];
            (data.pins || []).forEach(function (pin) {
                _childPinStore.set(pin.uuid, pin);
                const m = _buildChildMarker(pin);
                if (m) {
                    _childMarkerMap.set(pin.uuid, m);
                    newMarkers.push(m);
                }
            });
            if (newMarkers.length) (clusterGroup as unknown as { addLayers: (layers: L.Marker[]) => void }).addLayers(newMarkers);
        })
        .catch(function (err) {
            console.warn("[UL] Could not load child pins:", err);
        })
        .finally(function () {
            _setFetching(false);
        });
    return _childPinsFetchPromise;
}

function setChildPinsActive(on: boolean): Promise<void> {
    if (on === _childPinsActive) return _childPinsFetchPromise || Promise.resolve();
    _childPinsActive = on;
    if (on) {
        return _loadChildPins();
    }
    if (_childMarkerMap.size) (clusterGroup as unknown as { removeLayers: (layers: L.Marker[]) => void }).removeLayers(Array.from(_childMarkerMap.values()));
    _childPinStore.clear();
    _childMarkerMap.clear();
    return Promise.resolve();
}

function toggleChildPins(): void {
    void setChildPinsActive(!_childPinsActive);
}

// Keep the layer in sync with the filter panel: any filter change (or reset)
// re-fetches the child pins with the same criteria the root pins use.
document.getElementById("filter-form")!.addEventListener("htmx:afterRequest", function (e) {
    const detail = (e as CustomEvent<{ successful?: boolean }>).detail;
    if (_childPinsActive && detail && detail.successful) _loadChildPins();
});

// Refreshed at send time rather than wherever the store changes: there are
// several such places and one of them forgetting would silently hand the
// server a claim this page can no longer back.
document.getElementById("filter-form")!.addEventListener("htmx:configRequest", function (e) {
    _syncFilterStoreClaim();
    const field = document.getElementById("fp-store-fingerprint") as HTMLInputElement | null;
    const detail = (e as CustomEvent<{ parameters?: Record<string, unknown> }>).detail;
    if (detail && detail.parameters && field) detail.parameters["store_fingerprint"] = field.value;
});

// Places layer group (landmarks from external APIs, including Google Places)
const _placesLayerGroup = L.layerGroup();
const _placesMarkerMap = new Map<string, L.Marker>(); // place_id → L.Marker
let _placesLayerActive = false;

// Waterways + railways, including OSM lifecycle-tagged historic routes.
// Geometry is fetched only for the visible viewport from UrbanLens'
// server-side Overpass proxy; this keeps browser requests small and avoids
// exposing or depending directly on a public community endpoint.
const _infrastructureLayerGroup = L.layerGroup();
const _infrastructureRenderer = L.canvas({ padding: 0.5 });
const _INFRASTRUCTURE_MIN_ZOOM = 11;
let _infrastructureLayerActive = false;
let _infrastructureAbortController: AbortController | null = null;
let _infrastructureRequestSerial = 0;
let _infrastructureLastBbox = "";
let _infrastructureFetchTimer: ReturnType<typeof setTimeout> | null = null;
let _infrastructureZoomNoticeShown = false;

interface InfrastructureProps {
    kind?: "rail" | "waterway";
    historic?: boolean;
    name?: string;
    type?: string;
    status?: string;
    osm_url?: string;
}
interface InfrastructureResponse extends GeoJSON.FeatureCollection {
    truncated?: boolean;
}

function _infrastructureStyle(feature?: GeoJSON.Feature): L.PathOptions {
    const props: InfrastructureProps = feature?.properties || {};
    if (props.kind === "rail") {
        return {
            color: props.historic ? "#d97706" : "#8b1e3f",
            weight: props.historic ? 3 : 3.5,
            opacity: 0.9,
            dashArray: props.historic ? "8 7" : undefined,
            lineCap: "round",
            lineJoin: "round",
        };
    }
    return {
        color: props.historic ? "#0f766e" : "#1479b8",
        weight: props.historic ? 2.75 : 3,
        opacity: 0.9,
        dashArray: props.historic ? "4 7" : undefined,
        lineCap: "round",
        lineJoin: "round",
    };
}

function _bindInfrastructureFeature(feature: GeoJSON.Feature, layer: L.Layer): void {
    const props: InfrastructureProps = feature?.properties || {};
    const name = _escHtml(props.name || (props.kind === "rail" ? "Railway" : "Waterway"));
    const type = _escHtml(props.type || "");
    const status = _escHtml(props.status || "");
    const osmUrl = typeof props.osm_url === "string" && props.osm_url.startsWith("https://www.openstreetmap.org/") ? props.osm_url : "";
    const swatch = props.kind === "rail" ? (props.historic ? "#d97706" : "#8b1e3f") : props.historic ? "#0f766e" : "#1479b8";
    layer.bindTooltip(name, { sticky: true, direction: "top" });
    layer.bindPopup(`
            <div class="pin-popup">
                <div class="popup-title"><span style="color:${swatch};margin-right:.3rem;">━</span>${name}</div>
                <div class="popup-address">${type}${type && status ? " · " : ""}${status}</div>
                ${osmUrl ? `<a href="${osmUrl}" target="_blank" rel="noopener noreferrer" class="view-full-pin">View source on OpenStreetMap</a>` : ""}
            </div>
        `);
}

function _setInfrastructureLoading(loading: boolean): void {
    const button = document.querySelector("[data-map-layer=\"infrastructure\"]");
    if (button) {
        button.classList.toggle("is-loading", loading);
        button.setAttribute("aria-busy", loading ? "true" : "false");
    }
}

function _loadInfrastructure(): void {
    if (!_infrastructureLayerActive) return;
    if (map.getZoom() < _INFRASTRUCTURE_MIN_ZOOM) {
        if (_infrastructureAbortController) _infrastructureAbortController.abort();
        _infrastructureLayerGroup.clearLayers();
        _infrastructureLastBbox = "";
        _setInfrastructureLoading(false);
        if (!_infrastructureZoomNoticeShown) {
            toastr.info("Zoom in to level 11 or closer to load waterways and railways.");
            _infrastructureZoomNoticeShown = true;
        }
        return;
    }

    _infrastructureZoomNoticeShown = false;
    const bbox = map.getBounds().toBBoxString();
    if (bbox === _infrastructureLastBbox && _infrastructureLayerGroup.getLayers().length) return;
    if (_infrastructureAbortController) _infrastructureAbortController.abort();
    _infrastructureAbortController = new AbortController();
    const requestSerial = ++_infrastructureRequestSerial;
    _setInfrastructureLoading(true);

    fetch(`${MAP_CFG.urls.mapInfrastructure}?bbox=${encodeURIComponent(bbox)}`, {
        signal: _infrastructureAbortController.signal,
        headers: { "X-Requested-With": "XMLHttpRequest" },
    })
        .then(async (response) => {
            if (response.ok) return response.json() as Promise<InfrastructureResponse>;
            const error = await response.json().catch(() => ({}) as { error?: string });
            throw new Error(error.error || "Could not load infrastructure data.");
        })
        .then((data) => {
            if (!_infrastructureLayerActive || requestSerial !== _infrastructureRequestSerial) return;
            _infrastructureLayerGroup.clearLayers();
            // L.geoJSON's real runtime forwards these options to L.polyline/L.polygon
            // per-feature, so `renderer` works even though @types/leaflet's
            // GeoJSONOptions doesn't declare it - PathOptions does.
            const infrastructureGeoJsonOptions: L.GeoJSONOptions & L.PathOptions = {
                renderer: _infrastructureRenderer,
                style: _infrastructureStyle,
                onEachFeature: _bindInfrastructureFeature,
            };
            L.geoJSON(data, infrastructureGeoJsonOptions).addTo(_infrastructureLayerGroup);
            _infrastructureLastBbox = bbox;
            if (data.truncated) toastr.warning("This area has many waterways and railways. Zoom in to see the complete set.");
        })
        .catch((error) => {
            if (error?.name !== "AbortError" && _infrastructureLayerActive) {
                toastr.warning(error?.message || "Could not load waterways and railways.");
            }
        })
        .finally(() => {
            if (requestSerial === _infrastructureRequestSerial) _setInfrastructureLoading(false);
        });
}

function _scheduleInfrastructureFetch(): void {
    if (!_infrastructureLayerActive) return;
    if (_infrastructureFetchTimer) clearTimeout(_infrastructureFetchTimer);
    _infrastructureFetchTimer = setTimeout(_loadInfrastructure, 250);
}

function toggleInfrastructureLayer(): void {
    _infrastructureLayerActive = !_infrastructureLayerActive;
    if (_infrastructureLayerActive) {
        _infrastructureLayerGroup.addTo(map);
        _loadInfrastructure();
    } else {
        if (_infrastructureFetchTimer) clearTimeout(_infrastructureFetchTimer);
        if (_infrastructureAbortController) _infrastructureAbortController.abort();
        _setInfrastructureLoading(false);
        map.removeLayer(_infrastructureLayerGroup);
    }
    _renderMapAttribution();
}

map.on("moveend", _scheduleInfrastructureFetch);

let _mapBaseAttributionText = "";
function _renderMapAttribution(): void {
    const el = document.getElementById("page-footer-attribution-text");
    if (!el) return;
    el.textContent = _mapBaseAttributionText + (_infrastructureLayerActive ? " · Infrastructure © OpenStreetMap contributors" : "");
}

// -- Layers: shared engine (ts/shared/map-layers.ts) ------------------------
// Tile layers, the layers flyout panel, dark map mode, "remember"
// persistence, tile-loading feedback, and footer attribution all live in
// the shared MapLayers engine - the exact same code every other map on the
// site runs. This page only contributes its own custom toggles (pins, places).
_mapLayers = MapLayers.create(map, {
    root: document.getElementById("map-layers-panel"),
    apiKey: MAP_CFG.openweathermapApiKey,
    darkMode: _MAP_DARK_MODE as MapDarkMode,
    defaultBase: _DEFAULT_MAP_VIEW,
    storageKey: _LAYER_CACHE_KEY,
    contextMenu: false, // this page binds extra "Add Pin Here" items itself
    loadingTarget: document.getElementById("map"),
    // Footer attribution replaces Leaflet's on-map control (see the shared
    // partials/layout/footer.html, rendered via show_map_footer=True).
    onAttribution: function (text) {
        _mapBaseAttributionText = text;
        _renderMapAttribution();
    },
    // Persist dark mode to the server so the preference survives page refresh.
    onDarkModeChange: function (mode) {
        _MAP_DARK_MODE = mode;
        const csrf = (document.querySelector("[name=csrfmiddlewaretoken]") as HTMLInputElement | null)?.value || "";
        fetch(MAP_CFG.urls.settingsSaveMapDarkMode, {
            method: "POST",
            headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
            body: `mode=${encodeURIComponent(mode)}`,
        }).catch(() => {});
    },
    custom: {
        // The pins button highlights when pins are HIDDEN (activeWhenOff).
        pins: { isActive: () => pinsVisible, toggle: togglePins, activeWhenOff: true },
        childpins: { isActive: () => _childPinsActive, toggle: toggleChildPins },
        infrastructure: { isActive: () => _infrastructureLayerActive, toggle: toggleInfrastructureLayer },
        places: { isActive: () => _placesLayerActive, toggle: togglePlacesLayer },
    },
});

// Dev toolbar: apply map dark mode without a full page reload.
window.applyMapDarkMode = function (mode: string): void {
    _MAP_DARK_MODE = mode;
    _mapLayers!.setDarkMode(mode as MapDarkMode);
};

map.on("popupopen", function () {
    // -- Rating stars -------------------------------------------------------
    document.querySelectorAll<HTMLElement>(".popup-stars").forEach(function (starsEl) {
        const stars = starsEl.querySelectorAll<HTMLElement>(".popup-star");

        // Hover preview
        stars.forEach(function (star) {
            star.addEventListener("mouseenter", function (this: HTMLElement) {
                const val = Number.parseInt(this.dataset.val || "0", 10);
                stars.forEach((s) => s.classList.toggle("popup-star--preview", Number.parseInt(s.dataset.val || "0", 10) <= val));
            });
            star.addEventListener("mouseleave", function () {
                stars.forEach((s) => s.classList.remove("popup-star--preview"));
            });
        });

        // Click to save
        stars.forEach(function (star) {
            star.addEventListener("click", function (this: HTMLElement) {
                const val = Number.parseInt(this.dataset.val || "0", 10);
                const popup = this.closest<HTMLElement>(".pin-popup")!;
                const pinId = popup.dataset.id;
                const uuid = popup.dataset.uuid;

                // Update visual immediately
                stars.forEach((s) => s.classList.toggle("popup-star--on", Number.parseInt(s.dataset.val || "0", 10) <= val));

                // sendJson throws on a non-2xx; a bare fetch().then(json)
                // reported "Rating saved" for ratings the server refused.
                _sendJson(`/dashboard/rest/reviews/create_or_update/${pinId}/`, "PATCH", { rating: val })
                    .then(() => {
                        if (uuid && _pinStore.has(uuid)) {
                            _pinStore.get(uuid)!.rating = val;
                            _scheduleCache();
                        }
                        toastr.success("Rating saved.");
                    })
                    .catch(() => toastr.error("Failed to save rating."));
            });
        });
    });

    document.querySelectorAll<HTMLElement>(".pin-popup").forEach(function (popupEl) {
        popupEl.addEventListener("click", function (e) {
            const target = e.target as HTMLElement;
            if (!target.matches("span.value")) {
                return;
            }
            e.stopPropagation();

            const span = target;
            const popup = span.closest<HTMLElement>(".pin-popup")!;
            const initialValue = span.innerText;
            let value = initialValue;
            const name = span.parentElement!.dataset.name!;
            let input: HTMLInputElement | HTMLSelectElement;

            // If the field is status, create a select element
            if (name === "status") {
                const select = document.createElement("select");
                const options = [
                    { display: "Visited", value: "visited" },
                    { display: "Not Visited", value: "not visited" },
                    { display: "Wish to Visit", value: "wish to visit" },
                    { display: "Demolished", value: "demolished" },
                ];
                options.forEach(function (option) {
                    const optionElement = document.createElement("option");
                    optionElement.value = option.value;
                    optionElement.innerText = option.display;
                    if (option.value === value) {
                        optionElement.selected = true;
                    }
                    select.appendChild(optionElement);
                });
                select.style.display = "block";
                select.addEventListener("change", saveInputValue);
                input = select;
            } else if (name === "last_visited") {
                if (value === "never") {
                    value = new Date().toLocaleDateString();
                }
                const dateInput = document.createElement("input");
                dateInput.type = "date";
                // year-month-day with 0 prefixes
                const date = new Date(value);
                const year = date.getFullYear();
                const month = date.getMonth() + 1;
                const day = date.getDate();
                dateInput.value = `${year}-${month < 10 ? "0" : ""}${month}-${day < 10 ? "0" : ""}${day}`;

                let firstInput = true;
                dateInput.addEventListener("keydown", function (e) {
                    if (firstInput && (e.key === "Backspace" || e.key === "Delete")) {
                        e.preventDefault();
                        dateInput.value = "";
                        firstInput = false;
                    } else {
                        firstInput = false;
                    }
                });
                input = dateInput;
            } else {
                // Otherwise, create an input element
                const textInput = document.createElement("input");
                textInput.value = value;
                input = textInput;
            }

            input.name = name;
            input.dataset.initialValue = initialValue;
            span.replaceWith(input);
            input.focus();

            function saveInputValue(): void {
                let value: string | null = input.value;
                const pinUuid = popup.dataset.uuid;
                const data: Record<string, string | null> = {};
                if (name === "last_visited") {
                    if (value === "" || value === "never" || value === undefined) {
                        value = null;
                    } else {
                        value = new Date(value).toISOString().split("T")[0]!;
                    }
                }
                data[name] = value;

                if ((value || "never") === initialValue) {
                    console.log("value is the same as initial value, skipping save");
                    return;
                }

                fetch(`/dashboard/rest/pins/${pinUuid}/`, {
                    method: "PATCH",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": MAP_CFG.csrfToken,
                    },
                    body: JSON.stringify(data),
                })
                    .then((response) => {
                        if (!response.ok) {
                            throw new Error(`HTTP error! status: ${response.status}`);
                        } else {
                            return response.json();
                        }
                    })
                    .then(() => {
                        console.log(`${name} updated successfully`);
                        toastr.success(`${name.charAt(0).toUpperCase() + name.slice(1)} updated successfully`);
                        const newSpan = document.createElement("span");
                        newSpan.classList.add("value");

                        if (name === "last_visited") {
                            newSpan.innerText = value && value !== "never" ? new Date(value).toLocaleDateString() : "never";
                        } else {
                            newSpan.innerText = value || "";
                        }

                        input.replaceWith(newSpan);
                    })
                    .catch((error) => {
                        console.error("Error:", error);
                        toastr.error(`Failed to update ${name}`);
                    });
            }

            // Cast to the common HTMLElement base: addEventListener's generic overload
            // can't resolve the event type from a union receiver (HTMLInputElement |
            // HTMLSelectElement) and degrades to the plain Event overload otherwise.
            (input as HTMLElement).addEventListener("keydown", function (e: KeyboardEvent) {
                if (e.key === "Enter") {
                    saveInputValue();
                } else if (e.key === "Escape") {
                    const newSpan = document.createElement("span");
                    newSpan.classList.add("value");
                    newSpan.innerText = initialValue;
                    input.replaceWith(newSpan);
                }
            });

            input.addEventListener("blur", function () {
                if (input.value !== initialValue && document.body.contains(input)) {
                    saveInputValue();
                }
            });

            document.querySelectorAll<HTMLElement>(".pin-popup .add-tag-button").forEach(function (button) {
                button.addEventListener("click", function (e) {
                    e.stopPropagation();
                    const origButton = this;
                    const tagInput = document.createElement("input");
                    tagInput.name = "tags";
                    origButton.replaceWith(tagInput);
                    tagInput.focus();
                    function saveTagValue(this: HTMLInputElement): void {
                        const value = this.value;
                        const pinUuid = this.closest<HTMLElement>(".pin-popup")!.dataset.uuid;
                        const data: Record<string, string> = {};
                        data[this.name] = value;
                        // sendJson throws on a non-2xx; a bare fetch().then(json)
                        // announced "updated successfully" for refused edits.
                        _sendJson(`/dashboard/rest/pins/${pinUuid}/`, "PATCH", data)
                            .then(() => {
                                toastr.success(`${this.name.charAt(0).toUpperCase() + this.name.slice(1)} updated successfully`);
                                const newButton = document.createElement("button");
                                newButton.classList.add("add-tag-button");
                                newButton.innerText = "+";
                                this.replaceWith(newButton);
                            })
                            .catch((error) => {
                                console.error("Error:", error);
                                toastr.error(`Failed to update ${this.name}`);
                            });
                    }

                    tagInput.addEventListener("keydown", function (e) {
                        if (e.key === "Enter") {
                            saveTagValue.call(tagInput);
                        }
                    });

                    tagInput.addEventListener("blur", function () {
                        saveTagValue.call(tagInput);
                    });
                });
            });
        });
    });
});

document.addEventListener("htmx:configRequest", (event) => {
    (event as CustomEvent<{ headers: Record<string, string> }>).detail.headers["X-CSRFToken"] = MAP_CFG.csrfToken;
});

document.body.addEventListener("htmx:afterOnLoad", function () {
    // Dispatch a custom event after the dialog is loaded
    setTimeout(() => {
        const event = new Event("dialogLoaded");
        document.dispatchEvent(event);
        hideLoadingMessage(); // Hide loading message when content is loaded
    }, 250);
});

map.on("contextmenu", function (e) {
    map.closePopup();
    const lat = e.latlng.lat;
    const lng = e.latlng.lng;
    _showContextMenuMarker(lat, lng);
    MapContextMenu.show({
        lat: lat,
        lng: lng,
        zoom: map.getZoom(),
        clientX: e.originalEvent.clientX,
        clientY: e.originalEvent.clientY,
        extraItems: [
            {
                icon: "add_location",
                label: "Add Pin Here",
                onClick: function () {
                    const transferred = _contextMenuMarker;
                    _contextMenuMarker = null;
                    if (transferred) _addPinMarker = transferred;
                    openAddPinDialog(lat, lng, { keepMarker: !!transferred });
                },
            },
        ],
        onClose: function () {
            _clearContextMenuMarker();
        },
    });
});
map.invalidateSize();

// Re-validate map size on resize and orientation change (important on mobile).
// Also re-fits the filter panel's accordion sections to the new viewport height
// (self-guards on the panel being open, so this is a cheap no-op while it's closed).
(function () {
    let _resizeTimer: ReturnType<typeof setTimeout>;
    let _fitTimer: ReturnType<typeof setTimeout>;
    function onResize(): void {
        clearTimeout(_resizeTimer);
        _resizeTimer = setTimeout(function () {
            map.invalidateSize();
        }, 150);
        clearTimeout(_fitTimer);
        _fitTimer = setTimeout(function () {
            _autoFitFilterSections();
        }, 150);
        if (_pinListResizeTimer) clearTimeout(_pinListResizeTimer);
        _pinListResizeTimer = setTimeout(function () {
            _refreshPinList();
        }, 150);
    }
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", function () {
        setTimeout(function () {
            map.invalidateSize();
        }, 300);
        setTimeout(function () {
            _autoFitFilterSections();
        }, 300);
    });
})();

// Pins visibility is page-specific state (the cluster layer), registered
// with the shared layers engine as the "pins" custom toggle - the engine
// owns the button state, this owns the layer.
function togglePins(): void {
    if (pinsVisible) {
        map.removeLayer(pinsLayerGroup);
    } else {
        pinsLayerGroup.addTo(map);
    }
    pinsVisible = !pinsVisible;
    updatePinCounter();
}

// -- Places layer (VIP only) -----------------------------------------------
const _SHOW_PLACES_LAYER = MAP_CFG.showPlacesLayer;
let _placesFetchTid: ReturnType<typeof setTimeout> | null = null;

interface PlaceData {
    place_id?: string;
    source: "nps" | "wikipedia" | "google";
    name?: string;
    lat: number;
    lng: number;
    url?: string;
    vicinity?: string;
    formatted_address?: string;
    rating?: number;
    user_ratings_total?: number;
    types?: string[];
    editorial_summary?: { overview?: string };
    description?: string;
    website?: string;
}

const _PLACES_SOURCE_ICONS: Record<string, { icon: string; cls: string }> = {
    nps: { icon: "park", cls: "search-result-marker-icon--places-nps" },
    wikipedia: { icon: "bookmark", cls: "search-result-marker-icon--places-wiki" },
    google: { icon: "g_mobiledata", cls: "search-result-marker-icon--places" },
};

function _placeSourceLabel(source: string): string {
    if (source === "nps") return "National Park Service";
    if (source === "wikipedia") return "Wikipedia";
    return "Google Places";
}

function _placeFallbackUrl(place: PlaceData): string {
    if (place.url) return place.url;
    if (place.source === "wikipedia" && place.name) return `https://en.wikipedia.org/wiki/${encodeURIComponent(place.name.replace(/ /g, "_"))}`;
    if (place.source === "nps") return "https://www.nps.gov/findapark/index.htm";
    if (place.place_id && place.source === "google")
        return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(place.name || `${place.lat},${place.lng}`)}&query_place_id=${encodeURIComponent(place.place_id)}`;
    return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(place.name || `${place.lat},${place.lng}`)}`;
}

function _formatPlaceTypes(types: string[] | undefined): string {
    return (types || [])
        .filter(Boolean)
        .slice(0, 3)
        .map((t) => String(t).replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase()))
        .join(", ");
}

function _buildPlaceDetailRows(place: PlaceData, details: Partial<PlaceData> = {}): string {
    const merged = { ...place, ...details };
    const website = details.website || merged.url || _placeFallbackUrl(place);
    const parts: string[] = [];

    if (place.source === "nps") {
        // Combine source label and website link into one row
        parts.push(
            `<a class="map-context-menu__item map-context-menu__item--link places-popup-website" href="${_escapeHtml(website)}" target="_blank" rel="noopener noreferrer"><i class="material-symbols-outlined">travel_explore</i>National Park Service | More Information</a>`,
        );
    } else if (place.source === "wikipedia") {
        parts.push(
            `<a class="map-context-menu__item map-context-menu__item--link places-popup-website" href="${_escapeHtml(website)}" target="_blank" rel="noopener noreferrer"><i class="material-symbols-outlined">menu_book</i>Wikipedia | Read Article</a>`,
        );
    } else {
        const source = _placeSourceLabel(place.source);
        parts.push(`<div class="places-popup-detail"><i class="material-symbols-outlined">travel_explore</i><span>${_escapeHtml(source)}</span></div>`);
    }

    if (merged.formatted_address || merged.vicinity) {
        parts.push(`<div class="places-popup-detail"><i class="material-symbols-outlined">location_on</i><span>${_escapeHtml(merged.formatted_address || merged.vicinity)}</span></div>`);
    }
    if (merged.rating) {
        const total = merged.user_ratings_total ? ` (${_escapeHtml(String(merged.user_ratings_total))} reviews)` : "";
        parts.push(`<div class="places-popup-detail"><i class="material-symbols-outlined">star</i><span>${_escapeHtml(String(merged.rating))} ★${total}</span></div>`);
    }
    const typeLabel = _formatPlaceTypes(merged.types);
    const suppressType = place.source === "wikipedia" || (place.source === "nps" && typeLabel.toLowerCase() === "national park");
    if (typeLabel && !suppressType) {
        parts.push(`<div class="places-popup-detail"><i class="material-symbols-outlined">category</i><span>${_escapeHtml(typeLabel)}</span></div>`);
    }
    const summary = merged.editorial_summary?.overview || merged.description;
    if (summary) {
        parts.push(`<div class="places-popup-summary">${_escapeHtml(summary)}</div>`);
    }
    if (place.source !== "nps" && place.source !== "wikipedia") {
        parts.push(
            `<a class="map-context-menu__item map-context-menu__item--link places-popup-website" href="${_escapeHtml(website)}" target="_blank" rel="noopener noreferrer"><i class="material-symbols-outlined">open_in_new</i>Website / more information</a>`,
        );
    }
    return parts.join("");
}

function _buildPlacesMarker(place: PlaceData): L.Marker {
    const lat = place.lat,
        lng = place.lng;
    const src = _PLACES_SOURCE_ICONS[place.source] || _PLACES_SOURCE_ICONS.google!;
    const marker = L.marker([lat, lng], {
        icon: L.divIcon({
            className: "search-result-marker-wrap",
            html: `<i class="material-icons search-result-marker-icon ${src.cls}">${src.icon}</i>`,
            iconSize: [28, 28],
            iconAnchor: [14, 28],
        }),
        zIndexOffset: 1000,
        title: place.name,
    });
    marker.on("click", function (e) {
        map.closePopup();
        const anchor = _markerMenuAnchor(e, lat, lng);
        const detailsWrap = document.createElement("div");
        detailsWrap.className = "places-popup-details";
        detailsWrap.innerHTML = _buildPlaceDetailRows(place);
        const menu = MapContextMenu.show({
            lat: lat,
            lng: lng,
            zoom: map.getZoom(),
            clientX: anchor.x,
            clientY: anchor.y,
            header: place.name,
            preamble: detailsWrap,
            extraItems: [
                {
                    icon: "add_location",
                    label: "Add to my map",
                    onClick: function () {
                        openAddPinDialog(lat, lng, { defaultName: place.name, placeId: place.place_id });
                    },
                },
                {
                    icon: "info",
                    label: "More information",
                    onClick: function () {
                        _showPlaceInfoPanel(place);
                    },
                },
            ],
        });
        if (place.source === "google" && place.place_id) {
            fetch(`${MAP_CFG.urls.mapPlacesDetails}?place_id=${encodeURIComponent(place.place_id)}`, { headers: { "X-Requested-With": "XMLHttpRequest" } })
                .then((resp) => (resp.ok ? (resp.json() as Promise<{ place?: PlaceData }>) : null))
                .then((data) => {
                    if (data?.place && document.body.contains(detailsWrap)) {
                        detailsWrap.innerHTML = _buildPlaceDetailRows(place, data.place);
                        _placeFloatingMenu(menu, anchor.x, anchor.y);
                    }
                })
                .catch(() => {});
        }
        if (place.source === "wikipedia") {
            const wikiTitle = (place.name || "").replace(/ /g, "_");
            fetch(`https://en.wikipedia.org/api/rest_v1/page/summary/${encodeURIComponent(wikiTitle)}`, {
                headers: { Accept: "application/json" },
            })
                .then((resp) => (resp.ok ? (resp.json() as Promise<{ extract?: string; description?: string }>) : null))
                .then((data) => {
                    if (data && document.body.contains(detailsWrap)) {
                        const extract = data.extract || "";
                        const short = extract.length > 400 ? extract.slice(0, 397) + "..." : extract;
                        detailsWrap.innerHTML = _buildPlaceDetailRows(place, {
                            description: short,
                            vicinity: data.description || place.vicinity || "",
                        });
                        _placeFloatingMenu(menu, anchor.x, anchor.y);
                    }
                })
                .catch(() => {});
        }
    });
    return marker;
}

async function _fetchAndShowPlaces(): Promise<void> {
    if (!_placesLayerActive) return;
    const zoom = map.getZoom();
    const center = map.getCenter();
    // Pass zoom so the server can apply the minimum zoom restriction per source
    // (Google Places requires zoom ≥ 10; NPS and Wikipedia show at any zoom).
    const url = `${MAP_CFG.urls.mapPlacesNearby}?lat=${center.lat.toFixed(5)}&lng=${center.lng.toFixed(5)}&zoom=${zoom}`;
    try {
        const resp = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (!resp.ok) return;
        const data = (await resp.json()) as { places?: PlaceData[] };
        const userLatLngs = new Set([..._pinStore.values()].map((p) => `${Number.parseFloat(String(p.latitude)).toFixed(4)},${Number.parseFloat(String(p.longitude)).toFixed(4)}`));
        (data.places || []).forEach((place) => {
            if (!place.place_id || _placesMarkerMap.has(place.place_id)) return;
            // Skip if user already has a pin at this location (within ~11m)
            const pKey = `${place.lat.toFixed(4)},${place.lng.toFixed(4)}`;
            if (userLatLngs.has(pKey)) return;
            const marker = _buildPlacesMarker(place);
            _placesLayerGroup.addLayer(marker);
            _placesMarkerMap.set(place.place_id, marker);
        });
    } catch {
        /* best effort */
    }
}

function _schedulePlacesFetch(): void {
    if (_placesFetchTid) clearTimeout(_placesFetchTid);
    _placesFetchTid = setTimeout(_fetchAndShowPlaces, 600);
}

async function _showPlaceInfoPanel(place: PlaceData): Promise<void> {
    const panel = document.getElementById("place-info-panel");
    if (!panel) return;
    const titleEl = panel.querySelector(".place-info-title");
    const bodyEl = panel.querySelector(".place-info-body");
    if (titleEl) titleEl.textContent = place.name || "";
    panel.removeAttribute("hidden");
    panel.classList.add("is-open");

    // NPS and Wikipedia carry all detail data inline - no extra fetch needed.
    if (place.source === "nps" || place.source === "wikipedia") {
        const parts: string[] = [];
        if (place.vicinity) parts.push(`<div class="place-info-address"><i class="material-symbols-outlined">location_on</i> ${_ulEscText(place.vicinity)}</div>`);
        if (place.description) parts.push(`<div class="place-info-summary">${_ulEscText(place.description)}</div>`);
        if (place.url) parts.push(`<div class="place-info-link"><a href="${_ulEscAttr(_ulSafeUrl(place.url))}" target="_blank" rel="noopener noreferrer">More information</a></div>`);
        if (bodyEl) bodyEl.innerHTML = parts.join("") || "<em>No details available.</em>";
        return;
    }

    // Google Places - fetch details from the server.
    if (bodyEl) bodyEl.innerHTML = '<span class="place-info-loading">Loading...</span>';
    try {
        const resp = await fetch(`${MAP_CFG.urls.mapPlacesDetails}?place_id=${encodeURIComponent(place.place_id || "")}`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (!resp.ok) throw new Error();
        const data = (await resp.json()) as { place?: PlaceData };
        const d = data.place || ({} as PlaceData);
        const parts: string[] = [];
        if (d.formatted_address) parts.push(`<div class="place-info-address"><i class="material-symbols-outlined">location_on</i> ${_ulEscText(d.formatted_address)}</div>`);
        if (d.rating) parts.push(`<div class="place-info-rating">Rating: ${_ulEscText(d.rating)} ★</div>`);
        if (d.editorial_summary?.overview) parts.push(`<div class="place-info-summary">${_ulEscText(d.editorial_summary.overview)}</div>`);
        if (d.website) parts.push(`<div class="place-info-link"><a href="${_ulEscAttr(_ulSafeUrl(d.website))}" target="_blank" rel="noopener noreferrer">Website</a></div>`);
        if (bodyEl) bodyEl.innerHTML = parts.join("") || "<em>No details available.</em>";
    } catch {
        if (bodyEl) bodyEl.innerHTML = "<em>Could not load details.</em>";
    }
}

function togglePlacesLayer(): void {
    if (!_SHOW_PLACES_LAYER) return;
    _placesLayerActive = !_placesLayerActive;
    const btn = document.querySelector("#places-button");
    if (_placesLayerActive) {
        _placesLayerGroup.addTo(map);
        btn?.classList.add("active");
        _fetchAndShowPlaces();
    } else {
        map.removeLayer(_placesLayerGroup);
        btn?.classList.remove("active");
    }
}

// Refresh places when map moves (debounced)
map.on("moveend zoomend", function () {
    if (_placesLayerActive) _schedulePlacesFetch();
});

// Update the Leaflet icon URLs
const newIcon = L.icon({
    iconUrl: MAP_CFG.assets.leafletMarkerIcon,
    shadowUrl: MAP_CFG.assets.leafletMarkerShadow,
    iconSize: [25, 41],
    shadowSize: [41, 41],
    iconAnchor: [12, 41],
    shadowAnchor: [12, 41],
    popupAnchor: [1, -34],
});
L.Marker.prototype.options.icon = newIcon;

async function _deletePinByUuid(pinUuid: string, pinName: string): Promise<boolean> {
    // Shared flow (base.html): confirm, then ask what to do with child pins
    // when the server reports the pin has some (delete them or keep them).
    const result = await deletePinCascade(pinUuid, pinName, MAP_CFG.csrfToken);
    if (result === null) {
        toastr.error("Failed to delete pin");
        return false;
    }
    if (result !== true) return false;
    toastr.success("Location deleted successfully");
    // Kept (promoted) child pins and any other side effects surface on the
    // next pin poll - flag the cache dirty so that happens promptly.
    try {
        localStorage.setItem("ul_pins_dirty", "1");
    } catch {
        /* poll covers it */
    }
    if (pinUuid && _markerMap.has(pinUuid)) {
        clusterGroup.removeLayer(_markerMap.get(pinUuid)!);
        _markerMap.delete(pinUuid);
    }
    if (pinUuid) _pinStore.delete(pinUuid);
    _scheduleCache();
    map.closePopup();
    updatePinCounter();
    _refreshPinList();
    return true;
}

// -- Pin popup overflow menu -----------------------------------------
// The popup's destructive and secondary actions (add to list, promote,
// edit, delete) live behind one button so the popup reads as information
// with a way in, rather than a row of icons to decode. Leaflet rebuilds
// popup DOM on every open, so these are delegated by class rather than
// wired per marker.
function closePinPopupMenus(): void {
    document.querySelectorAll<HTMLElement>(".popup-menu-items:not([hidden])").forEach(function (menu) {
        menu.hidden = true;
        const toggle = menu.parentElement?.querySelector(".popup-menu-toggle");
        if (toggle) toggle.setAttribute("aria-expanded", "false");
    });
}
window.closePinPopupMenus = closePinPopupMenus;

function togglePinPopupMenu(button: HTMLElement): void {
    const menu = button.parentElement?.querySelector<HTMLElement>(".popup-menu-items");
    if (!menu) return;
    const wasOpen = !menu.hidden;
    closePinPopupMenus();
    menu.hidden = wasOpen;
    button.setAttribute("aria-expanded", String(!wasOpen));
}
window.togglePinPopupMenu = togglePinPopupMenu;

// A click anywhere else, or Escape, closes it - including a click on the
// map itself, which Leaflet does not route through the popup.
document.addEventListener("click", function (event) {
    if (!(event.target as HTMLElement).closest(".popup-menu")) closePinPopupMenus();
});
document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closePinPopupMenus();
});

async function deletePin(button: HTMLElement): Promise<void> {
    const popup = button.closest<HTMLElement>(".pin-popup")!;
    const pinUuid = popup.dataset.uuid!;
    const pinName = popup.querySelector(".popup-title")?.textContent?.trim() || "this pin";
    await _deletePinByUuid(pinUuid, pinName);
}
window.deletePin = deletePin;

async function promoteChildPins(pinSlug: string, childCount: number): Promise<void> {
    const n = childCount || 0;
    const confirmed = await confirmAction({
        title: "Promote child pins?",
        message: `This moves ${n} child pin${n === 1 ? "" : "s"} up one level, so ${n === 1 ? "it is" : "they are"} no longer nested under this pin.`,
        confirmLabel: "Promote",
    });
    if (!confirmed) return;
    fetch("/dashboard/map/pin/" + encodeURIComponent(pinSlug) + "/promote-children/", {
        method: "POST",
        headers: { "X-CSRFToken": MAP_CFG.csrfToken },
    })
        .then(function (r) {
            if (!r.ok) throw new Error();
            return r.json() as Promise<{ promoted: number }>;
        })
        .then(function (data) {
            map.closePopup();
            toastr.success(`${data.promoted} pin${data.promoted === 1 ? "" : "s"} promoted.`);
            try {
                localStorage.setItem("ul_pins_dirty", "1");
            } catch {
                /* poll covers it */
            }
            _refreshAllPins();
            if (_childPinsActive) _loadChildPins();
        })
        .catch(function () {
            toastr.error("Failed to promote child pins.");
        });
}
window.promoteChildPins = promoteChildPins;

// ============================================================
// Multi-select tool: select, merge, delete (+undo), bulk edit
// ============================================================

function toggleSelectMode(): void {
    if (_selectMode) {
        exitSelectMode();
    } else {
        enterSelectMode();
    }
}
window.toggleSelectMode = toggleSelectMode;

function enterSelectMode(): void {
    if (_selectMode) return;
    _selectMode = true;
    document.getElementById("select-pins-button")?.classList.add("active");
    document.getElementById("map")?.classList.add("select-mode");
    _addPinClickMode = false;
    _markerMap.forEach(function (m) {
        if (m.dragging) m.dragging.disable();
    });
    _renderBulkSelectToolbar();
}

function exitSelectMode(): void {
    if (!_selectMode) return;
    _selectMode = false;
    document.getElementById("select-pins-button")?.classList.remove("active");
    document.getElementById("map")?.classList.remove("select-mode");
    // Dragging is only ever off for the length of a rubber-band gesture
    // (see _initSelectDragRectangle); this covers one that never finished.
    map.dragging.enable();
    _markerMap.forEach(function (m) {
        if (m.dragging) m.dragging.enable();
    });
    _lastClickedPinUuid = null;
    _clearSelection();
    if (window.ulBulkToolbar) window.ulBulkToolbar.clear("pins");
}
window.exitSelectMode = exitSelectMode;

// Root and child (sub) pin markers/data live in separate registries - these
// helpers resolve either kind so selection works the same for both.
function _anyMarker(uuid: string): L.Marker | undefined {
    return _markerMap.get(uuid) || _childMarkerMap.get(uuid);
}
function _anyPinData(uuid: string): PinData | undefined {
    return _pinStore.get(uuid) || _childPinStore.get(uuid);
}

function _togglePinSelection(uuid: string): void {
    if (_selectedPinUuids.has(uuid)) {
        _selectedPinUuids.delete(uuid);
    } else {
        _selectedPinUuids.add(uuid);
    }
    const marker = _anyMarker(uuid);
    marker?.getElement()?.classList.toggle("is-selected", _selectedPinUuids.has(uuid));
    _renderBulkSelectToolbar();
    _refreshBulkMergeDialogIfOpen();
}

function _clearSelection(): void {
    _selectedPinUuids.forEach(function (uuid) {
        _anyMarker(uuid)?.getElement()?.classList.remove("is-selected");
    });
    _selectedPinUuids.clear();
    _renderBulkSelectToolbar();
    _refreshBulkMergeDialogIfOpen();
}

// Marker DOM elements are recreated whenever the cluster layer regroups
// (zoom, spiderfy), dropping the .is-selected class - re-stamp it so the
// selection highlight survives any map interaction.
function _syncSelectionClasses(): void {
    if (!_selectedPinUuids.size) return;
    _selectedPinUuids.forEach(function (uuid) {
        _anyMarker(uuid)?.getElement()?.classList.add("is-selected");
    });
}
clusterGroup.on("animationend spiderfied unspiderfied layeradd", _syncSelectionClasses);
map.on("zoomend moveend", function () {
    requestAnimationFrame(_syncSelectionClasses);
});

function _renderBulkSelectToolbar(): void {
    if (!window.ulBulkToolbar) return;
    const n = _selectedPinUuids.size;
    // Select mode itself stays on regardless of count (see
    // enterSelectMode/exitSelectMode) - only the shared bar's visibility
    // and per-button availability track the selection. Merge needs at
    // least 2 pins; everything else needs at least 1. "deselect" doubles
    // as "exit select mode" here, same as every other page's bulk bar.
    window.ulBulkToolbar.sync("pins", n, {
        add_to_list: n >= 1 ? _openAddToListForSelection : null,
        merge: n >= 2 ? openBulkMergeDialog : null,
        edit: n >= 1 ? openBulkEditDialog : null,
        export: n >= 1
            ? function () {
                  (document.getElementById("bulk-export-dialog") as HTMLDialogElement).showModal();
              }
            : null,
        delete: n >= 1 ? openBulkDeleteDialog : null,
        deselect: exitSelectMode,
    });
}

// Submit the hidden bulk-export form for the current selection in the
// chosen format - a real form POST (not fetch) so the browser downloads
// the response via its own Content-Disposition handling.
function _exportSelection(format: string): void {
    const uuids = Array.from(_selectedPinUuids);
    if (!uuids.length) return;
    (document.getElementById("bulk-export-format-field") as HTMLInputElement).value = format;
    const uuidFieldsContainer = document.getElementById("bulk-export-uuid-fields")!;
    uuidFieldsContainer.innerHTML = "";
    uuids.forEach(function (u) {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "uuids";
        input.value = u;
        uuidFieldsContainer.appendChild(input);
    });
    (document.getElementById("bulk-export-form") as HTMLFormElement).submit();
    (document.getElementById("bulk-export-dialog") as HTMLDialogElement).close();
}
window._exportSelection = _exportSelection;

// Add every currently-selected pin to a list, via the same "Add to a List"
// picker the pin popup and the filtered sidebar list use.
function _openAddToListForSelection(): void {
    const ids = Array.from(_selectedPinUuids)
        .map((uuid) => _anyPinData(uuid)?.id)
        .filter((id): id is number => id != null);
    if (!ids.length) return;
    openAddToListDialog(ids);
}

function _selectedPinName(uuid: string): string {
    const pin = _anyPinData(uuid);
    return (pin && (pin.name || pin.address)) || "Untitled pin";
}

// -- Rectangle drag-select: pointerdown-move-up on the map container while
// select mode is active, so mouse, touch and pen all run the same path. A
// move threshold keeps a plain click/tap on empty map space from being
// treated as a (zero-size) drag-select. ---------------------------------
(function _initSelectDragRectangle() {
    const container = map.getContainer();
    container.addEventListener("pointerdown", function (e) {
        // A second finger belongs to a pinch, not to a second rectangle.
        if (!_selectMode || !e.isPrimary || e.button !== 0) return;
        const startLL = map.mouseEventToLatLng(e);
        const startX = e.clientX;
        const startY = e.clientY;
        // A finger wanders further than a mouse does while pressing, so a
        // coarse pointer has to travel further before this reads as a drag.
        const threshold = e.pointerType === "mouse" ? 6 : 12;
        let dragging = false;

        // Select mode leaves map dragging on so touch users can still move
        // the map; it is off only for the length of this gesture. Pointerdown
        // runs ahead of Leaflet's own mousedown/touchstart, so the map cannot
        // pan even for the first pixel of a rubber band.
        map.dragging.disable();

        function cleanup(): void {
            container.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", onUp);
            window.removeEventListener("pointercancel", onCancel);
            if (_dragSelectRect) {
                map.removeLayer(_dragSelectRect);
                _dragSelectRect = null;
            }
            map.dragging.enable();
        }
        function onMove(ev: PointerEvent): void {
            if (ev.pointerId !== e.pointerId) return;
            if (!dragging && Math.hypot(ev.clientX - startX, ev.clientY - startY) < threshold) return;
            if (!dragging) {
                dragging = true;
                // Captured only once the gesture is definitely a drag: capturing
                // on pointerdown would retarget the click to the container and
                // tapping a pin would stop toggling its selection.
                container.setPointerCapture(e.pointerId);
            }
            // The rectangle owns the gesture now - the browser must not take
            // it for a scroll.
            if (ev.pointerType !== "mouse") ev.preventDefault();
            if (_dragSelectRect) map.removeLayer(_dragSelectRect);
            _dragSelectRect = L.rectangle(L.latLngBounds(startLL, map.mouseEventToLatLng(ev)), {
                color: "#1E88E5",
                weight: 2,
                fillOpacity: 0.08,
                dashArray: "4 4",
                interactive: false,
            }).addTo(map);
        }
        function onUp(ev: PointerEvent): void {
            if (ev.pointerId !== e.pointerId) return;
            cleanup();
            if (!dragging) return;
            const bounds = L.latLngBounds(startLL, map.mouseEventToLatLng(ev));
            function selectInBounds(marker: L.Marker, uuid: string): void {
                if (!_selectedPinUuids.has(uuid) && bounds.contains(marker.getLatLng())) _togglePinSelection(uuid);
            }
            _markerMap.forEach(selectInBounds);
            if (_childPinsActive) _childMarkerMap.forEach(selectInBounds);
        }
        function onCancel(ev: PointerEvent): void {
            if (ev.pointerId === e.pointerId) cleanup();
        }

        container.addEventListener("pointermove", onMove);
        // The end of the gesture is watched on the window: until the capture
        // above is taken, a release outside the map would never reach the
        // container and would strand map dragging in its disabled state.
        window.addEventListener("pointerup", onUp);
        window.addEventListener("pointercancel", onCancel);
    });
})();

// -- Bulk delete + undo -------------------------------------------------
function openBulkDeleteDialog(): void {
    const uuids = Array.from(_selectedPinUuids);
    if (!uuids.length) return;
    const n = uuids.length;
    document.getElementById("bulk-delete-warning")!.textContent =
        `Delete ${n} pin${n === 1 ? "" : "s"}? This also removes ${n === 1 ? "its" : "their"} reviews, visit history, notes, and map annotations - those are not recoverable by Undo.`;
    document.getElementById("bulk-delete-pin-list")!.innerHTML = uuids
        .map(function (uuid) {
            return `<li>${_escHtml(_selectedPinName(uuid))}</li>`;
        })
        .join("");
    const phrase = `delete ${n} pin${n === 1 ? "" : "s"}`;
    document.getElementById("bulk-delete-confirm-label")!.textContent = `Type "${phrase}" to confirm`;
    const input = document.getElementById("bulk-delete-confirm-input") as HTMLInputElement;
    input.value = "";
    const confirmBtn = document.getElementById("bulk-delete-confirm-btn") as HTMLButtonElement;
    confirmBtn.disabled = true;
    input.oninput = function () {
        confirmBtn.disabled = input.value !== phrase;
    };
    confirmBtn.onclick = function () {
        _submitBulkDelete(uuids);
    };
    (document.getElementById("bulk-delete-dialog") as HTMLDialogElement).showModal();
}
window.openBulkDeleteDialog = openBulkDeleteDialog;

interface BulkDeleteResponse {
    count: number;
    descendant_count: number;
    undo_token: string;
}

function _submitBulkDelete(uuids: string[]): void {
    const confirmBtn = document.getElementById("bulk-delete-confirm-btn") as HTMLButtonElement;
    confirmBtn.disabled = true;
    // sendJson carries the server's own refusal text into the catch below;
    // the bare fetch this replaces threw an empty Error, so a refusal the
    // server had explained (too many pins selected, say) reached the user as
    // an unexplained "Failed to delete pins."
    _sendJson<BulkDeleteResponse>(MAP_CFG.urls.pinBulkDelete, "POST", { uuids: uuids })
        .then(function (data) {
            (document.getElementById("bulk-delete-dialog") as HTMLDialogElement).close();
            uuids.forEach(function (uuid) {
                if (_markerMap.has(uuid)) {
                    clusterGroup.removeLayer(_markerMap.get(uuid)!);
                    _markerMap.delete(uuid);
                }
                if (_childMarkerMap.has(uuid)) {
                    clusterGroup.removeLayer(_childMarkerMap.get(uuid)!);
                    _childMarkerMap.delete(uuid);
                }
                _pinStore.delete(uuid);
                _childPinStore.delete(uuid);
            });
            _totalPins = _pinStore.size;
            exitSelectMode();
            _showUndoDeleteToast(data.count, data.descendant_count, data.undo_token);
        })
        .catch(function (err) {
            toastr.error((err && err.message) || "Failed to delete pins.");
            confirmBtn.disabled = false;
        });
}

function _showUndoDeleteToast(count: number, descendantCount: number, token: string): void {
    let label = `${count} pin${count === 1 ? "" : "s"}`;
    if (descendantCount > 0) {
        label += ` (+${descendantCount} child pin${descendantCount === 1 ? "" : "s"})`;
    }
    const msg = `${label} deleted. ` + `<button type="button" class="toast-undo-btn" onclick="_undoBulkDelete('${token}', this)">Undo</button>`;
    toastr.success(msg, "", { timeOut: 10000, extendedTimeOut: 4000, closeButton: true, tapToDismiss: false });
}

function _undoBulkDelete(token: string, btn: HTMLButtonElement | null): void {
    if (btn) btn.disabled = true;
    fetch(MAP_CFG.urls.pinBulkUndo, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": MAP_CFG.csrfToken },
        body: JSON.stringify({ token: token }),
    })
        .then(function (r) {
            if (!r.ok) throw new Error();
            return r.json();
        })
        .then(function () {
            toastr.clear();
            toastr.success("Pins restored.");
            _refreshAllPins();
        })
        .catch(function () {
            toastr.error("Could not undo - the delete may have expired.");
        });
}
window._undoBulkDelete = _undoBulkDelete;

// -- Merge ----------------------------------------------------------------
let _bulkMergeTargetUuid: string | null = null;

function _pinCardIconHtml(pin: PinData | undefined): string {
    if (!pin || !pin.icon) return '<i class="material-icons tag-icon-empty">place</i>';
    if (/^[a-z_]+$/.test(pin.icon)) return `<i class="material-icons">${_escHtml(pin.icon)}</i>`;
    if (/^(https?:\/\/|\/)/.test(pin.icon)) return `<img src="${_escHtml(pin.icon)}" style="width:24px;height:24px;object-fit:cover;border-radius:4px;" alt="">`;
    return `<span class="tag-icon-emoji">${_escHtml(pin.icon)}</span>`;
}

function _pinMiniCardHtml(uuid: string, isTarget: boolean): string {
    const pin = _anyPinData(uuid);
    const safe = _safePinColor(pin?.color);
    const style = safe ? `color:${safe}` : "";
    const swapBtn = isTarget ? "" : `<button type="button" class="cat-merge-swap-btn" data-swap-uuid="${uuid}" title="Make this the top-level pin"><i class="material-symbols-outlined">swap_vert</i></button>`;
    const removeBtn = `<button type="button" class="cat-merge-remove-btn" data-remove-uuid="${uuid}" title="Remove from this merge (also deselects it on the map)"><i class="material-symbols-outlined">close</i></button>`;
    return (
        `<div class="cat-merge-mini-card${isTarget ? " cat-merge-mini-card--target" : ""}" data-merge-uuid="${uuid}">` +
        `<div class="tag-card-icon cat-merge-mini-icon" style="${style}">${_pinCardIconHtml(pin)}</div>` +
        `<div class="cat-merge-mini-info"><div class="cat-merge-mini-name">${_escHtml(_selectedPinName(uuid))}</div></div>` +
        swapBtn +
        removeBtn +
        `</div>`
    );
}

function _renderBulkMergeDialog(): void {
    const uuids = Array.from(_selectedPinUuids);
    if (!_bulkMergeTargetUuid || !_selectedPinUuids.has(_bulkMergeTargetUuid)) _bulkMergeTargetUuid = uuids[0] ?? null;
    const sources = uuids.filter(function (u) {
        return u !== _bulkMergeTargetUuid;
    });
    document.getElementById("bulk-merge-dialog-title")!.textContent = `Merge ${uuids.length} pin${uuids.length === 1 ? "" : "s"}`;
    document.getElementById("bulk-merge-target-card")!.innerHTML = _bulkMergeTargetUuid ? _pinMiniCardHtml(_bulkMergeTargetUuid, true) : "";
    document.getElementById("bulk-merge-sources-list")!.innerHTML = sources.length
        ? sources
              .map(function (u) {
                  return _pinMiniCardHtml(u, false);
              })
              .join("")
        : '<p class="cat-merge-empty-hint">Select more pins on the map to add them to this merge.</p>';
    const confirmBtn = document.getElementById("bulk-merge-confirm-btn") as HTMLButtonElement;
    if (uuids.length >= 2 && _bulkMergeTargetUuid) {
        confirmBtn.innerHTML = `<i class="material-symbols-outlined">merge</i> Merge into ${_escHtml(_selectedPinName(_bulkMergeTargetUuid))}`;
        confirmBtn.disabled = false;
    } else {
        confirmBtn.innerHTML = '<i class="material-symbols-outlined">merge</i> Merge';
        confirmBtn.disabled = true;
    }
    _setMergeTargetHighlight(_bulkMergeTargetUuid);
}

// -- Merge target/hover highlight sync (map <-> list) ----------------------
// Same UX as the trip page's map<->list hover-sync: the target pin is
// enlarged and highlighted on the map so it's unmistakable, and hovering
// either a marker or its mini-card in the dialog highlights the other, both
// ways.
let _mergeTargetHighlightUuid: string | null = null;

function _mergeDialogIsOpen(): boolean {
    const dlg = document.getElementById("bulk-merge-dialog") as HTMLDialogElement | null;
    return !!(dlg && dlg.open);
}

function _setMergeTargetHighlight(uuid: string | null): void {
    if (_mergeTargetHighlightUuid === uuid) return;
    if (_mergeTargetHighlightUuid) _anyMarker(_mergeTargetHighlightUuid)?.getElement()?.classList.remove("is-merge-target");
    _mergeTargetHighlightUuid = uuid || null;
    if (uuid) _anyMarker(uuid)?.getElement()?.classList.add("is-merge-target");
}

function _mergeHoverMiniCard(uuid: string, on: boolean): void {
    document.querySelector(`.cat-merge-mini-card[data-merge-uuid="${uuid}"]`)?.classList.toggle("cat-merge-mini-card--hover", on);
}

// List (mini-card) -> map: hovering a card in the merge dialog highlights its marker.
document.getElementById("bulk-merge-dialog")!.addEventListener("mouseover", function (e) {
    const card = (e.target as HTMLElement).closest<HTMLElement>(".cat-merge-mini-card");
    if (card) _anyMarker(card.dataset.mergeUuid!)?.getElement()?.classList.add("is-merge-hover");
});
document.getElementById("bulk-merge-dialog")!.addEventListener("mouseout", function (e) {
    const card = (e.target as HTMLElement).closest<HTMLElement>(".cat-merge-mini-card");
    if (card) _anyMarker(card.dataset.mergeUuid!)?.getElement()?.classList.remove("is-merge-hover");
});

// Clear all highlight state on close (Cancel, backdrop/Esc, or a
// successful merge) - the mouse may have left a marker/card without a
// mouseout firing (e.g. the dialog closing programmatically), so sweep
// every selected pin's marker rather than relying on paired on/off events.
document.getElementById("bulk-merge-dialog")!.addEventListener("close", function () {
    _setMergeTargetHighlight(null);
    _selectedPinUuids.forEach(function (uuid) {
        _anyMarker(uuid)?.getElement()?.classList.remove("is-merge-hover");
    });
});

// Marker DOM elements are recreated whenever the cluster layer regroups
// (zoom, spiderfy), dropping the .is-merge-target class - re-stamp it,
// same treatment _syncSelectionClasses gives .is-selected above.
function _syncMergeTargetClass(): void {
    if (!_mergeTargetHighlightUuid || !_mergeDialogIsOpen()) return;
    _anyMarker(_mergeTargetHighlightUuid)?.getElement()?.classList.add("is-merge-target");
}
clusterGroup.on("animationend spiderfied unspiderfied layeradd", _syncMergeTargetClass);
map.on("zoomend moveend", function () {
    requestAnimationFrame(_syncMergeTargetClass);
});

// Keep the open merge dialog mirrored to the map selection - in docked
// (side) mode the map stays interactive, so users can keep clicking pins
// to grow or shrink the merge list while the dialog is open.
function _refreshBulkMergeDialogIfOpen(): void {
    const dlg = document.getElementById("bulk-merge-dialog") as HTMLDialogElement | null;
    if (!dlg || !dlg.open) return;
    if (!_selectedPinUuids.size) {
        dlg.close();
        return;
    }
    _renderBulkMergeDialog();
}

// Pan/zoom so every selected pin stays visible next to the docked dialog.
function _fitMapToSelection(dlg: HTMLElement | null): void {
    const latLngs: L.LatLng[] = [];
    _selectedPinUuids.forEach(function (uuid) {
        const m = _markerMap.get(uuid);
        if (m) latLngs.push(m.getLatLng());
    });
    if (!latLngs.length) return;
    _programmaticMove = true;
    const dialogWidth = ((dlg && dlg.getBoundingClientRect().width) || 380) + 48;
    map.fitBounds(L.latLngBounds(latLngs), {
        paddingTopLeft: [60, 60],
        // Right padding clears the docked dialog; bottom clears the select toolbar.
        paddingBottomRight: [dialogWidth, 110],
        maxZoom: 17,
    });
}

function openBulkMergeDialog(): void {
    if (_selectedPinUuids.size < 2) return;
    _bulkMergeTargetUuid = null;
    _renderBulkMergeDialog();
    const dlg = document.getElementById("bulk-merge-dialog") as HTMLDialogElement;
    // Wide screens: dock the dialog to the side (non-modal) and keep every
    // selected pin in view; small screens: centered modal as before.
    const docked = window.innerWidth >= 1100;
    dlg.classList.toggle("bulk-merge-dialog--docked", docked);
    if (docked) {
        dlg.show();
        requestAnimationFrame(function () {
            _fitMapToSelection(dlg);
        });
    } else {
        dlg.showModal();
    }
}
window.openBulkMergeDialog = openBulkMergeDialog;

document.getElementById("bulk-merge-dialog")!.addEventListener("click", function (e) {
    const removeBtn = (e.target as HTMLElement).closest<HTMLElement>(".cat-merge-remove-btn");
    if (removeBtn) {
        const uuid = removeBtn.dataset.removeUuid!;
        // Removing from the merge list deselects the pin on the map too -
        // the merge list *is* the selection.
        if (_selectedPinUuids.has(uuid)) _togglePinSelection(uuid);
        return;
    }
    const swapBtn = (e.target as HTMLElement).closest<HTMLElement>(".cat-merge-swap-btn");
    if (swapBtn) {
        _bulkMergeTargetUuid = swapBtn.dataset.swapUuid!;
        _renderBulkMergeDialog();
    }
});

interface BulkMergeResponse {
    reparented?: boolean;
}

document.getElementById("bulk-merge-confirm-btn")!.addEventListener("click", function (this: HTMLButtonElement) {
    const uuids = Array.from(_selectedPinUuids);
    const sources = uuids.filter(function (u) {
        return u !== _bulkMergeTargetUuid;
    });
    const btn = this;
    const saved = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="cat-merge-spinner"></span> Merging...';
    _sendJson(MAP_CFG.urls.pinBulkMerge, "POST", { target_uuid: _bulkMergeTargetUuid, source_uuids: sources })
        .then(function () {
            (document.getElementById("bulk-merge-dialog") as HTMLDialogElement).close();
            exitSelectMode();
            _refreshAllPins();
            // Merged sources become child pins of the target and stop appearing as
            // root-level markers - without this they'd look like they vanished.
            // Turn the Child Pins layer on (if it wasn't already) so they're still
            // visible immediately after the merge.
            if (_childPinsActive) {
                _loadChildPins();
                toastr.success("Pins merged.");
            } else {
                _mapLayers!.toggleCustom("childpins");
                toastr.success("Pins merged. They're now shown as child pins on the map (Layers → Child Pins).");
            }
        })
        .catch(function (err) {
            toastr.error((err && err.message) || "Merge failed.");
            btn.disabled = false;
            btn.innerHTML = saved;
        });
});

// -- Bulk edit: description replace + label add/remove --------------------
// Thin id-based wrapper over the shared chip picker (ts/shared/label-picker.ts) -
// the same component the saved-filter pages used to duplicate.
function _makeLabelChipPicker(chipsElId: string, searchElId: string, suggElId: string, tabsElId?: string): ChipPickerApi {
    return createChipPicker({
        chipsEl: document.getElementById(chipsElId)!,
        searchEl: document.getElementById(searchElId) as HTMLInputElement,
        suggEl: document.getElementById(suggElId)!,
        tabsEl: tabsElId ? (document.getElementById(tabsElId) ?? undefined) : undefined,
    });
}

const _bulkEditAddPicker = _makeLabelChipPicker("bulk-edit-add-label-chips", "bulk-edit-add-label-search", "bulk-edit-add-label-suggestions", "bulk-edit-add-label-tabs");
const _bulkEditRemovePicker = _makeLabelChipPicker("bulk-edit-remove-label-chips", "bulk-edit-remove-label-search", "bulk-edit-remove-label-suggestions", "bulk-edit-remove-label-tabs");

// -- Bulk edit: set parent pin (single-pick search, not a label chip set) --
interface ParentSearchResult {
    uuid: string;
    name: string;
    subtitle?: string;
}

interface BulkEditParentPicker {
    reset: () => void;
    getSelectedUuid: () => string | null;
}

const _bulkEditParentPicker: BulkEditParentPicker = (function () {
    const chipEl = document.getElementById("bulk-edit-parent-chip")!;
    const searchEl = document.getElementById("bulk-edit-parent-search") as HTMLInputElement;
    const suggEl = document.getElementById("bulk-edit-parent-suggestions")!;
    const inputWrap = document.getElementById("bulk-edit-parent-input-wrap")!;
    let selected: { uuid: string; name: string } | null = null;
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;

    function renderChip(): void {
        chipEl.innerHTML = "";
        inputWrap.hidden = !!selected;
        if (!selected) return;
        const chip = document.createElement("span");
        chip.className = "apdlg-label-chip-item";
        chip.innerHTML = `<span class="apdlg-chip-name">${_escHtml(selected.name)}</span><button class="apdlg-chip-remove" type="button" aria-label="Remove">x</button>`;
        chip.querySelector(".apdlg-chip-remove")!.addEventListener("click", function () {
            selected = null;
            renderChip();
        });
        chipEl.appendChild(chip);
    }

    function renderSuggestions(list: ParentSearchResult[]): void {
        if (!list.length) {
            suggEl.hidden = true;
            suggEl.innerHTML = "";
            return;
        }
        suggEl.innerHTML = "";
        list.forEach(function (r) {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "apdlg-label-sugg-item";
            const subtitle = r.subtitle ? ` <span class="cat-merge-empty-hint">${_escHtml(r.subtitle)}</span>` : "";
            item.innerHTML = `<span class="apdlg-sugg-name">${_escHtml(r.name)}</span>${subtitle}`;
            item.addEventListener("click", function () {
                selected = { uuid: r.uuid, name: r.name };
                renderChip();
                searchEl.value = "";
                suggEl.hidden = true;
            });
            suggEl.appendChild(item);
        });
        suggEl.hidden = false;
    }

    searchEl.addEventListener("input", function () {
        if (debounceTimer) clearTimeout(debounceTimer);
        const q = searchEl.value.trim();
        if (q.length < 2) {
            suggEl.hidden = true;
            return;
        }
        debounceTimer = setTimeout(function () {
            const params = new URLSearchParams({ q: q });
            _selectedPinUuids.forEach(function (u) {
                params.append("exclude", u);
            });
            fetch("" + MAP_CFG.urls.pinParentSearch + "?" + params.toString())
                .then(function (r) {
                    return r.ok ? (r.json() as Promise<{ results?: ParentSearchResult[] }>) : { results: [] };
                })
                .then(function (data) {
                    renderSuggestions(data.results || []);
                })
                .catch(function () {
                    suggEl.hidden = true;
                });
        }, 250);
    });
    searchEl.addEventListener("blur", function () {
        setTimeout(function () {
            suggEl.hidden = true;
        }, 150);
    });

    return {
        reset: function () {
            selected = null;
            renderChip();
            searchEl.value = "";
            suggEl.hidden = true;
        },
        getSelectedUuid: function () {
            return selected ? selected.uuid : null;
        },
    };
})();

function openBulkEditDialog(): void {
    const uuids = Array.from(_selectedPinUuids);
    if (!uuids.length) return;
    document.getElementById("bulk-edit-dialog-title")!.textContent = `Edit ${uuids.length} pins`;
    (document.getElementById("bulk-edit-description-value") as HTMLTextAreaElement).value = "";
    (document.getElementById("bulk-edit-description-nochange") as HTMLInputElement).checked = true;
    (document.getElementById("bulk-edit-rating-value") as HTMLInputElement).value = "";
    _bulkEditAddPicker.reset();
    _bulkEditAddPicker.setCandidates(_apdlgAllLabels.map((l) => ({ ...l, id: String(l.id) })));
    _bulkEditRemovePicker.reset();
    _bulkEditRemovePicker.setCandidates([]);
    _bulkEditParentPicker.reset();
    const removeHint = document.getElementById("bulk-edit-remove-label-hint")!;
    removeHint.textContent = "Loading...";
    fetch("" + MAP_CFG.urls.pinBulkEditLabelOptions + "?" + uuids.map((u) => "uuids=" + encodeURIComponent(u)).join("&"))
        .then(function (r) {
            return r.ok ? (r.json() as Promise<{ labels?: LabelCandidate[] }>) : { labels: [] };
        })
        .then(function (data) {
            _bulkEditRemovePicker.setCandidates((data.labels || []).map((l) => ({ ...l, id: String(l.id) })));
            removeHint.textContent = "Only labels present on at least one selected pin are offered here.";
        })
        .catch(function () {
            removeHint.textContent = "Only labels present on at least one selected pin are offered here.";
        });
    (document.getElementById("bulk-edit-dialog") as HTMLDialogElement).showModal();
}
window.openBulkEditDialog = openBulkEditDialog;

interface BulkEditPayload {
    uuids: string[];
    description?: string;
    rating?: number;
    add_label_ids: string[];
    remove_label_ids: string[];
    parent_uuid?: string;
}

document.getElementById("bulk-edit-confirm-btn")!.addEventListener("click", function (this: HTMLButtonElement) {
    const uuids = Array.from(_selectedPinUuids);
    if (!uuids.length) return;
    const btn = this;
    const saved = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="cat-merge-spinner"></span> Saving...';
    const payload: BulkEditPayload = { uuids: uuids, add_label_ids: _bulkEditAddPicker.getSelectedIds(), remove_label_ids: _bulkEditRemovePicker.getSelectedIds() };
    if (!(document.getElementById("bulk-edit-description-nochange") as HTMLInputElement).checked) {
        payload.description = (document.getElementById("bulk-edit-description-value") as HTMLTextAreaElement).value;
    }
    const ratingValue = (document.getElementById("bulk-edit-rating-value") as HTMLInputElement).value;
    if (ratingValue !== "") payload.rating = Number.parseInt(ratingValue, 10);
    const parentUuid = _bulkEditParentPicker.getSelectedUuid();
    if (parentUuid) payload.parent_uuid = parentUuid;
    _sendJson<BulkMergeResponse>(MAP_CFG.urls.pinBulkEdit, "POST", payload)
        .then(function (data) {
            (document.getElementById("bulk-edit-dialog") as HTMLDialogElement).close();
            toastr.success("Pins updated.");
            exitSelectMode();
            _refreshAllPins();
            // Pins reparented via this dialog become child pins and stop appearing
            // as root-level markers - refresh the Child Pins layer so they don't
            // look like they vanished (same treatment as bulk merge).
            if (data.reparented) {
                if (_childPinsActive) _loadChildPins();
                else _mapLayers!.toggleCustom("childpins");
            }
        })
        .catch(function (err) {
            toastr.error((err && err.message) || "Update failed.");
        })
        .finally(function () {
            btn.disabled = false;
            btn.innerHTML = saved;
        });
});

// -- Keyboard shortcuts (only when map is visible, not in input fields) ------
document.addEventListener("keydown", function (e) {
    // Skip if focus is inside a text/date input or textarea
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tag)) return;
    // Skip if a dialog is open
    if (document.querySelector("dialog[open]")) return;
    // Skip if map container isn't in viewport
    const mapEl = document.getElementById("map");
    if (!mapEl) return;
    const rect = mapEl.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    const key = e.key.toLowerCase();
    // Cmd/Ctrl combos reserved for browser - don't intercept
    if (e.metaKey || e.ctrlKey) return;

    switch (key) {
        case "f": {
            // F - toggle filter panel; focus name search when opening
            const fp = document.getElementById("filter-panel")!;
            if (fp.classList.contains("open")) {
                toggleFilterPanel();
            } else {
                toggleFilterPanel();
                setTimeout(() => document.getElementById("fp-search")?.focus(), 50);
            }
            break;
        }
        case "j": // J - Jump to location
            document.getElementById("addr-search-input")?.focus();
            break;
        case "p":
            _mapLayers!.toggleCustom("pins");
            break; // P - Toggle pins
        case "b":
            _mapLayers!.toggleBorders();
            break; // B - Toggle borders
        case "l":
            _mapLayers!.togglePanel();
            break; // L - Layers
        case "s":
            _mapLayers!.toggleBase("satellite");
            break; // S - Satellite view
        case "t":
            _mapLayers!.toggleBase("topographic");
            break; // T - Topography view
        case "w":
            _mapLayers!.toggleWeather();
            break; // W - Weather view
        case "d":
            _mapLayers!.toggleDark();
            break; // D - Dark mode
        case "escape":
            if (_selectMode) {
                exitSelectMode();
            } else if (document.getElementById("filter-panel")?.classList.contains("open")) {
                toggleFilterPanel();
            } else if (document.getElementById("pin-list-panel")?.classList.contains("open")) {
                _togglePinListPanel();
            } else if (_mapLayers!.isPanelOpen()) {
                _mapLayers!.closePanel();
            }
            break;
    }
});

// -- Filter panel ------------------------------------------------------
function toggleFilterPanel(): void {
    const panel = document.getElementById("filter-panel")!;
    const btn = document.getElementById("search-pins-button")!;
    const handle = document.getElementById("filter-panel-handle");
    const mapEl = document.getElementById("map")!;
    const isOpen = panel.classList.toggle("open");
    btn.classList.toggle("active", isOpen);
    mapEl.classList.toggle("filter-open", isOpen);
    if (handle) {
        handle.classList.toggle("open", isOpen);
        handle.setAttribute("aria-expanded", String(isOpen));
        const icon = handle.querySelector(".material-symbols-outlined, .material-icons");
        if (icon) icon.textContent = isOpen ? "chevron_right" : "chevron_left";
    }
    if (isOpen) requestAnimationFrame(_autoFitFilterSections);
}
window.toggleFilterPanel = toggleFilterPanel;

// Measures the filter panel's actual available height vs. its fully-expanded
// content height, and collapses accordion sections (least important first)
// until it fits - rather than guessing from fixed window-height breakpoints.
// Panels carry data-importance="high|medium|low"; new panels just need the
// attribute, no code change here.
function _autoFitFilterSections(): void {
    const panel = document.getElementById("filter-panel");
    const form = document.getElementById("filter-form") as HTMLFormElement | null;
    const header = panel?.querySelector<HTMLElement>(".fp-header");
    if (!panel || !form || !header || !panel.classList.contains("open")) return;

    const sections = Array.from(form.querySelectorAll<HTMLDetailsElement>(".fp-accordion-item[data-importance]"));
    if (!sections.length) return;

    // Open everything first so scrollHeight reflects the true natural content height.
    sections.forEach((el) => {
        el.open = true;
    });

    const formStyle = getComputedStyle(form);
    const vPad = Number.parseFloat(formStyle.paddingTop || "0") + Number.parseFloat(formStyle.paddingBottom || "0");
    const available = panel.clientHeight - header.offsetHeight - vPad;
    if (available <= 0) return; // panel not laid out yet

    if (form.scrollHeight <= available) return; // everything fits, nothing to collapse

    // Collapse least-important-first; high importance is the last resort.
    const order: Record<string, number> = { low: 0, medium: 1, high: 2 };
    const collapsible = sections.filter((el) => el.dataset.importance! in order).sort((a, b) => order[a.dataset.importance!]! - order[b.dataset.importance!]!);

    for (const el of collapsible) {
        if (form.scrollHeight <= available) break;
        el.open = false;
    }
    // If it still overflows after collapsing everything, #filter-form's own
    // overflow-y:auto lets the user scroll as a last resort.
}

// -- Pin list panel ------------------------------------------------------
function _togglePinListPanel(): void {
    const panel = document.getElementById("pin-list-panel")!;
    const btn = document.getElementById("pin-list-button");
    const handle = document.getElementById("pin-list-handle");
    const mapEl = document.getElementById("map")!;
    const isOpen = panel.classList.toggle("open");
    btn?.classList.toggle("active", isOpen);
    mapEl.classList.toggle("pin-list-open", isOpen);
    if (handle) {
        handle.classList.toggle("open", isOpen);
        handle.setAttribute("aria-expanded", String(isOpen));
        const icon = handle.querySelector(".material-icons");
        if (icon) icon.textContent = isOpen ? "chevron_left" : "chevron_right";
    }
    if (isOpen) _refreshPinList();
}
window._togglePinListPanel = _togglePinListPanel;

// The pin list sidebar's page size adapts to however many .pin-list-item
// rows actually fit in the scrollable body without a scrollbar, instead of
// a fixed count - row height varies with label/meta content, so this is
// measured from the actually-rendered DOM after each fetch rather than
// computed up front from CSS alone. Seeded with the server's own default
// (matches _PIN_LIST_PAGE_SIZE) and refined once real rows exist to
// measure.
let _pinListPageSize = 25;
const _PIN_LIST_MIN_PAGE_SIZE = 5;
const _PIN_LIST_MAX_PAGE_SIZE = 100;
let _pinListAutoAdjusting = false;
let _pinListResizeTimer: ReturnType<typeof setTimeout> | undefined;

function _refreshPinList(): void {
    const panel = document.getElementById("pin-list-panel");
    if (!panel || !panel.classList.contains("open")) return;
    const form = document.getElementById("filter-form") as HTMLFormElement;
    const params = new URLSearchParams(new FormData(form) as unknown as Record<string, string>);
    // Scopes the sidebar's list (and its total count) to pins within the
    // map's current viewport - kept as a plain query param here rather
    // than a #filter-form field, since #filter-form is also read by
    // map.search and "add these pins to a list" (addPinsToList), neither
    // of which should become viewport-scoped.
    const b = map.getBounds();
    params.set("bounds", `${b.getSouth()},${b.getWest()},${b.getNorth()},${b.getEast()}`);
    params.set("page_size", String(_pinListPageSize));
    htmx.ajax("GET", `${MAP_CFG.urls.mapPinsList}?${params.toString()}`, { target: "#pin-list-body", swap: "innerHTML" });
}

// Measures the just-rendered page against the body's available height and
// updates _pinListPageSize when it's meaningfully off. Returns true when
// an adjustment was made (caller should re-fetch with the new size).
function _pinListAdjustPageSize(): boolean {
    const body = document.getElementById("pin-list-body");
    const results = document.getElementById("pin-list-results");
    const list = body?.querySelector(".pin-list");
    const items = list ? list.querySelectorAll(".pin-list-item") : [];
    if (!body || !results || !list || !items.length) return false;

    const rowHeight = list.scrollHeight / items.length;
    if (!rowHeight) return false;

    // Everything sharing the scrollable body with the <ul> itself (the
    // count line, "add to list" button, pagination bar) counts against
    // the space available for rows.
    const chromeHeight = results.scrollHeight - list.scrollHeight;
    const availableForList = body.clientHeight - chromeHeight;
    if (availableForList <= 0) return false;

    const idealCount = Math.max(_PIN_LIST_MIN_PAGE_SIZE, Math.min(_PIN_LIST_MAX_PAGE_SIZE, Math.floor(availableForList / rowHeight)));

    const totalCountEl = results.querySelector(".pin-list-count strong");
    const totalCount = totalCountEl ? Number.parseInt(totalCountEl.textContent || "0", 10) || 0 : 0;
    const isOverflowing = body.scrollHeight > body.clientHeight + 1;
    const hasRoomToGrow = items.length < totalCount && !isOverflowing;
    if (Math.abs(idealCount - _pinListPageSize) < 2 || (!isOverflowing && !hasRoomToGrow)) {
        return false;
    }

    _pinListPageSize = idealCount;
    return true;
}

document.getElementById("pin-list-body")!.addEventListener("htmx:afterSwap", (e) => {
    if ((e.target as HTMLElement).id !== "pin-list-body") return;
    if (_pinListAutoAdjusting) {
        // Second swap in this adjust-and-refetch cycle - accept it even if
        // still imperfect, rather than chasing convergence indefinitely.
        _pinListAutoAdjusting = false;
        return;
    }
    if (_pinListAdjustPageSize()) {
        _pinListAutoAdjusting = true;
        _refreshPinList();
    }
});

// Icon-less saved filters show a live "how many pins would match" count
// instead of a generic fallback icon (see _saved_filters_toolbar.html) -
// refreshed whenever the sidebar's filter criteria or the set of active
// toolbar filters changes, same trigger points as _refreshPinList().
function sfToolbarRefreshCounts(): void {
    const placeholders = document.querySelectorAll<HTMLElement>("#map-saved-filters-toolbar [data-count-placeholder]");
    if (!placeholders.length) return;
    const form = document.getElementById("filter-form") as HTMLFormElement | null;
    if (!form) return;
    const params = new URLSearchParams(new FormData(form) as unknown as Record<string, string>);
    fetch(`${MAP_CFG.urls.savedFiltersCounts}?${params.toString()}`)
        .then((r) => (r.ok ? (r.json() as Promise<{ counts: Record<string, number> }>) : null))
        .then((data) => {
            if (!data) return;
            placeholders.forEach((el) => {
                const btn = el.closest<HTMLElement>(".sf-toolbar-btn");
                const uuid = btn ? btn.dataset.filterUuid : null;
                if (uuid && Object.prototype.hasOwnProperty.call(data.counts, uuid)) {
                    el.textContent = String(data.counts[uuid]);
                }
            });
        })
        .catch(() => {});
}

function _flyToPinFromList(uuid: string, lat: number | null, lng: number | null): void {
    if (lat == null || lng == null) return;
    _programmaticMove = true;
    map.setView([lat, lng], Math.max(map.getZoom(), 16));
    // _anyMarker checks both the root (_markerMap) and child-pin
    // (_childMarkerMap) registries - a plain _markerMap lookup silently
    // found nothing (and so never opened the popup) for any sidebar
    // entry that was actually a child pin.
    setTimeout(() => {
        const m = _anyMarker(uuid);
        if (m) m.openPopup();
    }, 350);
    if (window.matchMedia("(max-width: 768px)").matches) _togglePinListPanel();
}
window._flyToPinFromList = _flyToPinFromList;

// -- Add pins to a list ----------------------------------------------------
// Three modes, chosen by what's passed to openAddToListDialog():
//  - no args: replay the active filter-form criteria (sidebar's "Add these
//    pins to a list" button, over the full unpaginated filtered set)
//  - a single pin id (string/number): the pin popup's list icon
//  - an array of pin ids: the multi-select toolbar's "Add to List" tool
let _addToListPinIds: Array<number | string> | null = null;
function openAddToListDialog(pinIdOrIds?: number | string | Array<number | string> | null): void {
    _addToListPinIds = pinIdOrIds == null ? null : Array.isArray(pinIdOrIds) ? pinIdOrIds : [pinIdOrIds];
    (document.getElementById("add-to-list-dialog") as HTMLDialogElement | null)?.showModal();
}
window.openAddToListDialog = openAddToListDialog;

function filterAddToListResults(query: string): void {
    const q = (query || "").trim().toLowerCase();
    document.querySelectorAll<HTMLElement>("#add-to-list-results li").forEach((li) => {
        li.style.display = !q || (li.dataset.search || "").includes(q) ? "" : "none";
    });
}
window.filterAddToListResults = filterAddToListResults;

function _csrfToken(): string {
    return (document.querySelector("#filter-form [name=csrfmiddlewaretoken]") as HTMLInputElement | null)?.value || "";
}

function addPinsToList(listUuid: string, confirmed?: boolean): void {
    let params: URLSearchParams;
    if (_addToListPinIds) {
        params = new URLSearchParams();
        _addToListPinIds.forEach((id) => params.append("pin_ids", String(id)));
    } else {
        const form = document.getElementById("filter-form") as HTMLFormElement;
        params = new URLSearchParams(new FormData(form) as unknown as Record<string, string>);
    }
    if (confirmed) params.set("confirmed", "true");
    const addUrl = MAP_CFG.urls.listsItemsAdd.replace("00000000-0000-0000-0000-000000000000", listUuid);
    fetch(addUrl, {
        method: "POST",
        headers: { "X-CSRFToken": _csrfToken() },
        body: params,
    }).then((response) => {
        if (response.status === 409) {
            return response.json().then((data: { count: number }) => {
                document.getElementById("list-add-confirm-text")!.textContent = `Add ${data.count} pins to this list?`;
                const dlg = document.getElementById("list-add-confirm-dialog") as HTMLDialogElement;
                document.getElementById("list-add-confirm-btn")!.addEventListener(
                    "click",
                    () => {
                        dlg.close();
                        addPinsToList(listUuid, true);
                    },
                    { once: true },
                );
                dlg.showModal();
            });
        }
        if (!response.ok) {
            toastr.error("Could not add pins to that list.");
            return;
        }
        (document.getElementById("add-to-list-dialog") as HTMLDialogElement | null)?.close();
        toastr.success("Pins added to list.");
    });
}
window.addPinsToList = addPinsToList;

function createListAndAddPins(): void {
    const nameInput = document.getElementById("add-to-list-new-name") as HTMLInputElement;
    const name = (nameInput.value || "").trim();
    if (!name) {
        nameInput.focus();
        return;
    }
    // sendJson throws on a non-2xx carrying the server's own text, which is
    // what this endpoint sends: a duplicate list name answers 409 with
    // "You already have a list with that name." A bare fetch calling
    // r.json() unconditionally would throw inside an unhandled promise on
    // that plain-text body instead - the most likely failure of this
    // feature would do nothing at all, with no message and no closed dialog.
    _sendJson<{ uuid: string }>(MAP_CFG.urls.listsCreate, "POST", { name })
        .then((data) => {
            nameInput.value = "";
            addPinsToList(data.uuid);
        })
        .catch((err) => {
            toastr.error((err && err.message) || "Could not create that list.");
        });
}
window.createListAndAddPins = createListAndAddPins;

document.getElementById("filter-form")!.addEventListener("change", () => {
    _refreshPinList();
    sfToolbarRefreshCounts();
});
let _pinListInputTimer: ReturnType<typeof setTimeout> | undefined;
document.getElementById("filter-form")!.addEventListener("input", (e) => {
    if ((e.target as HTMLElement).getAttribute("name") !== "name") return;
    if (_pinListInputTimer) clearTimeout(_pinListInputTimer);
    _pinListInputTimer = setTimeout(_refreshPinList, 600);
});

function resetFilters(): void {
    const form = document.getElementById("filter-form") as HTMLFormElement;
    form.querySelectorAll<HTMLInputElement>("input[type=text], input[type=date]").forEach((el) => {
        el.value = "";
    });
    form.querySelectorAll<HTMLInputElement>("input[type=checkbox]").forEach((el) => {
        el.checked = false;
    });
    form.querySelectorAll<HTMLInputElement>("input[type=radio]").forEach((el) => {
        el.checked = false;
    });
    form.querySelectorAll<HTMLSelectElement>("select[name]").forEach((el) => {
        el.selectedIndex = 0;
    });
    if (window.UrbanLensDualRangeSlider) {
        window.UrbanLensDualRangeSlider.resetAll(form);
    }
    // Reset visit section
    _syncVisitUI("");
    // Reset date reset buttons
    _syncVisitDateReset();
    _syncCreatedDateReset();
    _dateBuiltRange.sync();
    _dateAbandonedRange.sync();
    _lastViewedRange.sync();
    // Reset label selected area
    if (_labelPicker) _labelPicker.clear();
    // Reset a saved filter's regions, if one had been applied
    const includeRegions = document.getElementById("fp-include-regions") as HTMLInputElement | null;
    const excludeRegions = document.getElementById("fp-exclude-regions") as HTMLInputElement | null;
    if (includeRegions) includeRegions.value = "";
    if (excludeRegions) excludeRegions.value = "";
    // Clear any "applied" indicator left by applySavedFilter()
    document.querySelectorAll(".fp-saved-filter-apply--active").forEach((el) => el.classList.remove("fp-saved-filter-apply--active"));
    _exitFilterMode();
    _updateFilterLabel();
}
window.resetFilters = resetFilters;

function _countActiveFilters(): number {
    const form = document.getElementById("filter-form") as HTMLFormElement | null;
    if (!form) return 0;
    let n = 0;
    if (((form.querySelector("input[name=name]") as HTMLInputElement | null)?.value || "").trim()) n++;
    if (form.querySelector("input[type=checkbox]:checked")) n++;
    for (const el of form.querySelectorAll<HTMLInputElement>("input[type=date]")) {
        if ((el.value || "").trim()) {
            n++;
            break;
        }
    }
    // Hidden slider inputs are cleared when at full range; non-empty means active
    const seenSliders = new Set<string>();
    for (const el of form.querySelectorAll<HTMLInputElement>("input[type=hidden][data-role]")) {
        // Group min/max pairs: count the pair as one filter
        const base = el.name.replace(/^(min|max)_/, "");
        if ((el.value || "").trim() && !seenSliders.has(base)) {
            seenSliders.add(base);
            n++;
        }
    }
    // Visit radio selection stored in the separate hidden input
    const visitsHidden = form.querySelector("input[name=has_visits]") as HTMLInputElement | null;
    if ((visitsHidden?.value || "").trim()) n++;
    // Plain <select> filters: Has Links, Security Indicators, and any custom-field selects
    for (const el of form.querySelectorAll<HTMLSelectElement>("select[name]")) {
        if ((el.value || "").trim()) n++;
    }
    // Label groups
    const labelGroups = form.querySelector("input[name=label_groups]") as HTMLInputElement | null;
    if ((labelGroups?.value || "").trim()) n++;
    // Geographic regions, carried through from an applied saved filter
    for (const key of ["include_regions", "exclude_regions"]) {
        const el = form.querySelector(`input[name=${key}]`) as HTMLInputElement | null;
        if ((el?.value || "").trim()) n++;
    }
    // Bottom-right toolbar's own active saved filters (toggleToolbarSavedFilter) -
    // a SEPARATE mechanism from every field above (its state lives in
    // _activeToolbarFilters/#fp-toolbar-filter-ids, not a sidebar form field).
    // Omitting this meant a toolbar-only filter (no sidebar criteria) made
    // _hasActiveFilters() report false, so the htmx:beforeRequest handler
    // below treated it as "no filters" and skipped the server round-trip
    // entirely - the toolbar filter's client-side optimistic pass (which
    // doesn't even support every criteria type _apply_toolbar_filters does
    // server-side) was left as the only, never-reconciled, effect.
    n += _activeToolbarFilters.size;
    return n;
}

function _hasActiveFilters(): boolean {
    return _countActiveFilters() > 0;
}

function _updateFilterLabel(): void {
    const count = _countActiveFilters();
    const label = document.getElementById("fp-active-label");
    const btn = document.getElementById("search-pins-button");
    if (label) {
        if (count > 0) {
            label.textContent = String(count);
            label.hidden = false;
        } else {
            label.hidden = true;
        }
    }
    btn?.classList.toggle("has-filters", count > 0);
    const saveBtn = document.getElementById("fp-save-filter-btn");
    if (saveBtn) saveBtn.style.display = count > 0 ? "" : "none";
}

// Show the active-filter count label whenever any filter changes
document.getElementById("filter-form")!.addEventListener("change", _updateFilterLabel);
document.getElementById("filter-form")!.addEventListener("input", _updateFilterLabel);

// -- A saved filter's serialized criteria, and its extra custom-field shapes --
interface CustomFieldFilterCriterion {
    field_id: number | string;
    contains?: string;
    equals?: string;
    checked?: boolean;
    ref_id?: number | string;
    after_time?: string;
    before_time?: string;
    min?: number | string;
    max?: number | string;
    after?: string;
    before?: string;
}

interface SavedFilterCriteria {
    name?: string;
    min_rating?: number;
    max_rating?: number;
    min_priority?: number;
    max_priority?: number;
    min_danger?: number;
    max_danger?: number;
    min_vulnerability?: number;
    max_vulnerability?: number;
    min_detail_pins?: number;
    max_detail_pins?: number;
    has_visits?: string;
    visited_after?: string;
    visited_before?: string;
    created_after?: string;
    created_before?: string;
    date_built_after?: string;
    date_built_before?: string;
    date_abandoned_after?: string;
    date_abandoned_before?: string;
    last_viewed_after?: string;
    last_viewed_before?: string;
    has_links?: string;
    security_fences?: string;
    security_alarms?: string;
    security_cameras?: string;
    security_security?: string;
    security_signs?: string;
    security_vps?: string;
    security_plywood?: string;
    security_locked?: string;
    label_groups?: LabelGroup[];
    tags?: Array<number | string>;
    exclude_tags?: Array<number | string>;
    custom_fields?: CustomFieldFilterCriterion[];
    include_regions?: GeoJSON.MultiPolygon;
    exclude_regions?: GeoJSON.MultiPolygon;
}

// -- Apply a saved filter, merged additively with whatever is already active --
// Conflict rule: for a given scalar/range field, an already-active sidebar
// value always wins - the saved filter only fills in fields currently at
// their default. Labels are always UNIONED into the current included set
// (never touching excludes), regardless of whether other labels are active.
function applySavedFilter(chipEl: HTMLElement): void {
    let criteria: SavedFilterCriteria;
    try {
        criteria = JSON.parse(chipEl.dataset.criteria || "{}");
    } catch {
        return;
    }
    const form = document.getElementById("filter-form") as HTMLFormElement | null;
    if (!form) return;

    // Dual-range slider fields: each hidden input's value is empty when its
    // handle sits at the full-range default; only apply when still empty.
    const sliderFields: Record<string, "min" | "max"> = {
        min_rating: "min",
        max_rating: "max",
        min_priority: "min",
        max_priority: "max",
        min_danger: "min",
        max_danger: "max",
        min_vulnerability: "min",
        max_vulnerability: "max",
        min_detail_pins: "min",
        max_detail_pins: "max",
    };
    for (const [fieldName, role] of Object.entries(sliderFields)) {
        const value = (criteria as unknown as Record<string, unknown>)[fieldName];
        if (value == null) continue;
        const hidden = form.querySelector<HTMLInputElement>(`input[name="${fieldName}"][data-role="${role}-hidden"]`);
        if (!hidden || (hidden.value || "").trim() !== "") continue;
        const root = hidden.closest<HTMLElement>("[data-ul-dual-range-slider]");
        const slider = root ? root.querySelector<HTMLInputElement>(`[data-role="${role}"]`) : null;
        if (!slider) continue;
        slider.value = String(value);
        if (window.UrbanLensDualRangeSlider && root) window.UrbanLensDualRangeSlider.sync(root);
    }

    // Visit status (single hidden field driving the radio chips + date UI).
    if (criteria.has_visits) {
        const hiddenVisits = document.getElementById("fp-visits-hidden") as HTMLInputElement | null;
        if (hiddenVisits && !(hiddenVisits.value || "").trim()) {
            _syncVisitUI(criteria.has_visits);
        }
    }

    // Plain date inputs.
    for (const key of [
        "visited_after",
        "visited_before",
        "created_after",
        "created_before",
        "date_built_after",
        "date_built_before",
        "date_abandoned_after",
        "date_abandoned_before",
        "last_viewed_after",
        "last_viewed_before",
    ] as const) {
        const value = criteria[key];
        if (!value) continue;
        const input = form.querySelector<HTMLInputElement>(`input[name="${key}"]`);
        if (input && !(input.value || "").trim()) input.value = value;
    }
    _syncVisitDateReset();
    _syncCreatedDateReset();
    _dateBuiltRange.sync();
    _dateAbandonedRange.sync();
    _lastViewedRange.sync();

    // Plain select fields (Has Links, Security Indicators): current wins if empty.
    for (const key of ["has_links", "security_fences", "security_alarms", "security_cameras", "security_security", "security_signs", "security_vps", "security_plywood", "security_locked"] as const) {
        const value = criteria[key];
        if (!value) continue;
        const select = form.querySelector<HTMLSelectElement>(`select[name="${key}"]`);
        if (select && !(select.value || "").trim()) select.value = value;
    }

    // Labels: union saved include-ids into the current included set; never
    // touch excludes, and never auto-add a saved filter's exclusions.
    // Structured label_groups (from the map's own formula bar) takes
    // precedence; a filter saved from the Filters tab's simple
    // include/exclude picker instead stores flat tags/exclude_tags (it has
    // no formula-bar UI to produce label_groups), so that shape needs its
    // own union-into-included handling here too - otherwise a filter whose
    // only criterion is "include label X" (a very common case from that
    // dialog) merged nothing at all and looked like a dead click.
    if (Array.isArray(criteria.label_groups)) {
        const mergeIds: number[] = [];
        for (const group of criteria.label_groups) {
            if (group.op === "not") continue;
            for (const id of group.ids || []) mergeIds.push(id);
        }
        if (_labelPicker && mergeIds.length) _labelPicker.mergeIncludeIds(mergeIds);
    } else if (Array.isArray(criteria.tags) && criteria.tags.length) {
        if (_labelPicker) _labelPicker.mergeIncludeIds(criteria.tags);
    }

    // Custom fields: same "current wins if empty" rule, keyed by cf_<field_id>*.
    for (const cf of criteria.custom_fields || []) {
        if ("contains" in cf && cf.contains !== undefined) {
            const input = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}"]`);
            if (input && !(input.value || "").trim() && cf.contains) input.value = cf.contains;
        } else if ("equals" in cf && cf.equals !== undefined) {
            const select = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}"]`);
            if (select && !(select.value || "").trim() && cf.equals) select.value = cf.equals;
        } else if ("checked" in cf && cf.checked !== undefined) {
            const select = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}"]`);
            if (select && !(select.value || "").trim()) select.value = cf.checked ? "checked" : "unchecked";
        } else if ("ref_id" in cf && cf.ref_id != null) {
            const select = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}"]`);
            if (select && !(select.value || "").trim()) select.value = String(cf.ref_id);
        } else if ("after_time" in cf || "before_time" in cf) {
            const afterInput = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}_after"]`);
            const beforeInput = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}_before"]`);
            if (afterInput && !(afterInput.value || "").trim() && cf.after_time) afterInput.value = cf.after_time;
            if (beforeInput && !(beforeInput.value || "").trim() && cf.before_time) beforeInput.value = cf.before_time;
        } else if ("min" in cf || "max" in cf) {
            const minInput = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}_min"]`);
            const maxInput = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}_max"]`);
            if (minInput && !(minInput.value || "").trim() && cf.min != null) minInput.value = String(cf.min);
            if (maxInput && !(maxInput.value || "").trim() && cf.max != null) maxInput.value = String(cf.max);
        } else if ("after" in cf || "before" in cf) {
            const afterInput = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}_after"]`);
            const beforeInput = form.querySelector<HTMLInputElement>(`[name="cf_${cf.field_id}_before"]`);
            if (afterInput && !(afterInput.value || "").trim() && cf.after) afterInput.value = cf.after;
            if (beforeInput && !(beforeInput.value || "").trim() && cf.before) beforeInput.value = cf.before;
        }
    }

    // Geographic regions: current wins if empty, same rule as everything else here.
    for (const key of ["include_regions", "exclude_regions"] as const) {
        const value = criteria[key];
        if (!value) continue;
        const hidden = document.getElementById(key === "include_regions" ? "fp-include-regions" : "fp-exclude-regions") as HTMLInputElement | null;
        if (hidden && !(hidden.value || "").trim()) hidden.value = JSON.stringify(value);
    }

    // Mark the chip as applied - the merge above can't be cleanly un-applied
    // (values are written straight into shared form fields), so this stays
    // on until resetFilters() clears the whole panel rather than toggling.
    chipEl.classList.add("fp-saved-filter-apply--active");

    // Trigger the sidebar's normal submission path. The form's own 'change'
    // listener (registered above) already calls _refreshPinList() - calling
    // it again here would fire a second, unsynchronized request to the same
    // target racing the first (whichever response lands last silently wins
    // the DOM, which could show stale/empty results).
    htmx.trigger(form, "change");
    _updateFilterLabel();
}
window.applySavedFilter = applySavedFilter;

// -- Bottom-right saved-filters toolbar: independent, AND-combined toggle layer --
// Unlike applySavedFilter() above (which merges values into the sidebar form and
// can't be cleanly un-applied), toolbar filters are tracked separately and ride
// along with #filter-form via the hidden toolbar_filter_ids input, so the server
// ANDs them against the sidebar's own criteria (controllers/maps.py) without any
// of applySavedFilter()'s "only fill empty fields" merge logic.
const _activeToolbarFilters = new Map<string, SavedFilterCriteria>(); // filterUuid -> criteria
const _toolbarHiddenByFilter = new Map<string, Set<string>>(); // pinUuid -> Set<filterUuid> currently hiding it
let _sfToolbarPageIdx = 0;

function _sfPointInRing(pt: GeoJSON.Position, ring: GeoJSON.Position[]): boolean {
    let inside = false;
    const ptx = pt[0]!,
        pty = pt[1]!;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const pi = ring[i]!;
        const pj = ring[j]!;
        const xi = pi[0]!,
            yi = pi[1]!;
        const xj = pj[0]!,
            yj = pj[1]!;
        const intersect = yi > pty !== yj > pty && ptx < ((xj - xi) * (pty - yi)) / (yj - yi) + xi;
        if (intersect) inside = !inside;
    }
    return inside;
}
function _sfPointInPolygon(pt: GeoJSON.Position, polygon: GeoJSON.Position[][]): boolean {
    if (!polygon.length || !_sfPointInRing(pt, polygon[0]!)) return false;
    for (let k = 1; k < polygon.length; k++) {
        if (_sfPointInRing(pt, polygon[k]!)) return false; // inside a hole
    }
    return true;
}
function _sfPointInMultiPolygon(pt: GeoJSON.Position, geojson: GeoJSON.MultiPolygon | undefined): boolean {
    if (!geojson || !geojson.coordinates) return false;
    return geojson.coordinates.some((polygon) => _sfPointInPolygon(pt, polygon));
}

// Best-effort client-side predicate over a cached pin, using only fields the map's
// pin payload actually carries (name, rating, last_visited, label ids, lat/lng).
// Dimensions the payload doesn't carry at all today (danger, priority, vulnerability,
// visited/created date ranges, custom fields, overlapping_pins, and label-group
// ancestor/descendant expansion) are NOT evaluated here - this function only ever
// narrows on fields it's confident about, and the authoritative server response
// (triggered right alongside this) corrects anything it couldn't judge.
function _sfClientMatches(pin: PinData, criteria: SavedFilterCriteria): boolean {
    if (criteria.name) {
        const q = String(criteria.name).toLowerCase();
        if (!(pin.name || "").toLowerCase().includes(q)) return false;
    }
    const rating = Number(pin.rating || 0);
    if (criteria.min_rating != null && rating < Number(criteria.min_rating)) return false;
    if (criteria.max_rating != null && rating > Number(criteria.max_rating)) return false;
    if (criteria.has_visits === "yes" && (!pin.last_visited || pin.last_visited === "never")) return false;
    if (criteria.has_visits === "no" && pin.last_visited && pin.last_visited !== "never") return false;

    const pinLabelIds = new Set(
        _pinTagObjects(pin)
            .map((t) => String(t.id))
            .filter(Boolean),
    );
    if (Array.isArray(criteria.label_groups) && criteria.label_groups.length) {
        for (const group of criteria.label_groups) {
            const ids = (group.ids || []).map(String);
            if (!ids.length) continue;
            if (group.op === "not") {
                if (ids.some((id) => pinLabelIds.has(id))) return false;
            } else if (group.op === "and") {
                if (!ids.every((id) => pinLabelIds.has(id))) return false;
            } else if (!ids.some((id) => pinLabelIds.has(id))) return false; // 'or'
        }
    } else {
        if (Array.isArray(criteria.tags) && criteria.tags.length && !criteria.tags.some((id) => pinLabelIds.has(String(id)))) return false;
        if (Array.isArray(criteria.exclude_tags) && criteria.exclude_tags.length && criteria.exclude_tags.some((id) => pinLabelIds.has(String(id)))) return false;
    }

    if (pin.latitude && pin.longitude) {
        const pt: GeoJSON.Position = [Number(pin.longitude), Number(pin.latitude)];
        if (criteria.include_regions && !_sfPointInMultiPolygon(pt, criteria.include_regions)) return false;
        if (criteria.exclude_regions && _sfPointInMultiPolygon(pt, criteria.exclude_regions)) return false;
    }
    return true;
}

// Apply/remove one filter's optimistic effect over whatever pins are currently
// rendered - narrowing (turnOn) only ever hides pins already on the map; broadening
// (turnOff) only ever restores pins this same filter previously hid, so this can never
// show a pin the server excluded for an unrelated reason.
function _sfApplyFilterClientSide(filterUuid: string, criteria: SavedFilterCriteria, turnOn: boolean): void {
    for (const [uuid, marker] of _markerMap) {
        if (turnOn) {
            const pin = _pinStore.get(uuid);
            if (!pin || _sfClientMatches(pin, criteria)) continue;
            if (!_toolbarHiddenByFilter.has(uuid)) _toolbarHiddenByFilter.set(uuid, new Set());
            const hiders = _toolbarHiddenByFilter.get(uuid)!;
            if (!hiders.size) clusterGroup.removeLayer(marker);
            hiders.add(filterUuid);
        } else {
            const hiders = _toolbarHiddenByFilter.get(uuid);
            if (!hiders || !hiders.has(filterUuid)) continue;
            hiders.delete(filterUuid);
            if (!hiders.size) {
                _toolbarHiddenByFilter.delete(uuid);
                clusterGroup.addLayer(marker);
            }
        }
    }
}

function toggleToolbarSavedFilter(btnEl: HTMLElement): void {
    const uuid = btnEl.dataset.filterUuid;
    if (!uuid) return;
    let criteria: SavedFilterCriteria;
    try {
        criteria = JSON.parse(btnEl.dataset.criteria || "{}");
    } catch {
        return;
    }

    const turningOn = !_activeToolbarFilters.has(uuid);
    if (turningOn) _activeToolbarFilters.set(uuid, criteria);
    else _activeToolbarFilters.delete(uuid);
    btnEl.classList.toggle("active", turningOn);

    // 1. Instant optimistic pass over already-known pins - no network wait.
    _sfApplyFilterClientSide(uuid, criteria, turningOn);

    // 2. Authoritative reconciliation, same pipeline every other filter change uses.
    // (The form's own 'change' listener already calls _refreshPinList() -
    // see the note in applySavedFilter() for why not to call it again here.)
    const hidden = document.getElementById("fp-toolbar-filter-ids") as HTMLInputElement | null;
    if (hidden) hidden.value = Array.from(_activeToolbarFilters.keys()).join(",");
    const form = document.getElementById("filter-form")!;
    htmx.trigger(form, "change");
    sfToolbarRefreshCounts();
    _updateFilterLabel();
}
window.toggleToolbarSavedFilter = toggleToolbarSavedFilter;

// -- Toolbar paging (>10 filters): slides the track, no server round trip ---------
function _sfToolbarPageSize(): number {
    const btn = document.querySelector(".sf-toolbar-btn");
    return btn ? btn.getBoundingClientRect().width + 4 : 44; // button + track gap
}
function _sfToolbarButtonCount(): number {
    return document.querySelectorAll(".sf-toolbar-btn").length;
}
function _sfToolbarVisibleCount(): number {
    const viewport = document.querySelector(".sf-toolbar-viewport");
    if (!viewport) return 10;
    // +4 compensates for there being no trailing gap after the last button
    // (the track is exactly N*width + (N-1)*gap wide) - without it, this
    // undercounts by one whenever everything fits exactly (e.g. a viewport
    // sized for exactly 2 buttons was computing room for only 1).
    return Math.max(1, Math.floor((viewport.clientWidth + 4) / _sfToolbarPageSize())) || 10;
}
function sfToolbarPage(direction: number): void {
    const total = _sfToolbarButtonCount();
    const perPage = _sfToolbarVisibleCount();
    const maxIdx = Math.max(0, total - perPage);
    _sfToolbarPageIdx = Math.min(maxIdx, Math.max(0, _sfToolbarPageIdx + direction * perPage));
    _sfToolbarRender();
}
window.sfToolbarPage = sfToolbarPage;

function _sfToolbarRender(): void {
    const track = document.getElementById("sf-toolbar-track");
    const viewport = document.querySelector<HTMLElement>(".sf-toolbar-viewport");
    const root = document.getElementById("map-saved-filters-toolbar");
    if (!track || !root || !viewport) return;
    const offset = _sfToolbarPageIdx * _sfToolbarPageSize();
    track.style.transform = `translateX(-${offset}px)`;
    // Whether there's actually more content is a direct pixel-geometry
    // question, not a perPage/total arithmetic estimate (which inherits the
    // same "assumes a trailing gap" rounding the page-size calc above has to
    // work around) - a 1px tolerance avoids sub-pixel rounding flicker for
    // content that fits exactly.
    root.classList.toggle("has-prev", offset > 1);
    root.classList.toggle("has-next", track.scrollWidth - offset - viewport.clientWidth > 1);
}

// Re-sync toggle state + paging whenever the toolbar is (re)rendered - initial page
// load, and every OOB swap after a saved filter is created/deleted elsewhere.
function _sfToolbarInit(): void {
    const buttons = document.querySelectorAll<HTMLElement>(".sf-toolbar-btn");
    const knownUuids = new Set<string>();
    buttons.forEach((btn) => {
        const uuid = btn.dataset.filterUuid!;
        knownUuids.add(uuid);
        btn.classList.toggle("active", _activeToolbarFilters.has(uuid));
    });
    // Drop bookkeeping for filters that no longer exist (deleted elsewhere), restoring
    // any pin whose only remaining "hider" was one of those now-gone filters.
    for (const uuid of Array.from(_activeToolbarFilters.keys())) {
        if (!knownUuids.has(uuid)) _activeToolbarFilters.delete(uuid);
    }
    for (const [pinUuid, hiders] of Array.from(_toolbarHiddenByFilter.entries())) {
        for (const filterUuid of Array.from(hiders)) {
            if (!knownUuids.has(filterUuid)) hiders.delete(filterUuid);
        }
        if (!hiders.size) {
            _toolbarHiddenByFilter.delete(pinUuid);
            const marker = _markerMap.get(pinUuid);
            if (marker) clusterGroup.addLayer(marker);
        }
    }
    _sfToolbarPageIdx = 0;
    _sfToolbarRender();
    sfToolbarRefreshCounts();
}
document.body.addEventListener("htmx:oobAfterSwap", (e) => {
    const detail = (e as CustomEvent<{ target?: HTMLElement }>).detail;
    if (detail?.target?.id === "map-saved-filters-toolbar") _sfToolbarInit();
});
window.addEventListener("resize", () => _sfToolbarRender());
setTimeout(_sfToolbarInit, 150);

// Restore filter state from URL params (supports Back button + page refresh)
function _restoreFiltersFromUrl(): void {
    if (!location.search) return;
    const params = new URLSearchParams(location.search);
    if (![...params.keys()].length) return;
    const form = document.getElementById("filter-form") as HTMLFormElement | null;
    if (!form) return;
    // Deep link from Organize > Labels "View on map" - zoom to the filtered
    // result once the first filtered batch of pins comes back (see data.html),
    // rather than leaving the user at whatever the map's default view was.
    if (params.has("label_groups")) window._fitToFilteredPinsOnce = true;
    let anySet = false;
    for (const [key, value] of params.entries()) {
        if (_MAP_VIEW_PARAM_KEYS.has(key)) continue;
        const el = form.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${CSS.escape(key)}"]`);
        if (!el) continue;
        if (el instanceof HTMLInputElement && (el.type === "checkbox" || el.type === "radio")) {
            // Find the matching option
            const match = form.querySelector<HTMLInputElement>(`[name="${CSS.escape(key)}"][value="${CSS.escape(value)}"]`);
            if (match) {
                match.checked = true;
                anySet = true;
            }
        } else {
            el.value = value;
            anySet = true;
        }
    }
    // Rebuild toolbar toggle state (button .active + optimistic hide) from the
    // restored hidden input - _sfToolbarInit() runs after this (see setTimeout
    // below) but only knows about buttons, not which filters were active, so
    // that has to be reconstructed here from the actual criteria.
    const toolbarIdsRaw = ((document.getElementById("fp-toolbar-filter-ids") as HTMLInputElement | null)?.value || "").trim();
    if (toolbarIdsRaw) {
        for (const uuid of toolbarIdsRaw.split(",").filter(Boolean)) {
            const btn = document.getElementById(`sf-toolbar-btn-${uuid}`);
            if (!btn || _activeToolbarFilters.has(uuid)) continue;
            let criteria: SavedFilterCriteria;
            try {
                criteria = JSON.parse(btn.dataset.criteria || "{}");
            } catch {
                continue;
            }
            _activeToolbarFilters.set(uuid, criteria);
            btn.classList.add("active");
            _sfApplyFilterClientSide(uuid, criteria, true);
        }
        anySet = true;
    }
    if (anySet) {
        _updateFilterLabel();
        // Open filter panel so user can see restored filters
        const mapEl = document.getElementById("map");
        if (mapEl && !mapEl.classList.contains("filter-open")) toggleFilterPanel();
        // Trigger HTMX to apply the restored filters
        setTimeout(() => htmx.trigger(form, "change"), 200);
    }
}
// Delay until after initial pin load
setTimeout(_restoreFiltersFromUrl, 100);

// -- Toggle-radio: clicking a selected radio deselects it (resets to "Any") --
// Capture checked state on mousedown (before browser toggles it)
document.getElementById("filter-form")!.addEventListener(
    "mousedown",
    (e) => {
        const radio = (e.target as HTMLElement).closest<HTMLInputElement>("input[type=radio]");
        if (radio && radio.value !== "") {
            radio.dataset.wasChecked = radio.checked ? "true" : "false";
        }
    },
    true,
);
document.getElementById("filter-form")!.addEventListener(
    "click",
    (e) => {
        const radio = (e.target as HTMLElement).closest<HTMLInputElement>("input[type=radio]");
        if (!radio || radio.value === "") return;
        if (radio.dataset.wasChecked === "true") {
            // Was already selected - reset to the blank (Any) option
            const anyRadio = document.querySelector<HTMLInputElement>(`#filter-form input[type=radio][name="${radio.name}"][value=""]`);
            if (anyRadio) {
                anyRadio.checked = true;
                htmx.trigger(document.getElementById("filter-form")!, "change");
            }
        }
        radio.dataset.wasChecked = "false";
    },
    true,
);

// -- Filter refresh indicator (subtle bottom pill, same as pin fetch) -------
function _showFilterFetchIndicator(): void {
    _setFetching(true, "Updating map...");
}
function _hideFilterFetchIndicator(): void {
    _setFetching(false);
}

// -- Skip server round-trip when no filters are active --------------------
document.getElementById("filter-form")!.addEventListener("htmx:beforeRequest", (e) => {
    if (!_hasActiveFilters()) {
        e.preventDefault();
        _exitFilterMode();
        _updateFilterLabel();
        return;
    }
    _showFilterFetchIndicator();
});
document.getElementById("filter-form")!.addEventListener("htmx:afterRequest", _hideFilterFetchIndicator);

// -- Prevent filter panel pointer events from reaching the Leaflet map --
document.getElementById("filter-panel")!.addEventListener(
    "wheel",
    (e) => {
        e.stopPropagation();
    },
    { passive: true },
);
document.getElementById("filter-panel")!.addEventListener("mousedown", (e) => {
    e.stopPropagation();
});
document.getElementById("filter-panel")!.addEventListener("touchstart", (e) => {
    e.stopPropagation();
});
// Both panels are children of #map, so a press inside them reaches the
// container-level rubber-band listener unless stopped here.
document.getElementById("filter-panel")!.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
});
document.getElementById("filter-panel")!.addEventListener("contextmenu", (e) => {
    e.stopPropagation();
});

// -- Prevent pin list panel pointer events from reaching the Leaflet map --
document.getElementById("pin-list-panel")!.addEventListener(
    "wheel",
    (e) => {
        e.stopPropagation();
    },
    { passive: true },
);
document.getElementById("pin-list-panel")!.addEventListener("mousedown", (e) => {
    e.stopPropagation();
});
document.getElementById("pin-list-panel")!.addEventListener("touchstart", (e) => {
    e.stopPropagation();
});
document.getElementById("pin-list-panel")!.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
});
document.getElementById("pin-list-panel")!.addEventListener("contextmenu", (e) => {
    e.stopPropagation();
});

// -- Visit filter UI ------------------------------------------------------
function _syncVisitUI(val: string): void {
    const hiddenEl = document.getElementById("fp-visits-hidden") as HTMLInputElement | null;
    const resetBtn = document.getElementById("fp-visits-reset") as HTMLElement | null;
    const datesEl = document.getElementById("fp-visits-dates") as HTMLElement | null;
    const chipYes = document.getElementById("fp-visits-chip-yes");
    const chipNo = document.getElementById("fp-visits-chip-no");
    if (hiddenEl) hiddenEl.value = val;
    if (resetBtn) resetBtn.style.display = val !== "" ? "" : "none";
    if (datesEl) datesEl.style.display = val === "yes" ? "" : "none";
    if (datesEl && val !== "yes") {
        datesEl.querySelectorAll<HTMLInputElement>("input[type=date]").forEach((el) => {
            el.value = "";
        });
    }
    // Reflect active chip styling
    [chipYes, chipNo].forEach((c) => {
        if (!c) return;
        const r = c.querySelector<HTMLInputElement>("input[type=radio]");
        if (r) r.checked = r.value === val;
    });
    const det = document.getElementById("fp-acc-visits");
    if (det) det.classList.toggle("fp-acc-active", val !== "");
}
function _resetVisits(): void {
    _syncVisitUI("");
    htmx.trigger(document.getElementById("filter-form")!, "change");
}

function _syncVisitDateReset(): void {
    const after = document.getElementById("fp-visited-after") as HTMLInputElement | null;
    const before = document.getElementById("fp-visited-before") as HTMLInputElement | null;
    const btn = document.getElementById("fp-visited-dates-reset") as HTMLElement | null;
    if (btn) btn.style.display = after?.value || before?.value ? "" : "none";
}

function _resetVisitDates(): void {
    const after = document.getElementById("fp-visited-after") as HTMLInputElement | null;
    const before = document.getElementById("fp-visited-before") as HTMLInputElement | null;
    if (after) after.value = "";
    if (before) before.value = "";
    _syncVisitDateReset();
    htmx.trigger(document.getElementById("filter-form")!, "change");
}

function _syncCreatedDateReset(): void {
    const after = document.getElementById("fp-created-after") as HTMLInputElement | null;
    const before = document.getElementById("fp-created-before") as HTMLInputElement | null;
    const btn = document.getElementById("fp-created-dates-reset") as HTMLElement | null;
    if (btn) btn.style.display = after?.value || before?.value ? "" : "none";
}

function _resetCreatedDates(): void {
    const after = document.getElementById("fp-created-after") as HTMLInputElement | null;
    const before = document.getElementById("fp-created-before") as HTMLInputElement | null;
    if (after) after.value = "";
    if (before) before.value = "";
    _syncCreatedDateReset();
    htmx.trigger(document.getElementById("filter-form")!, "change");
}
window._resetVisits = _resetVisits;
window._resetVisitDates = _resetVisitDates;
window._resetCreatedDates = _resetCreatedDates;

// Wire date inputs to keep reset buttons in sync
["fp-visited-after", "fp-visited-before"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", _syncVisitDateReset);
});
["fp-created-after", "fp-created-before"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", _syncCreatedDateReset);
});

// -- Additional plain after/before date-range filters (Date Built, Date
// Abandoned, Last Viewed) - same after/before + reset-button idiom as
// Visited/Created above, factored into a helper since it now repeats
// three more times.
interface DateRangeReset {
    sync(): void;
    reset(): void;
}
function _wireDateRangeReset(afterId: string, beforeId: string, btnId: string): DateRangeReset {
    function sync(): void {
        const after = document.getElementById(afterId) as HTMLInputElement | null;
        const before = document.getElementById(beforeId) as HTMLInputElement | null;
        const btn = document.getElementById(btnId) as HTMLElement | null;
        if (btn) btn.style.display = after?.value || before?.value ? "" : "none";
    }
    function reset(): void {
        const after = document.getElementById(afterId) as HTMLInputElement | null;
        const before = document.getElementById(beforeId) as HTMLInputElement | null;
        if (after) after.value = "";
        if (before) before.value = "";
        sync();
        htmx.trigger(document.getElementById("filter-form")!, "change");
    }
    [afterId, beforeId].forEach((id) => {
        document.getElementById(id)?.addEventListener("change", sync);
    });
    return { sync, reset };
}
const _dateBuiltRange = _wireDateRangeReset("fp-date-built-after", "fp-date-built-before", "fp-date-built-dates-reset");
const _dateAbandonedRange = _wireDateRangeReset("fp-date-abandoned-after", "fp-date-abandoned-before", "fp-date-abandoned-dates-reset");
const _lastViewedRange = _wireDateRangeReset("fp-last-viewed-after", "fp-last-viewed-before", "fp-last-viewed-dates-reset");
window._dateBuiltRange = _dateBuiltRange;
window._dateAbandonedRange = _dateAbandonedRange;
window._lastViewedRange = _lastViewedRange;
(function _initVisits() {
    ["fp-visits-chip-yes", "fp-visits-chip-no"].forEach((id) => {
        const chip = document.getElementById(id);
        if (!chip) return;
        const radio = chip.querySelector<HTMLInputElement>("input[type=radio]");
        if (!radio) return;
        chip.addEventListener("mousedown", () => {
            chip.dataset.wasChecked = String(radio.checked);
        });
        chip.addEventListener("click", () => {
            if (chip.dataset.wasChecked === "true") {
                _resetVisits();
            } else {
                _syncVisitUI(radio.value);
                htmx.trigger(document.getElementById("filter-form")!, "change");
            }
        });
    });
})();

// -- Floating address search bar --------------------------------------
// Uses the shared LocationSearchEngine (see ts/shared/location-search-engine.ts)
// bound to the {% map_search_bar %} component; this page supplies the
// map-specific callbacks (panning, temp markers, opening pin popups) and keeps
// the pin-recently-viewed tracking, which is specific to this page's marker/popup model.
const _homeLat = _SERVER_CENTER_LAT !== null ? _SERVER_CENTER_LAT : _GPS_FALLBACK_LAT;
const _homeLng = _SERVER_CENTER_LNG !== null ? _SERVER_CENTER_LNG : _GPS_FALLBACK_LNG;
const _searchHome = _homeLat !== null && _homeLng !== null && _MAP_CENTER_MODE !== "gps" ? { lat: _homeLat, lng: _homeLng, zoom: _defaultZoom } : null;

type LocationSelectResult = Parameters<LocationSearchAttachOptions["onSelect"]>[0];
type LocationMultiResult = Parameters<NonNullable<LocationSearchAttachOptions["onMultiResult"]>>[0];

function _onLocationMultiResult({ searchTerm, results }: LocationMultiResult): void {
    _clearSearchMarker();
    const latLngs: L.LatLngTuple[] = [];
    results.forEach((r, idx) => {
        latLngs.push([r.lat, r.lng]);
        const marker = _createTempPlaceMarker(r.lat, r.lng, r.title || searchTerm, true);
        marker.on(
            "click",
            ((mlat: number, mlng: number, mname: string) => {
                return (e: L.LeafletMouseEvent) => {
                    map.closePopup();
                    const anchor = _markerMenuAnchor(e, mlat, mlng);
                    MapContextMenu.show({
                        lat: mlat,
                        lng: mlng,
                        zoom: map.getZoom(),
                        clientX: anchor.x,
                        clientY: anchor.y,
                        extraItems: [
                            {
                                icon: "add_location",
                                label: "Create pin",
                                onClick: () => {
                                    openAddPinDialog(mlat, mlng, { defaultName: mname });
                                },
                            },
                            {
                                icon: "close",
                                label: "Dismiss marker",
                                onClick: () => {
                                    marker.remove();
                                },
                            },
                        ],
                    });
                };
            })(r.lat, r.lng, r.title || searchTerm),
        );
        marker.addTo(map);
        if (idx === 0) _searchMarker = marker;
    });
    _programmaticMove = true;
    if (latLngs.length === 1) {
        map.setView(latLngs[0]!, 14);
    } else {
        map.fitBounds(L.latLngBounds(latLngs), { padding: [40, 40], maxZoom: 14 });
    }
    if (results.length > 1) toastr.info(`Found ${results.length} results for "${searchTerm}"`);
}

function _onLocationSelect(result: LocationSelectResult): void {
    _programmaticMove = true;
    map.setView([result.lat, result.lng], result.zoom || _defaultZoom);
    if (result.type === "mylocation") {
        _showUserLocationMarker(result.lat, result.lng);
        return;
    }
    if (result.type === "pin" && result.pinSlug) {
        let found = false;
        for (const [uuid, pin] of _pinStore) {
            if (pin.slug === result.pinSlug || uuid === result.pinSlug) {
                setTimeout(() => {
                    const m = _markerMap.get(uuid);
                    if (m) m.openPopup();
                }, 350);
                found = true;
                break;
            }
        }
        if (!found) {
            // Not a root pin - it's a child (sub) pin. Turn the Child Pins
            // layer on when it isn't already, so the jumped-to pin is
            // actually visible, then open its popup.
            const openChild = () => {
                for (const [uuid, pin] of _childPinStore) {
                    if (pin.slug === result.pinSlug || uuid === result.pinSlug) {
                        setTimeout(() => {
                            const m = _childMarkerMap.get(uuid);
                            if (m) m.openPopup();
                        }, 350);
                        return;
                    }
                }
            };
            Promise.resolve(setChildPinsActive(true)).then(() => {
                _mapLayers!.syncButtons();
                openChild();
            });
        }
    } else {
        _showSearchMarker(result.lat, result.lng, result.title);
    }
}

// One-time cleanup: 'ul_addr_history_v1' used to be unscoped, so a shared
// browser leaked one user's typed search queries to the next user who
// logged in (UL-239). Drop the stale global entry; going forward the key
// carries the profile id, matching recentPinsKey below.
try {
    localStorage.removeItem("ul_addr_history_v1");
} catch {
    /* ignore */
}

const _addrSearch = LocationSearchEngine.attach("addr", {
    historyKey: "ul_addr_history_v1_" + _PROFILE_UUID + "",
    recentPinsKey: "ul_recent_pins_v1_" + _PROFILE_UUID + "",
    sources: {
        localPins: { url: MAP_CFG.urls.mapAutocompleteLocal },
        googlePlaces: { url: MAP_CFG.urls.mapAutocompletePlaces },
        topCities: { url: MAP_CFG.urls.mapAutocompleteEmpty },
    },
    resolvePlaceUrl: MAP_CFG.urls.mapResolvePlace,
    pinCacheProfileUuid: _PROFILE_UUID,
    home: _searchHome,
    enableMyLocation: true,
    getUserLocationCache: _getCachedUserLocation,
    setUserLocationCache: _cacheUserLocation,
    onGeolocationVisit: _GEOLOCATION_TRACKING_ALLOWED ? _recordGeolocationVisit : null,
    defaultZoom: _defaultZoom,
    onSelect: _onLocationSelect,
    onMultiResult: _onLocationMultiResult,
    onSearchStart: _clearSearchMarker,
    onFetchingChange: _setFetching,
});

document.getElementById("addr-search-geolocate")?.addEventListener("click", () => {
    _addrSearch?.recenterToUserLocation();
});

// Track recently viewed pins via Leaflet popup events, so they can surface
// in the search bar's empty-state suggestions next time it's opened.
// Deferred so `map` is guaranteed to exist by the time the handler fires.
setTimeout(() => {
    try {
        map.on("popupopen", (e) => {
            try {
                const pinDiv = e.popup.getElement()?.querySelector<HTMLElement>(".pin-popup");
                if (!pinDiv) return;
                const uuid = pinDiv.dataset.uuid;
                if (!uuid) return;
                const pin = _pinStore.get(uuid);
                if (!pin || !pin.latitude || !pin.longitude) return;
                _addrSearch?.trackRecentPin({
                    slug: pin.slug || uuid,
                    name: pin.name || "",
                    lat: Number.parseFloat(String(pin.latitude)),
                    lng: Number.parseFloat(String(pin.longitude)),
                    url: pin.viewLocationUrl || "/dashboard/map/pin/" + encodeURIComponent(pin.slug || uuid) + "/",
                });
            } catch {
                /* ignore */
            }
        });
    } catch {
        /* ignore */
    }
}, 0);

// -- Combined label filter section ----------------------------------------
// Backed by the shared picker engine (ts/shared/label-picker.ts) - the same
// engine drives the saved-filter dialog and detail page, so include/exclude,
// chip dragging, the AND/OR toggle, and the formula bar behave identically
// everywhere labels filter pins. Serializes into the #fp-label-groups hidden
// input as label_groups JSON.
const _labelPicker: FilterPickerApi | null = (() => {
    const list = document.getElementById("fp-label-list");
    if (!list) return null; // the Labels accordion only renders when the user has labels
    return createFilterPicker({
        els: {
            list: list,
            selected: document.getElementById("fp-label-selected")!,
            colIncl: document.getElementById("fp-label-col-incl")!,
            colExcl: document.getElementById("fp-label-col-excl")!,
            inclChips: document.getElementById("fp-label-incl-chips")!,
            exclChips: document.getElementById("fp-label-excl-chips")!,
            modeBtn: document.getElementById("fp-incl-mode-btn"),
            groupsInput: document.getElementById("fp-label-groups") as HTMLInputElement | null,
            formulaBar: document.getElementById("fp-label-formula") as HTMLInputElement | null,
            formulaSuggestions: document.getElementById("fp-formula-suggestions"),
            formulaErrors: document.getElementById("fp-formula-errors"),
            formulaDisplay: document.getElementById("fp-formula-display"),
            formulaDisplayText: document.getElementById("fp-formula-display-text"),
            formulaDisplayClear: document.getElementById("fp-formula-display-clear"),
            accordion: document.getElementById("fp-acc-labels"),
            kindTabs: document.getElementById("fp-label-kind-tabs"),
        },
        onChange: () => {
            htmx.trigger(document.getElementById("filter-form")!, "change");
        },
    });
})();

// -- Shared UI helpers (also used in organize page) --------------------
function _escHtml(s: unknown): string {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function pickColor(pickerId: string, valueId: string, colorHex: string, btn?: HTMLElement): void {
    const picker = document.getElementById(pickerId);
    if (picker) picker.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
    if (btn) btn.classList.add("selected");
    const input = document.getElementById(valueId) as HTMLInputElement | null;
    if (input) input.value = colorHex;
}
window.pickColor = pickColor;

// The icon catalogue is fetched on first open rather than rendered into
// every picker - one grid is 594 KB of markup (P68). This mirrors
// frontend/ts/shared/icon-picker.ts; the copy exists because this page
// defines its own IconPicker with add-pin-specific behaviour in pick()
// and _handleUpload(), so it cannot import the shared one.
interface IconCatalogue {
    tabs: string;
    items: string;
}
let _iconGridRequest: Promise<IconCatalogue> | null = null;

function _parseIconCatalogue(html: string): IconCatalogue {
    const holder = document.createElement("template");
    holder.innerHTML = html;
    const tabs = holder.content.querySelector("[data-icon-picker-tabs]");
    const items = holder.content.querySelector("[data-icon-picker-items]");
    return { tabs: tabs ? tabs.innerHTML : "", items: items ? items.innerHTML : "" };
}

function _loadIconCatalogue(url: string): Promise<IconCatalogue> {
    if (!_iconGridRequest) {
        _iconGridRequest = fetch(url, { credentials: "same-origin" })
            .then((response) => {
                if (!response.ok) throw new Error("icon grid: HTTP " + response.status);
                return response.text();
            })
            .then(_parseIconCatalogue)
            .catch((error: unknown) => {
                // Dropped so the next open retries, rather than leaving the
                // picker reading "Loading icons..." until a page reload.
                _iconGridRequest = null;
                throw error;
            });
    }
    return _iconGridRequest;
}

function _iconGridStatus(grid: HTMLElement): HTMLElement {
    let node = grid.querySelector<HTMLElement>(".icon-picker-status");
    if (!node) {
        node = document.createElement("div");
        node.className = "icon-picker-status";
        grid.appendChild(node);
    }
    return node;
}

function _fillIconGrid(id: string): Promise<void> {
    const grid = document.getElementById("icon-grid-" + id);
    if (!grid || grid.dataset.iconsLoaded === "1" || !grid.dataset.gridUrl) return Promise.resolve();
    const status = _iconGridStatus(grid);
    status.textContent = "Loading icons...";
    return _loadIconCatalogue(grid.dataset.gridUrl)
        .then((catalogue) => {
            // Re-checked: two opens can await the same promise, and the second
            // must not append a second copy of the catalogue.
            if (grid.dataset.iconsLoaded === "1") return;
            grid.insertAdjacentHTML("beforeend", catalogue.items);
            const tabs = document.getElementById("icon-tabs-" + id);
            if (tabs) tabs.insertAdjacentHTML("beforeend", catalogue.tabs);
            grid.dataset.iconsLoaded = "1";
            status.remove();
            const input = document.getElementById("icon-value-" + id) as HTMLInputElement | null;
            const current = input ? input.value : "";
            if (current) {
                grid.querySelectorAll<HTMLElement>(".icon-picker-item").forEach((item) => {
                    item.classList.toggle("selected", item.dataset.icon === current);
                });
            }
        })
        .catch(() => {
            status.textContent = "Icons could not be loaded. Close and reopen to try again.";
        });
}

// Re-applies whatever filter is showing, for items that arrived after it was
// set. Mirrors reapplyFilter in frontend/ts/shared/icon-picker.ts.
function _reapplyIconFilter(id: string): void {
    const panel = document.getElementById("icon-panel-" + id);
    const search = panel ? panel.querySelector<HTMLInputElement>(".icon-picker-search-input") : null;
    const query = search ? search.value.trim() : "";
    if (query) {
        IconPicker.search(id, query);
        return;
    }
    const activeTab = panel ? panel.querySelector<HTMLElement>(".icon-tab.active") : null;
    IconPicker.setTabSilent(id, activeTab ? activeTab.dataset.cat || "" : "");
}

const IconPicker = {
    toggle(id: string): void {
        const panel = document.getElementById("icon-panel-" + id);
        if (!panel) return;
        const isHidden = panel.hasAttribute("hidden");
        document.querySelectorAll(".icon-picker-panel").forEach((p) => p.setAttribute("hidden", ""));
        if (isHidden) {
            panel.removeAttribute("hidden");
            const search = panel.querySelector<HTMLInputElement>(".icon-picker-search-input");
            if (search) {
                search.value = "";
                search.focus();
                IconPicker.search(id, "");
            }
            IconPicker.setTabSilent(id, "");
            // Revealed first, filled second, so the fetch shows as a loading
            // row inside an open panel rather than as a click that does nothing.
            // The chrome is live while the icons are still arriving, so
            // whatever the user typed or clicked meanwhile is re-applied to
            // them rather than reset - _reapplyIconFilter, not setTabSilent.
            _fillIconGrid(id).then(() => _reapplyIconFilter(id));
        }
    },
    setTabSilent(id: string, cat: string): void {
        const panel = document.getElementById("icon-panel-" + id);
        if (!panel) return;
        panel.querySelectorAll<HTMLElement>(".icon-tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === cat));
        const grid = document.getElementById("icon-grid-" + id);
        if (!grid) return;
        grid.querySelectorAll<HTMLElement>(".icon-picker-item").forEach((item) => {
            item.style.display = !cat || item.dataset.cat === cat || !item.dataset.cat ? "" : "none";
        });
    },
    setTab(id: string, cat: string, btn: HTMLElement): void {
        const panel = document.getElementById("icon-panel-" + id);
        if (!panel) return;
        panel.querySelectorAll(".icon-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const search = panel.querySelector<HTMLInputElement>(".icon-picker-search-input");
        if (search) search.value = "";
        const grid = document.getElementById("icon-grid-" + id);
        if (!grid) return;
        grid.querySelectorAll<HTMLElement>(".icon-picker-item").forEach((item) => {
            item.style.display = !cat || item.dataset.cat === cat || !item.dataset.cat ? "" : "none";
        });
    },
    search(id: string, query: string): void {
        const q = query.toLowerCase().trim();
        const panel = document.getElementById("icon-panel-" + id);
        if (!panel) return;
        panel.querySelectorAll<HTMLElement>(".icon-tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === ""));
        const grid = document.getElementById("icon-grid-" + id);
        if (!grid) return;
        grid.querySelectorAll<HTMLElement>(".icon-picker-item").forEach((item) => {
            if (!q) {
                item.style.display = "";
            } else {
                const label = item.dataset.label || "",
                    icon = item.dataset.icon || "",
                    keywords = item.dataset.keywords || "";
                item.style.display = label.includes(q) || icon === q || keywords.includes(q) ? "" : "none";
            }
        });
    },
    pick(id: string, icon: string, btn: HTMLElement | null): void {
        const input = document.getElementById("icon-value-" + id) as HTMLInputElement | null;
        if (input) input.value = icon;
        const current = document.getElementById("icon-current-" + id);
        if (current) {
            if (icon) {
                current.innerHTML = /^[a-z_]+$/.test(icon) ? '<i class="material-icons icon-picker-current-mi">' + icon + "</i>" : '<span class="icon-picker-current-glyph">' + icon + "</span>";
            } else {
                current.innerHTML = '<span class="icon-picker-none-label">No icon</span>';
                // Picking "none": clear any uploaded custom icon
                const uploadInput = document.getElementById("icon-upload-input-" + id) as HTMLInputElement | null;
                if (uploadInput) uploadInput.value = "";
                if (id === "add-pin") {
                    window._addPinCustomIconFile = null;
                }
                // Label edit context: mark existing custom icon for clearing
                const clearFlag = document.getElementById("edit-clear-custom-" + id) as HTMLInputElement | null;
                if (clearFlag) clearFlag.value = "1";
            }
        }
        const grid = document.getElementById("icon-grid-" + id);
        if (grid) {
            grid.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
            if (btn) btn.classList.add("selected");
        }
        const panel = document.getElementById("icon-panel-" + id);
        if (panel) panel.setAttribute("hidden", "");
    },
    _handleUpload(id: string, input: HTMLInputElement): void {
        if (!input.files || !input.files[0]) return;
        const file = input.files[0];
        if (id === "add-pin") {
            window._addPinCustomIconFile = file;
        }
        const reader = new FileReader();
        reader.onload = (e) => {
            const current = document.getElementById("icon-current-" + id);
            if (current) {
                current.innerHTML = '<img src="' + String(e.target?.result) + '" class="icon-picker-custom-preview" alt="">';
            }
            // Clear any text icon value
            const textInput = document.getElementById("icon-value-" + id) as HTMLInputElement | null;
            if (textInput) textInput.value = "";
            // Label edit context: clear the "delete" flag (we're replacing, not removing)
            const clearFlag = document.getElementById("edit-clear-custom-" + id) as HTMLInputElement | null;
            if (clearFlag) clearFlag.value = "";
            // Deselect any grid icon
            const grid = document.getElementById("icon-grid-" + id);
            if (grid) grid.querySelectorAll(".icon-picker-item.selected").forEach((b) => b.classList.remove("selected"));
            // Close panel
            const panel = document.getElementById("icon-panel-" + id);
            if (panel) panel.setAttribute("hidden", "");
        };
        reader.readAsDataURL(file);
    },
};
window.IconPicker = IconPicker;

// Close icon picker on outside click
document.addEventListener("click", (e) => {
    if (!(e.target as HTMLElement).closest(".icon-picker-dropdown")) {
        document.querySelectorAll(".icon-picker-panel").forEach((p) => p.setAttribute("hidden", ""));
    }
});

// -- Add-Pin / Edit-Pin dialog -----------------------------------------
let _addPinClickMode = false;
let _addPinMarker: L.Marker | null = null;
let _editPinUuid: string | null = null; // null = add mode; uuid string = edit mode
let _addPinGooglePlaceId: string | null = null; // set when adding a pin from a Places marker
let _addPinCanonicalName: string | null = null; // canonical place name from the external marker (not user-edited)
window._addPinCustomIconFile = null;

function _resetAddPinDialog(): void {
    _editPinUuid = null;
    _addPinGooglePlaceId = null;
    _addPinCanonicalName = null;
    (document.getElementById("apdlg-addr") as HTMLInputElement).value = "";
    (document.getElementById("apdlg-lat") as HTMLInputElement).value = "";
    (document.getElementById("apdlg-lng") as HTMLInputElement).value = "";
    (document.getElementById("apdlg-name") as HTMLInputElement).value = "";
    (document.getElementById("apdlg-addr-suggestions") as HTMLElement).hidden = true;
    (document.getElementById("apdlg-addr-wrap") as HTMLElement).hidden = false;
    (document.getElementById("apdlg-location-selected") as HTMLElement).hidden = true;
    document.getElementById("apdlg-location-text")!.textContent = "";

    const headerH2 = document.querySelector("#add-pin-dialog .apdlg-header h2");
    if (headerH2) headerH2.innerHTML = '<i class="material-icons" style="font-size:1.1rem;vertical-align:-.15em;margin-right:.35rem;">add_location</i>Add Pin';

    // Reset icon picker
    const iconCurrent = document.getElementById("icon-current-add-pin");
    if (iconCurrent) iconCurrent.innerHTML = '<span class="icon-picker-none-label">No icon</span>';
    const iconValue = document.getElementById("icon-value-add-pin") as HTMLInputElement | null;
    if (iconValue) iconValue.value = "";
    const uploadInput = document.getElementById("icon-upload-input-add-pin") as HTMLInputElement | null;
    if (uploadInput) uploadInput.value = "";
    window._addPinCustomIconFile = null;
    const clearCustomIconFlag = document.getElementById("edit-clear-custom-add-pin") as HTMLInputElement | null;
    if (clearCustomIconFlag) clearCustomIconFlag.value = "";
    document.querySelectorAll("#icon-grid-add-pin .icon-picker-item").forEach((i) => i.classList.remove("selected"));

    // Reset color picker
    document.querySelectorAll("#apdlg-color-picker .color-swatch").forEach((s) => s.classList.remove("selected"));
    const colorVal = document.getElementById("apdlg-color-value") as HTMLInputElement | null;
    if (colorVal) colorVal.value = "";

    // Reset label selector
    _apdlgSelectedLabels = [];
    _apdlgRenderSelectedChips();
    const labelSearch = document.getElementById("apdlg-label-search") as HTMLInputElement | null;
    if (labelSearch) labelSearch.value = "";
    const labelSugg = document.getElementById("apdlg-label-suggestions") as HTMLElement | null;
    if (labelSugg) labelSugg.hidden = true;
    _apdlgActiveKind = "";
    document.querySelectorAll("#apdlg-label-kind-tabs .tad-tab").forEach((t, i) => t.classList.toggle("tad-tab--active", i === 0));

    const customize = document.getElementById("apdlg-customize") as HTMLDetailsElement | null;
    if (customize) customize.open = false;

    const btn = document.getElementById("apdlg-submit") as HTMLButtonElement;
    btn.disabled = false;
    btn.textContent = "Add Pin";

    const deleteBtn = document.getElementById("apdlg-delete") as HTMLElement | null;
    if (deleteBtn) deleteBtn.hidden = true;

    const detailsLink = document.getElementById("apdlg-view-details") as HTMLAnchorElement | null;
    if (detailsLink) {
        detailsLink.hidden = true;
        detailsLink.href = "#";
    }
}

// Clears both the icon (standard or custom-uploaded) and color overrides
// for the pin currently open in the dialog, leaving labels untouched.
function _apdlgClearCustomizations(): void {
    IconPicker.pick("add-pin", "", null);
    const clearSwatch = document.querySelector<HTMLElement>("#apdlg-color-picker .color-swatch.color-clear");
    pickColor("apdlg-color-picker", "apdlg-color-value", "", clearSwatch ?? undefined);
}
window._apdlgClearCustomizations = _apdlgClearCustomizations;

async function _apdlgDeletePin(): Promise<void> {
    if (!_editPinUuid) return;
    const pin = _pinStore.get(_editPinUuid);
    const pinName = (pin && pin.name) || "this pin";
    if (await _deletePinByUuid(_editPinUuid, pinName)) closeAddPinDialog();
}
window._apdlgDeletePin = _apdlgDeletePin;

interface OpenAddPinDialogOptions {
    keepMarker?: boolean;
    defaultName?: string;
    placeId?: string;
}

function openAddPinDialog(lat: number | null = null, lng: number | null = null, opts: OpenAddPinDialogOptions = {}): void {
    const keepMarker = opts.keepMarker === true;
    const defaultName = (opts.defaultName || "").trim();
    const placeId = opts.placeId || null;
    _resetAddPinDialog();

    // Remove any previous preview marker unless transferring the context-menu pin
    if (!keepMarker && _addPinMarker) {
        map.removeLayer(_addPinMarker);
        _addPinMarker = null;
    }
    _addPinClickMode = false;

    // Pre-fill coordinates when called from right-click context menu
    if (lat !== null && lng !== null) {
        _setAddPinLocation(lat, lng);
    } else {
        // No explicit coordinates (e.g. the toolbar add-pin button) - default to
        // the browser's last-known device location, if the user has shared one,
        // rather than leaving the dialog blank and forcing a map click.
        const cachedLoc = _getCachedUserLocation();
        if (cachedLoc) {
            _setAddPinLocation(cachedLoc.lat, cachedLoc.lng, "Your current location");
        } else {
            setTimeout(() => {
                _addPinClickMode = true;
            }, 0);
        }
    }

    // Pre-fill name from Google place name or fallback search query
    if (defaultName) {
        const nameEl = document.getElementById("apdlg-name") as HTMLInputElement | null;
        if (nameEl) nameEl.value = defaultName;
    }

    // Store Google Place ID and canonical name so they can be sent with the
    // add-pin request.  The canonical name lets the server skip a geocoding
    // API call when creating a new Location row.
    if (placeId) _addPinGooglePlaceId = placeId;
    if (defaultName) _addPinCanonicalName = defaultName;

    (document.getElementById("add-pin-dialog") as HTMLDialogElement).showModal();
}
window.openAddPinDialog = openAddPinDialog;

function openEditPinDialog(pinUuid: string): void {
    const pin = _pinStore.get(pinUuid);
    if (!pin) {
        // Not in the local pin store yet - most likely the store's cache predates
        // this pin (e.g. it just arrived via a bulk import). Fetch it fresh before
        // giving up, instead of assuming the cache is authoritative.
        _refreshPinInStore(pinUuid, () => {
            if (_pinStore.get(pinUuid)) {
                openEditPinDialog(pinUuid);
            } else {
                toastr.error("Pin not found.");
            }
        });
        return;
    }

    _resetAddPinDialog();
    _editPinUuid = pinUuid;

    // Remove any previous preview marker
    if (_addPinMarker) {
        map.removeLayer(_addPinMarker);
        _addPinMarker = null;
    }
    _addPinClickMode = false;

    // Update dialog header
    const headerH2 = document.querySelector("#add-pin-dialog .apdlg-header h2");
    if (headerH2) headerH2.innerHTML = '<i class="material-icons" style="font-size:1.1rem;vertical-align:-.15em;margin-right:.35rem;">edit_location</i>Edit Pin';

    // Pre-fill name
    (document.getElementById("apdlg-name") as HTMLInputElement).value = pin.name || "";

    // Pre-fill location (coordinates - we don't cache the address text)
    if (pin.latitude && pin.longitude) {
        const lat = Number.parseFloat(String(pin.latitude)),
            lng = Number.parseFloat(String(pin.longitude));
        _setAddPinLocation(lat, lng, `${lat.toFixed(5)}, ${lng.toFixed(5)}`);
    }

    // Pre-fill icon/color from the pin's OWN overrides only - never from
    // pin.icon/pin.color, which are the *effective* (possibly label-inherited)
    // display values. Pre-filling from those would silently bake a label's
    // icon/color onto the pin the next time this form is saved, even if the
    // user never opened the Customize section.
    if (pin.own_custom_icon_url) {
        const iconCurrent = document.getElementById("icon-current-add-pin");
        if (iconCurrent) iconCurrent.innerHTML = `<img src="${_escapeHtml(pin.own_custom_icon_url)}" class="icon-picker-custom-preview" alt="">`;
    } else if (pin.own_icon) {
        const iconValue = document.getElementById("icon-value-add-pin") as HTMLInputElement | null;
        if (iconValue) iconValue.value = pin.own_icon;
        const iconCurrent = document.getElementById("icon-current-add-pin");
        if (iconCurrent) {
            if (/^[a-z_]+$/.test(pin.own_icon)) {
                iconCurrent.innerHTML = `<i class="material-icons icon-picker-current-mi">${_escapeHtml(pin.own_icon)}</i>`;
            } else {
                iconCurrent.innerHTML = `<span class="icon-picker-current-glyph">${_escapeHtml(pin.own_icon)}</span>`;
            }
        }
    }

    // Pre-fill color
    if (pin.own_color) {
        const swatch = document.querySelector<HTMLElement>(`#apdlg-color-picker .color-swatch[data-color="${pin.own_color}"]`);
        pickColor("apdlg-color-picker", "apdlg-color-value", pin.own_color, swatch ?? undefined);
    }

    // Pre-fill labels from the pin's own labels. Match by id (the reliable key -
    // label names aren't guaranteed unique across kinds/owners) and fall back
    // to a name match only for stale cache entries from before ids were cached.
    // If a label can't be found in _apdlgAllLabels at all (e.g. it's no longer
    // visible to this profile), keep it selected anyway using the cached data
    // so saving the form doesn't silently drop it from the pin.
    const tagsData = _pinTagObjects(pin);
    _apdlgSelectedLabels = tagsData
        .map((tag): LabelCandidate | null => {
            const found = tag.id != null ? _apdlgAllLabels.find((b) => String(b.id) === String(tag.id)) : _apdlgAllLabels.find((b) => b.name.toLowerCase() === tag.name.toLowerCase());
            if (found) return found;
            if (tag.id == null) return null;
            return { id: Number(tag.id), name: tag.name, icon: tag.icon || "", color: tag.color || "", kind: tag.kind || "" };
        })
        .filter((x): x is LabelCandidate => x !== null);
    _apdlgRenderSelectedChips();

    // Change submit button label
    const btn = document.getElementById("apdlg-submit") as HTMLButtonElement;
    btn.disabled = false;
    btn.textContent = "Save Changes";

    const deleteBtn = document.getElementById("apdlg-delete") as HTMLElement | null;
    if (deleteBtn) deleteBtn.hidden = false;

    const detailsLink = document.getElementById("apdlg-view-details") as HTMLAnchorElement | null;
    if (detailsLink && pin.viewLocationUrl) {
        detailsLink.href = pin.viewLocationUrl;
        detailsLink.hidden = false;
    }

    (document.getElementById("add-pin-dialog") as HTMLDialogElement).showModal();
}
window.openEditPinDialog = openEditPinDialog;

function closeAddPinDialog(): void {
    _editPinUuid = null;
    _addPinClickMode = false;
    if (_addPinMarker) {
        map.removeLayer(_addPinMarker);
        _addPinMarker = null;
    }
    (document.getElementById("add-pin-dialog") as HTMLDialogElement).close();
}
window.closeAddPinDialog = closeAddPinDialog;
// Backdrop-click-to-close (not drag-outside) is handled by the site-wide
// dialog handler in themes/base.html via data-closefn="closeAddPinDialog"
// on the <dialog> element - see that file for the drag-guard logic.

// Map click: place pin while dialog is open
map.on("click", (e) => {
    if (!_addPinClickMode) return;
    _setAddPinLocation(e.latlng.lat, e.latlng.lng);
});

// Address autocomplete (Nominatim)
let _addrTimer: ReturnType<typeof setTimeout> | undefined;
document.getElementById("apdlg-addr")!.addEventListener("input", function (this: HTMLInputElement) {
    clearTimeout(_addrTimer);
    const q = this.value.trim();
    if (!q) {
        (document.getElementById("apdlg-addr-suggestions") as HTMLElement).hidden = true;
        return;
    }
    // Accept raw lat,lng input
    const coordRe = /^(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)$/;
    const m = q.match(coordRe);
    if (m) {
        const lat = Number.parseFloat(m[1]!),
            lng = Number.parseFloat(m[2]!);
        if (lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180) {
            _setAddPinLocation(lat, lng);
            (document.getElementById("apdlg-addr-suggestions") as HTMLElement).hidden = true;
        }
        return;
    }
    _addrTimer = setTimeout(() => _fetchAddrSuggestions(q), 380);
});

interface NominatimResult {
    display_name: string;
    lat: string;
    lon: string;
}

function _fetchAddrSuggestions(query: string): void {
    fetch(`https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=5`, {
        headers: { Accept: "application/json", "Accept-Language": "en" },
    })
        .then((r) => r.json() as Promise<NominatimResult[]>)
        .then((results) => {
            const box = document.getElementById("apdlg-addr-suggestions") as HTMLElement;
            box.innerHTML = "";
            if (!results.length) {
                box.hidden = true;
                return;
            }
            results.forEach((r) => {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "apdlg-suggestion";
                btn.textContent = r.display_name;
                btn.addEventListener("mousedown", (e) => {
                    e.preventDefault(); // prevent input blur firing first
                    _setAddPinLocation(Number.parseFloat(r.lat), Number.parseFloat(r.lon), r.display_name);
                    box.hidden = true;
                });
                box.appendChild(btn);
            });
            box.hidden = false;
        })
        .catch(() => {
            /* ignore */
        });
}

function _setAddPinLocation(lat: number, lng: number, label: string | null = null): void {
    (document.getElementById("apdlg-lat") as HTMLInputElement).value = lat.toFixed(6);
    (document.getElementById("apdlg-lng") as HTMLInputElement).value = lng.toFixed(6);
    document.getElementById("apdlg-location-text")!.textContent = label || `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
    (document.getElementById("apdlg-addr-wrap") as HTMLElement).hidden = true;
    (document.getElementById("apdlg-location-selected") as HTMLElement).hidden = false;
    (document.getElementById("apdlg-addr-suggestions") as HTMLElement).hidden = true;
    _addPinClickMode = false;
    const latlng = L.latLng(lat, lng);
    map.setView(latlng, Math.max(map.getZoom(), 15));
    if (_addPinMarker) {
        _addPinMarker.setLatLng(latlng);
    } else {
        _addPinMarker = L.marker(latlng).addTo(map);
    }
}

function _clearAddPinLocation(): void {
    (document.getElementById("apdlg-lat") as HTMLInputElement).value = "";
    (document.getElementById("apdlg-lng") as HTMLInputElement).value = "";
    (document.getElementById("apdlg-addr") as HTMLInputElement).value = "";
    document.getElementById("apdlg-location-text")!.textContent = "";
    (document.getElementById("apdlg-addr-wrap") as HTMLElement).hidden = false;
    (document.getElementById("apdlg-location-selected") as HTMLElement).hidden = true;
    (document.getElementById("apdlg-addr-suggestions") as HTMLElement).hidden = true;
    if (_addPinMarker) {
        map.removeLayer(_addPinMarker);
        _addPinMarker = null;
    }
    _addPinClickMode = true;
    (document.getElementById("apdlg-addr") as HTMLInputElement).focus();
}

document.getElementById("apdlg-location-clear")!.addEventListener("click", _clearAddPinLocation);

// Clicking the location pill (not the x button) switches the input back to edit mode
// with the existing location text pre-filled so the user can refine it.
function _switchToEditLocation(): void {
    const currentText = document.getElementById("apdlg-location-text")!.textContent;
    (document.getElementById("apdlg-lat") as HTMLInputElement).value = "";
    (document.getElementById("apdlg-lng") as HTMLInputElement).value = "";
    (document.getElementById("apdlg-addr") as HTMLInputElement).value = currentText || "";
    document.getElementById("apdlg-location-text")!.textContent = "";
    (document.getElementById("apdlg-addr-wrap") as HTMLElement).hidden = false;
    (document.getElementById("apdlg-location-selected") as HTMLElement).hidden = true;
    (document.getElementById("apdlg-addr-suggestions") as HTMLElement).hidden = true;
    if (_addPinMarker) {
        map.removeLayer(_addPinMarker);
        _addPinMarker = null;
    }
    _addPinClickMode = true;
    const addrInput = document.getElementById("apdlg-addr") as HTMLInputElement;
    addrInput.focus();
    addrInput.select();
    if (currentText) _fetchAddrSuggestions(currentText);
}
document.getElementById("apdlg-location-edit")!.addEventListener("click", _switchToEditLocation);

// Hide address suggestions when input loses focus
document.getElementById("apdlg-addr")!.addEventListener("blur", () => {
    setTimeout(() => {
        (document.getElementById("apdlg-addr-suggestions") as HTMLElement).hidden = true;
    }, 150);
});

// -- Label selector ----------------------------------------------------
let _apdlgSelectedLabels: LabelCandidate[] = [];
let _apdlgAllLabels: LabelCandidate[] = [];
let _apdlgActiveKind = ""; // '' = All - only filters which candidates are offered, see _apdlgKindOk
(() => {
    const dataEl = document.getElementById("apdlg-label-data");
    if (dataEl) {
        try {
            _apdlgAllLabels = JSON.parse(dataEl.textContent || "[]") as LabelCandidate[];
        } catch {
            /* ignore */
        }
    }
})();

// Shared by every candidate lookup below (suggestions + Tab/comma/space/Enter
// top-match) so the active kind tab narrows all of them consistently.
function _apdlgKindOk(label: LabelCandidate): boolean {
    return !_apdlgActiveKind || label.kind === _apdlgActiveKind;
}

document.querySelectorAll<HTMLElement>("#apdlg-label-kind-tabs .tad-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
        document.querySelectorAll("#apdlg-label-kind-tabs .tad-tab").forEach((t) => t.classList.remove("tad-tab--active"));
        tab.classList.add("tad-tab--active");
        _apdlgActiveKind = tab.dataset.tab || "";
        const searchEl = document.getElementById("apdlg-label-search") as HTMLInputElement;
        searchEl.focus();
        _apdlgShowSuggestions(searchEl.value);
    });
});

function _apdlgRenderSelectedChips(): void {
    const chips = document.getElementById("apdlg-label-chips");
    if (!chips) return;
    chips.innerHTML = "";
    _apdlgSelectedLabels.forEach((b) => {
        const chip = document.createElement("span");
        chip.className = "apdlg-label-chip-item";
        chip.dataset.id = String(b.id);
        const iconHtml = b.icon ? `<span class="apdlg-chip-icon">${b.icon}</span>` : "";
        chip.innerHTML = `${iconHtml}<span class="apdlg-chip-name">${_escHtml(b.name)}</span><button class="apdlg-chip-remove" type="button" aria-label="Remove">x</button>`;
        chip.querySelector(".apdlg-chip-remove")!.addEventListener("click", () => {
            _apdlgSelectedLabels = _apdlgSelectedLabels.filter((x) => x.id !== b.id);
            _apdlgRenderSelectedChips();
        });
        chips.appendChild(chip);
    });
}

function _apdlgShowSuggestions(query: string): void {
    const box = document.getElementById("apdlg-label-suggestions") as HTMLElement | null;
    if (!box) return;
    const q = query.toLowerCase().trim();
    const selectedIds = new Set(_apdlgSelectedLabels.map((b) => b.id));
    let matches = _apdlgAllLabels.filter((b) => !selectedIds.has(b.id) && _apdlgKindOk(b) && (!q || b.name.toLowerCase().includes(q)));
    matches = matches.slice(0, 12);
    box.innerHTML = "";
    matches.forEach((b) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "apdlg-label-sugg-item";
        const iconHtml = b.icon ? `<span class="apdlg-sugg-icon">${b.icon}</span>` : "";
        const kindLabel = `<span class="apdlg-sugg-kind apdlg-sugg-kind--${b.kind}">${b.kind}</span>`;
        item.innerHTML = `${iconHtml}<span class="apdlg-sugg-name">${_escHtml(b.name)}</span>${kindLabel}`;
        item.addEventListener("mousedown", (e) => {
            e.preventDefault();
            _apdlgSelectLabel(b);
        });
        box.appendChild(item);
    });
    // "Create a label" option at bottom
    const createBtn = document.createElement("button");
    createBtn.type = "button";
    createBtn.className = "apdlg-label-sugg-item apdlg-label-sugg-create";
    createBtn.innerHTML = '<i class="material-symbols-outlined">add_circle_outline</i><span>Create a label' + (q ? ` "${_escHtml(query.trim())}"` : "") + "</span>";
    createBtn.addEventListener("mousedown", (e) => {
        e.preventDefault();
        _apdlgPromptCreateLabel(query.trim());
    });
    box.appendChild(createBtn);
    box.hidden = false;
}

function _apdlgSelectLabel(label: LabelCandidate): void {
    if (_apdlgSelectedLabels.find((b) => b.id === label.id)) return;
    _apdlgSelectedLabels.push(label);
    _apdlgRenderSelectedChips();
    const input = document.getElementById("apdlg-label-search") as HTMLInputElement | null;
    if (input) {
        input.value = "";
        input.focus();
    }
    (document.getElementById("apdlg-label-suggestions") as HTMLElement).hidden = true;
}

function _apdlgPromptCreateLabel(prefilledName: string): void {
    (document.getElementById("apdlg-label-suggestions") as HTMLElement).hidden = true;
    // Show kind-picker mini dialog
    const kindDlg = document.getElementById("apdlg-create-label-dialog") as HTMLDialogElement | null;
    if (!kindDlg) return;
    const nameEl = document.getElementById("apdlg-cbname") as HTMLInputElement | null;
    if (nameEl) nameEl.value = prefilledName;
    kindDlg.showModal();
}

const labelInput = document.getElementById("apdlg-label-search") as HTMLInputElement | null;
if (labelInput) {
    labelInput.addEventListener("input", function (this: HTMLInputElement) {
        _apdlgShowSuggestions(this.value);
    });
    labelInput.addEventListener("focus", function (this: HTMLInputElement) {
        _apdlgShowSuggestions(this.value);
    });
    labelInput.addEventListener("blur", () => {
        setTimeout(() => {
            const box = document.getElementById("apdlg-label-suggestions") as HTMLElement | null;
            if (box) box.hidden = true;
        }, 150);
    });
    labelInput.addEventListener("keydown", function (this: HTMLInputElement, e: KeyboardEvent) {
        const box = document.getElementById("apdlg-label-suggestions") as HTMLElement | null;
        if (!box || box.hidden) return;
        const items = [...box.querySelectorAll<HTMLElement>(".apdlg-label-sugg-item:not(.apdlg-label-sugg-create)")];
        // Tab: select top real suggestion
        if (e.key === "Tab" && items.length) {
            e.preventDefault();
            const q = this.value.trim();
            const topMatch = _apdlgAllLabels.find((b) => !_apdlgSelectedLabels.find((s) => s.id === b.id) && _apdlgKindOk(b) && b.name.toLowerCase().includes(q.toLowerCase()));
            if (topMatch) _apdlgSelectLabel(topMatch);
            return;
        }
        // Comma or space: select top suggestion only if it's an exact/prefix match dead-end
        if ((e.key === "," || e.key === " ") && items.length) {
            const q = this.value.trim().toLowerCase();
            // Find if continuing to type would still match: any label that starts with q but isn't q
            const wouldContinue = _apdlgAllLabels.some((b) => !_apdlgSelectedLabels.find((s) => s.id === b.id) && _apdlgKindOk(b) && b.name.toLowerCase().startsWith(q) && b.name.toLowerCase() !== q);
            if (!wouldContinue && items.length === 1) {
                e.preventDefault();
                const topMatch = _apdlgAllLabels.find((b) => !_apdlgSelectedLabels.find((s) => s.id === b.id) && _apdlgKindOk(b) && b.name.toLowerCase().includes(q));
                if (topMatch) {
                    _apdlgSelectLabel(topMatch);
                    return;
                }
            }
        }
        // Arrow key navigation
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            const allItems = [...box.querySelectorAll<HTMLElement>(".apdlg-label-sugg-item")];
            const focused = box.querySelector<HTMLElement>(".apdlg-label-sugg-item:focus");
            let idx = focused ? allItems.indexOf(focused) : -1;
            idx = e.key === "ArrowDown" ? Math.min(idx + 1, allItems.length - 1) : Math.max(idx - 1, 0);
            allItems[idx]?.focus();
        }
        if (e.key === "Escape") {
            box.hidden = true;
        }
        if (e.key === "Enter") {
            const focused = box.querySelector<HTMLElement>(".apdlg-label-sugg-item:focus");
            if (focused) {
                e.preventDefault();
                focused.dispatchEvent(new MouseEvent("mousedown"));
                return;
            }
            // No item focused: mirror Tab and select the top real suggestion.
            if (items.length) {
                e.preventDefault();
                const q = this.value.trim();
                const topMatch = _apdlgAllLabels.find((b) => !_apdlgSelectedLabels.find((s) => s.id === b.id) && _apdlgKindOk(b) && b.name.toLowerCase().includes(q.toLowerCase()));
                if (topMatch) _apdlgSelectLabel(topMatch);
            }
        }
    });
}

// A label created inline from the Add/Edit Pin dialog is immediately usable
// there, but the Filter panel's label list is only rendered once at page
// load - append a matching chip so it's filterable/draggable without a reload.
function _appendFilterPanelLabelChip(label: LabelCandidate): void {
    const list = document.getElementById("fp-label-list");
    if (!list || list.querySelector(`[data-label-id="${label.id}"]`)) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "fp-chip fp-label-avail";
    btn.draggable = true;
    btn.dataset.labelId = String(label.id);
    btn.dataset.labelName = (label.name || "").toLowerCase();
    btn.dataset.labelText = label.name || "";
    btn.dataset.labelColor = label.color || "";
    btn.dataset.labelIcon = label.icon || "";
    btn.dataset.labelKind = label.kind || "";
    if (label.color) {
        btn.style.borderColor = label.color + "55";
        btn.style.background = label.color + "18";
        btn.style.color = label.color + "cc";
    }
    btn.innerHTML = (label.icon ? `<span style="font-size:.9em">${_escHtml(label.icon)}</span>` : "") + _escHtml(label.name || "");
    // No per-button wiring needed: the shared picker engine's delegated
    // list listeners (click/right-click/drag) cover appended buttons.
    list.appendChild(btn);
}

interface CreateLabelResponse {
    id: number;
    name: string;
    kind: string;
    icon?: string;
    color?: string;
}

// Create-a-label submit (from the mini dialog)
(() => {
    const submitBtn = document.getElementById("apdlg-cb-submit") as HTMLButtonElement | null;
    if (!submitBtn) return;
    submitBtn.addEventListener("click", () => {
        const name = ((document.getElementById("apdlg-cbname") as HTMLInputElement | null)?.value || "").trim();
        const kind = document.querySelector<HTMLInputElement>('input[name="apdlg-cb-kind"]:checked');
        if (!name || !kind) {
            toastr.warning("Enter a name and choose a type.");
            return;
        }
        submitBtn.disabled = true;
        const fd = new FormData();
        fd.append("name", name);
        const labelCreateUrls: Record<string, string> = {
            tag: MAP_CFG.urls.labelCreateTag,
            category: MAP_CFG.urls.labelCreateCategory,
            status: MAP_CFG.urls.labelCreateStatus,
        };
        const createUrl = labelCreateUrls[kind.value];
        if (!createUrl) {
            toastr.warning("Unknown label type.");
            submitBtn.disabled = false;
            return;
        }
        fetch(createUrl, {
            method: "POST",
            body: fd,
            headers: { "X-CSRFToken": MAP_CFG.csrfToken, Accept: "application/json" },
        })
            .then((r) => (r.ok ? (r.json() as Promise<CreateLabelResponse>) : r.text().then((t) => Promise.reject(t))))
            .then((data) => {
                const newLabel: LabelCandidate = { id: data.id, name: data.name, kind: data.kind, icon: data.icon || "", color: data.color || "" };
                _apdlgAllLabels.push(newLabel);
                _apdlgSelectLabel(newLabel);
                _appendFilterPanelLabelChip(newLabel);
                (document.getElementById("apdlg-create-label-dialog") as HTMLDialogElement).close();
                toastr.success(`Label "${data.name}" created.`);
            })
            .catch((err) => {
                toastr.error("Failed to create label: " + String(err));
            })
            .finally(() => {
                submitBtn.disabled = false;
            });
    });
})();

// -- Shared helper: fetch updated pin data and refresh map marker ---------
interface RefreshPinResponse {
    pin?: PinData;
    labels?: LabelDict;
}

function _refreshPinInStore(pinSlug: string, onDone: (() => void) | null): void {
    fetch(`${MAP_CFG.urls.mapPinJson}`.replace("placeholder-slug", pinSlug), {
        headers: { "X-Requested-With": "XMLHttpRequest" },
    })
        .then((r) => (r.ok ? (r.json() as Promise<RefreshPinResponse>) : null))
        .then((d) => {
            if (d && d.pin) {
                _mergeLabels(d.labels);
                window.updateCachedPin(d.pin);
            }
            onDone?.();
        })
        .catch(() => {
            window.invalidatePinCache();
            _refreshAllPins().then(onDone).catch(onDone);
        });
}

interface QuickEditResponse {
    pin_slug?: string;
}

interface ConflictingLocation {
    slug: string;
    display_name?: string;
    name?: string;
    is_current?: boolean;
    wiki_url?: string;
    existing_pin_url?: string;
    existing_pin_name?: string;
}

interface AddPinResponse {
    pin_slug?: string;
    pin_uuid?: string;
    conflicting_locations?: ConflictingLocation[];
}

// Form submit
document.getElementById("apdlg-submit")!.addEventListener("click", function (this: HTMLButtonElement) {
    const lat = (document.getElementById("apdlg-lat") as HTMLInputElement).value.trim();
    const lng = (document.getElementById("apdlg-lng") as HTMLInputElement).value.trim();

    if (_editPinUuid) {
        // -- Edit mode ------------------------------------------------------
        const formData = new FormData();
        formData.append("name", (document.getElementById("apdlg-name") as HTMLInputElement).value.trim());
        if (lat) formData.append("latitude", lat);
        if (lng) formData.append("longitude", lng);
        const iconVal = document.getElementById("icon-value-add-pin") as HTMLInputElement | null;
        formData.append("icon", iconVal && iconVal.value ? iconVal.value : "");
        if (window._addPinCustomIconFile) {
            formData.append("custom_icon", window._addPinCustomIconFile);
        } else {
            const clearCustomIconFlag = document.getElementById("edit-clear-custom-add-pin") as HTMLInputElement | null;
            if (clearCustomIconFlag && clearCustomIconFlag.value) formData.append("clear_custom_icon", clearCustomIconFlag.value);
        }
        const colorVal = document.getElementById("apdlg-color-value") as HTMLInputElement | null;
        formData.append("color", colorVal && colorVal.value ? colorVal.value : "");
        _apdlgSelectedLabels.forEach((b) => formData.append("label_ids", String(b.id)));
        // Send label_ids key even when empty so the server knows to clear labels
        if (!_apdlgSelectedLabels.length) formData.append("label_ids", "");

        const uuid = _editPinUuid;
        this.disabled = true;
        this.textContent = "Saving...";
        fetch(`/dashboard/map/quick-edit/${uuid}/`, {
            method: "POST",
            body: formData,
            headers: { "X-CSRFToken": MAP_CFG.csrfToken },
        })
            .then((r) => (r.ok ? r.json().catch(() => ({}) as QuickEditResponse) : r.text().then((t) => Promise.reject(t || r.statusText))))
            .then((data: QuickEditResponse) => {
                toastr.success("Pin updated!");
                closeAddPinDialog();
                if (data && data.pin_slug) {
                    _refreshPinInStore(data.pin_slug, null);
                } else {
                    _refreshPinInStore(uuid, null);
                }
                map.closePopup();
            })
            .catch((err) => toastr.error("Failed to save pin: " + String(err)))
            .finally(() => {
                this.disabled = false;
                this.textContent = "Save Changes";
            });
    } else {
        // -- Add mode -------------------------------------------------------
        if (!lat || !lng) {
            toastr.warning("Set a location first - click the map or enter an address.");
            return;
        }
        const formData = new FormData();
        // Leave name blank (never 'Unnamed Location') when the user
        // didn't type one - Pin.effective_name already falls back to the
        // location's own display name, live, forever. Submitting a
        // placeholder string here used to get saved as a real pin.name
        // AND get flagged name_is_user_provided=True (see post_add_pin's
        // bool((name or "").strip())), permanently locking the pin out of
        // ever picking up a better name once one becomes available - the
        // exact opposite of what leaving the field blank should mean.
        formData.append("name", (document.getElementById("apdlg-name") as HTMLInputElement).value.trim());
        formData.append("latitude", lat);
        formData.append("longitude", lng);

        // Icon
        const iconVal = document.getElementById("icon-value-add-pin") as HTMLInputElement | null;
        if (iconVal && iconVal.value) formData.append("icon", iconVal.value);
        if (window._addPinCustomIconFile) formData.append("custom_icon", window._addPinCustomIconFile);

        // Color
        const colorVal = document.getElementById("apdlg-color-value") as HTMLInputElement | null;
        if (colorVal && colorVal.value) formData.append("color", colorVal.value);

        // Labels (unified tags/categories/statuses)
        _apdlgSelectedLabels.forEach((b) => formData.append("label_ids", String(b.id)));

        // Pass Google Place ID and canonical name when adding from a Places marker.
        // The canonical name lets the server skip a geocoding API call.
        if (_addPinGooglePlaceId) formData.append("google_place_id", _addPinGooglePlaceId);
        if (_addPinCanonicalName) formData.append("place_canonical_name", _addPinCanonicalName);

        // Capture the place_id now - it will be cleared by _resetAddPinDialog inside
        // closeAddPinDialog, so we need a local copy for the success handler.
        const submittedPlaceId = _addPinGooglePlaceId;

        this.disabled = true;
        this.textContent = "Saving...";
        const pinLat = Number.parseFloat(lat);
        const pinLng = Number.parseFloat(lng);
        const pinName = String(formData.get("name") || "New Pin");
        fetch(MAP_CFG.urls.pinAdd, {
            method: "POST",
            body: formData,
            headers: { "X-CSRFToken": MAP_CFG.csrfToken },
        })
            .then((r) => (r.ok ? r.json().catch(() => ({}) as AddPinResponse) : r.text().then((t) => Promise.reject(t || r.statusText))))
            .then((data: AddPinResponse) => {
                toastr.success("Pin added!");
                closeAddPinDialog();
                // Fetch just the new pin's data and inject it into the store/map -
                // avoids a full reload of all pins on every add.
                const tempMarker = L.marker([pinLat, pinLng]).addTo(map);
                tempMarker.bindPopup(`<strong>${pinName}</strong><br><em>Saving...</em>`);
                if (data && data.pin_slug) {
                    _refreshPinInStore(data.pin_slug, () => map.removeLayer(tempMarker));
                } else {
                    window.invalidatePinCache();
                    _refreshAllPins()
                        .then(() => map.removeLayer(tempMarker))
                        .catch(() => map.removeLayer(tempMarker));
                }
                // When multiple locations cover the same coordinates, prompt the user to choose.
                if (data && data.conflicting_locations && data.conflicting_locations.length > 1 && data.pin_slug) {
                    _showLocationConflictPicker(data.pin_slug, data.pin_uuid ?? null, data.conflicting_locations);
                }
                // Remove the external-source marker that the user pinned - the user's
                // own pin now represents this place, so showing both would be redundant.
                if (submittedPlaceId) {
                    const gpm = _placesMarkerMap.get(submittedPlaceId);
                    if (gpm) {
                        _placesLayerGroup.removeLayer(gpm);
                        _placesMarkerMap.delete(submittedPlaceId);
                    }
                }
            })
            .catch((err) => toastr.error("Failed to add pin: " + String(err)))
            .finally(() => {
                this.disabled = false;
                this.textContent = "Add Pin";
            });
    }
});

// -- Location conflict picker -----------------------------------------------
function _showLocationConflictPicker(pinSlug: string, pinUuid: string | null, locations: ConflictingLocation[]): void {
    const dlg = document.getElementById("loc-conflict-dialog") as HTMLDialogElement | null;
    const list = document.getElementById("loc-conflict-list");
    const cancelBtn = document.getElementById("loc-conflict-cancel-btn") as HTMLButtonElement | null;
    if (!dlg || !list) return;

    function escapeHtml(s: unknown): string {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
    }
    function escapeAttr(s: unknown): string {
        return String(s).replace(/"/g, "&quot;");
    }

    list.innerHTML = "";
    locations.forEach((loc) => {
        const displayName = loc.display_name || loc.name || "Unnamed";
        const item = document.createElement("div");
        item.className = "loc-conflict-option" + (loc.is_current ? " loc-conflict-option--current" : "");
        let actions: string;
        if (loc.is_current) {
            actions = '<a href="' + loc.wiki_url + '" target="_blank" class="btn btn--ghost btn--sm">Wiki</a>';
        } else if (loc.existing_pin_url) {
            // A pin can only exist once per location for this profile - offer to
            // merge the new pin into the existing one instead of "switching" to it.
            actions =
                '<a href="' +
                loc.existing_pin_url +
                '" target="_blank" class="btn btn--ghost btn--sm">View pin</a>' +
                '<button class="btn btn--primary btn--sm loc-conflict-merge-btn" data-slug="' +
                escapeAttr(loc.slug) +
                '" data-name="' +
                escapeAttr(loc.existing_pin_name || displayName) +
                '">Merge into this pin</button>';
        } else {
            actions =
                '<a href="' +
                loc.wiki_url +
                '" target="_blank" class="btn btn--ghost btn--sm">Wiki</a>' +
                '<button class="btn btn--primary btn--sm loc-conflict-switch-btn" data-slug="' +
                escapeAttr(loc.slug) +
                '" data-name="' +
                escapeAttr(displayName) +
                '">Use this</button>';
        }
        item.innerHTML =
            '<div class="loc-conflict-option-name">' +
            escapeHtml(displayName) +
            (loc.is_current ? ' <span class="loc-conflict-badge">Current</span>' : "") +
            (loc.existing_pin_url ? ' <span class="loc-conflict-badge loc-conflict-badge--warn">You already have a pin here</span>' : "") +
            "</div>" +
            '<div class="loc-conflict-option-actions">' +
            actions +
            "</div>";
        list.appendChild(item);
    });

    function _linkPin(locSlug: string, btn: HTMLButtonElement, onSuccess: () => void): Promise<void> {
        return fetch("/dashboard/map/pin/" + pinSlug + "/link/" + locSlug + "/", {
            method: "POST",
            headers: { "X-CSRFToken": MAP_CFG.csrfToken, "X-Requested-With": "XMLHttpRequest" },
        })
            .then((r) => (r.ok ? r.json() : Promise.reject(new Error("link failed"))))
            .then(onSuccess)
            .catch(() => {
                toastr.error("Failed to update this pin's location.");
                btn.disabled = false;
            });
    }

    list.querySelectorAll<HTMLButtonElement>(".loc-conflict-switch-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            const locName = btn.dataset.name;
            btn.disabled = true;
            btn.textContent = "Switching...";
            void _linkPin(btn.dataset.slug!, btn, () => {
                toastr.success("Switched to " + locName);
                dlg.close();
                window.invalidatePinCache();
                void _refreshAllPins();
            });
        });
    });

    list.querySelectorAll<HTMLButtonElement>(".loc-conflict-merge-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            const locName = btn.dataset.name;
            btn.disabled = true;
            btn.textContent = "Merging...";
            void _linkPin(btn.dataset.slug!, btn, () => {
                toastr.success("Merged into " + locName);
                dlg.close();
                window.invalidatePinCache();
                void _refreshAllPins();
            });
        });
    });

    if (cancelBtn) {
        cancelBtn.onclick = () => {
            if (!pinUuid) {
                dlg.close();
                return;
            }
            cancelBtn.disabled = true;
            fetch(MAP_CFG.urls.pinBulkDelete, {
                method: "POST",
                headers: { "X-CSRFToken": MAP_CFG.csrfToken, "Content-Type": "application/json" },
                body: JSON.stringify({ uuids: [pinUuid] }),
            })
                .then((r) => (r.ok ? Promise.resolve() : Promise.reject(new Error("delete failed"))))
                .then(() => {
                    toastr.success("Pin creation cancelled.");
                    dlg.close();
                    window.invalidatePinCache();
                    void _refreshAllPins();
                })
                .catch(() => {
                    toastr.error("Failed to cancel pin creation.");
                })
                .finally(() => {
                    cancelBtn.disabled = false;
                });
        };
    }

    dlg.showModal();
}
