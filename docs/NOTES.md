# UrbanLens Notes

Non-obvious details about how UrbanLens works, gathered from a codebase audit (2026-07-11).
This complements `docs/FEATURES.md` (what the app does) and `CLAUDE.md` (how to work in the repo).
These are facts about current behavior, not guarantees — verify against the code before relying
on specifics that matter (line numbers, exact model fields).

## Location vs. Pin vs. Wiki — the core data split

Three models are easy to confuse and have strictly separated responsibilities:

- **`Location`** — the shared, global truth about a physical place: canonical name, address
  components, coordinates, Google CID. Not user-specific. Many `Pin`s and one `Wiki` can point
  at the same `Location`.
- **`Pin`** — one user's *personal* record for a location: custom name override (nullable —
  `None` means "use the location's name", see `pin.effective_name`), private notes, icon,
  priority, status, last-visited date, marker coordinates. Address/place metadata is read via
  proxy properties that delegate to `self.location`; it is never stored directly on `Pin`.
- **`Wiki`** — an *opt-in*, community-editable page about a `Location`. Not every Location has
  one; users seed them explicitly. Keeping Wiki opt-in was a deliberate privacy decision — see
  "coordinate immutability" below for the related concern about leaking exact locations, and
  "Wiki visibility" below for who is actually allowed to see one once it exists.

`Location.lat`/`long` are **immutable after insert** (enforced by both `Location.save()` and a
DB trigger). Address components stay mutable to allow geocode backfill. Use
`Location.objects.get_nearby_or_create(...)` rather than constructing new Locations directly when
coordinates might already exist nearby.

## Wiki visibility — pinned, not public

A `Wiki` existing is not the same as a `Wiki` being visible. **A profile may only see (or act on)
a Wiki for a Location it has pinned itself** — a community wiki is not a public wiki. The site's
entire premise is that a place is only discoverable through someone's own exploration (their own
pin); a wiki readable by any logged-in user would let people browse other users' pinned locations
by guessing/crawling `location_slug` values, defeating that.

Every wiki-scoped controller must resolve its `Location`/`Wiki` through
`services.wiki_access.resolve_visible_wiki(request, location_slug)` (or check
`location_visible_to(location, profile)` directly for views that need a different response shape
than a redirect/404 — e.g. a JSON polling endpoint that should return an empty list rather than
error). The check is simply "does the requester have a `Pin` at this `Location`" —
`Pin.objects.filter(profile=profile, location=location).exists()`.

The critical detail is **response shape, not just access control**: a location_slug for a wiki the
requester hasn't pinned must be **indistinguishable** from a location_slug that doesn't exist at
all, or has no wiki. `resolve_visible_wiki` raises the identical `Http404` in all three cases for
exactly this reason — a 403 (or any response that differs from "not found") turns the slug into an
oracle that reveals *someone* has pinned that spot, even without exposing the wiki's content. This
was itself a bug found and fixed on 2026-07-11 (see below).

This was audited and fixed across the codebase on 2026-07-11 after a review surfaced that nearly
every wiki-scoped view (`location_wiki.py`, `aliases.py`, `image_gallery.py`, `boundary.py`,
`detail_pins.py`, `labels.py`, `markup.py`) only checked `LoginRequiredMixin` — any authenticated
user could view, and in several cases *edit*, any wiki on the site by slug, regardless of whether
they had ever pinned that location. `comments.py` was the sole exception that already gated on pin
ownership, but it returned a distinguishable 403 (rather than 404), which was itself a smaller
version of the same leak. All of these now go through the shared resolver.

Two deliberate exceptions worth knowing about, both because a hard 404 doesn't fit their contract:
- `LocationDetailPinJsonView` (background map-overlay polling) returns `{"detail_pins": []}` for
  both "no wiki here" and "wiki exists but you haven't pinned it" — same empty-array shape either
  way, so there's nothing to distinguish.
- `WikiImageView` (reposition/delete a single wiki photo) relies on `image.profile != profile`
  instead of a separate location check, because only a profile with a pin at that location could
  ever have uploaded the image in the first place (upload itself is gated).

