---
status: accepted
date: 2026-09-07
---

# A moderator's removal costs reputation slightly and reversibly, through a per-event weight

Formerly `D9`. Detail: [`docs/designs/reputation-removal-weighting.md`](../designs/reputation-removal-weighting.md).

Jess ruled that a contribution removed by a moderator may cost its contributor reputation, but only very slightly, in a way that can be reversed or re-weighted later, and without reshaping any architecture for reputation, which is best effort. So each `ReputationEvent` carries a `weight` (default 1), and the counted total sums `value * weight`. A moderator's removal lowers the weight slightly (0.9 is a starting number); reversing it sets the weight back to 1.

## Considered options

- `retract_event`: rejected. Retraction removes the whole value, which is right for a contributor's own withdrawal, not for "only very slightly".
- Re-scoring the row: rejected. Scoring applies diminishing returns and caps once, and would re-apply them.

## Consequences

- `lifetime_earned` ignores the weight, so a removal costs standing, never access already granted.
- Per-event weights leave room for a learned model to replace the heuristic later.
