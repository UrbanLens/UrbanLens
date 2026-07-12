/**
 * Shared "jump to" location search: a debounced multi-source address bar
 * (local pins/locations, OpenStreetMap Nominatim, proxied Google Places,
 * coordinates, Plus Codes) with a keyboard-navigable suggestion dropdown,
 * search history, and a "My Location" GPS shortcut.
 *
 * `LocationSearchEngine.create(options)` owns no page-specific state (map
 * instance, marker layers, pin store, ...) and instead reports selections
 * through the onSelect/onMultiResult callbacks so each page decides what
 * "jumping" means for it (panning a Leaflet map, filling in a destination
 * field, etc). Currently used by the main map's address bar and the safety
 * check-in destination picker.
 */

interface SelectResult {
    lat: number;
    lng: number;
    zoom: number;
    title: string;
    type: string;
    pinSlug?: string;
    raw?: unknown;
}

interface MultiResult {
    searchTerm: string;
    anchorText: string;
    anchorLat: number;
    anchorLng: number;
    results: Array<{ lat: number; lng: number; title: string; raw: unknown }>;
}

interface RecentPin {
    slug: string;
    name?: string;
    lat: number;
    lng: number;
    url?: string;
}

interface SourceConfig {
    url: string;
}

export interface LocationSearchOptions {
    input: HTMLInputElement;
    suggestions: HTMLElement;
    bar?: HTMLElement | null;
    clearBtn?: HTMLElement | null;
    historyBtn?: HTMLElement | null;
    historyKey?: string | null;
    recentPinsKey?: string | null;
    sources?: {
        localPins?: SourceConfig;
        osmNominatim?: false;
        googlePlaces?: SourceConfig;
        topCities?: SourceConfig;
    };
    resolvePlaceUrl?: string | null;
    home?: { title?: string; subtitle?: string; lat: number; lng: number; zoom?: number } | null;
    enableMyLocation?: boolean;
    getUserLocationCache?: (() => { lat: number; lng: number } | null) | null;
    setUserLocationCache?: ((lat: number, lng: number) => void) | null;
    onGeolocationVisit?: ((lat: number, lng: number) => void) | null;
    defaultZoom?: number;
    onSelect: (result: SelectResult) => void;
    onMultiResult?: ((result: MultiResult) => void) | null;
    onSearchStart?: (() => void) | null;
    onFetchingChange?: ((on: boolean, message?: string) => void) | null;
    onToast?: ((level: "success" | "error" | "warning" | "info", message: string) => void) | null;
}

export interface LocationSearchEngineInstance {
    search: (query: string) => void;
    showEmptySuggestions: () => void;
    clear: () => void;
    recenterToUserLocation: () => void;
    trackRecentPin: (entry: RecentPin) => void;
}

interface SuggestionResult {
    type: string;
    icon?: string;
    title: string;
    subtitle?: string;
    geocodeQuery?: string;
    searchQuery?: string;
    externalUrl?: string;
    action?: string;
    lat?: number;
    lng?: number;
    zoom?: number;
    pin_slug?: string;
    place_id?: string;
    raw?: unknown;
}

