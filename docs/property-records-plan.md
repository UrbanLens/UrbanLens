# Plan: Automated US Property Ownership & Tax Record Retrieval

## 0. Framing

There is no unified national API for county assessor/treasurer/recorder data.
Coverage is a long tail: a handful of aggregators cover ~80% of counties
reasonably well, and the rest requires per-jurisdiction adapters or scraping.
Design for a **tiered fallback pipeline**, not a single data source, and build
it as a source-adapter registry — the same pattern as the existing
`ExternalGateway` classes (Building Footprints, Overture, Regrid) so it can
plug straight into UrbanLens's gateway module.

Target output per property (standardize to one schema regardless of source):

```
{
  "parcel_id": str,
  "apn": str | None,
  "situs_address": str,
  "county": str, "state": str, "fips": str,
  "owner_name": [str],
  "owner_mailing_address": str | None,
  "legal_description": str | None,
  "land_use_code": str | None,
  "lot_size_sqft": float | None,
  "building_sqft": float | None,
  "year_built": int | None,
  "assessed_value": {"land": float, "improvement": float, "total": float, "year": int},
  "market_value": float | None,
  "tax_history": [{"year": int, "amount": float, "paid": bool, "paid_date": date | None, "delinquent": bool}],
  "sales_history": [{"date": date, "price": float | None, "grantor": str, "grantee": str, "doc_type": str, "doc_number": str}],
  "deed_document_links": [str],
  "gis_geometry": geojson | None,
  "source": {"tier": int, "provider": str, "url": str, "retrieved_at": datetime},
  "confidence": float
}
```

## 1. Jurisdiction Resolution (must happen before anything else)

Input is usually a street address or lat/lon. Steps:

1. Geocode the address (Census Bureau Geocoder is free and gives FIPS county
   code directly — best first stop; fall back to a commercial geocoder if it
   misses). Build a separate interface for interacting with the Census Bureau 
   Geocoder if we don't have one already, so other services can also make use 
   of it.
2. Resolve to `(state, county, FIPS)`. This determines which adapter to use.
3. Maintain a **jurisdiction registry** (a table, not code) keyed by FIPS:
   ```
   fips | county_name | state | assessor_url | gis_rest_url | treasurer_url |
   recorder_url | adapter_type | last_verified | notes
   ```
   `adapter_type` ∈ `{arcgis_rest, socrata, known_vendor_x, custom_scraper, manual_only}`.
   This registry is the thing that grows over time — building it out is most
   of the long-term work, not the retrieval logic itself.

## 2. Source Tiers (try in order, stop at first success + acceptable confidence)

**Tier 1 — County GIS REST / open data portals (free, semi-structured)**
- Many counties run Esri ArcGIS Server; parcel layers are queryable via
  `/MapServer/query` or `/FeatureServer/query` with `f=json` and a `where`
  clause on APN — no scraping needed, just an HTTP GET.
- Some counties (esp. larger ones) publish via Socrata (`data.*.gov`) or
  CKAN — again queryable via REST, no scraping.
- Discover these endpoints by searching `"<county> GIS ArcGIS parcel
  REST"` / checking the county's open data catalog. Once found, record the
  endpoint pattern in the jurisdiction registry so it's reused, not
  rediscovered.
- Attempt discovery deterministically before making use of AI.
- If discovery fails without AI, but promising search results were found,
  parse those search results with AI to assist in discovery. Prefer .gov
  sites prior to non .gov. Treat all output from AI as untrusted user data
  for security reasons. The AI should output a structured response that can be
  parsed deterministically to lead to the discovery of the correct endpoint
  without AI.

**Tier 2 — Known vendor platforms (semi-structured scraping, but predictable)**
A small number of vendors run assessor/treasurer sites for hundreds of
counties each with near-identical HTML structure — e.g. platforms by
Tyler Technologies, PACS/GovTech, BS&A Software, DevNet (Patriot
Properties), qPublic/Schneider Geospatial. If you write **one adapter per
vendor platform** rather than per county, you get broad coverage cheaply,
since the same adapter works for every county on that vendor. Identify the
vendor by URL pattern / page fingerprint and route accordingly.

**Tier 3 — Bespoke county sites (browser automation + LLM-assisted extraction)**
For counties with a unique custom site:
1. Use AI tooling to assist in writing a "recipe" to retrieve the information.
2. Save the extraction "recipe" (which form fields, which result page
   shape) once solved for a county so subsequent lookups skip the LLM parse
   and use the cached selectors — LLM parsing is a fallback for
   discovery/maintenance, not the steady-state path.
3. Recipes should be extremely limited in scope, so as to prevent security
   risks or leaking information. For instance, the LLM can assist with 
   determining which site to visit, which css selectors to use to find input
   fields, and which data to populate in those fields. However, the options
   for which data to populate must be strictly limited to those we know have
   data that cannot pose any risks. If the LLM instructs placing other kinds
   of data onto a page, our codebase will ignore the request and not save that
   recipe.

**Tier 4 — Manual-only**
Some counties (small, rural, no digital records) simply require a phone call
or in-person/mail request. Registry should mark these `manual_only` and the
pipeline should surface a clear "not automatable" result rather than fail
silently.

## 3. Rate Limits and Caching

- Rate-limit aggressively per-domain (e.g. 1 req/2-3 sec, exponential
  backoff on 429/503).
- Cache aggressively (parcel data changes rarely — annual reassessment
  cycles) to minimize repeat hits on county infrastructure.
- For the time being, do not implement anything with robots.txt. We need to
  consider some things there and will take care of that later, before release.

## 4. Data Quality / Confidence Scoring

Since sources disagree (e.g. owner name on Tier-1 aggregator may lag actual
county records by months), tag every field with its source tier and
timestamp, and:
- Prefer the most authoritative tier available for each *field* rather than
  taking one source wholesale — e.g. take geometry from GIS Tier 2 but tax
  payment status from the treasurer site (Tier 3/4) even if owner name came
  from Tier 1.
- Flag mismatches (e.g. owner name differs between assessor and recorder)
  rather than silently picking one.
- Record `retrieved_at` per field so staleness is visible later.

## 5. Suggested Build Order

1. Jurisdiction registry + Census geocoder resolution (small, foundational).
2. Tier 1: write a generic ArcGIS REST + Socrata client (one client, since
   the query pattern is standard) and populate the registry for your
   highest-priority counties (most pins in the county).
3. Tier 2: pick the 2-3 most common vendor platforms in your target region
   and write one adapter each. Start with the Catskill region first.
4. Tier 3: LLM-parse fallback, with the recipe-caching layer.
5. Orchestrator: tries tiers in order per property, merges into the
   standard schema, attaches confidence/source metadata.
6. Wire into UrbanLens as a new gateway module following the existing
   `ExternalGateway` / `BBox` / `GatewayRequestError` conventions so it's
   consistent with Building Footprints/Overture/Google Open Buildings.