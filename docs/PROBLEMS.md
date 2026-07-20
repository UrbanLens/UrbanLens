# PROBLEMS

Bugs or quirks identified during other work but out of scope to investigate/fix at the time.
Each entry should have enough detail (repro steps, file:line, symptoms) for a future session
to pick up without re-discovering the problem from scratch.

**Status as of 2026-07-19**: worked through this backlog across several autonomous rounds this
session (see `docs/prompts/completed.md` for fix details). Everything cleanly actionable without
a browser, a product decision, or a large/risky refactor has been fixed: the ZIP re-import
malware-scan gap, the wiki-reference boundary-mate bug, the Cloudflare AI cost table, `tos_
accepted_at`'s missing UI, the satellite/street-view dead coordinate check, two orphaned `.pyc`
files, four of six identity-masking render sites, the stale `PinOverviewEditableTitleTests` test
debt, and the pin-description click-to-edit JS that investigation turned up along the way. Every
item still below is blocked on one of three things, noted per entry: (a) a product/policy
decision only a human can make (which TTLs, which radius, whether to mask two more identity
sites, whether to build vs. remove a dead settings toggle), (b) browser-based verification this
environment cannot do, or (c) a large, multi-file feature build or refactor that PROBLEMS.md's
own entry already flags as substantial follow-up work rather than a bug-fix-sized change - not
attempted blind, to avoid introducing new untested surface area in sensitive code (payments-
adjacent notification delivery, the encrypted-message export format, a 650-line map filter
sidebar). If picking this back up, the "Suggested next step"/"Why not fixed" text in each entry
is the starting point.

---

## UL-277: pin-detail external-data freshness window is one global knob, not per-source

Original wording (`TODO.md:28`): "Cache time needs adjustments for some pin details data. Load
page, wait 10 minutes, reload page, some items are marked as 'fresh'."

Verified the mechanism itself is technically correct, not buggy: `LocationCache.set()`
(`models/cache/location_cache.py:72-91`) upserts via `update_or_create`, which correctly bumps
`updated` (an `auto_now` field) on every write - no stale-timestamp defect. `LocationCache.is_stale`
compares `timezone.now() - self.updated` against a single value: `SiteSettings.get_current().
external_data_cache_days` - a **site-wide, multi-day, one-size-fits-all** setting applied
identically to every external-data source cached through `LocationCache` (Wikipedia, LoopNet,
NPS, EPA, satellite/street-view providers, etc.).

That's the actual gap: 10 minutes can never cross a days-scale threshold, so *any* source cached
this way is - by design - still "fresh" after only 10 minutes, regardless of whether that
specific source's real-world data changes fast enough to warrant a shorter window. This is a
genuine product/policy question, not a code defect: which specific sources need a shorter TTL
than the current global default, and what should each be? (Weather isn't cached via
`LocationCache` at all, so it isn't the culprit here - whatever "pin details data" the reporter
means is some other `PanelSource`.)

**Why not fixed**: implementing this properly means adding a per-source TTL (a new field on
`PanelSource`/`InfoPanelSource`, or a source→days mapping in `SiteSettings`) - a real feature
addition, not a bug fix - and I don't know which sources the reporter considers too slow to
refresh. Guessing at specific TTL values per source without that input risks either not fixing
the actual complaint or breaking the deliberate multi-day caching that protects rate-limited
upstream APIs for sources that genuinely don't need to refresh often.

**Suggested next step**: ask which specific pin-detail panel(s) felt stale after 10 minutes, then
add a per-source override (defaulting to the existing global `external_data_cache_days`) rather
than lowering the global value for everything.

---

## UL-255: "Remember last map position" - server side verified correct; likely real cause is unrelated URL-view-sync precedence, needs browser verification

Investigated the whole chain: `MapCenterForm.save()` (`forms/settings_form.py:380`), the profile
model's `get_map_center()`/`get_map_center_template_context()` (`models/profile/model.py:602-650`),
`SaveMapPositionView` (`controllers/settings.py:465-495`, correctly debounced 800ms + gated on
`map_center_mode == REMEMBER`), and the map page's own JS (`pages/map/index.html:1788-1836`,
correctly debounced with a `sendBeacon` fallback on `pagehide`/`beforeunload` so the last pan/zoom
before closing the tab isn't lost). All of it is correctly wired - `MapCenterMode.REMEMBER` = the
same lowercase `"remember"` string on both the Python enum and the JS string comparison, and the
server-rendered `_SERVER_CENTER_LAT`/`_MAP_CENTER_MODE` template vars correctly reflect
`profile.remembered_map_lat/lng` when the mode is REMEMBER.

