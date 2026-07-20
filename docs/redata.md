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

## ✅ Fixed in REData (2026-07-19)

All eight numbered findings (#3-#10) were addressed directly in `../REData` (not just
documented) across two passes - model changes, three generated migrations, service
logic, and new tests, all verified with `ruff check --fix`, `mypy`, and the full REData
pytest suite (496 passed, no regressions) run inside the local Docker Compose stack
against a real Postgres/Redis backend. #1/#2 were struck as non-issues (see the
correction note above) rather than fixed - REData deliberately doesn't own pin/wiki/
per-user concerns, so there was nothing there to implement.

### 3. Owner/sale records are never auto-populated from a fetched property record — FIXED
OLD's plugin (`src\urbanlens\dashboard\plugins\builtin\property_records.py`) runs a
Celery-driven background enrichment cycle whose `_write_official_owners_and_sales`
(covered by 11 dedicated tests in `test_property_records_plugin.py`) auto-creates
`OwnerSource.OFFICIAL` `WikiOwner`/`WikiPropertySale` rows from a freshly-fetched
`PropertyRecord`, deduping on repeat fetch and never overwriting user-entered data.

**Fix**: `parcels\services\parcel_lookup.py` now has `_write_official_owners_and_sales`,
`_get_or_create_official_owner`, and `_sale_price_as_decimal`, ported from OLD's plugin
logic and adapted to REData's typed `PropertyRecord` dataclass (no JSON re-parsing
needed, unlike OLD). `lookup_or_refresh_parcel` calls it immediately after upserting the
`Parcel` row, inside the same `transaction.atomic()` block so a fetch can never leave the
parcel updated without its owner/sale rows (or vice versa). Matches OLD's behavior
exactly: only ever creates or links `OFFICIAL` rows, never edits/unlinks an existing one,
so a `MANUAL` correction is never overwritten. Covered by 19 new tests in
`parcels\tests\hypothesis\test_parcel_lookup.py`.

### 4. Cross-property owner identity is lost — FIXED
OLD's `WikiOwner` has `locations = ManyToManyField(..., related_name="owners")`
(`models\property_owner\model.py:80`), explicitly documented as modeling "the same
landlord owns many places" — one owner entity linked to many properties, and unlinking
never deletes the shared record.

**Fix**: `parcels\models\owner\model.py`'s `ParcelOwner.parcel` FK was changed to
`ParcelOwner.parcels = ManyToManyField("parcels.Parcel", related_name="owners")` —
restoring `WikiOwner`'s exact modeling, adapted to REData's Parcel-keyed identity. The
same real-world owner reported on multiple parcels now resolves to one shared row
(matched deterministically by case-insensitive name + mailing address, `OFFICIAL` rows
only - see `_get_or_create_official_owner`'s docstring for why an exact rather than fuzzy
match was chosen) instead of a duplicate per parcel. Migration `0002_owner_many_to_many_parcels.py`
was generated via `manage.py makemigrations` (verified against the real schema, not
hand-written). `admin.py`, `api\serializers.py` (`ParcelOwnerSerializer`), and
`api\views.py` (`ParcelOwnerViewSet.get_queryset`/`perform_create`) were all updated for
the M2M relation - DRF's `ModelSerializer.create()` already handles M2M kwargs correctly,
so the API's create-owner endpoint didn't need special-casing. Covered by new
cascade/cross-parcel-identity tests in `test_parcel_owner.py`, plus integration coverage
in `test_parcel_lookup.py`.

