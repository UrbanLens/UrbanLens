/**
 * Undo/redo over whole-document snapshots.
 *
 * The editor's document is small enough that a deep copy per edit is cheaper
 * than maintaining an inverse for every kind of mutation, and it cannot drift
 * from the thing it describes the way a hand-written inverse can.
 *
 * The rule that makes undo feel predictable is that a checkpoint is taken
 * *before* every gesture, not after some of them. Recording only part of what
 * a user does does not give them less undo, it gives them arbitrary undo: the
 * step lands on whichever state happened to be recorded last, so one press can
 * discard an unbounded amount of work.
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
     * Args:
     *     clone: Deep copy for a snapshot. Callers pass their own so this
     *         stays independent of how the document is represented.
     *     limit: How many steps to retain. The oldest is dropped past this.
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
     *
     * Call before mutating, at the start of a gesture.
     *
     * Args:
     *     current: The document as it stands, before the edit.
     *     group: Collapses a run of related edits - successive keystrokes in
     *         one field - into a single step. Passing the same group while it
     *         is still open records nothing; any other checkpoint closes it.
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
     *
     * Args:
     *     current: The live document, which becomes the redo target.
     *
     * Returns:
     *     The state to adopt, or null when there is nothing to undo.
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
     *
     * Args:
     *     current: The live document, which becomes the undo target.
     *
     * Returns:
     *     The state to adopt, or null when there is nothing to redo.
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
     *
     * Call when the document being edited is replaced - an initial load, or a
     * switch to another saved version. A snapshot outliving the document it
     * was taken from is not a safety net: applying it writes the previous
     * document's contents over the one now open.
     */
    clear(): void {
        this.undoStack.length = 0;
        this.redoStack.length = 0;
        this.group = null;
    }
}
