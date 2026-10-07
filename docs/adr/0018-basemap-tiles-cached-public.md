---
status: superseded by ADR-0027
date: 2026-09-23
---

# Proxied basemap tiles are cached `public`

Formerly `D18`. Detail: [`docs/designs/basemap-tile-cache-headers-public.md`](../designs/basemap-tile-cache-headers-public.md).

The basemap tile proxy answered `Cache-Control: public, max-age=<ttl>, immutable`, so a CDN or shared cache could hold tiles. The reasoning was that the sign-in check guards upstream vendor quota, not the bytes: the cache key is layer plus z/x/y only, and every signed-in viewer gets the same catalogue and so the same tile bytes.
