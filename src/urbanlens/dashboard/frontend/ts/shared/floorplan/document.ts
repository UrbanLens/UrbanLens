/**
 * The floorplan document as the editor sees it.
 */

import type { Pt } from "./coords";
import type { Segment } from "./planar";

export type WallKind = "exterior" | "interior" | "fence" | "virtual" | "collapsed";

/**
 * Every wall kind, in the order they are offered, with what to call it.
 */
export const WALL_KINDS: ReadonlyArray<{ value: WallKind; label: string }> = [
    { value: "exterior", label: "Exterior wall" },
    { value: "interior", label: "Interior wall" },
    { value: "fence", label: "Fence" },
    { value: "virtual", label: "Virtual (open edge)" },
    { value: "collapsed", label: "Collapsed / ruined" },
];
export type WallThickness = "thin" | "normal" | "thick";
export type OpeningKind = "door" | "doorway" | "gate" | "window" | "hatch";

/** Every opening kind, in the order they are offered, with what to call it. */
export const OPENING_KINDS: ReadonlyArray<{ value: OpeningKind; label: string }> = [
    { value: "door", label: "Door" },
    { value: "doorway", label: "Doorway (no door)" },
    { value: "gate", label: "Gate" },
    { value: "window", label: "Window" },
    { value: "hatch", label: "Hatch" },
];
export type OpeningSwing = "none" | "left" | "right" | "double";
// Trimmed to the kinds that earn their own icon.
export type MarkerKind = "hazard" | "stair" | "elevator";

/**
 * The fields every floorplan item carries, whatever kind of thing it is.
 */
/** One entry in a plan's reference pool: a photo, a scan, a page. */
export interface Reference {
    uuid?: string;
    kind?: string;
    title?: string;
    url?: string;
    description?: string;
    attributes?: Record<string, unknown>;
    /** The site Image this stands for, when it stands for one. */
    image_uuid?: string | null;
}

export interface ItemDetails {
    /** Free text about the thing itself. */
    description?: string;
    /** What state it is in - "sound", "rotten", "collapsed in 2019". */
    condition?: string;
    /** ISO date, when known. */
    built_date?: string | null;
    /**
     * Producer-specific extras. Material lives here rather than in a column of
     * its own: it is one of an open-ended set of properties a surveyor might
     * record, and every one of those would otherwise be a migration.
     */
    attributes?: Record<string, unknown>;
    /**
     * Reference-pool uuids this item cites. The pool holds each photo once
     * however many walls, doors and locks point at it, so this is a list of
     * pool entries rather than of images.
     */
    references?: string[];
}

/** Whether a lock is presently securing its opening. */
export type LockState = "unknown" | "locked" | "unlocked";

/** Every lock state, in the order they are offered, with what to call it. */
export const LOCK_STATES: ReadonlyArray<{ value: LockState; label: string }> = [
    { value: "unknown", label: "Not known" },
    { value: "locked", label: "Locked" },
    { value: "unlocked", label: "Unlocked" },
];

/**
 * One lock on an opening; a door may carry several, or none.
 */
export interface Lock extends ItemDetails {
    uuid?: string;
    /** Free-form type or label: "padlock", "deadbolt", "chain". */
    name?: string;
    state: LockState;
    /** What opens it, in whatever shape the recorder used. */
    key_attributes?: Record<string, unknown>;
}

export interface Opening extends ItemDetails {
    uuid?: string;
    kind: OpeningKind;
    t_start: number;
    t_end: number;
    swing: OpeningSwing;
    sill_meters?: number | null;
    locks?: Lock[];
}

