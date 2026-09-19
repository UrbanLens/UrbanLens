# Reply to REData's `T8`: the catalogue is wired and called; the MapLibre migration itself is not built

- **Status: PARTIALLY ANSWERED, 2026-09-19.** `T8`'s §1 (call the dormant tile catalogue, branch on
  `source_type`) is verified and implemented on `release/v_0_8_0`. `T8`'s §2 (the MapLibre GL JS
  migration itself) is not built - it is tracked separately as `PL8`
  (`docs/designs/leaflet-to-maplibre-migration.md`).
- **Direction: inbound.** `../REData`'s `docs/urbanlens-handoff.md` (their `T8`) to this repo.
- `id: N25` · `status: current`

Written 2026-09-19 against `release/v_0_8_0` and `../REData`'s `main` as of PR #65 (commit
`821941d7`). Nothing in `../REData` was edited to produce this. `T8` was written 2026-09-17 and
corrected 2026-09-18 (its own §0); this reply re-checked the corrected version, not the original.

## What was verified against this codebase, not just against `T8`'s claims

| `T8`'s claim | Verdict |
|---|---|
| `registerRedataLayers()` existed, was dead code | confirmed - defined at (then) `map-layers.ts:142`, no caller anywhere |
| REData's `source_type` contract (`D11`) is real but not deployed to production as of the correction | not independently re-checked against REData's production; taken as given per `T8` §0's own correction |
| `comment-map.js:721` creates a Leaflet map per HTMX-swapped preview with no cleanup | confirmed - `L.map(...)` inside an `htmx:afterSettle` handler, no `.remove()` anywhere in the file |
| `leaflet-rotate@0.2.8` (GPL-3.0) is a dependency, used only by `floorplan-editor.ts` | confirmed - `package.json` entry, single importer |
| `map-clusters.ts`, `map-export.ts`, `map-image-overlays.ts` exist and match the description | confirmed by reading each file; see `PL8` for the per-file detail |
| no MapLibre GL JS dependency exists anywhere in this codebase | confirmed - no `package.json` entry, no vendored asset, no import |
| "27 Leaflet maps" | **close - 25 by this session's own final count, after two earlier undercounts; see the correction section below** |

**The map count.** `T8`'s own title and REData's `PL12` item 6 both say "27." This section originally
searched only `frontend --include='*.ts' --include='*.js'` and reported first 15, then (after a
correction below) 12 call sites, both times calling that "under half of 27" and treating REData's number
as effectively refuted. That framing was wrong, and is superseded by a second, later correction (also
below): the search itself was scoped too narrowly - it never looked in `dashboard/templates/**/*.html`,
where 13 more real `L.map(...)` calls live in inline `<script>` blocks (boundary editors, region
pickers, lightboxes, preview maps - see the correction for the full list). **Corrected, final count:
25 `L.map(` call sites across 20 files** - 12 across 8 `.ts`/`.js` source files (unchanged from the
first correction) plus 13 across 12 Django templates. 25 is close to REData's "27," not a refutation of
it - within rounding distance, unlike either of this document's own two earlier numbers. Not reconciled
against REData's own counting method, which is not described in `T8` beyond the total, so the
remaining gap of 2 is unexplained, not necessarily an error on either side.

## What was implemented this session: `T8` §1, the catalogue wiring

**Backend**, `controllers/basemap_tiles.py` (`BasemapTileCatalogueView.get`, now lines 64-123):
reads `source_type` from REData's response, defaulting to `"raster"` when absent (a pre-`D11`
REData deployment never sends the field at all, per `T8` §0's own correction) at line 105. A
raster entry keeps the existing behavior - its `url_template` is rewritten to this app's own proxy
(`_tile_url_template`, line 122), never passed through REData's vendor URL directly, because it
needs REData's key. A vector entry passes `style_url` through unproxied (line 117) - REData never
proxies a single vector tile, so there is no key to hide - and is dropped if `style_url` is
missing (line 106).

`services/apis/locations/redata_basemap_tiles_gateway.py`'s `list_sources` docstring is updated to
document `source_type`/`style_url`; no code change was needed there, since it already passed the
response dicts through raw.

New tests, `tests/hypothesis/test_basemap_tile_proxy.py`, `BasemapCatalogueTests`: a raster entry
with no `source_type` still resolves (`test_a_raster_entry_without_source_type_is_treated_as_raster`),
a vector entry passes through `style_url` unproxied
(`test_a_vector_entry_passes_through_its_style_url_unproxied`), a vector entry with no `style_url`
is dropped (`test_a_vector_entry_with_no_style_url_is_not_offered`), alongside the four pre-existing
cases in the same class. Run 2026-09-19 inside `urbanlens_development_main_app` (`docker exec ...
pytest ... -k BasemapCatalogueTests -v`, unique `UL_TEST_DB_NAME`): **8 passed, 14 deselected in
235.78s**. That runtime is long for eight mocked `TestCase`s - a second, unrelated pytest process
was running concurrently in the same container for the whole span (see `P130`) - but the result
itself is a pass, not a guess.

