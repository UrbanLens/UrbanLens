# Map-performance investigation: a benchmarking trap and a test-instrument gap

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## N10 — A seed-then-measure benchmark without `ANALYZE` measures the planner's ignorance, not the query

`id: N10` · `status: current` · `updated: 2026-09-10`

This cost the map-performance investigation three rounds and will recur in any benchmark here that
`bulk_create`s rows and queries immediately: Postgres has no statistics on a freshly-populated
table, so the planner guesses, badly, until `ANALYZE` runs (autovacuum eventually does this on its
own schedule, not synchronously after a bulk insert). Measured this session:
`MapPinPayloadService.all()` at 5,000 pins ran 4.683s wall without `ANALYZE` and 0.384s with it - a
12x artifact from statistics alone, not from any code difference (see R27 for the surrounding
numbers). It looked exactly like a regression, and sent the investigation chasing an innocent
annotation before the missing `ANALYZE` was identified as the actual cause.

Corollary worth keeping: an ablation that reports a strict *subset* of a query as measurably slower
than the whole query it is part of is a signal the measurement is invalid (stale plan, cold cache,
contended host), not a finding - a subset cannot legitimately cost more than its superset. This
investigation hit exactly that shape (one ablation reported 24x slower than the full query it was a
piece of) before tracing it to the same missing-`ANALYZE` cause.

**Action for any future seed-then-measure benchmark in this repo: run `ANALYZE` (or `VACUUM
ANALYZE`) immediately after seeding, before timing anything.** Not yet enforced by any shared test
helper - `core/tests/scaling.py`'s `SeedScalingMixin` (parent of `QueryScalingMixin`,
`RenderTimeScalingMixin`, and the new `InstantiationScalingMixin` below) has no `ANALYZE` call
anywhere in its `seed_rows()` path (confirmed by reading `core/tests/scaling.py:45-52`), so any
subclass benchmarking against a table with no prior statistics can reproduce this artifact. Not
measured whether the shared mixins' typical seed sizes (tens to low hundreds of rows, per
`docs/TOOLING.md`'s own examples) are small enough in practice to avoid triggering it - the map's
4.683s/0.384s split was measured at 5,000 rows, and smaller seeds may or may not cross the same
planner threshold.

## N11 — `QueryScalingMixin` and `RenderTimeScalingMixin` are structurally blind to a per-row object-count regression; a third axis now exists

`id: N11` · `status: current` · `updated: 2026-09-10`

Complements `docs/TOOLING.md`'s existing `QueryScalingMixin`/`RenderTimeScalingMixin` sections
(`TOOLING.md:522-566`, R13) rather than correcting them - neither is inaccurate about what it
measures, only silent about what it cannot.

The pre-fix `MapPinPayloadService.all()` (removed in `0fab2b35a`; its last form was
`return [self.serialize(pin) for pin in self.prepare_queryset(query).iterator(chunk_size=1000)]`)
is the concrete example of a defect shape both existing scaling mixins ship green against:

- **`QueryScalingMixin` counts statements, and `.iterator(chunk_size=1000)` keeps that count flat
  below 1,000 rows.** Django's `iterator()` re-runs any `select_related`/`prefetch_related`
  companion query once per chunk, so the query count only jumps at multiples of 1,000 rows - any
  `QueryScalingMixin` subclass seeding fewer than 1,000 rows per side (the common case, per
  `docs/TOOLING.md`'s own examples) never crosses that boundary and asserts a flat query count
  against code that is anything but flat in objects built.
- **`RenderTimeScalingMixin` reads the resulting per-row cost as a slow machine, not a defect**,
  because its whole design is a ratio against a machine-speed-cancelling baseline
  (`TOOLING.md:544-552`) - a uniformly 6x-too-expensive row on every trial looks like ordinary
  contention, not a regression, unless a run happens to compare against a fixed absolute budget
  rather than only its own ratio.

`InstantiationScalingMixin` (`core/tests/instantiation_scaling.py`, new this cycle, already wired
to the map payload at `dashboard/tests/hypothesis/test_map_payload_instantiation_scaling.py`)
closes this gap: it counts `django.db.models.signals.post_init` dispatches per rendered row, which
Django's `Model.__init__` sends unconditionally, so it sees every `select_related` companion and
every `prefetch_related` through-row object regardless of how flat the query count looks. Its own
module docstring records the numbers this axis exists to catch - 63,240 objects built for 10,000
rows, over a flat 21 queries, at 88% of a 5.79s wall time inside `Model.__init__`
(`core/tests/instantiation_scaling.py:1-8` — not re-measured independently this session; carried
from that module's own docstring, and consistent with this investigation's own R27 numbers).

Corollary worth keeping the other direction, proven the hard way in this investigation: an
object-count budget alone is not sufficient either. The post-fix payload passes an
`InstantiationScalingMixin` check at zero model objects while its *SQL* could in principle still
regress independently (the query-count/query-fingerprint axis `QueryScalingMixin`/`django-perf-rec`
cover) - a performance suite needs the query-time axis and the object-count axis together, not
either alone.

Not proposed as an edit to `docs/TOOLING.md` itself this session; that doc's "Test helpers" section
(`TOOLING.md:520-604`) does not yet document `InstantiationScalingMixin` at all - worth adding
there directly, in the same style as its siblings, next time that file is touched.