export interface Wall extends ItemDetails {
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

export interface RoomSeed extends ItemDetails {
    uuid?: string;
    name: string;
    x: number;
    y: number;
    height_meters?: number | null;
}

export interface Marker extends ItemDetails {
    uuid?: string;
    kind: MarkerKind;
    name?: string;
    x: number;
    y: number;
    facing_degrees?: number | null;
    connector_id?: string | null;
    // WGS-84, computed from x/y and filled in just before every save) so the server can create/move this marker's detail-pin twin without.
    lat?: number | null;
    lng?: number | null;
    // The linked detail pin's own icon/color, when it has customizations of its own (set via the Private Pin page's detail-pin dialog).
    icon?: string | null;
    color?: string | null;
}

export interface Floor extends ItemDetails {
    uuid?: string;
    /** Position in the stack; 0 is the ground datum, negatives below it. */
    level: number;
    /**
     * The lift-button code ("G", "14", "4A", "B2", "M"). Blank derives one
     * from the level, which is the ordinary case - see designations.ts.
     */
    designation?: string;
    /** Optional nickname. Never affects numbering. */
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
    reference_pool?: Reference[];
    /**
     * Which version of the plan this document was read at. Sent back on save
     * so the server can tell that another tab has replaced it since; see
     * services/floorplans/serialization.py.
     */
    version_token?: string;
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
        // No name: writing "Ground floor" into the document as though the author had typed it is what left a renamed floor with no record.
        floors: [{ level: 0, name: "", walls: [], rooms: [], markers: [] }],
        source_pool: [],
        reference_pool: [],
    };
}

let localIdCounter = 0;
/**
 * A client-side id for a wall that has never been saved.
 */
export const nextLocalId = (): string => `local-${++localIdCounter}`;

/**
 * An id for a stair or lift shaft, unique everywhere.
 */
export function newConnectorId(): string {
    const globalCrypto = globalThis.crypto as Crypto | undefined;
    if (globalCrypto?.randomUUID) return globalCrypto.randomUUID();
    // randomUUID needs a secure context. Anywhere it is missing, a timestamp
    // and some randomness still beats a per-session counter by a wide margin.
    return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

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

/** What to carry across when one floor's contents are copied onto another. */
export interface CopyFloorOptions {
    /** Room seeds, so the copy keeps the names the author already typed. */
    rooms?: boolean;
    /** Markers. Off by default: a hazard is a fact about one storey. */
    markers?: boolean;
    /**
     * Whether copied markers keep their ``connector_id``. Off by default:
     * that id is what makes two markers the same stairwell, so carrying it
     * over silently joins the copy into the original's shaft.
     */
    connectors?: boolean;
}

/**
 * A deep copy of *source*'s contents, with fresh identity throughout.
 */
export function copyFloorContents(source: Floor, options: CopyFloorOptions = {}): Pick<Floor, "walls" | "rooms" | "markers"> {
    const { rooms = true, markers = false, connectors = false } = options;

    const walls: Wall[] = source.walls.map((wall) => ({
        ...wall,
        uuid: nextLocalId(),
        // Its own list, citing the same pool rows. Spreading shares the array
        // itself, so a later push on the copy would reach into the original.
        ...(wall.references ? { references: [...wall.references] } : {}),
        openings: wall.openings.map((opening) => ({
            ...opening,
            uuid: nextLocalId(),
            ...(opening.references ? { references: [...opening.references] } : {}),
            // Locks come across as their own new rows.
            ...(opening.locks ? { locks: opening.locks.map((lock) => ({ ...lock, uuid: nextLocalId() })) } : {}),
        })),
    }));

    const copiedRooms: RoomSeed[] = rooms ? source.rooms.map((room) => ({ ...room, uuid: nextLocalId() })) : [];

    const copiedMarkers: Marker[] = markers
        ? source.markers.map((marker) => {
              // lat/lng are recomputed from x/y before every save; carrying
              // the source's across would only be stale until then.
              const { lat: _lat, lng: _lng, ...rest } = marker;
              return {
                  ...rest,
                  uuid: nextLocalId(),
                  connector_id: connectors ? marker.connector_id : null,
              };
          })
        : [];

    return { walls, rooms: copiedRooms, markers: copiedMarkers };
}

/**
 * Read a single attribute off an item.
 */
export function attribute(item: ItemDetails, key: string): string {
    const value = item.attributes?.[key];
    return typeof value === "string" ? value : "";
}

/**
 * Write a single attribute, dropping it when cleared.
 */
export function setAttribute(item: ItemDetails, key: string, value: string): void {
    const next = { ...(item.attributes || {}) };
    if (value.trim()) next[key] = value;
    else delete next[key];
    item.attributes = next;
}
