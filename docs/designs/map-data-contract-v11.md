# D12 — The map cache becomes an accelerator the site can lose, and labels stop being copied into every pin

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D12` · `status: accepted` · `updated: 2026-09-11`

Follows R27 (`docs/MAP_PERFORMANCE.md`), which measured the map payload's cost as 88% Python object
construction and replaced the object graph with a column projection. That fix made the payload
cheap. This decides the shape of the data around it. P102 and P106 are closed - by removing the
mechanisms they were defects in rather than by patching each one - and P101 closes when decision 5
replaces the mutable structure its races live in.

Jess's framing constrains this more than the performance numbers do: *"we anticipate having a large
number of users, and there may be reasons we can't keep all user's data fully cached in redis
forever ... Prewarming the cache should only occur to make an already performant site snappier, not
to address this problem."*

## Decision 1: the per-pin Valkey cache changes shape; it is not removed

`MapPinCache` stores one JSON payload per pin in a hash plus a zset for ordering, mutated by seven
signal receivers. P101 lists three races in `rebuild` alone (concurrent writes lost, a two-key
`RENAME` that is not atomic, a lock released without comparing the token it holds). Each is fixable.

**An earlier version of this decision argued partly from storage, and was wrong to.** It claimed
~1.7 KB per pin and 17–20 MB for a 10,000-pin profile. Measured against Valkey's own `MEMORY USAGE`
(X17): **662 bytes per pin, 6.6 MB for that profile** — an overstatement of 2.6×, and Jess said so
before the measurement was taken. Memory pressure is not the argument.

The argument that survives measurement is shape. The same account's data is **18× smaller** as one
gzipped document (0.36 MB) than as the per-pin hash (6.6 MB), and is read with a single `GET`
against a server that executes commands on one thread rather than `HMGET` across a multi-megabyte
hash. Building it is also cheaper: 851 ms against 1,197 ms for the cache rebuild. Meanwhile the
uncached database path measures 36.8 ms of CPU per 1,000 pins, which is what keeps the cache an
accelerator rather than a dependency — and is why a cache with three known races is not worth
repairing in place when a shape with no room for them costs less.

So the mutable per-pin structure goes and the immutable per-document one (Decision 5) takes its
place in the same phase. Valkey keeps accelerating the map; it stops holding a data structure four
write paths have to keep coherent.

The write-path half is replaced by `services/map_pins/touch.py` — one `UPDATE ... SET updated = now()`
per event, alongside the cache invalidation that already happened. Three paths did that `UPDATE` by
hand with a comment explaining why; the four reorder paths did not, and left the client drawing a
stale icon for six hours (P106). Both consequences now follow from one call, so the next write path
that forgets is missing a function call rather than missing a statement nobody knew about.

## Decision 2: labels ship once per document; pins carry `label_ids`

Today every pin payload embeds its labels' name, colour and icon. That denormalisation is why
editing one label used to rewrite every pin carrying it (P102). That fan-out is already gone - the
profile's cached set is dropped whole instead of rewritten pin by pin - but the *reason* it existed
is still here: a label's colour is copied into every payload, so the payloads really are stale after
an edit and something has to deal with them.

Under a per-profile label dictionary plus `label_ids` they are not stale at all. A label edit
changes one entry in one dictionary, the pins are untouched, and there is nothing to invalidate -
which is O(1) structurally rather than by optimisation, and is what lets the cached document survive
a label edit instead of being thrown away by it.

Icon and colour resolution stays on the server, in `payload.py`, unchanged — `resolve_icon` and
`resolve_color` remain the only implementation of those rules. The client derives only chips, status
and categories, which are dictionary lookups by `kind` and cannot drift from a rule. A TS
reimplementation of the resolution rules was rejected: the map page's inline script cannot import TS
modules (P34/P83), the external API's sync clients need server-resolved values anyway, and it would
make three copies of one rule.

## Decision 3: one streamed NDJSON document with a derived ETag, and a delta over the existing tombstones

The client currently makes 20 sequential round trips of 500 pins for a 10k account, and polls
`Max(Pin.updated)` — which a deletion never moves, so a pin deleted in another tab stays on the map.

- `GET /dashboard/map/document/` streams `head` (carrying the label dictionary), `pin` lines in
  batches of 1000, and `end`. NDJSON because the inline script can split a `ReadableStream` on
  newlines in a few dozen lines, each line is a complete value, and a missing `end` line detects a
  truncated response — which a bare JSON array cannot.
- The ETag is **derived**, not stored: a hash over `Max(Pin.updated)`, the root-pin count,
  `Max(PinTombstone.created)`, the label and customisation maxima, and the payload version. Four
  indexed aggregates, no writes. A stored counter was rejected because it needs a `bump()` at every
  `.update()`/`bulk_update` site — the same sites that already forget — and serialises concurrent
  writers on one row during bulk imports. `services/search/saved_filter_cache.py::pins_fingerprint`
  already exists for the same reason (`Max(updated)` misses deletes); this generalises it.
- `?since=` returns changed pins plus deleted uuids from `PinTombstone`, which already exists with
  400-day retention and is already consumed by `services/pins/pin_sync.py` with a 410 full-resync
  fallback. No new table.
- A ceiling (`UL_MAP_DOCUMENT_MAX_PINS`, 30,000) degrades to viewport mode rather than timing out,
  which revives the bbox path the client already has and currently never uses.

The client store moves to IndexedDB. localStorage's ~5 MB origin quota means the accounts that most
need a cache are exactly the ones that hit `QuotaExceededError` and refetch everything on every
visit — the cache works for everyone who does not need it.

## Decision 4: the filter POST returns data, and `map/data.html` goes

`map.search` renders the whole account into an HTML document today (11.45 MB before R27). It streams
the same NDJSON instead — and when the client's ETag matches, only uuids, because the client already
holds the pins. The XSS invariant changes from "escaped inside an HTML document" to "never an HTML
document", which is a stronger property and is what `test_map_xss.py` will assert.

## Decision 5: the cache comes back, immutable and version-keyed

`ul:map-doc:v11:<profile>:<etag>` → gzipped NDJSON, written `SET NX`, gated on size. Because the
content is a pure function of the key, concurrent builders write identical bytes: there is no lock,
no rename, no generation flag, nothing for a race to corrupt. P101's class of defect cannot recur in
this shape.

Jess allocated 10 GB for Valkey on 2026-09-10, and asked for the cache to be made more useful rather
than removed, which settles this as in-scope for the same phase rather than deferred behind a
measurement. Measured sizes (X17): a 10,000-pin profile's document is 0.36 MB gzipped against 6.6 MB
for the per-pin representation, so an 8 GB cache holds a great many more accounts either way — the
point of the change is the access pattern, not the headroom. What the memory does **not**
change is the ordering: the uncached path is measured green first, and the cache is never what makes
the endpoint viable. Valkey is single-threaded for command execution, so a 2 MB `GET` is also a
better neighbour than `HMGET` across a 17 MB hash — the document shape is more clearly right at 8 GB
than it was at 512 MB, not less.

Prewarm is a debounced rebuild on touch, for profiles that opened the map in the last 24 hours. That
is the only prewarming this design has, and it exists to make a fast page faster.

## Decision 6: no vector tiles for the per-user map

`ST_AsMVT` plus Leaflet.VectorGrid was assessed and rejected here. The per-user map is a client-side
data model — popups, the sidebar list, instant filter switching with no round trip, bulk select,
draggable markers — all driven from `_pinStore`. Tiles would replace "load once, pan freely" with a
request per pan, for a dataset bounded at 30,000 that markercluster handles.

It is the right architecture for the thing that is genuinely unbounded: the D3 public/community map,
where the dataset is not per-user and cannot be shipped whole. Revisit for the per-user map only if a
real account lives above the ceiling in viewport mode.

## Deferred, with the trigger that would reopen each

- Geometry-tier-then-card-tier streaming: reconsider if p50 time-to-all-markers exceeds 1.5 s on the
  largest real account, or if the ceiling is raised.
- Maintained `Pin.latest_rating` / `Pin.child_count` columns (~10 ms per 1000 pins): reconsider when
  SQL exceeds 50% of document build time. If built, use a database trigger — `pin_restructure` and
  `pin_merge` re-parent with `.update()` and fire no signals.
- Client-side icon rules held to Python by a generated fixture: only if delta re-sends after a label
  edit ever measure as a problem.
