# PL8 — Converting this app's Leaflet maps to MapLibre GL JS is a real multi-week body of work, not built; this is the punch list so it does not need re-deriving

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: PL8` · `status: live` · `updated: 2026-09-19`

Written 2026-09-19, against `release/v_0_8_0`. This plan exists because REData's `docs/urbanlens-handoff.md`
(their `T8`, in `../REData`) describes this side of the work in detail and that detail should not be
re-derived from scratch by whoever eventually picks it up. See
`docs/handoffs/redata-maplibre-catalogue-wiring.md` (`N25`) for what was verified and what was already
built this session - the tile-catalogue wiring, `T8`'s §1, which is separable from this plan and is
**done**, not part of it. This document is the remainder: `T8`'s §2, REData's `PL12` item 6, `D12`'s full
scope.

**Self-hosting fallback is decided, not open.** `D17` (`docs/designs/basemap-self-hosting-fallback.md`)
settles the question of what a self-hosted instance without a REData deployment falls back to once this
migration lands: no new third-party dependency, ever - the client constructs its own MapLibre style
document, wrapping the same free raster vendors (`TILE_DEFS` in `map-layers.ts`) as a `"raster"` source
for self-hosters and this app's own proxy for the hosted instance, upgrading a layer to a `style_url`-driven
vector source only once REData actually serves one. `D17`'s style-construction shape is now code, not
just decision text: `ts/shared/maplibre-raster-style.ts` (`buildRasterStyle`, `toMapLibreTileUrls`) turns
a `TILE_DEFS`-shaped source into a minimal MapLibre style document, correcting a real incompatibility
found while building it - MapLibre's TileJSON-based `tiles` array has no `{s}`/`{r}` token at all
(verified against the actual `maplibre-gl@5.24.0` bundle's own tile-URL substitution logic), unlike
Leaflet's template syntax every `TILE_DEFS` entry is written in, so `{s}` is expanded into one literal
URL per subdomain and `{r}` is dropped. Not wired into any map yet - pure and tested (`maplibre-raster-style.test.ts`)
against both synthetic URLs and this app's own real vendor URLs, including a live check that every
expanded subdomain endpoint (CARTO's/OpenTopoMap's `a`/`b`/`c`) actually serves tiles.

**No priority or sequencing decision is made here.** This is a description of the work's shape and size,
for whoever decides when (or whether) to schedule it.

## Source of truth, and where it lives

This UrbanLens repo has its own `D11` (connection pooling) and `D12` (the map-data cache contract) -
**unrelated decisions that happen to share numbers** with REData's. The ones that govern this plan are
on REData's side, in `../REData`:

- `docs/PLANS.md` `PL12` - "Every cartographic layer resolves to a vendor... both clients are raster-only
  Leaflet apps." Item 6 is the client migration this plan covers; item 7 (the catalogue wiring) is done;
  item 8 (the Flutter app) stays on raster until item 6 ships, by REData's own acceptance line, and needs
  nothing from this repo.
- `docs/DECISIONS.md` `D11` - the vector tile contract (`source_type`, `style_url`).
- `docs/DECISIONS.md` `D12` - MapLibre GL JS adopted natively, not under the `maplibre-gl-leaflet`
  bridge, with REData's own reasoning for why (Hosted-tier by MapLibre's own definition, no active
  maintainer, "no rotation / bearing / pitch support," ~31 fps ceiling, absent from MapLibre's own
  migration guide, and the OSM/Home Assistant precedents `D12` cites).
- `docs/urbanlens-handoff.md` (`T8`) - written for this repo specifically, file-by-file.

## What already exists to convert from (measured this session, `N25`)

**15 `L.map(` call sites across 10 source files**, not REData's inherited "27" - see `N25` for the exact
command and the file-by-file breakdown, including why the raw grep returns 23 across 16 (one test file,
five compiled-JS/TS-source duplicate pairs). The correction does not change this plan's shape: every file
`T8` named by name is real.

- `ts/entries/`: `consensus.ts`, `map-page.ts` (highest traffic), `map-annotations.ts` (2 maps - the main
  editor map and a lightbox preview map), `spotguessr.ts` (2 maps - area guess and round guess),
  `floorplan-editor.ts`.
- `ts/shared/album-map.ts` (shared, used from the album view).
- Hand-written vanilla JS with no TS source: `static/js/comment-map.js` (3 maps - see below),
  `static/js/pin-select-map.js`, `static/dashboard/js/albums.js`, `static/dashboard/js/article-wysiwyg.js`.

**No MapLibre GL JS dependency exists yet anywhere in this codebase** - confirmed this session: no
`package.json` entry, no vendored/CDN asset, no import.

## The work, per REData's `D12` and `T8` §2

Each item below is REData's description of what this side needs, kept close to verbatim because it is
already specific and file-accurate, not because it should be treated as this repo's own design.

1. **Pin MapLibre GL JS v5, not v6.** v6 is ESM-only, and that switch "has failed silently under
   bundlers elsewhere" per `D12`'s own research. Move to v6 deliberately and separately, once checked
   against this project's actual bundler (bun) - not as part of this migration.
2. **A WebGL2 fallback engine is required, not optional, here** - unlike REData's own staff-only
   dashboard, which shipped a plain "unsupported browser" message instead. `D12`'s decision is explicit
   that Leaflet stays on hand as a genuine second rendering engine for the browsers that fail WebGL2 -
   caniuse put that at 95.73% global support in Aug 2026, so ~4.27% of traffic needs it. The cited
   precedent is Home Assistant: adopt the `maplibre-gl-leaflet` bridge, remove it days later once the
   native port lands, keep bare Leaflet only as the WebGL-failure fallback path - the bridge is
   scaffolding for the migration, not a destination.
3. **`map-clusters.ts` is a rebuild, not a port.** It uses an `iconCreateFunction` returning HTML,
   `spiderfyOnMaxZoom`, `animate: true`, and a `maxClusterRadius` that is a function of zoom. MapLibre's
   native clustering has none of those: a scalar radius, no animation, no spiderfy, no HTML icons. There
   is no drop-in equivalent to port.
4. **`map-export.ts` needs a genuinely separate offscreen MapLibre instance**, not a mode switch on the
   live map. It currently rasterizes by reading a private `_tileZoom` and calling `getTileUrl()` per
   tile; MapLibre's equivalent needs `preserveDrawingBuffer`, and that flag's per-frame cost must never
   touch the interactive map.
5. **`map-image-overlays.ts`'s hand-rolled homography (Gaussian elimination) is deleted outright**, in
   favor of MapLibre's native four-corner `image` source. This is a deletion, not a port.
6. **`leaflet-rotate` cannot simply be deleted - REData's `T8` was wrong about this repo.** Checked
   2026-09-19, ahead of acting on it: `leaflet-rotate` (GPL-3.0, unmaintained, monkey-patches Leaflet's
   core) is load-bearing, not dead weight. `ts/entries/floorplan-editor.ts` wires it into a real, live
   "rotate" tool for the floor-plan editor - a toolbar button, a `t` keyboard shortcut, `map.rotate`/
   `touchRotate`/`shiftKeyRotate`/`rotateControl` options, and `map.on("rotate", ...)` handlers driving
   grid rendering and undo checkpoints (`floorplan-editor.ts:172-213`, `2351-2456`). `canRotateView`
   already feature-detects `L.Map.prototype.setBearing` and hides the button when the CDN script fails
   to load, so the code is defensive about *absence*, not evidence the feature is unused. This item
   becomes **port the rotate tool to MapLibre's native `bearing`/`setBearing()`/`dragRotate`** (MapLibre
   supports rotation and pitch natively, unlike Leaflet - REData's own `D12` cites the *lack* of
   rotation support as one reason `maplibre-gl-leaflet` is bridge-only scaffolding, not a destination),
   not a deletion. `leaflet-rotate` itself still gets removed once the floorplan editor's map converts -
   just not for the reason `T8` gave, and not before its replacement exists.
7. **Leaflet.draw becomes Terra Draw.**
8. **Done ahead of the port, independent of it.** `comment-map.js:721`'s per-preview map leak (a fresh
   `L.map(...)` on every HTMX-swapped thumbnail preview with no `.remove()`, confirmed this session)
   is fixed - the leak's cause (a Leaflet map instance discarded when HTMX detaches its container) has
   nothing to do with which rendering engine draws the tiles, so there was no reason to wait for the
   port. Both leaks in the file are fixed the same way: `_renderMapThumb` and `_expandCommentMap` tag
   their Leaflet instance onto its container element, and a delegated `htmx:beforeCleanupElement`
   listener disposes it the moment HTMX detaches that element - not `htmx:afterSettle` (`_initThumbs`'s
   own trigger, for rendering *new* thumbnails), which the pinned htmx source confirms fires on the
   swap's settled target, not on whatever the swap removed. Leaflet's own `Map.remove()`
   is not safe to call twice (verified against the pinned `leaflet@1.9.4` source: the second call
   dereferences internal state the first call already deleted and throws), so every disposal in the
   file is routed through one tag-guarded helper that no-ops on a second attempt, rather than trusting
   that undocumented behavior. Verified against the real file (not just read) with a scratch harness
   simulating both Leaflet's actual `remove()` semantics and htmx's actual bubbling
   `CustomEvent` dispatch (checked against the pinned `htmx@1.9.11` source too) - not committed to the
   suite, since this file has no established test-harness convention yet and inventing one was out of
   scope for a bug fix. Still true and unfixed: what this item does not cover is the couple of call
   sites that replace a pane's innerHTML directly instead of using an HTMX swap - no htmx event fires
   for those either, so the delegated listener cannot reach them; `_expandCommentMap`'s own
   stale-cache check is the one path that already handles this gap for the dialog viewer map, using
   the same idempotent disposal helper. A leaked WebGL context (after the port lands) would have been
   a different order of problem than leaked Leaflet DOM, since Chrome caps a page at 16 contexts total
   - moot now that the underlying leak is closed regardless of which engine renders the map.
9. **The CSP shift raster tiles need.** REData's own two internal maps, already converted
   (`feat/scout-campaign`, per `T8`), found that MapLibre fetches tiles via `fetch()`/XHR - governed by
   `connect-src` - where Leaflet loads them as `<img>`, governed by `img-src`. Any CSP that only allows
   the vendor/REData origins under `img-src` needs the same origins added under `connect-src` before a
   converted map can load a single tile.
10. **A hand-rolled `IControl` replaces `L.control.layers`.** MapLibre has no built-in layer-toggle
    control equivalent; REData's own dashboard conversion is a working reference for the shape of one.

## What is explicitly out of scope here

- **REData's two internal dashboard maps** (boundary map, Location Explorer) are already converted, on
  their side, on `feat/scout-campaign` - not this repo's work, cited here only as a working reference
  for the WebGL2-detection and `IControl` patterns.
- **The Flutter app** (`PL12` item 8) is deliberately parked on raster until this item ships, by REData's
  own acceptance line. Nothing is needed from Flutter now.
- **REData's infrastructure side** (`PL12` items 1-5: DEM chain, Valhalla, PMTiles mirrors, Martin) is
  independent - a self-hosted basemap archive existing and a client that can draw vector tiles are two
  separate prerequisites for the same eventual outcome, not a dependency chain in either direction, per
  `T8` §3.
- **The tile-catalogue wiring** (`T8` §1) - done, see `N25`.

## Not measured, and not decided

- No estimate of engineering time is recorded here; REData's own framing ("the real body of this item")
  and the item count above are the only sizing information available.
- Whether the bridge-then-native sequence (`maplibre-gl-leaflet` adopted temporarily, per the Home
  Assistant precedent `D12` cites) or a direct native build is used here has not been decided.
- Whether MapLibre's native clustering can be made to approximate this codebase's spiderfy/animate/HTML-icon
  behavior closely enough, or whether the rebuild in item 3 above ships with visibly different clustering
  behavior, is untested in either direction - REData's own `PL12` records the same gap on its side ("no
  benchmark exists in either direction" at this repository's pin-count ceiling).
- No target date, owner, or priority. See the top of this document.
