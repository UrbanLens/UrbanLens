# D18 — Proxied basemap tiles are cached `public`, not `private`, because the sign-in check gates upstream quota, not the bytes

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D18` · `status: accepted` · `updated: 2026-09-23`

## Decision

This deployment's basemap tile proxy answers `Cache-Control: public, max-age=<ttl>, immutable`,
not `private`. Made in `a4229e994` ("perf(map): let a CDN hold the proxied basemap tiles"); the
header is built by `_keep_for()` in
`src/urbanlens/dashboard/controllers/basemap_tiles.py:163-184`, called from every tile/404 response
path in that file.

## Rationale

The tile proxy's sign-in check exists to control who spends the deployment's upstream tile-vendor
quota, not because the response bytes differ per viewer. Two facts make `public` correct rather
than merely convenient:

- The cache key is layer + z/x/y only — `basemap_tile_cache_key()` in
  `src/urbanlens/dashboard/services/map/tile_cache_keys.py:17` takes no viewer identity.
- `catalogue_for_viewer()` in
  `src/urbanlens/dashboard/services/map/basemap_catalogue.py:314` distinguishes only
  `authenticated: bool` — signed-in vs. signed-out — so every signed-in viewer is served the same
  tile catalogue and, in turn, identical tile bytes for a given coordinate.

`private` would have kept every tile out of any CDN or shared cache in front of the proxy, forcing
each viewer's browser to re-fetch (and this deployment to re-spend upstream quota on) a tile every
other viewer already has.

TTLs (`src/urbanlens/dashboard/controllers/basemap_tiles.py:49-84`): 7 days
(`_TILE_CACHE_TTL = 7 * 86400`) for an ordinary tile, 365 days (`_UNDERLAY_CACHE_TTL`) for the
world-level underlay set (`z <= _UNDERLAY_MAX_ZOOM`, i.e. z≤2), and `min(ttl, _TILE_CACHE_TTL)` for
a cached 404.

## Accepted trade-off

Documented in `src/urbanlens/dashboard/services/map/tile_authorisation.py`: a tile already held by
a browser or a CDN in front of the proxy can be viewed without the sign-in check re-running, and a
revoked session's already-cached tiles remain servable for up to the TTL above (a week for most
layers, up to a year for world-level underlay tiles). Accepted because the tiles are public map
imagery — the check is a quota gate, not a confidentiality boundary, per the Rationale above.

## What would reopen this

Any per-viewer or per-plan tile layer — a private overlay, or paid-tier imagery whose bytes differ
by viewer or entitlement — would break the premise that identical (layer, z, x, y) means identical
bytes for every signed-in viewer. Such a layer's responses would need to be `private`, or the cache
key would need to include viewer/plan identity.

## Enforcement

- `tests/integration/specs/ui/basemap-tile-cache.spec.ts`
- `src/urbanlens/dashboard/tests/hypothesis/test_basemap_tile_shared_cache.py`

Not re-run this session; existence and path confirmed by `ls`, not by executing either suite.
