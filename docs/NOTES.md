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
  priority, status, last-visited date. Address/place metadata is read via proxy properties that
  delegate to `self.location`; it is never stored directly on `Pin`.

  **A pin has no coordinates of its own.** `Pin._meta` carries no latitude/longitude field at all,
  and `pin.effective_latitude` returns `float(self.location.latitude)` — so a pin's position *is*
  its location's position, exactly, always. (This line used to claim "marker coordinates" among a
  pin's own fields, which cost a session's worth of wrong reasoning; see the detach entry below.)
- **`Wiki`** — an *opt-in*, community-editable page about a `Location`. Not every Location has
  one; users seed them explicitly. Keeping Wiki opt-in was a deliberate privacy decision — see
  "coordinate immutability" below for the related concern about leaking exact locations, and
  "Wiki visibility" below for who is actually allowed to see one once it exists.

`Location.lat`/`long` are **immutable after insert** (enforced by both `Location.save()` and a
DB trigger). Address components stay mutable to allow geocode backfill. Use
`Location.objects.get_nearby_or_create(...)` rather than constructing new Locations directly when
coordinates might already exist nearby.

**To move a pin or a wiki, relink it to a different `Location` — never change the coordinates of
the one it currently points at.** The two rules compose into a thing worth stating outright,
because it is not obvious from either alone: since a pin has no coordinates of its own, and a
location's cannot change, "give this pin its own place at the same point" is not expressible at
all. That is why there is no "detach from this location" action — the only coherent way to stop
sharing a place's record is to relink to a different place, or move (see
`controllers/pin_edit.PinRelinkView`, and the 2026-08-13 entry in `docs/PROBLEMS.md`).

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

## Child pin and child wiki slugs carry a parent prefix

A child pin or child wiki slug is `{parent-prefix}-{name}`, not a bare slugify of the child's
name. The prefix is the shortest existing alias that is already compact (3–8 characters after
slugify), or — when every alias is still too long — one derived from the parent's canonical name:
initials of significant words (`Hudson River State Hospital` → `hrsh`), the first word when the
initials are too short (`Ford Motors` → `ford`), or a truncation of that word when even the first
word is too long (`Switzerland` → `switz`).

Truncation drops whole trailing words rather than clipping mid-word. Hyphenated compounds
(`non-contributing`) are one word, so `Staff/Tenant House 1900 (non-contributing)` under HRSH
becomes `hrsh-stafftenant-house-1900`, not `…-non-contributi`. Dropped words are added back only
to make a collision unique or a too-short result long enough. Existing slugs are not rewritten.

Wiki pages are routed by `Location.slug`. A child wiki whose location still has a UUID fallback
slug copies the wiki's prefixed slug onto the location so the URL matches.

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
doesn't match the ground" — so they share a single prompt on the Private Pin page rather than
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

The Private Pin page's "show sub pin details" toggle (`?children=1`) is not one mechanism - every
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
  still register defaults directly in `rate_limiter.SERVICE_REGISTRY` (see the repo-root `ROADMAP.md`, UL-294).
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