function escHtml(s: string): string {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

// Matches full Plus Codes (XXXXXXXX+XX) and shortened codes (XXXX+XX City)
const PLUS_CODE_RE = /^([23456789CFGHJMPQRVWXcfghjmpqrvwx]{4,8}\+[23456789CFGHJMPQRVWXcfghjmpqrvwx]{0,2})([\s,].*)?$/;

export function isPlusCode(q: string): boolean {
    return PLUS_CODE_RE.test((q || "").trim());
}

async function resolvePlusCode(q: string): Promise<{ lat: number; lng: number } | null> {
    // Google Maps JS API types aren't part of this project's TS setup; this
    // branch is optional and feature-detected, so `any` is intentional here.
    const googleMaps = (window as unknown as { google?: { maps?: { Geocoder?: new () => any } } }).google?.maps;
    if (googleMaps?.Geocoder) {
        return new Promise((resolve) => {
            new googleMaps.Geocoder!().geocode({ address: q.trim() }, (results: any[], status: string) => {
                if (status === "OK" && results && results.length > 0) {
                    const loc = results[0].geometry.location;
                    resolve({ lat: loc.lat(), lng: loc.lng() });
                } else {
                    resolve(null);
                }
            });
        });
    }
    try {
        const r = await fetch(`https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(q.trim())}&format=json&limit=1`, {
            headers: { Accept: "application/json", "Accept-Language": "en" },
        });
        const data = await r.json();
        if (data && data.length > 0) return { lat: Number.parseFloat(data[0].lat), lng: Number.parseFloat(data[0].lon) };
    } catch {
        /* network error - treated as unresolved below */
    }
    return null;
}

export function parseCoordinates(q: string): { lat: number; lng: number } | null {
    const m1 = q.trim().match(/^(-?\d{1,3}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)$/);
    if (m1) {
        const a = Number.parseFloat(m1[1]!);
        const b = Number.parseFloat(m1[2]!);
        if (Number.isFinite(a) && Number.isFinite(b)) {
            if (Math.abs(a) <= 90 && Math.abs(b) <= 180) return { lat: a, lng: b };
            if (Math.abs(b) <= 90 && Math.abs(a) <= 180) return { lat: b, lng: a };
        }
    }
    const dmsRe = /(\d{1,3})[°\s]+(\d{1,2})['\s]+(\d{1,2}(?:\.\d+)?)["\s]*([NSns])\s+(\d{1,3})[°\s]+(\d{1,2})['\s]+(\d{1,2}(?:\.\d+)?)["\s]*([EWew])/;
    const m2 = q.match(dmsRe);
    if (m2) {
        const lat = (Number.parseFloat(m2[1]!) + Number.parseFloat(m2[2]!) / 60 + Number.parseFloat(m2[3]!) / 3600) * (/[Ss]/.test(m2[4]!) ? -1 : 1);
        const lng = (Number.parseFloat(m2[5]!) + Number.parseFloat(m2[6]!) / 60 + Number.parseFloat(m2[7]!) / 3600) * (/[Ww]/.test(m2[8]!) ? -1 : 1);
        if (Math.abs(lat) <= 90 && Math.abs(lng) <= 180) return { lat, lng };
    }
    return null;
}

function sectionKey(label: string): string {
    const l = label.toLowerCase();
    if (l.includes("pin") || l.includes("location")) return "pins";
    if (l.includes("google")) return "places";
    if (l.includes("place") || l.includes("address")) return "suggestions";
    if (l.includes("cit")) return "cities";
    if (l.includes("navigation") || l.includes("quick")) return "navigation";
    if (l.includes("recent") || l.includes("history")) return "history";
    if (l.includes("coord")) return "coordinates";
    return "suggestions";
}

async function nominatimSearch(query: string, { limit = 5, viewbox = null as string | null } = {}): Promise<any[]> {
    const url =
        `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=${limit}&addressdetails=1`
        + (viewbox ? `&viewbox=${viewbox}&bounded=0` : "");
    const r = await fetch(url, { headers: { Accept: "application/json", "Accept-Language": "en" } });
    return r.json();
}

function generateDerivedSuggestions(query: string): SuggestionResult[] {
    const results: SuggestionResult[] = [];
    const words = query.trim().split(/\s+/).filter((w) => w.length > 0);
    if (words.length >= 2) {
        const last = words[words.length - 1]!;
        const rest = words.slice(0, -1).join(" ");
        results.push({
            type: "derived",
            icon: "search",
            title: `Search for "${rest}" near ${last}`,
            subtitle: "Jump to result",
            geocodeQuery: `${rest} near ${last}`,
        });
        if (words.length === 2) {
            results.push({
                type: "derived",
                icon: "search",
                title: `Search for "${words[1]} ${words[0]}"`,
                subtitle: "Jump to result",
                geocodeQuery: `${words[1]} ${words[0]}`,
            });
        }
    }
    results.push({
        type: "external",
        icon: "open_in_new",
        title: `Search Google Maps for "${query}"`,
        subtitle: "Opens in a new tab",
        externalUrl: `https://maps.google.com/maps?q=${encodeURIComponent(query)}`,
    });
    return results;
}

function create(options: LocationSearchOptions): LocationSearchEngineInstance {
    const {
        input,
        suggestions,
        bar,
        clearBtn = null,
        historyBtn = null,
        historyKey = null,
        recentPinsKey = null,
        sources = {},
        resolvePlaceUrl = null,
        home = null,
        enableMyLocation = true,
        getUserLocationCache = null,
        setUserLocationCache = null,
        onGeolocationVisit = null,
        defaultZoom = 15,
        onSelect,
        onMultiResult = null,
        onSearchStart = null,
        onFetchingChange = null,
        onToast = null,
    } = options;

    const barEl = bar || input.parentElement!;

    function toast(level: "success" | "error" | "warning" | "info", message: string): void {
        if (onToast) {
            onToast(level, message);
            return;
        }
        if (typeof window.toastr !== "undefined") {
            (window.toastr[level] ?? window.toastr.info)(message);
            return;
        }
        if (level === "error") console.error(message);
        else console.warn(message);
    }

    function setFetching(on: boolean, message?: string): void {
        onFetchingChange?.(on, message);
    }

    function getHistory(): string[] {
        if (!historyKey) return [];
        try {
            const raw = localStorage.getItem(historyKey);
            const parsed = JSON.parse(raw ?? "[]");
            return Array.isArray(parsed) ? parsed : [];
        } catch {
            return [];
        }
    }

    function addToHistory(q: string): void {
        if (!historyKey || !q || !q.trim()) return;
        try {
            const deduped = getHistory().filter((x) => x !== q);
            deduped.unshift(q);
            localStorage.setItem(historyKey, JSON.stringify(deduped.slice(0, 20)));
        } catch {
            /* storage unavailable - ignore */
        }
    }

    function getRecentPins(limit: number): RecentPin[] {
        if (!recentPinsKey) return [];
        try {
            const raw = localStorage.getItem(recentPinsKey);
            const list = raw ? JSON.parse(raw) : [];
            return list.slice(0, limit);
        } catch {
            return [];
        }
    }

    function trackRecentPin(entry: RecentPin): void {
        if (!recentPinsKey) return;
        try {
            const raw = localStorage.getItem(recentPinsKey);
            const list = raw ? JSON.parse(raw) : [];
            const filtered = list.filter((p: RecentPin) => p.slug !== entry.slug);
            filtered.unshift(entry);
            localStorage.setItem(recentPinsKey, JSON.stringify(filtered.slice(0, 10)));
        } catch {
            /* storage unavailable - ignore */
        }
    }

    function getCachedUserLocation(): { lat: number; lng: number } | null {
        return getUserLocationCache ? getUserLocationCache() : null;
    }

    function cacheUserLocation(lat: number, lng: number): void {
        setUserLocationCache?.(lat, lng);
    }

    let addrBarTimer: ReturnType<typeof setTimeout> | undefined;
    let historyNavIdx = -1;
    let activeIdx = -1;
    let searchSeq = 0;
    let updateHistoryBtn: () => void = () => {};

    function makeCollapsibleHdr(label: string, key: string, slot: HTMLElement): HTMLElement {
        const hdr = document.createElement("div");
        hdr.className = "addr-suggestion-group-hdr";
        hdr.dataset.collapsible = "1";
        hdr.dataset.section = key;
        hdr.textContent = label;
        const hint = document.createElement("span");
        hint.className = "addr-section-collapsed-hint";
        hdr.appendChild(hint);
        hdr.addEventListener("click", () => {
            slot.classList.toggle("is-collapsed");
            const collapsed = slot.classList.contains("is-collapsed");
            hdr.dataset.collapsed = collapsed ? "1" : "";
            const count = slot.querySelectorAll(".addr-suggestion").length;
            hint.textContent = collapsed ? `${count} suggestion${count !== 1 ? "s" : ""}` : "";
        });
        return hdr;
    }

    function makeSlot(container: HTMLElement): HTMLElement {
        const div = document.createElement("div");
        div.className = "addr-source-slot";
        container.appendChild(div);
        return div;
    }

    function highlight(idx: number): void {
        const items = [...suggestions.querySelectorAll<HTMLElement>(".addr-suggestion")];
        activeIdx = Math.max(-1, Math.min(items.length - 1, idx));
        items.forEach((el, i) => el.classList.toggle("addr-suggestion--active", i === activeIdx));
        if (activeIdx >= 0) items[activeIdx]?.scrollIntoView({ block: "nearest" });
    }

    function clearSearch(): void {
        const wasEmpty = !input.value.trim();
        input.value = "";
        clearBtn?.classList.remove("addr-search-clear--visible");
        suggestions.hidden = true;
        searchSeq++;
        activeIdx = -1;
        historyNavIdx = -1;
        if (!wasEmpty) input.focus();
        updateHistoryBtn();
    }

    async function runNearQuery(geocodeQuery: string, fallbackTitle: string): Promise<void> {
        onSearchStart?.();
        setFetching(true, "Searching…");
        try {
            const nearMatch = geocodeQuery.match(/^(.+?) near (.+)$/i);
            if (!nearMatch) {
                const data = await nominatimSearch(geocodeQuery, { limit: 1 });
                if (data?.length) {
                    const lat = Number.parseFloat(data[0].lat);
                    const lng = Number.parseFloat(data[0].lon);
                    onSelect({ lat, lng, zoom: 14, title: data[0].display_name || fallbackTitle, type: "address", raw: data[0] });
                } else {
                    toast("warning", `No results for "${geocodeQuery}"`);
                }
                return;
            }
            const searchTerm = nearMatch[1]!.trim();
            const anchorText = nearMatch[2]!.trim();
            const anchorData = await nominatimSearch(anchorText, { limit: 1 });
            const anchorLat = anchorData[0] ? Number.parseFloat(anchorData[0].lat) : null;
            const anchorLng = anchorData[0] ? Number.parseFloat(anchorData[0].lon) : null;

            if (anchorLat == null) {
                toast("warning", `Couldn't find "${anchorText}" - try a more specific location`);
                return;
            }

            const pad = 0.5;
            const searchData = await nominatimSearch(searchTerm, {
                limit: 5,
                viewbox: `${anchorLng! - pad},${anchorLat - pad},${anchorLng! + pad},${anchorLat + pad}`,
            });

            if (!searchData?.length) {
                onSelect({ lat: anchorLat, lng: anchorLng!, zoom: 13, title: anchorText, type: "address" });
                toast("warning", `No "${searchTerm}" found near ${anchorText}`);
                return;
            }

            const results = searchData.map((r: any) => ({
                lat: Number.parseFloat(r.lat),
                lng: Number.parseFloat(r.lon),
                title: r.display_name || searchTerm,
                raw: r,
            }));

            if (onMultiResult) {
                onMultiResult({ searchTerm, anchorText, anchorLat, anchorLng: anchorLng!, results });
            } else {
                const first = results[0]!;
                onSelect({ lat: first.lat, lng: first.lng, zoom: 14, title: first.title, type: "address" });
                if (results.length > 1) toast("info", `Found ${results.length} results for "${searchTerm}"`);
            }
        } catch {
            toast("error", "Search failed - check your connection.");
        } finally {
            setFetching(false);
        }
    }

    function buildSuggestionItem(result: SuggestionResult): HTMLButtonElement {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `addr-suggestion addr-suggestion--${result.type}`;

        const subtitle = result.subtitle ? `<span class="addr-suggestion-sub">${escHtml(result.subtitle)}</span>` : "";
        btn.innerHTML = `
            <i class="material-icons addr-suggestion-icon">${escHtml(result.icon || "place")}</i>
            <span class="addr-suggestion-content">
                <span class="addr-suggestion-title">${escHtml(result.title)}</span>
                ${subtitle}
            </span>`;

        btn.addEventListener("mousedown", async (e) => {
            e.preventDefault();
            suggestions.hidden = true;
            activeIdx = -1;

            if (result.externalUrl) {
                window.open(result.externalUrl, "_blank", "noopener");
                return;
            }

            if (result.geocodeQuery) {
                input.value = result.geocodeQuery;
                clearBtn?.classList.add("addr-search-clear--visible");
                updateHistoryBtn();
                addToHistory(result.geocodeQuery);
                await runNearQuery(result.geocodeQuery, result.title);
                return;
            }

            if (result.searchQuery) {
                input.value = result.searchQuery;
                clearBtn?.classList.add("addr-search-clear--visible");
                updateHistoryBtn();
                addToHistory(result.searchQuery);
                startMultiSearch(result.searchQuery);
                suggestions.hidden = false;
                return;
            }

            if (result.action === "gps") {
                input.value = "My Location";
                clearBtn?.classList.add("addr-search-clear--visible");
                updateHistoryBtn();
                recenterToUserLocation();
                return;
            }

            input.value = result.title;
            clearBtn?.classList.add("addr-search-clear--visible");
            updateHistoryBtn();
            addToHistory(result.title);

            if (result.lat != null && result.lng != null) {
                onSelect({
                    lat: result.lat,
                    lng: result.lng,
                    zoom: result.zoom || defaultZoom,
                    title: result.title,
                    type: result.type,
                    pinSlug: result.pin_slug,
                    raw: result,
                });
            } else if (result.place_id) {
                if (!resolvePlaceUrl) return;
                try {
                    const r = await fetch(`${resolvePlaceUrl}?place_id=${encodeURIComponent(result.place_id)}`, {
                        headers: { "X-Requested-With": "XMLHttpRequest" },
                    });
                    if (r.ok) {
                        const d = await r.json();
                        if (d.lat != null) {
                            if (d.name) input.value = d.name;
                            onSelect({ lat: d.lat, lng: d.lng, zoom: result.zoom || defaultZoom, title: d.name || result.title, type: result.type, raw: d });
                        }
                    } else {
                        toast("error", "Could not resolve location - try searching again.");
                    }
                } catch {
                    toast("error", "Could not resolve location - check your connection.");
                }
            }
        });
        return btn;
    }

    async function fetchSourceIntoSlot(
        seq: number,
        label: string,
        url: string,
        parser: (raw: any) => SuggestionResult[],
        slot: HTMLElement,
        onDone?: (hasResults: boolean) => void,
        fetchOpts?: RequestInit,
    ): Promise<void> {
        slot.innerHTML = `<div class="addr-source-loading" data-seq="${seq}">
            <span class="addr-spinner"></span><span class="addr-source-loading-label">${escHtml(label)}...</span>
        </div>`;
        suggestions.hidden = false;
        try {
            const resp = await fetch(url, fetchOpts ?? { headers: { "X-Requested-With": "XMLHttpRequest" } });
            if (seq !== searchSeq) return;
            slot.innerHTML = "";
            if (!resp.ok) {
                onDone?.(false);
                return;
            }
            const raw = await resp.json();
            if (seq !== searchSeq) return;
            const results = parser(raw);
            if (!results?.length) {
                onDone?.(false);
                return;
            }
            slot.appendChild(makeCollapsibleHdr(label, sectionKey(label), slot));
            for (const r of results) slot.appendChild(buildSuggestionItem(r));
            suggestions.hidden = false;
            highlight(-1);
            onDone?.(true);
        } catch {
            if (seq === searchSeq) {
                slot.innerHTML = "";
                onDone?.(false);
            }
        }
    }

    function recenterToUserLocation(): void {
        if (!navigator.geolocation) {
            toast("warning", "Geolocation is not supported by your browser.");
            return;
        }
        suggestions.hidden = true;
        const cached = getCachedUserLocation();
        if (cached) {
            onSelect({ lat: cached.lat, lng: cached.lng, zoom: defaultZoom, title: "My Location", type: "mylocation" });
        } else {
            setFetching(true, "Getting your location…");
        }
        navigator.geolocation.getCurrentPosition(
            (pos) => {
                cacheUserLocation(pos.coords.latitude, pos.coords.longitude);
                onGeolocationVisit?.(pos.coords.latitude, pos.coords.longitude);
                onSelect({ lat: pos.coords.latitude, lng: pos.coords.longitude, zoom: defaultZoom, title: "My Location", type: "mylocation" });
                if (!cached) setFetching(false);
            },
            () => {
                if (!cached) {
                    setFetching(false);
                    toast("warning", "Could not get your location. Check permissions.");
                }
            },
            { timeout: 8000, maximumAge: 300000 },
        );
    }

    function geocodeAddress(): void {
        const q = (input.value || "").trim();
        if (!q) return;

        const items = [...suggestions.querySelectorAll<HTMLElement>(".addr-suggestion")];
        if (activeIdx >= 0 && activeIdx < items.length) {
            items[activeIdx]!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
            return;
        }

        suggestions.hidden = true;

        const coords = parseCoordinates(q);
        if (coords) {
            onSelect({ lat: coords.lat, lng: coords.lng, zoom: 16, title: q, type: "coordinates" });
            return;
        }

        if (isPlusCode(q)) {
            setFetching(true, "Resolving Plus Code…");
            resolvePlusCode(q)
                .then((resolved) => {
                    setFetching(false);
                    if (resolved) {
                        onSelect({ lat: resolved.lat, lng: resolved.lng, zoom: 16, title: q, type: "plus_code" });
                    } else {
                        toast("warning", "Could not resolve Plus Code - try adding a city name.");
                    }
                })
                .catch(() => {
                    setFetching(false);
                    toast("error", "Plus Code resolution failed.");
                });
            return;
        }

        addToHistory(q);
        nominatimSearch(q, { limit: 1 })
            .then((results) => {
                if (!results.length) {
                    toast("warning", "Address not found.");
                    return;
                }
                onSelect({ lat: Number.parseFloat(results[0].lat), lng: Number.parseFloat(results[0].lon), zoom: 16, title: results[0].display_name || q, type: "address", raw: results[0] });
            })
            .catch(() => toast("error", "Geocoding failed - check your connection."));
    }

    function startMultiSearch(query: string): void {
        onSearchStart?.();
        const seq = ++searchSeq;
        const box = suggestions;

        box.innerHTML = "";
        box.hidden = true;
        activeIdx = -1;

        const coordSlot = makeSlot(box);
        const localSlot = sources.localPins ? makeSlot(box) : null;
        const osmSlot = sources.osmNominatim !== false ? makeSlot(box) : null;
        const placesSlot = sources.googlePlaces ? makeSlot(box) : null;
        const noMsgSlot = makeSlot(box);
        const derivedSlot = makeSlot(box);

        let pendingSources = [localSlot, osmSlot, placesSlot].filter(Boolean).length;
        let primaryHits = 0;
        function onPrimaryDone(hasResults: boolean): void {
            if (hasResults) primaryHits++;
            if (--pendingSources === 0 && primaryHits === 0 && !parseCoordinates(query) && !isPlusCode(query)) {
                noMsgSlot.innerHTML = '<div class="addr-no-results">No exact matches found</div>';
            }
        }

        const coords = parseCoordinates(query);
        if (coords) {
            coordSlot.appendChild(makeCollapsibleHdr("Coordinates", "coordinates", coordSlot));
            coordSlot.appendChild(
                buildSuggestionItem({
                    type: "coordinates",
                    title: `${coords.lat.toFixed(6)}, ${coords.lng.toFixed(6)}`,
                    subtitle: "Jump to these exact coordinates",
                    lat: coords.lat,
                    lng: coords.lng,
                    zoom: 16,
                    icon: "my_location",
                }),
            );
            box.hidden = false;
        } else if (isPlusCode(query)) {
            coordSlot.appendChild(makeCollapsibleHdr("Plus Code", "coordinates", coordSlot));
            const pcBtn = buildSuggestionItem({ type: "plus_code", title: query.trim(), subtitle: "Jump to this Plus Code location", icon: "pin_drop" });
            // Override the generic click handler: lat/lng must be resolved asynchronously first.
            const freshBtn = pcBtn.cloneNode(true) as HTMLButtonElement;
            freshBtn.addEventListener(
                "mousedown",
                async (e) => {
                    e.preventDefault();
                    e.stopImmediatePropagation();
                    box.hidden = true;
                    activeIdx = -1;
                    setFetching(true, "Resolving Plus Code…");
                    try {
                        const resolved = await resolvePlusCode(query);
                        if (resolved) {
                            onSelect({ lat: resolved.lat, lng: resolved.lng, zoom: 16, title: query.trim(), type: "plus_code" });
                        } else {
                            toast("warning", "Could not resolve Plus Code - try adding a city name.");
                        }
                    } catch {
                        toast("error", "Plus Code resolution failed.");
                    } finally {
                        setFetching(false);
                    }
                },
                true,
            );
            coordSlot.appendChild(freshBtn);
            box.hidden = false;
        }

        if (localSlot) {
            fetchSourceIntoSlot(seq, "Your Pins & Locations", `${sources.localPins!.url}?q=${encodeURIComponent(query)}`, (data) => data.results || [], localSlot, onPrimaryDone);
        }

        if (osmSlot) {
            fetchSourceIntoSlot(
                seq,
                "Places & Addresses",
                `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=5&addressdetails=1`,
                (data) =>
                    (data || []).map((r: any) => ({
                        type: "address",
                        title: r.name || (r.display_name || "").split(",")[0].trim(),
                        subtitle: r.display_name || "",
                        lat: Number.parseFloat(r.lat),
                        lng: Number.parseFloat(r.lon),
                        zoom: 15,
                        icon: "place",
                    })),
                osmSlot,
                onPrimaryDone,
                { headers: { Accept: "application/json", "Accept-Language": "en" } },
            );
        }

        if (placesSlot) {
            fetchSourceIntoSlot(seq, "Google Places", `${sources.googlePlaces!.url}?q=${encodeURIComponent(query)}`, (data) => (data.disabled ? [] : data.results || []), placesSlot, (hasResults) => {
                onPrimaryDone(hasResults);
                // Google Places already found the place, so the "Search Google Maps for..."
                // entry would be redundant - remove it to avoid showing the same result twice.
                if (hasResults) {
                    derivedSlot.querySelectorAll(".addr-suggestion--external").forEach((el) => el.remove());
                    if (!derivedSlot.querySelector(".addr-suggestion")) {
                        derivedSlot.querySelector(".addr-suggestion-group-hdr")?.remove();
                    }
                }
            });
        }

        const derived = generateDerivedSuggestions(query);
        derivedSlot.appendChild(makeCollapsibleHdr("Search Suggestions", "suggestions", derivedSlot));
        for (const r of derived) derivedSlot.appendChild(buildSuggestionItem(r));
        box.hidden = false;
    }

    function showEmptySuggestions(): void {
        const seq = ++searchSeq;
        const box = suggestions;

        box.innerHTML = "";
        activeIdx = -1;

        function emptySection(label: string, key: string): HTMLElement {
            const slot = document.createElement("div");
            slot.className = "addr-source-slot";
            slot.appendChild(makeCollapsibleHdr(label, key, slot));
            box.appendChild(slot);
            return slot;
        }

        const history = getHistory();
        if (history.length) {
            const histSlot = emptySection("Recent Searches", "history");
            for (const q of history.slice(0, 3)) {
                histSlot.appendChild(buildSuggestionItem({ type: "history", icon: "history", title: q, subtitle: "Recent search", searchQuery: q }));
            }
        }

        const hasGeo = enableMyLocation && !!navigator.geolocation;
        const hasHome = !!home;
        if (hasGeo || hasHome) {
            const navSlot = emptySection("Quick Navigation", "navigation");
            if (hasGeo) {
                navSlot.appendChild(buildSuggestionItem({ type: "mylocation", icon: "my_location", title: "My Location", subtitle: "Jump to your current GPS position", action: "gps" }));
            }
            if (hasHome) {
                navSlot.appendChild(
                    buildSuggestionItem({
                        type: "home",
                        icon: "home",
                        title: home!.title || "Home",
                        subtitle: home!.subtitle || "Default map center",
                        lat: home!.lat,
                        lng: home!.lng,
                        zoom: home!.zoom || defaultZoom,
                    }),
                );
            }
        }

        const recentPins = getRecentPins(2);
        if (recentPins.length) {
            const recentSlot = emptySection("Recently Viewed", "recent");
            for (const pin of recentPins) {
                recentSlot.appendChild(buildSuggestionItem({ type: "pin", icon: "push_pin", title: pin.name || "Unnamed", subtitle: "Recently viewed", lat: pin.lat, lng: pin.lng, zoom: 16, pin_slug: pin.slug }));
            }
        }

        let citySlot: HTMLElement | null = null;
        if (sources.topCities) {
            citySlot = makeSlot(box);
            fetchSourceIntoSlot(seq, "Your Top Cities", sources.topCities.url, (data) => data.results || [], citySlot, (hasResults) => {
                if (!hasResults && !box.querySelectorAll(".addr-suggestion").length) box.hidden = true;
            });
        }

        const hasStatic = box.querySelectorAll(".addr-suggestion").length > 0;
        box.hidden = !hasStatic && !(citySlot && citySlot.firstChild);
        if (citySlot?.firstChild) box.hidden = false;
    }

    let mouseOverSuggestions = false;
    let blurTimer: ReturnType<typeof setTimeout> | undefined;

    updateHistoryBtn = () => {
        const isEmpty = !input.value.trim();
        const hasHistory = getHistory().length > 0;
        historyBtn?.classList.toggle("addr-search-history--visible", isEmpty && hasHistory);
    };

    suggestions.addEventListener("mouseenter", () => {
        mouseOverSuggestions = true;
    });
    suggestions.addEventListener("mouseleave", () => {
        mouseOverSuggestions = false;
    });
    suggestions.addEventListener("mouseup", () => {
        if (mouseOverSuggestions && document.activeElement !== input) input.focus();
    });

    function hideSuggestionsSoon(): void {
        clearTimeout(blurTimer);
        blurTimer = setTimeout(() => {
            if (!barEl.contains(document.activeElement) && !mouseOverSuggestions) {
                suggestions.hidden = true;
                activeIdx = -1;
            }
        }, 200);
    }

    barEl.addEventListener("mousedown", (e) => {
        const target = e.target as HTMLElement;
        if (target.closest(".addr-search-history, .addr-search-clear, .addr-suggestion")) return;
        if (target !== input) {
            e.preventDefault();
            input.focus();
        }
    });

    suggestions.addEventListener("mousedown", (e) => e.preventDefault());
    suggestions.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });

    historyBtn?.addEventListener("click", () => {
        const hist = getHistory();
        if (!hist.length) return;
        historyNavIdx = (historyNavIdx + 1) % hist.length;
        input.value = hist[historyNavIdx]!;
        clearBtn?.classList.add("addr-search-clear--visible");
        updateHistoryBtn();
        startMultiSearch(hist[historyNavIdx]!);
    });

    clearBtn?.addEventListener("click", clearSearch);

    input.addEventListener("input", function () {
        const q = this.value.trim();
        clearBtn?.classList.toggle("addr-search-clear--visible", !!q);
        updateHistoryBtn();
        clearTimeout(addrBarTimer);
        if (!q) {
            searchSeq++;
            activeIdx = -1;
            historyNavIdx = -1;
            showEmptySuggestions();
            return;
        }
        suggestions.hidden = true;
        addrBarTimer = setTimeout(() => startMultiSearch(q), 250);
    });

    input.addEventListener("keydown", function (e) {
        const items = [...suggestions.querySelectorAll<HTMLElement>(".addr-suggestion")];
        if (e.key === "ArrowDown") {
            e.preventDefault();
            highlight(activeIdx + 1);
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            if (!this.value.trim() && activeIdx < 0) {
                const hist = getHistory().slice(0, 10);
                if (!hist.length) {
                    if (suggestions.hidden) showEmptySuggestions();
                    return;
                }
                historyNavIdx = historyNavIdx < 0 ? 0 : (historyNavIdx + 1) % hist.length;
                const chosen = hist[historyNavIdx]!;
                this.value = chosen;
                clearBtn?.classList.add("addr-search-clear--visible");
                updateHistoryBtn();
                suggestions.hidden = true;
                activeIdx = -1;
                clearTimeout(addrBarTimer);
                addrBarTimer = setTimeout(() => startMultiSearch(chosen), 200);
            } else {
                highlight(activeIdx - 1);
            }
        } else if (e.key === "Enter") {
            e.preventDefault();
            if (activeIdx >= 0 && activeIdx < items.length) {
                items[activeIdx]!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
            } else {
                geocodeAddress();
            }
        } else if (e.key === "Escape") {
            clearSearch();
        }
    });

    input.addEventListener("blur", hideSuggestionsSoon);

    input.addEventListener("focus", function () {
        clearTimeout(blurTimer);
        updateHistoryBtn();
        if (!this.value.trim()) {
            showEmptySuggestions();
        } else if (suggestions.children.length) {
            suggestions.hidden = false;
        }
    });

    updateHistoryBtn();

    return {
        search: startMultiSearch,
        showEmptySuggestions,
        clear: clearSearch,
        recenterToUserLocation,
        trackRecentPin,
    };
}

