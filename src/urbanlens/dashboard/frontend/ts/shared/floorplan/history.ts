/**
 * Undo/redo over whole-document snapshots.
 */

/** Deep-copies a snapshot, so a stored state cannot alias the live one. */
export type Clone<T> = (value: T) => T;

export class History<T> {
    private readonly undoStack: T[] = [];
    private readonly redoStack: T[] = [];

    /**
     * The group the most recent checkpoint belonged to, so a run of related
     * edits collapses into one step. Null between groups.
     */
    private group: string | null = null;

    /**
 * Args: clone: Deep copy for a snapshot.
 */
    constructor(
        private readonly clone: Clone<T>,
        private readonly limit = 20,
    ) {}

    get canUndo(): boolean {
        return this.undoStack.length > 0;
    }

    get canRedo(): boolean {
        return this.redoStack.length > 0;
    }

    /** How many steps are held, for tests and for the limit's own guarantee. */
    get depth(): number {
        return this.undoStack.length;
    }

    /**
 * Record *current* as the state to come back to.
 */
    checkpoint(current: T, group: string | null = null): void {
        if (group !== null && group === this.group) return;
        this.group = group;
        this.undoStack.push(this.clone(current));
        if (this.undoStack.length > this.limit) this.undoStack.shift();
        // History has forked: anything that was ahead is no longer reachable.
        this.redoStack.length = 0;
    }

    /**
 * Step back one gesture.
 */
    undo(current: T): T | null {
        const previous = this.undoStack.pop();
        if (previous === undefined) return null;
        this.redoStack.push(this.clone(current));
        this.group = null;
        return previous;
    }

    /**
 * Step forward one gesture.
 */
    redo(current: T): T | null {
        const next = this.redoStack.pop();
        if (next === undefined) return null;
        this.undoStack.push(this.clone(current));
        this.group = null;
        return next;
    }

    /**
 * Forget everything.
 */
    clear(): void {
        this.undoStack.length = 0;
        this.redoStack.length = 0;
        this.group = null;
    }
}