## Labels are one model wearing four hats

`Label` (`dashboard/models/labels/model.py`, renamed from `Badge` in migration 0034) is the single
backing model for **tags**, **categories**, **statuses**, and **person labels** — distinguished
only by a `kind` field (see `KIND_*` constants in `labels/meta.py`). `models/categories/` is a thin
module re-exporting `Label` as `Category` (real, distinct `CategorySerializer`/`CategoryViewSet`/
`CategoryFilter` classes, not separate tables) for backward compatibility; the old `models/tags/`
alias shim was unused and has been deleted outright. The standalone `categories`/`tags`/`statuses`
controllers and viewsets were deleted in favor of the unified label controller/viewset with a
`kind` parameter — if you find references to the old separate modules elsewhere, they're stale.

Per-user visual overrides (color/icon) on a shared global label live in a separate
`LabelCustomization` model — editing "your" label color never mutates the label other users see.

## Pin slugs are scoped per-profile, not global

`Pin` has `UniqueConstraint(fields=["profile", "slug"], condition=Q(slug__isnull=False))` — slugs
are unique *per user*, not site-wide. Code that looks up a pin by slug alone (without also
filtering by profile) will silently return the wrong user's pin if two users have pins with the
same slug. This has been a recurring source of bugs in upload/gallery/weather endpoints — always
scope slug lookups by the owning profile.

There's also `UniqueConstraint(fields=["location", "profile"], condition=Q(parent_pin__isnull=True))`
— a user can only have one top-level pin per Location (sub-pins via `parent_pin` are exempt).
TODO NOTE From Jess: I could be mistaken, but I think there shouldn't be an exception for sub-pins. Sub-pins will be nearby, of course, but the coordinates won't be exactly, precisely the same. This exception allows for two pins to precisely overlap on a map, which surely not very helpful.

## Boundary matching only trusts the auto-generated polygon

`Boundary.generated_polygon` (from external building-footprint APIs) is the only polygon used by
`get_for_point`/`get_all_for_point`/`within_bounding_box`. The user-editable `polygon` field is
for *display* only and is deliberately excluded from matching logic — otherwise a user could
inflate their boundary to claim overlap with other pins/areas.

## Parcel vs. building scope — counted from children, never from the parcel data

A Pin (and its Wiki) has always doubled as both *the parcel* and *the building*, because for an
ordinary place those are the same thing. On a campus they are not: the pin at
`41.73315, -73.93037` was rendering "TOOL SHED (1937) — NON-CONTRIBUTING, Building Number 154"
from NY SHPO's CRIS inventory, because `CrisBuildingPanelSource` took the *first*
`resource_type == "building"` match inside a 200 m radius. `services/locations/site_scope.py`
is the one place that decides which a marker is.

Two rules, in order. **An explicit choice wins**: `pin_type_is_user_provided` marks a type the
user actually picked, mirroring how `name_is_user_provided` guards `Pin.name`. **Otherwise, count
the children typed as buildings** — two or more (`MULTI_BUILDING_THRESHOLD`) makes the parent a
parcel.

What is deliberately *not* a rule: "REData says this parcel has several buildings." That signal is
real, and it drives the "would you like to add pins for the buildings here?" offer — but on its own
it would silently reclassify a house with a detached garage, so it never flips scope by itself. The
user accepting the offer creates the child pins, and *those* flip it.

Consequences worth knowing:

- **Suppression is render-side only.** The CRIS/REData/Overture cache rows are per-`Location` and
  shared by every user pinning that place, whose own hierarchies differ — so `fetch()` caches the
  same payload regardless of scope and only `render_context()` branches. CRIS additionally caches
  any historic-district record under a `district` key, which a parcel-scope pin renders *instead
  of* a building.
- **CRIS media items are not suppressed.** Attachment photos are additive and clearly
  source-labelled; dropping a campus's entire CRIS photo set would be a regression.
