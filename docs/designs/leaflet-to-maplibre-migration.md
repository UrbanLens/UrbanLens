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
URL per subdomain and `{r}` is dropped. Subdomains are an explicit `RasterSourceInput.subdomains` parameter
(defaulting to Leaflet's own `"abc"`), not a hardcoded constant - caught on reassessment: `TileDef.options`
in `map-layers.ts` is typed as the real `L.TileLayerOptions`, which already has a `subdomains` field, so a
future `TILE_DEFS` entry could set a custom one with no compile error to catch a hardcoded assumption.
`tileSize: 256` was checked against real tile bytes (not just the pre-existing code comment it was copied
from) for every current `TILE_DEFS` vendor, Esri's JPEG-served satellite layer included - genuinely 256px
across the board. The source's `maxzoom` (from `maxNativeZoom`) is MapLibre's *upscale* field ("data from
tiles at the maxzoom are used... at higher zoom levels", matching Leaflet's own semantics) - this module
never sets the differently-named, differently-behaved *layer*-level `maxzoom` (a hide/cutoff field), which
isn't even in its `MapLibreRasterLayer` type. Not wired into any map yet - pure and tested
(`maplibre-raster-style.test.ts`) against both synthetic URLs and this app's own real vendor URLs, including
a live check that every expanded subdomain endpoint (CARTO's/OpenTopoMap's `a`/`b`/`c`) actually serves tiles.

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

**25 `L.map(` call sites across 20 files**, close to REData's "27" - not the "under half of 27" this
document claimed across two earlier, narrower counts (12 across 8, before that 15 across 10). Both were
undercounts of the same kind: real call sites in `.ts`/`.js` source were counted correctly, but nothing
ever searched Django templates, where 13 more real `L.map(...)` calls live in inline `<script>` blocks.
See `N25`'s own correction sections for the full history and per-file breakdown of both fixes - the
`.ts`/`.js` miscounts (a compiled-duplicate file wrongly counted twice, two `Array.prototype.map()`
false positives from an unanchored grep pattern) and the template-search gap. This does not change this
plan's shape: every file `T8` named by name is real, and REData's own number turns out closer to right
than this document's repeated attempts to correct it.

- `ts/entries/`: `consensus.ts`, `map-page.ts` (highest traffic), `map-annotations.ts` (2 maps - the main
  editor map and a lightbox preview map), `spotguessr.ts` (2 maps - area guess and round guess),
  `floorplan-editor.ts`.
- `ts/shared/album-map.ts` (shared, used from the album view - its compiled output, reached via
  `ts/entries/albums.ts` → `ts/shared/album-items.ts` → `initAlbumMap()`, lands in
  `static/dashboard/js/albums.js`, not a second source of its own).
- Hand-written vanilla JS with no TS source: `static/js/comment-map.js` (3 maps - see below),
  `static/js/pin-select-map.js`.
- Django templates with an inline `<script>` block that builds its own map, no TS/JS source at all - 12
  files, 13 maps: `_photo_lightbox.html`, `wiki/_boundary_vote_dialog.html`,
  `safety/_safety_map_script.html`, `pin_lists/_saved_filter_dialog_scripts.html`,
  `pin_share/detail.html`, `settings/index.html`, `pin_lists/detail.html` (2 - a boundary editor and a
  separate overview map), `pin_lists/saved_filter_detail.html`, `vault/photos.html`, `trips/detail.html`,
  `profile/common_pins.html`, `memories/index.html`.

**As of 2026-09-19, the pilot conversion below is live** - `maplibre-gl` is a `package.json`
`devDependency` (types only; the runtime is CDN-loaded via the already-pinned `vendor_assets.py`
entries), and `_renderMapThumb` genuinely renders through it on WebGL2-capable browsers. See "The
first real conversion" below for what shipped and what's still Leaflet-only.

## The work, per REData's `D12` and `T8` §2

Each item below is REData's description of what this side needs, kept close to verbatim because it is
already specific and file-accurate, not because it should be treated as this repo's own design.

1. **Pin MapLibre GL JS v5, not v6.** v6 is ESM-only, and that switch "has failed silently under
   bundlers elsewhere" per `D12`'s own research. Move to v6 deliberately and separately, once checked
   against this project's actual bundler (bun) - not as part of this migration.
2. **A WebGL2 fallback engine is required, not optional, here** - unlike REData's own staff-only
   dashboard, which shipped a plain "unsupported browser" message instead. `D12`'s decision is explicit
   that Leaflet stays on hand as a genuine second rendering engine for the browsers that fail WebGL2 -
   caniuse put that at 95.73% global support in Aug 2026, so ~4.27% of traffic needs it. Home Assistant is
   the cited precedent for exactly that permanent dual-engine pattern - not, as an earlier version of this
   item implied, for temporarily adopting the `maplibre-gl-leaflet` *bridge*: `D12`'s own text cites Home
   Assistant adopting that bridge on 2026-08-27 and abandoning it ten days later as *corroborating evidence
   against* the bridge, one of two such migrations it cites, not a step this port follows. There is no
   bridge stage here at all - see "Bridge-vs-native is decided, not open" below. "Genuine second rendering
   engine," not a migration crutch, has a document-wide consequence not yet drawn out everywhere: whatever
   each item below ports to MapLibre, the pre-existing Leaflet implementation needs to keep working for the
   ~4.27% that stays on Leaflet, not get deleted once its MapLibre replacement lands. Item 4
   (`map-export.ts`) says this explicitly now; items 3 (`map-clusters.ts`), 6 (`leaflet-rotate`), and 7
   (`leaflet-draw`/Terra Draw) do not yet, checked on reassessment - and each may resolve differently, not
   uniformly "keep it forever": item 3's existing Leaflet clustering is this app's own code, cheap to keep
   running; item 6's `leaflet-rotate` is the unmaintained GPL-3.0 dependency item 6 itself already flagged
   as undesirable, so a degraded (no-rotate) experience for that 4.27% may be the right call there instead
   of keeping it forever - though that tension is real, not dismissed: `D12`'s "genuine second rendering
   engine," rejecting REData's own staff-only dashboard's plain "unsupported browser" message, is a
   standard stated about the core mapping experience, not proven to extend to every individual tool at
   full parity - but nothing in `D12` or this document actually says so either way. Both open, not
   decided here.
3. **`map-clusters.ts` is a rebuild, not a port.** It uses an `iconCreateFunction` returning HTML,
   `spiderfyOnMaxZoom`, `animate: true`, and a `maxClusterRadius` that is a function of zoom. MapLibre's
   native clustering has none of those: a scalar radius, no animation, no spiderfy, no HTML icons. There
   is no drop-in equivalent to port. Not the only clustering consumer, found while scanning the newly
   discovered template call sites: `memories/index.html`'s inline script builds its own
   `L.markerClusterGroup({ chunkedLoading: true })` for trip/visit/photo layers, independent of
   `map-clusters.ts` (no shared import, no `iconCreateFunction`/`spiderfyOnMaxZoom`/`animate`). This one
   only uses the plain default marker/radius behavior MapLibre's native clustering already offers, so it
   may be a genuine port, not a rebuild - unverified, not yet compared against MapLibre's actual default
   cluster icon rendering. `map-clusters.ts`'s `createPinClusterGroup()` is the established shared
   pattern, not just an available one: `map-annotations.ts` already imports it, and `map-page.ts`'s own
   hand-rolled duplicate (a second, driftable copy of the same badge logic - `.pin-cluster--{s,m,l}`
   sizing kept in lockstep by hand in two places) was closed out as `P92` on 2026-09-17 specifically
   because nothing enforced that agreement. `memories/index.html` is the one remaining hand-rolled
   holdout this scan has found; whoever converts it should treat it as the same class of pre-existing
   drift `P92` fixed, not just a MapLibre-porting question.
4. **`map-export.ts` needs a genuinely separate offscreen MapLibre instance**, not a mode switch on the
   live map. It currently rasterizes by reading a private `_tileZoom` and calling `getTileUrl()` per
   tile - both read directly from `map-export.ts`'s own source, not assumed. MapLibre's equivalent needs
   `preserveDrawingBuffer`, confirmed as a real `canvasContextAttributes` option (defaulting `false`) in
   the pinned `maplibre-gl@5.24.0` bundle itself, not taken from MapLibre's public docs; that flag's
   per-frame cost must never touch the interactive map. Not addressed anywhere yet, caught cross-checking
   against item 2, tightened on reassessment - the coupling isn't simply "needs WebGL2 too," it's what
   the module's own doc comment says it does: "rasterizes the CURRENTLY VISIBLE view of a Leaflet map,"
   reading the interactive map's own live state (`layers.baseKey()`, drawn markup) rather than building an
   independent view from scratch, so a MapLibre replacement needs that same live state and is coupled to
   whichever engine actually holds it. `supportsWebGL2()` is a plain per-browser capability check, not a
   page-wide cached decision, so calling it again for an offscreen export instance returns the same answer
   the interactive map's own call already got - there is no scenario in this plan, as currently described,
   where WebGL2 is available but the interactive map ends up on Leaflet anyway; that would require some
   other, currently-undescribed fallback trigger. For the ~4.27% of traffic item 2 keeps on Leaflet (WebGL2
   genuinely unavailable), `map-export.ts` needs its current `getTileUrl()`-per-tile rasterization kept as
   a permanent second path, not a migration-period stopgap that gets deleted once the MapLibre one exists.
5. **`map-image-overlays.ts`'s hand-rolled homography (Gaussian elimination) is deleted outright**, in
   favor of MapLibre's native four-corner `image` source. This is a deletion, not a port - checked
   directly on reassessment, both halves: the file's own `solve8()` ("Solve an 8x8 linear system by
   Gaussian elimination with partial pivoting") feeds `matrix3dForCorners()` to warp a plain `<img>` via
   CSS `transform: matrix3d(...)`, using `map.latLngToLayerPoint()`/`map.containerPointToLatLng()` for
   coordinate conversion - no `L.ImageOverlay` plugin involved at all; and the pinned style-spec types
   confirm MapLibre's `ImageSourceSpecification` really is `{type: "image", url, coordinates: [4x
   [lng,lat]]}`, a genuine native four-corner replacement, not an assumed one.
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
7. **`Leaflet.draw` becomes Terra Draw - a bigger item than this one line implied, checked on
   reassessment.** `leaflet-draw` is a real dependency in four places, not incidentally: the boundary
   polygon editor in `ts/entries/map-annotations.ts` (create/edit/delete, plus a custom right-click
   delete-during-edit wired via `attachEditRightClickDelete`), the single-polygon area-guess tool in
   `ts/entries/spotguessr.ts`, and two Django templates with their own inline draw controls -
   `pin_lists/detail.html` (a pin list's boundary) and `pin_lists/_saved_filter_dialog_scripts.html` (a
   saved filter's include/exclude regions). Two behaviors any port must preserve, not just "swap the
   drawing library," because both were real, fixed production bugs: (1) `leaflet-draw`'s own remove tool
   only *stages* a deletion, reverted by starting any other draw/edit tool - `P27` (title: "Saved-filter
   regions use leaflet-draw's transactional remove tool, so deleted polygons resurrect on the next
   draw," resolved 2026-09-14; its own body confirms "the pin-list boundary map used the same tool") -
   so **all four** integrations build their draw control with `edit.remove: false` - not just the two
   templates, checked fresh on reassessment after an earlier pass here undercounted this too - but land on
   three different replacements, not one shared one: the two templates use
   `ts/shared/region-delete.ts`'s `ImmediateDeleteMode` (click a region to delete it at once), guarded by
   `test_region_delete_is_immediate.py` scanning both templates' source for a dropped `remove: false`;
   `map-annotations.ts` has its own right-click "Delete boundary" context-menu item with a confirm dialog
   before `removeLayer`; `spotguessr.ts` has no delete affordance at all, because `setAreaGeometry` calls
   `clearLayers()` before adding each newly-drawn polygon, so there is never more than one to delete -
   the bug class doesn't apply there, not because a workaround was built. A port needs to preserve
   whichever of these three patterns each map actually uses, not assume `region-delete.ts` covers all
   four. (2) `L.geoJSON` collapses a
   stored `MultiPolygon` into one Leaflet layer, so deleting or editing one part acted on the whole
   region - `P120` (resolved 2026-09-14) - fixed by `region-delete.ts`'s `polygonParts()`, which splits
   any stored geometry into one polygon per part before it loads. Checked on reassessment, against Terra
   Draw's own guides (`2.STORE.md`, `4.MODES.md`, `6.EVENTS.md` at `JamesLMilner/terra-draw`) and web
   search, not its source - it is not an installed dependency here to read directly. The `MultiPolygon`
   half resolves cleanly: Terra Draw's `addFeatures` only accepts `Point`/`LineString`/`Polygon` -
   `2.STORE.md` states multi-geometry types "are not supported" and must be split before adding, the
   same shape `polygonParts()` already produces, so that helper (or its logic) carries over rather than
   needing a replacement. The immediate-delete half is still open, not resolved: Terra Draw ships
   undo/redo covering create/update/delete (`6.EVENTS.md`'s delete event plus a documented
   `Ctrl+Z`/`Ctrl+Shift+Z` stack, on by default per secondary sources - not confirmed against a primary
   API reference), which is a materially different mechanism from `leaflet-draw`'s bug - an explicit,
   opt-in undo action rather than an incidental revert triggered by switching tools - but whether
   starting another draw/edit mode silently discards a pending delete the way `leaflet-draw`'s did is
   not documented anywhere checked. Still flagged as open, now for a narrower, specific question rather
   than the whole behavior.
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
   scope for a bug fix. Still true and unfixed: what this item does not cover is four raw-innerHTML-swap
   call sites, not the "couple" `_initThumbs`'s own code comment estimates - reverified directly against
   `messages/index.html` (all three of its `#dm-thread-pane` fetch fallbacks: the plain-POST send
   handler, `sendStagedShare`, and `sendStagedGroupShare`) and `pin_share_dialog.html`
   (`_pinShareNewMap`'s `#pin-share-map-grid` refresh). No htmx event fires for a raw `innerHTML =`
   assignment, so the delegated listener cannot reach whatever thumbnails the pane held before the
   swap; `#dm-thread-pane` is shared between 1:1 and group threads, so even `sendStagedGroupShare` -
   whose own new content never renders a thumbnail, since group message partials skip
   `_map_view_preview.html` - can still discard a previous 1:1 thread's already-initialized ones.
   `_expandCommentMap`'s own stale-cache check is the one path that already handles this gap, and only
   for the dialog viewer map's own cache, using the same idempotent disposal helper. A leaked WebGL
   context (after the port lands) would have been a different order of problem than leaked Leaflet DOM,
   since Chrome caps a page at 16 contexts total - moot now that the underlying leak is closed regardless
   of which engine renders the map.
9. **The CSP shift raster tiles need.** REData's own two internal maps, already converted
   (`feat/scout-campaign`, per `T8`), found that MapLibre fetches tiles via `fetch()`/XHR - governed by
   `connect-src` - where Leaflet loads them as `<img>`, governed by `img-src`. Checked directly against
   this app's own pinned `maplibre-gl@5.24.0` bundle, not just REData's framing: the mechanism is
   specifically `XMLHttpRequest` (`responseType: "arraybuffer"`, then `createImageBitmap()`d), not
   `fetch()` - same `connect-src`-governed CSP category either way (see `D17`), so REData's conclusion
   holds, just via one specific API rather than either-of-two. Any CSP that only allows the vendor/REData
   origins under `img-src` needs the same origins added under `connect-src` before a converted map can
   load a single tile.
10. **A hand-rolled `IControl` replaces `L.control.layers`.** MapLibre has no built-in layer-toggle
    control equivalent; REData's own dashboard conversion is a working reference for the shape of one -
    checked directly against `../REData`'s actual source this session, not assumed to exist: its
    `location-explorer-map.ts` (`entries-classic/`) has a complete `LayerToggleControl` class (`onAdd`,
    `onRemove`, registered via `map.addControl(new LayerToggleControl(map, groupLayers), "top-right")`),
    whose own doc comment calls itself "the MapLibre-native replacement for Leaflet's `L.Control.Layers`,
    which has no MapLibre equivalent at all" - independent confirmation of this item's own framing, not
    just this document's assertion. Its shape: one checkbox per layer group plus bulk "Select
    all"/"Deselect all" buttons, driven by `map.setLayoutProperty(layerId, "visibility", ...)` rather than
    swapping layers in and out of the map. A real port here still needs this repo's own layer set worked
    out against that shape, not a copy - not attempted this session.

## The first real conversion: `_renderMapThumb` is done (2026-09-19)

`comment-map.js`'s `_renderMapThumb` (the `.comment-map-thumb` preview, also reused by the DM
composer's attach-preview chip) is converted, both engines live on `release/v_0_8_0`, not just
scoped - see "What was actually built" below. The reasoning for picking it as the pilot (zero
interactivity, no layer-switcher/draggable-marker/context-menu porting problem, a small
already-hardened blast radius after this session's own leak-fix commits `6b117c695`/`504e145d4`)
held up. One real gap in that reasoning was found and closed while building this, not before:
`_renderMapThumb` also renders arbitrary stored `ShapeSpec` markup (`markup-engine.ts`'s
`renderShape`) onto the map, not just a base tile layer - `L.polyline`/`L.marker`/`L.circle`/
`L.polygon`/`L.divIcon` calls with no MapLibre equivalent, for a feature ("Attach a Map") where a
comment/DM's attached map having drawn shapes is the common case, not the exception. Skipping that
would have shipped a MapLibre path that only actually activated for markup-free maps. This item
originally scoped only the tile-layer swap; the markup port turned out to be the larger half of the
actual work, not a footnote to it.

The other 24 `L.map()` call sites are not yet converted or individually re-checked against this
pilot's specific findings (most are one-off template maps - settings preview, safety check-in, trip
detail, and so on - simple in a different way than `_renderMapThumb`: single-purpose and
low-traffic, but not necessarily non-interactive). The two next-simplest-looking candidates checked
before this pilot both turned out to have their own porting problem the thumbnail path doesn't:
`album-map.ts` (`initAlbumMap`) wires a base-layer switcher control, draggable photo markers with
position-save-on-drop, and a context menu; `_photo_lightbox.html`'s map wires a draggable marker
with a `dragend` handler calling a server-side reposition endpoint. Whoever picks the next call site
should check it for its own equivalent of `_renderMapThumb`'s markup-rendering gap, not assume
"static tile layer" covers everything a candidate map draws.

### What was actually built

- **`ts/shared/maplibre-markup.ts`** (new) - the MapLibre-native counterpart to `markup-engine.ts`'s
  `renderShape`, covering all seven `ShapeSpec` types (`line`, `arrow`, `circle`, `rect`, `polygon`,
  `text`, `pin`) as MapLibre GeoJSON sources/layers (`fill`+`line` layer pairs for filled shapes) and
  `maplibregl.Marker`s (arrow/text/pin), reusing `markup-engine.ts`'s own `arrowheadSvg`/
  `textLabelHtml`/`bearing`/`arrowheadSize` (now exported) rather than re-deriving them, so an
  arrow or text label looks identical on either engine. `circle` has one deliberate, known visual
  approximation, documented in the module's own header comment: Leaflet's `L.circle` recomputes a
  screen-space ellipse every frame from the current projection, which MapLibre's layer model has no
  equivalent of, so this module instead builds a fixed 64-point polygon approximating the true
  geodesic circle (haversine, `R = 6371000` - matching `L.CRS.Earth.R`, so the radius itself agrees
  with Leaflet's own `distanceTo`). Visually indistinguishable at the radii/zooms this app's shapes
  are actually drawn at; not the same algorithm, and the gap grows at extreme radii or high latitude.
  Not covered by an automated test this session (no browser-DOM MapLibre test harness exists yet in
  this codebase to assert against, the same gap item 8's own manual-harness note already flagged for
  Leaflet) - verified instead by a real-browser Playwright check (screenshot + structural assertions:
  correct engine picked, correct marker/layer counts, zero console errors) against `/dashboard/map/`,
  covering both the MapLibre path and the forced-no-WebGL2 Leaflet fallback in the same run.
- **`ts/shared/map-layers.ts`**: new `rasterSourceFor(kind)`, the MapLibre-side counterpart to the
  existing `tileLayer(kind)` - same `TILE_DEFS` resolution order, returns the `RasterSourceInput`
  shape `buildRasterStyle` needs instead of an `L.TileLayer`.
  `webgl-support.ts`/`maplibre-raster-style.ts` each gained an `installGlobalX` publishing their
  existing pure functions onto `window` (`WebGLSupport`, `MaplibreRasterStyle`), the same pattern
  `MapLayers`/`MapExport` already use - all three wired into `entries-classic/core.ts`.
- **`comment-map.js`**: `_renderMapThumb` now branches - `typeof maplibregl !== "undefined" &&
  window.WebGLSupport.supportsWebGL2()` picks a new `_renderMapThumbMaplibre` path (constructs the
  map from `buildRasterStyle`, tags `el._ulMaplibreMap` immediately after construction for the same
  leak-prevention reason item 8's Leaflet tagging does, adds the borders overlay and markup group
  once `'load'` fires, since MapLibre requires the style to be ready before `addSource`/`addLayer`);
  everything else falls through to the original, unmodified Leaflet path. `_disposeMapOn` now checks
  both `_ulLeafletMap` and `_ulMaplibreMap` tags independently. A MapLibre-side `_fitMaplibreToMarkup`/
  `_computeMarkupLngLatBounds` mirror the Leaflet pair without depending on `L` (the MapLibre path
  must work even where Leaflet happens not to be loaded on a page), with one deliberate
  simplification: it always fits to markup bounds when markup exists rather than first checking
  whether the saved view already covers it, so it can zoom out slightly more than strictly necessary
  in the case the Leaflet path special-cases - not an attempt at pixel parity.
- **CSP**: `connect-src` gained the tile-vendor origins (`*.basemaps.cartocdn.com`,
  `*.tile.opentopomap.org`, `server.arcgisonline.com`/`services.arcgisonline.com`) item 9 above
  said MapLibre's XHR-based tile fetches need, mirroring `img-src`'s existing entries for the same
  vendors.
- **Templates**: `{% vendor_asset "maplibregl_css/js" %}` added alongside every existing
  `leaflet_css/js` pair (22 templates) - the same set of pages, not a broader rollout, so a page that
  never loaded Leaflet still never loads MapLibre either.
- **`maplibre-gl@5.24.0`** added as a `devDependency` (`bun add -D`, matching the version already
  vendor-pinned in `vendor_assets.py`) for its own shipped TypeScript types only - every reference to
  it in this codebase's `.ts` files is `import type`, so nothing bundles it; the runtime library is
  still loaded exclusively via the CDN `<script>` tag, exactly like Leaflet's own `declare const L`
  pattern. `node_modules/` write permission, blocked as of 2026-09-05, was re-checked this session and
  is open again.

Verified in a real browser (`bin/sync_app.sh --frontend` into `development_main`, Playwright against
`/dashboard/map/` with a synthetic `_renderMapThumb` call covering all seven shape types): the
MapLibre path renders tiles, the borders overlay, all seven shape types, and the reference marker
(screenshot-confirmed, not just structurally asserted); forcing `supportsWebGL2()` to `false` in the
same run confirms the Leaflet path still renders correctly, unregressed. Zero console errors either
way. `bun run typecheck`, `bun run test:ts` (1015 pass), and the frontend build all clean.

What a real next conversion batch still needs: pick another of the 24 remaining call sites, check it
against this pilot's own markup-rendering lesson specifically, and budget for whichever of the two
still-open items above (Terra Draw's immediate-delete question, item 7; the clustering rebuild vs.
port question, item 3) it actually touches.

## What is explicitly out of scope here

- **REData's two internal dashboard maps** (boundary map, Location Explorer) are already converted, on
  their side - not this repo's work, cited here only as a working reference for the WebGL2-detection and
  `IControl` patterns. Checked directly against `../REData`'s actual git history on reassessment, twice
  now: the conversion commit is `7727ba36` ("feat(dashboard): migrate boundary map and Location Explorer
  to MapLibre GL JS," 2026-09-17) - confirmed genuinely the one that introduced `LayerToggleControl`, since
  `location-explorer-map.ts` had zero `maplibregl`/`LayerToggleControl` references the commit before it,
  and roughly doubled in size (182 → 318 lines) in this one. First reassessment pass called it "on
  `feat/scout-campaign`, not yet merged" - re-checked on a second pass and that part was wrong:
  `git merge-base --is-ancestor 7727ba36 main` confirms it landed in REData's actual `main` via
  `821941d7` ("Merge pull request #65 from avranu/feat/scout-campaign," 2026-09-18); `feat/scout-campaign`
  is a long-lived branch that kept accumulating unrelated coverage-scout data commits after that merge,
  which is what its current "10 ahead of main" count was actually counting, not unmerged map-conversion
  work. `main`'s copy of `location-explorer-map.ts` is byte-identical to the feature branch's (`git diff`
  is empty) - REData's conversion is live on their main line, not sitting on a feature branch.
- **The Flutter app** (`PL12` item 8) is deliberately parked on raster until this item ships, by REData's
  own acceptance line. Nothing is needed from Flutter now.
- **REData's infrastructure side** (`PL12` items 1-5: DEM chain, Valhalla, PMTiles mirrors, Martin) is
  independent - a self-hosted basemap archive existing and a client that can draw vector tiles are two
  separate prerequisites for the same eventual outcome, not a dependency chain in either direction, per
  `T8` §3.
- **The tile-catalogue wiring** (`T8` §1) - done, see `N25`.

## Bridge-vs-native is decided, not open (corrected on reassessment)

This document previously listed bridge-then-native sequencing as undecided, under "Not measured, and not
decided" below. That was stale, and self-contradicted this same document: items 2 and 6 above already cite
`D12` as binding for this repo's plan, not just REData's own two maps. Checked directly against `D12`'s
actual text (`status: accepted`, decided 2026-09-17), not the summary: its "Costs accepted" section prices
out `map-clusters.ts`, `map-export.ts`, `comment-map.js:721`, and `leaflet-rotate` **by name** - all
UrbanLens files, not REData's - and its "Sequencing" bullet reads "Convert REData's two maps before
UrbanLens's 27." A decision that costs out this repo's own files by path and orders this repo's own
conversion relative to REData's is a decision that governs this repo's plan, not merely an
REData-internal precedent cited for context. `D12`'s verdict: **native, not bridged** -
`maplibre-gl-leaflet` is Hosted-tier with no active maintainer, has "no rotation / bearing / pitch
support" by its own README (the same gap item 6 above independently found blocking the floorplan rotate
tool), throttles updates to ~31fps by construction, and two real migrations (OpenStreetMap, Home
Assistant) both dropped it within weeks of shipping vector tiles - all `D12`'s own findings, re-read from
its text this session, not independently re-verified here against the bridge's own README/repo or either
migration's actual history. REData converts its two maps first, as the pilot; this repo's port follows,
native from the start, no bridge stage of its own.

## Not measured, and not decided

- No estimate of engineering time is recorded here; REData's own framing ("the real body of this item")
  and the item count above are the only sizing information available.
- Whether MapLibre's native clustering can be made to approximate this codebase's spiderfy/animate/HTML-icon
  behavior closely enough, or whether the rebuild in item 3 above ships with visibly different clustering
  behavior, is untested in either direction - REData's own `PL12` records the same gap on its side ("no
  benchmark exists in either direction" at this repository's pin-count ceiling).
- No target date, owner, or priority. See the top of this document.
