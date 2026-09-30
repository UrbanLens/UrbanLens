/**
 * The Memories page (pages/memories/index.html): a map and a month-grouped timeline of routes, trips, visits
 * and photos, fed a page at a time by `memories.data`.
 */

import { escHtml } from "../shared/escape-html";
import { openVisitDialog } from "../shared/visit-dialog";
import { createClusterGroup, type PinClusterGroup } from "../shared/map-clusters";

declare const L: typeof import("leaflet");

type MarkerType = "trip" | "visit" | "photo";

interface MemoryEvent {
    type: string;
    occurred_at: string;
    title?: string;
    subtitle?: string;
    url?: string;
    icon?: string;
    color?: string;
    thumbnail_url?: string;
    latitude?: number | null;
    longitude?: number | null;
    extra?: { path_geojson?: string; visit_id?: number | string; pin_slug?: string };
}

interface FeedPage {
    events?: MemoryEvent[];
    truncated?: boolean;
    next_before?: string;
}

const TYPE_ICONS: Record<string, string> = { route: "route", trip: "luggage", visit: "pin_drop", photo: "photo_camera" };
const US_CENTER: [number, number] = [39.8283, -98.5795];

function byId<T extends HTMLElement>(id: string): T {
    const el = document.getElementById(id);
    if (!el) throw new Error(`memories: #${id} is missing`);
    return el as T;
}

function isoDay(date: Date): string {
    return date.toISOString().slice(0, 10);
}

