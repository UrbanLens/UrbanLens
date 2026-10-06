/**
 * What the map composer's drawing holds: the shapes, their order, which is selected and which are
 * hidden, and the history that undo walks back through.
 *
 * No Leaflet here. The composer's map draws whatever this says and reports what the viewer did to
 * it, so everything about whether an edit happened, and whether it can be undone, is testable on its
 * own.
 *
 * Shapes are the snapshot format (`ShapeSpec`, what `MarkupMap.to_snapshot` emits and the save
 * endpoint takes), so a saved map loads as it is and saves as it was, with no second format to
 * migrate between.
 */
import type { LatLngTuple, ShapeSpec } from "./markup-engine";

export type ShapeType = ShapeSpec["type"];

/** One shape as the composer holds it. */
export interface DocItem {
    /** Stable for the life of the document - the layers list and the map both key on it. */
    id: string;
    /** Per type, in the order drawn: the 2 in "Arrow 2". Kept through undo, so a name never moves to another shape. */
    ordinal: number;
    shape: ShapeSpec;
    /** Hidden while editing. A hidden shape is not saved and not in a download - what is on screen is what is kept. */
    hidden: boolean;
}

/** What changed, so the map redraws only what it must. */
export type DocChange =
    /** Shapes were added, removed, reordered, shown or hidden - or everything changed at once. */
    | { kind: "structure" }
    /** These shapes' geometry or style changed in place. */
    | { kind: "items"; ids: string[] }
    /** Only the selection moved. */
    | { kind: "selection" };

const TYPE_NAMES: Record<ShapeType, string> = {
    line: "Line",
    arrow: "Arrow",
    circle: "Circle",
    rect: "Rectangle",
    polygon: "Polygon",
    text: "Text",
    pin: "Pin",
};

/** The fewest points each type is drawn from; anything with fewer is not a shape. */
const MIN_POINTS: Record<ShapeType, number> = { line: 2, arrow: 2, circle: 2, rect: 2, polygon: 3, text: 1, pin: 1 };

/** Undo reaches back this far; older steps are dropped. */
export const HISTORY_LIMIT = 100;

/** A shape's display name in the layers list. */
export function typeName(type: ShapeType): string {
    return TYPE_NAMES[type];
}

function isShapeType(value: unknown): value is ShapeType {
    return typeof value === "string" && Object.prototype.hasOwnProperty.call(TYPE_NAMES, value);
}

