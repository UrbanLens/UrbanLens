# D12 — The map cache becomes an accelerator the site can lose, and labels stop being copied into every pin; built, with the delta and viewport mode deferred

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
cheap. This decides the shape of the data around it. P101, P102 and P106 are closed - by removing the
mechanisms they were defects in rather than by patching each one.

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
place. Valkey keeps accelerating the map; it stops holding a data structure four write paths have to
keep coherent.

**Done 2026-09-11**, and the measurement that settled it is worth keeping. On a 30,000-pin account a
500-pin page cost 29.8 ms from the database against 8.8 ms from the cache - a 21 ms saving, paid for
with a 3,866 ms worker rebuild on every invalidation. After R27 made the payload a column projection,
the path the cache was accelerating was already fast enough that the cache was not worth its
coherence problem. `map.pins` serves from the projection now and still pages.

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

Under a per-profile label dictionary plus `label_ids` the payloads are not stale at all. A label
edit changes one entry in one dictionary and leaves every pin payload correct, which is O(1)
structurally rather than by optimisation.

**It does not follow that the cached document survives a label edit, and the first draft of this
section claimed it did.** The document's ETag is derived from the pin collection's fingerprint, and
every label write moves that fingerprint by design - `touch_pins_for_labels` bumps `Pin.updated` on
the carrying pins, which is the P106 mechanism the client's poll and the sync API's `?since=` both
read. So a label edit still invalidates the cached document. What normalisation buys is that the
*payloads* stop being wrong, the document shrinks, and a client holding the data can correct a chip
by replacing one dictionary entry rather than refetching the pins that carry it. Making the cached
document itself survive a label edit would mean caching the dictionary separately from the pin
lines; deferred, with the trigger below.

**Built 2026-09-11, and measured.** At a realistic four labels per pin over a 17-label vocabulary,
10,000 pins weigh 3.88 MB rather than 6.46 MB - **40% smaller uncompressed, 12% gzipped** - and the
dictionary that replaces the repetition is 1,335 bytes. Gzip was already collapsing most of the
duplication, so the compressed saving is the smaller number; the uncompressed one is what the server
allocates, the client parses and the browser's store holds. Full figures in X17.

Every response carries the labels its own pins name, not the account's whole vocabulary: the views
are already in hand from serializing those pins, so a page costs no query for it. Only the streamed
document asks for the account's, because its head goes out before its first pin line - one join over
the through table, measured at 26 ms on a 10,000-pin account, once per document.

Icon and colour resolution stays on the server, in `payload.py`, unchanged — `resolve_icon` and
`resolve_color` remain the only implementation of those rules. The client derives only chips, status
and categories, which are dictionary lookups by `kind` and cannot drift from a rule. A TS
reimplementation of the resolution rules was rejected: the map page's inline script cannot import TS
modules (P34/P83), the external API's sync clients need server-resolved values anyway, and it would
make three copies of one rule.

## Decision 3: one streamed NDJSON document with a derived ETag, and a delta over the existing tombstones

The client currently makes 20 sequential round trips of 500 pins for a 10k account, and polls
`Max(Pin.updated)` — which a deletion never moves, so a pin deleted in another tab stays on the map.

- `GET /dashboard/map/document/` streams `head`, `pin` lines in batches of 1000, and `end`. NDJSON
  because the inline script can split a `ReadableStream` on newlines in a few dozen lines, each line
  is a complete value, and a missing `end` line detects a truncated response — which a bare JSON
  array cannot. **Built 2026-09-11**, without the label dictionary, which waits on decision 2.
  `map.pins` is unchanged and still pages: it is what a client that wants pages uses, what an
  account over the ceiling is told to fall back to, and what progressive loading would be built on.
