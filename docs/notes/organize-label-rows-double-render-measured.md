# X25 — Organize rendered its label cards twice to deliver three numbers per card: the deferred stats cost ~16 ms against ~100 ms to render the cards, so the deferral was removed

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X25` · `type: experiment` · `status: holds` · `updated: 2026-09-16` · `source: two pytest probes in urbanlens_development_main_test_runner, settings=urbanlens.UrbanLens.settings.test, session of 2026-09-16 (scratch probes, not preserved)`

This extends [X20](label-cache-and-filter-cost-measured.md) §3, which priced the Organize tags tab
alone (282 ms wall / 260 ms CPU, unit=view, n=1 request read off 30 warm runs on the perf
container) and recommended cached rendered HTML as the saving. X20 never measured the `label.rows`
HTMX backfill every Organize load triggered; this record does. It does not contradict X20 — X20's
tab-deferral description was accurate (deferral landed 2026-08-31 in `2f197c58a`, before X20
measured) — it measures a request X20's scope excluded, so X20 understated Organize's per-view cost
rather than getting it wrong.

**The deferral this record measured has since been removed** (see *Outcome*). The numbers below
describe the code as it stood before that change.

## Method

Two probes, both inside `urbanlens_development_main_test_runner`,
`settings=urbanlens.UrbanLens.settings.test`. Both seeded one profile with 120 tag labels (30 of
them given a parent), 200 pins, 5 labels per pin, with a throwaway first user created and logged in
first to absorb the bootstrap site-admin promotion.

1. **Request-level.** Real `django.test.Client`, `force_login`. 1 warm-up then 7 timed requests per
   endpoint, median wall time reported; query count and response size captured separately on an 8th
   request via `CaptureQueriesContext`.
2. **Decomposition.** Direct calls, no HTTP: the queryset, the priming and the template render timed
   separately, 1 warm-up then median of 7 each, under `override_settings(DEBUG=True)` so
   `connection.queries` populated. Rendered via `render_to_string` with **no request**, so context
   processors are excluded and the figure is template cost alone.

## Results — request level; wall time is the median of 7, queries/bytes from a separate 8th run

| request | median wall | queries | bytes |
|---|---|---|---|
| `organize.index?tab=tags` | 379.48 ms | 19 | 813,028 |
| `label.rows?label_kind=tag` | 541.34 ms | 10 | 425,871 |

A single Organize view cost roughly 920 ms of wall time across the two requests together; the
backfill (`label.rows`) was 59% of that — the larger half of the page's real cost, not a cheap
top-up.

## Results — decomposition; this is the part that decided the fix

| stage | median wall | queries |
|---|---|---|
| label list, `.with_hierarchy()` (what the first paint used) | 24.10 ms | 4 |
| label list, `.with_pin_counts()` (what the backfill used) | 34.79 ms | 4 |
| label list, two `GROUP BY` aggregates instead (alternative, not adopted) | 23.60 ms | 6 |
| `prime_total_pin_counts` on an already-built list | 5.06 ms | 2 |
| render 120 cards, `stats_pending=True` (deferred paint) | 97.60 ms | — |
| render 120 cards, `stats_pending=False` (real stats) | 104.54 ms | — |

**The stats cost ~16 ms** (~10.7 ms of extra queryset over the no-stats version, plus ~5.1 ms of
priming) **against ~100 ms to render the cards they sit in.** The deferral therefore spent a second
full render plus a round trip to save ~16 ms.

The two render rows are the sharpest part: `stats_pending` only substitutes a spinner `<span>` for
each number, so **the deferred paint emitted 414,122 bytes against the real paint's 413,982** — the
same markup, minus three numbers per card. The first paint was never cheaper in bytes or in
rendering; it was only cheaper by the ~16 ms of stats it skipped.

## The mechanism, read from source (before the change)

- `build_organize_page_context` unconditionally set `stats_pending: True`. `_rows_if_active`
  materialised only the active tab's queryset, built with `.with_hierarchy()` — no pin counts.
- `organize_label_panel.html:2` fired `hx-get` to `label.rows` on `revealed` whenever
  `stats_pending or rows_deferred`. For the active tab `rows_deferred` was `False` — but the
  condition was an `or`, so it fired on `stats_pending` alone. The page therefore rendered all ~120
  cards inline without counts, then immediately re-fetched the same ~120 cards from `label.rows`
  with counts and swapped them in via `innerHTML`. **The active tab's card list was rendered twice.**
- **The count path was never an N+1.** `_rows_ctx` calls `Label.prime_total_pin_counts(label_list)`
  once per request: one edge-list query, a Python descendant walk, and a single `Count("pins")`
  aggregate — 2 queries, measured above at 5.06 ms. The cost was rendering and payload bytes, not
  query count.

## Outcome

The deferral was removed rather than optimised: `stats_pending` is deleted, the active tab computes
its stats inline (`.with_pin_counts()` plus priming in `_rows_if_active`), and the active tab no
longer issues `label.rows` at all. Per Organize view that trades **~16 ms added to the page** for
**one 541 ms / 425 KB request that no longer happens**.

Tab deferral (`rows_deferred`) is untouched and still worthwhile — an inactive tab renders no cards
at all, which is a real saving rather than a re-render. The reveal path still calls `label.rows`,
which still returns full cards.

## Two things found on the way, neither fixed here

1. **Nothing reads a child label's `pin_count`.** `with_pin_counts()` prefetches children with
   `Count("pins", distinct=True)` annotated on each; no template or serializer reads it, so that
   `COUNT(DISTINCT …)` is computed per child and discarded. Dropping it (with `.only()` children and
   two `GROUP BY` aggregates in place of the two correlated subqueries) measured at 23.60 ms — the
   stats become free relative to `with_hierarchy`'s 24.10 ms. Not adopted: the method is shared with
   `external_api/views.py` and the label serializer, and ~11 ms did not justify the blast radius
   while the ~540 ms win was available elsewhere.
2. **The two render paths disagree about customizations.** `organize.py` applies
   `.with_customizations_for(profile)` to categories and statuses; `labels.py::_queryset_for_kind`
   applies it only to tags. Both paths render the same cards, so a customized global category or
   status can show a different name or colour on the first paint than after a tab reveal or any
   mutation that re-renders rows. Unverified against a browser; found by reading, not measuring.

A third finding, recorded before the fix chose a different route: `renderTreeView` deep-clones each
card (`card.cloneNode(true)`) and rewrites only the clone's own `id`, so ids nested inside the clone
are duplicated in the DOM, and `buildNode`'s recursion has no "already placed" guard, so a label
with several parents is cloned once per parent path. That is invalid HTML and makes any
`hx-swap-oob` keyed by id unsafe on this page. Still true, still latent, no longer on the path of
this work.

## Caveats

- **Absolute milliseconds are soft.** Both probes ran inside pytest with `setup_test_environment()`
  active (per-render signal instrumentation), on a shared 8-core host under other load, through
  `django.test.Client` with no nginx, gunicorn, or network in the path. The ratios are the reliable
  part — everything compared here was measured identically, in the same process, same session.
- **The decomposition's render figure excludes context processors**, since `render_to_string` was
  called without a request. It is template cost alone, which is what the stats-versus-rendering
  comparison needs; it is not the whole of what a real request pays. The gap between the ~100 ms
  render here and the 541 ms request above is request overhead, dominated by context processors.
- **200 pins is far below a real heavy account.** X20's account carried 10,000 pins / 48,539
  pin-label rows. The stats side scales with pins-per-label and with label count (the two correlated
  subqueries in `with_pin_counts` are one subquery execution per row), so the ~16 ms figure will
  grow on a heavy account while the render side grows too; the ratio has not been re-measured at
  that size.
- **Different substrate from X20.** This ran in the dev test runner; X20 ran in the 2-CPU perf
  container. Do not cross-compare absolute milliseconds between the two records.

## What this does not establish

Not measured: any Organize tab other than tags, any account above 200 pins, wall time through nginx
or gunicorn, or the post-change request-level cost of `organize.index` (the ~16 ms addition is the
decomposition's figure, not a fresh end-to-end measurement).