The generic undo system (`services/undo/`, `models/undo/UndoAction`) stores the serialized
payload needed to restore or redo an action directly on the `UndoAction` row itself, not in a
cache: a cache entry can vanish well before its nominal TTL (no shared Redis/Valkey configured,
so a locmem cache other workers can't see; or early eviction under memory pressure), which used
to surface as an undo entry that still listed as recent and un-expired but silently failed the
moment it was actually restored. A dozen per-model/mutation handlers exist under
`services/undo/handlers/` (pin, wiki, trip, label, label membership, saved filter, pin list,
safety check-in, markup map, plus `_mutation` variants for pin/wiki/photo field changes) - see
that package's docstrings for exactly what each does and doesn't restore.

Both `restore_undo_action` and `redo_undo_action` claim the row under `select_for_update()` and
check `undone_at`/expiry *after* acquiring the lock, so a double-submit (a retried request, a
race between two tabs) always leaves exactly one winner - the loser gets `UndoAlreadyRestoredError`
rather than double-applying. See `tests/hypothesis/test_undo_restore_is_single_use.py` and its
`test_undo_redo_is_single_use.py` sibling, which both prove this with a real two-instance
double-submit rather than just asserting on the code shape.

Related but broader rule that bit this codebase before: **never call `.save()` inside a
`post_save` signal handler or in `__str__`** — it causes recursive-save bugs; use
`queryset.update()` for side-effect-free caching instead, and always set `dispatch_uid` on signal
connections. The project's linter (ruff) has previously stripped "redundant-looking" early-return
guards out of signal handlers — if a guard is load-bearing, make the code redundant enough that
the linter can't tell, rather than relying on the guard alone.

## One stored file can back several `Image` rows

Two independent features point more than one row at the same storage key rather than duplicating
bytes: sharing a pin copies its photos by reusing the name
(`services/sharing/pin_sharing.py`), and a deduplicated upload reuses both the original *and* its
thumbnail (`services/photos/uploads.attach_deduped_copy`, which also deliberately does not charge
quota a second time).

The consequence is easy to miss and expensive to get wrong: **anything that deletes or replaces a
stored file must first ask whether another row still needs it**, via
`services.media.images.file_still_referenced`. Deleting unconditionally does not error - it leaves
some other profile's photo pointing at nothing, with a broken image and no trace of why.

Three places do this today, and they are the three that touch stored bytes: `delete_stored_file`
(row deletion, which also takes the pks being removed in the same batch, or a bulk delete would
never remove anything), `downscale_stored_image` (re-encode / EXIF strip), and
`write_image_thumbnail` (preview regeneration). A new one belongs on that list.

Note this defers rather than skips cleanup: `strip_exif_from_stored_photos` walks every row, so the
last row referencing an old file is the one that removes it. The file still goes; it just goes when
nothing needs it.

## Rate limiting and cost tracking

Every external API call should go through a `Gateway` subclass so it's covered by
`ApiRateLimit`/`ApiCallLog` (calls/min, calls/day, USA-only geo-filter where relevant, enabled
toggle). This is required groundwork for the still-unbuilt cost-reporting feature (the repo-root `ROADMAP.md`,
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
- **Why `manage.py test` fails locally but works in CI.** The advice elsewhere is "use pytest, never
  `manage.py test`", and the symptom is real - `ValueError: Missing staticfiles manifest entry` on
  any test that renders a page (measured: 28 of 33 errors in one module). The reason is worth
  knowing, because it is not "the runner never sets `TESTING`". It does, in
  `setup_test_environment()`. But `STORAGES` is computed **at settings-import time** from `TESTING`
  (`settings/base.py`), and the runner's hook runs after that, so the manifest storage has already
  been chosen. `pytest` avoids it because `TESTING` also checks for `pytest` in `sys.argv`, which is
  true before settings are read.

  CI is unaffected and is *not* misconfigured: `.github/workflows/ci.yml` sets
  `DJANGO_SETTINGS_MODULE=urbanlens.UrbanLens.settings.test`, and that module sets `TESTING = True`
  at import - before `STORAGES` is decided. So `manage.py test` under `settings.test` is fine, and
  only the default settings module hits this. (Established 2026-08-17 while checking whether CI's
  Django step was silently broken; it is not.)
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

## OpenHistoricalMap: cache aggressively, don't treat it as a live dependency

OHM's public Overpass fork (`services/apis/locations/open_historical_map.py`) is volunteer-run
infrastructure with no formal SLA or published rate-limit policy, and a real history of being
overwhelmed - the only hard number it publishes is a 2-concurrent-request limit. This integration
deliberately leans on `LocationCache` rather than calling Overpass live: coverage/year discovery is
proactively backfilled the same way boundary data is, and each year's GeoJSON is cached
essentially forever (past OHM data for a given year doesn't change). Don't "fix" this into
something snappier without keeping that constraint in mind - a naive per-request Overpass call is
exactly the pattern that gets this fork's users rate-limited or blocked.

One consequence worth knowing before touching the source-string scheme: `LocationCache`'s unique
key is the `(location, source)` pair alone - `query_key` is descriptive metadata, not part of
lookup. Per-year feature caching therefore gives each requested year its own `source` string
(`f"ohm_features_{year}"`) rather than one `source` with a `query_key` per year. It looks like it
could be collapsed into a single source + query_key; it can't, without breaking per-year lookup.

## Decisions from the 2026-07-23 session (reconstructed 2026-08-15)

Six code comments cite "decision 2026-07-23, docs/PROBLEMS.md" for decisions that were never in
that file - the originals lived in `docs/notes/ai/`, which is gitignored, so no fresh checkout can
read them (see PROBLEMS.md, "`completed.md` is referenced from three places"). What follows is
**reconstructed from the citing comments' own one-line summaries** - the reasoning as recorded at
the call sites, promoted to a tracked file so the citations point at something reachable. If the
original notes surface, replace this section with them.

- **Per-recipient payloads** (`services/messaging/direct_messages.py`,
  `services/messaging/group_chats.py`): a live incoming message is serialized once *per viewer*,
  resolving sender identity through the viewer's own masking (and, for DMs, image-consent) rules -
  never one shared payload for all recipients, which had leaked names the server-rendered thread
  would mask. The per-viewer *payload* is the guarantee; the per-viewer *query* was not, and cost a
  `Friendship` lookup per member (twice per group send). `Profile.viewers_who_can_see` resolves the
  same question for a whole room in a fixed number of queries - the mirror of
  `visible_profile_pks`, and held to `can_view_profile` by the same agreement test.
- **Opaque identifiers** (`services/security/e2ee.py`): the E2EE group key-rotation API keys its
  payload by a deterministic per-(group, member) HMAC token rather than profile slugs, which had
  handed every member the real slug of masked members (the PR #111 finding). Group-scoped so tokens
  cannot correlate a member across groups.
- **Wire them all** (`services/notifications/notification_text_alerts.py`,
  `models/notifications/signals.py`): every `<type>_whatsapp`/`<type>_sms` preference toggle
  delivers, via central `post_save` wiring - previously only safety check-ins and DMs read their
  toggles and every other stored preference silently did nothing.
- **Option (a): a validation endpoint** (`controllers/account.py`): E2EE signup/password flows
  validate the raw password server-side through a dedicated rate-limited endpoint (option a),
  rather than duplicating every configured validator's rules in TypeScript and keeping them in
  sync by hand (option b). The raw password crosses HTTPS exactly once, is validated in memory,
  and is never stored or logged.

## Package `__init__` import ordering in `services/trivia` and `services/spotguessr`

Both packages' `__init__.py` files import their submodules in **dependency order, not
alphabetical order**, and each carries an `# isort: skip_file` guard. The `session` submodule must
be imported last: it imports back into the package, so importing it before the package's other
attributes are set intermittently raises `ImportError: partially initialized module` depending on
which process triggers the package import first - celery workers hit it, a plain `manage.py
check` didn't. Letting `ruff --fix` or an editor's organize-imports re-sort either file
reintroduces the race. (Promoted here 2026-08-15 from the two files' comments, which previously
pointed at a PROBLEMS.md entry that never existed.)


## A floorplan save forks rather than overwrites

Floorplans are hours of hand tracing, so `services/floorplans/resolution.floorplan_for_editing` is
deliberately narrow about what a save may write into. Only a version the saving profile *owns*, named
by uuid in the posted document, is updated in place. Everything else - no uuid, an unknown uuid, a
REData-origin document, or another user's version - creates a new version owned by the saver.

Two ways work would otherwise have been lost, both real:

- Re-dating a loaded plan resolved "the version in force at the new date" and rewrote *that*, so
  setting `valid_from` on a baseline destroyed the baseline instead of recording a renovation.
- Resolution is place-scoped, so any user could load - and then overwrite - another user's plan for
  the same building.

The document's item uuids belong to the version they came from, which is what makes forking safe:
they don't match the new version's (empty) contents, so `_sync` recreates the items rather than
moving them off the original.

Reads are profile-scoped for the same reason the writes are careful: a plan names doors, locks and
key attributes, which is not something to hand to everyone who happens to pin the same building.


## The floorplan document is REData's contract, not ours

`services/floorplans/serialization.py` emits and accepts exactly the shape
`../REData/src/redata/parcels/services/floorplans.py` does, and that is a constraint rather than a
coincidence: REData's write side **rejects unknown keys outright**, so any field we invent breaks a
future push upstream, and any field it emits that we ignore is data silently dropped on the way in.
`FloorplanDocumentContractTests` pins the key sets in both directions.

Two consequences worth knowing before editing that module:

- **Array order is the order.** Items carry a `sort_order` column, assigned from their position in
  the document, but it is never *emitted* - REData doesn't emit it either, and a second
  representation of the same fact is a second thing to keep in step. Re-arranging items in an editor
  survives because the array does.
- **A reference may name a `key` instead of a uuid.** A client drawing a door on a wall it just drew
  has no uuid for either, so an item may name itself with a write-only `key` that any reference
  (`room`, `mounted_on`, `parent`, `connects_rooms`, `spans_floors`, `source`, `references`) can
  point at. Keys live for one document and are never emitted on read.

`labels` is the one field we add, per item, and it is invisible to the upstream shape.

## A lost Celery child fails once; it is not redelivered forever

`CELERY_TASK_ACKS_LATE` stays on — a task is acknowledged after it finishes, so
a worker that dies mid-run does not swallow the job. `CELERY_TASK_REJECT_ON_WORKER_LOST`
is off, which is the part worth understanding, because "reject on worker lost"
sounds like the safer of the two settings and is not.

That setting governs one narrow case: **the child died and the parent survived
to observe it.** In a prefork worker that means an OOM kill or a segfault inside
a C decoder — deterministic, caused by the task's own payload, and therefore
reproduced exactly on the next attempt. Celery's failure handler rejects such a
message *with requeue*, and nothing bounds the redelivery:

- `max_retries` counts `task.retry()` calls. This redelivery comes from the
  broker, so no counter is touched.
- The time limits never engage. A cgroup OOM kill takes seconds.
- `visibility_timeout` does not pace it. That governs a message whose worker
  vanished *without* rejecting it; kombu's `_restore` re-queues a rejected
  message immediately.
- The Redis/Valkey transport enforces no delivery limit. kombu does stamp
  `redelivered = True` on the restored message, and Celery currently ignores it.

The loop is also silent: that branch sets `send_failed_event = False` and skips
`mark_as_failure`, so a task looping on this stores no result, sends no
`task_failure` signal, and emits no `task-failed` event. It is invisible to the
exporter in `services/core/celery_events.py` — the one place it would otherwise
show up — while permanently occupying a concurrency slot.

**The usual argument for keeping it on does not apply**, and this is the piece
that is easy to get wrong: losing a task to an infrastructure event is a
different code path. When the *whole* worker goes away there is no parent left
to reject anything, and the message returns via kombu's `restore_unacked_once`
(clean shutdown) or the visibility timeout (SIGKILL). Both still work with this
setting off. So the setting's only real domain is the case where retrying is
wrong by construction.

`CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT` is pinned to Celery's default of True
for the same reason: set to False it reaches the same unbounded branch for any
task exceeding `CELERY_TASK_TIME_LIMIT`, which is far easier to hit than an OOM.
`dashboard.E007` and `dashboard.E008` refuse both combinations at startup, and
`tests/hypothesis/test_celery_worker_lost.py` drives the real Celery `Request`
so a version bump that changes this surfaces as a test failure rather than a
silently different runtime.

Worth being concrete about the cost, because "a task fails twice" undersells it:
`media-worker` decodes bytes a stranger uploaded and runs `--concurrency=2`, and
its threat model is decoder memory-corruption bugs - inputs that kill the child.
A segfault raises the same `WorkerLostError` as an OOM. Under the old settings
one such upload permanently held one of two slots, silently; two held the whole
interactive media queue, with the container still reporting healthy.

This is the bound on *how many times*; whether a second run is safe at all is a
separate question, answered per task — the duplicate-delivery survey in
`docs/reports/2026-08-11-codebase-audit.md` covers the side-effecting families.