### 5. Rate-limit tuning dropped for property-records-specific services — FIXED
OLD's plugin (`plugins\builtin\property_records.py:359-383`) registers tuned
`ServiceDefaults` for `census_geocoder` (20/min, 1000/day, `usa_only=True`),
`property_records_gis` (30/min, 2000/day), and `property_records_scrape` (10/min,
500/day — deliberately lower, since it's scraping); `census_tigerweb` (30/min, 2000/day)
was similarly tuned via a separate plugin (`plugins\builtin\census_tigerweb.py:74-84`).
NEW's static `SERVICE_REGISTRY` (`src\redata\core\services\rate_limiter.py:52-72`) had no
entries for any of these four keys, and NEW has no plugin system to supply them
dynamically — they silently fell back to a generic, untuned default (20/min, 500/day, no
`usa_only`, no cost/notes metadata).

**Fix**: added all four tuned `ServiceDefaults` entries directly to REData's static
`SERVICE_REGISTRY` (`core\services\rate_limiter.py`), copied from OLD's plugin values
verbatim (including the deliberately-lower scrape budget and the `usa_only` flag).
Covered by 6 new tests in `core\tests\test_rate_limiter.py`, including one confirming
`get_limit_config` actually persists the tuned budget rather than the generic fallback.

### 8. REData's REST API views had zero test coverage — FIXED
`ParcelLookupView`, `ParcelViewSet`, `ParcelOwnerViewSet`, `ParcelSaleViewSet`
(`src\redata\api\views.py:41-162`) are real, reachable business logic, but no test ever
created a `Parcel`/`ParcelOwner`/`ParcelSale` and asserted on the serialized response, or
exercised create/update/delete through these views. Given fix #3 above, these are now the
primary write path for owner/sale data (alongside the automated write-back), so this
mattered more than when the tables were never populated at all.

**Fix**: added `api\tests\test_views.py` — 16 end-to-end tests using a real `APIClient`
and a real `ApiKey` (not mocked auth), covering: coordinate validation on the lookup
endpoint, the 404-vs-503 split on `PropertyRecordsUnavailableError` by permanence, scope
enforcement (401 unauthenticated / 403 wrong scope) on every view, `ParcelViewSet`/
`JurisdictionViewSet` read access, and `ParcelOwnerViewSet`/`ParcelSaleViewSet` create
behavior — including that `recorded_by` is always taken from the authenticated key's
user and never from client-supplied request data. While writing these, found and fixed a
related bug: `ParcelSale.sale_price` had no validator, so a negative price hit the DB's
`ck_parcels_sale_price_gte_0` CHECK constraint as an unhandled `IntegrityError` (500)
instead of a normal 400 — added `MinValueValidator(0)`, which DRF's `ModelSerializer`
picks up automatically from the model field.

### 6. Sale→Owner linkage flattened to free text — FIXED
OLD's sale models had `previous_owners`/`new_owners` M2M fields back to the owner model
(`models\property_owner\model.py:114-115,149-150`), so a sale recorded structured links
to who transferred to whom. NEW's `ParcelSale` only had flat `grantor`/`grantee`
CharFields with no FK/M2M back to `ParcelOwner`.

**Fix**: added `ParcelSale.previous_owners`/`new_owners` (`ManyToManyField` to
`ParcelOwner`, `related_name="sales_as_previous_owner"`/`"sales_as_new_owner"` -
matching OLD's exact related-name convention). `grantor`/`grantee` text fields are kept
(still populated straight from the source, unconditionally) and now *additionally*
resolved to owner rows: `parcel_lookup._write_official_sale` calls the same
`_get_or_create_official_owner` used for `owner_name`, so a grantor/grantee reuses an
already-known owner instead of creating a disconnected duplicate, and links them to the
sale (`previous_owners`/`new_owners`) and to the parcel itself (`ParcelOwner.parcels`) -
mirroring OLD's `_write_official_owners_and_sales` exactly, including that a grantor/
grantee becomes a linked owner of that parcel, not just sale metadata. Migration
`0003_sale_owner_links.py` generated via `makemigrations`. Covered by new tests in
`test_parcel_owner.py` (model-level M2M behavior) and `test_parcel_lookup.py`
(`WriteBackSaleOwnerLinkageTests` - linkage, owner reuse, blank grantor/grantee).

### 7. `ParcelSale` had no custom queryset/manager helpers — FIXED
OLD's `PinPropertySaleQuerySet`/`WikiPropertySaleQuerySet` provide `for_pin()`/
`for_location()` convenience filters. NEW's `parcels\models\sale\` directory had no
`queryset.py` at all.

**Fix**: added `parcels\models\sale\queryset.py` (`ParcelSaleQuerySet`/`ParcelSaleManager`)
with `official()`/`manual()` (mirroring `ParcelOwnerQuerySet`), `for_parcel()` (REData's
equivalent of OLD's `for_pin`/`for_location` - REData has no Pin/Location, only Parcel),
and `for_owner()` (new - REData-only, since `previous_owners`/`new_owners` didn't exist
in OLD's shape either; matches a sale where the given owner appears on either side).
Covered by `ParcelSaleQuerySetTests` in `test_parcel_owner.py`.

### 9. `jurisdictions:write` API scope was declared but had no endpoint — FIXED
`ApiKeyScope` includes `jurisdictions:write`, but `PropertyJurisdictionSerializer`'s own
docstring said "No write endpoint in v1."

**Fix**: `JurisdictionViewSet` is now a full `ModelViewSet` (was read-only
`Retrieve`/`ListModelMixin`) - no `urls.py` change needed since it was already
router-registered, so the same URLs simply gained `POST`/`PATCH`/`PUT`/`DELETE`. Added
`PropertyJurisdictionWriteSerializer` (the full registry shape, including the Tier 1-3
adapter wiring - `gis_rest_url`, `field_map`, `scrape_recipe`, etc. - that the read
serializer deliberately omits), selected via `get_serializer_class` branching on
read-vs-write actions the same way `ParcelOwnerViewSet`/`ParcelSaleViewSet` already
branch `required_scopes`. `discovered_by` is set server-side from the authenticated
user on create, never client-supplied. Covered by 6 new tests in
`JurisdictionViewSetTests` (scope enforcement, create, client-supplied-`discovered_by`
rejection, update).

### 10. `OwnerSource` default flip had no API-boundary guard — FIXED
OLD's docstring states `OFFICIAL` "is never directly user-editable" - enforced at the
controller/view layer. NEW's `ParcelOwner`/`ParcelSale` default to `OFFICIAL` with no
guard stopping an API caller with `owners:write`/`sales:write` scope from creating an
`OFFICIAL`-sourced row directly.

**Fix**: added `source` to `read_only_fields` on both `ParcelOwnerSerializer` and
`ParcelSaleSerializer`, and both viewsets' `perform_create` now explicitly force
`source=OwnerSource.MANUAL` - mirroring `parcel`/`recorded_by`'s existing
server-side-only handling. A client-supplied `source` value (on create *or* update, since
read-only fields are excluded from both) is now always ignored; `OFFICIAL` is reachable
only through `parcels.services.parcel_lookup`'s automated write-back. Covered by 4 new
tests across both viewsets' test classes in `api\tests\test_views.py`.

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

REData now fully encapsulates the property-records feature's data and behavior: the
retrieval pipeline was already a faithful port, and all eight real gaps found in the
original audit (automated owner/sale write-back, cross-property owner identity,
rate-limit tuning, sale→owner linkage, missing `ParcelSale` queryset helpers, the unwired
`jurisdictions:write` scope, the `OwnerSource` API-boundary guard, and REST API test
coverage) are now fixed and verified against a real Postgres/Redis backend with zero
regressions.

One loose end worth a conscious decision, not a blocker: backport the `GEOPIN`/`REID`
field-mapping fix (see "Improvements found in NEW" above) to OLD if it's going to stay
live even briefly, since OLD currently rejects Guilford County, NC's real ArcGIS layer.

---

## Integration status (2026-07-20): UrbanLens now consumes REData

`dashboard.plugins.builtin.property_records` was rewired to fetch records via a new
`RedataGateway` (`services.apis.property_records.redata_gateway`) calling REData's
`GET /api/v1/parcels/lookup/` instead of a local tiered pipeline. Removed from UrbanLens
as a result (all now superseded by REData, confirmed with zero remaining references):
`services\apis\property_records\{arcgis_socrata,discovery,field_mapping,html_scrape,
jurisdiction,merge,meta,normalize,orchestrator,pacing,relevance,schema,vendor_templates}.py`,
`services\apis\locations\census_geocoder.py`, `models\property_jurisdiction\*` (+ a new
migration dropping the table), `management\commands\{discover_property_jurisdiction,
test_property_record}.py`, and their corresponding hypothesis tests.

**Deliberately kept, unchanged**: `models\property_owner\*` (`PinOwner`/`WikiOwner`/
`PinPropertySale`/`WikiPropertySale`) and `controllers\property_owner.py` - this is
UrbanLens's own private-per-pin/shared-per-wiki community-data layer with its own
authorization rules, exactly the concern the correction note above says belongs in the
consuming app, not REData. `_write_official_owners_and_sales` in the plugin still
auto-populates these `OwnerSource.OFFICIAL` rows from a successful fetch, unchanged -
only where the fetched data comes from changed.

Requires `UL_REDATA_API_URL`/`UL_REDATA_API_KEY` configured (see `.env-sample`) - the
plugin's `get_service_defaults` now declares one `redata_api` rate-limit budget instead
of the three third-party ones it used to (`census_geocoder`/`property_records_gis`/
`property_records_scrape`).

---

## Expansion (2026-07-20): authoritative property/building boundaries

Investigated what other data REData's sources return that wasn't yet surfaced. Two findings,
both now implemented:

### 1. `parcel_geometry` was already flowing through, just unused

REData's Tier 1 ArcGIS adapter has always captured parcel boundary geometry
(`arcgis_socrata.py`, `returnGeometry=true&outSR=4326`) and included it in every
`record_payload` - UrbanLens's plugin received it in `_fetch_payload` all along but only ever
rendered a "Boundary available" chip, never the geometry itself. Meanwhile
`services\locations\boundaries.py`'s `BoundaryProviderChain` had an empty **property**
boundary slot for US coordinates - `RegridGateway` (the one provider that could fill it) is
implemented but deliberately excluded (paid service, no current plans to integrate).

**Fix**: added `RedataBoundaryProvider`
(`services\apis\locations\boundaries\redata.py`), wired in as the *first* provider in the
default chain (`services\locations\boundaries.py`) - authoritative county-GIS geometry beats
community/ML-derived boundaries whenever it's available, and it's a quiet no-op (not an
error) both for installs that haven't configured REData and for jurisdictions REData hasn't
researched. Added `esri_rings_to_polygon` (`services\apis\locations\base.py`) to convert
REData's raw Esri ring-list geometry into a GEOS `Polygon`/`MultiPolygon` - correctly
classifying exterior shells vs. holes by ring winding direction (Esri's convention is the
*opposite* of GeoJSON's) and assigning each hole to whichever shell actually contains it
(point-in-polygon test, since Esri doesn't guarantee array ordering). This conversion didn't
exist anywhere before; REData's own schema docstring explicitly deferred it, assuming a
Leaflet consumer that could draw raw rings directly.

**Bonus fix found via this work**: `best_polygon_from_geometry` (the existing helper every
other boundary provider already used) had a real, confirmed bug - `Polygon(geos_geometry)`
where `geos_geometry` was already a `Polygon` (a `MultiPolygon`'s own element). Django's
`Polygon` constructor has no "copy an existing Polygon" overload and raises `TypeError` on
one, so any provider resolving a genuinely multi-shell geometry (a building complex with
detached wings, a multi-part OSM relation) would have crashed instead of returning the
largest shell. Fixed the same way as the new code: return the element directly, never
re-wrap it. Covered by a new regression test
(`test_location_background_services.py::BestPolygonFromGeometryTests`) that reproduces the
crash against real GEOS objects.

### 2. Building footprints - not previously possible, now supported

REData's registry (`PropertyJurisdiction`) only ever stored one GIS endpoint per county (the
parcels layer) - even though many of the same county ArcGIS servers also publish a *separate*
building-footprint layer as a sibling on the same MapServer, REData had no way to query it.

**Fix (REData)**: added `PropertyJurisdiction.gis_building_layer_url` (manually curated, like
`vendor`/`scrape_recipe` - distinguishing a real building-footprint layer from the dozens of
other layers a county MapServer typically exposes needs a human looking at the actual layer
list, not a heuristic), `ArcGisSocrataGateway.query_building_geometry_by_point`, and
`PropertyRecord.building_geometry` (same Esri-ring-list shape as `parcel_geometry`). Wired
into `orchestrator._try_tier1` as a best-effort supplementary lookup - a down/misconfigured
building layer degrades to "no building geometry", never fails the parcel match itself.
Exposed automatically via the existing `record_payload` (no API-layer change needed, same as
`parcel_geometry` before it). ArcGIS-only (Socrata has no sibling-layer concept); ARM-length
from the merge pipeline's tier-authority logic for free, since `merge.py`'s `_CONTENT_FIELDS`
is derived from `dataclasses.fields(PropertyRecord)` automatically.

**Fix (UrbanLens)**: `RedataBoundaryProvider.get_typed_boundaries` reads `building_geometry`
the same way as `parcel_geometry`, filling the chain's **building** slot too when present -
authoritative survey-grade footprints from the county itself, a real quality upgrade over the
ML-derived Microsoft/Google/Overture datasets whenever a jurisdiction has one configured.

### 3. Other data considered, not pursued

`tax_history` (payment/delinquency status) and `deed_document_links` are schema fields REData
never actually populates from any tier today. Not an "available but withheld" situation like
the geometry was - Tier 1 assessor GIS layers don't typically carry that data at all; it would
need a real Tier 2/3 (treasurer/recorder site) integration verified against a live target,
which is explicitly the unpopulated part of REData's own roadmap (no vendor scrape templates
yet). Not attempted here for the same reason REData's own CLAUDE.md gives for not guessing at
vendor templates without a concrete site to verify against.
