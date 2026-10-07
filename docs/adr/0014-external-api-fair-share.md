---
status: accepted
date: 2026-09-12
---

# A shared external-service budget is divided by who is competing for it, not by user count

Formerly `D14`. Detail: [`docs/designs/external-api-fair-share.md`](../designs/external-api-fair-share.md).

External-API budgets are app-wide, so one account could exhaust a service for everyone. Jess rejected a fixed per-user cap, because it throttles ordinary users to protect quota that mostly goes unused. The direction, which is hers, is to measure use per service over time: a lone user may take nearly the whole budget, and only under real contention does each active consumer get a protected floor. Attribution (`ApiCallLog.profile`) is built; the admission rule is not.

## Consequences

- The per-consumer floor is to be set from measured traffic, not guessed. The parameters in the doc are not signed off.
- The app-wide budget remains the spend cap. Unattributed background work is not throttled by fair share.
