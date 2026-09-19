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
   caniuse put that at 95.73% global support in Aug 2026, so ~4.27% of traffic needs it. Home Assistant is
   the cited precedent for exactly that permanent dual-engine pattern - not, as an earlier version of this
   item implied, for temporarily adopting the `maplibre-gl-leaflet` *bridge*: `D12`'s own text cites Home
   Assistant adopting that bridge on 2026-08-27 and abandoning it ten days later as *corroborating evidence
   against* the bridge, one of two such migrations it cites, not a step this port follows. There is no
   bridge stage here at all - see "Bridge-vs-native is decided, not open" below.
3. **`map-clusters.ts` is a rebuild, not a port.** It uses an `iconCreateFunction` returning HTML,
   `spiderfyOnMaxZoom`, `animate: true`, and a `maxClusterRadius` that is a function of zoom. MapLibre's
   native clustering has none of those: a scalar radius, no animation, no spiderfy, no HTML icons. There
   is no drop-in equivalent to port. Not the only clustering consumer, found while scanning the newly
   discovered template call sites: `memories/index.html`'s inline script builds its own
   `L.markerClusterGroup({ chunkedLoading: true })` for trip/visit/photo layers, independent of
   `map-clusters.ts` (no shared import, no `iconCreateFunction`/`spiderfyOnMaxZoom`/`animate`). This one
   only uses the plain default marker/radius behavior MapLibre's native clustering already offers, so it
   may be a genuine port, not a rebuild - unverified, not yet compared against MapLibre's actual default
   cluster icon rendering.
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
   any stored geometry into one polygon per part before it loads. Whether Terra Draw's own API can
   express an equivalent "delete commits immediately, no staging" mode and one-layer-per-polygon loading
   is not verified in this session - flagged as open, not assumed either way.
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
   for the dialog viewer map's own cache, using the same idempotent disposal helper. A leaked WebGL context (after the port lands) would have been
   a different order of problem than leaked Leaflet DOM, since Chrome caps a page at 16 contexts total
   - moot now that the underlying leak is closed regardless of which engine renders the map.
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
    control equivalent; REData's own dashboard conversion is a working reference for the shape of one.

## Where a first real conversion should start

Not code yet - scoped here because the foundational pieces (vendor asset pin, WebGL2 detection,
`buildRasterStyle`) now exist unwired and the next concrete step is choosing where they land first, not
building more scaffolding in the abstract. There are 25 distinct `L.map()` call sites across the codebase
(`comment-map.js` alone has three: composer, dialog viewer, thumbnail) - most of them one-off template
maps (settings preview, safety check-in, trip detail, and so on) that are simple in a different way than
`_renderMapThumb`: single-purpose and low-traffic, but not necessarily non-interactive. `_renderMapThumb`
in
`comment-map.js` (the `.comment-map-thumb` preview, also reused by the DM composer's attach-preview chip)
is the strongest pilot candidate among those actually checked - not an exhaustive review of all 25, but
every candidate compared against it so far loses on the same axis:

- **Zero interactivity to preserve.** Its own Leaflet options are `zoomControl: false, dragging: false,
  scrollWheelZoom: false, doubleClickZoom: false, touchZoom: false` - it is a static rendered view, not an
  interactive map. A MapLibre swap here needs no gesture/control porting at all.
- **No layer-switcher UI, no draggable markers, no context menu.** Contrast the two next-simplest-looking
  candidates actually checked: `album-map.ts` (`initAlbumMap`) wires `createMapLayers` (a base-layer
  switcher control), draggable photo markers with position-save-on-drop, hover sync back to a DOM list,
  and a context menu; `_photo_lightbox.html`'s map (also `zoomControl`/`dragging`/`scrollWheelZoom` all
  `false`, so it looked just as promising at a glance) turns out to wire a draggable marker with a
  `dragend` handler that calls a server-side reposition endpoint. Each of those is its own porting problem
  (`IControl`, MapLibre's `Marker` drag API, a live network call mid-drag) this thumbnail path has none
  of. The other 22 call sites are not yet individually checked.
- **Small, already-hardened, already-understood blast radius.** Two commits this session
  (`6b117c695` - the leak/dispose fix and htmx cleanup wiring; `504e145d4` - the throw-before-tag ordering
  fix found on re-review) already put this exact function through adversarial review, so its behavior is
  fresh and verified, not archaeology.
- **Every foundational piece it would need already exists, unwired:** `buildRasterStyle`/
  `toMapLibreTileUrls` (this document, above) for the source, `maplibregl_js`/`maplibregl_css` in
  `vendor_assets.py` for the library itself, `supportsWebGL2` (`webgl-support.ts`) for the
  fallback-to-Leaflet decision D17 requires.

What a real (not scoped-only) first conversion batch still needs, none of which exists yet: the vendor
asset actually referenced by a template (today it's pinned but consumed nowhere), the CSP `connect-src`
change item 9 above already specifies, a `WebGL2 → MapLibre / no-WebGL2 → Leaflet` branch actually wired
into `_renderMapThumb` (not just the pure detector function), and - per this repo's own testing
convention - verification in a real browser, not just `bun test`, since this is a rendering change.

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
Assistant) both dropped it within weeks of shipping vector tiles. REData converts its two maps first, as
the pilot; this repo's port follows, native from the start, no bridge stage of its own.

## Not measured, and not decided

- No estimate of engineering time is recorded here; REData's own framing ("the real body of this item")
  and the item count above are the only sizing information available.
- Whether MapLibre's native clustering can be made to approximate this codebase's spiderfy/animate/HTML-icon
  behavior closely enough, or whether the rebuild in item 3 above ships with visibly different clustering
  behavior, is untested in either direction - REData's own `PL12` records the same gap on its side ("no
  benchmark exists in either direction" at this repository's pin-count ceiling).
- No target date, owner, or priority. See the top of this document.
