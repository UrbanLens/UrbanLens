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

## PR #111 (v0.5.0b0 release): CodeQL flagged 60 "new" alerts, mostly pre-existing debt attributed to an oversized diff (found 2026-07-23)

Merging the `@features/v0.5.0` branch into `main` (PR #111) is a 300+-file diff, large enough that
GitHub's own CodeQL check flags every finding across it as "new to this PR" - its own output says
so directly: *"Alerts not introduced by this pull request might have been detected because the
code changes were too large."* One was a genuine, directly-fixable critical: `fetch_page_text`
(`services/ai/link_extraction.py`) auto-followed redirects and only validated hostnames at
submission time, so a hostile page could 302 the fetch to an internal address, or a DNS-rebind
could flip a hostname to a private IP in the gap before the Celery task ran - **fixed** same session
(manual bounded redirect-following with per-hop revalidation, plus resolving hostnames at validate
time instead of only checking literal IPs).

The other ~59 (18 high, 41 medium) were not worked through individually - two spot-checks turned up
false positives from this exact "oversized diff" effect, not real bugs:

- `services/apis/security/hibp.py:55`'s "weak sensitive data hashing" flags a `hashlib.sha1(...,
  usedforsecurity=False)` call - required by HIBP's k-anonymity Pwned Passwords API protocol itself
  (SHA-1 prefix lookup is the documented API contract, not an app-chosen algorithm), already marked
  `usedforsecurity=False` to say exactly that to scanners.
- `_notification_push.html:82,97`'s "xss"/"client-side-unvalidated-url-redirection" flags
  `window.location = n.url` - already gated behind a custom `isSafeUrl()` (same-origin-only,
  rejects `javascript:`/scheme-relative) with an explanatory comment. CodeQL's JS taint tracking
  doesn't model bespoke app sanitizer functions as a barrier, so this is very likely a false
  positive too, though not re-verified with the same rigor as the SSRF fix.

**Why the rest weren't triaged individually**: 59 alerts spanning dozens of files (stack-trace
exposure across ~15 controllers, more XSS/redirect sites, clear-text-logging claims in files that -
per `git log` - were already refactored specifically to redact sensitive values via
`redact_coordinate`/`redact_params` helpers, `py/bad-tag-filter` in four hypothesis test files) is a
full security-audit-scale task, not a PR-merge-blocking-CI-error-scale one, and this branch has
already been through many rounds of exactly this kind of security hardening (see this file's other
entries and `docs/prompts/completed.md`) - blind bulk-fixing without individually verifying each
against current (often already-hardened) code risks both false "fixes" for things already correct
and, worse, introducing new bugs across files this session never otherwise touched.

**Suggested next step**: pull the full alert list (`gh api "repos/UrbanLens/UrbanLens/code-scanning/alerts?ref=refs/pull/111/merge&state=open"`
while the PR is still open, or `state=open` against `main` after merge) and work through the
`py/stack-trace-exposure` cluster first - it's the largest single group (~28 instances across
`account.py`, `boundary.py`, `calendar_sync.py`, `direct_message_shares.py`, `direct_messages.py`,
`group_chats.py`, `maps.py`, `pin.py`, `pin_edit.py`, `pin_lists.py`, `safety.py`, `webauthn.py`,
`external_api/views.py`) and the most mechanically fixable if each is confirmed to actually return
`str(exc)`/traceback content in an HTTP response rather than just logging it server-side.

---

## PR #111: `0001_initial.py` rename may break `manage.py migrate` on an installation that only ever applied the old squashed name (flagged by Codex, verified against dev DBs 2026-07-23)

`d8eb1529` renamed `0001_initial_squashed_0006_alter_notificationlog_notification_type_and_more.py`
to `0001_initial.py` (pure rename, matching the 0007/0008/0009 squash-renames), and `b829d49b`
then dropped its `replaces` list entirely because its first entry, `('dashboard', '0001_initial')`,
collided with the file's own new name and made `manage.py migrate` fail immediately with
"Cyclical squash replacement found." Codex correctly points out that dropping `replaces` outright
loses the bridge for any database that only ever recorded the *old squashed name* as applied
(never the pre-squash individual `0001`-`0006` migrations under their original names) - such a
database would have `0002_boundary_emailsendlog_...` (etc.) marked applied while its now-renamed
dependency `0001_initial` is not, which is exactly `InconsistentMigrationHistory`.

**Verified empirically against two real databases** (`ssh` to the `chiron` dev VM, queried
`django_migrations` directly - see this session's transcript for the exact commands):

- `urbanlens_development_db` (the canonical `~/UrbanLens` checkout's DB): has **both**
  `0001_initial` through `0006_alter_notificationlog_notification_type_and_more` (the pre-squash
  individual names) **and** `0001_initial_squashed_0006_alter_notificationlog_notification_type_and_more`
  applied (the latter one second after the former six - Django's squash-bridging recording the
  squash as satisfied once its `replaces` list was fully applied, back when `replaces` still
  existed). `0001_initial` (matching the *new* filename) is therefore already marked applied here,
  so the rename is safe on this specific database: nothing to re-run, nothing inconsistent.
- `urbanlens_devs1_db`: only has the new-style `0001_initial` (a from-scratch install past this
  point already) - trivially safe, no legacy naming involved at all.

**Not verified**: the actual production database (Portainer-managed, not directly reachable the
way the `chiron` dev VM is per `CLAUDE.md`). If production's migration history went straight from
nothing to the *old* squashed name only (skipping the pre-squash individual `0001`-`0006` names
entirely - e.g. a deploy that started after the squash was introduced but before this PR's rename),
it would hit the exact break Codex describes.

**Why not blind-fixed**: the "obvious" fix (restore the full original `replaces` list) is what
caused the cyclical-self-reference bug `b829d49b` already had to back out. A *correct* fix needs
`replaces` to name the **old squashed filename** specifically -
`replaces = [('dashboard', '0001_initial_squashed_0006_alter_notificationlog_notification_type_and_more')]`
- not the pre-squash individual names, and not its own new name - but getting Django's
partial-replaces bridging semantics exactly right (what happens if only *some* of a `replaces`
list is applied) isn't something to guess at without a scratch database in the specific "only the
old squashed name applied" state to test against, which nothing currently reachable is in.

**Suggested next step before deploying v0.5.0 to production**: run the same read-only check this
session did - `SELECT name, applied FROM django_migrations WHERE app='dashboard' AND name LIKE
'0001%' ORDER BY id;` - against production *before* running `migrate`. If it shows only
`0001_initial_squashed_0006_alter_notificationlog_notification_type_and_more` (no bare
`0001_initial` row), the content is identical between the old and new file (confirmed via `git
show d8eb1529 --stat`: pure rename, 0 insertions/deletions) - the simplest safe remediation is a
one-time manual `UPDATE django_migrations SET name = '0001_initial' WHERE app = 'dashboard' AND
name = '0001_initial_squashed_0006_alter_notificationlog_notification_type_and_more';` immediately
before running `migrate`, rather than encoding a `replaces` bridge in code.

---

## PR #111: E2EE signup/password-reset only enforces an 8-char minimum client-side; the configured 12-char+complexity+common-password+HIBP policy never runs (flagged by Codex 2026-07-23)

`frontend/ts/shared/e2ee-client.ts`'s `wireSignupForm`/password-reset wiring derives a login
credential from the user's raw password client-side before submit specifically so the raw
password never reaches the server (`prepareSignupSubmit`) - a deliberate, documented design
(comment: "the server only ever sees the derived credential, which always 'looks strong'"). The
file already acknowledges the consequence and enforces one guard: `MIN_PASSWORD_LENGTH = 8`. But
`settings/base.py`'s actual `AUTH_PASSWORD_VALIDATORS` requires more: `MinimumLengthValidator`
(12, not 8), `UserAttributeSimilarityValidator`, `CommonPasswordValidator`,
`NumericPasswordValidator`, and two custom validators - `ComplexityValidator` and
`HaveIBeenPwnedValidator` (this project's own HIBP k-anonymity check, `services/apis/security/hibp.py`).
None of these run client-side, so with JS enabled (required for E2EE to work at all, i.e. the
normal path for every real signup) a password like `passwordpassword` (16 chars, passes the 8-char
floor) sails through, becomes a derived credential that trivially "looks" strong to the server, and
the account ends up with an effectively unenforced password policy.

**Why not fixed here**: no endpoint currently exists for a client to check a raw password against
the configured validators (checked: no password-strength AJAX endpoint anywhere in
`controllers/`/`services/`) - this needs new plumbing, either (a) a small endpoint that runs the
raw password through `django.contrib.auth.password_validation.validate_password` (including the
two custom validators) before the client proceeds to derive+submit, transmitted once over HTTPS
during signup only (a bounded, arguably-acceptable exposure, since a fresh signup password has no
alternative but to reach the server keystroke-for-keystroke at least once somewhere in any password
flow), or (b) reimplementing each validator's logic in TypeScript (duplicating `ComplexityValidator`
and keeping length/common-password rules in sync by hand - `HaveIBeenPwnedValidator`'s k-anonymity
check *could* be called directly from the browser against the real HIBP API without touching this
app's server at all, sidestepping the raw-password-to-our-server question for that one check
specifically). Choosing between those two designs is a security-relevant product decision this
session didn't make unilaterally.

**Suggested next step**: decide (a) vs (b) above, then wire whichever into `prepareSignupSubmit`
(and the password-reset equivalent) before the derive step, surfacing validator failures as the
same inline form errors the legacy non-JS path already produces server-side.

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

## Dev-server verification routine only rebuilt the `app` service, leaving `celery-worker`/`celery-worker-panels`/`celery-beat` stale (found 2026-07-20)

Process note, not a product bug: this session's whole deploy-and-verify loop (`docker compose up
--build -d app` after every feature, then live-check on `dev.urbanlens.org`) only ever rebuilt the
`app` service. Discovered while verifying the Location Data "Overview" tab (see completed.md) -
`ElevationPanelSource`'s background fetch (dispatched via `schedule_panel_fetch` to the real
`panel_fetch` Celery queue, consumed by `celery-worker-panels`) never completed; `docker compose
ps` showed `celery-worker`/`celery-worker-panels`/`celery-beat` all "Up 15 hours" - i.e. still
running whatever code existed before most of this session's feature work, including panel/plugin
registrations added earlier the same day. `celery-worker-panels`' own logs additionally showed an
unrelated stale-schema error (`relation "dashboard_property_jurisdiction" does not exist`) from a
migration that had since removed that table, confirming the worker was genuinely running old code
against the new database - not just old code against a matching schema.

**Impact on this session's own verification claims**: any prior feature this session that relies on
a real Celery task (background panel fetches, `_process_photo_upload`'s EXIF extraction, media
materialize's download) was verified correctly wherever verification ran `pytest`/`manage.py shell`
*inside the `app` container* (those import the current code directly, unaffected) or eagerly
invoked the task function rather than dispatching through the real queue. Anywhere verification
specifically exercised the real async dispatch path via a live browser (i.e. actually waiting for a
background fetch to land), it could have been silently checking against stale worker code without
that being obvious - the request would still return promptly (schedule_panel_fetch itself doesn't
block on the worker), so a stuck/stale fetch looks identical to "still fetching, poll again" rather
than an obvious failure.

**Fixed for future rounds of this session**: now rebuilding `celery-worker celery-worker-panels
celery-beat` alongside `app` in the same `docker compose up --build -d` call whenever a change
touches Celery task code. **Not retroactively re-verified**: earlier features this session that
depend on the real async pipeline (media materialize, EXIF direction extraction via real photo
uploads, the drag-drop-onto-map materialization) were not re-checked against a freshly-rebuilt
worker after this was discovered - their pytest-level verification (which doesn't go through the
stale worker) should still be valid, but a live "upload a real photo and watch it process" check
specifically was not redone. If a discrepancy shows up in one of those features, rebuild the worker
services first before assuming the feature code itself is wrong.

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

---

## `max_upload_file_size_mb` (admin-configurable, up to 20,000MB) isn't coupled to clamd's `StreamMaxLength` (found 2026-07-20, fixed 2026-07-20)

While fixing the "250kb image upload rejected as too large to scan for malware" report (see
completed.md), couldn't reproduce the exact failure against the dev server's live clamd daemon - a
250KB `InMemoryUploadedFile` run through the real `image_upload_error`/`malware_error_for_upload`
path scanned clean (verified via `manage.py shell` on `urbanlens_development_app`, talking to the
real `urbanlens_development_clamav` container). But found a real, structural mismatch that's almost
certainly the actual cause (with "250kb" most likely a typo/misremembering of "250mb," or the
original report's environment having an even smaller clamd limit than dev's): `StreamMaxLength` is
left unset in `docker-compose.yml` (commented out in clamd.conf, so clamd falls back to its own
compiled-in default - the commented reference value is `25M`), while `SiteSettings.
max_upload_file_size_mb` (`file_size_error_for_upload`, checked *before* the malware scan) defaults
to 250MB and is admin-adjustable up to 20,000MB via the site-admin UI. Any upload between clamd's
actual stream cap and the site's configured max passes our own size check but then fails clamd's
with the confusing "too large to scan" message - which is real regardless of whether it's exactly
what the original reporter hit.

**Fixed**: pinned `CLAMD_CONF_StreamMaxLength=1000M` in `docker-compose.yml` (the image's `/init`
entrypoint turns `CLAMD_CONF_<Directive>` env vars into clamd.conf directives), comfortably above
the 250MB default, and added the actual byte count to the warning log when `BufferTooLongError`
fires so a recurrence is diagnosable instead of a total mystery. Then closed the drift risk
structurally: lowered `MaxValueValidator(20_000)` on `SiteSettings.max_upload_file_size_mb` to `900`
(migration `0088`, which also clamps any already-persisted value above 900 down to it), matched
`site_admin.html`'s `max="20000"` to `900`, and - the part that actually mattered, since
`site_admin.py`'s POST handler sets model attributes and calls `.save()` directly without ever
running form/`full_clean()` validation - added the same upper clamp to that handler's manual
`int(...)` parsing (it previously only clamped to a floor of `1`, with no ceiling at all, so the
model-level validator alone would never have been enforced through that code path). An admin can no
longer configure a value clamd can't actually stream-scan.

---

## `.btn`'s `display: inline-flex` beats `[hidden]` sitewide (found 2026-07-20)

While fixing the broken comment/Notes "Attach a Map" dialog (several elements toggled via JS
`.hidden` were staying visible - see completed.md - because their own classes set `display`
unconditionally, which beats the browser's default `[hidden] { display: none }` since author
styles always win over the UA stylesheet at equal specificity), found the same root cause one
level higher: `.btn` (`_buttons.scss`) includes the `btn-base` mixin (`_mixins.scss:180-198`),
which sets `display: inline-flex` with nothing gating it on `:not([hidden])`. Any `<button
class="btn ...">` or `<a class="btn ...">` anywhere on the site that gets `.hidden = true`'d from
JS (as opposed to removed from the DOM, or hidden via some wrapper element instead) will silently
stay visible.

Only fixed the one concrete instance this surfaced (`#comment-map-composer-save[hidden] { display:
none; }`, scoped to that button) rather than adding `&[hidden] { display: none; }` to `.btn`
itself - that mixin backs essentially every button on the site, and a global change, however
logically safe, is a broader blast radius than this dialog's fix warranted. Worth doing as its own
small, deliberate change: add the guard to `btn-base` once, then grep for other `.btn`/`.btn--*`
elements toggled via a bare `.hidden = ...` (as opposed to `hidden` attribute template conditionals,
which never render the element in the first place and aren't affected) to confirm nothing else is
silently broken the same way.

---

## `SmithsonianGateway`'s `online_media_type` filter param is unverified against the live API (found 2026-07-22)

While tightening Smithsonian's search relevance (see `services/apis/assets/smithsonian.py` -
`quote_name`/`quote_locality`/`include_address`/`search_with_country`/`reject_address_derived_names`,
same fix class as `LOCJsonGateway`), noticed `get_data()` sends `online_media_type: "Images"` as a
bare top-level GET param. Secondary research (couldn't reach `edan.si.edu/openaccess/apidocs/` - it's
a JS-rendered SPA `WebFetch` can't execute, and `si.edu/openaccess/*` pages 403 to fetchers) turned up
one indirect data point: a third-party EDAN client uses `fq=["online_media_type:\"Images\""]` (a
filter-query array with `field:"value"` syntax), not a bare top-level param - suggesting the current
param name/shape may be silently ignored by the live `/search` endpoint (unknown GET params are
typically no-ops on REST APIs, not errors, so this wouldn't be visible as a failure).

**Why not fixed**: doesn't explain the reported symptom (irrelevant *subject matter*, not irrelevant
*media type* - `parse_response()`/`_generate_media()` already drop rows with no media URL regardless
of whether server-side filtering happened), so it's out of scope for that fix. And the one candidate
correct syntax (`fq=[...]`) comes from a third-party client inferring EDAN's *internal* API, not
confirmed against the public `api.si.edu/openaccess/api/v1.0/search` endpoint specifically - risky to
guess at without a live response to check against.

**Suggested next step**: hit the live endpoint once with both the current param and a candidate
`fq=online_media_type:"Images"` (or as a list) and diff the actual JSON responses to confirm which
one server-side filters vs. is silently dropped.

---

## Internet Archive: `mediatype:texts` is excluded, but holds the best location material (found 2026-07-22)

While fixing Internet Archive's search relevance (see `services/apis/assets/internet_archive.py`),
confirmed against the live `advancedsearch.php` endpoint that the gateway's
`mediatype:(image OR movies)` filter excludes the single richest source of on-topic material in the
archive. A field-scoped query for `Eastern State Penitentiary` restricted to `image OR texts`
returned the inspectors' annual reports (State Library of Pennsylvania), a Pennsylvania legislature
committee report on the prison, `Historic landmarks of Philadelphia`, and `Pennsylvania ghost towns`
- i.e. exactly the historical/architectural record this project wants - whereas the same query
restricted to `image OR movies` returned a Ghost Adventures episode and a GeekBeat.TV review.
`archive.org/services/img/{identifier}` generates a cover thumbnail for `texts` items, so they
render as real gallery tiles rather than the empty-thumbnail fallback.

**Why not fixed**: the reported bug was irrelevant results, and the existing filter's exclusion of
books is a documented deliberate choice ("isn't useful in a photo gallery") - widening the media
types the gallery shows is a product decision, not part of a relevance fix.

**Suggested next step**: decide whether the Media gallery should carry document tiles at all (LOC
already yields them, with no thumbnail); if yes, add `texts` to `_MEDIA_TYPE_FILTER` and re-check
precision on a few pins.

---

## Internet Archive: uploader-supplied `subject` tags are a residual noise floor (found 2026-07-22)

The relevance fix matches the location name against `title` OR `subject`. `subject` is
uploader-supplied and unmoderated, so an item tagged with a landmark it isn't actually about still
passes - a live search for `Eastern State Penitentiary` kept `WWE Studio Shots 2006` on a subject
match. Precision is vastly better than before (the same pin previously returned Voice of America
radio broadcasts via full-text matching), and dropping `subject` from `_NAME_FIELDS` would lose
genuine untitled photographs, so this was accepted rather than tightened.

**Suggested next step**: if it proves noisy in practice, rank title matches above subject-only
matches rather than excluding the latter.

---

## `schedule_panel_fetch` 500s the request when the Celery broker is unreachable (found 2026-07-22)

`services/external_data.py:schedule_panel_fetch` calls `fetch_panel_source.apply_async(...)`
unguarded. When the broker/result backend is down, Celery raises
`RuntimeError: Retry limit exceeded while trying to reconnect to the Celery result store backend`
*inside the request*, so the panel endpoint returns a 500 instead of a quiet 204 or a placeholder.
Every `InfoPanelSource` panel shares this path, so a broker outage turns the whole pin detail page's
external-data column into a wall of 500s rather than degrading to "no data yet".

Surfaced while testing the new buildings-offer endpoint (`controllers/pin_buildings.py`), which
reuses the same helper - not caused by it. Pre-existing.

**Why not fixed**: the codebase already has `services.celery.safely_enqueue_task` for exactly this
(it swallows broker failures), and switching `schedule_panel_fetch` to it is a one-line change - but
it changes the failure semantics of every panel at once (a swallowed enqueue means the single-flight
cache marker is set with no task behind it, so the panel would poll to exhaustion instead of
erroring), which deserves its own change with its own test coverage rather than riding along with an
unrelated feature.

**Suggested next step**: route the dispatch through `safely_enqueue_task` and, on failure, delete
the just-added `source.flight_key(pin)` marker so the next poll retries the enqueue rather than
waiting out `FLIGHT_TTL_SECONDS` behind a task that was never queued.

---

## Pin-share notification still says "sub pin" after the Child Pins rename (found 2026-07-22)

`test_child_pins.py::PinShareBundleTests::test_notification_mentions_sub_pins` asserts the bundled
share notification contains `"2 child pins"`, but `controllers/pin_sharing.py` builds
`f" It comes with {bundled_count} sub pin{'s' ...}"` - so the assertion can never pass. Confirmed
pre-existing and unrelated to the parcel-scope work: `git log -L` shows the message string last
changed in `b9b39354` (2026-07-11) and the test's expected wording in `d5413907` (2026-07-20,
"Refactor 'Sub Pins' terminology to 'Child Pins' across the codebase") - the refactor updated the
test but not the string it tests.

**Why not fixed**: the rename is visibly half-done across user-facing copy (the pin detail page's
floating menu still reads "Sub pin details", the map dialog still says "Add Detail Pin"), so picking
a wording for this one notification in isolation is a product decision, not a bug fix - and it sits
in a feature area unrelated to the change that surfaced it.

**Suggested next step**: settle on one term and sweep the remaining user-facing strings together
(notification message, FAB toggle label, detail-pin dialog title/hints), updating the tests that
encode the old wording alongside them.

---

## `test_profile_photo_strip.py`'s `_location()` helper always collides (found 2026-07-22)

`test_own_profile_page_shows_wiki_attached_photo_not_pin_only_one` calls the module's `_location()`
helper twice, and that helper hardcodes `latitude=40.0, longitude=-74.0`. `Location` has a unique
constraint on `(latitude, longitude)`, so the second call raises
`IntegrityError: duplicate key value violates unique constraint
"dashboard_locations_latitude_longitude_fdb6594d_uniq"` - deterministically, every run.

Confirmed pre-existing and unrelated to the parcel-scope work: the file is untouched by that change,
the test dates from `590d2a54` (2026-07-20, "Add profile page photo strip with privacy-scoped
visibility"), and it fails identically with all parcel-scope changes stashed.

**Why not fixed**: it is a one-line fix (give `_location()` a per-call coordinate offset, as
`test_child_pins.py`'s own `_make_pin` already does), but it sits in an unrelated feature's test
file, and the sibling tests in that class may be relying on the shared-coordinate behaviour in ways
worth checking before changing the helper for all of them.

**Suggested next step**: make `_location()` mint unique coordinates per call (module-level counter,
mirroring `_make_pin` in `test_child_pins.py`), then re-run the whole file to confirm no sibling test
depended on the collision.

---

## UL-355: `osm.ch` in the Overpass mirror pool is a Switzerland-only extract, silently returning "no data" worldwide

Found while benchmarking the new self-hosted Overpass instance (full results and method:
`docs/overpass-mirror-test.md`, run 2026-07-22).

`https://overpass.osm.ch/api/interpreter` is listed as an equal peer in `_API_MIRRORS`
(`services/apis/locations/boundaries/overpass.py:35`), but it does **not** carry global OSM data -
it is a regional extract. Direct coverage probe:

| Probe | osm.ch | a global instance |
|---|---|---|
| Zürich HB (CH) | 1 element, `Zürich Hauptbahnhof` | 1 element, same |
| Grand Central (US) | **0 elements** | 4 elements |
| Berlin Hbf (DE) | **0 elements** | 4 elements |

Across a 10-query benchmark it returned 0 elements for 9 of 10 queries; the sole exception was a
Germany-wide bbox that clips Swiss territory (268 elements, vs 10,454 from a global mirror).

**Why this is a correctness bug, not just a slow mirror**: it answers **HTTP 200, fast, with an
empty `elements` list**. `OverpassGateway.query` only fails over on 429/502/503/504 and network
errors, and `elements_for_query` treats any successful response as authoritative. Because
`_available_endpoints()` shuffles the pool and picks a random starting endpoint, roughly **1 in 6
boundary/enrichment/parcel-building lookups outside Switzerland resolves to "no features found"** -
with no exception, no retry against another mirror, and no log line. Downstream that is
indistinguishable from OSM genuinely having no data: a pin gets no boundary, a parcel reports no
buildings, `get_typed_boundaries` returns `{building: None, property: None}`.

This also means the existing `LocationCache` may hold empty-result entries that were caused by
this mirror rather than by real absence of data.

**Repro**: 
```
POST https://overpass.osm.ch/api/interpreter
data=[out:json][timeout:60];node(around:300,40.7527,-73.9772)["railway"="station"];out tags;
-> 200 OK, {"elements": []}     # Grand Central; a global instance returns 4
```

**Fix**: remove the `osm.ch` entry from `_API_MIRRORS` (`overpass.py:35`). One line.

**Worth considering alongside it** (larger, hence not bundled): the pool has no notion of an
endpoint that lies by omission. A pool member that consistently returns 0 elements where others
return data is detectable, and `overpass.py`'s failover currently cannot express "this response
was successful but wrong". The same benchmark found `private.coffee` (20/20 failures) and
`kumi.systems` (19/20) are also effectively dead, and that the equal-peer shuffle premise in the
`_API_MIRRORS` comment ("routinely answer the same query in well under a second") no longer holds
- see the recommendations section of `docs/overpass-mirror-test.md`.

**Why not fixed here**: the scope of this session was benchmarking, and changing the production
mirror pool is a deployment-affecting decision (which endpoint becomes primary depends on whether
the self-hosted instance is considered production-ready).

---

## Deferred from this session: aliases/labels aggregation, and boundary voting (2026-07-22)

The ROADMAP's "Pin Restructure" section asks for two more things this session's follow-up work
did not attempt, deliberately:

**Aliases and labels are not yet aggregated across child pins.** The parent detail page's "show
sub pin details" toggle now aggregates map markers, the photo gallery, visit history, and (as of
this session) Notes/comments - but `pin_alias_suggestions` (`controllers/pin.py`) and the
category/tag/status membership panel (`controllers/labels.py`'s `LabelPinMembershipView` /
`label_membership_panel.html`) are both strictly per-pin, with no descendant awareness. Unlike
comments (a dedicated, pin-only view), both of these are shared generic components also used for
Wiki and Image label/alias editing - bolting hierarchy-aware aggregation onto them risks either
duplicating the template or polluting a generic component with a pin-specific concern, and
deserved its own scoped pass rather than being rushed alongside everything else in this session.

**Boundary-source voting (REData vs. Overpass, weighted by recency) was not started at all.** It
needs a new model (`BoundaryVote` or similar), a weighting/tie-breaking algorithm, a comparison
dialog with a side-by-side map, and a way to surface "cast a vote" once consensus already exists -
a materially larger, standalone feature (see ROADMAP.md's "Pin Restructure" section, last bullet)
that shouldn't be attempted as a rider on other work.

**Suggested next step**: scope each as its own task. For aliases/labels, decide whether aggregation
means read-only "also shown on sub pin X" listings (cheapest, matches what comments got) or genuine
cross-pin editing before touching the shared templates. For boundary voting, the model/algorithm
design is the right place to start - the ROADMAP entry already specifies the weighting rule in
detail.

---

## Test suite's localhost-only network guard trips on the dev docker-compose network (2026-07-22)

Found while re-verifying the dev environment (`/projects/environments/dev/s1/UrbanLens`) and
updating `CLAUDE.md`'s environment section. Running pytest inside the `app` container against the
already-running dev compose stack (`docker exec urbanlens_devs1_app /app/.venv/bin/python -m
pytest ...`) currently fails 2 tests:

- `test_profile_photo_strip.py::ProfilePageShowsPhotoStripTests::test_other_viewer_without_wiki_access_sees_no_strip_at_all`
- `test_wiki_access_boundary_mates.py::LocationVisibleToBoundaryMateTests::test_wiki_page_reachable_via_boundary_mate_pin`

Both fail with `RuntimeError: External network access is disabled during tests. Attempted to
connect to '<container-bridge-ip>'` from `core/testing_network.py`'s `LocalhostOnlyNetwork` guard,
raised from a Redis/valkey socket connect. The guard (by design, per its docstring) only allows
`localhost`/`127.0.0.1`/`::1`. But this compose stack's `UL_VALKEY_URL` (`docker-compose.yml`)
defaults to `redis://urbanlens_valkey:6379/0`, which resolves to a container bridge IP on this
network - never `localhost` - so any code path that opens its own Redis socket directly (rather
than through whatever connection path the guard already exempts) trips it, even though the
connection is legitimate same-stack traffic, not a real external call.

**Why not fixed**: this is a structural mismatch between "run tests against the already-running
dev compose stack via `docker exec`" and the test/network guard's assumption that Redis/Postgres
are reachable on localhost (true in whatever CI/test setup the guard was originally written for,
false in this multi-container topology). Fixing it means either (a) deciding tests should run in a
dedicated topology where valkey/db are localhost-reachable (e.g. `network_mode: service:app` for a
test-runner service, or exposing valkey/db ports to the host and running pytest bare - which then
hits the *other* known gap, no system GDAL on the bare host, see `CLAUDE.md`), or (b) widening the
guard to also allow the compose network's service IPs/hostnames, which risks quietly weakening a
guard that's deliberately strict. Neither is a small, obviously-correct change, so it wasn't
attempted blind.

**Suggested next step**: decide on a dedicated test-running topology (option (a) above) rather than
patching the guard - it's the only fix that doesn't touch code whose entire job is to fail loud on
anything that isn't provably local.
