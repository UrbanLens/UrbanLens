/**
 * Pure geometry helpers for the Tools-page local folder scanner
 * (entries/photo-location-scan.ts): greedy proximity clustering of scanned
 * photo GPS hits, and filtering clusters against the browser's cached pins.
 * Mirrors the server-side clustering in services/pin_suggestions.py (same
 * default merge radius) so client-side grouping roughly matches what the
 * backend will do again once results are uploaded - kept dependency-free
 * (no DOM/File System Access APIs) so it can run under `bun test` directly.
 */

/** Default merge radius in metres - matches boundary.DEFAULT_RADIUS_METERS server-side. */
export const DEFAULT_CLUSTER_RADIUS_M = 50;

/** Radius within which a cluster is considered to already have a cached pin. */
export const EXISTING_PIN_RADIUS_M = 100;

/**
 * Representative photo File references kept per cluster for the opt-in
 * preview/upload picker - a small sample, not every hit that fed the
 * cluster. Matches the server's MAX_SUGGESTION_PHOTOS cap with headroom for
 * browsing before narrowing down to a selection.
 */
export const MAX_CLUSTER_PHOTOS_SHOWN = 6;

export interface PhotoHit {
    lat: number;
    lng: number;
    /** ISO YYYY-MM-DD capture date, when known. */
    date?: string;
    /** The source file, kept only so the opt-in picker can preview/upload it. */
    file?: File;
}

export interface PhotoCluster {
    lat: number;
    lng: number;
    count: number;
    dates: string[];
    /** Up to MAX_CLUSTER_PHOTOS_SHOWN representative files, in scan order. */
    photos: File[];
}

export interface CachedPinPoint {
    lat: number;
    lng: number;
}

/** Great-circle distance in metres between two lat/lng points. */
export function haversineMeters(a: { lat: number; lng: number }, b: { lat: number; lng: number }): number {
    const R = 6_371_000;
    const toRad = (deg: number) => (deg * Math.PI) / 180;
    const dLat = toRad(b.lat - a.lat);
    const dLng = toRad(b.lng - a.lng);
    const lat1 = toRad(a.lat);
    const lat2 = toRad(b.lat);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
    return R * 2 * Math.asin(Math.sqrt(h));
}

/**
 * Add one hit to a running list of clusters, merging into the nearest one
 * within radiusM of its running centroid or starting a new cluster.
 *
 * Incremental (rather than a batch clusterHits(allHits)) so the Tools page
 * can update its live results list as each matching file is found during a
 * scan, without re-clustering everything found so far on every file.
 */
export function addHitToClusters(clusters: PhotoCluster[], hit: PhotoHit, radiusM: number = DEFAULT_CLUSTER_RADIUS_M): PhotoCluster[] {
    for (const cluster of clusters) {
        if (haversineMeters(cluster, hit) <= radiusM) {
            const n = cluster.count + 1;
            cluster.lat = cluster.lat + (hit.lat - cluster.lat) / n;
            cluster.lng = cluster.lng + (hit.lng - cluster.lng) / n;
            cluster.count = n;
            if (hit.date && !cluster.dates.includes(hit.date)) cluster.dates.push(hit.date);
            if (hit.file && cluster.photos.length < MAX_CLUSTER_PHOTOS_SHOWN) cluster.photos.push(hit.file);
            return clusters;
        }
    }
    clusters.push({ lat: hit.lat, lng: hit.lng, count: 1, dates: hit.date ? [hit.date] : [], photos: hit.file ? [hit.file] : [] });
    return clusters;
}

/** Cluster a full batch of hits from scratch (used by tests / non-incremental callers). */
export function clusterHits(hits: PhotoHit[], radiusM: number = DEFAULT_CLUSTER_RADIUS_M): PhotoCluster[] {
    let clusters: PhotoCluster[] = [];
    for (const hit of hits) clusters = addHitToClusters(clusters, hit, radiusM);
    return clusters;
}

/** Whether a cluster falls within radiusM of any cached pin. */
export function isNearCachedPin(cluster: { lat: number; lng: number }, pins: CachedPinPoint[], radiusM: number = EXISTING_PIN_RADIUS_M): boolean {
    return pins.some((pin) => haversineMeters(cluster, pin) <= radiusM);
}

/** Partition clusters into (new, alreadyHavePin) against a list of cached pin points. */
export function partitionByCachedPins(clusters: PhotoCluster[], pins: CachedPinPoint[], radiusM: number = EXISTING_PIN_RADIUS_M): { fresh: PhotoCluster[]; existing: PhotoCluster[] } {
    const fresh: PhotoCluster[] = [];
    const existing: PhotoCluster[] = [];
    for (const cluster of clusters) {
        (isNearCachedPin(cluster, pins, radiusM) ? existing : fresh).push(cluster);
    }
    return { fresh, existing };
}
