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
`detail_pins.py`, `badges.py`, `markup.py`) only checked `LoginRequiredMixin` — any authenticated
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

## Badges are one model wearing four hats

`Badge` (`dashboard/models/badges/model.py`) is the single backing model for **tags**,
**categories**, **statuses**, and **person labels** — distinguished only by a `kind` field
(see `KIND_*` constants in `badges/meta.py`). `models/categories/` and the old `tags`/`statuses`
modules are now thin aliases re-exporting `Badge`/`Category` for backward compatibility, not
separate tables. The standalone `categories`/`tags`/`statuses` controllers and viewsets were
recently deleted in favor of the unified badge controller/viewset with a `kind` parameter — if you
find references to the old separate modules elsewhere, they're stale.

Per-user visual overrides (color/icon) on a shared global badge live in a separate
`BadgeCustomization` model — editing "your" badge color never mutates the badge other users see.

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
