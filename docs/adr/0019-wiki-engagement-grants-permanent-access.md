---
status: accepted
date: 2026-09-23
---

# Engaging with a place's wiki grants permanent access

Formerly `D19`.

Jess ruled that a contributor should not lose a wiki because they later moved or deleted the pin that earned it. A profile that views a wiki, or shares content to it, while it holds access keeps access to the place's whole access domain after every qualifying pin is gone. The grant is recorded only after the access check passes, so it entrenches legitimate access and cannot be earned by probing. A Location with no Place has no domain to grant, so there access still ends with the pin.

## Consequences

- Amends ADR-0001 (engagement is a second way to keep access, not to get it) and ADR-0004 (`PlaceAccessGrant` gains a second writer).
- `LocationDetailPinJsonView` checks access itself and records no grant.
