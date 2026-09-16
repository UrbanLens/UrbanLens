# X23 — Template-engine share of a map request is unpinned between ~26% and ~41%, not the 61-66% the Jinja2/JinjaX port was originally justified on; the two conflicting `header.html` figures were still both correct

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X23` · `type: experiment` · `status: holds` · `updated: 2026-09-16` · `source: real django.test.Client GET of map.view via force_login, cProfile + CaptureQueriesContext + connection.execute_wrapper + custom per-template render instrumentation, run in urbanlens_development_main_test_runner, settings=urbanlens.UrbanLens.settings.test, UL_TEST_DB_NAME=ul_profile_probe --reuse-db, session of 2026-09-16 (scratch probe scripts, not preserved); corrected in place 2026-09-16, same session day, after an independent probe (see [X24](map-request-redundant-sql-measured.md)) found the 107.19 ms denominator below does not reproduce clean — rewritten here, not stacked as a separate correction`

This is the sequel to [X22](nav-fragment-cache-cost-measured.md), which measured the two nav
fragments in isolation and left open what share of a real request template rendering — and
`header.html` specifically — actually is. This record measures the whole `map.view` request.
Read X22 first; it is not restated here beyond what this record depends on.

## Method

Real `django.test.Client` GET of `map.view`, `force_login`, `DEBUG=False`, cached template loader
— both confirmed at runtime, not assumed. A throwaway user was created first so the measured
account was not the bootstrap site admin: the first `User` created in a test session is
auto-promoted and granted every feature, which would have inflated every code path gated on a
feature flag. Response body 239,887 bytes. Shared 8-core host at load 3.2-3.4 for the whole run.

**Stale-container caveat, recorded because it cost real time**: at the start of this session
`/app/src` inside `urbanlens_development_main_test_runner` was stale relative to the host working
tree — `header.html` was 19,937 bytes / md5 `2e1afa24...` in-container versus 19,633 bytes / md5
`aca89424...` on the host. It was synced with `bin/run_tests.sh` before any number below was taken.
Any template measurement from earlier in this session, before the sync, was reading different
bytes than the working tree and is not reported here.

## Total request cost is not pin-dependent

Warm timing, 4 warmups discarded, 9 timed requests per account:

| pins | wall median | wall min-max | CPU median |
|---|---|---|---|
| 0 | 107.19 ms | 81.32-118.93 | 87.34 ms |
| 500 | 91.60 ms | 83.48-113.74 | 73.88 ms |

The 500-pin run executed second in the same pytest process, so its lower numbers are process
warmth, not a pin effect — pins load via a separate AJAX endpoint, not through this template path.
Both runs rendered the identical 46 renders of 31 templates. Safe conclusion: no pin-dependence in
template cost, as expected.

**This 107.19 ms denominator does not reproduce.** An independent probe the same day, same
container, same database, same 0-pin user (X24's session, below) measured a clean uninstrumented
median of **67.38 ms** for the identical `map.view` GET — 13 timed samples after 6 warmups: 55.27,
58.96, 59.63, 61.63, 61.66, 66.74, 67.38, 67.39, 68.15, 75.37, 76.57, 78.61 ms; mean 67.35, stdev
7.83 (~12%). That is 63% of the 107.19 ms recorded above under nominally identical conditions.
107.19 ms most likely carries residual cost from this session's own instrumentation state or from
pytest process artifacts that X24's probe deliberately avoided (it did not call
`setup_test_environment()`); it should not be read as a clean warm-request baseline. See "The
verdict" below for what this does to the engine-share percentage.

## Per-template exclusive cost (0 pins, one real request)

Root inclusive render: 44.14 ms. Buckets are disjoint: `engine = excl - reverse - sql - deferred -
lazy`.

```
 excl_ms   engine  reverse     sql  deferred   lazy   n  template
  25.625   18.590    0.300   6.520     0.215  0.000   1  dashboard/themes/base.html
   9.935    1.468    0.996   0.000     7.332  0.139   1  dashboard/partials/layout/header.html
   1.110    1.110    0.000   0.000     0.000  0.000   5  ui/_dual_range_slider.html
   1.005    1.005    0.000   0.000     0.000  0.000   2  map/_layers_panel.html
   0.782    0.782    0.000   0.000     0.000  0.000   9  ui/_dialog_header.html
   0.720    0.720    0.000   0.000     0.000  0.000   1  pin_lists/_saved_filter_appearance_fields.html
   0.612    0.612    0.000   0.000     0.000  0.000   1  dashboard/pages/map/index.html
