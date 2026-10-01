# Place consolidation: one answer to "is this the same place?"

**Status**: **IMPLEMENTED 2026-08-04** (migrations `0026_places`, `0027_places_backfill`,
`0028_places_cleanup`). Original design approved 2026-07-27 (Jess + research session); built out
in full while fixing the multi-building campus bugs, with three refinements agreed on the way —
see "Amendments as built" at the end, which supersede the corresponding text below.

The app is pre-release, so the schema changes here were made without back-compat shims beyond the
data migrations described.

## The problem

The app currently answers "are these two pins / this pin and this wiki / these two users at the
same place?" four different ways in four different subsystems:

| Subsystem | Predicate | Where |
|---|---|---|
| Pin drop/import dedup | 50m radius snap onto an existing `Location` | `services/pins/pin_creation.py` (~L174), `models/location/queryset.py::get_nearby_or_create` |
| Wiki visibility | point-in-official-polygon containment, 50m circle fallback | `services/wiki/wiki_access.py::location_visible_to` |
| Wiki creation dedup | exact `Location` row (`OneToOneField`) | `models/wiki/model.py`, `models/wiki/queryset.py::get_or_create_for_location` |
| "Places in common" | exact `location_id` equality, no geometry at all | `services/pins/common_pins.py::common_pin_location_ids` |

Consequences, all confirmed against the code:

1. **Coordinate warping.** The 50m snap at drop/import discards the user's exact coordinates
   whenever *any* Location (including another user's) exists within 50m. This contradicts the
   product goal of recording the precise coordinate the user placed (Location stores 6-decimal
   lat/lng ≈ 0.11m, and the DB enforces coordinate immutability — the precision machinery exists,
   but the snap defeats it at the front door).
2. **Inconsistent invariants.** Manual moves use exact-match (`threshold_meters=0` in
   `services/pins/pin_edit.py::move_pin_to_coordinates`), so "no two root pins within 50m" is only an
   at-creation behavior, not an invariant.
3. **Duplicate wikis for one real place.** Wiki dedup is exact-Location; two users pinning the
   same large property >50m apart get two Locations and can create two wikis.
   `reconcile_wiki_nesting` auto-nests overlapping-boundary wikis but never merges them.
4. **Duplicated official geometry.** Boundaries hang off Location rows
   (`models/boundary/model.py`), so every Location inside one parcel stores its own copy of the
   parcel polygon, generated separately (duplicate provider API calls; copies can drift).
5. **Boundary-mates are invisible to "places in common".** Two friends pinned on the same parcel
   at different Locations can see each other's wiki content but show zero places in common.
6. **Hot-path inefficiency.** `location_visible_to` fetches polygons and runs GEOS
   `.contains()` in Python over all of the requesting profile's pins, per request.
7. **Fragile trust filtering.** The one Boundary table mixes official geometry with user/community
   drawings, so every access-predicate query must stack exclusion filters
   (`pin__isnull=True, wiki__isnull=True, profile__isnull=True, source=""`). One missed filter is
   a privacy hole. The invariant is currently upheld and documented (`docs/NOTES.md` §1.3), but
   it is upheld by discipline, not by structure.

## What stays the same

These are correct today and this design deliberately preserves them:

- **`Location` = immutable exact coordinate.** Coordinates are identity
  (`IMMUTABLE_FIELDS`, DB trigger, unique `(latitude, longitude)`). Pins keep their FK to
  Location. A pin move keeps repointing to a new/existing Location at exact coordinates.
  Pins do **not** FK directly to geometry: place membership is a *resolved, cached*
  relationship, never an identity, so geometry changes can never corrupt provenance.
- **The trust invariant.** Access derives only from officially-sourced geometry. User- and
  community-drawn polygons are display-only, everywhere, forever.
- **Explicit wiki creation.** Wikis are never created as a side effect
  (`services/locations/creation.py`); this design leans on that harder (see splits).
- **The provider chain and boundary voting.** `BoundaryProviderChain` (REData → Overpass →
  Overture → Microsoft → Google) and recency-weighted `BoundaryVote` voting carry over; they just
  anchor to Place instead of Location.
- **Share provenance.** `LocationExposure` / `resolve_origin_share` / `record_share_exposure`
  are untouched by this design (they key off Location, which survives).

## Decisions

1. **Introduce a `Place` entity; wikis re-anchor to it** (`Wiki.location: OneToOne(Location)` →
   `Wiki.place: OneToOne(Place)`). This is the fix for duplicate wikis, duplicated geometry, and
   predicate drift.
2. **One concrete `Place` table with a `kind` field, not concrete Parcel/Building subclasses.**
   Proxy models (`Parcel`, `Building`) may wrap it for kind-specific behavior. Rationale: every
   relationship (wiki anchor, location resolution, boundary rows, votes) is shared across kinds;
   only service-layer behavior differs (which provider, split semantics). Concrete inheritance
   would put a join in the middle of the access predicate to buy separation nothing needs, and a
   future kind would become a schema migration instead of an enum value.
3. **Zones/POIs (warning areas, camera sightlines, entrances, pathways) are NOT Places.** They
   are annotations: expressive, user-drawn, untrusted. They live in the annotation layer (the
   existing per-pin/per-wiki drawn-geometry rows, extended later with a purpose type) and are
   structurally incapable of granting access because the access predicate never reads that table.
   This converts invariant-by-discipline (#7 above) into invariant-by-structure.
4. **Dedup is boundary-based only. No radius, anywhere.** Every creation path stores the user's
   exact coordinates; "you already have a pin here" becomes a place-membership check.
5. **Parcel splits: supersession + grandfathering + earned ancestor access** (details below).
   Grandfather grants are materialized at split-processing time; ancestor access can also be
   *earned* recursively by holding access to every child.
6. **Child wikis are created lazily, never auto-created by a split.** A split creates child
   *Places* automatically; child wikis appear when a user with a pin there explicitly creates
   one, auto-nested via place lineage.

## Target model

### Place

One row per real-world parcel-ish or building-ish thing.

- `kind`: `PARCEL` | `BUILDING` (extensible enum).
- `parent`: nullable self-FK. Building → containing parcel. Post-split parcel → superseded
  campus parcel. This containment FK does the real modeling work; `kind` is closer to a label on
  hierarchy depth.
- `status`: `CURRENT` | `SUPERSEDED`. A superseded Place keeps its geometry for display and
  history but its boundary is **removed from the access predicate** (critical: the old campus
  boundary still geometrically contains every post-split pin; containment against historical
  geometry must never grant access).
- Official geometry moves here: the location-default and source-candidate Boundary rows
  re-anchor from Location to Place. `BoundaryVote` follows. Generated geometry becomes
  **append-only**: a regeneration writes a new geometry row with a validity start, closing the
  previous row's validity end, instead of overwriting `generated_polygon` in place. No history
  *features* are built now — this just stops destroying the data those features (and
  grandfather auditing) will need.
- Only the provider chain and boundary voting can write Place geometry. There is no
  user-writable path, and no user-drawn geometry in any table the access predicate reads.

### Location.place

Nullable FK + `place_resolved_at`. Resolution: the most specific **current** Place whose official
geometry contains the point (building footprint if inside one, else parcel). Re-resolved when a
Place's geometry regenerates or a split is processed; a Location with no containing Place has
`place=NULL` (its pins behave exactly like today's boundary-less locations). This cached FK is
what makes every predicate below an indexed lookup instead of a geometry query.

### Wiki

`Wiki.place: OneToOneField(Place)` — one community wiki per Place, which is the dedup that
exact-Location dedup was trying to be. `Wiki.parent_wiki` nesting follows place lineage
(building wikis under parcel wikis; post-split parcel wikis under the superseded campus wiki).
`Pin.wiki` survives as a denormalized cache, set/refreshed from the pin's resolved place.

UI caveat that is a hard requirement: a viewer without access to a wiki's parent must see **no
parent breadcrumb or link at all** — rendering a link that 404s leaks the parent's existence,
defeating the uniform-404 design in `external_api/views_wiki.py` and `resolve_visible_wiki`.

### PlaceAccessGrant

`(profile, place, reason, created)` — materialized access that containment can no longer prove.
Written **only** by split processing (`reason=GRANDFATHERED`): when a split is processed, snapshot
every profile with a qualifying pin inside the old boundary and write grants. We deliberately do
not attempt "did this profile historically have a pin here" recomputation — pin-position history
doesn't exist, and the snapshot is deterministic and auditable. Because grandfathering means
nobody can *lose* access when a split is processed, split processing is safe to automate; a
false-positive detection is annoying, not harmful.

### The unified access predicate

```
access(profile, place):
    if any grant (profile, place) exists:                      -> True   # grandfathered
    if place.status == CURRENT:
        return profile has a pin whose location.place is place
               or any descendant of place                      # pin in a building counts
                                                               # toward the parcel's wiki
    if place.status == SUPERSEDED:
        return place has children and access(profile, child)
               holds for EVERY child                           # earned ancestor access
```

Properties, all deliberate:

- **Earned ancestor access is recursive over *access*, not literal pins-in-every-leaf.** With
  campus → {A, B} and A → {A1, A2}: access to the campus requires access to A (earnable via pins
  in A1 *and* A2, or a grant) and access to B. Each additional pin can unlock the next ancestor
  tier — a user proves knowledge of the broader campus by proving knowledge of all its parts.
  This also solves the "community's campus history is invisible to newcomers" problem: newcomers
  can earn their way into it.
- **Computed access is revocable; grants are not.** Deleting/moving the pin that completed an
  "all children" set silently un-earns the ancestor — consistent with how all pin-derived access
  behaves. The move-warning UX (below) covers the accidental case. Grants persist regardless of
  pin churn ("access users have become accustomed to" survives without maintenance pins).
- **Accepted disclosure**: earning ancestor access reveals that the child parcels were once one
  property. Parcel lineage is public county-record data; accepted as low-risk.
- Recursion depth is bounded by split history (rare events); with `Location.place` cached and
  grants indexed, the whole predicate is a handful of indexed queries — replacing the per-request
  Python GEOS loop in `location_visible_to`.

Every current predicate collapses onto this one: wiki visibility (`location_visible_to`),
"places in common" (`common_pins` becomes place-id intersection, finally counting
boundary-mates), creation dedup, and merge suggestions.

### Splits / supersession flow

1. **Detect** during boundary regeneration: the provider now returns materially different parcel
   geometry — the old boundary now contains multiple distinct new parcels, or the parcel for
   this point shrank past a threshold. Thresholds are implementation-time tuning; err toward
   flagging, since processing is grandfather-safe.
2. **Process**: create child Places (`parent` = old Place), mark the old Place `SUPERSEDED`,
   snapshot grants, re-resolve affected Locations to their new Places.
3. **Wikis**: nothing is auto-created. The superseded Place keeps its wiki (if any); child
   wikis are created lazily by users with pins there and auto-nest under it. Newcomers see a
   child wiki as an ordinary top-level wiki (no parent link — see UI caveat above).
4. **Content**: redistribution of campus-wiki content into child wikis is a community action by
   people who already have access, via normal wiki editing. No automated migration, no "hidden
   history here" teaser (which would itself leak).

### Creation, import, and move flows

- **Drop/create**: store exact coordinates (existing `get_nearby_or_create` with
  `threshold_meters=0` semantics, everywhere). Then a place-membership check: if the profile
  already has a root pin resolving to the same Place, prompt — merge into it, add as child pin,
  or cancel. The existing `get_all_for_point` choice UI (`services/pins/pin_creation.py` ~L184) is
  the seed of this interaction.
- **Import**: exact coordinates + `client_uuid` idempotency. Same-place collisions can't prompt
  per-row mid-import; default: dedupe onto the existing pin (today's outcome, but
  boundary-based instead of radius-based) and report consolidations in the import summary.
- **Exact-coordinate overlap**: two pins (including child pins) may never sit at precisely the
  same Location for one profile. Root pins already have a DB constraint; child pins get a
  service-layer check at both child-pin creation paths. (DB-level place uniqueness is
  deliberately *not* attempted: place resolution changes over time, and a re-resolution must
  never be able to violate a DB constraint retroactively. Place-level dedup is service-layer.)
- **Move**: membership change is detectable at write time (resolved place differs, or an earned
  ancestor set breaks). One confirm dialog: "Moving this pin will disconnect you from the
  community page for <place>. Move anyway / Cancel." No other new UX.

## Migration plan

Phased; each phase lands green before the next. Splits/grants (phase 4) can trail the rest —
splits are rare and the schema supports them from phase 1.

- **Phase 0 — standalone fixes** (independent of Place): **DONE 2026-07-27.**
  - `services.pins.pin_creation.resolve_child_pin_location` is now the single child-pin Location
    resolver: exact-coordinate matching (quantized to the fields' own 6dp, so it can't race the
    `(latitude, longitude)` unique constraint) plus the rule that no two of one profile's pins
    may share a point. All four child-pin paths use it - the map UI dialog
    (`controllers/detail_pins.py`, which also had its own duplicate resolver, now deleted),
    `create_pin_for_profile(parent_id=...)`, `pin_wiki_sync.pull_children_from_wiki`, and
    `pin_restructure.create_building_pins`. The two batch callers skip a colliding item instead
    of aborting; the two interactive ones surface a 400. The child-pin *move* path enforces it
    too, excluding the pin being moved so a no-op move stays a no-op. This also removed two
    service→controller imports.
  - `location_visible_to` now evaluates containment in the database (one indexed EXISTS) instead
    of loading every point the profile ever pinned. Both it and the move preview share one
    parameterized core (`_visible_given_pins`), so the preview cannot drift from the enforced
    rule. New tests pin the anti-gaming invariant directly: a user-drawn `polygon` and a
    pin-owned Boundary row must never widen visibility.
  - Move-warning shipped end to end: `wiki_access.wikis_hidden_by_pin_move` lists only wikis the
    owner currently sees *and* would lose; both the internal `PinViewSet` and the external API
    refuse such a move once with 409 + the wiki names, and proceed on `confirm_wiki_loss`. The
    pin page's marker drag shows a confirm dialog and snaps back on cancel.
  - Not done, deliberately: `visible_wiki_location_ids` still does Python-side containment (it is
    already scoped to locations that have a wiki, so it is bounded), and two other
    `threshold_meters=0` callers keep the latent 500 described in `docs/PROBLEMS.md`.
- **Phase 1 — Place + resolution**: Place model; backfill by clustering existing Locations via
  their location-default boundaries (Locations whose generated polygons are the same/overlapping
  geometry → one Place); move official Boundary rows + votes to Place, deduplicating per-Location
  copies; `Location.place` resolution service + backfill; boundary generation re-anchors to
  Place (one provider run per place, not per Location).
- **Phase 2 — wiki re-anchor**: `Wiki.place` migration via `location.place`; merge duplicate
  wikis landing on the same Place (reuse `services/wiki/wiki_merge.py`); recompute `Pin.wiki` caches.
- **Phase 3 — predicate unification**: creation/import flip to exact-coords + place dedup;
  `location_visible_to`, `common_pins`, merge suggestions, and `reconcile_wiki_nesting` all move
  to place-id logic; remove the 50m constants (`models/location/queryset.py` L81 hardcode,
  `get_nearby_or_create` default, `DEFAULT_RADIUS_METERS` stays only as the no-boundary display
  circle).
- **Phase 4 — supersession**: status/lifecycle, append-only geometry versioning enforcement,
  split detection, grant snapshotting, earned-ancestor access, move-warning upgraded to the
  unified predicate.

Testing: hypothesis property tests on the access predicate are mandatory — especially the
recursive earned-access rule (arbitrary lineage trees, pin sets, grant sets) and adversarial
cases (user-drawn geometry must never influence access; superseded boundaries must never grant
via containment; parent links must never render for non-members). Migration dry-runs on a dev
environment (`~/dev/s1..s3`) against a production-shaped dataset before merging phase 1.

## Security invariants (must survive every phase)

1. Access derives only from Place official geometry (provider chain + voting output) and
   split-processing grants. No user-drawn geometry in any access-predicate table.
2. Uniform 404: nonexistent place, place without wiki, and inaccessible wiki are
   indistinguishable; no parent-wiki links rendered to non-members.
3. Superseded boundaries never grant access via containment.
4. Grants are written only by split processing — no API surface creates them.
5. Community/wiki annotation drawings remain area-capped (`SiteSettings.max_bbox_area_km2`).

## Accepted tradeoffs & open items

- Earned ancestor access is revocable by pin deletion/move (accepted; move-warning mitigates).
- Earning discloses parcel lineage (accepted; public record).
- Import same-place collisions dedupe silently onto the existing pin + summary report (accepted
  default; revisit if users complain).
- A profile can currently hold a root pin *and* a child pin at the same Location (see the
  ordering comment in `models/pin/queryset.py::get_nearby_or_create`); the exact-overlap rule
  makes this unreachable going forward, and existing occurrences should be surfaced as
  `PinMergeSuggestion`s during phase 3 rather than force-migrated.
- Split-detection thresholds are tuning, not design.
- Google Places calls involved in any of this continue to route through REData
  (`services/apis/locations/places_resolution`), per the standing integration rule.

---

## Amendments as built (2026-08-04)

Three changes to the design above, made while implementing it against the multi-building campus
bugs. Where these conflict with the text above,
these win.

### 1. `parent_relation` carries the access semantics, not `status`

The predicate above keyed "how is this parent earned?" off `Place.status`, which conflated two
different questions and could not express the rule Jess stated: *pinning a building should give you
its parcel, but a parcel's parent must be earned*. Under status-as-semantics that only held as long
as a `CURRENT` place never had a `CURRENT` parcel parent — true for splits, false for a campus that
spans several current tax parcels.

So the edge carries it instead:

- **`PART_OF`** — the child is a component of the parent and the parent is fully implied by it
  (building → parcel). **No access semantics at all.**
- **`MEMBER_OF`** — the child is one of several independent peers (split child → superseded campus;
  parcel → site). Earned only by holding every member.

`status` now controls exactly one thing: whether a coordinate may resolve onto this geometry.

### 2. Access is evaluated per *domain*, and it is symmetric

An **access domain** is a maximal set of places connected by `PART_OF` edges, denormalised onto
`Place.domain_root`. It is indivisible — a pin anywhere in it grants every wiki in it, in **either**
direction:

```
access(profile, place) := domain_access(profile, place.domain_root)

domain_access(profile, D):
    grant(profile, D) exists                                    -> True
    profile has a pin whose location.place.domain_root is D     -> True
    profile has a pin at a Location that D owns (exact match)   -> True   # placeless fallback
    D has MEMBER_OF children c1..cn (n >= 1)
        and domain_access(profile, ci.domain_root) for EVERY i  -> True   # earned, recursive
```

Downward access (parcel ⇒ its buildings) was the deliberate addition. Without it, splitting a
property into 124 buildings hides content from people who already had the property — a regression
caused purely by a structural edit — and the parcel wiki cannot legitimately list its own buildings
without leaking their existence. Decided 2026-08-04.

`PART_OF` never appears in the recursion, so the whole hierarchy reduces to a DAG of domains joined
by `MEMBER_OF` edges, and the common case is one indexed equality test.

### 3. Grants are also written by the backfill, not only by splits

Security invariant #4 said grants are written only by split processing. It now reads: **only by
split processing and the one-time Place backfill migration** — still no API surface, so the intent
(no user-reachable path) is unchanged. This is load-bearing given the strict parcel-to-parcel rule:
a campus wiki that turns out to span several tax parcels becomes a `SITE` aggregate at backfill
time, and without a grant every existing holder would lose it.

### Smaller decisions worth recording

- **`Wiki.location` survives** as a plain FK for coordinates, address, and URL routing;
  `Wiki.place` is the OneToOne identity/dedup anchor. Re-anchoring the *URL* as well would have
  churned every template, view, and test for no gain. `resolve_visible_wiki` finds the wiki via the
  place, so everyone who pinned one property reaches it from their own slug.
- **Official geometry is `Place.geometry`, a column** — not a location-default `Boundary` row.
  Location-default rows are gone; `Boundary` now holds only user/community drawings and per-provider
  voting candidates, and no access path reads that table at all.
- **`Place.geometry` is nullable.** A building nobody has a footprint for keeps identity, lineage,
  and its own wiki without ever being resolvable.
- **Aggregates are excluded from point resolution entirely**, which makes strict earning
  unbypassable rather than merely unlikely.
- **The ambiguity notice lists only places the viewer can already reach.** In the rare overlap
  where they cannot, it does not render at all — trading a nudge for the guarantee that it can
  never be an oracle. The other place stays reachable the ordinary way.
- **`pin_type` is derived from the place** and fanned out to every user's marker on it
  (`site_scope.reclassify_markers_on_place`), rather than set on the acting user's pin.
- **The 50 m snap is gone.** Exact coordinates are always kept; "one root pin per property" is now
  enforced against the property itself.
