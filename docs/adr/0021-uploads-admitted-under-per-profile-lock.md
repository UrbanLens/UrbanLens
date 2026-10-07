---
status: accepted
date: 2026-09-24
---

# Uploads are admitted under a per-profile database lock; still no running total

Formerly `D21`. Detail: [`docs/designs/storage-running-total.md`](../designs/storage-running-total.md) (section D21).

ADR-0008 kept a cache lock that was meant to narrow the quota race, but it never waited, so concurrent uploads were never serialized. The same gap let one file be stored twice and let parallel calls pass a spent external-media allowance. Every upload is now admitted inside `storage.reserve_upload(profile, size)`: a transaction-scoped Postgres advisory lock per profile, then the quota read as `SUM(file_size)`, the duplicate-checksum lookup, the per-suggestion photo cap and the external-media daily ceiling, then the insert. A waiter past its bound (20 s for a request, 120 s for an import) is refused with 429. An agent took this decision under Jess's standing instruction to fix verified problems at the root; Jess has not yet confirmed it.

## Considered options

- A denormalised running-total counter: still rejected, as in ADR-0008. `file_size` changes in five places, and a hot counter row would queue the media worker's re-encode behind uploads.

## Consequences

- One profile's uploads are serialized. The browser uploads one file at a time, so only another tab or a background import contends. A client that uploads in parallel for throughput would reopen this.
