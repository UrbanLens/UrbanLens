/**
 * Pure (no DOM/Leaflet dependency) formatting and small decision logic for
 * spotguessr.ts, pulled out specifically so it's unit-testable - mirrors why
 * map-layers.ts's normalizeBase() lives where it does (see that module's own
 * docstring). spotguessr.ts is an `entries/` file that runs top-level side
 * effects (DOM lookups, a WebSocket connect attempt) the moment it's
 * imported, so it can't be imported directly by a test the way this module can.
 */

export type PanelName = "settings" | "lobby" | "game" | "summary" | "empty";

const PANEL_NAMES: PanelName[] = ["settings", "lobby", "game", "summary", "empty"];

/** Which of SpotGuessr's 5 top-level panels should be visible, given the one that's "active". */
export function panelVisibility(active: PanelName): Record<PanelName, boolean> {
    const visibility = {} as Record<PanelName, boolean>;
    for (const name of PANEL_NAMES) visibility[name] = name === active;
    return visibility;
}

// e.g. "+750 bonus (country, state, city)" - the exact per-tier point values
// live server-side (services.spotguessr.geo_bonus) to avoid drift; the
// client just reports the total and which tiers matched.
export function bonusSuffix(bonusPoints: number, bonusTiers: string[]): string {
    return bonusPoints ? ` (+${bonusPoints} bonus: ${bonusTiers.join(", ")})` : "";
}

export function avatarInitial(username: string): string {
    return username.charAt(0).toUpperCase() || "?";
}

export interface FormattedRatingDelta {
    text: string;
    direction: "up" | "down" | "flat";
}

// Rounds to 1 decimal place to match the server's own rounding
// (services.spotguessr.serializers, which rounds RatingChange.delta the same way).
export function formatRatingDelta(delta: number | null | undefined): FormattedRatingDelta | null {
    if (delta === null || delta === undefined) return null;
    const rounded = Math.round(delta * 10) / 10;
    if (rounded === 0) return { text: "±0 rating", direction: "flat" };
    const direction = rounded > 0 ? "up" : "down";
    const arrow = direction === "up" ? "▲" : "▼";
    const sign = direction === "up" ? "+" : "";
    return { text: `${arrow} ${sign}${rounded} rating`, direction };
}

export function formatCountdown(remainingSeconds: number): string {
    const clamped = Math.max(0, remainingSeconds);
    const minutes = Math.floor(clamped / 60);
    const seconds = clamped % 60;
    return minutes > 0 ? `${minutes}:${String(seconds).padStart(2, "0")}` : `${seconds}s`;
}

// Animation math - shared by the points count-up (animateCountUp()) and the
// reveal distance line "drawing itself in" (animateLineDrawIn()), both in
// spotguessr.ts. Kept here, not there, purely so the math is unit-testable
// without a DOM/Leaflet/requestAnimationFrame environment.

/** Decelerating ease - starts fast, settles gently into the final value. */
export function easeOutQuad(progress: number): number {
    const clamped = Math.min(1, Math.max(0, progress));
    return 1 - (1 - clamped) * (1 - clamped);
}

/** The count-up's displayed value at a given (0-1) point through its animation. */
export function countUpValue(from: number, to: number, progress: number): number {
    return from + (to - from) * easeOutQuad(progress);
}

/** Linear interpolation between two [lat, lng] points at a given (0-1) point through an animation. */
export function interpolateLatLng(from: [number, number], to: [number, number], progress: number): [number, number] {
    const eased = easeOutQuad(progress);
    return [from[0] + (to[0] - from[0]) * eased, from[1] + (to[1] - from[1]) * eased];
}

// Minimal shape spotguessr.ts's real SummaryParticipant satisfies - kept
// local (rather than imported from the entry file) so this module has no
// dependency on it either.
export interface SummaryParticipantLike {
    profile_id: number;
    username: string;
    total_points: number;
    best_round_points?: number | null;
    best_round_distance_meters?: number | null;
}

export function summaryBestRoundSubtitle(participant: SummaryParticipantLike): string | undefined {
    if (participant.best_round_points === null || participant.best_round_points === undefined) return undefined;
    const distance = participant.best_round_distance_meters;
    const distanceSuffix = distance !== null && distance !== undefined ? ` (${(distance / 1000).toFixed(2)} km)` : "";
    return `Best round: ${participant.best_round_points} pts${distanceSuffix}`;
}

// Ties (or a lone solo participant) both fall through to the neutral message -
// only a multiplayer game with a strict leader gets the winner callout.
export function summaryHeadline(
    participants: SummaryParticipantLike[],
    multiplayer: boolean,
    viewerProfileId: number,
): { heading: string; icon: string } {
    if (!multiplayer) return { heading: "Nice work!", icon: "explore" };

    const sorted = [...participants].sort((a, b) => b.total_points - a.total_points);
    const [leader, runnerUp] = sorted;
    if (!leader || (runnerUp && runnerUp.total_points === leader.total_points)) {
        return { heading: "It's a tie!", icon: "handshake" };
    }
    return {
        heading: leader.profile_id === viewerProfileId ? "You win! 🎉" : `${leader.username} wins!`,
        icon: "emoji_events",
    };
}
