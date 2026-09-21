# I5 — What the browser could fetch instead of the server, and what that costs in privacy

`id: I5` · `status: actionable` · `updated: 2026-09-21`

> **Written by a Claude agent. Not authoritative.** Re-measure before relying on a figure, and
> rewrite this file when you find it wrong.

At 1,000 concurrent users the app container is CPU-throttled 31.6% of the time (P134), so any
request-path work moved off it is capacity. Nine external calls happen inside a request. This is
which of them a browser could make directly, and what is given up by letting it.

**The privacy question is the whole question, and it is Jess's to answer, not this document's.**
`profile.external_apis_enabled` is worded "allow external services to retrieve anonymized research
data for you" (`models/profile/model.py:600`). Today that anonymity is real and enforced: the
provider sees UrbanLens's IP, not the user's, and a user who declines simply has no call made for
them. A browser-side call cannot keep either half. The provider sees the viewer's own IP tied to
the exact coordinate they are looking at, and a client-side opt-out can only hide a button - it
cannot stop a browser from making a request. **So every "possible" row below is a change to what
that setting promises, not only a change to where a fetch happens.**

## The one candidate with no privacy delta

**The Overpass infrastructure overlay.** `MapController.infrastructure_features`
(`controllers/maps.py:252`) proxies Overpass QL per pan/zoom while the layer is on, cached 5
minutes per viewport bbox. Its primary endpoint is `overpass.osm.urbanlens.org` - **UrbanLens's
own mirror** (`services/apis/locations/boundaries/overpass.py:27`), so the browser talking to it
directly discloses nothing to a third party that the project does not already host. Keyless,
CORS-open, and genuinely synchronous in the request today. This is the one to do first.

The public fallbacks (`overpass-api.de`, `maps.mail.ru`) would be a third-party disclosure, so a
client-side implementation should fail closed to the server proxy rather than fall back in the
browser.

## Possible, but each trades the proxy guarantee away

| call | keyless? | CORS? | what the provider learns |
|---|---|---|---|
| Wikipedia geosearch (`assets/wikipedia.py:113`) | yes | yes, with `origin=*` on the query - the bare `Origin` header alone returns no CORS header | viewer IP ↔ exact coordinate |
| Nominatim forward geocoding (`locations/geocode_resolution.py:41`) | yes | yes | viewer IP ↔ the address they typed |

Wikipedia geosearch is also bundled into the same cached JSON response as the Google and NPS
branches (`maps.py:885-1036`), so extracting it is a refactor of a shared endpoint rather than a
swap. Nominatim carries a 1 req/sec application-wide ToS ceiling, which a browser fleet cannot
honour - moving it client-side does not distribute that limit, it breaks it.

## Ruled out

Google Places Nearby Search, Place Details, Places Autocomplete and Street View metadata, and the
REData NPS parks lookup, all need a server-only key. `settings/app.py:714` names the Google key
`unrestricted` precisely because it is *not* referrer-locked; a `google_domain_restricted_api_key`
is declared at `app.py:715` and referenced nowhere else, so no browser-safe key is wired up today.
These are also a paid, VIP-gated feature already cached 90 days on a ~2 km grid, so most repeat
requests never reach the provider anyway. Moving any of them client-side would additionally lose
the automatic `ApiCallLog` cost row the `Gateway` base writes.

## Found while looking, unrelated to offload

`MapController.streetview_check` (`maps.py:455-484`, fires on every map right-click) calls
`urllib.request.urlopen` directly rather than `self.session`. It therefore writes **no**
`ApiCallLog` row and is subject to **no** rate limit - the one call in this table outside the
machinery `dashboard/CLAUDE.md` says everything must be inside. That is a billing and abuse
exposure independent of anything here, and is worth its own problem record.

## What this does not establish

- **How much server work any of this removes.** Nothing here was measured; the k6 harness does not
  exercise a single one of these endpoints, so the ladder cannot answer it either. The Overpass
  overlay's share of the request path is unmeasured.
- **Whether users would accept the disclosure.** A product question. The honest shape is probably
  a per-provider consent rather than one flag, since the Overpass mirror and Wikipedia are not the
  same ask.
