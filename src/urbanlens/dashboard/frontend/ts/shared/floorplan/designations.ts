/**
 * What each storey is *called*, derived from where it sits in the stack.
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
 */
export function leadingInt(designation: string): number | null {
    const match = /^(\d+)/.exec(designation.trim());
    if (!match) return null;
    const value = Number.parseInt(match[1] as string, 10);
    return Number.isNaN(value) ? null : value;
}

/**
 * The depth a below-grade designation names.
 */
function basementDepth(designation: string): number | null {
    return leadingInt(designation.trim().replace(/^[Bb]/, ""));
}

/**
 * Work out the label for every floor in a plan.
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
