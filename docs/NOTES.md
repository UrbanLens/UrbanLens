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
`services.wiki.wiki_access.resolve_visible_wiki(request, location_slug)` (or check
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

## Label names are unique per owner and kind, case-insensitively

Since migrations 0042/0043, `Label` carries
`UniqueConstraint(Lower("name"), "profile", "kind", nulls_distinct=False)`. Three consequences that
are not obvious from the model:

**It is case-insensitive.** "Abandoned" and "abandoned" are the same label. This matches what
callers already assumed - `services/media/media_labels.py` pre-filtered with `name__iexact` because
`get_or_create(name=...)` alone is case-sensitive and the intended identity was not. Any lookup that
feeds a create must use `name__iexact`, or the `get` misses an existing row, the insert violates the
constraint, and `get_or_create`'s own retry (which repeats the same exact-match `get`) cannot
recover.

**Global labels are constrained against each other.** A global label has `profile IS NULL`, and
Postgres treats NULLs as distinct by default - so without `nulls_distinct=False` two identical
global labels would still be possible. That flag needs Postgres 15+; this project runs 17.

**Shadowing a global label is refused by the application, not the database.** A personal label and a
global label with the same name differ in `profile`, so the constraint permits both. The check in
`services/labels/uniqueness.py` is deliberately wider and refuses it, because two identically-named
labels in one list are indistinguishable to the user. Migration 0042 merged the pre-existing ones
into the global label, which survives.

Every write path checks `find_conflicting_label` *before* writing and returns a message (HTML views
400, external API 409, undo-restore refuses) - reaching the constraint means a 500, so the check is
the interface and the constraint is the backstop.

For tests: a new `Profile` is seeded with ~46 default labels, so `Label.objects.create(name="Hospital",
kind=KIND_CATEGORY)` now raises. Use `core/tests/labels.ensure_label()` when the fixture wants "a
label with this identity", and a fresh name when it genuinely needs a *new* row - `ensure_label`
returns the seeded one, which fires no create signal and already has parents.

## Pin slugs are scoped per-profile, not global

`Pin` has `UniqueConstraint(fields=["profile", "slug"], condition=Q(slug__isnull=False))` — slugs
are unique *per user*, not site-wide. Code that looks up a pin by slug alone (without also
filtering by profile) will silently return the wrong user's pin if two users have pins with the
same slug. This has been a recurring source of bugs in upload/gallery/weather endpoints — always
scope slug lookups by the owning profile.

There's also `UniqueConstraint(fields=["location", "profile"], condition=Q(parent_pin__isnull=True))`
— a user can only have one top-level pin per Location (sub-pins via `parent_pin` are exempt).
TODO NOTE From Jess: I could be mistaken, but I think there shouldn't be an exception for sub-pins. Sub-pins will be nearby, of course, but the coordinates won't be exactly, precisely the same. This exception allows for two pins to precisely overlap on a map, which surely not very helpful.

## Matching reads `Place.geometry`, and nothing else

Official geometry lives on `Place`, written only by the provider chain and boundary voting. The
`Boundary` table is now *only* user- and community-drawn shapes (plus per-provider voting
candidates), and no access or matching path reads it at all — so "a drawn polygon can never widen
what you can see" is a property of the schema rather than a filter every query has to remember to
apply. `Boundary.objects.resolve_for_*` still consults those drawn rows, because they are for
display.

## Place is the answer to "is this the same place?"

`Location` is an exact coordinate; `Place` is the real-world parcel or building that coordinate
stands on, and it is what wikis, official geometry, boundary votes, and access all hang off.
`Location.place` is a *resolved cache*, recomputed whenever geometry changes — never an identity,
so a provider correcting a boundary can move a location between places without disturbing pin,
share, or wiki provenance.

Before Place, geometry hung off each Location and was fetched by point lookup. Importing 124
buildings onto one campus therefore created 124 Locations each holding its own copy of the *same
parcel polygon* — so every point on the campus sat inside 125 boundaries at once. That one cause
produced three separate-looking bugs: visitors told that 124 other locations covered their pin, a
building's page drawing the whole parcel, and one property accumulating 125 wikis.

Load-bearing details:

- **Most specific wins**, ordered by cached `area_sqm`. A footprint is always smaller than the
  parcel around it, so area ordering subsumes "deepest in the tree" without walking it, and stays
  deterministic for two overlapping unrelated parcels.
- **`Place.geometry` is nullable.** A building nobody has a footprint for still gets a place: it
  keeps identity, lineage, and its own wiki, and can never be resolved onto.
- **Aggregates are excluded from resolution entirely** (`PlaceQuerySet.resolvable`), which is what
  makes strict earning unbypassable rather than merely unlikely — a pin in a gap between a site's
  parcels cannot land on the site itself.
- **A placeless coordinate behaves exactly as it always did**: circle fallback for display,
  exact-Location pin for wiki access.

## Access is per *domain*, and only `MEMBER_OF` edges gate anything

A **domain** is a parcel plus everything `PART_OF` it, denormalised onto `Place.domain_root` so the
predicate is one indexed equality test. It is indivisible: a pin anywhere in it grants every wiki
in it, in *either* direction. Organising a property into its 124 buildings is an organisational
act and must not change who can see what — the alternative hides content from people who already
had the property, purely because someone pressed a button.

`MEMBER_OF` (a split's superseded campus, or a site spanning several tax parcels) is the only
access boundary. Such a parent is earned only by holding access to *every* member, because its
knowledge genuinely exceeds any one child's. Earning is recursive over *access*, not over literal
pins, so completing one tier can complete the tier above it.

`PlaceAccessGrant` covers what containment can no longer prove. It is written by exactly two
callers — the Place backfill migration and split processing — and by no API surface. That
snapshot is what makes automatic split and site detection safe: a false positive costs a redundant
row, never someone's access.

Consequence worth knowing: a newcomer who pins one building of a *multi-parcel* campus gets that
parcel's domain but not the campus, and sees no hint the campus wiki exists. That is the strict
rule working as intended; existing holders keep it via backfill grants.

## Parcel vs. building scope — derived from the place, not asserted per pin

A Pin (and its Wiki) doubles as both *the parcel* and *the building* for an ordinary house, because
those are the same thing there. On a campus they are not: the pin at `41.73315, -73.93037` was
rendering "TOOL SHED (1937) — NON-CONTRIBUTING, Building Number 154" from NY SHPO's CRIS inventory,
because `CrisBuildingPanelSource` took the *first* `resource_type == "building"` match inside a
200 m radius. `services/places/scope.py` is the one place that decides which a marker is, and
`site_scope.is_site_scope` reads it.

Rules, in order. **An explicit choice wins**: `pin_type_is_user_provided` marks a type the user
actually picked, mirroring how `name_is_user_provided` guards `Pin.name`. **Otherwise it is derived
from the resolved place**: a marker on a property holding `MULTI_BUILDING_THRESHOLD` or more
buildings commits to describing the grounds or one structure; a marker on a single-building
property stays `LOCATION_MARKER`, which is not "unknown" but the honest answer.

`Pin.pin_type` / `Wiki.pin_type` are caches of that derivation, refreshed by
`site_scope.reclassify_markers_on_place`. Deriving from the place and fanning out matters: scope is
a fact about the *property*, so importing buildings retypes **every user's** marker on it, not just
the marker belonging to whoever pressed the button — and it can't drift when buildings are added or
removed later.

What is deliberately *not* a rule: "REData says this parcel has several buildings." That signal
drives the "would you like to add pins for the buildings here?" offer, but on its own it would
silently reclassify a house with a detached garage. Accepting the offer creates the building
*places*, and those flip it.

**What each scope draws** (`services/places/scope.py::place_polygon`) — the answer to "a building's
page shouldn't show the parcel":

| Marker | PROPERTY | BUILDING |
|---|---|---|
| Building on a multi-building property | *nothing* | its footprint |
| Building on an ordinary property | its parcel | its footprint |
| Parcel / site | its outline | *nothing* |

The two "nothing" cells are the point. A building on a campus is not about the 200 acres it sits
on, and a campus marker must not pick one of its 124 structures to represent it.

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

## The restructure suggestion is one dialog, and it is offered exactly once

`services/pins/pin_restructure.py` answers two questions that are really one — "this pin's hierarchy
doesn't match the ground" — so they share a single prompt on the pin detail page rather than
interrupting twice:

1. buildings on this property with no child pin yet, and
2. the owner's own *top-level* pins standing inside the property boundary, which is what a map built
   before child pins existed looks like.

Load-bearing details:

- **Matching an existing marker to a building prefers the footprint polygon**, not a radius. REData
  publishes real outlines (`geometry`) for most buildings it knows; a 15 m radius from the centroid
  both misses a pin at the far end of a long hall and wrongly claims one standing on the neighbour.
  `site_scope.BUILDING_MATCH_METERS` is now only the fallback for sources publishing a bare centroid.
- **Nesting requires a *real* property boundary.** `Boundary.effective_polygon_for_pin` synthesizes a
  50 m circle when nothing is known, and suggesting that every pin within a city block belongs to a
  house would be worse than suggesting nothing — hence `property_polygon()` rejecting `source ==
  "circle"`.
- **Three answers, two scopes.** "No" sets `Pin.restructure_offer_dismissed` permanently for that pin
  (new buildings or new matching top-level pins never revive it). "Don't show again" *also* clears
  `Profile.suggest_pin_restructure`, and still marks the pin — so turning the setting back on later
  doesn't resurrect the prompt on the one pin they explicitly declined.
- **The plan is recomputed on apply**, never trusted from the rendered page, so anything pinned or
  nested between render and click is simply skipped.
- **The import persists each building as a `Place` first**, from the footprint it already holds,
  and attaches each new child pin's Location to its place directly rather than resolving by
  containment. Two reasons: a point lookup for a building returns the *parcel* when no footprint
  provider covers it (which is how 124 markers each ended up claiming to be the whole hospital),
  and a building centroid can legitimately fall outside its own concave footprint.
- **A spent poll budget still offers what's known.** The nesting half doesn't depend on the REData
  lookup, so a slow/unavailable parcel fetch degrades to "offer the nesting" rather than hiding the
  whole suggestion.
- The "Buildings on this Property" panel keeps its own buildings-only import button, which ignores
  the dismissal entirely — that one is an action the user went looking for, not an interruption.

## Wiki-to-wiki auto-merge is the one hierarchy change with no confirmation dialog

`services/wiki/wiki_merge.py` is the exception to this codebase's usual "ask first" pattern for
structural changes. Two community wikis are independent, user-initiated pages - nothing stops
someone wiki-ing a building before anyone's wiki-ed the campus it sits on. Once both resolve onto
places, `reconcile_wiki_nesting` re-parents the child's wiki under the ancestor's automatically,
per the ROADMAP's explicit "without needing user confirmation." It runs from the single choke
point every place-resolution call site shares (`services.locations.boundaries.
generate_location_boundaries`), checking both directions - does an ancestor place have a wiki, and
do any of my direct children have root wikis - so it converges regardless of creation order.

Nesting follows **place lineage**, not geometry: the hierarchy was already decided when the places
were provisioned, so this walks one FK chain instead of sorting every containing polygon by area,
and it cannot disagree with the access model, which reads the same edges. A wiki on a coordinate no
provider knows has no lineage to walk, so it falls back to containment against official place
outlines (`_containing_root_wiki_by_geometry`).

Load-bearing details:

- **Merging is only ever `parent_wiki = X`.** `Wiki.location` is a OneToOne that never moves, so no
  Pin, Article, comment, or WikiEdit history needs to follow - unlike a pin-hierarchy change, there
  is nothing else to touch.
- **A synthesized circle boundary can never trigger it** - `wiki_property_polygon` rejects
  `Boundary.objects.resolve_for_wiki`'s `"circle"` source, exactly like `pin_restructure.
  property_polygon` does for the pin-nesting suggestion. Otherwise every wiki within 50 m of a
  bigger one would silently become its child.
- **The candidate parent search is root-wikis-only** (`parent_wiki__isnull=True`), not "any
  ancestor" - this is what keeps the cycle check trivial (a root candidate can never be a
  descendant of the wiki being reconciled). The multi-level case (building → wing → campus) still
  resolves to the tightest fit in one pass, because reconciling the *middle* wiki's own boundary
  runs both directions at once: it finds the campus as its own container, and finds the building
  already sitting inside its own polygon, in the same call.
- **The wiki object is re-fetched at the top of `reconcile_wiki_nesting`**, never trusted from the
  caller - a stale in-memory `parent_wiki_id` (e.g. set moments earlier by a *different* wiki's own
  reconciliation absorbing this one) would otherwise let direction 1 re-run and silently overwrite
  a just-established, tighter parent with an outer one. Every real call site already passes a
  freshly loaded wiki, so this is a no-op extra read in production and a guard against a future
  caller that doesn't.
- **No admin UI to reverse a merge yet** - it's an audit trail only (`WikiEdit` on the parent,
  `editor=None`). See `docs/PROBLEMS.md`.

## Sub-pin data is never hidden by nesting - but each surface aggregates independently

The pin detail page's "show sub pin details" toggle (`?children=1`) is not one mechanism - every
aggregating view (map markers, photo gallery, visit history, and now Notes/comments) independently
swaps its own queryset from `pin.<related>` to `<Model>.objects.filter(pin__in=Pin.objects.
filter(pk=pin.pk).with_descendants())` when the flag is set, and independently threads
`extra_query="children=1"` through its own pagination. There is no shared helper - see
`controllers.comments._pin_comments_context` for the newest one, modelled directly on
`controllers.visits._render_visit_history`. `services.pins.pin_restructure`/`services.pins.pin_wiki_sync`
cover pin ↔ external-data and pin ↔ wiki *hierarchy* fixes; this is purely about *display* -
aliases and labels are not yet aggregated this way (tracked in `docs/PROBLEMS.md`).

A comment/note's delete button always posts back to the **top-level pin's** URL regardless of which
descendant it actually lives on (see `_comment_body.html`'s `{% url 'pin.comment.delete' pin.slug
comment.id %}`), so `PinCommentDeleteView.delete` resolves the comment by searching the whole
subtree, not just the exact pin in the URL - otherwise deleting an aggregated child's note 404s.

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

Wiki "how many people have this pinned" style counts (`services/wiki/community_counts.py`) are
deliberately fuzzed (small random jitter, cached for a day) rather than exact — an exact count
combined with a timeline could otherwise let someone infer individual pinning activity.

## Achievements — awards are permanent, and streaks are judged on read

Three non-obvious rules hold the achievement system together:

**Awards are never revoked.** `evaluate_profile` only ever grants. Deleting pins drops
`pins_created` below the threshold, and the award stays. That is why there are no `post_delete`
handlers in `models/achievements/signals.py` — their absence is deliberate, not an oversight.

**Streak achievements compare against `longest_length`, never `current_length`.** Breaking a
streak must not take back the award it earned. Separately, `current_length` is only ever
*advanced* — nothing fires on the day a user stops — so it is stale by construction. Always read
it through `ProfileStreak.current_length_as_of()`, which returns 0 once `last_day` is more than a
day old. The stored column is not the live value.

**One activity row per day is what makes streaks idempotent.** `record_activity` is safe to call
on every write because `ProfileActivityDay` is unique on (profile, kind, day); only the first call
of a day advances the streak. Do not "optimise" that `get_or_create` away — uploading fifty photos
in an afternoon would then count as fifty streak days.

Two things about the write path. Streak days are recorded **synchronously**, inside the
contributing transaction, not in the Celery task — streaks are the only metric with no source of
truth outside our own tables, so the day has to be written even when no streak award exists yet,
or an award added later would have nothing to reward. The *evaluation* enqueue, by contrast, is
gated on `active_metric_keys()`: if no active award measures the affected metric, nothing is
queued at all. That gate is deliberately **uncached** — caching it made write-path behaviour
depend on whatever ran before it, because a rolled-back transaction leaves no signal to
invalidate on.

Two more things that bite: metric keys are stored on `Achievement.metric`, so renaming one in the
registry orphans every award pointing at it (needs a data migration). And `Achievement.metric`
takes its `choices` as a *callable* precisely so registering a new metric does not generate a
migration — don't "simplify" it to a literal list.

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
  `services.pins.pin_creation.create_pin_for_profile` call as the map UI's "Add pin" form (see
  `controllers/maps.py`) - this is intentional, not incidental reuse. Any validation/sanitization
  added to one caller must go in that shared function so it automatically covers the other.
- `external_api/` never imports from - or gets imported by - the internal viewsets under
  `models/*/viewset.py`. It has its own auth (`ApiKeyAuthentication`, bearer token, never added to
  `DEFAULT_AUTHENTICATION_CLASSES`) and its own throttle scopes (`external_api_read`,
  `external_api_write`, `external_api_burst` - per-credential rather than per-user). A request's
  tier is derived from the view's own `required_scopes_by_method` declaration: any required scope
  ending in `:write`/`:manage` makes it a write, and a method with no declaration is treated as a
  write so a forgotten declaration fails into the tighter bucket. The burst cap applies to every
  request on top of whichever hourly cap matched.
- The external API's scope vocabulary lives in `ApiKeyScope` (`models/account/model.py`) and is
  mirrored verbatim into `OAUTH2_PROVIDER["SCOPES"]`; settings load before the app registry, so
  they cannot import the model, and `test_external_api_scopes` guards the mirror against drift.
  `_default_api_key_scopes()` deliberately stays at the original four values - widening it (or
  backfilling existing rows) would silently expand every already-issued key's grant to messages,
  photos and safety check-ins that its owner never consented to. New scopes are opt-in, reachable
  via OAuth2 where the consent screen enumerates them. `permissions.OAUTH2_ONLY_SCOPES` goes
  further and refuses the `messages:*` scopes to PAT-style keys outright.

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

## Georeferenced map image overlays

`MapImageOverlay` stores **four WGS-84 corners**, not a transform matrix. The matrix is recomputed
client-side on every map move from those corners
(`frontend/ts/shared/map-image-overlays.ts:matrix3dForCorners`), which is why an overlay stays
correct across zoom levels, base-layer switches, and would survive the rendering ever moving off
Leaflet. Storing a matrix would bake in one particular projection and pixel origin.

Rendering is a plain `<img>` under a CSS `matrix3d` rather than `L.ImageOverlay`: Leaflet's own
overlay only accepts axis-aligned bounds, which cannot express rotation, shear, or the trapezoidal
distortion a flatbed scan of an old sheet actually has. The homography solve is deliberately
in-repo (~70 lines) rather than a `leaflet-distortableimage` dependency, so the corner semantics
stay ours.

Two things that look like they could be simplified but can't:

* `.ul-map-overlay-image` must keep `transform-origin: 0 0`. The homography is solved against the
  image's natural pixel rectangle starting at (0,0); any other origin silently shifts every corner.
* A degenerate quadrilateral (three corners dragged onto one point) makes the 8x8 system singular.
  `matrix3dForCorners` returns null there and the caller keeps the previous transform - applying a
  NaN matrix would make the overlay vanish with no handle left to drag it back.

`?preview=1` is not involved here: an overlay's image must be something a browser renders directly,
which is why the external-URL path rejects PDFs/TIFFs and tells the user to upload instead (the
upload path runs through the normal media pipeline).