**The more likely actual cause**: the same page has a *separate, unrelated* shareable-map-view
feature (`pages/map/index.html:709-779`) that writes `?lat=&lng=&zoom=` into the URL bar via
`history.replaceState`/`pushState` on every pan/zoom, and on page load:
```js
const _urlMapView = _parseMapViewFromUrl();
const _initialCenter = _urlMapView ? _urlMapView.center : _serverCenter;
```
`_urlMapView` (from the URL query string) takes **absolute priority** over `_serverCenter` (the
REMEMBER-mode value from the server) whenever present. Reloading the *same tab* after panning
would appear to "remember" the position via the URL, independent of whether REMEMBER mode or the
server round-trip is actually working - masking a real failure there. Conversely, navigating fresh
(new tab, bookmark, clicking a nav link with no query string) should correctly fall through to
`_serverCenter` per the code as written; if the bug report describes that specific scenario still
failing, the actual defect is somewhere I haven't found yet.

**Why not fixed**: I can't run a browser in this environment to confirm which scenario the
reporter actually hit, and I don't know whether `_urlMapView` winning over REMEMBER-mode is
intentional (a shared/bookmarked map-view link arguably *should* override a viewer's own
settings) or the bug itself. Changing the precedence without knowing the intended semantics risks
breaking the shareable-link feature, which is clearly a deliberately, carefully built feature
(pushState/replaceState/popstate handling, debounced sync) - not something to touch on a guess.

**Suggested next step**: ask the user which exact reproduction they mean (same-tab reload after
panning, vs. a genuinely fresh navigation), or add a `has_map_view_url` marker distinguishing "the
URL carries an intentionally-shared view" from "this tab's own view-sync happened to leave stale
params" - the former should win, the latter probably shouldn't.

---

## Most per-event WhatsApp/SMS notification toggles are stored but never delivered (found 2026-07-18)

`NotificationPreference` has `*_whatsapp` opt-in booleans for ~10 event types (trip_updated,
friend_request, comment_reply, comment_liked, friend_accepted, added_to_trip, wiki_updated,
pin_shared, visit_suggested, ...), settable in Settings → Account — but the only delivery code
that ever reads any of them is the safety check-in path (`services/safety.py:518-521`) and, as of
the 2026-07-18 DM audit fixes, the new-message path (`services/direct_messages.py`
`_schedule_message_text_alerts` → `tasks.send_direct_message_text_alerts_if_unread`). Every other
toggle silently does nothing: a user who enables "trip updated → WhatsApp" never receives
anything. Fix pattern to follow: the DM implementation (delayed Celery task, re-check relevance,
per-streak debounce, dispatch via `services/notification_delivery.py`). Alternatively, if these
channels aren't wanted for a given event type, remove its toggle from the settings UI rather than
leaving a dead control. `docs/FEATURES.md`'s notification-matrix description was corrected to
reflect current reality.

---

## ~~Profile hero renders the bio/ghost-viewer content twice under certain conditions~~ (RESOLVED 2026-07-18 - both were test false-positives, not rendering bugs)

Originally logged as two suspected profile-page rendering bugs surfaced by failing tests.
Root-caused and closed: **neither was a real bug** - both tests false-positived on markup/script
text legitimately introduced by the bio click-to-edit slice, the same inert-string false-positive
class every other profile-page test had already been hardened against:

1. `test_bio_appears_exactly_once` - the click-to-edit bio element carries the raw bio twice in
   markup BY DESIGN (`data-raw-bio="..."` attribute + visible element text), so a raw substring
   count of 1 became impossible the moment the editor shipped. No visible duplicate ever existed.
   Test rewritten to count visible-text occurrences (`>` + bio) only.
2. `test_previewed_page_renders_as_other_user_with_banner` - the bio editor's wiring script
   contains the JS comment "// autosave endpoint the full Edit Profile page's fields already use",
   so `assertNotContains(response, "Edit Profile")` matched inert script text on every render.
   The ghost preview correctly hides the real Edit Profile button (it's gated on
   `profile.user == request.user`, and the middleware swaps `request.user` for the ghost).
   Test rewritten against script-stripped content.

Both tests pass again; nothing in production code needed changing.

---

## Identity-masking for hidden profiles: two remaining render sites need a product decision (found 2026-07-18, narrowed 2026-07-19)

