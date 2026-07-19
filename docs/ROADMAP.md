# UrbanLens Development Roadmap & Agent Working Plan

A strategic planning document for agents (and humans) doing implementation, bug-hunting, and
feature work on UrbanLens. Generated 2026-07-18 from a full review of `TODO.md`,
`docs/FEATURES.md`, `docs/NOTES.md`, `docs/api-expansion-candidates.md`, `docs/prompts/todo.md`,
recent git history, and the codebase structure.

**How to use this document:** You are probably a capable agent who can plan your own task. What
you are at risk of losing is *the larger picture*: how the feature you're touching interacts with
five others, which invariants are load-bearing, and which maintainability/performance choices will
bite six months out. Read Parts 1–3 before writing code. Use Parts 4–5 to pick and scope work.
Use Part 6 as your definition-of-done checklist. When you complete, verify, or invalidate anything
here, update this document (and `TODO.md` / `docs/FEATURES.md` / `docs/PROBLEMS.md` as
appropriate) — this is a living document.

---

## Part 1 — The Big Picture

### 1.1 What UrbanLens is, and its central tension

UrbanLens helps photographers and urban explorers document, organize, and responsibly share
fragile locations. Nearly every design decision flows from one tension:

> **Locations are the product, and locations are the thing being protected.**
> A location leaking to the wrong audience gets it vandalized, sealed, or demolished.

This is why UrbanLens is not "Google Maps with friends." The privacy model is
**discovery-gated**: you earn access to community knowledge about a place by having found the
place yourself (having a pin there). Sharing is deliberately narrow (one pin, one friend,
provenance-tracked), and mass-sharing is treated as a threat vector, not a growth feature.

Every feature you build or fix must be evaluated against this tension. A convenience feature that
lets a user enumerate, scrape, or infer other users' pinned locations is a **critical bug**, even
if it works exactly as its author intended.

### 1.2 The core data model (three models, strictly separated)

- **`Location`** — shared, global truth about a physical place (canonical name, address,
  coordinates, Google CID). Not user-specific. Coordinates are **immutable after insert**
  (model + DB trigger). Use `Location.objects.get_nearby_or_create(...)`, never raw construction
  when coordinates might already exist nearby.
- **`Pin`** — one user's personal record pointing at a Location (name override, private notes,
  icon, priority, status, marker coords). Address/place metadata is proxied read-only from
  `self.location`; never stored on Pin. If a pin "moves," it gets a *new* Location — the old
  Location is never mutated.
- **`Wiki`** — opt-in community page for a Location. Visible **only** to users with their own pin
  at that Location (recently widened to boundary-mate Locations — see §1.3.1). A community wiki
  is *not* a public wiki.

Confusing these three is the most common class of design error in this codebase. When in doubt:
user-specific data → Pin; shared factual data → Location; shared editorial/community data → Wiki.

### 1.3 Invariants that must never be violated

These are the load-bearing rules. Any change that touches these areas needs a test proving the
invariant still holds. Most of these were each, at some point, the subject of a real security or
privacy bug.

1. **Wiki visibility is pinned-gated, and non-visibility is indistinguishable from
   non-existence.** All wiki-scoped controllers resolve through
   `services/wiki_access.resolve_visible_wiki(...)` (or `location_visible_to(...)` for
   non-redirect response shapes). An unpinned wiki slug must return the *identical* `Http404` as a
   nonexistent slug — a 403, or any distinguishable response, is an oracle telling an attacker
   "someone has pinned this spot." JSON polling endpoints return the same empty shape either way.
2. **Blocking vetoes active contact-initiation** (DMs, trip invites, friend requests, force-adds
   by username) — it does not merely hide passive visibility. Recently fixed twice
   (`e6001cec`, `d23f2b73`); any new contact path (group-chat adds, meetups, shared lists, safety
   contacts, mentions) must check block status at the initiation point. A test named "blocked"
   must actually exercise a *blocked* relationship, not just a privacy tier.
3. **Every pin/location share path calls `resolve_origin_share` + `record_share_exposure`**
   (`services/share_provenance.py`) so the `LocationExposure` provenance chain stays intact. New
   sharing surfaces (DM pin shares, group chats, trips, lists, markup maps, exports) must not
   bypass this.
4. **External sync channels need separate revocation.** Membership removal is not the same as
   sync revocation — leaving a trip must also stop Google Calendar sync (`f742a3f3`). Any future
   external mirror (calendar, webhook, RSS, export subscription) needs an explicit revocation
   path wired into every membership/permission-removal flow.
5. **Import data is untrusted requests, not facts.** Imported archives must not be able to forge
   friendships, reference foreign pin UUIDs/label IDs, traverse paths (zip-slip), or exhaust disk
   (extraction caps) — see `ff4039ce`, `35d89b98`. Anything imported that implies a relationship
   with another user becomes a *request* requiring the other party's consent.
6. **Boundary matching only trusts `Boundary.generated_polygon`** (from external footprint data).
   The user-editable `polygon` is display-only — otherwise users could inflate boundaries to
   claim overlap with other users' pins/areas and widen their wiki access (§1.3.1 makes this
   invariant *more* critical, not less).
7. **Community counts are fuzzed** (`services/community_counts.py`) — never expose exact
   "N users pinned this" counts; exact counts + timeline = individual activity inference.
