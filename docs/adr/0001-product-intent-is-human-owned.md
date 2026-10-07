---
status: accepted
date: 2026-08-27
---

# Product intent is human-owned and lives in GOALS.md

Formerly `D1`. Detail: [`docs/GOALS.md`](../GOALS.md).

`GOALS.md` is Jess's statement of product intent and is read-only to agents. Where code or any other doc conflicts with it, or it says nothing, ask Jess rather than assume. Its fixed points: a pin is private by default and reaches anyone else only as an opt-in, consent-tracked copy, enforced by construction; a wiki is reachable only by someone who earned access with a pin inside the place's official boundary, and no surface may reveal that a location exists; direct messages are end-to-end encrypted with no way to turn it off.

## Consequences

- Shared surfaces (trips, multiplayer games) source content from the wiki or a consented copy, never a live pin reference.
- ADR-0019 adds a second way to keep wiki access (engagement), not a second way to get it.
