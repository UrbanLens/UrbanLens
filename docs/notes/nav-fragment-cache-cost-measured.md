# X22 — Fragment-caching the nav costs more than the rendering it skips; the cost is `reverse()`, not the template

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X22` · `type: experiment` · `status: holds` · `updated: 2026-09-16` · `source: pure Template.render micro-benchmark + real Valkey GET/SET benchmark in urbanlens_development_main_test_runner, session of 2026-09-16 (scratch scripts, not preserved); reverted implementation verified by 37 pytest cases plus ruff/mypy, not committed`

## What was tried

`{% cache %}` (Django template fragment caching) was added around the two nav lists in
`src/urbanlens/dashboard/templates/dashboard/partials/layout/header.html`: the `<ul class="app-nav-links">`
block (`header.html:12-28`) and the `<ul class="app-nav-drawer-links">` block (`header.html:175-193`), each
wrapped so the cache tag sits *outside* the `<ul>...</ul>` span — `test_ai_page_help.py:44` greps
`<ul class="{class}">(.*?)</ul>` and would silently stop matching any `{% url %}` name if the tag moved inside.
Cache key varied on `user.is_authenticated`, `show_games_nav` and `nav_section`, TTL 300s.

The account menu (`header.html:85-152`) was deliberately excluded: it carries `{{ user.username }}`, the
avatar (`header.html:88-106`) and `{% csrf_token %}` at `header.html:152`. Django's masked CSRF token is
trivially unmasked, so a fragment cache serving that block to more than one session would disclose the
first visitor's CSRF secret to whoever hit the cache next.

The implementation was completed, tested and adversarially reviewed, and found **correct**. It was then
**reverted anyway**, because it is slower than not caching. Both facts belong in this record — the code was
not wrong, the plan was.

## Measurements

All on chiron, load average 3.70/8 cores, 2026-09-16. Two different methods were used for different questions
and are not interchangeable (see "Unreconciled" below).

**1. Render cost of the two nav regions**, isolated: plain `Template.render` over a plain `Context`
(no middleware, no view, no HTTP), 300 iterations/trial, 5 trials, run inside
`urbanlens_development_main_test_runner`:

| trial | ms |
|---|---|
| 1 | 0.625 |
| 2 | 0.624 |
| 3 | 0.671 |
| 4 | 0.690 |
| 5 | 0.615 |

Median **0.625 ms** (min 0.615, max 0.690), unit = one render of both regions together. `nav_links` renders
709 bytes, `nav_drawer` 978 bytes. The whole of `header.html` rendered the same way is **1.691 ms** for
23,914 bytes — so the two nav regions are **38.6%** of the template's own render cost by this method.

**2. Cost of serving the same bytes from cache instead**, real Valkey through `ResilientRedisCache`
(`src/urbanlens/core/cache_backend.py:91`) in the app container, not LocMem:

| region | bytes | GET ms | SET ms |
|---|---|---|---|
| `nav_links` | 709 | 0.3405 | 0.3439 |
| `nav_drawer` | 978 | 0.4648 | 0.4503 |

Two `GET`s per page = **0.805 ms**, against **0.625 ms** of rendering the cache would have skipped.
**Net: −0.18 ms/page** — a loss, not a saving. At D15's 1,000-concurrent-user target
(`docs/designs/capacity-target-and-load-model.md`), that is roughly 2,000 extra Valkey ops/second bought for a
negative return. For contrast, a LocMemCache hit for the same two payloads was **0.009 ms** — the loss here is
the network round trip to Valkey, not cache machinery overhead in general.

**3. Where the render cost actually is.** Same two regions, same method, with every `{% url 'name' %}` tag
replaced by a literal string:

| region | url tags | full ms | no-`url`-tags ms | reversal share |
|---|---|---|---|---|
| `nav_links` | 10 | 0.361 | 0.034 | 91% |
| `nav_drawer` | 11 | 0.570 | 0.051 | 91% |

`reverse()` measured directly: **22.99 µs/call**. A dict lookup of a precomputed value: **0.091 µs/call** — a
saving of 22.90 µs/call, ~252×. The nav's 21 `{% url %}` tags total → **0.481 ms/page** recoverable by
memoising argument-free reversal, with no network round trip, no staleness window and no cross-user
key-completeness hazard (a memo cannot leak between users the way a shared cache key can).

The template tree has **1,143 `{% url %}` tags across 460 templates** (not re-audited beyond nav this
session), so a `reverse()` memo generalises to all of them; fragment caching does not, because most fragments
are not shared across the accounts that would need to hit the same cache key.

## Unreconciled: this session's 1.691 ms vs. an earlier ~10.86 ms claim

An earlier planning pass justified caching `header.html` by citing "~10.86 ms saved on every page" — the
*exclusive* cost of the whole 374-line `header.html` (`wc -l` confirms 374 lines as of this session),
measured under `cProfile` exclusive-time attribution inside real HTTP requests, on different hardware (the
perf environment, not chiron). When CSRF and PII considerations forced the cacheable region down from the
whole template to just the two nav `<ul>`s, that whole-template savings figure was carried over uncorrected
to justify a change that covers only a fraction of the template.

**No document in `docs/` currently records that ~10.86 ms figure or a plan to cache `header.html`** — it was
not found in `PROBLEMS.md`, `ROADMAP.md`, `P34`, `P83`, `X21`, or any `docs/notes/*.md` file (checked by
grepping "10.86", "header.html", "fragment cach", "{% cache" and "vary_on" across `docs/` on 2026-09-16). If
it exists, it was never committed to this directory, so there is no older entry to rewrite here — this
paragraph is the correction, on its own.

The two whole-template numbers do not reconcile: **1.691 ms** here (pure `Template.render`, no middleware,
chiron) against **11.23 ms** reportedly measured in the perf environment (`cProfile` exclusive time inside a
real request, different hardware). These are not the same measurement and are **not comparable** — different
method, different hardware, different call context (a bare render vs. exclusive time inside full middleware
and view dispatch). Do not average them, and do not apply either one to the nav regions specifically: the nav
is 38.6% of the *pure-render* total measured here, and nothing in this session establishes what share it is
of the *exclusive-cProfile* total from the other environment.

## Verification status of the reverted implementation

The code itself was not the problem — recorded here so nobody re-discovers the same false lead if fragment
caching is retried in a context where the arithmetic favors it.

- **37 tests passed**: a new fragment-cache suite, plus `test_ai_page_help.py`, `test_games_controller.py`,
  `test_page_query_scaling.py` and `test_context_processor_cost.py` (all four exist at
  `src/urbanlens/dashboard/tests/hypothesis/`). `ruff` clean, `mypy` clean.
- **Adversarial review checked 8 claims; all 8 survived**:
  1. The cached regions read only the three `vary_on` variables.
  2. No `i18n_patterns`/`LocaleMiddleware` is configured, so `{% url %}` output is not language- or
     user-dependent.
  3. The nav labels are hardcoded English.
  4. Django 6's `CacheNode.render` resolves the `@deferred` (`context_processors.py:34`)
     `Deferred`/`SimpleLazyObject` to a concrete bool before `make_template_fragment_key` stringifies it,
     so the key is a stable `"True"`/`"False"`, not an object repr that would vary per-request.
  5. Naming `show_games_nav` in `vary_on` adds no new query: the template body already read it
     unconditionally, and `user_features()` (`models/subscriptions/model.py:293`) short-circuits to
     `frozenset()` for an unauthenticated user before touching the database.
  6. `ResilientRedisCache.get`/`.set` (`core/cache_backend.py:91`) fail open, so a Valkey outage degrades to
     re-rendering rather than raising.
  7. The cache tags sit outside the `<ul>`, so `test_ai_page_help.py`'s `<ul class="...">(.*?)</ul>` regex
     contract (`test_ai_page_help.py:44`) still matches.
  8. (Recorded by the reviewing session as surviving; the specific eighth claim's wording was not preserved
     in a form this entry can cite verbatim — not re-verified this session.)
- **One gap the reviewer flagged, worth preserving**: nothing in the suite generically asserts that no *new*
  per-user value is ever added inside a cached region without a matching `vary_on` term. The tests check the
  three known fields; they do not fail if a future edit adds a fourth per-user read inside the `{% cache %}`
  block and forgets to vary on it. Any future revival of this approach needs that generic guard before it
  ships, not a fourth hand-written assertion.

The implementation was never committed — it was written, measured, reviewed and reverted within one working
session, so there is no commit to point to for the code itself; this file is the only record of it.

## Conclusion

Fragment-caching a small, cheap-to-render region behind a network cache is a net loss whenever the round trip
costs more than the render it replaces. The threshold measured here: a Valkey `GET` costs ~0.34-0.46 ms for a
~1 KB fragment, so a fragment must cost more than that to render before caching it can pay for itself. The nav
did not — it cost 0.625 ms to render and 0.805 ms to fetch from cache, a −0.18 ms/page loss plus ~2,000
extra Valkey ops/second at 1,000 concurrent users for that loss.

The finding that survives the revert: **`{% url %}` reversal, not template machinery, is 91% of the nav's own
render cost.** Memoising argument-free `reverse()` calls is the fix that generalises across all 1,143 `{% url %}`
tags in 460 templates; fragment-caching one nav is not.

**Caveat for a future implementer**: a correct `reverse()` memo must key on script prefix, active language and
active urlconf — not just the URL name and its arguments — or it will silently return a stale path under
`override_settings(ROOT_URLCONF=...)` in tests, and under any deployment that mounts the app at a non-root
prefix.

## What this does not establish

- Whether fragment caching would pay off for a *larger* or more expensive fragment elsewhere in the tree;
  this session priced only the two nav regions.
- The exclusive-cProfile share of `header.html` under real request/middleware conditions on this hardware —
  only the pure-template number (1.691 ms) was measured here, and it is not directly comparable to the
  perf-environment figure it is being contrasted against.
- Whether a `reverse()` memo, once built, changes the balance for any *other* candidate fragment-cache site;
  not evaluated.
