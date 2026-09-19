# Reply to REData's `T8`: the catalogue is wired, called on every page, and serving production tiles

- **Status: §1 ANSWERED, §2 IN PROGRESS, 2026-09-19.** `T8` §1 (call the dormant tile catalogue,
  branch on `source_type`, stop hardcoding vendor URLs) is implemented and verified in a browser
  against REData's production instance. `T8` §2 (the MapLibre GL JS migration) is under way, tracked
  as `PL8` (`docs/designs/leaflet-to-maplibre-migration.md`).
- **Direction: inbound.** `../REData`'s `docs/urbanlens-handoff.md` (their `T8`) to this repo.
- `id: N25` · `status: current`

Written 2026-09-19 against `release/v_0_8_0` and `../REData`'s `main` (`ab2a5b6a`). Nothing in
`../REData` was edited to produce this. This file was rewritten on 2026-09-19 to state current
facts directly; it previously carried five stacked "Correction, found on..." sections, which
`docs/README.md`'s house style forbids. What each of those corrected is folded in below.

> Read `T8` from `main`, not from the working tree. `../REData`'s primary checkout on this host
> sits on `feat/scout-campaign`, whose `docs/urbanlens-handoff.md` is two revisions behind. Use
> `git show origin/main:docs/urbanlens-handoff.md`.

## The one thing `T8` gets wrong today: the contract *is* deployed

`T8` §0 says, in the present tense, "**It is on `main` and not yet on damballa**... REData's deploy
is deliberately held until UrbanLens 0.8.0 ships," and offers a curl check to confirm. Run on
2026-09-19 against `https://redata.urbanlens.org`, that check comes back **positive**:

```
GET /api/v1/tiles/sources/  →  200, five entries, every one carrying "source_type": "raster"
   street · terrain · satellite · dark · borders, each with url_template and requires_auth: true
```

So the `D11` contract is live in production now, and `T8` §0 is stale. Everything else §0 predicts
holds exactly: **all five layers are raster, none carries a `style_url`**, which matches its note
that vector waits on a `basemap-tiles-s3` Secret and a Garage bucket that only Jess can create. This
repo's client was therefore built for both branches and exercised against the raster one.

## What `T8`'s other claims turned out to be

| `T8`'s claim | Verdict |
|---|---|
| `registerRedataLayers()` existed, was dead code | confirmed, and worse than stated - see below |
| REData's `source_type` contract is real but not deployed to production | **superseded** - it is deployed, measured above |
| `comment-map.js:721` leaks a Leaflet map per HTMX-swapped preview | was true; **fixed** since, in `PL8` item 8 (`6b117c695`, `504e145d4`) - the instance is tagged onto its container and disposed by a delegated `htmx:beforeCleanupElement` listener |
| `leaflet-rotate@0.2.8` (GPL-3.0) is used only by `floorplan-editor.ts` | confirmed as to *where*; **refuted as to what it does** - see below |
| `map-clusters.ts`, `map-export.ts`, `map-image-overlays.ts` match the description | confirmed by reading each; per-file detail in `PL8` |
| no MapLibre GL JS dependency exists | was true; it is a dependency now, and two maps run on it |
| "27 Leaflet maps" | **25** by direct count - close, not a refutation; see below |

**`leaflet-rotate` is not delete-outright.** `T8` §2 and `D12`'s own "Costs accepted" section both
say its "only capability MapLibre lacks is device-compass rotation, which nothing in this codebase
uses." That is false here: `floorplan-editor.ts` wires it into a live floor-plan rotate tool with a
toolbar button, a `t` shortcut, and undo/checkpoint integration (`floorplan-editor.ts:172-213`,
`2351-2456`). The work is a port to MapLibre's native `bearing`/`setBearing()`, not a deletion.
**This applies to `D12` directly, not just to `T8`** - `D12` is `status: accepted`, so REData's team
may be planning against the "deleted outright" framing independently.

**The map count is 25 `L.map(` call sites across 20 files** - 12 across 8 `.ts`/`.js` files plus 13
across 12 Django templates. Two earlier counts in this file (15, then 12) were both wrong and both
reported as "under half of 27": the grep was `--include='*.ts' --include='*.js'`, which structurally
cannot see the inline `<script>` blocks where 13 real maps live, and it was unanchored, so
`parseHTML.map(` matched as `L.map(`. Use `grep -rn '\bL\.map(' --include='*.ts' --include='*.js'
--include='*.html'`. The remaining gap of 2 against REData's 27 is unexplained rather than an error
on either side; `T8` does not describe its counting method.

## What is implemented: `T8` §1

`T8`'s ask was "call `registerRedataLayers()`, branch on `source_type`, and stop hardcoding vendor
tile URLs where the catalogue already names them." All three are done, but the first one needed more
than adding calls.

