# D25 — Map search geocodes browser-direct, with full as-you-type autocomplete

`id: D25` · `status: accepted` · `updated: 2026-09-29` · `decided by: Jess, 2026-09-29`

**This is Jess's ruling, not an agent's proposal.** Do not reopen it without asking her.

## Decision

- The location search engine, the add-pin address box and the markup map's title suggestion call Nominatim
  straight from the browser (`shared/location-search-engine.ts`, `static/js/comment-map.js`). That keeps
  geocoding traffic off UrbanLens's servers; a server proxy adds load rather than removing it.
- As-you-type autocomplete stays fully live. It is never made cache-only or cut.
- Enforcing a third party's terms of use is not this codebase's job. If a feature looks like it breaks
  one, write it up and ask Jess; don't degrade the feature.
- UrbanLens self-hosts geocoding infrastructure (Overpass in `../OpenStreetMap`, and Nominatim). A future
  autocomplete source could run through a self-hosted service.

## History

On 2026-09-29 an agent moved these calls behind a cached, rate-limited server proxy and made as-you-type
suggestions cache-only, citing Nominatim's usage policy (7a65b4498, f6df0c851, dfecf0807). All three were
reverted the same day, along with migration 0113 (the plugin's rate-limit change), before reaching staging
or production.

## Open for Jess

- `services/apis/locations/nominatim.py` and the browser code hardcode `https://nominatim.openstreetmap.org`.
  Pointing them at the self-hosted Nominatim needs its URL; nothing in this repo or its siblings names it.
