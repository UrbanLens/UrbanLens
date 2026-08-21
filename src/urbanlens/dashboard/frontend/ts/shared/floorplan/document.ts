/**
 * The floorplan document as the editor sees it.
 *
 * Mirrors `services/floorplans/serialization.py` exactly. Walls are the only
 * stored geometry and live in plan-local metres; rooms are seed points that
 * bind to whichever derived face contains them, so a geometry edit can never
 * orphan a room's name.
 */

import type { Pt } from "./coords";
import type { Segment } from "./planar";

export type WallKind = "exterior" | "interior" | "virtual" | "collapsed";
export type WallThickness = "thin" | "normal" | "thick";
export type OpeningKind = "door" | "doorway" | "window" | "hatch";
export type OpeningSwing = "none" | "left" | "right" | "double";
// Trimmed to the kinds that earn their own icon: an entrance is already a
// door opening on a wall, and "photo"/"note"/"fixture" markers carried no
// information a label field didn't already say.
export type MarkerKind = "hazard" | "stair" | "elevator";

export interface Opening {
    uuid?: string;
    kind: OpeningKind;
    t_start: number;
    t_end: number;
    swing: OpeningSwing;
    sill_meters?: number | null;
}

export interface Wall {
    uuid?: string;
    kind: WallKind;
    thickness: WallThickness;
    name?: string;
    ax: number;
    ay: number;
    bx: number;
    by: number;
    openings: Opening[];
}

export interface RoomSeed {
    uuid?: string;
    name: string;
    x: number;
    y: number;
    height_meters?: number | null;
}

export interface Marker {
    uuid?: string;
    kind: MarkerKind;
    name?: string;
    x: number;
    y: number;
    facing_degrees?: number | null;
    connector_id?: string | null;
}

export interface Floor {
    uuid?: string;
    level: number;
    name: string;
    elevation_meters?: number | null;
    height_meters?: number | null;
    walls: Wall[];
    rooms: RoomSeed[];
    markers: Marker[];
}

export interface VersionSummary {
    uuid: string;
    name: string;
    valid_from: string | null;
}

export interface FloorplanDocument {
    uuid?: string;
    name: string;
    valid_from: string | null;
    /**
     * Coordinate anchor for plan-local metres. Not `origin`: that key already
     * carries provenance ("local" / "community" / "redata") and the save view
     * merges it in over the document.
     */
    plan_origin: { lat: number; lng: number } | null;
    rotation_degrees: number;
    floors: Floor[];
    source_pool?: unknown[];
    reference_pool?: unknown[];
    /** Server-supplied, read-only here. */
    origin?: string;
    versions?: VersionSummary[];
}

/** A fresh single-storey plan anchored at *at*. */
export function emptyDocument(at: { lat: number; lng: number }): FloorplanDocument {
    return {
        name: "",
        valid_from: null,
        plan_origin: at,
        rotation_degrees: 0,
        floors: [{ level: 0, name: "Ground floor", walls: [], rooms: [], markers: [] }],
        source_pool: [],
        reference_pool: [],
    };
}

let localIdCounter = 0;
/**
 * A client-side id for a wall that has never been saved.
 *
 * Face derivation needs to attribute every edge to a wall, including ones the
 * user drew a second ago, so identity cannot wait for a round trip.
 */
export const nextLocalId = (): string => `local-${++localIdCounter}`;

/** Stable identity for a wall, saved or not. */
export function wallId(wall: Wall): string {
    if (!wall.uuid) wall.uuid = nextLocalId();
    return wall.uuid;
}

/** The floor's walls as geometry segments, for face derivation. */
export function wallSegments(floor: Floor): Segment[] {
    return floor.walls.map((wall) => ({
        wallId: wallId(wall),
        a: { x: wall.ax, y: wall.ay },
        b: { x: wall.bx, y: wall.by },
    }));
}

/** Endpoints of a wall as points. */
export const wallStart = (wall: Wall): Pt => ({ x: wall.ax, y: wall.ay });
export const wallEnd = (wall: Wall): Pt => ({ x: wall.bx, y: wall.by });

/** Metres of wall, for the length readout. */
export const wallLength = (wall: Wall): number => Math.hypot(wall.bx - wall.ax, wall.by - wall.ay);