function initMemories(root: HTMLElement): void {
    const cfg = root.dataset;
    const feedUrl = cfg.feedUrl ?? "";
    const today = cfg.today ?? isoDay(new Date());
    const visitUrlBase = cfg.visitUrlBase ?? "";
    const earliest = cfg.earliestDate;
    const lat = parseFloat(cfg.mapLat ?? "");
    const lng = parseFloat(cfg.mapLng ?? "");
    const center: [number, number] = isNaN(lat) || isNaN(lng) ? US_CENTER : [lat, lng];

    const map = L.map("memories-map", { attributionControl: false }).setView(center, 6);
    // The shared toolbar's screenshot tool reads it.
    window.map = map;
    // The same starting layer and "remember" key as the main map; attribution goes to the page footer.
    window.MapLayers.create(map, {
        root: document.getElementById("memories-map-layers"),
        defaultBase: cfg.defaultBase ?? null,
        darkMode: cfg.darkMode === "dark" || cfg.darkMode === "system" ? cfg.darkMode : "light",
        storageKey: cfg.layersStorageKey ?? null,
        onAttribution: window.MapLayers.setAttribution,
    });

    map.createPane("memoriesRoutesPane").style.zIndex = "410";
    map.createPane("memoriesMarkersPane").style.zIndex = "420";

    const routeLayer = L.layerGroup([], { pane: "memoriesRoutesPane" }).addTo(map);
    const markerLayers: Record<MarkerType, PinClusterGroup> = {
        trip: createClusterGroup({ chunkedLoading: true }, map),
        visit: createClusterGroup({ chunkedLoading: true }, map),
        photo: createClusterGroup({ chunkedLoading: true }, map),
    };
    Object.values(markerLayers).forEach((group) => group.addTo(map));

    function divIcon(type: string): L.DivIcon {
        return L.divIcon({
            className: "",
            html: '<div class="memories-marker-icon memories-type-' + escHtml(type) + '"><i class="material-symbols-outlined">' + escHtml(TYPE_ICONS[type]) + "</i></div>",
            iconSize: [26, 26],
            iconAnchor: [13, 13],
        });
    }

    function popupHtml(event: MemoryEvent): string {
        let html = '<div class="memories-popup">';
        if (event.thumbnail_url) html += '<img class="memories-popup-thumb" alt="" src="' + escHtml(event.thumbnail_url) + '">';
        html += '<div class="memories-popup-title">' + escHtml(event.title) + "</div>";
        if (event.subtitle) html += '<div class="memories-popup-subtitle">' + escHtml(event.subtitle) + "</div>";
        if (event.url) html += '<a class="memories-popup-link" href="' + escHtml(event.url) + '">View details &rarr;</a>';
        return html + "</div>";
    }

    function isMarkerType(type: string): type is MarkerType {
        return type in markerLayers;
    }

    function renderMap(events: MemoryEvent[]): void {
        routeLayer.clearLayers();
        Object.values(markerLayers).forEach((group) => group.clearLayers());
        const bounds: L.LatLngBounds[] = [];

        events.forEach((event) => {
            if (event.type === "route" && event.extra?.path_geojson) {
                try {
                    const layer = L.geoJSON(JSON.parse(event.extra.path_geojson), {
                        pane: "memoriesRoutesPane",
                        style: { color: event.color, weight: 3, opacity: 0.75 },
                    }).bindPopup(popupHtml(event));
                    layer.addTo(routeLayer);
                    layer.eachLayer((l) => {
                        if (l instanceof L.Polyline) bounds.push(l.getBounds());
                    });
                } catch {
                    // Malformed geometry: skip the route.
                }
                return;
            }
            if (event.latitude == null || event.longitude == null || !isMarkerType(event.type)) return;
            const marker = L.marker([event.latitude, event.longitude], { icon: divIcon(event.type), pane: "memoriesMarkersPane" });
            marker.bindPopup(popupHtml(event));
            markerLayers[event.type].addLayer(marker);
            bounds.push(L.latLngBounds([event.latitude, event.longitude], [event.latitude, event.longitude]));
        });

        // Copied first: a polyline's getBounds() is its own bounds object, which extend() would change.
        const combined = bounds.reduce<L.LatLngBounds | null>((acc, b) => (acc ? acc.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast())), null);
        if (combined?.isValid()) map.fitBounds(combined, { padding: [30, 30], maxZoom: 14 });
    }

    function monthLabel(dateStr: string): string {
        return new Date(dateStr).toLocaleDateString(undefined, { month: "long", year: "numeric" });
    }

    function renderTimeline(events: MemoryEvent[]): void {
        const container = byId("memories-timeline");
        container.innerHTML = "";
        if (!events.length) {
            container.innerHTML = '<div class="memories-empty"><i class="material-symbols-outlined">explore_off</i>No memories in this range yet.</div>';
            return;
        }

        const groups = new Map<string, MemoryEvent[]>();
        events.forEach((event) => {
            const key = monthLabel(event.occurred_at);
            const group = groups.get(key);
            if (group) group.push(event);
            else groups.set(key, [event]);
        });

        groups.forEach((groupEvents, key) => {
            const heading = document.createElement("div");
            heading.className = "memories-timeline-group-title";
            heading.textContent = key;
            container.appendChild(heading);

            const grid = document.createElement("div");
            grid.className = "memories-timeline-grid";
            groupEvents.forEach((event) => grid.appendChild(timelineCard(event)));
            container.appendChild(grid);
        });
    }

    function timelineCard(event: MemoryEvent): HTMLAnchorElement {
        const card = document.createElement("a");
        card.className = "memories-card";
        card.href = event.url || "#";
        if (event.type === "photo" && event.thumbnail_url) {
            // Photos open in the on-page lightbox rather than navigating to the gallery.
            card.classList.add("memories-card--photo");
            card.dataset.photoSrc = event.thumbnail_url;
            card.dataset.photoCaption = event.subtitle ? (event.title ?? "") + " · " + event.subtitle : (event.title ?? "");
            card.addEventListener("click", (e) => {
                e.preventDefault();
                openLightbox(card);
            });
        } else if (!event.url) {
            card.classList.add("memories-card--static");
            card.addEventListener("click", (e) => e.preventDefault());
        }
        const media = event.thumbnail_url
            ? '<img class="memories-card-thumb" alt="" src="' + escHtml(event.thumbnail_url) + '">'
            : '<div class="memories-card-icon memories-type-' + escHtml(event.type) + '"><i class="material-symbols-outlined">' + escHtml(event.icon) + "</i></div>";
        card.innerHTML =
            media +
            '<div class="memories-card-body">' +
            '<div class="memories-card-title">' + escHtml(event.title) + "</div>" +
            '<div class="memories-card-subtitle">' + escHtml(event.subtitle) + "</div>" +
            "</div>";

        // A visit is enriched in place through the shared visit dialog.
        const visitId = event.extra?.visit_id;
        const pinSlug = event.extra?.pin_slug;
        if (event.type === "visit" && visitId && pinSlug) {
            const detailsBtn = document.createElement("button");
            detailsBtn.type = "button";
            detailsBtn.className = "memories-card-details-btn";
            detailsBtn.title = "Add photos, a map, or notes";
            detailsBtn.innerHTML = '<i class="material-symbols-outlined">add_photo_alternate</i>';
            detailsBtn.addEventListener("click", (e) => {
                e.preventDefault();
                e.stopPropagation();
                openVisitDialog(visitUrlBase + pinSlug + "/" + visitId + "/");
            });
            card.appendChild(detailsBtn);
        }
        return card;
    }

    const lbDialog = byId<HTMLDialogElement>("memories-lightbox");
    const lbImg = byId<HTMLImageElement>("memories-lightbox-img");
    const lbCaption = byId("memories-lightbox-caption");
    let lbItems: HTMLElement[] = [];
    let lbIdx = 0;

    function showLightbox(): void {
        const el = lbItems[lbIdx];
        if (!el) return;
        lbImg.src = el.dataset.photoSrc ?? "";
        lbCaption.textContent = el.dataset.photoCaption || "";
    }

    function navLightbox(dir: number): void {
        if (!lbItems.length) return;
        lbIdx = (lbIdx + dir + lbItems.length) % lbItems.length;
        showLightbox();
    }

    function openLightbox(card: HTMLElement): void {
        // Prev/next walks the photo cards currently rendered.
        lbItems = Array.from(document.querySelectorAll<HTMLElement>(".memories-card--photo"));
        lbIdx = Math.max(0, lbItems.indexOf(card));
        showLightbox();
        lbDialog.showModal();
    }

    byId("memories-lightbox-close").addEventListener("click", () => lbDialog.close());
    byId("memories-lightbox-prev").addEventListener("click", () => navLightbox(-1));
    byId("memories-lightbox-next").addEventListener("click", () => navLightbox(1));
    document.addEventListener("keydown", (e) => {
        if (!lbDialog.open) return;
        if (e.key === "ArrowLeft") navLightbox(-1);
        else if (e.key === "ArrowRight") navLightbox(1);
    });

    let currentEvents: MemoryEvent[] = [];

    function applyLegendFilter(): void {
        const offTypes = new Set(Array.from(document.querySelectorAll<HTMLElement>(".memories-legend-item.is-off")).map((el) => el.dataset.type));
        const filtered = currentEvents.filter((e) => !offTypes.has(e.type));
        renderMap(filtered);
        renderTimeline(filtered);
    }

    // The feed is capped server-side, so a wide range arrives a page at a time. The next page is fetched as soon
    // as the current one lands, so "Load more" is a render rather than a wait.
    let feedStart = "";
    let feedEnd = "";
    let nextBefore: string | null = null;
    let prefetchedPage: FeedPage | null = null;
    let prefetchInFlight = false;

    // No id is common to all four event types, and a same-day event can sit on both sides of a page boundary.
    function eventKey(event: MemoryEvent): string {
        return [event.type, event.occurred_at, event.url || "", event.title || ""].join("|");
    }

    function requestPage(start: string, end: string, before: string | null): Promise<FeedPage> {
        let url = feedUrl + "?start=" + encodeURIComponent(start) + "&end=" + encodeURIComponent(end);
        // An exclusive cursor rather than a narrower `end`, which would stall on a day holding more than a page.
        if (before) url += "&before=" + encodeURIComponent(before);
        return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } }).then((resp) => resp.json() as Promise<FeedPage>);
    }

    function prewarmNextPage(): void {
        if (!nextBefore || prefetchInFlight) return;
        prefetchInFlight = true;
        const requestedFor = nextBefore;
        requestPage(feedStart, feedEnd, requestedFor)
            .then((data) => {
                // Dropped if the range moved on while it was in flight.
                if (nextBefore === requestedFor) prefetchedPage = data;
                prefetchInFlight = false;
            })
            .catch(() => {
                prefetchInFlight = false;
            });
    }

    function absorbPage(data: FeedPage, append: boolean): void {
        const incoming = data.events || [];
        if (append) {
            const seen = new Set(currentEvents.map(eventKey));
            incoming.forEach((event) => {
                if (!seen.has(eventKey(event))) currentEvents.push(event);
            });
        } else {
            currentEvents = incoming;
        }
        nextBefore = data.truncated ? (data.next_before ?? null) : null;
        prefetchedPage = null;
        applyLegendFilter();
        renderLoadMore();
        prewarmNextPage();
    }

    function renderLoadMore(): void {
        const existing = document.getElementById("memories-load-more");
        if (!nextBefore) {
            existing?.remove();
            return;
        }
        if (existing) return;
        const button = document.createElement("button");
        button.id = "memories-load-more";
        button.type = "button";
        button.className = "btn btn--ghost";
        button.textContent = "Load earlier memories";
        button.addEventListener("click", () => {
            if (!nextBefore) return;
            if (prefetchedPage) {
                absorbPage(prefetchedPage, true);
                return;
            }
            button.disabled = true;
            requestPage(feedStart, feedEnd, nextBefore)
                .then((data) => absorbPage(data, true))
                .catch(() => {
                    button.disabled = false;
                });
        });
        const timeline = document.getElementById("memories-timeline");
        timeline?.parentNode?.insertBefore(button, timeline.nextSibling);
    }

    function fetchMemories(start: string, end: string): void {
        feedStart = start;
        feedEnd = end;
        nextBefore = null;
        prefetchedPage = null;
        requestPage(start, end, null)
            .then((data) => absorbPage(data, false))
            .catch(() => {
                byId("memories-timeline").innerHTML = '<div class="memories-empty"><i class="material-symbols-outlined">error</i>Couldn\'t load memories.</div>';
            });
    }

    const startInput = byId<HTMLInputElement>("memories-start-date");
    const endInput = byId<HTMLInputElement>("memories-end-date");

    function setRange(startStr: string, endStr: string): void {
        startInput.value = startStr;
        endInput.value = endStr;
        fetchMemories(startStr, endStr);
    }

    // A visit logged or edited in the shared visit dialog.
    document.addEventListener("memoriesFeedRefresh", () => {
        if (startInput.value && endInput.value) fetchMemories(startInput.value, endInput.value);
    });

    byId("memories-apply-range").addEventListener("click", () => {
        if (startInput.value && endInput.value) fetchMemories(startInput.value, endInput.value);
    });

    document.querySelectorAll<HTMLElement>(".memories-quick-range [data-range]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const range = btn.dataset.range;
            if (range === "all") {
                setRange(earliest || today, today);
                return;
            }
            const end = new Date(today);
            const start = new Date(end);
            start.setDate(start.getDate() - parseInt(range ?? "0", 10));
            setRange(isoDay(start), isoDay(end));
        });
    });

    document.querySelectorAll<HTMLElement>(".memories-legend-item").forEach((item) => {
        item.addEventListener("click", () => {
            item.classList.toggle("is-off");
            applyLegendFilter();
        });
    });

    // The trailing 90 days, the server-side default.
    const initialEnd = new Date(today);
    const initialStart = new Date(initialEnd);
    initialStart.setDate(initialStart.getDate() - 90);
    setRange(isoDay(initialStart), isoDay(initialEnd));
}

const root = document.getElementById("memories-page");
if (root && document.getElementById("memories-map")) initMemories(root);