- **Child markers classify themselves.** The detail-pin dialog's Type select defaults to "Auto"
  (a blank submission). `classify_detail_marker` generates the marker's own boundaries first, and
  a location with a generated `BoundaryType.BUILDING` polygon *is* on a building — the provider
  chain only fills that row when some provider has a footprint containing that exact point. A
  marker that isn't on one keeps its provisional Point of Interest type, which is right for the
  entrances and hazards users also drop.
- **The dialog only submits `pin_type` when it was touched.** Otherwise every autosave (a colour
  tweak, a drag) would mark an automatic classification as a user decision and freeze it.

## Plugin system rules

- Plugin classes (`dashboard/plugins/builtin/*.py`) are instantiated during `AppConfig.ready()`.
  **Imports and `__init__` must never touch the database or network** — real work belongs in the
  contribution objects (`PanelSource`, provider classes), which run at request/Celery time.
- A failure importing, instantiating, or calling any single plugin is caught, logged, and
  isolated — it never breaks startup or an unrelated request.
- API client code stays a `Gateway` subclass under `dashboard/services/apis/...` with a
  `service_key`; the plugin class is just the manifest wiring it into rate limiting, panels, and
  the admin inventory. Not every service has been converted to a plugin yet — unconverted ones
  still register defaults directly in `rate_limiter.SERVICE_REGISTRY` (see `TODO.md` UL-294).
- Name candidates from `NameProvider`s are quality-gated: address-derived fragments (street names,
  city names) and generically meaningless names are rejected before being persisted as aliases.

## Community counts are fuzzed, not exact

Wiki "how many people have this pinned" style counts (`services/community_counts.py`) are
deliberately fuzzed (small random jitter, cached for a day) rather than exact — an exact count
combined with a timeline could otherwise let someone infer individual pinning activity.

## Undo framework — do not "delete" through save()/post_save

The generic undo system (`services/undo/`, `models/undo/UndoAction`) stages deletions in cache
before they're finalized. Per-model handlers exist for pin, wiki, safety check-in, and trip.
Related but broader rule that bit this codebase before: **never call `.save()` inside a
`post_save` signal handler or in `__str__`** — it causes recursive-save bugs; use
`queryset.update()` for side-effect-free caching instead, and always set `dispatch_uid` on signal
connections. The project's linter (ruff) has previously stripped "redundant-looking" early-return
guards out of signal handlers — if a guard is load-bearing, make the code redundant enough that
the linter can't tell, rather than relying on the guard alone.

## Rate limiting and cost tracking

Every external API call should go through a `Gateway` subclass so it's covered by
`ApiRateLimit`/`ApiCallLog` (calls/min, calls/day, USA-only geo-filter where relevant, enabled
toggle). This is required groundwork for the still-unbuilt cost-reporting feature (`TODO.md`
UL-52/UL-53) — new integrations should track a running cost estimate per call even before that
reporting UI exists.

## Property-records discovery heuristics — the live incidents behind each rule

`services/apis/property_records/relevance.py` holds the pure accept/reject/ordering rules for
Tier 1 endpoint discovery; each carries a condensed rationale in code. The full incidents that
calibrated them (all confirmed against live county infrastructure, July 2026 — none invented
defensively):

- **Bare `land` excluded from the parcel-name pattern**: California's statewide `i15_LandUse_*`
  agricultural land-use/crop-classification layers (fields `CLASS1-3`, `CROPTYP1-3`,
  `WATERSOURC` — nothing about ownership or tax) matched on "landuse" containing "land" for two
  unrelated counties (San Francisco, Sacramento). "Land Records"/"Land Ownership" remain matched
  as exact phrases.
- **Canonical titles vs. loose matches**: Athens County, OH publishes both "Parcels"
  (address/owner/assessed-value fields) and "Mineral_Parcels" (subsurface mineral-rights
  boundaries with none of those fields) side by side — both match the loose pattern equally, so
  which won was API response ordering until canonical-title preference existed. In the other
  direction, a real layer whose service name was `Statewide_CoastalZoneBoundary_Cadastral`
  (coastal regulatory boundary, zero parcel fields) matched purely on "cadastr" — hence a loose
  name match now also needs at least one corroborating field. The rule that *any* name match
  suffices was itself a fix for Loudoun County, VA's public-schools sites layer (`LCPSSITES`,
  fields `SCH_CODE`/`CLASS`) validating purely because it responded like a real ArcGIS layer.
