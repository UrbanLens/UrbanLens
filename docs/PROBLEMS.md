# PROBLEMS

Bugs or quirks identified during other work but out of scope to investigate/fix at the time.
Each entry should have enough detail (repro steps, file:line, symptoms) for a future session
to pick up without re-discovering the problem from scratch.

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

## Wiki-reference custom field picker doesn't recognize boundary-mate Locations

`src/urbanlens/dashboard/services/custom_field_references.py`, `referenceable_queryset()`'s
`"wiki"` branch (currently `Wiki.objects.filter(location__pins__profile=profile)`) duplicates
the exact-Location-row-only wiki-visibility check that
`services/wiki_access.location_visible_to` had until it was fixed in commit `15e6e2e2`
(2026-07-18) to also recognize a pin at a *different* Location whose point falls inside the
wiki's own generated boundary polygon (nearly-identical coordinates routinely resolve to
distinct Location rows - see that commit's message for the full rationale).

This second copy in `custom_field_references.py` was never updated, so a user whose pin sits on
the same building as an existing wiki - but at a boundary-mate Location row, not the wiki's own
exact Location - can now see and open that wiki (round 13 fixed that), but still cannot select it
as a target for a REFERENCE-type custom field pointing at "Wiki". Under-permissive, not a
security leak (the reverse - a bug that *grants* extra access - would be far more urgent).

**Why not fixed immediately**: `location_visible_to` is a per-object point-in-polygon check
(Python-side GEOS `.contains()`), not queryset-composable. A naive fix (loop over every other
Wiki in the DB calling `location_visible_to` per row) would be a real N+1/scalability risk on a
site with many wikis. The right fix is a shared, queryset-level "locations visible to this
profile via boundary-matching" helper that both `wiki_access.py` and
`custom_field_references.py` can call - worth building deliberately rather than bolting on.

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

## Identity-masking for hidden profiles: remaining render sites not yet covered (found 2026-07-18)

While building `services/identity_visibility.py` (masks a person's name/username/avatar when
their `profile_visibility` doesn't permit the viewer to see them - wired into the trip member
panel, trip activity attribution, trip comments, and both group-chat member/message surfaces),
a full-codebase audit turned up several more genuine leaks of the same shape, deliberately left
unfixed this round to keep that change reviewable. All of them show a person's raw
`username`/avatar/profile link even when that person's `profile_visibility` should hide it from
the current viewer:

1. **Trip list cards** - `templates/dashboard/partials/trips/trip_list_partial.html:64-68,86`
   (`membership.profile.user.username` in a tooltip + avatar image, `t.creator.user.username` in
   the creator badge). Whatever view renders the trip list (`TripListView` or similar) would need
   to resolve identities across every listed trip's members, not just one trip's - more diffuse
   than the single-trip render sites already fixed.
2. **Notification text bakes in the raw username at creation time**, so a template-side fix can't
   reach it: `controllers/trip.py:471` (`f'{inviter.username} invited you to join...'`),
   `services/group_chats.py:132,203` (`f"{creator.username} added you to the group..."`,
   `f"{actor.username} added you to the group..."`), `services/direct_message_shares.py:122`
   (trip-invite-via-DM). Each of these would need to resolve the inviter/actor's identity toward
   the *specific recipient* before formatting the stored `NotificationLog.message` string - the
   `suggest_mutual_connection` fix earlier in this same session (`services/connections.py`) is a
   worked example of doing this correctly (mask *before* string-formatting, not after).
3. **1:1 DM template inconsistencies** - `_thread.html` demonstrates the correct pattern for the
   thread header (`display_name`/`display_avatar_url` from `display_identity_for`) but still uses
   raw `partner.username` in four other spots in the same file: the block-confirm text (line 63),
   empty-state text (119), composer placeholder (142), and locked-composer text (166).
   `_message_items.html:32`'s quoted-reply header (`message.reply_to.sender.username`) has the
   same gap. Even the reference implementation isn't fully consistent.
4. **Pin/wiki comment author** - `templates/dashboard/partials/comments/_comment_body.html:3-12`
   renders `comment.profile.avatar`/`.username` raw. The comment *content* is correctly gated by
   `Profile.can_view_comments_from` (`controllers/comments.py:80,87,265`, an all-or-nothing
   "can see this comment at all" check using `comment_visibility`, a different field from
   `profile_visibility`) - but once a comment passes that gate, its author's name/avatar aren't
   separately masked the way `profile_visibility` would call for. Photo attachments already have
   a masking treatment for a related concern (`blurred_profiles`, same controller) showing the
   pattern exists but wasn't extended to the author identity itself.