- The ETag is **derived**, not stored: a hash over `Max(Pin.updated)`, the root-pin count,
  `Max(PinTombstone.created)`, the label and customisation maxima, and the payload version. Four
  indexed aggregates, no writes. A stored counter was rejected because it needs a `bump()` at every
  `.update()`/`bulk_update` site — the same sites that already forget — and serialises concurrent
  writers on one row during bulk imports. `services/search/saved_filter_cache.py::pins_fingerprint`
  already exists for the same reason (`Max(updated)` misses deletes); this generalises it.
- `?since=` returns changed pins plus deleted uuids from `PinTombstone`, which already exists with
  400-day retention and is already consumed by `services/pins/pin_sync.py` with a 410 full-resync
  fallback. No new table.
- A ceiling (`UL_MAP_DOCUMENT_MAX_PINS`, 30,000) degrades rather than timing out. **Built**, as a
  `head` line naming `mode: "paged"` and no pin lines, which sends the client back to the paged
  endpoint. Viewport mode over the bbox path the client already has is the better answer and is not
  built.

The client store moves to IndexedDB. localStorage's ~5 MB origin quota means the accounts that most
need a cache are exactly the ones that hit `QuotaExceededError` and refetch everything on every
visit — the cache works for everyone who does not need it.

## Decision 4: the filter POST returns data, and `map/data.html` goes

`map.search` renders the whole account into an HTML document today (11.45 MB before R27). It streams
the same NDJSON instead — and when the client's ETag matches, only uuids, because the client already
holds the pins. The XSS invariant changes from "escaped inside an HTML document" to "never an HTML
document", which is a stronger property and is what `test_map_xss.py` will assert.

## Decision 5: the cache comes back, immutable and version-keyed

`ul:map-doc:<format>:<profile>:<etag>` → gzipped NDJSON, written `SET NX`, gated on the ceiling.
Because the content is a pure function of the key, concurrent builders write identical bytes: there
is no lock, no rename, no generation flag, nothing for a race to corrupt. P101's class of defect
cannot recur in this shape. P101 closed with the per-pin cache it lived in.

**Streaming does not release anything until the last byte.** A `StreamingHttpResponse` holds its
database connection for the whole of the response - measured open at every megabyte of a 3.2 MB
document with `CONN_MAX_AGE=0` - and the worker with it. So nginx buffering stays *on* for this
endpoint: unbuffered, a slow client would set how long a worker and a Postgres backend are occupied,
which is the failure this endpoint exists to avoid. Buffered, nginx drains the application at local
speed and feeds the slow client from its own buffers. The cost is time-to-first-marker, and it is
small because nginx forwards as its buffers fill rather than waiting for the end.

**A deployment note that cost an hour to find.** The document is built by a Celery task, so a deploy
that updates the web image without rebuilding the worker leaves `build_map_document` unregistered:
every miss enqueues a task the worker rejects, the cache never warms, and the only symptom is
`Received unregistered task` in the worker log and an endpoint that is merely slow. The worker and
the web tier must be deployed together - which is already true for other reasons, but this is the
first thing that fails quietly when they are not.

**Built 2026-09-11.** Two details the design did not anticipate. The document is built in a Celery
task rather than in the request that missed, because building it holds the whole thing in memory and
streaming it exists precisely so a request never does; a miss streams from the database and enqueues
the build. And the builder re-reads the fingerprint afterwards and discards the result if it moved,
because the key is a promise about the content and a write during the build would make it a lie.

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

**The build claim is per account, not per version, and the first version of it was wrong.** Keyed on
the version - which is what "several tabs must not each build the same document" suggests - every
edit makes a new key, so one person editing their own map enqueued one full rebuild per edit.
Measured at four edits, four builds; on a 30,000-pin account that is around sixteen seconds of
worker time for four clicks, and each build is discarded anyway if the next edit lands while it
runs. Keyed on the account it is at most one rebuild per account per window, and whoever claims next
builds whatever version is current by then, which is the one worth having. The cost is that a rapidly
edited account is cached less often - which is the correct trade for a cache whose uncached path is
the baseline.

## Decision 6: no vector tiles for the per-user map

