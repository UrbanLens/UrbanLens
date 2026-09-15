# X20 — A browser label cache pays in rendering and filter round-trips, not in label joins: Organize spends ~250 ms CPU a view rendering and a filter change 300-460 ms of server time, against 1-2 ms in a browser

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X20` · `type: experiment` · `status: holds` · `updated: 2026-09-15` · `source: label_bench.py / label_bench_browser.mjs / label_bench_client.mjs, session of 2026-09-15 (scratch scripts, not preserved)`

## Question

Would a browser-side label cache help at 1,000 concurrent users? Three shapes were priced: rendering Organize and the
label dialogs from a cached vocabulary, filtering and searching pins in the browser, and serving cached rendered HTML.
Each is priced by the whole request or render it would remove, not by the label bytes inside a response.

## Method

**Environment:** the perf environment (`ul_perf_app`, `ul_perf_db`, 2 CPU / 2 GB each) at commit `045101506`, Postgres
JIT off. Dev test containers stopped; nothing else loading the host.

**Account `e2e-labels-heavy`:** 10,000 root pins; 210 visible labels (own + global), 150 carried by pins; 120
parent→child edges (10 tag roots → 30 children → 60 grandchildren, 5 category roots → 25 children); 48,539 pin-label
rows, 4.85 per pin, Zipf-distributed (busiest label on 6,376 pins, median 110); 10 restyled global labels, 4,000
reviews, 2,500 aliases, 8 saved filters.

**Server:** in-process Django test client over the full middleware stack, inside a rolled-back transaction; 5 warm-ups
then 30 timed runs; wall and app-thread CPU p50/p95, SQL time and statement count, response bytes raw and at gzip
level 1. Level 1 is what nginx sends: `src/urbanlens/config/nginx/nginx.conf:109` turns gzip on and sets no
`gzip_comp_level`. One cold pass (1 run) followed a Postgres restart.

**Browser:** headless Chromium with the real 1.19 MB stylesheet, 30 runs. In-browser filter and search timings ran in
Node (V8) on the same 8-core host, not a phone; a phone will be several times slower.

**Limitations:**

- The cold pass emptied Postgres's shared buffers but not the OS page cache. Its SQL times matched the warm ones, so it
  shows only first-request Python warm-up (a first map document build at 2,488 ms CPU against 729 ms warm), not a cold
  disk. Dropping the page cache needs root.
- Every server figure is in-process: no nginx, TLS or network.
- The account is the heavy end. Costs that scale with pins, cards or saved filters are proportionally smaller for
  smaller accounts.

## 1. A filter change on the map

Each change fires `map.search` and `saved_filters.counts`, plus `map.pins.list` when the sidebar is open. Warm p50
(p95).

| Request | wall ms | app CPU ms | SQL ms (stmts) | gz1 bytes |
|---|---|---|---|---|
| `map.search`, name matching all 10k pins | 127 (299) | 68 | 61 (6) | 223,059 |
| `map.search`, narrow name | 72 | 21 | 52 (6) | 643 |
| `map.search`, label root + descendants | 41 | 21 | 18 (7) | 37,568 |
| `map.search`, and(tag,tag) not(status) | 43 | 21 | 23 (9) | 19,619 |
| `map.search`, label root + min_rating 3 | 39 | 20 | 19 (7) | 10,571 |
| `saved_filters.counts` (8 filters) | 260-336 | 58-64 | 216-277 (22-25) | ~245 |
| `map.pins.list` sidebar | 98-269 | 27-57 | 63-212 (6-12) | 167-4,437 |

**In the browser:** an authoritative matcher over the pin store (name, official, wiki and alias names; rating; label
groups with descendant expansion), five filters, 50 runs each:

| Pins in store | p50 | worst p95 |
|---|---|---|
| 10,000 | 0.7-1.7 ms | 3.1 ms |
| 20,000 | 1.1-3.4 ms | 7.4 ms |
| 50,000 | 3-11 ms | 14 ms |

The descendant index builds in 0.16-0.36 ms, once per vocabulary change. Counting 8 saved filters is about 8 passes.

**At D15's ~9 name-filter changes/s** (`docs/designs/capacity-target-and-load-model.md:48-51`), if every filtering
account were this size:

- `map.search`: 0.61 app cores + 0.54 DB-seconds/s, and 1.96 MB/s egress for the broad filter.
- `saved_filters.counts`: 0.54 app cores + 2.2-2.5 DB-seconds/s, on a database with 2 CPUs. The view returns early for
  an account with no saved filters; otherwise it costs roughly pins × saved filters on every change.

**Caveats:**

- **The k6 capacity journey undercounts filter cost.** `tests/perf/k6/population.js` fires `map.search` but neither
  `saved_filters.counts` nor `map.pins.list`.
- **The browser can be authoritative only for criteria the store carries:** names, rating, label groups (once hierarchy
  edges are in the vocabulary) and `last_visited`. Danger, vulnerability, date ranges, custom fields, regions, links
  and detail pin counts still need the server. Two semantics differ: server `min_rating` matches any review
  (`models/pin/queryset.py:264`) while the store carries the latest, and server `has_visits` also accepts a "Visited"
  status label (`queryset.py:274`).
- **The existing optimistic matcher cannot be promoted as-is.** `_sfClientMatches`
  (`templates/dashboard/pages/map/index.html:4429`) compares `last_visited` to `'Never'` (`:4437-4438`) while the map
  payload sends `"never"` (`services/map_pins/payload.py:223`), narrows on label groups without descendant expansion,
  and keeps its hides in `_toolbarHiddenByFilter` until the filter is toggled off; the server response does not restore
  them. Read from source, not yet reproduced in a browser.

## 2. The full map document build (10k pins)

| Variant | wall ms | CPU ms | SQL ms | gz1 bytes |
|---|---|---|---|---|
| As shipped | 1,333 (1,462) | 729 (876) | 616 (22 stmts) | 631,046 |
| Label ids only; appearance resolved client-side; no dictionary | 1,258 | 691 | 621 (20 stmts) | 606,964 |
| No labels at all (a floor, not a proposal) | 949 | 423 | 513 (10 stmts) | 475,272 |

Labels cost 306 ms CPU (42%), 384 ms wall and 156 KB gz1 (25%) of a build. A client-resolved vocabulary removes 38 ms
CPU and 24 KB gz1 of that. The rest is per-pin membership (`label_ids` and the pairs query), which the client needs.
The client's pin store already holds membership between builds, so what would remove the remainder is not rebuilding:
D12's deferred delta mode (`docs/designs/map-data-contract-v11.md:204`) or a more compact membership encoding. Neither
was measured.

## 3. Organize

| Tab | wall ms | CPU ms (p95) | SQL ms | raw / gz1 bytes |
|---|---|---|---|---|
| tags, 117 cards | 282 | 260 (437) | 25 | 908,782 / 99,859 |
| categories | 198 | 176 | 23 | 695,141 / 90,208 |
| status | 128 | 109 | 22 | 520,436 / 79,647 |
| priority | 151 | 138 | 16 | 983,866 / 95,342 |

- **The cost is template rendering, not SQL:** ~2 ms CPU per card, against 25 ms of SQL for the page.
- **A static shell**, priced as a light authenticated page (`notifications.view`: 30 ms CPU, 11 ms SQL), saves ~230 ms
  CPU per tags view.
- **Cached rendered HTML** costs 1.5 ms CPU to fetch from Valkey plus the 4.2 ms middleware floor
  (`notifications.unread_count`), saving ~255 ms CPU per hit. Pin counts load separately (the page has 157 loading
  placeholders and no inline counts), so a cached page goes stale on label, hierarchy or customization edits, not on
  pin edits.
- **At ~1.2 Organize views/s** (5% of D15's page views) that is ~0.3 app cores for label-heavy accounts.
- **In Chromium:** the server HTML body takes 58 ms p50 (81 p95) to parse, style and lay out; a label-free shell plus
  cards built from the real card template takes 54 ms p50 (82 p95). Building the markup takes 0.4 ms and parsing the
  17 KB vocabulary 0.1 ms, so client rendering costs the browser nothing measurable.
- **Bytes (gz1):** the body is 58 KB, of which cards are 21.6 KB and the shell 33.5 KB; the vocabulary is 3.2 KB and
  cacheable. The full response is 100 KB; the remaining ~42 KB is head and inline script.

Rendering Organize in the browser conflicts with `dashboard/CLAUDE.md`'s HTMX-first preference. Cached server HTML gets
the same saving without it.

## 4. Search panel

Two queries, `Bench Tag 1` and `Perf Pin 12`.

- `search.panel`: 476-530 ms wall, 122-127 ms CPU, 339-397 ms SQL.
- The pins provider alone: 26-31 ms CPU and 255-332 ms SQL in one statement, 75-84% of the panel's SQL.
- The same search over the browser store: 1-2 ms at 10k pins, 3-10 ms at 50k; its text index builds once per load in
  10 ms (10k) or 38 ms (50k).
- At D15's search rate (5% of views, two panel requests each, ~2.5/s) the pins query alone is 0.64-0.83 DB-seconds/s.
- The server ranks by trigram similarity, which tolerates typos; the browser test matched substrings. In-browser results
  are instant hits, not the same ranking.
- That one statement is worth fixing server-side whatever is decided about caching.
- "pin" is a type keyword and "tag" a stopword in the parser, so neither query ran every provider; only the pins
  provider's share is comparable.

## 5. Label dialogs and the map page

- `label.pin` category panel (the pin page's label editor): 51 ms wall, 44 ms CPU, 7 ms SQL, 212,813 raw / 10,475 gz1.
- `pin.bulk_edit.label_options`, 20 pins: 12 ms wall, 7 ms CPU, 796 gz1.
- `map.view`: 93 ms wall, 71 ms CPU, 704,928 raw / 162,163 gz1. On this account its label JSON block
  (`apdlg-label-data`) is 18,225 raw / 3,210 gz1, and each label name appears once more in a per-label list (not sized).
  Its largest inline script is 275,131 raw / 85,121 gz1, re-sent on every map page load and uncacheable because it is
  inline (P34, P83).

## Why "the browser already caches labels" and "a cache would save bytes" are both true

The browser caches the label dictionary only for the map's pin store (`_labelDict`, persisted under
`ul_pins_v5_<profile uuid>`, `pages/map/index.html:669-680`). Nothing else reads it, so Organize, the map page's label
list and JSON, and the pin label editor re-send label data as HTML on every request, and the map document re-sends the
dictionary in its head on every build.

## Conclusions (open for reassessment, not decisions)

- **Organize's per-view saving is real** (~230-255 ms CPU on a 117-card tab), from cached HTML or a client render;
  cached HTML gets it without a client rendering path.
- **Filter round-trips are the largest removable load:** up to three requests per change, with `saved_filters.counts`
  the heaviest (216-277 ms SQL per change for this account).
- **A client vocabulary saves little inside the map document build;** membership dominates the labels' share.
- **Search's pins-provider query is a server fix target** independent of any caching decision.
- **The k6 journey should add `saved_filters.counts` and `map.pins.list` to a filter change.**

## What this does not establish

- Whether to build any of it: these are costs, not a decision.
- Any client behaviour on a phone, or any figure through nginx, TLS or a network.
- A cold-disk figure.
- The `_sfClientMatches` defects in a running browser.