- **Stale-title deprioritization**: Washington State's statewide publisher (WAGeoservices)
  maintains both "Previous Parcels" (last year's snapshot) and a fresher "Current Parcels";
  nothing structural distinguishes them, so without the marker the winner was list-order luck.
  Deprioritized, never rejected — a whole-county snapshot from last year still beats nothing.
- **Narrow-subset URL slugs rejected outright**: Cook County, IL's only ArcGIS-discoverable
  "parcels" layer was a regional sub-agency's tax-delinquency tracker (36K of the county's 1.8M+
  parcels, no owner/address fields); Guilford County, NC's was an agricultural-preservation
  program's enrollment list (21K of ~200K+, slug `..._Agricultural_Districts_VAD_Parcels`).
  Unlike staleness these are worse than nothing: a tiny, unrepresentative slice.
- **Non-production markers (`test`/`staging`/`demo`/`sample`), word-bounded against
  separator-normalized text**: Carver County, MN's only discoverable candidate lived under a
  service literally named `..._Public_Parcel_App_Test`, with genuinely flaky schema (fields
  present on some fetches, `null` on others). Underscores are word characters to Python's regex
  engine, so `\btest\b` never fires inside `..._Test/` without normalizing separators first.
- **Feature-count floor (2,000)**: calibrated upward twice from live false accepts — a
  one-feature "Property Boundary" layer with genuine assessor fields; a 121-row stray upload
  literally named "Parcels" for a major metro county; the 1,153-row Williamson Act
  agricultural-contract registry (Santa Clara, CA); a 588-row highway-widening project extract
  (`U2524_Final_Parcels`) with real owner/address/value fields.
- **Wrong-jurisdiction title checks**: a Skagit County, WA search surfaced someone's personal
  `..._thurston_county` export of the statewide dataset, and "Florida Statewide Parcels" tied
  with the correct Washington item (neither names a county, so only a state-level check can
  separate them). County-level stays a ranking signal only (too noisy to reject on — a same-named
  county can genuinely exist in-state, or a metro-area page can mention a neighbor in passing); a
  match immediately followed by "County" is ignored because 30+ states' names are also county
  names elsewhere ("Washington County Parcels, Minnesota" is not about Washington state).
  Originally applied only to the portal-search path; a later live round found the *original*
  web-search+regex step (`_rank_candidates`) had no jurisdiction check at all - a "Douglas County,
  NE" search validated Douglas County, *Oregon*'s real, working endpoint, because Oregon's own
  site (whose own page title literally says "Douglas County, OR") is simply better-indexed for the
  query text than Nebraska's. Fixed by extracting candidates per search result (not from one
  merged blob) so each URL keeps its own result's title/snippet text for the same check - but the
  bug *recurred* even after that fix: the fixed query still returned zero Nebraska candidates for
  the regex step to extract at all (Nebraska's real results were ArcGIS Hub landing pages, not raw
  pasted REST URLs), so demoting Oregon behind a nonexistent rival didn't help. State-level
  mismatches (unlike county-level) are precise enough - full state name or the "City/County, ST"
  comma-abbreviation convention, case-sensitive so the literal word "or" is never misread as
  Oregon - that a confirmed one is now a hard rejection, not just a demotion. Root cause of the
  Douglas NE/OR incident itself: the deterministic query template used the literal word "OR" as a
  plain-English "either/or" connector (`"...ArcGIS REST OR Socrata..."`), but every provider here
  does keyword matching, not boolean search, so "OR" was just another term to match - and it
  collides with Oregon's own postal abbreviation, which appears throughout every Oregon county
  government page's own title/URL. Rewritten to avoid the literal word entirely.