export type LocationSearchAttachOptions = Omit<LocationSearchOptions, "input" | "suggestions" | "bar" | "clearBtn" | "historyBtn">;

/**
 * Binds the engine to a search bar rendered by the shared
 * {% map_search_bar prefix %} template tag (see
 * dashboard/templatetags/map_components.py). The tag emits a fixed id scheme
 * - `{prefix}-search-bar/-input/-history/-clear/-suggestions` - so callers
 * only supply the prefix and the page-specific callbacks.
 *
 * @param prefix - The id prefix passed to the template tag.
 * @param options - Engine options minus the element references.
 * @returns The engine instance, or null when the bar isn't on the page.
 */
function attach(prefix: string, options: LocationSearchAttachOptions): LocationSearchEngineInstance | null {
    const byId = (suffix: string) => document.getElementById(`${prefix}-search-${suffix}`);
    const input = byId("input") as HTMLInputElement | null;
    const suggestions = byId("suggestions");
    if (!input || !suggestions) return null;
    return create({
        input,
        suggestions,
        bar: document.getElementById(`${prefix}-search-bar`),
        clearBtn: byId("clear"),
        historyBtn: byId("history"),
        ...options,
    });
}

export const LocationSearchEngine = { create, attach };

export function installGlobalLocationSearchEngine(): void {
    window.LocationSearchEngine = LocationSearchEngine;
}

declare global {
    interface Window {
        LocationSearchEngine: typeof LocationSearchEngine;
    }
}