`services/identity_visibility.py` masks a person's name/username/avatar when their
`profile_visibility` doesn't permit the viewer to see them. Of the six gaps originally found in
a full-codebase audit, four are now fixed (trip list cards, notification text baked in at
creation time for trip-invite/group-add, 1:1 DM template inconsistencies, pin/wiki comment
author - see `docs/prompts/completed.md`'s 2026-07-19 entry for the fix details, and the new
`mask_profile_references` helper in `identity_visibility.py` that both the trip-list and
comment-author fixes now share). Two remain, both explicitly needing a human product call rather
than an autonomous fix:

1. **Group member-add search results** - `_group_member_results.html` /
   `GroupMemberSearchView` (`controllers/group_chats.py`) shows found profiles' real
   username/avatar unconditionally once they pass `can_direct_message`, without a
   `profile_visibility` check. Arguably lower priority (the searcher typed the exact username
   they're looking for), but worth a deliberate product decision rather than leaving it as an
   unreviewed gap.
2. **Trip comments have no `can_view_comments_from` gate at all** - unlike pin/wiki comments
   (`controllers/comments.py`), `_render_trip_comments` (`controllers/trip.py`) never checks the
   author's `comment_visibility` before including a comment. This is a different, more aggressive
   control (hides the whole comment, not just the author's identity) - deliberately not added as
   part of the identity-masking work, since the original ask was specifically about hiding
   name/avatar while keeping content visible, and changing content visibility is a separate
   product decision. (Note: trip comment AUTHOR identity was already correctly masked before this
   round even started - `_render_trip_comments` resolves and re-points `c.author`/`r.author` via
   `resolve_visible_identities`, rendered through `trip_comments_panel.html`'s own
   `display_name`/`display_avatar_url` markup, a separate template from pin/wiki's
   `_comment_body.html`. Only the content-visibility gate itself is the remaining gap here.)

---

## Saved-filter include/exclude label picker: no drag-reorder or formula mode

`src/urbanlens/dashboard/templates/dashboard/partials/pin_lists/_saved_filter_label_picker.html`
+ `initSavedFilterLabelPickers()` (`_saved_filter_dialog_scripts.html`) now give the saved-filter
detail page and create/edit dialog a search-driven chip picker for include/exclude labels,
matching the main map filter sidebar's core interaction (search a suggestions list, click to add
a removable chip). Two richer features present on the main map's own filter sidebar
(`pages/map/index.html`, ~lines 4451-5099) were deliberately not ported over:

- **Drag-and-drop** (dragging a chip between the include/exclude boxes, or reordering within one).
- **Advanced filtering formulas** (boolean/grouped label logic beyond a flat include/exclude set).

**Why not fixed**: the main map's version is ~650 lines of tightly-coupled inline JS/markup
specific to that page's own closure-scoped `fp-*` element IDs, with no existing extraction seam -
pulling it out into a shared, reusable module (the right way to do this, given both consumers
should behave identically) is a substantial refactor of a large, sensitive, frequently-touched
file, not a bug-fix-batch-sized change. A smaller reusable factory already exists on that page
(`_makeLabelChipPicker`, ~line 3115, used by the bulk-edit dialog) but it also has no
drag/formula support - it's the same tier of feature the saved-filter page now has, not the
richer sidebar version. Worth a deliberate future task: extract the main map's rich picker into
a shared TS module (`frontend/ts/shared/`), used by the main map, bulk-edit dialog, and
saved-filter page alike.

---

## Data export: comments/photos/trips/direct_messages have no importer

`services/export.py` exports `comments.json`, `photos/` (files + metadata), `trips.json`, and
`direct_messages.json`, but `services/import_data.py`'s `_IMPORTERS` dispatch table has no entry
for any of the four - `_IMPORT_ORDER = ["labels", "pins", "custom_fields", "pin_lists",
"visit_history", "connections", "settings"]`. A full round-trip export→import silently drops
these four categories even though they're present (and correct) in the archive.

**Why not fixed as part of the export-completeness pass**: each of these four is meaningfully
harder than the categories that already round-trip, and this gap predates the current session's
work (these exporters already existed; only the *newer* feature fields - articles, ratings,
security indicators, media labels, expanded settings - were in scope this round, and those were
folded into the existing pins/photos/profile/settings/custom_fields importers, which now round-
trip correctly - see docs/prompts/completed.md's "Data export completeness" entry):

- **Photos**: the export copies real files into the archive's `photos/` folder. Importing needs
  to re-upload each file into storage, respect the importing profile's quota
  (`services.storage.get_quota_bytes`), and re-associate via `target_type`/`target_name` (a
  human-readable string in the export, not a uuid - matching back to a pin/wiki reliably needs
  either resolving `target_name` against `pin_uuid_map` or exporting a `target_uuid` instead).
- **Comments**: same `target_type`/`target_name` resolution problem as photos, plus a question of
  whether comments should re-target pins/wikis at all versus just being informational.
- **Trips**: `members` is a list of *usernames*, not uuids - re-creating a multi-member trip on
  import would need to resolve each member to a local Profile (only where one exists - members
  are other people's accounts, not something an import can create) and decide the right
  membership status/role for them, which raises the same "requests-not-facts" question
  `_import_connections`'s docstring already discusses for friendships.
- **Direct messages**: encrypted messages export raw ciphertext keyed to the exporting device's
  current key version (`key_version`) - importing them anywhere requires the same E2EE key
  material to still be valid, which is a materially different (and riskier) undertaking than
  the other three.

If a future session tackles this, `comments`/`photos` are the more tractable pair (fix the
target-reference shape first, in export.py, so the importer has something reliable to match on).

---

## Property records: Tier 2/3 framework is real and working, but no vendor/county is populated yet

`docs/property-records-plan.md` designs a 4-tier fallback pipeline for county property/tax
records. All four tiers are now implemented end-to-end in
`services/apis/property_records/` and wired into `plugins/builtin/property_records.py`:
jurisdiction registry + Census-based resolution (Tier 0), a generic ArcGIS/Socrata client (Tier
1), a vendor-template routing layer (Tier 2) sharing an HTML scrape/recipe
engine (`html_scrape.py`) with per-county bespoke recipes (Tier 3), an explicit `MANUAL_ONLY`
short-circuit (Tier 4), and per-field merging across however many tiers a jurisdiction has
configured (`merge.py`, plan section 4 - lower tier number wins per field, disagreements are
flagged in `field_mismatches` rather than silently resolved). `orchestrator.get_property_record`
tries every tier a jurisdiction has real configuration for and merges whatever succeeds; a
jurisdiction with nothing configured for a given tier gets a `PropertyRecordsUnavailableError`
with a specific machine-readable reason rather than a silent gap.

**What's still missing is data, not code**, and that's deliberate rather than an oversight:

- **No `PropertyJurisdiction.scrape_recipe` has been populated for a real county.**
  `discovery.discover_tier3_recipe` (AI-assisted, cross-validates the model's proposed form field
  against the real page's actual `<input name=...>` attributes - it can't hallucinate a field
  that doesn't exist) is implemented and unit-tested, but has never been run against a live site
  in this session. `apply_tier3_discovery` deliberately
  never sets `last_verified` - it confirms the field exists, not that submitting it returns real
  data - so any recipe it saves needs a human to confirm against one known real property first.
- **No headless-browser executor.** The plan's Tier 3 describes "browser automation" for
  JS-heavy sites; `html_scrape.execute_scrape_recipe` is plain `requests` GET/POST, which works for
  query-string-driven sites (e.g. qPublic's `KeyValue=` pattern)
  but not old-style ASP.NET `__VIEWSTATE` postback forms or anything JS-rendered. Adding Playwright
  (no existing browser-automation dependency in this project) is real, scoped follow-up work if a
  target site needs it - not attempted here to avoid a large new infra dependency without a
  concrete site that actually needs it.
- The `discover_property_jurisdiction` command (both `--tier1` default and `--tier3` modes) has
  never been run against a real search provider/AI backend end-to-end in this session (only
  unit-tested with mocks) - worth a smoke-test pass once a specific county is targeted.

---

## UL-354: Wikipedia missing for some HRSH buildings - likely geosearch radius, not a bug

Original wording (`TODO.md:30`): "Wikipedia not showing up for some HRSH buildings." HRSH (Harlem
Valley/Hudson River State Hospital-style abandoned psychiatric campuses) are exactly the kind of
site this app's urbex audience pins: a single historic complex sprawling across many acres, with
dozens of individual pins for separate buildings scattered around the same campus.

`WikipediaGateway._geo_search()` (`services/apis/assets/wikipedia.py:282-298`) queries Wikipedia's
`geosearch` API with a **500m radius** (`_RADIUS_METERS`) and takes the closest 5 candidates
(`_MAX_CANDIDATES`). A large historic hospital campus typically has exactly one Wikipedia article,
geotagged at a single point (usually the main/administrative building). Individual building pins
within 500m of that point would find the article; pins for outbuildings further out on the same
campus (which for a site like this can easily span 500-1000+ meters) would get zero geosearch
candidates and correctly render "no article found" - matching the reported symptom exactly
(*some* buildings show it, others on the same campus don't).

**Why not fixed now**: this is a plausible, well-reasoned hypothesis based on how the mechanism
works, not a confirmed diagnosis - there's no specific pin/coordinate attached to the report to
verify it against directly (unlike UL-385, where a pre-written failing test proved the exact
mechanism). It's also not obviously a "bug" to fix by just widening the radius: 500m is presumably
tuned to avoid false-positive matches (an ordinary pin picking up a nearby-but-unrelated article),
and blindly increasing it site-wide risks introducing exactly that regression for every other pin
in the app. The real fix is a product call: widen the radius specifically for large-campus cases,
add a "same complex, different building" special case, or accept the limitation and let users
manually confirm/link an article via whatever manual-override path already exists (if any -
check `pin.wiki.create`/`_pin_detail_hero_body.html` for a manual Wikipedia-link mechanism before
assuming a code-only fix is even the right first step).

---

## `docker compose exec app pytest` can't reach Valkey - no documented way to run pytest against the dev container

Discovered while trying to verify the article-editor changes (2026-07-19/20) with real Postgres,
since local Windows dev has no Docker (per this project's CLAUDE.md) so `pytest` there never
touches a real database - DB-backed tests just error out with a connection refusal locally.

Tried running the suite inside the running `app` container on the dev VM instead
(`docker compose exec app python -m pytest ...`), which does reach Postgres fine, but every
session-touching test then fails with `RuntimeError: External network access is disabled during
tests. Attempted to connect to '172.18.0.2'` (or, with `UL_VALKEY_URL=redis://localhost:6379/0`
forced, a plain connection-refused) - see `core/tests/testing_network.py`'s
`LocalhostOnlyNetwork` guard, which only allows loopback connections during tests. The app's real
runtime `UL_VALKEY_URL` resolves to the `valkey` service's docker-network hostname/IP (correct for
serving traffic), which the guard then blocks as non-localhost, and there's no override I found
that both resolves to the running Valkey container *and* satisfies the guard's localhost check
from inside `app`'s network namespace.

**Not fixed now**: this is infra/tooling, not a product bug, and out of scope for whatever feature
prompted hitting it. Workaround used in the moment: ran the non-DB `SimpleTestCase` subset locally
on Windows (passes, catches import/syntax regressions) plus ruff/mypy, and relied on live
browser verification via Playwright-over-CDP against the dev server for the DB-touching behavior
instead. A real fix would be either a dedicated `docker compose run` test profile that points
`UL_VALKEY_URL` at `redis://localhost:6379` with Valkey ALSO listening on `app`'s loopback (e.g.
via a sidecar or `network_mode: service:app`), or relaxing `LocalhostOnlyNetwork` to allow the
compose network's Valkey service specifically during test runs.

---

## Hardcoded (non-theme-aware) `#2563eb`/`#4f46e5` blue in `_explainer.scss`, `_map.scss`, `_e2ee.scss`

Found while fixing the "illegible dark-blue Nominatim link color" report (see completed.md) - that
fix covered every `var(--undefined-name, #hex)` broken-reference instance of the same blue
(`--ul-primary`, `--ul-link`, `--color-accent`, none of which were ever actually defined, so they
always rendered their hardcoded fallback regardless of theme) across `_pin-detail.scss`,
`_messages.scss`, `_gallery.scss`, and `_markup.scss`, by pointing them at the real,
now-dark-mode-aware `--ul-primary-color` token.

These three files use the *same* blue (`#2563eb` in `_explainer.scss`/`_map.scss`, `#4f46e5` in
`_e2ee.scss`) but as genuine hardcoded literals - `color: #2563eb;`, several `rgba(37, 99, 235,
.NN)` washes, and `linear-gradient(135deg, #2563eb, #06b6d4)` gradients - not a broken variable
reference. `_explainer.scss` in particular builds a whole small design system out of this blue
(border/background/text all coordinated at different opacities) for its info-callout component, so
a blind find-replace to `var(--ul-primary-color)` on just the solid-color instances would leave the
rgba() washes mismatched. Left alone this round because verifying each is genuinely a dark-mode
legibility bug (vs. e.g. a component that's already fine because its own surface is intentionally
dark in both themes, like the lightbox case handled in the fix above) needs checking each
component's actual rendered surface, not just grepping for the hex value.