- **Wrong-jurisdiction data with no informative name, caught by geography instead**: a Boone
  County, MO search's portal-search fallback accepted a layer plainly named "Parcels" (canonical,
  trusted alone) living inside a service named `NicholasWV_AGOL`, owned by `nicholas_assessor` -
  Nicholas County, *West Virginia*'s own data, surfaced by AGOL's item-search for an unrelated
  "Boone County Missouri parcels" query. Nothing in that service's own title says "County" or
  spells out a full state name, so the title-based checks above had nothing to match against - the
  same class of gap once documented as a deliberate, unaddressed residual limitation. Closed not
  by chasing name-text further but by cross-checking the candidate's own ArcGIS `extent` against
  the target county's real extent (Census TIGERweb, free/keyless, already used elsewhere in this
  codebase) - a check no misleading or uninformative name can evade. The two boxes are compared in
  whatever spatial reference the *candidate* reports (a live one used NAD83 Missouri state plane,
  wkid 26854, not the WGS-84/Web-Mercator pair that would've been tempting to hardcode) by asking
  TIGERweb to reproject the county's extent into that same wkid server-side, rather than this
  codebase inverting an arbitrary projection itself.
- **Portal search results aren't guaranteed relevant at all**: the same Boone County, MO query
  also surfaced (among only three total results) a one-off watershed-analysis dataset (item title
  `GBFW_data_20240926`, snippet "Data for the Greater Bonne Femme Watershed analysis 2024") that
  happened to contain a sub-layer plainly named "Parcels" - genuinely within Boone County's extent
  (so the geography check above didn't catch it) and just over the feature-count floor (2,452
  rows, a small study-area extract, not the whole county). AGOL's item search is a fuzzy free-text
  match across title/tags/description, not a relevance guarantee. Fixed with a pre-probe filter:
  the portal item's own title or snippet must itself mention parcels/assessment data before any
  live request is spent on it - unlike a leaf layer's name (trusted alone when canonical; a real
  county's actual parcel layer is routinely just named "Parcels" with nothing else distinguishing
  it), the *container item* surfaced by search has no such excuse.
- **Narrow-subset markers keep growing**: Kent County, MI's search accepted a 3,793-row "Parcel
  Status from February 2020 Consent Decree" layer (a groundwater-contamination litigation
  tracker - real Kent County parcels, but a sliver of its ~220K total). Its own title says
  "Parcel", so the portal-item-plausibility filter above correctly let it through; only a
  narrow-subset marker could catch it, so `consent.?decree` joined the existing
  delinquency/easement/agricultural list.
