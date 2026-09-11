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

## The document endpoint, measured on the development stack

`map.document` was built on 2026-09-11 and measured against the real development
database and Valkey, seeded with 2,000 pins (daphne, not the deployment's gunicorn, so treat these
as relative):

| pins | miss | hit | speedup | document, gzipped |
|---|---|---|---|---|
| 500 | 76 ms | 18 ms | 4.3x | 21 KB |
| 2,000 | 168 ms | 13 ms | 12.5x | 81 KB |
| 10,000 | 955 ms | 20 ms | 47.2x | 402 KB |

Best of three, after a warm-up request. **The miss is linear at ~95 ms per 1,000 pins; the hit is
flat at 13-20 ms whatever the size**, because it is one `GET` and one write of already-compressed
bytes. So the cache helps most where it matters most - at the 30,000 ceiling the miss would be
around 2.9 s against a hit still in the tens of milliseconds.

The request and streaming layers cost nothing measurable over the payload build: the same work with
no request around it measured 170 ms against the endpoint's 168 ms at 2,000 pins.

### Over real HTTP, at the ceiling

The table above is in-process. Measured through nginx and a browser's request
API against a 30,000-pin account, which is what
`tests/integration/specs/ui/map-document.spec.ts` asserts on:

| | uncached | cached |
|---|---|---|
| 30,000 pins | 3,971-4,749 ms | 179-526 ms |

Both halves move by roughly 20% run to run on an otherwise idle development box,
which is why that spec's absolute budgets are set at 8,000 ms and 1,000 ms and
why its real assertion is the **ratio** - measured at 22-26x, and asserted at 3x.
A ratio is measured on one host in one run; a millisecond is a claim about a
machine.

### What a streamed response holds while it streams

A `StreamingHttpResponse` keeps its database connection open until the last byte is written, even at
`CONN_MAX_AGE=0`: sampled at each megabyte of a 3.2 MB document, the connection was open every time.
That is the argument for leaving nginx buffering on for this endpoint rather than the reverse - the
alternative lets a slow client decide how long a worker and a backend are occupied.

**The wire sizes in that table are not a production comparison.** The hit is served pre-gzipped from
Valkey; the miss streams plain NDJSON and is gzipped by nginx, which Django's test client does not
go through. On the wire in production they are comparable. What the hit saves is the work - no
queries, no serialization, no compression - not the bytes.

**A first measurement said 2,227 ms and was wrong.** That was the first request through a cold
process, and the warm-up - imports, query plans - was the whole of the difference. It was briefly
blamed on one chunk being yielded per line; the chunking was kept anyway, because one write per line
is wrong over real HTTP, but Django's test client consumes the generator directly and never writes
to a socket, so nothing here can measure that either way.

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
