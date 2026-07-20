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

The items below (#3, #4, #5, #8) were addressed directly in `../REData` (not just
documented) - model changes, a generated migration, service logic, and new tests, all
verified with `ruff check --fix`, `mypy`, and the full REData pytest suite (477 passed,
no regressions) run inside the local Docker Compose stack.

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

---

## 🟡 Still open (not requested this round, worth tracking before further cutover)

### 6. Sale→Owner linkage flattened to free text
OLD's sale models had `previous_owners`/`new_owners` M2M fields back to the owner model
(`models\property_owner\model.py:114-115,149-150`), so a sale recorded structured links
to who transferred to whom. NEW's `ParcelSale` only has flat `grantor`/`grantee`
CharFields (`sale\model.py:16-17`) with no FK/M2M back to `ParcelOwner` — matches the
plan doc's schema shape, but is a step down in queryability from what OLD had built.
Not changed in this pass since it wasn't requested and would mean either widening
`ParcelSale`'s schema or overloading the owner-identity matching built for fix #3/#4 onto
sale grantor/grantee text, which felt like scope creep beyond what was asked.

### 7. `ParcelSale` has no custom queryset/manager helpers
OLD's `PinPropertySaleQuerySet`/`WikiPropertySaleQuerySet` provide `for_pin()`/
`for_location()` convenience filters (`models\property_owner\queryset.py:52-87`). NEW's
`parcels\models\sale\` directory has no `queryset.py` or `meta.py` at all — `ParcelSale`
relies on the bare default manager. Minor, but worth adding before this becomes the only
copy of the logic.

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

REData now fully encapsulates the property-records feature's data and behavior: the
retrieval pipeline was already a faithful port, and the four real gaps (automated
owner/sale write-back, cross-property owner identity, rate-limit tuning, REST API test
coverage) are fixed as of this pass. It is safe to proceed with removing UrbanLens's
`services\apis\property_records\*`, `models\property_jurisdiction\*`,
`models\property_owner\*`, `controllers\property_owner.py`, and the `property_records`
plugin, **provided** UrbanLens's replacement code (which will call REData's REST API
instead) re-implements the pin-private/wiki-shared visibility split and per-user
authorization on its own side — REData deliberately doesn't and shouldn't do that; see
the correction note at the top of this document.

Two loose ends worth a conscious decision, not a blocker:
- Backport the `GEOPIN`/`REID` field-mapping fix (#✨ above) to OLD if it's going to stay
  live even briefly, since OLD currently rejects Guilford County, NC's real ArcGIS layer.
- Items #6, #7, #9, #10 (sale→owner linkage flattened to text, no `ParcelSale` queryset
  helpers, unwired `jurisdictions:write` scope, `OwnerSource` default flip) are minor and
  weren't part of this round's requested fixes - still open, tracked above.
