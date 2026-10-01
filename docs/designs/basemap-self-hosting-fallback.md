# D17 — Self-hosted instances keep today's free raster vendors as the basemap fallback; the MapLibre migration adds no new third-party dependency for them

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D17` · `status: accepted` · `updated: 2026-09-19`

Written against `PL8` (the deferred Leaflet→MapLibre GL JS migration) and this session's REData
catalogue-wiring work (`N25`). Jess raised the question directly: the REData tile catalogue
(`T8`/`D11`, REData's `../REData`) only benefits the hosted/public instance — self-hosters running
their own UrbanLens have no REData deployment at all — so does the MapLibre port need a *new* free
third-party provider (OSM tiles directly? Protomaps' own hosted API,
[protomaps.com/api](https://protomaps.com/api)? something else) as a fallback basemap source for
them, the way REData currently is one for the hosted instance?

## Today's answer, already true and now tested

Under Leaflet, this is already solved and was not broken by this session's wiring.
`registerRedataLayers()` (`frontend/ts/shared/map-layers.ts`) only **overrides** `TILE_DEFS`
entries it actually receives from REData; an unconfigured or unreachable REData resolves to `[]`
and every built-in entry — `street`/`dark` (CARTO), `topographic` (OpenTopoMap), `satellite`/
`borders` (Esri) — is left exactly as it was before REData existed. `BasemapTileCatalogueView`
answers `{"layers": []}` for an unconfigured deployment
(`test_unconfigured_redata_yields_no_layers`), the identical shape a self-hoster's browser sees.
Added this session: `map-layers.test.ts`'s "every built-in layer still resolves to its free vendor
when REData is unconfigured (self-hosting)" test asserts this directly, covering all five built-in
layers in one place rather than leaving the guarantee implicit in "nothing calls
`registerRedataLayers` badly."

## Why MapLibre raises the question again

Leaflet's `TileLayer` takes a bare XYZ URL template — there was never a "what if there's no style
document" question, because there is no style document. MapLibre GL JS renders a *style
specification* (a JSON document naming sources and layers), and a **vector** source's whole
content is `style_url` — REData-only, per `D11`; a self-hoster has nothing to fetch there at all.
Read naively, that looks like self-hosters need a second free vector-tile provider standing in for
REData once the client stops being able to draw bare XYZ raster templates directly.

**That reading is wrong.** The MapLibre style spec's `"raster"` source type is exactly a bare XYZ
tile-URL template — `{"type": "raster", "tiles": ["https://.../{z}/{x}/{y}.png"], "tileSize": 256}`
— functionally identical to what Leaflet's `TileLayer` already does. Nothing about rendering a
raster layer in MapLibre requires vector data, a hosted style API, or REData.

**Checked, not assumed: CORS is not a blocker.** Unlike Leaflet's `<img>`-tag tile loading, MapLibre
loads raster tiles via `XMLHttpRequest` with `responseType: "arraybuffer"`, decoding the response
through `createImageBitmap` to upload it to a WebGL texture - confirmed by grepping the actual
`maplibre-gl@5.24.0` bundle (the version pinned this session, see `vendor_assets.py`) for its tile
fetch path, not assumed from general library knowledge. Functionally this is the same CORS category as
`fetch()`, not `<img src>`: a cross-origin `XMLHttpRequest`/`fetch()` a server doesn't explicitly permit
fails outright, where an `<img src>` load of the same URL would have succeeded regardless. Verified
2026-09-19 with a direct `curl -H "Origin: https://example.com"` against all four `TILE_DEFS` vendor
endpoints (`basemaps.cartocdn.com`, `tile.opentopomap.org`, `server.arcgisonline.com`,
`services.arcgisonline.com`): every one already answers `Access-Control-Allow-Origin: *`. No vendor
change is required for the client-built raster style to work. (`PL8` item 9's separate CSP note still
applies - `connect-src` governs both `XMLHttpRequest` and `fetch()`, where `img-src` governs `<img>` -
but that's a same-origin-policy header on *this app's* nginx
config, unrelated to the vendor CORS question this paragraph checks.)

## Decision

**No new third-party dependency is added for self-hosters.** The MapLibre port constructs its
style document **client-side**, in this app's own frontend code, rather than always fetching one
from a URL:

- **Self-hosted, no REData configured (the common self-hosting case):** the client builds a
  minimal style JSON in memory wrapping the same built-in CARTO/OpenTopoMap/Esri XYZ endpoints as
  `"raster"` sources. Zero network fetch of any third-party style document, zero new dependency,
  zero new terms-of-service surface to track, identical vendor set to today.
- **Hosted instance, REData configured, raster (today's REData state — every layer, per `T8` §0):**
  same client-built-style pattern, but the raster source's `tiles` array points at this app's own
  `/dashboard/map/basemap-tiles/<layer>/{z}/{x}/{y}/` proxy instead of a vendor directly — REData's
  key still never reaches the browser. Still no external style URL fetched.
- **Hosted instance, REData configured, vector (once REData's self-hosted Protomaps mirror ships —
  blocked on the infrastructure-repo step `T8` §1 already documents, not on anything here):** the
  same layer id upgrades from a raster source to a `style_url`-driven vector source. The client
  fetches REData's own style document at that point, and only then. Self-hosters are unaffected —
  they stay on raster indefinitely unless they stand up their own REData deployment and point it at
  a vector mirror themselves.

## Costs accepted

- A self-hosted instance never gets vector-quality rendering (styled water bodies, label
  placement, client-side hillshading) unless its operator runs their own REData deployment with a
  self-hosted vector mirror configured. They get the exact same raster tiles this project has
  always shipped, now drawn by MapLibre instead of Leaflet — a rendering-engine change, not a
  vendor or quality change, for that population.
- Protomaps' own hosted API was considered and rejected as *this app's* built-in self-hosting
  default: it is a metered product beyond a limited free tier, would add a required external
  account/key and a new terms-of-service surface to every self-hosted instance out of the box, and
  duplicates what the existing keyless CARTO/OpenTopoMap/Esri vendors already provide reliably
  today (the same ones this codebase already depends on and already handles rate-limiting/ToS
  concerns for — see `map-layers.ts`'s `BASE_ERROR_TILE_URL` comment on OSM's own hotlinking
  policy). Nothing stops a self-hoster's *own* REData deployment from pointing at Protomaps, a
  self-built Planetiler archive, or any other vector source later (that is REData's `PL12`
  item 3's decision, made per-deployment) — this app does not need to hardcode a choice on their
  behalf.
- The client-built-style approach means this app's frontend, not REData, owns the fallback vendor
  list and its attribution strings for the raster case — already true today (`TILE_DEFS` in
  `map-layers.ts`) and unchanged by this decision.

This governs the style-construction shape of `PL8`'s MapLibre work; `PL8` is updated to reference
this record rather than leave the self-hosting question open.
