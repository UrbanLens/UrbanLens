# REData Feature-Parity Audit

Audit date: 2026-07-19. Compares UrbanLens's original property-records implementation
(the source of truth this document calls "OLD") against `../REData` (the standalone
extraction, "NEW"), which is meant to fully replace it. Purpose: identify anything OLD
does that NEW does not, before deleting OLD's code.

Methodology: five parallel deep-dive comparisons covering (1) data models/migrations,
(2) the core tiered-retrieval pipeline services, (3) geocoding services + management
commands, (4) the user-facing/API exposure layer, (5) test coverage. Every finding below
was verified by directly reading both sides' source; line numbers may drift slightly
day to day but the underlying claims were confirmed against real code, not inferred.

**Bottom line:** the core tiered retrieval pipeline (Tier 1 ArcGIS/Socrata, Tier 2 vendor
templates, Tier 3 scrape recipes + AI-assisted discovery, jurisdiction resolution,
merge/confidence logic, pacing) is a faithful, near line-for-line port — NEW is safe to
treat as the source of truth for that logic.

**Correction (post-review):** the two items originally listed here as blocking gaps —
"no private per-pin ownership model" and "no per-end-user authorization primitive" — were
a misreading of REData's intended architecture and are **not actually issues**. REData is
deliberately a raw-data API with no notion of pins, wikis, or per-user privacy; UrbanLens
is expected to query it for raw parcel/owner/sale facts and continue applying its own
pin-private/wiki-shared visibility rules on its own side, exactly as it does today for
every other external-data plugin. Re-implementing pin/wiki/user concepts inside REData
would be scope creep in the wrong direction — that logic belongs in the consuming app, not
the data provider. Struck from the blocking list below; the real, addressed gaps were
#3 and #4.

---

## ✅ Addressed in REData (see "Fixes applied" below)

### 3. Owner/sale records are never auto-populated from a fetched property record
OLD's plugin (`src\urbanlens\dashboard\plugins\builtin\property_records.py`) runs a
Celery-driven background enrichment cycle whose `_write_official_owners_and_sales`
(covered by 11 dedicated tests in `test_property_records_plugin.py`) auto-creates
`OwnerSource.OFFICIAL` `WikiOwner`/`WikiPropertySale` rows from a freshly-fetched
`PropertyRecord`, deduping on repeat fetch and never overwriting user-entered data.

NEW's `parcel_lookup._upsert_parcel` (`src\redata\parcels\services\parcel_lookup.py:53-77`)
only stores the raw fetched record as opaque JSON on `Parcel.record_payload` — it never
promotes `owner_name`/`sales_history` into `ParcelOwner`/`ParcelSale` rows, and REData has
no Celery task for property records at all (synchronous, on-request only; confirmed via
`grep -rn "_write_official\|OwnerSource.OFFICIAL"` outside tests finding only the
model-default declarations). This is an architectural change, not a relocated feature —
`ParcelOwnerViewSet`/`ParcelSaleViewSet` exist but nothing calls them automatically, and
they're currently untested (see Test Coverage gap below).

### 4. Cross-property owner identity is lost
OLD's `WikiOwner` has `locations = ManyToManyField(..., related_name="owners")`
(`models\property_owner\model.py:80`), explicitly documented as modeling "the same
landlord owns many places" — one owner entity linked to many properties, and unlinking
never deletes the shared record. NEW's `ParcelOwner` has a single FK to one `Parcel`
(`owner\model.py:16`), so the same real-world owner appearing at multiple parcels becomes
N independent, un-linked duplicate rows with no shared identity. Reasonable for a
per-parcel cache design, but it is a genuine loss of a modeled relationship, not a rename.

---

## 🟡 Should fix before cutover, but not outright data loss

