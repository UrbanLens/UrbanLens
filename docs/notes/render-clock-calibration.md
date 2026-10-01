# X14 — Switching the render mixin to a CPU clock separates the classes worse, not better

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X14` · `status: holds` · `updated: 2026-09-10`

## The proposal, and why it was plausible

`RenderTimeScalingMixin` times requests with `time.perf_counter` and asserts that
one row costs under `MAX_ROW_COST_FRACTION = 0.10` of the page's own zero-row
render. During PL7's design pass it was proposed to switch that to
`time.process_time`, on the reasoning that CPU time does not inflate while a
loaded host deschedules the process, so the instrument would be less noisy — and
that CPU is anyway the defect class R27 found (Python object construction and
template rendering, not database wait).

Both halves are reasonable a priori, and the second is why N11 flagged the mixin
as structurally blind to a uniformly-too-expensive row. Neither is a reason to
change a **calibrated** instrument without re-calibrating: `0.10` was measured,
and swapping the clock silently changes what the number means, because CPU time
drops the database wait that `render_scaling.py`'s docstring says is included on
purpose.

## Method

The repo already ships the rig: `dashboard/tests/urls_render_scaling.py` serves
`/cheap/` (a two-span row) and `/expensive/` (a 1,249-button icon grid per row —
the shape that put a full icon picker inside every row of the achievement admin),
both behind the same 2,000-element fixed chrome so the denominator is realistic.

Five trials per view, each: baseline render, seed 3, render, seed 9, render;
best-of-5 timings per size, both clocks read around the same request. Row cost is
`(large - small) / 9`, expressed as a fraction of that view's own baseline.
App container, 2026-09-10, host load average ~2.6 on 16 cores. Script:
scratchpad `calibrate_clock.py`, driven with
`pytest -c /app/pyproject.toml --rootdir=/app`.

Unit of analysis: one (view, clock) pair's row-cost fraction.

## Result

| view | clock | min | median | max | baseline median |
|---|---|---|---|---|---|
| `/cheap/` | wall | 0.0038 | 0.0081 | 0.0642 | 2.3 ms |
| `/cheap/` | cpu | 0.0100 | 0.0161 | 0.0856 | 1.8 ms |
| `/expensive/` | wall | 3.1087 | 4.4502 | 5.1777 | 2.2 ms |
| `/expensive/` | cpu | 3.5244 | 4.9483 | 5.5033 | 2.0 ms |

Separation, worst benign observation to best offending one — the margin the
threshold has to sit inside:

- **wall: 0.0642 → 3.1087, a factor of 48.5**
- **cpu: 0.0856 → 3.5244, a factor of 41.2**

`0.10` sits between the classes under both clocks.

## Conclusion: keep `perf_counter`

The change was rejected on its own measurement. CPU time separates the two
classes **worse** (41.2x against 48.5x) and moves the worst benign reading from
0.0642 to 0.0856 — from 36% of the threshold to 86% of it, so the margin before a
false failure shrinks by more than half. The predicted noise benefit did not
appear at a scale that matters: CPU is somewhat steadier relative to its own
median (5.3x spread against 7.9x) and still swings by a factor of five, so
neither clock is quiet at these batch sizes.

What that leaves is a real limit rather than a fixable one. A best-of-5 wall or
CPU timing over a 3-row and a 12-row render is not a precise instrument, and
tightening `0.10` on either clock would buy sensitivity by spending the margin
that keeps a loaded host from failing the suite. The answer for the defect class
N11 was worried about is not a better clock — it is
`InstantiationScalingMixin`'s object count, which is exact, machine-independent,
and does not move with load at all. That is the instrument that caught the map
504; this one keeps the job it was calibrated for.

## Not measured

Behaviour on a genuinely idle host, or on one loaded past ~4. If the mixin ever
starts producing false failures in CI, re-run this before adjusting the
threshold — the question is which clock has more margin, and the answer here is
the one already in use.