**The catalogue was dormant in a way that adding callers could not fix.** `registerRedataLayers()`
fetches over HTTP. A map that constructs its tile layers and *then* learns which tiles this
deployment serves has already requested vendor tiles - and a map page URL can encode a pin's
coordinates, which is precisely what this site's blanket `Referrer-Policy: no-referrer` and the
whole proxy exist to keep from third parties. Six TypeScript entry modules did call it; five of
those called it without awaiting and raced their own map construction. Nine template maps never
called it at all.

So the catalogue is now **embedded in the document** rather than fetched: `{% basemap_tile_catalogue %}`
(`templatetags/map_components.py`) writes it into `themes/base.html` ahead of `core.js`, and
`map-layers.ts` reads it synchronously the first time anything asks for a tile source. No await, no
round trip, no window in which a vendor gets the coordinates. `registerRedataLayers()` remains as
the fallback for a client rendered outside that base template, and an embed that is present but
empty is authoritative rather than a reason to ask again.

`services/map/basemap_catalogue.py` is the single builder both the embed and
`BasemapTileCatalogueView` use. A signed-out viewer is offered only keyless entries: the raster
proxy is `LoginRequiredMixin`, so handing a public share page a proxy URL would swap a working
vendor layer for a grid of 404s.

**A real bug in the dead code, fixed before it ever ran.** REData's id for the topographic layer is
`terrain`; every call site here looks up `tileLayer("topographic")`. The old code wrote straight
into `TILE_DEFS[layer.id]`, so a REData terrain override would silently never have taken effect even
once wired up. `REDATA_ID_ALIASES` maps it across.

**Vector entries are registered, not dropped.** They land in `VECTOR_STYLE_DEFS` keyed the same way,
because `TILE_DEFS` describes a raster XYZ template and Leaflet cannot render a style document.
Nothing draws one yet. Leaflet never will - it has no vector renderer, so on a deployment serving a
vector layer the Leaflet fallback engine draws the built-in vendor raster for that layer while
MapLibre draws the self-hosted style. That divergence is inherent to `D12`'s two-engine decision.

**Verified in a browser, not from a source read**, on 2026-09-19 against production REData, with
`UL_ALLOW_OUTBOUND_APIS=true` set on the dev slot's web process for the duration and reverted after:

- `/dashboard/map/` (Leaflet) and `/dashboard/memories/` (a template map that never called the
  catalogue before): every base tile request went to `/dashboard/map/basemap-tiles/…`, **zero
  requests to cartocdn.com, opentopomap.org or arcgisonline.com**, no console errors.
- The MapLibre engine, driven through the same `MapLayers.create()`: all five sources
  (`ul-layers-street|dark|topographic|satellite|borders`) resolved to the same proxy templates,
  zero vendor hosts, zero errors.

## What this cost, and the bound added because of it

Turning the catalogue on converts ~30 static CDN tile requests per map page into ~30 authenticated
Django requests. Against production REData each uncached one takes **~1.5s**, and that is REData's
per-request key-verification cost rather than anything to do with tiles - the trivial catalogue
endpoint takes the same 1.5s, an invalid key is rejected in 55ms. Full measurement and cause in
**`P131`**; the fix is one hasher change in REData.

Until that lands, `basemap_tile_upstream_concurrency` (default 2) bounds how many tiles one web
process fetches upstream at once, answering an uncached 503 immediately over the cap rather than
blocking a request thread. Unbounded, one cold map load holds every thread in the process and queues
the rest of the site behind it - on `runserver`, which spawns threads without limit, a single map
load exhausted `ul_web`'s 54-connection limit outright (`FATAL: too many connections`). It is
containment, not a fix: a cold viewport paints partially and fills in on the next pan. Once REData
is quick the cap should essentially never be reached, and is worth revisiting then.

## What is in progress: `T8` §2

The MapLibre migration is under way as `PL8`. Two maps run on MapLibre GL JS v5 today
(`comment-map.js`'s `_renderMapThumb`, and the shared pin-share page), the shared layers engine runs
on both engines behind one `MapLayers.create()` call, and Leaflet is retained as the WebGL2 fallback
per `D12`. `PL8` carries the punch list.

## For whoever reads this next

- **`T8` §0 needs rewriting on REData's side** - the deploy it says is held has happened. Its vector
  paragraph is still accurate.
- **`P131` is the highest-value thing REData could fix for UrbanLens right now.** It is not specific
  to tiles; every one of the ~15 REData integrations here pays ~1.45s per call.
- Nothing here has been run against a live vector entry, because none exists to run against. The
  client path for one is written and unit-tested, not browser-verified.
- REData's `D11`/`D12` are on their side; this repo's own `D11`/`D12` are unrelated decisions that
  happen to share numbers.
- `D12`'s `leaflet-draw` → Terra Draw line is thin on both sides. `leaflet-draw` is used in four
  places here, two of which exist to work around a quirk that caused a real, fixed bug (`P27`: a
  staged deletion reverted itself on the next draw/edit). Whatever replaces it needs an equivalent
  to that immediate-delete behavior. Not verified whether Terra Draw has one.