/** A point as stored, accepting the `{lat, lng}` objects an older client wrote as well as pairs. */
function toLatLng(raw: unknown): LatLngTuple | null {
    let lat: unknown;
    let lng: unknown;
    if (Array.isArray(raw)) [lat, lng] = raw;
    else if (raw && typeof raw === "object") ({ lat, lng } = raw as { lat?: unknown; lng?: unknown });
    if (typeof lat !== "number" || typeof lng !== "number" || !Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    return [lat, lng];
}

function finiteOr(value: unknown): number | undefined {
    return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/**
 * A stored shape as the composer can edit it, or null when it is not one.
 *
 * Reads every shape any saved map holds: the snapshot format as `to_snapshot` writes it, `weight`
 * where an older client wrote that instead of `stroke_width`, and `{lat, lng}` points. What it
 * returns is always the current format, so saving it writes that.
 * @param raw - One entry of a snapshot's `markup`.
 */
export function normalizeShape(raw: unknown): ShapeSpec | null {
    if (!raw || typeof raw !== "object") return null;
    const source = raw as Record<string, unknown>;
    if (!isShapeType(source.type)) return null;
    const latlngs = Array.isArray(source.latlngs) ? source.latlngs.map(toLatLng) : [];
    if (latlngs.some((ll) => ll === null) || latlngs.length < MIN_POINTS[source.type]) return null;
    const shape: ShapeSpec = { type: source.type, latlngs: latlngs as LatLngTuple[] };
    if (typeof source.color === "string") shape.color = source.color;
    const width = finiteOr(source.stroke_width) ?? finiteOr(source.weight);
    if (width !== undefined) shape.stroke_width = width;
    const fill = finiteOr(source.fill_opacity);
    if (fill !== undefined) shape.fill_opacity = fill;
    const border = finiteOr(source.border_opacity);
    if (border !== undefined) shape.border_opacity = border;
    if (typeof source.border_color === "string" && source.border_color) shape.border_color = source.border_color;
    if (source.type === "text") {
        shape.label = typeof source.label === "string" ? source.label : "";
        const rotation = finiteOr(source.rotation);
        if (rotation) shape.rotation = rotation;
    }
    return shape;
}

/** A deep copy - shapes are plain data, and history must not share arrays with the live list. */
export function cloneShape(shape: ShapeSpec): ShapeSpec {
    return { ...shape, latlngs: shape.latlngs.map((ll) => [ll[0], ll[1]] as LatLngTuple) };
}

function cloneItems(items: readonly DocItem[]): DocItem[] {
    return items.map((item) => ({ ...item, shape: cloneShape(item.shape) }));
}

function sameItems(a: readonly DocItem[], b: readonly DocItem[]): boolean {
    return JSON.stringify(a) === JSON.stringify(b);
}

export class MarkupDocument {
    private list: DocItem[] = [];
    private undoStack: DocItem[][] = [];
    private redoStack: DocItem[][] = [];
    private selected: string | null = null;
    private nextId = 1;
    private ordinals: Partial<Record<ShapeType, number>> = {};
    /** The state an open transaction started from, or null outside one. */
    private before: DocItem[] | null = null;
    private readonly listeners = new Set<(change: DocChange) => void>();

    /**
     * Replaces everything with a saved map's shapes. History starts over: loading is not an edit.
     * @param shapes - A snapshot's `markup`; entries that are not shapes are dropped.
     * @returns How many entries were dropped.
     */
    load(shapes: readonly unknown[] | null | undefined): number {
        this.list = [];
        this.ordinals = {};
        let dropped = 0;
        for (const raw of shapes ?? []) {
            const shape = normalizeShape(raw);
            if (shape) this.list.push(this.newItem(shape));
            else dropped++;
        }
        this.undoStack = [];
        this.redoStack = [];
        this.before = null;
        this.selected = null;
        this.emit({ kind: "structure" });
        return dropped;
    }

    /** Every shape, bottom first. */
    items(): readonly DocItem[] {
        return this.list;
    }

    get(id: string | null): DocItem | undefined {
        return id ? this.list.find((item) => item.id === id) : undefined;
    }

    /** The shapes that are kept - visible ones, bottom first, as the snapshot format. */
    visibleShapes(): ShapeSpec[] {
        return this.list.filter((item) => !item.hidden).map((item) => cloneShape(item.shape));
    }

    hasMarkup(): boolean {
        return this.list.length > 0;
    }

    canUndo(): boolean {
        return this.undoStack.length > 0 || (this.before !== null && !sameItems(this.before, this.list));
    }

    canRedo(): boolean {
        return this.redoStack.length > 0;
    }

    /** "Arrow 2", or a text label's own words. */
    label(item: DocItem): string {
        if (item.shape.type === "text") {
            const text = (item.shape.label ?? "").trim();
            if (text) return text.length > 40 ? `${text.slice(0, 39)}…` : text;
        }
        return `${typeName(item.shape.type)} ${item.ordinal}`;
    }

    // -- Selection (not history) ----------------------------------------------------------------

    selectedId(): string | null {
        return this.selected;
    }

    select(id: string | null): void {
        const next = id && this.get(id) && !this.get(id)!.hidden ? id : null;
        if (next === this.selected) return;
        this.selected = next;
        this.emit({ kind: "selection" });
    }

    // -- Edits ----------------------------------------------------------------------------------

    /** Adds a shape on top. @returns Its id, or null when it is not a shape. */
    add(raw: ShapeSpec): string | null {
        const shape = normalizeShape(raw);
        if (!shape) return null;
        this.record();
        const item = this.newItem(shape);
        this.list.push(item);
        this.emit({ kind: "structure" });
        return item.id;
    }

    /**
     * Changes one shape in place.
     * @param change - Returns the new shape; given a copy, so changing it in place is fine too.
     * @returns Whether anything changed.
     */
    update(id: string, change: (shape: ShapeSpec) => ShapeSpec | void): boolean {
        const item = this.get(id);
        if (!item) return false;
        const draft = cloneShape(item.shape);
        const next = normalizeShape(change(draft) ?? draft);
        if (!next || JSON.stringify(next) === JSON.stringify(item.shape)) return false;
        this.record();
        item.shape = next;
        this.emit({ kind: "items", ids: [id] });
        return true;
    }

    remove(id: string): boolean {
        const index = this.list.findIndex((item) => item.id === id);
        if (index < 0) return false;
        this.record();
        this.list.splice(index, 1);
        if (this.selected === id) this.selected = null;
        this.emit({ kind: "structure" });
        return true;
    }

    /** Removes every shape. Undoable, like any other edit. */
    clear(): boolean {
        if (!this.list.length) return false;
        this.record();
        this.list = [];
        this.selected = null;
        this.emit({ kind: "structure" });
        return true;
    }

    setHidden(id: string, hidden: boolean): boolean {
        const item = this.get(id);
        if (!item || item.hidden === hidden) return false;
        this.record();
        item.hidden = hidden;
        if (hidden && this.selected === id) this.selected = null;
        this.emit({ kind: "structure" });
        return true;
    }

    /**
     * Moves a shape up (towards the top, positive) or down the stack.
     * @returns Whether it moved - not past either end.
     */
    move(id: string, steps: number): boolean {
        const from = this.list.findIndex((item) => item.id === id);
        if (from < 0) return false;
        const to = Math.max(0, Math.min(this.list.length - 1, from + Math.trunc(steps)));
        if (to === from) return false;
        this.record();
        const [item] = this.list.splice(from, 1);
        this.list.splice(to, 0, item!);
        this.emit({ kind: "structure" });
        return true;
    }

    // -- History --------------------------------------------------------------------------------

    /**
     * Groups every edit until {@link commit} into one undo step - a drag, or a slider held down.
     * Nested calls join the open transaction.
     */
    begin(): void {
        if (this.before === null) this.before = cloneItems(this.list);
    }

    /** Closes the open transaction, as one undo step if it changed anything. */
    commit(): void {
        const before = this.before;
        if (before === null) return;
        this.before = null;
        if (sameItems(before, this.list)) return;
        this.pushUndo(before);
        this.redoStack = [];
        this.emit({ kind: "structure" });
    }

    /** Puts back what the open transaction started from - an Escape mid-drag. */
    cancel(): void {
        const before = this.before;
        if (before === null) return;
        this.before = null;
        this.list = before;
        if (this.selected && !this.get(this.selected)) this.selected = null;
        this.emit({ kind: "structure" });
    }

    undo(): boolean {
        this.commit();
        const previous = this.undoStack.pop();
        if (!previous) return false;
        this.redoStack.push(cloneItems(this.list));
        this.restore(previous);
        return true;
    }

    redo(): boolean {
        this.commit();
        const next = this.redoStack.pop();
        if (!next) return false;
        this.pushUndo(cloneItems(this.list));
        this.restore(next);
        return true;
    }

    subscribe(listener: (change: DocChange) => void): () => void {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    }

    // -- Internals ------------------------------------------------------------------------------

    private newItem(shape: ShapeSpec): DocItem {
        const ordinal = (this.ordinals[shape.type] ?? 0) + 1;
        this.ordinals[shape.type] = ordinal;
        return { id: `m${this.nextId++}`, ordinal, shape, hidden: false };
    }

    /** Saves the state before an edit, unless a transaction already holds it. */
    private record(): void {
        if (this.before !== null) return;
        this.pushUndo(cloneItems(this.list));
        this.redoStack = [];
    }

    private pushUndo(state: DocItem[]): void {
        this.undoStack.push(state);
        if (this.undoStack.length > HISTORY_LIMIT) this.undoStack.shift();
    }

    private restore(state: DocItem[]): void {
        this.list = state;
        // A shape restored by undo keeps its name, so the next one drawn must not reuse it.
        for (const item of state) {
            if ((this.ordinals[item.shape.type] ?? 0) < item.ordinal) this.ordinals[item.shape.type] = item.ordinal;
        }
        if (this.selected && (!this.get(this.selected) || this.get(this.selected)!.hidden)) this.selected = null;
        this.emit({ kind: "structure" });
    }

    private emit(change: DocChange): void {
        for (const listener of [...this.listeners]) listener(change);
    }
}
