/**
 * Which markers on other floors a stair or lift can be joined to.
 *
 * Markers that share a ``connector_id`` are the same physical shaft seen on
 * different storeys, which is what lets the editor show "this stair continues
 * up" and what a reader follows between floors.
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
 *
 * Every other floor is offered, not just the two adjacent ones. A lift that
 * only opens on the ground and third storeys is an ordinary building, and so is
 * a stair on a plan whose middle floor has not been drawn yet - neither could
 * be linked at all while this only looked one storey up and down.
 *
 * Nearest storey first, and within a storey the marker closest in plan, so the
 * shaft directly overhead is the first button rather than one across the
 * building. Ties break on level so the order does not depend on how the floors
 * happen to be arranged in the document.
 *
 * Args:
 *     floors: Every floor in the document.
 *     current: The floor the selected marker is on.
 *     marker: The selected marker.
 *
 * Returns:
 *     Candidates, best first.
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
