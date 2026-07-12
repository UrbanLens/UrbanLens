/**
 * addHitToClusters/clusterHits mirror services/pin_suggestions.py's
 * _cluster_hits greedy merge (same default radius) - tested directly since
 * they have no DOM/File System Access dependency.
 */
import { describe, expect, test } from "bun:test";
import { addHitToClusters, clusterHits, haversineMeters, isNearCachedPin, partitionByCachedPins } from "./photo-location-cluster";

describe("haversineMeters", () => {
    test("is zero for the same point", () => {
        expect(haversineMeters({ lat: 40.7128, lng: -74.006 }, { lat: 40.7128, lng: -74.006 })).toBe(0);
    });

    test("returns a plausible distance for two known points", () => {
        // NYC to Philadelphia is roughly 130 km.
        const distance = haversineMeters({ lat: 40.7128, lng: -74.006 }, { lat: 39.9526, lng: -75.1652 });
        expect(distance).toBeGreaterThan(120_000);
        expect(distance).toBeLessThan(140_000);
    });
});

describe("clusterHits", () => {
    test("groups hits within the radius into one cluster", () => {
        const clusters = clusterHits(
            [
                { lat: 40.0, lng: -75.0, date: "2026-01-01" },
                { lat: 40.0001, lng: -75.0001, date: "2026-01-02" },
            ],
            50,
        );
        expect(clusters).toHaveLength(1);
        expect(clusters[0]?.count).toBe(2);
        expect(clusters[0]?.dates).toEqual(["2026-01-01", "2026-01-02"]);
    });

    test("keeps distant hits in separate clusters", () => {
        const clusters = clusterHits(
            [
                { lat: 40.0, lng: -75.0 },
                { lat: 41.0, lng: -76.0 },
            ],
            50,
        );
        expect(clusters).toHaveLength(2);
    });

    test("deduplicates repeated dates within a cluster", () => {
        const clusters = clusterHits(
            [
                { lat: 40.0, lng: -75.0, date: "2026-01-01" },
                { lat: 40.0, lng: -75.0, date: "2026-01-01" },
            ],
            50,
        );
        expect(clusters).toHaveLength(1);
        expect(clusters[0]?.count).toBe(2);
        expect(clusters[0]?.dates).toEqual(["2026-01-01"]);
    });

    test("empty input yields no clusters", () => {
        expect(clusterHits([], 50)).toEqual([]);
    });

    test("centroid drifts toward later hits in a cluster", () => {
        const clusters = clusterHits(
            [
                { lat: 40.0, lng: -75.0 },
                { lat: 40.0004, lng: -75.0 },
            ],
            50,
        );
        expect(clusters).toHaveLength(1);
        // The centroid should land between the two points, not stay pinned to the first.
        expect(clusters[0]?.lat).toBeGreaterThan(40.0);
        expect(clusters[0]?.lat).toBeLessThan(40.0004);
    });
});

describe("addHitToClusters", () => {
    test("mutates and returns the same array reference for incremental building", () => {
        const clusters = addHitToClusters([], { lat: 40.0, lng: -75.0 });
        const result = addHitToClusters(clusters, { lat: 40.0, lng: -75.0 });
        expect(result).toBe(clusters);
        expect(result).toHaveLength(1);
    });
});

describe("cached-pin partitioning", () => {
    const cluster = { lat: 40.0, lng: -75.0, count: 1, dates: [] as string[] };

    test("isNearCachedPin is true within radius", () => {
        expect(isNearCachedPin(cluster, [{ lat: 40.0001, lng: -75.0001 }])).toBe(true);
    });

    test("isNearCachedPin is false outside radius", () => {
        expect(isNearCachedPin(cluster, [{ lat: 41.0, lng: -76.0 }])).toBe(false);
    });

    test("partitionByCachedPins splits fresh vs already-pinned clusters", () => {
        const far = { lat: 41.0, lng: -76.0, count: 1, dates: [] as string[] };
        const { fresh, existing } = partitionByCachedPins([cluster, far], [{ lat: 40.0001, lng: -75.0001 }]);
        expect(fresh).toEqual([far]);
        expect(existing).toEqual([cluster]);
    });

    test("no cached pins means everything is fresh", () => {
        const { fresh, existing } = partitionByCachedPins([cluster], []);
        expect(fresh).toEqual([cluster]);
        expect(existing).toEqual([]);
    });
});
