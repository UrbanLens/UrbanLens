# Map performance

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## R27 — The map payload's cost was 88% Python object construction, not SQL; the fix is query- and allocation-flat

`id: R27` · `status: current` · `updated: 2026-09-10`

Measured in the `app` container against `MapPinPayloadService.all()`
(`services/map_pins/payload.py:492`, reached from `controllers/maps.py:520` `search_map_post` ->
`:1105` `get_map_data`), around the four fix commits on `release/v_0_8_0`: `0fab2b35a` (build the
payload from columns instead of a model graph), `cc0040878` (one payload shape per map endpoint),
`112df3dab` (the map cache answered questions it had not cached), `f2623a5d4` (stop an abandoned
request burning a worker for eight more minutes) - those commit messages record the fixes
themselves; this records the numbers behind them. The exact seeding/timing commands used to produce
the numbers below were not preserved from the original session - re-derive against a profile seeded
to the target pin count if re-running.

### Before, 10,000 pins

- `all()`: 5.79s wall, 5.28s user CPU (91% of wall) against 0.36s of Postgres time - i.e. almost
  entirely Python-CPU-bound, not I/O-bound.
- 63,240 Django model objects built to emit 10,000 flat dicts: a `select_related` companion per row
  plus one fresh `Label` instance per pin-label pair - 128 distinct labels became ~36,000 instances,
  because Django rebuilds the related object once per `prefetch_related` through-row rather than
  once per distinct related row, so a small shared vocabulary still costs one instance per fan-out
  edge. 1,586,920 `setattr` calls, 126,480 signal dispatches.
- The full filter POST round trip (`search_map_post` -> `get_map_data` -> `all()` -> `data.html`
  render): 9.2s wall, ~8.5s CPU, 11.45 MB of HTML. The same 10,000 payloads encoded with
  `json.dumps` instead of rendered: 0.128s / 10.66 MB — 20.5x faster to encode than the template
  took to render an equivalent size.
- The same 10,000 payloads built directly from SQL, bypassing the model layer entirely: 0.36s end
  to end. Postgres building the JSON itself, server-side: 0.28s.
- Cost was linear in pin count, not superlinear anywhere in the SQL: 438/566/571/610 μs per pin at
  1k/5k/10k/25k pins respectively.
- `MapPinCache`: `page(500 pins)` cache hit 12.6ms, miss 236.8ms; `MapPinCache.rebuild(10k)` 6.79s
  cold (see P101 for the concurrency defects this rebuild path still has).

### After, 5,000 pins, with `ANALYZE` run post-seed

(See N10 for why `ANALYZE` matters here: the same measurement *without* it read 4.683s — a 12x
artifact from stale planner statistics on a freshly-seeded table, not a real number, and it is not
comparable to anything below.)

- `all()`: 0.384s wall, 0.332s CPU, 14 queries totalling 0.167s SQL. Zero model objects built -
  confirmed structurally, not just by count, via `InstantiationScalingMixin` (see N11).
- Internals: `_prepare_rows` (`payload.py:390`) for all 5,000 rows: 0.192s. `_label_views_for`
  (`payload.py:394`) for 1,000 ids: 0.027s cold, 0.015s warm. `_serialize_rows` (`payload.py:426`)
  for 1,000 rows: 0.043s.
- `data.html` render: 0.076s / 3.68 MB, down from ~1.3s / 5.7 MB at the same 5,000-pin size before
  the fix.

### What this does not cover

Nothing here re-measures `MapPinCache`'s remaining race conditions (P101) or the autocomplete
endpoint's missing trigram indexes (P100) — neither is touched by the four commits above, and both
are still open.
