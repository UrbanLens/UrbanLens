---
status: accepted
date: 2026-10-06
---

# Proxied basemap tiles are kept by the viewer's browser only

Formerly `D27`. Supersedes ADR-0018. Detail: [`docs/designs/basemap-tile-cache-headers-private.md`](../designs/basemap-tile-cache-headers-private.md).

The basemap tile proxy answers `Cache-Control: private, max-age=<ttl>, immutable`, with the ttl cut to the vendor's own lifetime (a day for Esri). The deployment's own store still keeps tiles for a week. ADR-0018 made tiles `public` for a CDN, but missed two things: the endpoint is behind a login, and a shared cache's hit tells anyone who probes it that somebody looked at that coordinate, which is what the proxy exists to hide from vendors. Production's tile responses were never edge-cached anyway.

## Consequences

- Responses stay marked viewer-independent, so `Set-Cookie` and `Vary: Cookie` are stripped and a cookie refresh does not force a browser miss.
- A CDN rule that keys on the session or hides the hit/miss signal, or measured origin load only an edge cache can carry, would reopen this.