8. **Pin slugs are unique per-profile, not global.** Any slug lookup not scoped by profile
   silently returns another user's pin. Recurring bug source (uploads, galleries, weather).
   Similarly, non-anonymized sequential-ID URLs must not exist (UL-40).
9. **`Location.lat/long` immutable; `EncryptedTextField` keys derive from `SECRET_KEY`**
   (rotating it corrupts all encrypted data); **E2EE code changes touch read-side only** unless
   the task is explicitly cryptographic — never casually refactor sealing/wrapping logic.
10. **Deletion cleans up files and stages through undo.** Account/object deletion must not orphan
    FileFields on disk (`08dfa1eb` — CASCADE does not delete files) and destructive actions on
    pins/wikis/trips/check-ins go through the undo framework (`services/undo/`), never raw
    `.delete()` in a view.
11. **All external API calls go through a `Gateway` subclass** with a `service_key`, so rate
    limiting (`ApiRateLimit`), call logging (`ApiCallLog`), and future cost tracking apply.
    Track estimated cost per call now — the reporting UI (UL-52/53) will need the data.
12. **Never `save()` inside `post_save` or `__str__`**; use `queryset.update()`; always pass
    `dispatch_uid`. Write signal-handler guards so they're structurally redundant (the linter has
    stripped "redundant-looking" guards before).

#### 1.3.1 RESOLVED 2026-07-18: boundary-mate wiki visibility

Commit `15e6e2e2` widened wiki visibility from "pin at this exact Location" to "pin at any
Location within the same boundary" (a campus-scale place has many Locations), with
`tests/hypothesis/test_wiki_access_boundary_mates.py` covering it. This is fully committed, not
in-flight — re-checked 2026-07-18: invariant #1 holds (`location_visible_to` still returns a
plain bool feeding the same single `raise Http404`, so a boundary-mate match and a true 404 are
indistinguishable to the client either way); invariant #6 holds (the boundary query filters to
`pin__isnull=True, wiki__isnull=True, profile__isnull=True` location-default rows and excludes a
null `generated_polygon`, never touching the user-drawn `polygon` field). §4.1 item 3 is done;
`custom_field_references.py`'s wiki-reference picker still duplicates the *old*, pre-fix check
(documented in `docs/PROBLEMS.md`, not yet fixed — low severity, under-permissive only).

### 1.4 Product philosophy for feature decisions

When a TODO item is ambiguous, these tiebreakers reflect the owner's demonstrated intent:

- **Privacy > growth.** Features that encourage "pin hoarding" or mass redistribution
  (map subscription UL-222/223, share-with-partner) are flagged as *maybe never* in TODO.md.
  Don't build them without explicit direction; do build the safeguards around them (UL-299:
  cap *pin shares per time period*, not members-per-trip).
- **HTMX > JavaScript.** New interactivity is server-rendered fragments unless HTMX genuinely
  cannot do it (Leaflet, drag-drop, WebSockets). Every existing JS-heavy interaction is a
  standing refactor candidate (UL-80, UL-289).
- **Async > blocking.** Any non-instant operation goes to Celery, shows a progress indicator,
  and completes with a toast (UL-119). The `panel_fetch` queue exists specifically so slow panel
  fetches can't starve the general worker; CPU-heavy panels opt out via `PanelSource.queue`.
- **Plugins > one-off integrations.** New external services are `Gateway` + `UrbanLensPlugin`
  manifest (`docs/plugins.md`), not controller-level API calls.
- **The app is beta.** Weirdness is a bug, not a convention. Fix it or log it in
  `docs/PROBLEMS.md`. Existing suboptimal patterns are not precedent.
- **Free/open APIs before paid.** See §4.7.

---

## Part 2 — Feature Interaction Map

This is the part individual task-focused agents most often miss. Before changing feature X, scan
this section for X and check each listed interaction.

### 2.1 The pin/location graph touches everything

`Pin ↔ Location ↔ Wiki ↔ Boundary ↔ detail pins (sub-pins)` is the spine. Consequences:

- **Creating a pin** can: match an existing nearby Location (`get_nearby_or_create`), grant wiki
  visibility (incl. boundary-mates), trigger keyword auto-tagging, trigger background enrichment
  eligibility, appear in smart lists (criteria resync), invalidate the client pin cache
  (`ul_pins_dirty` flag; `CACHE_VERSION` in `pin-cache.ts` if payload shape changed), and create
  a `LocationExposure` record if it originated from a share.
- **Deleting a pin** can: revoke wiki visibility, orphan sub-pins, remove smart-list membership,
  strand trip activities and list items referencing it, and must stage through undo. UL-107 notes
  an *architectural* abuse: creating a throwaway pin to test whether a wiki exists, then
  deleting it. Any change to pin-create/delete latency or wiki-reveal timing interacts with this.
