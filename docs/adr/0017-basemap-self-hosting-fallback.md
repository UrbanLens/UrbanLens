---
status: accepted
date: 2026-09-19
---

# Self-hosted instances keep the free raster vendors as the basemap; no new dependency

Formerly `D17`.

Jess asked whether the Leaflet-to-MapLibre migration needs a new free tile provider for self-hosters, who have no REData. It does not. MapLibre's `raster` source is the same XYZ template Leaflet uses, and the existing vendors (CARTO, OpenTopoMap, Esri) already allow cross-origin fetches. So the client builds its style document itself, wrapping those vendors for a self-host, or this app's tile proxy when REData is configured. A vector style URL is fetched only once REData serves a vector mirror.

## Considered options

- Protomaps' hosted API as the built-in self-hosting default: rejected, because it is metered beyond a free tier and would add a required account, key and terms-of-service surface to every self-hosted instance.

## Consequences

- A self-hosted instance never gets vector rendering unless its operator runs REData with a vector mirror.
- This app's frontend owns the fallback vendor list and its attribution strings.