**Frontend**, `frontend/ts/shared/map-layers.ts`:

- **A real bug in the previously-dead code, fixed before it ever ran once.** REData's id for the
  topographic base layer is `"terrain"` (its `TileLayer.TERRAIN` enum value), but every call site
  in this codebase looks up `tileLayer("topographic")`, never `tileLayer("terrain")`. The old
  `registerRedataLayers()` wrote straight into `TILE_DEFS[layer.id]`, so a REData terrain override
  would silently never have taken effect even after being wired up. `REDATA_ID_ALIASES` (new,
  mirrors `BASE_ALIASES`'s existing reverse-direction mapping) maps `"terrain"` onto the
  `"topographic"` key a map actually reads.
- `registerRedataLayers()` skips a `source_type: "vector"` entry outright (nothing in this
  Leaflet-based engine can render a MapLibre style document) and treats a missing `source_type` as
  raster, matching the backend.
- **Memoized**: `redataLayersPromise` is a module-level `Promise` the function now returns instead
  of issuing a fresh `fetch` per call; `resetRedataLayersCacheForTests()` is exported for tests.
  Every page's map creation can now safely call `registerRedataLayers()` without each one
  triggering its own request.
- **Wired into all six of this repo's map-creation entry points** - the actual ask, since the
  function existed before this session and simply had no caller. `map-page.ts` (highest traffic)
  starts the fetch immediately after `L.map()` is constructed and awaits it only right before
  `MapLayers.create()` runs, so it overlaps with that module's other synchronous setup rather than
  blocking it. `consensus.ts`, `spotguessr.ts`, `album-map.ts`, `map-annotations.ts`,
  `floorplan-editor.ts` each fire `void registerRedataLayers();` at module top level - their maps
  are all created lazily (game start, editor boot, section-expand), well after module load, so the
  fetch has a head start; the failure mode on a very fast interaction is that one map instance
  falls back to a built-in vendor URL for that page load, a missed optimization rather than a
  correctness bug, since updating `TILE_DEFS` after a `TileLayer` is constructed does not
  retroactively re-point it.

New tests, `map-layers.test.ts`, `describe("registerRedataLayers", ...)`: raster registration, the
terrain→topographic alias, vector-entry skip, missing-`source_type`-as-raster, missing-
`url_template` skip, memoization (one fetch across two calls), fetch-rejection and non-ok-response
fallback to built-ins. Verified 2026-09-19, `bun test
src/urbanlens/dashboard/frontend/ts/shared/map-layers.test.ts`: **32 pass, 0 fail, 69 expect()
calls**.

**Also run this session:** `bun run typecheck` clean; `uv run ruff check --fix` clean on every
touched file; `mypy` clean via the app container - after `bin/sync_app.sh`, because the container's
`/app/src` is baked at image build and is not live-synced from the host checkout, and an earlier
mypy/pytest attempt against it was silently checking stale pre-edit code until that was caught.

## What was not implemented: `T8` §2

The MapLibre GL JS migration itself - `T8`'s "the real body of this item" - is not built. It needs
its own scoping/prioritization pass, not a build attempted speculatively in the same session as the
catalogue wiring. The full punch list `T8` gives, plus REData's `D12`, is carried forward as `PL8`
(`docs/designs/leaflet-to-maplibre-migration.md`) rather than re-derived by whoever picks it up
next.

## Correction, found while starting `PL8`'s work: `leaflet-rotate` is not dead weight

This doc's own table above only checked `T8`'s narrow claim ("a dependency, used only by
`floorplan-editor.ts`") - true, but incomplete. `T8` §2 (restated in `PL8` item 6) separately claimed
`leaflet-rotate`'s "only capability - device-compass rotation - is used nowhere in this codebase, so
there is nothing to reimplement," and that framed it as delete-outright, not port. Checked directly
2026-09-19 before acting on it: false. `floorplan-editor.ts` wires `leaflet-rotate` into a real, live
floor-plan "rotate" tool - toolbar button, `t` keyboard shortcut, undo/checkpoint integration
(`floorplan-editor.ts:172-213`, `2351-2456`). `PL8` item 6 is rewritten (not appended-under) to
reflect this: the item becomes porting the rotate tool to MapLibre's native `bearing`/`setBearing()`,
not deleting it outright. Not a hedge, confirmed on later reassessment: this is not just `T8`'s
claim, it is `D12`'s own - its "Costs accepted" section reads "`leaflet-rotate`... is deleted
outright. Its only capability MapLibre lacks is device-compass rotation, which nothing here uses,"
near-verbatim the same claim `T8` §2 restated. `D12` is `status: accepted`; REData's own team may be
planning against its "deleted outright" framing independent of `T8`/`PL12`, so this correction
applies to `D12` directly, not only to the handoff doc built on top of it.

## Correction, found on later reassessment: this doc's own "15 across 10" count was wrong

Not a `T8` error this time - a bug in this doc's own count, above. Two things wrong with it, both
checked directly rather than assumed: (1) `static/dashboard/js/albums.js` was listed as a fourth
"hand-written vanilla-JS file with no TS source," but it has one - `ts/entries/albums.ts` imports
`ts/shared/album-items.ts`, which imports and calls `initAlbumMap` from `ts/shared/album-map.ts`
(already counted separately in the same list), so `albums.js`'s `L.map(...)` is that same call
site's compiled output, not a second one, by the identical reasoning already applied to the other
five compiled-JS files this count excludes. (2) `static/dashboard/js/article-wysiwyg.js`'s two
"matches" are `parseHTML.map((parseRule) => ...)` - `Array.prototype.map()` on an unrelated
variable - not `L.map(` at all; the raw grep command above has no word boundary, so it matches the
literal substring "L.map(" wherever it falls inside a longer identifier, which is exactly what
happens inside "parseHTML.map(" - "parseHTML" ends in "L", immediately followed by ".map(".
Re-run with `grep -rln '\bL\.map(' ...` (word-boundary anchored) and `article-wysiwyg.js` drops out
of the result entirely. Corrected count at the time, verified this way: **12 call sites across 8
source files** - itself incomplete, corrected again below.

## Correction, found on a third reassessment: the count only ever searched `.ts`/`.js`, never templates

While checking a different item (`PL8` item 7, `Leaflet.draw` → Terra Draw) this session found that
two Django templates (`pin_lists/detail.html`, `_saved_filter_dialog_scripts.html`) build their own
`L.map(...)` directly in an inline `<script>` block - real call sites this doc's grep command
(`--include='*.ts' --include='*.js'`) structurally could not see, because Django templates are
`.html`. Widening the search (`grep -rln '\bL\.map(' src/urbanlens/dashboard/templates
--include='*.html'`) turns up **13 more real call sites across 12 template files**, none of them
compiled duplicates or false positives (each checked in context, not just grep-matched): the photo
lightbox (`_photo_lightbox.html`), the boundary-vote dialog (`wiki/_boundary_vote_dialog.html`), the
safety-check-in map (`safety/_safety_map_script.html`), the saved-filter region picker
(`_saved_filter_dialog_scripts.html`), the shared-pin page (`pin_share/detail.html`), the
map-center preview in settings (`settings/index.html`), the pin-list boundary editor *and* its
separate overview map (`pin_lists/detail.html`, 2), the saved-filter detail preview
(`pin_lists/saved_filter_detail.html`), the photo location-confirm map (`vault/photos.html`), the
trip map (`trips/detail.html`), the common-pins map (`profile/common_pins.html`), and the memories
map (`memories/index.html`). Checked for further gaps before settling on a final number: no
`new L.Map(` constructor-form calls anywhere, no split-token forms, no template directory outside
`dashboard/templates`.

**Corrected, final total: 25 `L.map(` call sites across 20 files** (12 across 8 `.ts`/`.js` files,
unchanged from the correction above, plus these 13 across 12 templates) - not "under half of 27," as
this doc claimed twice. REData's original number stands much closer to correct than either of this
document's own two earlier "corrections" of it. `PL8`'s count is fixed to match. Apologies to whoever
at REData read either earlier version of this section and adjusted their own numbers to match this
doc's - this doc's count was the one that needed the confidence walked back, not REData's.

## For whoever reads this next

- `T8` §0's check still applies before trusting §1 as deployed on REData's side: `curl -sH
  "Authorization: Bearer $KEY" https://<redata>/tiles/sources/ | jq '.[0] | keys'` -
  `source_type` present means the contract is live. As of this writing REData's own repo states
  it is deliberately held off production (damballa) until UrbanLens 0.8.0 ships, and every REData
  layer today is raster regardless - vector is additionally blocked on a Jess-only infrastructure
  step (a Garage bucket/Secret) REData's own team cannot unblock either. This session's backend and
  frontend changes were exercised against that raster-only shape; nothing here has been run against
  a live vector entry.
- `PL8` is the migration plan. `P130` is an unrelated environment defect found while trying to run
  this session's tests - it blocks every local pytest run that touches the database, not just this
  work, and needed a manual fix on `development_main` that has not been generalized to other dev
  slots.
- REData's `docs/urbanlens-handoff.md` (`T8`), `docs/PLANS.md` `PL12`, `docs/DECISIONS.md` `D11`
  and `D12` are on their side (`../REData`), not this repo's own `D11`/`D12` (connection pooling
  and the map-data cache contract), which are unrelated decisions that happen to share numbers.