5. **Group member-add search results** - `_group_member_results.html` /
   `GroupMemberSearchView` (`controllers/group_chats.py`) shows found profiles' real
   username/avatar unconditionally once they pass `can_direct_message`, without a
   `profile_visibility` check. Arguably lower priority (the searcher typed the exact username
   they're looking for), but worth a deliberate product decision rather than leaving it as an
   unreviewed gap.
6. **Trip comments have no `can_view_comments_from` gate at all** - unlike pin/wiki comments
   (`controllers/comments.py`), `_render_trip_comments` (`controllers/trip.py`) never checks the
   author's `comment_visibility` before including a comment. This is a different, more aggressive
   control (hides the whole comment, not just the author's identity) - deliberately not added as
   part of the identity-masking work, since the original ask was specifically about hiding
   name/avatar while keeping content visible, and changing content visibility is a separate
   product decision. Flagging since it's adjacent and was noticed in the same audit.

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

## Pre-existing stale test debt: `PinOverviewEditableTitleTests` (4 tests, all failing)

`src/urbanlens/dashboard/tests/hypothesis/test_pin_edit_controller.py:241-277` - a test class describing
an editable pin-title element (`pin-title--editable`, `data-raw-name`, `data-location-name`) inside
the pin detail page's **Details card** (`partials/pins/pin_overview_partial.html`, rendered by
`PinOverviewView`/`pin.overview`), wired to the `pin.quick_edit` endpoint (which genuinely exists -
`urls.py:171-173`, used by the main map popup's star-rating quick-edit today). All 4 tests currently
fail: `pin_overview_partial.html` has no title/name element of any kind - only the stats grid,
`deduplicated_identity_fields` rows, and the description row.

**Found while working on a related-but-distinct feature** (2026-07-18): "ensure the pin name (in the
hero) is edit in place" was implemented this session as a click-to-rename `<h1>` span in the page
**hero** (`.pin-name-editable`, `_pin_detail_hero_body.html`, wired to `pin.edit`) - a separate
element in a separate template from what these 4 tests describe. This looks like the same class of
issue as the `PinDetailsPageLinksCardTests` bug fixed the same session (a test written for a planned
or since-refactored piece of markup that was never built, or was removed without the test being
updated) - **not** something the hero-title change broke.

**Why not fixed now**: unlike the Links-card case (a clear "test points at the wrong template" bug
with one obvious correct answer), this one has a real product-design ambiguity: does the Details card
still need its *own* separate editable title now that the hero has one (redundant two-places-to-edit-
the-same-field UX), or were these tests written for an earlier design that the hero-based approach
superseded? That's a call for whoever's driving product decisions, not something to guess at while
mid-batch on an unrelated hero-layout task. If the Details-card title is wanted, `pin.quick_edit`
(single-field PATCH, already used for the map popup's star ratings) is the natural endpoint to wire
it to, matching the pre-written tests' `data-raw-name`/`data-location-name` contract. If it's not
wanted, delete `PinOverviewEditableTitleTests` instead.

---

## Satellite/street-view carousels: coordinate-null check is dead code

Found 2026-07-19 while deduplicating `PinController.satellite_view_carousell`/`street_view`
(UL-288) into a shared `_render_media_carousel` helper (preserved verbatim, not introduced by
the refactor). Both methods gate on `if lat is None or lng is None: return render(..., {"error":
"No coordinates available."})`, but `Pin.effective_latitude`/`effective_longitude`
(`models/pin/model.py:653-662`) are typed `-> float` and always `return float(self.location.
latitude)` - `Location.latitude`/`longitude` are non-nullable and immutable once set (see
CLAUDE.md's Location/Pin split), so these properties can never actually return `None`. The
null-coordinate branch in both carousel methods is unreachable.

This differs from the "null island" (0, 0) sentinel-coordinate gate used by the generic
`panel_info` dispatch elsewhere in the same file, which correctly checks falsiness (`not lat or
not lng`) rather than `is None` - that gate *is* reachable and correctly treats (0, 0) as "never
geocoded". The carousel methods' `is None` check looks like it was written assuming the same
sentinel-via-None convention, but as written it never fires, so a genuinely un-geocoded pin
(coordinates at (0, 0)) falls through to the readiness/collector path instead of the friendly
"No coordinates available." message.

**Why not fixed now**: out of scope for the dedup refactor it was found during, and the correct
fix depends on a product call the same way the sentinel-vs-null design already made once for
`panel_info` (checked in as `0, 0` = "never geocoded"): should the carousels adopt the same
falsiness check as `panel_info` (probably yes, for consistency), or is there a reason imagery
providers should still be queried at (0, 0)? If falsiness is the right call, mirror `panel_info`'s
existing gate rather than reinventing it.