SUM: excl 44.14 = engine 27.71 + reverse 2.03 + sql 6.52 + deferred 7.74 + lazy 0.14
OUTSIDE any template (view + middleware): sql 17.57, lazy 1.57, reverse 0.54
```

**This 27.71 ms engine numerator is itself inflated.** It is the sum of exclusive engine time
recorded by a custom per-template instrumentation layer that wraps every `Template._render` call to
attribute exclusive time per node. Wrapping the very call being measured adds overhead to that call,
and this run never cross-checked 27.71 ms against an unwrapped baseline. Treat it as a number of
uncertain size shaped like an upper bound on true engine cost, not a clean figure.

### Attribution artifact: `base.html`'s 25.6 ms contains `map/index.html`'s own content

With `{% extends %}`, `BlockNode.render` renders the child's overridden block nodelist *inside*
`compiled_parent._render`. `dashboard/pages/map/index.html` is 668 lines / 39,439 bytes — larger
than `base.html`'s 527 lines / 29,775 bytes — and its `{% block content %}` spans
`map/index.html:14-639`, so nearly all of it executes while attribution is charged to
`base.html`, leaving `map/index.html` itself showing only 0.612 ms exclusive.

An earlier session recorded `map/index.html = 24.96 ms` under origin-based attribution instead of
exclusive-per-node attribution. 25.63 ms here vs. 24.96 ms there, and this session's 46 renders of
31 templates vs. that session's 45 of 30, are the same underlying quantity described under two
different naming schemes — not a disagreement between sessions.

## Resolution of the `header.html` contradiction: both prior figures were correct

An earlier perf profile recorded `header.html` at 11.23 ms; X22's pure-`Template.render`
micro-benchmark recorded 1.691 ms for the whole file. Those are not in conflict — they measure
different things, and the breakdown of this session's 9.935 ms exclusive figure shows why:

| component | ms | share of 9.935 |
|---|---|---|
| deferred context values (`Deferred._setup` → ORM/SQL) | 7.332 | 74% |
| `{% url %}` reversal (34 tags in the file) | 0.996 | 10% |
| other `SimpleLazyObject` resolution | 0.139 | 1% |
| true template-engine work | 1.468 | 15% |

The 1.468 ms of engine work matches X22's 1.691 ms literal-context micro-benchmark to within 15%;
that benchmark supplied all context variables as literals, so it bought none of the deferred DB
work that a real request pays for. The split reproduces at 500 pins: 8.884 ms exclusive, engine
1.046, reverse 0.608, deferred 7.128, lazy 0.102 — consistent proportions at both pin counts.

**Important nuance**: the 7.33 ms is *first-reader* cost, not `header.html`'s own cost.
`src/urbanlens/dashboard/context_processors.py` registers 14 urbanlens context processors, of which
**12** are wrapped `@deferred` and **2** (`add_page_name`, `add_demo_context`) are eager and always
run in full regardless of whether any template reads them. The 12 deferred processors offer 26 keys
between them, of which this page reads 14. Whichever deferred keys a template reads first in render
order get billed there, and `header.html` renders first in this page. Caching or deleting
`header.html` relocates that cost to whatever template reads those variables next — it does not
remove it. Per-processor attribution among the 12 is now measured in
[X24](map-request-redundant-sql-measured.md).

## `reverse()` and SQL, independently confirmed against X22

`reverse()`: 84 calls / 2.74 ms / 32.6 µs per call at 0 pins; 84 calls / 1.95 ms / 23.2 µs per call
at 500 pins — the 500-pin per-call figure matches X22's directly-measured 22.99 µs/call. cProfile's
own count (420/5 calls per request, cumulative) agrees on the call count, but its 7.73 ms cumtime
figure is profiler-inflated; 2.74 ms is the honest wall-clock figure.

SQL: 24 queries / 20.00 ms via `CaptureQueriesContext` at 0 pins (23 queries / 20.00 ms at 500
pins) — that timer reports whole milliseconds, so it is coarse. `connection.execute_wrapper` on the
same request gave 27.48 ms; cProfile's psycopg `execute` gave 21.6 ms/req. Real SQL cost is
~20-27 ms, roughly 20-25% of the request against the now-questioned 107.19 ms denominator (see
above; against X24's clean 67.38 ms it would read ~30-40%). It splits: 17.57 ms outside any
template, 6.52 ms inside `base.html`, ~3.4 ms inside the deferred context values read from
`header.html`.

## cProfile category rollup

Over 5 requests; cProfile itself inflates wall time ~1.8x (190.33 ms/req profiled vs. 107.19 ms
unprofiled — the same questioned figure, but the rollup below is a share of the profiled run's own
190.33 ms total and does not otherwise depend on 107.19), penalizing Python-call-heavy engine code
more than C-level `psycopg` execution. Rollup below moves `cursor.execute` into the DB row rather
than leaving it under stdlib:

| category | share (0 pins) | share (500 pins) |
|---|---|---|
| django.db + psycopg (SQL) | 39.3% | 39.8% |
| stdlib / third party excl. psycopg | 23.7% | 22.3% |
| django/template/* (engine) | 22.4% | 23.9% |
| django/* other | 7.7% | 7.3% |
| django/utils/functional.py (SimpleLazyObject) | 4.1% | 3.9% |
| urbanlens/* app code | 1.3% | 1.5% |
| django/urls/* (reverse) | 1.2% | 1.1% |
| urbanlens context_processors.py | 0.2% | 0.2% |

The last row is a trap and needs to be stated plainly: exclusive attribution makes the context
processors look free, because their real cost is the SQL they *cause*, and that cost is attributed
to the DB row, not to `context_processors.py`.

## The verdict: the engine share is unpinned between ~26% and ~41%, not a hard ceiling

- The originally published ratio, 27.71 ms ÷ 107.19 ms, mixes two runs that should not have been
  divided together: 27.71 ms is engine time from the *instrumented* per-template run described
  above (every `Template._render` wrapped to attribute exclusive time), and 107.19 ms was treated as
  this session's *uninstrumented* warm median — but that median does not reproduce. An independent
  same-day probe on the same container, database and 0-pin user (X24) measured a clean
  uninstrumented median of **67.38 ms**. Against that denominator, the same 27.71 ms numerator reads
  as **41%**, not the originally reported figure.
- Both bounds are unsafe in the direction that matters for a ceiling claim: 27.71 ms is inflated by
  the very per-template instrumentation that produced it, and 107.19 ms looks like an
  instrumented-or-pytest-resident figure rather than a clean one (see "This 107.19 ms denominator
  does not reproduce" above). Neither run took its numerator and denominator from the same
  instrumentation state, so **the true engine share was not pinned by either run** — it lies
  somewhere in the ~26-41% range, and closing that range needs a future measurement that takes both
  figures from one run under one instrumentation regime.
- The previously derived "13-17% realistic" figure (a 2-3x engine speed-up applied to the low end of
  this range) is **withdrawn as unsafe**, not recomputed: it would compound an already-unsafe low
  bound, and recomputing it from the high end would carry the same denominator problem forward.
- Even the high end of the range overstates the addressable share: engine time includes the Python
  bodies of custom tags (`vendor_asset`, `map_toolbar`, `map_layers_panel`, `_dual_range_slider`)
  whose logic would be reimplemented as Jinja extensions, not eliminated, by a port.
- The 61-66% figure the port was originally justified on counts DB access, context-processor work
  and URL reversal as "template rendering" solely because it executes lexically inside a render
  call. That is still wrong regardless of where in ~26-41% the true engine share lands — even the
  high end of this range is well under 61-66%.
- `reverse()` memoization — previously proposed in X22 as the fix that generalizes — is worth
  ~2.5% of a request end to end, consistent with X22's per-nav estimate. This figure is unaffected
  by the denominator problem above: it was derived from an absolute per-call cost, not from a ratio
  against 107.19 ms.
- The lever this profile exposed, the `@deferred` context processors (7.33 ms billed to
  `header.html` alone, cross-cutting across every template in a way a template-engine port does not
  touch), now has a per-processor breakdown in [X24](map-request-redundant-sql-measured.md). X24
  also found that the processors are not where most of this request's redundant cost lives — app
  code defeating Django's own caches is.
- **Decision taken, not argued for here**: the project owner has since decided to adopt Jinja2 with
  JinjaX and to migrate templates opportunistically as they are edited, independent of this record's
  numbers. This correction moves the estimate up, not down — the range's upper bound is well above
  what this record originally reported — so its direction favours the port rather than opposing it.
  That is a fact about the correction; this record does not evaluate whether the decision was right.

## Confidence

**HIGH** on composition that does not depend on the 27.71/107.19 ratio: the `header.html` split
reproduced with matching proportions at two pin counts, `reverse()`'s per-call cost matches X22
independently, and the `{% extends %}` attribution artifact is a mechanical fact about
`BlockNode.render`, not a measurement subject to the denominator problem.

**LOW-MODERATE**, downgraded from this record's original HIGH, on the engine-share percentage
specifically. The direct-instrumentation figure (27.71 ms ÷ 107.19 ms) and the cProfile category
rollup's 22.4% were originally read as two independent methods agreeing — but the former rests on a
denominator (107.19 ms) a same-day independent probe could not reproduce (clean median 67.38 ms)
and a numerator (27.71 ms) produced by `Template._render`-wrapping instrumentation whose own
overhead was never subtracted. The 22.4% cProfile figure is internally self-consistent (a share of
that run's own 190.33 ms profiled total, not of 107.19 ms) but still carries cProfile's ~1.8x
inflation, which penalizes engine code more than psycopg. Neither figure should be cited as a clean
measurement of the engine share; use the ~26-41% range from "The verdict" instead.

**MEDIUM** on other absolute millisecond values. Distortions to weigh before citing an absolute
number from this record:

- Shared host at load ~3.3/8 cores; wall times ranged 81.32-118.93 ms within a single warm series.
- cProfile's ~1.8x inflation penalizes Python-call-heavy engine code more than C-level `psycopg`
  execution, so category shares under cProfile understate SQL and overstate engine relative to an
  unprofiled request.
- `CaptureQueriesContext`'s SQL timer has whole-millisecond resolution.
- This was measured through `django.test.Client` — no gunicorn, nginx, network, TLS, or
  static-file serving is included anywhere in these numbers.

## What this does not establish

- A single-point engine-share percentage: only a range (~26-41%), with the true value unpinned
  within it. Closing that range needs a run that takes both the numerator and the denominator from
  the same instrumentation state, which neither this session nor X24 did.
- Per-processor cost among the 12 `@deferred` context processors — flagged above as the more
  interesting lever, now measured in [X24](map-request-redundant-sql-measured.md).
- Anything about pages other than `map.view`, or about accounts other than a 0-pin and a 500-pin
  throwaway user on this one host.
- Whether a Jinja2/JinjaX port is worth doing for reasons other than raw engine speed (e.g.
  ergonomics, macro reuse) — out of scope for this record, and moot in practice: the port has since
  been decided independently of this record (see "The verdict" above).

Do not generalize this record's percentages beyond the map page on this host on this date.
