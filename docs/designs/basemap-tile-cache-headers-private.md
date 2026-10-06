# D27 — Proxied basemap tiles are kept by the viewer's browser only (`private`), for no longer than the vendor allows

`id: D27` · `status: accepted` · `updated: 2026-10-06` · `supersedes: D18`

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## Decision

The basemap tile proxy answers `Cache-Control: private, max-age=<ttl>, immutable`
(`_keep_for` in `src/urbanlens/dashboard/controllers/basemap_tiles.py`). For a raster layer fetched from a vendor,
`<ttl>` is cut to the vendor's own lifetime (`VendorTiles.browser_max_age`, `_browser_ttl`): Esri sends
`Cache-Control: max-age=86400` (read off World_Imagery and World_Street_Map on 2026-10-06), so every Esri layer
is kept a day by the browser. This deployment's own store still keeps the bytes for a week, and the sixteen world
tiles for a year (`_ttl_for`).

The vector-tile and vector-style proxies this decision also covered were removed on 2026-10-06: the browser now
fetches Protomaps' hosted tiles directly (`frontend/ts/shared/hosted-basemap.ts`), and their caching is
Protomaps' own (`cache-control: public, max-age=14400`, `vary: Origin`).

The response is still marked viewer-independent (`mark_viewer_independent` in `middleware.py`), so the session and
media-cookie layers' `Set-Cookie` and `Vary: Cookie` are stripped: the bytes are identical for every viewer who
passes the gate, and `Vary: Cookie` would make the browser miss on every cookie refresh.

## Why not `public` (D18)

D18 made the same responses `public` so a CDN could hold them. Two things it did not weigh:

- **The endpoint is behind a login.** A shared cache holding a tile answers anyone who asks for the URL.
- **A shared cache's hit says somebody here looked at that coordinate.** The proxy exists so the vendor does not
  learn which places viewers look at (`services/map/basemap_vendors.py`); a probe-able cache of the same URLs gives
  that away to anyone.

And it bought nothing measurable: the `urbanlens.org` zone's only cache ruleset covers `tiles.urbanlens.org`
paths (`../infrastructure/docs/runbooks/cloudflare-cache-rules.md`), and Cloudflare does not cache an
extension-less app path by default, so production's tile responses were never edge-cached. Not verified with a
signed-in request against production.

## What it costs

A tile a browser has not seen is answered by this deployment, from its own cache when anyone has drawn it before
- zero queries since the tallied call ledger (`services/core/call_tally.py`). A tile a browser has seen is not
asked for again for a day.

## What would reopen this

A CDN rule that keys on the session or strips the hit/miss signal, or a measured origin load from tiles that only an
edge cache can carry.

## Enforcement

- `src/urbanlens/dashboard/tests/hypothesis/test_basemap_tile_shared_cache.py`
- `src/urbanlens/dashboard/tests/hypothesis/test_basemap_tile_cost.py` (`test_no_shared_cache_may_keep_a_tile`)
- `tests/integration/specs/ui/basemap-tile-cache.spec.ts` - not run this session.