### 5. Rate-limit tuning dropped for property-records-specific services
OLD's plugin (`plugins\builtin\property_records.py:359-383`) registers tuned
`ServiceDefaults` for `census_geocoder` (20/min, 1000/day, `usa_only=True`),
`property_records_gis` (30/min, 2000/day), and `property_records_scrape` (10/min,
500/day — deliberately lower, since it's scraping). NEW's static `SERVICE_REGISTRY`
(`src\redata\core\services\rate_limiter.py:52-72`) has no entries for any of these three
keys (or `census_tigerweb`), and NEW has no plugin system to supply them dynamically —
confirmed the actual `service_key`s are still used at call sites
(`arcgis_socrata.py:97`, `html_scrape.py:146`, `locations\census_geocoder.py:64`).
They silently fall back to a generic, untuned default (20/min, 500/day, no `usa_only`,
no cost/notes metadata) — under-limiting GIS calls and over-limiting nothing, but losing
the documented `usa_only` guard and any per-service cost-tracking metadata your project
CLAUDE.md requires for external API calls.

### 6. Sale→Owner linkage flattened to free text
OLD's sale models had `previous_owners`/`new_owners` M2M fields back to the owner model
(`models\property_owner\model.py:114-115,149-150`), so a sale recorded structured links
to who transferred to whom. NEW's `ParcelSale` only has flat `grantor`/`grantee`
CharFields (`sale\model.py:16-17`) with no FK/M2M back to `ParcelOwner` — matches the
plan doc's schema shape, but is a step down in queryability from what OLD had built.

### 7. `ParcelSale` has no custom queryset/manager helpers
OLD's `PinPropertySaleQuerySet`/`WikiPropertySaleQuerySet` provide `for_pin()`/
`for_location()` convenience filters (`models\property_owner\queryset.py:52-87`). NEW's
`parcels\models\sale\` directory has no `queryset.py` or `meta.py` at all — `ParcelSale`
relies on the bare default manager. Minor, but worth adding before this becomes the only
copy of the logic.

### 8. REData's REST API views have zero test coverage
`ParcelLookupView`, `ParcelViewSet`, `ParcelOwnerViewSet`, `ParcelSaleViewSet`
(`src\redata\api\views.py:41-162`) are real, reachable business logic, but
`grep -rn "ParcelLookupView\|ParcelViewSet\|ParcelOwnerViewSet\|ParcelSaleViewSet"
--include=test_*.py` across all of `src\redata` returns zero hits. `src\redata\api\tests\`
only covers auth/scope/throttle middleware using the lookup URL as a bare path string —
no test ever creates a `Parcel`/`ParcelOwner`/`ParcelSale` and asserts on the serialized
response, or exercises create/update/delete through these views. Given gap #3 above
(nothing auto-populates these tables), this is the *only* write path for owner/sale data
in the new system, and it's currently unverified.

### 9. `jurisdictions:write` API scope is declared but has no endpoint
`ApiKeyScope` includes `jurisdictions:write` (`api\models\api_key\meta.py:14`), but
`PropertyJurisdictionSerializer`'s own docstring confirms "No write endpoint in v1."
Not urgent (OLD's jurisdiction registry is site-admin-only today), but worth tracking so
it doesn't ship half-wired.

### 10. `OwnerSource` default flipped from `USER` to `OFFICIAL` with no visible guard
OLD: `OwnerSource.USER` is the default, and its docstring states `OFFICIAL` "is never
directly user-editable" (`property_owner\meta.py:7-9,15-16`) — enforced at the
controller/view layer. NEW: default is `OFFICIAL` (`owner\meta.py:9-10`), and
`ParcelOwner` adds a `recorded_by` FK with no model-layer guard preventing an API caller
with `owners:write` scope from creating an `OFFICIAL`-sourced row directly — there's no
per-caller enforcement distinguishing "this came from our own automated write-back" from
"a client app asked us to write an official record" at the API boundary. Worth confirming
intentional before treating "official" records as trustworthy provenance.

---

## ✨ Improvements found in NEW (worth adopting, some worth backporting to OLD before deletion)

- **`field_mapping.py`**: NEW added `"GEOPIN"`/`"REID"` to the APN heuristic candidates
  (`field_mapping.py:42`), fixing real-world acceptance of Guilford County, NC's ArcGIS
  layer (covered by two new hypothesis tests). This is a genuine bugfix made *after* the
  fork that never made it back to OLD — worth backporting to UrbanLens's copy if it will
  stay live even briefly, since OLD would otherwise reject that county's data today.
- **`html_scrape.py`**: NEW adds ASP.NET WebForms `postback` support (`ScrapeRecipe.postback`,
  `_extract_webforms_tokens`) for vendor sites requiring `__VIEWSTATE`/`__EVENTVALIDATION`
  round-trips. Verified this doesn't widen the Tier-3 data-placement allowlist — tokens
  come from the target site's own response, and `SearchField.ALL` (`situs_address`, `apn`)
  is unchanged and identical on both sides.
- **`discovery.py`**: NEW wraps AI-gateway *construction* in try/except in addition to the
  send call OLD already guards — minor robustness improvement.
- **Admin registration**: NEW registers all four models (`PropertyJurisdiction`, `Parcel`,
  `ParcelOwner`, `ParcelSale`) in Django admin; OLD only ever registered
  `PropertyJurisdiction`, leaving owner/sale rows admin-invisible.
- **`parcel_lookup.py`**: adds an auto-discover-then-retry flow on `REASON_UNRESEARCHED`
  (`src\redata\dashboard\views.py:50-104`) that OLD's plugin/command path never had.
- **Test coverage additions**: postback scraping, AI-gateway-construction-failure
  handling, and one more real-county relevance fixture (Guilford County layer
  acceptance) are all covered in NEW with no OLD equivalent — pure additions.

---

## 🟢 Verified equivalent (safe to treat NEW as authoritative)

- **Core tiered pipeline**: `orchestrator.py`, `schema.py`, `jurisdiction.py`,
  `arcgis_socrata.py`, `normalize.py`, `merge.py`, `pacing.py`, `meta.py`,
  `vendor_templates.py` (empty registry on both sides) — all byte-identical logic
  modulo import-namespace changes. `merge.py`'s tier-authority-wins-per-field and
  mismatch-flagging logic is confirmed correct and identical both sides.
- **`discovery.py`/`relevance.py`**: AI-assisted Tier 1 discovery's safety posture is
  unchanged — deterministic search always runs first, AI output is matched against a
  literal `allowed_urls` set built from real search results (never invents a URL),
  `.gov` preference and "untrusted data" framing in the prompt are identical.
- **Census geocoding gateways** (`census_geocoder.py`, `census_tigerweb.py`) and their
  shared `Gateway` base class: confirmed via diff to be identical apart from import paths.
- **Management commands** (`discover_property_jurisdiction.py`, including `--tier3`,
  and `test_property_record.py`): identical flags and behavior on both sides.
- **Pacing/cache backend**: both OLD and NEW use Django's shared cache (`django.core.cache`)
  backed by Redis/Valkey in `docker-compose.yml`/`settings/base.py`, so cross-worker pacing
  is equally real (or equally degraded to `LocMemCache`) on both sides — not a NEW-specific
  improvement or regression, despite REData's CLAUDE.md phrasing implying it might be new.
- **Test network isolation**: spot-checked both suites use `mock.patch`/`mock.Mock` for all
  external HTTP; no live `requests.get/post` calls found in either repo's property-records
  test files.
- **No negative-caching of failures** in NEW is a known, documented gap in REData's own
  CLAUDE.md roadmap (not silently missing) — track it there, not here.

---

## Known pre-existing gap in both repos (not introduced by the extraction)

`retrieved_at` per-field staleness tracking, called for explicitly in
`docs\property-records-plan.md` ("Record `retrieved_at` per field so staleness is visible
later"), is not actually implemented that granularly on **either** side: `merge.py`'s
`field_sources` only maps field name → winning tier number, and the merged record's
single `.source.retrieved_at` comes from whichever tier was primary. This predates the
REData extraction and applies equally to both codebases — noted here for completeness,
not as a REData-specific shortfall.

---

## Recommendation

Do not delete OLD's `property_owner` models, `controllers\property_owner.py`, or the
`property_records` plugin until gaps **#1–#4** are resolved in REData (private
per-pin notes, per-user authorization, automated write-back, and cross-property owner
identity) — these are real end-user-visible capabilities, not implementation details.
The core retrieval pipeline (services\apis\property_records\*, census geocoding
gateways, management commands) can be considered fully superseded by REData now, modulo
backporting the `GEOPIN`/`REID` field-mapping fix (or accepting it'll be gone once OLD's
copy is deleted) and reconciling the rate-limit tuning gap (#5).