- **Renaming a pin/location** interacts with slugs (UL-226 — slug regeneration policy is
  unresolved) and with wiki titles (UL-26 — wiki must be titled from the *place* name, never leak
  the user's private custom title).
- **Pin status vs. "visited" tag vs. visit history** are three overlapping representations of the
  same concept (UL-329) — features reading "has the user been here" must decide which is
  authoritative; a consolidation design is overdue. NOTE from user: Pin status is an outdated concept, and should be considered deprecated. The Visited label indicates a visit independent of specific visit history data. Visit history data should add the visited badge. Therefore, the visited badge and visit history provide redundant indicators of "has the user been here", and I believe that is likely unavoidable.

### 2.2 Labels are one model wearing five hats

`Label` (kind ∈ tag/category/status/person/media) backs four UIs plus: map filter sidebar, saved
filters (criteria JSON), smart lists (auto-resync on `m2m_changed` — a previously-missed signal
path), bulk edit, import (auto-created badges + AI styling), per-user `LabelCustomization`
(never mutate the shared label for one user's color change), and the organize page. A change to
label merge/convert/delete must consider: saved-filter criteria referencing the label ID, smart
lists built on it, foreign-label-ID attach validation on import (was a real bug), and pin-cache
invalidation for marker badges (UL-279). These were previously called "badges", but are now labels.

### 2.3 Filters/lists/smart-lists are one system, three UIs

`SavedFilter` criteria JSON is the shared engine: map sidebar, saved-filter CRUD pages, smart
lists. Fixes to filter semantics (rating slider 0/"unrated" UL-296, phantom active filter
UL-271, geographic include/exclude polygons) must land in the *shared* criteria layer, not one
consumer, or the three UIs drift (the prompts backlog documents the filter-view page already
lagging the map sidebar's features badly). Date-built/date-abandoned range filters (fields exist
since migration 0067) are a known unbuilt filter-group.

### 2.4 Trips interlock with sharing, calendars, privacy, and blocking

A trip is a *sharing surface*: adding a member exposes activity pins → must record exposure
(§1.3-3), respect blocks (§1.3-2), and count toward share caps (UL-299). Google Calendar sync
mirrors trips externally → revocation on leave/removal (§1.3-4), and attendees-as-invites on
import interacts with the email-invite pipeline (UL-235/236: invite email ≠ signup email).
"Hidden" activities have unresolved visibility rules (UL-231: the hider must still see their own
pin). Child trips (UL-228) and trip variations remain unverified/undesigned.

### 2.5 Safety check-ins bridge the authenticated and unauthenticated worlds

The tokenized no-login contact portal is the app's only deliberately unauthenticated surface —
plus WebSocket chat, wiki auto-posting on escalation, email notifications, and attached markup
maps. UL-371 is a standing audit item: token reuse across browsers, wrong-token behavior,
pre-escalation access, exactly-what-is-disclosed-when, and a possible time-delayed
information-release design. Any change to wikis, notifications, markup maps, or email delivery
should check whether the safety flow consumes it. Coverage here is poor (§4.8) for the app's
highest-stakes feature.

### 2.6 Messaging: E2EE constrains everything downstream

Because DMs/group chats are E2EE, *the server cannot read message content*. Features that want
message data (search, AI, moderation, notifications preview) must be client-side or
metadata-only. Key rotation leaves old messages undecryptable-by-design (UI already handles it).
Pin shares in chat re-enter the provenance system; auto-detected coordinates/addresses in chat
("Add to my map") create pins → full §2.1 cascade. Group membership is per-stint (visibility
scoped to membership window). Blocking must gate DMs, group adds, and mentions.

### 2.7 Photos/visits/memories are one pipeline

EXIF GPS + timestamp drives: pin galleries, the site-wide photo library, visit suggestions,
pin suggestions (cluster-into-new-pins), Memories timeline, and Immich/Google Photos/Flickr
imports. Shared concerns: storage quotas (role-based), checksum dedup, WebP downscaling,
face/person tagging (future UL-303 — heavy privacy design needed), and "Show Photos From"
blur-not-hide visibility. A change to photo upload or EXIF parsing ripples across all consumers.
UL-361 (auto-visit from photo timestamps) is the natural next integration and mostly composes
existing pieces.

### 2.8 The external-data pipeline: gateway → plugin → panel → cache → enrichment

One architecture serves: on-demand pin-detail panels (Celery `panel_fetch` queue, 204-marker
protocol for empty results, suppression windows on timeout), the hourly background enrichment
drip (spends *leftover* rate-limit budget only, admin-tunable), name resolution
(quality-gated `NameProvider` candidates with user-configurable priority), boundary provider
chain, satellite/street-view carousels, and search. Adding a provider = `Gateway` subclass +
plugin manifest + registry entry; adding it anywhere else is a regression. External-service
failures (Overpass 504s, GDELT 429s, NPS rate-limit) are *expected backpressure* — handled and
logged at WARNING-level, not crashes; don't "fix" them into retry storms.

### 2.9 Notifications are a matrix, not a stream

11 event types × 4 channels (in-app/email/WhatsApp/SMS) with per-role email rate caps, WebSocket
push + polling fallback, and admin-side Gotify alerting as a separate channel. New features that
notify must: register an event type in the matrix (not hardcode a channel), respect caps, and
consider the unauthenticated safety-contact path. Known UX debt: dropdown-view should mark read
(UL-348); friend-request notifications are a dead end (UL-237).

### 2.10 Client-side caching is a correctness surface

`pin-cache.ts` (tile-based, versioned) + `ul_pins_dirty` localStorage flag + per-user scoping.
Known live issues: cache not tied to user on logout/login (UL-239 — data leak between users on a
shared browser, arguably a *privacy* bug, prioritize accordingly), `QuotaExceededError` at 8k+
pins (UL-355 — needs an eviction/compression strategy, not just a bigger bucket), stale badge
icons after organize edits (UL-279), and the standing rule that any pin-payload shape change
bumps `CACHE_VERSION`.

---

## Part 3 — Cross-Cutting Engineering Concerns

### 3.1 Performance & scalability (design for 10k+ pins per user)

The app now has real users with 8k+ pins; several systems were designed for hundreds.

- **Map pin pipeline**: possible localStorage quota (UL-355) - maybe not. Consider: IndexedDB
  instead of localStorage, payload slimming (the cache stores full pin payloads where markers
  need a subset), per-tile eviction, or server-side viewport-bounded queries as the primary path
  with cache as accel. This requires investigation to prevent over-engineering.
- **N+1 discipline**: `select_related`/`prefetch_related` everywhere; the geolocation-visits
  endpoint N+1 (boundary resolution per pin) already caused production nginx timeouts once.
  Any per-pin loop that touches Boundary/Location/Label should be audited at 10k scale.
- **Worker capacity**: staging login timeouts were traced to worker saturation under load, not
  slow code. `WEB_CONCURRENCY` matters; long-running work belongs on Celery (`panel_fetch` for
  panels); anything added to the request path must be near-instant.
- **Caching layers**: server API caches (per-Location, 7-day `LocationCache`), pin-detail
  freshness windows (UL-277 reports wrong "fresh" marking), client pin cache, saved-filter cache.
  When adding a cache, define: key scope (user!), invalidation trigger, and version bump story.
- **Static/asset pipeline**: css minification (UL-366), smaller mobile css (UL-365), image/API
  result caching (UL-113) are all open.

### 3.2 Maintainability

- **Inline JS → TS modules** (UL-289, listed twice in TODO — it's wanted): the `frontend/ts/`
  tree with `entries/` + `shared/` + tests is the pattern; large inline `<script>` blocks in
  templates are the anti-pattern. Chip away per-page; add unit tests for extracted engines
  (see `markup-engine.test.ts`, `pin-cache.test.ts` precedents).
- **Template partial reorganization** (UL-292) and duplicate template detection (UL-288).
- **The queryset/manager epic is DONE** (every concrete model has a queryset or a documented
  exception — see `docs/prompts/todo.md` final notes). Maintain it: new models ship with
  `queryset.py`; new repeated `.filter(...)` shapes get named queryset methods.
- **Legacy service → plugin conversion** (UL-294): weather, geocoding, search providers,
  routexl, wayback, overpass, datagov, digital commonwealth, apple maps, google earth,
  openhistoricalmap still register in `SERVICE_REGISTRY` directly. Converting them is
  low-risk, well-templated work (37 builtin plugins to copy from).
- **Docstrings**: Google style, everywhere, Sphinx-consumed. Ruff enforces much of style;
  always run `ruff check --fix` before hand-fixing.
- **MyPy**: fix types at the origin; no `cast`-to-silence. Known generics gaps: manager
  typing for `Badge.objects.tags()` (UL-126) and `user.profile` (UL-127) — a proper
  generic `Manager`/`QuerySet` typing pass in `models/abstract/` would fix a whole class.

### 3.3 Extensibility

- The **plugin contract** (`docs/plugins.md`) is the extension surface: rate-limited services,
  panels, imagery providers, name providers, enrichment sources, hooks. When adding a *new kind*
  of contribution (e.g. a new panel family, a routing provider chain, an export format), extend
  the plugin contract rather than hardcoding a registry — the cost is small and it keeps
  third-party parity.
- **Import/export formats** live in `services/import_formats/` with documented formats
  (`docs/import_formats.md`); new formats (XLS UL-162, KML/GPX/GeoJSON/CSV *export* UL-382,
  targeted/filtered exports UL-377) should slot into that framework symmetrically.
- **AI gateway** is pluggable (OpenAI/Cloudflare/HF/Ollama). New AI features (trip suggestions
  UL-60, chat assistant UL-293, county-strategy property lookup UL-46) go through the gateway
  with per-user limits and the extraction-review pattern (`/ai/extractions/`) as precedent for
  human-in-the-loop application of AI output. AI sandboxing (UL-163) becomes mandatory before
  any MCP/tool-use integration.

### 3.4 Security posture (standing items)

- **No sanitization library site-wide** — XSS defense is discipline: escape-by-default Django
  templates, no `innerHTML` with user data (map popups and tag chips were fixed; new JS must not
  regress), `json_safety.py` for `</script>` breakout. UL-362 (badge-name/all-fields XSS audit)
  remains open; adding DOMPurify or equivalent at trusted-sink boundaries is worth considering.
- **Import security audit** (UL-268) partially done (extraction caps, forged-relationship
  rejection); remaining: archive-bomb depth, content-type validation, image parsing hardening.
- **Permission double-checks** (UL-39): the pattern of dead unscoped controller methods that
  bypassed scoping (removed in `a41af9f6`, `9cf0f352`) argues for defense-in-depth: queryset-level
  scoping (`for_profile(...)`) as the *only* entry point, so a forgotten view-level check fails
  closed. Audits should grep for `objects.get(`/`objects.filter(` in controllers that don't go
  through a scoped queryset.
- **Rate/abuse limiting for users** (not just external APIs): DDOS/spam (UL-70), share caps
  (UL-299), pin-creation caps (UL-107), username-change limits (UL-145), login lockout exists.
- **Bandit + AI vuln scans in CI** (UL-31); pre-commit hooks fully wired (UL-15); API-key
  referrer restrictions (UL-137); log rotation/purge for incident response (UL-136).
- **Non-anonymized URL sweep** (UL-40): verify no sequential-ID routes remain.

### 3.5 Testing strategy

- **TDD for bug reports**: failing test first, then fix. Use pytest (never `manage.py test` —
  staticfiles-manifest 500s), always set a unique `UL_TEST_DB_NAME`.
- **Hypothesis property tests wherever possible** (UL-120/369) — but `@given` + `self.client`
  don't mix in this repo's TestCase (state leaks across examples): property-test pure
  logic/services directly; plain tests for views. (Fixing TestCase so they *do* mix is itself a
  wanted task — see the note in `docs/NOTES.md`.)
- **Don't test log-message strings** or trivial code; do mock external services.
- **Coverage buckets** (UL-369): the long-term plan is separable coverage for AI-generated vs
  hypothesis vs human-written tests. Current whole-suite: ~70% lines, 4846 tests, ~2h15m runtime.
  A test-suite *runtime* budget is becoming a real concern — prefer service-level tests over
  full-page renders where equivalent.
- **Known worst-covered security-adjacent modules** (from the 2026-07-18 full run):
  `controllers/safety.py` (49%), direct-message-shares trio (54–58%),
  `services/google_oauth.py` (39%), `consumers.py` (35%). These are the highest-value
  coverage targets in the repo.
- **Integration/E2E** (UL-368): nothing exists today. The chiron dev server
  (`https://dev.urbanlens.org`) is the natural target for a Playwright-style smoke suite
  (login → map load → pin create → pin detail → trip create).
- **Review AI-created tests for uselessness** (UL-38) — inflated coverage from assertion-free or
  tautological tests corrupts the coverage signal everything above depends on.

---

## Part 4 — Prioritized Work Areas

Ordering within each tier is roughly by (user impact × risk × leverage). IDs reference `TODO.md`.

### 4.1 Tier 1: Correctness & privacy bugs (do these first)

1. ~~**Per-user client cache isolation** (UL-239)~~ RESOLVED 2026-07-18 (`7f612479`) — the pin
   (`ul_pins_v5_<uuid>`) and layer (`ul_layers_v1_<uuid>`) caches were already profile-scoped by
   key. The actual leak was three `LocationSearchEngine.attach()` `historyKey` values (main map
   address search, comment-map composer search, safety check-in destination search) that were
   hardcoded, unscoped localStorage keys - a hardcoded `'ul_addr_history_v1'` sat directly next
   to a correctly-scoped `recentPinsKey` sibling in the same object literal, which is what made
   the inconsistency obvious once looked for. Fixed with a profile id/uuid suffix on each key
   plus a one-time `removeItem` of the stale unscoped entry so already-leaked history doesn't
   linger. Verified with `test_search_history_cache_scoping.py`.
2. **Map cache at 8k+ pins** (UL-355) — DOWNGRADED 2026-07-18 per Jess's own note in `TODO.md`:
   the observed `QuotaExceededError` may have been a stale-cache symptom, not a true 8.5k-pin
   quota problem (clearing the cache fixed it for the reporting user). Don't build an
   eviction/IndexedDB migration off this alone — reproduce with a fresh cache at genuine scale
   first, per §3.1's own over-engineering warning.
3. ~~**Finish/verify boundary-mate wiki access**~~ RESOLVED — see §1.3.1, fully committed and
   re-verified 2026-07-18.
4. **Settings persistence bugs** — UPDATED 2026-07-18: this was NOT one shared root cause.
   "Theme page allows selecting two default map layers at once" was already fixed (a JS
   comment in `settings/index.html` around line 1830 describes and fixes exactly this symptom,
   mirroring `map_center_mode`'s `selectMapMode()`) — server-side rendering is also provably
   single-value-exclusive by construction (a straight `==` comparison per radio tile). UL-255
   ("remember last map position") is server-side sound (see `test_save_map_position_view.py`
   / `test_profile_map_center.py::GetMapCenterRememberModeTests`); `docs/PROBLEMS.md` has the
   likely actual (client-side URL-precedence) cause — not a settings-save bug. UL-34 ("user
   settings don't seem to properly save") remains genuinely unverified: it's
   too vague to reproduce without asking Jess which specific setting/page - do that before
   spending more time on it, rather than guessing at a shared cause that these two related
   items turned out not to have.
5. **Filter-view page defects cluster** (prompts backlog): page overflows footer; deleted
   include/exclude polygons resurrect on next draw; map preview doesn't refresh on criteria
   change; icon picker dead; badge picker lacks the map-sidebar's features. One agent should own
   the whole page against the map sidebar as reference implementation (§2.3).
6. **Pin-detail cache freshness** (UL-277) — items marked "fresh" after 10+ minutes;
   audit the freshness-window computation in `external_data.py`.
7. ~~**Filter correctness**: unrated pin passing a rating filter (UL-270); sliders ignoring
   0/"unrated" (UL-296)~~ RESOLVED 2026-07-18 (`18d03c3d`) — `filter_by_criteria`'s min/max
   rating and danger criteria used walrus-truthiness (`if x := ...:`), silently skipping the
   filter whenever a slider was set to exactly 0; sibling filters (priority, vulnerability,
   detail_pins) had already been correctly fixed to `is not None` at some earlier point, only
   rating/danger were missed. Rating additionally needed 0 special-cased as "unrated" since the
   app never persists a real `Review.rating=0` row (`pin_edit.py` deletes the review instead).
   **Still open**: un-identifiable active filter state (UL-271) — unrelated, not investigated.
8. **Bulk edit shared-properties display** (UL-353) and organize badge-edit cache staleness
   (UL-279).
9. ~~**Comment thread integrity** (UL-219)~~ RESOLVED 2026-07-18 (`ce4b6af1`) — added
   `Comment.parent_deleted` (migration 0076) + a `pre_delete` signal (mirroring the existing
   `map_removed` pattern) flagging replies before `parent` is nulled by `SET_NULL`; the comment
   panel now renders a "Replying to a comment that was deleted" tombstone placeholder instead of
   the reply silently reappearing as an unexplained top-level comment. Shared across pin and
   wiki comment panels (`_build_context`/`_comment_body.html`).
10. ~~**First-install "welcome back" login copy** (UL-179)~~ RESOLVED 2026-07-18 (`e9d374c5`).
    ~~**Map initial-position flash** (UL-221)~~ RESOLVED-PENDING-BROWSER-VERIFICATION 2026-07-18
    (`82f11178`) — a returning GPS-mode user's map silently jumped once a fresh geolocation fix
    superseded the cached position it loaded at, contradicting the code's own comment that
    already promised no jump; fixed to match that comment. Client-side JS - only verified the
    fix renders, not actual browser behavior.

### 4.2 Tier 2: Verification backlog (cheap to check, unknown risk)

Each of these is "confirm, then either close or convert to a bug": visit-history display
(UL-114), search-query disambiguation keywords (UL-117), child trips (UL-228), badge-kind-change
UX (UL-155), dialog drag-outside-closes (UL-32), duplicate code between `location/index.html`
and `satellite_view.html` (UL-288), wiki section missing on one pin (UL-385), Wikipedia missing
for some HRSH buildings (UL-354), takeout Parking.csv unreadable (UL-203), import-updates-names
flow (UL-207). Closing these shrinks `TODO.md` noise cheaply — batch several per session, with a
repro test per confirmed bug.

11. ~~**Password reset for SSO users** (UL-257)~~ RESOLVED 2026-07-19 (`def2c4d6`) — SSO-only
    accounts were silently dropped by Django's stock `PasswordResetForm.get_users()`, while the
    view showed the same "check your email" success page regardless, so those users were told it
    worked and got nothing. Added `SsoAwarePasswordResetForm` (matches SSO-only accounts too,
    routes them to a distinct email naming their sign-in provider) while preserving
    anti-enumeration. Also found and fixed the real reason none of this app's branded
    `registration/*` templates (reset form/done/confirm/complete, the reset email subject, the
    HTML email, even `logged_out.html`) were ever rendering: `TEMPLATES["DIRS"]` was empty, so
    `django.contrib.admin`/`auth`'s own bundled templates of the same name silently won the
    `app_directories` lookup (both registered ahead of `dashboard` in `INSTALLED_APPS`). Verified
    directly via `get_template().origin.name` for every `registration/*` template - a class of
    bug the existing test suite couldn't have caught, since tests use `assertContains` against
    whatever template rendered without checking *which* template that was.

### 4.3 Tier 3: High-leverage features (composable from existing infrastructure)

These have most of their machinery already built:

1. **API cost tracking + reporting** (UL-52/53) — `ApiCallLog` already records calls; add
   per-call cost estimates in gateways, an aggregation model, a site-admin report, and the
   public combined-costs page. Unblocks informed decisions about every future integration.
2. **Bulk rating + remaining bulk ops** (UL-193 rump) — `PinBulk*` framework exists.
3. **Auto-visits from photo timestamps + Immich/Google Photos** (UL-361) — composes photo
   matching (§2.7) with the existing visit-suggestion confirm flow.
4. **Geolocation visit creation** (UL-312) — one endpoint + the shared visit dialog.
5. **Targeted exports + more formats** (UL-377, UL-382) — filtered export via the existing
   criteria engine; KML/GPX/GeoJSON/CSV writers mirror existing readers.
6. **Sunrise/sunset & golden hour** (UL-345) — `astral` locally or Open-Meteo; no key needed;
   photographers are a core audience. Feeds trip planning and the weather panel.
7. **Trip page pin-add parity** (UL-342) — map-click/coordinate/place-search add exists on the
   main map; port to trip context.
8. **Notification UX debt** (UL-348 mark-read-on-view; UL-237 friend-request pipeline flow) —
   high daily-use annoyance, low complexity.
9. **Homepage dashboard widgets** (prompts backlog) — widget picker + reorder, persisted;
   builds on the existing home overview page.
10. **Onboarding first-map import prompt** (UL-181) + screenshots in About/README (UL-16) —
    cheap activation wins.

### 4.4 Tier 4: Features needing real design before code

Do not start these without a written design (add it to `docs/`):

- **Share caps by rate, not membership** (UL-299) — define the cap unit (distinct
  locations/exposures per rolling window), enforcement points (trip add, DM share, group share,
  list share), and UX for hitting the cap. Interacts with §1.3-3 provenance data (which is the
  natural counting substrate).
- **Community moderation without a global moderator** (UL-51) — constraint: no human can be
  shown pins they can't already see. The TODO sketches masked-detail moderation or
  community-jury models; needs a threat-model writeup first.
- **Location voting → "public" locations** (UL-58) — the growth flywheel, and the biggest
  privacy risk in the backlog. Requires: vulnerability assessment gates, vote thresholds,
  per-user opt-in, and probably staged rollout behind `SiteFeature`. This may not be achievable
  within the bounds of the project goals.
- **Report button** (UL-51-adjacent), **hide/mute user** (UL-27) — moderation primitives that
  interact with blocking semantics (§1.3-2); define the visibility matrix
  (block vs mute vs hide) once, in one doc, before implementing any of the three.
- **Meetups** (UL-283), **connections vs friends tiers** (UL-66), **encountered users**
  (UL-100), **advanced privacy overrides** (UL-101) — social-graph expansions; each changes the
  privacy-tier matrix that ~9 visibility controls already bind to; design them against the
  existing 7-level granularity system, don't bolt on an 8th ad-hoc level.
- **Safety-checkin token/timing audit + timed information release** (UL-371) — see §2.5;
  security-sensitive; write the disclosure timeline spec first.
- **Property ownership records via county-strategy AI** (UL-46) — needs the AI-sandbox
  groundwork (UL-163) and a strategy-persistence schema; big but well-sketched in TODO.
- **Offline maps** (UL-287), **native apps** (UL-72+) — out of current scope; note only.

### 4.5 Tier 5: UI polish backlog

The long tail of dialog/layout/dark-mode items in `TODO.md` (UL-146/147, UL-182, UL-184,
UL-190, UL-210, UL-230/231/233, UL-238, UL-300, UL-352, UL-384, and the trip-details list under
"UI - Trip Details Page"). Guidance: batch by page, reuse shared components (the standardized
badge picker, shared visit dialog, shared map toolbar — UL-210's dialog reinventing pickers is
the anti-pattern to kill), and check `docs/FEATURES.md` before building "new" UI — several
requests are already half-implemented. Notes that read "fix" or "improve" the ui without
concrete details should be assessed or verified by the user prior to making changes; many
of them may already be complete, and "fixing" them will lead to unwanted ui changes.

### 4.6 Tier 6: Codebase health epics (background, always-valid work)

- Inline-JS extraction to TS with tests (§3.2) — page by page.
- Plugin conversion of the remaining legacy services (UL-294) — service by service.
- Coverage pass on the four worst security-adjacent modules (§3.5).
- Hypothesis tests for pure services (`geo.py`, `filter_criteria.py`, `import_formats/*`,
  `email_normalization.py`, `text_limits.py`, `units.py` are natural targets).
- Docstring/mypy sweeps; the generics fixes (UL-126/127).
- Template partial reorg (UL-292); vestigial-asset cleanup task (UL-205, UL-370).
- `TODO.md` hygiene (UL-363): when you complete or invalidate an item, strike it with an
  evidence note (the 2026-07-18 strike-sweep set the precedent format).

### 4.7 API & data-source expansion (see `docs/api-expansion-candidates.md` for the full menu)

Priorities, biased free/open-first, aligned with that doc's recommendations:

1. **Wire up already-implemented gateways** that have no UI surface yet (OpenHistoricalMap,
   Wayback, LOC, Digital Commonwealth, Apple Maps) — zero new vendors, panels are cheap.
2. **Weather redundancy**: Open-Meteo is already a plugin; add NOAA/NWS (US alerts — safety
   relevance) and Meteostat (historical conditions for photo dates).
3. **Urbex-signal enrichment**: NRHP/SHPO historic registers, USGS MRDS mines, OSMRE abandoned
   mine lands, OpenInfraMap/OpenRailwayMap overlays, Socrata demolition permits — these are the
   *most on-mission* data adds in the whole candidates doc (demolition permits are the
   realistic path to the "Demolition Alert" dream, UL-81).
4. **Wikidata + OpenPlaques** structured heritage facts for wiki/name enrichment.
5. **Geocoding headroom**: Photon is integrated; consider self-hosted Nominatim before any new
   paid geocoder.
6. **Archives**: DPLA + Openverse + NYPL as additional media-archive panels via the existing
   `media_archives` plugin family.
7. **Routing**: OpenRouteService/OSRM to replace/supplement RouteXL for trip optimization
   (feeds UL-60 AI trip suggestions).
8. **Elevation**: USGS EPQS/Open-Elevation — cheap, useful for tunnels/drainage/terrain context.

Integration rules: `Gateway` subclass + plugin manifest + `SERVICE_REGISTRY`/plugin rate-limit
defaults + per-call cost estimate + cached per-Location + graceful degradation (§2.8). Ask the
candidates doc's four questions (geographic gap / redundancy / urbex signal / cacheable) before
adding anything.

### 4.8 Operations & infrastructure

- Staging worker saturation under load (login timeouts) — capacity/`WEB_CONCURRENCY` tuning;
  the code path was measured fast (~70ms login GET on chiron).
- HTTPS enforcement fix (`dfb04003`) is committed but **verify it's deployed**; recommend
  enabling the front-door (Cloudflare) always-HTTPS as first-line defense.
- CI/CD pipeline (UL-25), bandit in CI (UL-31), external error alerting (UL-69, Gotify exists
  for admin alerts — wire app-error paths into it), backup delivery off-server (UL-373 is the
  user-facing variant; admin DB backups exist but live on the same host).
- Docker compose file-watch for dev (UL-297).
- Setup experience: full setup docs (UL-50), "send test email"-style feature checks in the
  setup wizard (UL-135).

---

## Part 5 — Implementer's Gotcha Checklist

Compact recap of things that have each burned at least one prior agent. Scan before starting;
details in `CLAUDE.md` and `docs/NOTES.md`.

**Environment**: PowerShell + `.venv_windows\Scripts\*.exe`; Bash sandbox has no `/mnt/c`;
Docker only on chiron (push branch → pull there → `docker compose up --build -d`); GDAL via
pyogrio DLLs only when `UL_ENVIRONMENT=local`; sass via bun on Windows.

**Django/ORM**: no `save()` in `post_save`/`__str__`; `dispatch_uid` always; linter strips
signal guards — make them structurally redundant; migrations: indexes dead last, nullable+unique
in one `AddField`, `RenameIndex` runs immediately during squashes (fold into `CreateModel`);
slug lookups scoped by profile; one top-level pin per (location, profile) — and note Jess's
open question in NOTES.md about whether the sub-pin exemption should exist at all.

**Templates/HTMX**: no `{# ... #}` multi-line comments (rendered to users!); `htmx:afterSwap`
fires before `showModal()` (init Leaflet from after-request); `_page_hero.html` needs `id=`
passed in, never a wrapper div; `_pagination_controls.html` needs hardcoded `{% url %}` for
dual-rendered partials; `page.next_page_number()` raises — branch on `has_next`;
`"prefix-"|add:obj.id` silently returns `''` — `|stringformat:"s"` first (empty ids collapse
per-item DOM into duplicates).

**Frontend**: bump `CACHE_VERSION` on pin-payload shape changes; set `ul_pins_dirty` from
non-map pages that mutate pins; drag-select on Leaflet needs explicit toggle disabling
`map.dragging`; keep the data-ext-panel-204 marker protocol intact for empty panels.

**Testing**: pytest + `UL_TEST_DB_NAME`; no `@given` with `self.client`; no log-string
assertions; TDD for reported bugs; mock external services.

**Process**: out-of-scope discoveries → `docs/PROBLEMS.md` with repro detail; completed
prompts → `docs/prompts/completed.md`; feature inventory changes → `docs/FEATURES.md`;
non-obvious behavior → `docs/NOTES.md`; TODO strikes with evidence.

---

## Part 6 — Working Process & Definition of Done

1. **Pick work top-down**: Tier 1 before Tier 3 before Tier 5, unless directed otherwise.
   Batch small items by page/subsystem; keep one subsystem per session where possible.
2. **Check `docs/FEATURES.md` first** — many "missing" features exist. Then check §2 here for
   interactions your change must respect.
3. **Reproduce before fixing** (failing test), **design before building** (Tier 4 items get a
   doc in `docs/` first).
4. **Definition of done** for any change:
   - [ ] Invariants in §1.3 that the change touches are re-verified (ideally by test).
   - [ ] Ruff (`--fix`), py_compile on touched files; mypy clean at the origin (no casts).
   - [ ] Tests: TDD repro for bugs; hypothesis where the logic is pure; none asserting
         log strings; pytest with unique `UL_TEST_DB_NAME`.
   - [ ] Async where non-instant; progress indicator + toast; HTMX-first UI.
   - [ ] Docstrings on new classes/methods; docs updated (`FEATURES.md`/`NOTES.md`/this file);
         `TODO.md` item struck with evidence if completed.
   - [ ] New external calls: gateway + plugin + rate limit + cost estimate + cache.
   - [ ] New share/contact/visibility paths: provenance recorded, blocks enforced,
         no response-shape oracles.
   - [ ] Client caches keyed/invalidated correctly (`CACHE_VERSION`, `ul_pins_dirty`, per-user).
5. **When you find something broken outside your scope**: fix it if trivial and safe; otherwise
   `docs/PROBLEMS.md` with repro steps and file:line. Never silently work around it.
6. **When this document is wrong**: the codebase wins. Fix the document as part of your change.

---

## Appendix A — Current state snapshot (2026-07-18)

- Branch `@features/v0.5.0`; migrations through 0071; ~4850 tests, ~70% line coverage. (migrations will 
  be compacted before merging to main, which will impact the 0071 number)
- 37 builtin plugins; remaining unconverted services listed in UL-294.
- §1.3.1 boundary-mate wiki visibility: RESOLVED, fully committed (see §1.3.1 for detail).
- Recent themes (last ~20 commits): trust/security audit of export/import, blocking enforcement,
  calendar-sync revocation, account-deletion file cleanup, dead-unscoped-view removal, SSO/passkey
  coverage, full-suite coverage run + triage.
- Known-open coverage holes: `controllers/safety.py` 49%, direct-message-shares 54–58%,
  `services/google_oauth.py` 39%, `consumers.py` 35%.
- Deployment: verify the HTTPS-enforcement nginx fix (`dfb04003`) and the nginx healthcheck fix
  (`d9033b03`) are live in production; staging worker saturation still unresolved (infra-side).
