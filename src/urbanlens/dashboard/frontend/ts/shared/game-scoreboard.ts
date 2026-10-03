/**
 * The end-of-game scoreboard rules SpotGuessr and Trivia share: how a player who stopped playing mid-game is ranked and labelled.
 */

/** How a player stopped playing a game under way, as the server's session summary reports it. */
export type Departure = "left" | "removed";

const DEPARTURE_LABELS: Record<Departure, string> = { left: "Left", removed: "Removed" };

export function departureLabel(departure: Departure | null | undefined): string | null {
    return departure ? DEPARTURE_LABELS[departure] : null;
}

export function departureBadge(departure: Departure | null | undefined): HTMLSpanElement | null {
    const label = departureLabel(departure);
    if (!label) return null;
    const badge = document.createElement("span");
    badge.className = "ul-badge ul-badge--neutral";
    badge.textContent = label;
    return badge;
}

export interface RankableEntry {
    points: number;
    departure?: Departure | null;
}

/**
 * Finishers by points, ranked from 1, then departed players by points, unranked: someone who left cannot place in a
 * game they did not finish.
 */
export function rankFinalScoreboard<T extends RankableEntry>(entries: T[]): { entry: T; rank: number | null }[] {
    const byPoints = [...entries].sort((a, b) => b.points - a.points);
    const finishers = byPoints.filter((entry) => !entry.departure);
    const departed = byPoints.filter((entry) => entry.departure);
    return [
        ...finishers.map((entry, index) => ({ entry, rank: index + 1 })),
        ...departed.map((entry) => ({ entry, rank: null })),
    ];
}