- **Bounding-box overlap is too loose for adjacent jurisdictions**: a New Castle County, DE
  search accepted a real, comprehensive, but wholly wrong dataset - *Chester County, PA's* actual
  194K-row parcels layer - because Chester's own extent happens to reach down near the PA/DE
  border, giving the two neighboring counties' bounding boxes a sliver of rectangle overlap
  despite Chester's data never covering Delaware at all. `extent_overlaps_county` originally
  tested plain bbox-intersection (built for the Nicholas County, WV incident above, where the two
  states are nowhere near each other); redefined to test whether the *target county's own
  centroid* falls inside the candidate's extent instead - still passes every legitimate match
  (a real county's own layer obviously contains its own centroid) and every legitimate statewide/
  regional layer (which contains every in-state county's centroid too), but correctly rejects a
  neighbor whose extent merely brushes the border.
- **Narrow-subset markers keep growing, round 2**: a Mecklenburg County, NC search accepted a
  2,701-row "Park Parcels" layer (parks-department-owned land only, fields
  `park_type`/`park_distr`/`bondsource`) - one coincidentally-matching field (`parcelid`) cleared
  the tier-1 corroboration bar. `park.?parcels`/`parks.?parcels` joined the marker list as a
  *phrase*, not a bare `park` token - deliberately, since "Park County" is a real jurisdiction
  (CO/MT/WY) whose own genuine parcels dataset must not collide with the rule.
- **Known residual gap** (deliberate): a sub-floor-clearing partial can still slip through
  (Franklin County, OH's 9K-row `Parcels_2022_01` likely is one) - the durable answer is the
  human-review workflow around `discovered_by`/`last_verified`, not an ever-taller heuristic
  stack. Socrata resources have no comparably cheap `extent` to geography-check, so they still
  rely on title-text checks alone.

Related, same package: Socrata's `distance_in_meters()` SoQL function is not supported on every
backend and fails the *entire* query with a 400 (observed on New Orleans' parcels resource) —
the Tier 1 gateway's point query deliberately has no `$order` clause. ArcGIS Online item-search
matches the spelled-out state name far better than the USPS abbreviation ("Athens County Ohio
parcels" vs. zero results for "...OH parcels"), and quoted phrases in the web-search discovery
query returned zero results from Brave — both query templates are deliberately un-"cleaned". The
web-search query template also deliberately avoids the literal word "OR" (see the Douglas
County, NE/OR incident above) - every provider here does plain keyword matching, so a
boolean-style "REST OR Socrata" connector is read as just another term, and it collides with
Oregon's own postal abbreviation.

## External API keys (dashboard/external_api/)

Inbound-facing, unlike everything else in "Rate limiting and cost tracking" above (which covers
*outbound* calls to third-party APIs). A few deliberate choices worth knowing before touching it:

- `ApiKey.prefix` is stored in plaintext specifically so `authenticate_api_key` can look up the
  owning row before hashing - Django's password hasher is intentionally slow, and unlike backup
  codes (bounded at ~10/user), a user can accumulate arbitrarily many keys over time. Never make
  `authenticate_api_key` iterate every active key's hash to find a match.
- Every key currently gets the same fixed `scopes` grant (`ApiKeyScope.PROFILE_READ` +
  `ApiKeyScope.PINS_WRITE` - see `models/account/model.py`). There's no scope-picker UI yet; the
  field exists as a real per-row value (not an implicit "any valid key can do everything"
  assumption) so a future picker only has to change what gets written at creation time, not the
  verification path in `external_api/permissions.py`.
- Pin creation from the external API goes through the exact same
  `services.pin_creation.create_pin_for_profile` call as the map UI's "Add pin" form (see
  `controllers/maps.py`) - this is intentional, not incidental reuse. Any validation/sanitization
  added to one caller must go in that shared function so it automatically covers the other.
- `external_api/` never imports from - or gets imported by - the internal viewsets under
  `models/*/viewset.py`. It has its own auth (`ApiKeyAuthentication`, bearer token, never added to
  `DEFAULT_AUTHENTICATION_CLASSES`) and its own throttle scope (`external_api_key`, per-key rather
  than per-user).

## Windows development environment quirks

- The venv is `.venv_windows\` (not `.venv`) because it was created on Windows — always invoke
  tools via `.venv_windows\Scripts\<tool>.exe`.
- GeoDjango's GDAL/GEOS dependency on Windows is satisfied via DLLs vendored by `geopandas`'s
  `pyogrio` dependency, resolved in `settings/_gdal_windows.py`. This only applies when
  `UL_ENVIRONMENT=local` (the default for local dev) — it is never invoked in Docker/CI/production,
  so don't "fix" GDAL issues there using the Windows path.
- Docker is not run from within Claude's environment — if Docker needs to be exercised, ask the
  user to run it manually rather than attempting `docker-compose` commands directly.
- Sass compiles fine natively on Windows via `bun run sass` — no Docker needed for frontend asset
  builds.

## Migrations churn on squashes

Django's `CreateModel` operation defers index creation to the end of a migration, but
`RenameIndex` executes immediately in migration order. Squashing migrations that rename an index
created earlier in the same squash will fail against a fresh database (works fine against an
already-migrated one, which is why it's easy to miss in review). Fold any such rename into the
`CreateModel`'s `Meta.indexes` instead of leaving a separate `RenameIndex` step. This recurs every
time migrations get re-squashed.

## Testing

- Custom test runner (`urbanlens.core.tests.runner.TestRunner`) suppresses log output on passing
  tests and surfaces it only on failure.
- `@given` (Hypothesis) and Django's `self.client` don't mix cleanly in this repo's `TestCase` —
  prefer calling the view/service function directly under `@given`, or drop Hypothesis for that
  particular test. TODO NOTE From Jess: We should probably fix TestCase so it does work cleanly.
- Don't write unit tests asserting an exact log message string — trivial wording changes then
  break tests for no functional reason.
