/**
 * Which markers on other floors a stair or lift can be joined to.
 */

import type { Floor, Marker, MarkerKind } from "./document";

/** Marker kinds that pass through more than one storey. */
export const CONNECTOR_KINDS = new Set<MarkerKind>(["stair", "elevator"]);

/** One offer: a connector marker on some other floor. */
export interface ConnectorCandidate {
    floor: Floor;
    marker: Marker;
}

/**
 * Rank the connector markers on other floors as link candidates.
 */
export function connectorCandidates(floors: readonly Floor[], current: Floor, marker: Marker): ConnectorCandidate[] {
    const found: Array<{ candidate: ConnectorCandidate; storeys: number; plan: number }> = [];
    for (const other of floors) {
        if (other === current) continue;
        for (const candidate of other.markers) {
            if (!CONNECTOR_KINDS.has(candidate.kind)) continue;
            found.push({
                candidate: { floor: other, marker: candidate },
                storeys: Math.abs(other.level - current.level),
                plan: Math.hypot(candidate.x - marker.x, candidate.y - marker.y),
            });
        }
    }
    found.sort((a, b) => a.storeys - b.storeys || a.plan - b.plan || a.candidate.floor.level - b.candidate.floor.level);
    return found.map((entry) => entry.candidate);
}
