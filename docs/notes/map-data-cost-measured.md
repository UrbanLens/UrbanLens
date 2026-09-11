# X17 — What one account's map data actually costs: the cache is 662 bytes a pin, not 1,700

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X17` · `status: holds` · `updated: 2026-09-11`

D12 argued for changing the map cache's shape partly on a storage figure — roughly 1.7 KB per pin,
so a 10,000-pin profile occupying 17–20 MB. That number was inherited, not measured, and Jess said
so: *"I'm not convinced that's an accurate assessment, since we could cache less than 20MB of data
per profile."*

Correct. Measured with `bin/perf/measure_map_payload.py --pins 10000` against a throwaway test
database on chiron, asking Valkey's own `MEMORY USAGE` rather than estimating from payload length:

| | per 1,000 pins | 10,000-pin profile |
|---|---|---|
| **Per-pin Valkey cache** (hash + zset, today) | — | **6.6 MB** (662 B/pin) |
| **One NDJSON document**, uncompressed | — | 4.5 MB (454 B/pin) |
| **One NDJSON document**, gzipped | — | **0.36 MB** (36 B/pin) |

The cache overstatement was 2.6×. At the old 512 MB instance that is ~77 such profiles rather than
the ~25 D12 claimed, and memory pressure is a much weaker argument than that decision made it.

## What the measurement does support

The same account's data is **18× smaller** as one gzipped document than as the per-pin hash it is
stored in today, and cheaper to produce:

| | wall | CPU |
|---|---|---|
| Build and write the per-pin cache (`MapPinCache.rebuild`) | 1,197 ms | 943 ms |
| Build the document (`MapPinPayloadService.all`) | 851 ms | 588 ms |
| Walk the paged path, 10 pages of 1,000 (`page()`) | 591 ms | 368 ms |

So the argument for the document shape is its size and its access pattern — one `GET` against a
single-threaded server, rather than `HMGET` across a multi-megabyte hash — and not the memory
budget. The cache is worth keeping; what is worth changing is what it holds.

## The database path, which decides whether any of it is optional

**36.8 ms of CPU per 1,000 pins** on the paged path, 59.1 ms of wall. The figure D12 rests on
(~33 ms/1,000) is confirmed, slightly optimistic. At that rate a 10,000-pin account's whole map
costs about a third of a second of CPU uncached, which is what makes the cache an accelerator rather
than a dependency.

`all()` costs more per row than `page()` (58.8 vs 36.8 ms CPU per 1,000) despite batching
internally. Not chased down; worth knowing before quoting the document build as the cheaper path on
CPU rather than on bytes.

## The caveat that matters most

`seed_heavy_account` gives every pin **one** shared label. A real account carries several per pin,
so:

- the per-pin and per-document byte figures are both **understated**;
- the gzipped figure is understated *most*, because 10,000 repetitions of one label name is the best
  case compression will ever see — expect the real ratio to be worse, possibly several times worse;
- label resolution is the cheapest it can be, so the CPU figures are floors.

The **ratio** between the two representations is more robust than either absolute, because both were
measured on the same rows in the same run. Re-measure with a realistic label distribution before
quoting an absolute at anyone.