`ST_AsMVT` plus Leaflet.VectorGrid was assessed and rejected here. The per-user map is a client-side
data model — popups, the sidebar list, instant filter switching with no round trip, bulk select,
draggable markers — all driven from `_pinStore`. Tiles would replace "load once, pan freely" with a
request per pan, for a dataset bounded at 30,000 that markercluster handles.

It is the right architecture for the thing that is genuinely unbounded: the D3 public/community map,
where the dataset is not per-user and cannot be shipped whole. Revisit for the per-user map only if a
real account lives above the ceiling in viewport mode.

## Status, 2026-09-11

Decisions 1, 2, 5 and 6 are built, and the residual SQL of 3.6 with them. Decision 3's streamed
document and derived ETag are built; its **delta and viewport mode are deferred**, and Decision 4 is
deferred at Jess's request. What follows is why the two halves of 3 were left, because "not built
yet" and "decided against for now" read the same in a diff and are not the same thing.

**The delta and the prewarm both optimise account sizes that do not exist yet.** The miss costs
about 95 ms per 1,000 pins, so a realistic 500-2,000-pin account misses in 50-190 ms - close enough
to a cached hit that the cache barely earns its place there, let alone a delta. Both only start to
pay at 10,000-30,000 pins, where a miss is one to four seconds. The document endpoint, the paged
fallback and the client all already work at that size; what is missing is an optimisation nobody can
currently feel.

**Prewarming on write is also the wrong shape as designed**, which is worth recording because
Decision 5 says to do it. Driving it from `touch.py` - the one place that sees every "this pin looks
different" - means resolving which profiles were touched, and a single edit to a *global* label
touches every profile carrying it. That would enqueue a full document rebuild per affected account:
the exact fan-out this whole design removed, reintroduced by the prewarm meant to make it faster. A
prewarm that is safe has to be driven from somewhere that already knows it is dealing with one
account's own change, and no such choke point exists today.

## Deferred, with the trigger that would reopen each

- The `?since=` delta over `PinTombstone` (Decision 3): reconsider when a real account is large
  enough that a refetch after its owner's own edit is noticeable - a 30,000-pin document is 400 KB
  gzipped and about four seconds uncached, against roughly a kilobyte for the change itself. The
  machinery to build it on already exists and is proven: `services/pins/pin_sync.py` does exactly
  this for the external API, including the 410-to-full-resync fallback when `since` predates
  tombstone retention.
- Viewport/`bbox` mode above the ceiling (Decision 3): reconsider when a real account approaches
  `UL_MAP_DOCUMENT_MAX_PINS`. Until one does, the over-ceiling path is the paged fallback, which is
  tested and correct.
- Prewarming the document on write (Decision 5): reconsider alongside the delta, and only with a
  per-account trigger that cannot fan out across profiles - see the status note above.
- Collapsing the fallback-photo lookup further: it is now one `JSONObject` subquery instead of two
  scalar ones (measured 224 ms against 136 ms at 5,000 pins with two photos each, a 39% saving on
  the projection). A maintained column would remove it entirely; see the deferred entry below.
- Caching the label dictionary separately from the pin lines, so a label edit invalidates only the
  dictionary: reconsider if label edits are frequent enough on a large account that document
  rebuilds show up in worker time. Today one rebuild is a few seconds on a 30,000-pin account and
  nothing on a typical one.
- Geometry-tier-then-card-tier streaming: reconsider if p50 time-to-all-markers exceeds 1.5 s on the
  largest real account, or if the ceiling is raised.
- Maintained `Pin.latest_rating` / `Pin.child_count` columns (~10 ms per 1000 pins): reconsider when
  SQL exceeds 50% of document build time. If built, use a database trigger — `pin_restructure` and
  `pin_merge` re-parent with `.update()` and fire no signals.
- Client-side icon rules held to Python by a generated fixture: only if delta re-sends after a label
  edit ever measure as a problem.
