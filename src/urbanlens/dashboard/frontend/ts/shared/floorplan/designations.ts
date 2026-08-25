/**
 * What each storey is *called*, derived from where it sits in the stack.
 *
 * A floor carries three separate facts, and conflating any two of them is what
 * made the old strip confusing:
 *
 * - **Position** (``level``) - a contiguous signed integer, 0 being the ground
 *   datum. Everything structural reads this: stacking, the floor-below
 *   underlay, stair and lift connectors.
 * - **Designation** - the lift-button code: ``G``, ``1``, ``14``, ``4A``,
 *   ``B2``, ``M``. Blank means "derive it", which is the normal case.
 * - **Nickname** (``name``) - free text like "Boiler level". Numbering-inert,
 *   and deliberately not consulted here: naming a floor "Shop" must not
 *   silently consume a number.
 *
 * Deriving the label rather than storing it is what lets a building skip its
 * 13th floor by designation while its levels stay contiguous, which is what
 * every other part of the editor needs them to be.
 */

/** The part of a floor this module reads. */
export interface FloorLike {
    /** Storey position; 0 is the ground datum, negatives are below it. */
    level: number;
    /** Author-typed code, or blank/absent to derive one. */
    designation?: string;
}

/** Label used for the ground datum when its designation is blank. */
export const GROUND_LABEL = "G";

/** Prefix for derived below-grade labels. */
export const BASEMENT_PREFIX = "B";

/**
 * The integer a designation starts with, if any.
 *
 * ``"4A"`` yields 4 and ``"14"`` yields 14, so an author who types a number
 * re-anchors the count above it. ``"M"`` and ``"PH"`` yield null and leave the
 * count alone, because a mezzanine sits between numbered storeys rather than
 * taking one of their numbers.
 *
 * Args:
 *     designation: The author-typed code.
 *
 * Returns:
 *     The leading integer, or null when the code does not begin with digits.
 */
export function leadingInt(designation: string): number | null {
    const match = /^(\d+)/.exec(designation.trim());
    if (!match) return null;
    const value = Number.parseInt(match[1] as string, 10);
    return Number.isNaN(value) ? null : value;
}

/**
 * The depth a below-grade designation names.
 *
 * Read through an optional ``B`` prefix, because that is how people write a
 * basement: typing ``B4`` has to re-anchor the floors beneath it just as
 * typing ``14`` re-anchors the ones above, and ``leadingInt`` alone would see
 * only a letter and leave the count where it was.
 *
 * Args:
 *     designation: The author-typed code.
 *
 * Returns:
 *     The depth, or null when the code names no number.
 */
function basementDepth(designation: string): number | null {
    return leadingInt(designation.trim().replace(/^[Bb]/, ""));
}

/**
 * Work out the label for every floor in a plan.
 *
 * Above ground, a counter walks upward from 1: a blank floor takes the counter
 * and advances it, a floor whose code starts with a number re-anchors the
 * counter past that number, and a floor whose code does not start with a
 * number is skipped over without disturbing it. Below ground the same walk
 * runs downward with a ``B`` prefix.
 *
 * Uniqueness is *not* guaranteed: two floors given the same explicit
 * designation keep it, since correcting the author's own typing here would be
 * more surprising than showing them the clash. Validate at the point of entry
 * if that matters.
 *
 * Args:
 *     floors: The plan's floors, in any order. Not modified.
 *
 * Returns:
 *     A label per floor, keyed by the floor object itself so callers do not
 *     have to keep positions and floors in step.
 */
export function deriveDesignations<T extends FloorLike>(floors: readonly T[]): Map<T, string> {
    const labels = new Map<T, string>();

    const above = floors.filter((floor) => floor.level >= 0).sort((a, b) => a.level - b.level);
    let counter = 1;
    for (const floor of above) {
        const typed = (floor.designation || "").trim();
        if (typed) {
            labels.set(floor, typed);
            const anchor = leadingInt(typed);
            if (anchor !== null) counter = anchor + 1;
            continue;
        }
        // The ground datum is named, not numbered, and does not consume the
        // first storey number - a building's "1" sits above its ground floor.
        if (floor.level === 0) {
            labels.set(floor, GROUND_LABEL);
            continue;
        }
        labels.set(floor, String(counter));
        counter += 1;
    }

    const below = floors.filter((floor) => floor.level < 0).sort((a, b) => b.level - a.level);
    let depth = 1;
    for (const floor of below) {
        const typed = (floor.designation || "").trim();
        if (typed) {
            labels.set(floor, typed);
            const anchor = basementDepth(typed);
            if (anchor !== null) depth = anchor + 1;
            continue;
        }
        labels.set(floor, `${BASEMENT_PREFIX}${depth}`);
        depth += 1;
    }

    return labels;
}

/**
 * Renumber floors so their levels are contiguous, holding the ground datum.
 *
 * Sparse or colliding levels arrive from a mid-stack deletion, from a
 * third-party import, and from any older client. Everything structural assumes
 * one storey per level and no gaps, so the repair happens on load rather than
 * being enforced at the edge where a legitimate outside plan would be rejected.
 *
 * Args:
 *     floors: The plan's floors. Not modified.
 *
 * Returns:
 *     The same floors in stack order, each paired with the level it should
 *     take. The floor nearest the old ground datum keeps level 0.
 */
export function contiguousLevels<T extends FloorLike>(floors: readonly T[]): Array<{ floor: T; level: number }> {
    const ordered = [...floors].sort((a, b) => a.level - b.level);
    if (!ordered.length) return [];

    // Whichever floor is nearest the datum stays the datum, so a repair never
    // silently moves which storey the user considers the ground.
    let groundIndex = 0;
    for (let i = 1; i < ordered.length; i++) {
        const candidate = ordered[i] as T;
        const best = ordered[groundIndex] as T;
        if (Math.abs(candidate.level) < Math.abs(best.level)) groundIndex = i;
    }

    return ordered.map((floor, index) => ({ floor, level: index - groundIndex }));
}
