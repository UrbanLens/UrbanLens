---
status: superseded by ADR-0021
date: 2026-09-24
---

# Storage quotas are enforced generally, not exactly

Formerly `D8`.

The quota check reads `SUM(file_size)` and then inserts in a separate step, so concurrent uploads can overshoot a quota. Jess ruled on 2026-09-06 that exact enforcement is not worth a denormalised running-total counter: imprecise enforcement still stops one user consuming unbounded storage. An over-quota profile keeps everything it has uploaded and is barred from uploading more until it is back under.

## Consequences

- The running-total column (I4 in the same doc) is not built. A plan to sell storage would reopen this.
