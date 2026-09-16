# X23 — Template-engine work is ~26% of a map request, not the 61-66% the Jinja2/JinjaX port was justified on, and the two conflicting `header.html` figures were both correct

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X23` · `type: experiment` · `status: holds` · `updated: 2026-09-16` · `source: real django.test.Client GET of map.view via force_login, cProfile + CaptureQueriesContext + connection.execute_wrapper + custom per-template render instrumentation, run in urbanlens_development_main_test_runner, settings=urbanlens.UrbanLens.settings.test, UL_TEST_DB_NAME=ul_profile_probe --reuse-db, session of 2026-09-16 (scratch probe scripts, not preserved)`

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

**Important nuance**: the 7.33 ms is *first-reader* cost, not `header.html`'s own cost. The 14
`@deferred` context processors in `src/urbanlens/dashboard/context_processors.py` bill to whichever
template reads them first in render order, and `header.html` renders first in this page. Caching or
deleting `header.html` relocates that cost to whatever template reads those variables next — it
does not remove it. (Per-processor attribution among the 14 was not measured this session.)

## `reverse()` and SQL, independently confirmed against X22

`reverse()`: 84 calls / 2.74 ms / 32.6 µs per call at 0 pins; 84 calls / 1.95 ms / 23.2 µs per call
at 500 pins — the 500-pin per-call figure matches X22's directly-measured 22.99 µs/call. cProfile's
own count (420/5 calls per request, cumulative) agrees on the call count, but its 7.73 ms cumtime
figure is profiler-inflated; 2.74 ms is the honest wall-clock figure.

SQL: 24 queries / 20.00 ms via `CaptureQueriesContext` at 0 pins (23 queries / 20.00 ms at 500
pins) — that timer reports whole milliseconds, so it is coarse. `connection.execute_wrapper` on the
same request gave 27.48 ms; cProfile's psycopg `execute` gave 21.6 ms/req. Real SQL cost is
~20-27 ms, roughly 20-25% of the request. It splits: 17.57 ms outside any template, 6.52 ms inside
`base.html`, ~3.4 ms inside the deferred context values read from `header.html`.

## cProfile category rollup

Over 5 requests; cProfile itself inflates wall time ~1.8x (190.33 ms/req profiled vs. 107.19 ms
unprofiled), penalizing Python-call-heavy engine code more than C-level `psycopg` execution. Rollup
below moves `cursor.execute` into the DB row rather than leaving it under stdlib:

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

## The verdict

- Template render is 44.14 of 107.19 ms inclusive = 41% of the request; true engine work is
  27.71 ms = **26%**.
- 26% is the hard ceiling for what a template-engine swap could remove, and only if the replacement
  engine were infinitely fast. At a realistic 2-3x speed-up on just the engine slice, that is
  ~14-18 ms of 107 ms, i.e. **13-17%** — not the 61-66% cited to justify the Jinja2/JinjaX port.
- Even 27.71 ms overstates the addressable share: it includes the Python bodies of custom tags
  (`vendor_asset`, `map_toolbar`, `map_layers_panel`, `_dual_range_slider`) whose logic would be
  reimplemented as Jinja extensions, not eliminated, by a port.
- The 61-66% figure this port was justified on counts DB access, context-processor work and URL
  reversal as "template rendering" solely because it executes lexically inside a render call — it
  is not engine cost.
- Two independent methods agree on the engine share: direct per-template instrumentation gives 26%,
  cProfile category rollup gives 22.4%. That agreement is the basis for confidence in this figure.
- `reverse()` memoization — previously proposed in X22 as the fix that generalizes — is worth
  ~2.5% of a request end to end, consistent with X22's per-nav estimate.
- The lever this profile newly exposes is the `@deferred` context processors: 7.33 ms on every
  authenticated page (~3.4 ms SQL, ~4 ms ORM/Python), cross-cutting across every template in a way
  a template-engine port does not touch. Per-processor attribution among the 14 is not yet
  measured.

## Confidence

**HIGH** on composition and relative shares: the `header.html` split reproduced with matching
proportions at two pin counts, and two independent methods (direct instrumentation, cProfile) agree
on the engine share to within 4 points.

**MEDIUM** on absolute millisecond values. Distortions to weigh before citing an absolute number
from this record:

- Shared host at load ~3.3/8 cores; wall times ranged 81.32-118.93 ms within a single warm series.
- cProfile's ~1.8x inflation penalizes Python-call-heavy engine code more than C-level `psycopg`
  execution, so category shares under cProfile understate SQL and overstate engine relative to an
  unprofiled request.
- `CaptureQueriesContext`'s SQL timer has whole-millisecond resolution.
- This was measured through `django.test.Client` — no gunicorn, nginx, network, TLS, or
  static-file serving is included anywhere in these numbers.

## What this does not establish

- Per-processor cost among the 14 `@deferred` context processors — flagged above as the more
  interesting lever, not measured here.
- Anything about pages other than `map.view`, or about accounts other than a 0-pin and a 500-pin
  throwaway user on this one host.
- Whether a Jinja2/JinjaX port is worth doing for reasons other than raw engine speed (e.g.
  ergonomics, macro reuse) — out of scope for this record.

**Do not use this record to start the Jinja2/JinjaX port**, and do not generalize its percentages
beyond the map page on this host on this date.
