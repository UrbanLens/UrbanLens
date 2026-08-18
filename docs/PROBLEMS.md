# PROBLEMS

Bugs or quirks identified during other work but out of scope to investigate/fix at the time.

> **Referencing this file from code:** name the entry, not just the file. There are 33 source
> references reading `see docs/PROBLEMS.md`, and this document is over 7,000 lines - a bare pointer
> costs the reader a full-text search and, in practice, they do not do it. Prefer
> `see "the documented docker cp resync breaks the app container" in docs/PROBLEMS.md`. Cite **every**
> relevant entry, not the nearest one - `Friendship.muted` has two (wrong shape, and never read), and
> a pointer to one implies it is the whole story where a bare pointer at least led to both. Headings
> are stable here; line numbers are not. **A date works nearly as well as a heading** - `external_api/
> serializers.py` cites "docs/PROBLEMS.md, 2026-07-28" and that alone locates the entry unambiguously,
> because entry headings carry their date. A **distinctive identifier** in the surrounding prose works
> just as well - `external_api/views.py` says only "Recorded in docs/PROBLEMS.md" but names
> `MapController.resolve_place`, which locates the entry immediately. What fails is a bare reference
> whose comment describes the problem only in general words (this file is append-only and grew by ~800 lines on 2026-08-14
> alone).

---

> Resolved entries live in [`PROBLEMS-ARCHIVE.md`](PROBLEMS-ARCHIVE.md). This file is what is
> still open, still partial, or still worth knowing before touching the area it describes.

## ⚠ Dev environment `devs1` is down - read this before restarting anything (2026-08-14)

Four entries below describe one situation. They were filed in discovery order; this is the order
they must be *acted* on, because fixing the visible problem first breaks something currently healthy.

1. **Snapshot the database.** Three of the pending migrations carry data
   (`0027_places_backfill`, `0039_encrypt_contact_and_note_fields`, `0042_label_merge_duplicates`).
2. **`manage.py migrate`** - the dev DB is **18 migrations behind** (`0026`-`0043`). See
   *"the dev database is 18 migrations behind the code"*.
3. **Then** `docker compose restart app`. Not before: Celery workers do not autoreload, so they are
   currently running old code that matches the old schema and are **healthy**. A restart makes them
   load current code against a stale database.
4. **Keep the `chown` on every `docker cp`** - see *"the documented `docker cp` resync breaks the
   app container"*. Without it the app cannot write `logs/django.log`, Django's logging config
   raises, and `runserver` dies before binding port 8000. The ownership has been repaired once
   already; the next unguarded resync undoes it.

Why this needed a summary: the underlying drift was recorded in `CLAUDE.local.md` on 2026-08-06 as a
*stale-files* problem and went unrecognised as a *database* problem for eight days. The information
was never missing - it was filed under a heading nobody would search when the site stopped serving.

---
Each entry should have enough detail (repro steps, file:line, symptoms) for a future session
to pick up without re-discovering the problem from scratch.

## OPEN 2026-08-15: frontend TypeScript audit - remaining findings

Full-tree audit of `dashboard/frontend/ts/` (every file read, eight passes). The four
security/safety items were fixed in the same pass; everything below was found but **not** fixed.
Line numbers are as of 2026-08-15 and will drift.

**Highest-value single change:** ~40 raw `fetch()` call sites bypass `shared/fetch-json.ts`
(`fetchJson`/`sendJson`), several with no `response.ok` check at all, so a non-2xx dies in a
`void`-ed promise with no toast. Six hand-rolled wrappers exist beside it: `postForm`/`getJson`
(triplicated across the three games), `postForHtml` (organize-tab-manager), `postJson`
(album-items), `savePosition` (album-map). Migrating them is mechanical, adds timeouts (several
uploads can currently hang forever), and converts the dominant silent-failure mode into the
required toast-on-error behaviour. Two deliberate exceptions to keep: `webauthn-client.ts`
(self-contained for the minimal auth layout, already ok-checked) and the two E2EE calls that need
raw `Response` semantics (201-vs-200, `redirected`).

**Correctness, user-visible:**

- `entries/map-annotations.ts:2264` - right-click-to-delete-vertex is dead code:
  `m.on("contextmenu.rcdelete" as never, ...)` is jQuery-style event namespacing that Leaflet does
  not support, so it binds a literal event name that never fires - while the toast at :2338 tells
  the user to right-click. The `as never` casts were the compiler flagging exactly this.
- `entries/map-annotations.ts:1371` - `loadDetailPins` has no ok-check and **clears the existing
  pin layer and list** on failure (console.warn only). :2558 `flushDpAutoSave` swallows validation
  errors, so autosaved edits are silently lost. :2047 `placeMediaItemAt` has no ok-check and no
  loading indicator for a server-side image materialize. :1643/:1712 bulk promote/delete
  `Promise.all` paths have no `.catch`, so one failure is an unhandled rejection with the
  selection never cleared.
- `entries/photo-location-scan.ts:207` - the `webkitdirectory` fallback path (Firefox/Safari)
  reuses an already-aborted `AbortController`, so after one Stop click **every** later scan halts
  on the first file. Also: hits accumulate across scans (re-scanning double-counts into clusters),
  and the photo uploads that run *after* the "Uploaded" toast have no progress indicator.
- `shared/map-export.ts:270` + `themes/base.html:816` - `download()` awaits tile fetches (up to 8s
  each) but no caller awaits it: no spinner, no toast, unhandled rejections, and the save flow
  closes the composer mid-export so shapes project against a map being torn down.
- `shared/markup-toolbar.ts:748` - `flushMarkupAutoSave` never checks `r.ok`, so a 400 (e.g.
  over-long label) reports success; the single pending-save slot also means editing item A then B
  inside the 500ms debounce silently discards A's changes, and nothing flushes on unload.
- `entries/organize.ts:106,311` - the Media tab is **fully dead UI**: the template renders it
  selectable with checkboxes, a filter bar and Edit buttons, but no `OrgTabManager` is built for
  it, `ORG_FILTER_NAMESPACES`/`TAB_FILTER_NS` omit it, and the consolidated dialog opener has no
  `media-label-edit-dialog-body` case, so Edit swaps a form into a dialog nothing opens.
  Separately, `_organize_label_card.html:77` references `peopleMergeSingle`, which is defined
  nowhere in the codebase.
- `shared/organize-filter-engine.ts:188` - `countVisibleCards` tests `card.style.display`, but
  tree view sets `display` on the `.tag-tree-item` *wrapper*, so cross-tab match counts and the
  "N categories also match" footer count every card as visible. It duplicates `getOrgVisibleCards`
  (:99), which gets it right.
- `shared/map-image-overlays.ts:209` - corner drag never handles `pointercancel`; an interrupted
  touch gesture leaves `map.dragging` disabled permanently.
- `entries/spotguessr.ts:1491` - `submitGuess` has no in-flight guard, so a double-click posts
  twice and double-counts the session score; :840 `reportRoundTimeout` has no error handling, so a
  failed timeout POST hangs the round forever. All three games silently null the WebSocket on
  close with no reconnect and no "connection lost" notice.
- `entries/trivia.ts:856` and `entries/consensus.ts:1051` - missing the round-id guard spotguessr
  has (`lastRevealedRoundId`), so the last player to answer double-counts HUD points.
- `shared/organize-priority.ts:69` and `shared/album-items.ts:118` - optimistic reorder with no
  rollback on failure and no save sequencing, so two rapid drags can persist the stale order while
  the DOM shows the new one. `shared/album-map.ts:113` is the model to copy - it rethrows after
  toasting so the marker snaps back.
- `shared/confirm-dialog.ts:90` - re-entrancy: opening a second dialog while one is open
  overwrites `resolveCurrent` (first promise pends forever) and `showModal()` on an open dialog
  throws into the promise executor.
- `shared/scroll-to-hash.ts:50` - re-scrolls on *every* `htmx:afterSettle` for the page's life, so
  any later swap yanks the reader back to the original anchor.
- `shared/onboarding-tour.ts:87` - auto-dismiss hooks bind only to elements present at init; HTMX
  swaps orphan them, so dismissed cards reappear.
- `shared/organize-header.ts:113` - a transient window resize below 768px *permanently* overwrites
  the stored gallery view preference.
- `entries/article-wysiwyg.ts:532` - the first WYSIWYG keystroke re-serializes the whole article
  through a lossy `tiptap-markdown` parse (`html: false`), rewriting content document-wide, not
  just at the edit point. Needs round-trip tests over real saved articles before it is trusted.
- `shared/e2ee-client.ts:238` - the `e2ee-busy` class it sets during login has **no CSS rule
  anywhere**, so the ~1s synchronous Argon2id derivation shows no indicator at all; the unlock
  dialog (:682) has no busy state either, while the reset dialog next to it does it correctly.
- `shared/e2ee-client.ts:1326` - retry storm: a thread with an unreadable key re-fetches the same
  conversation/group key once per message (50 sequential identical failing requests on a
  50-message thread). :1459 `decryptDom` also strips `data-e2ee-*` *before* attempting decryption,
  so a transient failure is permanently unrecoverable on WS-appended messages.
- E2EE keys persist in IndexedDB across logout - `clearProfileKeys` is called only from
  `resetKeys`. Possibly intended (documented same-origin trust boundary), but the logout gap looks
  unconsidered rather than chosen; decide it explicitly and add a "forget this device" action.

**Operational:**

- `shared/location-search-engine.ts:140,197,916` - three direct browser-to-Nominatim calls bypass
  the server-side rate limiter and cost tracking and violate Nominatim's usage policy. The file's
  own comment already flags this as a KNOWN GAP. Needs the server-side geocode proxy (mirroring
  the Google Places one), which would also enable one aggregated suggestion endpoint.

**Structural (no user-visible symptom):**

- The three games triplicate ~1,500 lines of session/lobby/chat/invite/fetch plumbing (19 blocks
  differing only by an `sg-`/`cs-`/`trivia-` prefix). Extracting `game-net` / `game-session` /
  `game-friends` / `score-rows` removes ~1,000 net lines and makes the next game cost ~500 lines
  instead of ~1,300.
- `entries/map-annotations.ts` is eight features in one 2,645-line `init()` closure. Three
  extractions are nearly free today: rectangle-select (already generic, zero closure deps), the
  satellite/street-view carousel twins (95% identical), and the building-import dialog.
- `shared/e2ee-client.ts` (1,537 lines) mixes key-lifecycle service code with three hand-built
  `innerHTML` dialogs; the crypto/store layering beneath it is clean and should not be disturbed.
- Five picker widgets reimplement the same dropdown mechanics (four different blur-close timeouts,
  three chip implementations, five `escapeHtml` variants, keyboard nav missing entirely from
  `createChipPicker` and `label-rel-picker`). Note these files are otherwise **correctly**
  HTMX-shaped - the server renders their option lists and TS only filters - so do not "fix" them
  by adding round trips.
- Organize's five modules communicate over four channels at once (imports, 13 window globals with
  last-writer-wins handler slots, CustomEvents, an htmx response header), and the kind/ns/tab
  vocabulary is encoded in five separate places. It also runs a private copy of the shared
  `window.ulBulkToolbar` that `static/js/bulk-toolbar.js` says it mirrors.
- `shared/map-layers.ts:198` has no `destroy()`, so document/matchMedia/map listeners accumulate
  on per-dialog maps (the comment-map composer). `shared/photo-map.ts:204` is the model.
- Test coverage is inverted: all test files cover the small shared modules; the four largest files
  (`map-annotations`, `spotguessr`, `e2ee-client`, `consensus`) had zero until this pass added
  `e2ee-client.test.ts`/`e2ee-store.test.ts` (see `ts/testing/fake-indexeddb.ts` - happy-dom has
  no IndexedDB, which is why the store was untested).

**HTMX opportunities** (per the HTMX-first rule) - roughly 1,500-2,000 lines of DOM templating
that the server could render: game lobby lists/summaries/friend pickers, map-annotations' detail
sidebar + photo panel + bulk-edit dialog + `doSendSelectedDpToWiki` (which hand-parses
`HX-Trigger` over raw fetch - it is hand-rolled htmx already), organize's merge dialog (a third
copy of card rendering the server owns) and `postForHtml`/`replaceRows` (a hand-rolled
`hx-post`+`hx-swap`), album add/remove (two round trips where sibling flows do one `hx-post`), the
article live preview (already POSTs to a server render endpoint), and the three E2EE dialog shells.

**Out of scope but larger than all of the above:** 21,378 lines of untyped, untested, unlinted
inline JavaScript across 131 template `<script>` blocks - `map/index.html` alone is 5,152 lines
(and holds the pin-cache *writer*), `messages/index.html` 1,771, `trips/detail.html` 1,375,
`location/index.html` 1,284, `base.html` 1,116. Several audited bugs sit on the inline side of a
TS/template seam; that is where bugs collect, because the types stop there.

## NOTE 2026-08-11: do not naively wrap `PinShareCreateView.post` in `transaction.atomic`

`ATOMIC_REQUESTS` is unset, so views run in autocommit. `controllers/pin_sharing.py:137` performs
a related sequence - stamp the pin's origin share, create the `PinShare`, `share.images.set(...)`,
`record_share_exposure(share)`, optionally share the attached markup map, then create child-pin
shares - with no transaction. A partial failure can leave a `PinShare` with no `LocationExposure`,
which is the provenance invariant `CLAUDE.md` calls out.

**Wrapping the view in `atomic()` would make this worse, not better.**
`share_provenance.record_share_exposure` deliberately catches `DatabaseError`, logs, and returns
None (`share_provenance.py:119`) so a bookkeeping failure doesn't fail the user's share. Inside an
`atomic()` block Django marks the transaction broken as soon as a `DatabaseError` occurs, and every
subsequent query raises `TransactionManagementError` - so the naive fix converts a tolerated,
logged degradation into a hard 500 on the share itself, and takes the markup-map and child-pin
shares down with it.

If atomicity is wanted here, the swallow has to move inside its own nested `atomic()` first, so
the savepoint absorbs the error and the outer transaction survives. Recording this because
"multi-write view with no transaction" looks like an obvious omission and is not.

The same audit over all of `controllers/` flagged 18 methods with 3+ direct writes and no
transaction, but the count is inflated by dispatchers: the top hit
(`controllers/settings.py:159`, "18 writes") is a 15-branch `if/elif` on `section` where exactly
one branch runs. Only paths whose writes share an invariant are worth looking at.

## Coverage note (not a defect): 20 of 32 notification types have no per-type delivery control

Measured 2026-08-11: 32 `NotificationType` values, 13 preference stems, 12 of which match a type.
The uncovered 20 include `safety_ci_due`, `safety_ci_overdue`, `pin_import_complete`,
`friend_suggestion`, `spotguessr_invite`, `trivia_invite`, `consensus_invite`, `map_shared`,
`ai_extraction` and the generic `error`/`warning`/`info`.

This is deliberate and documented in `preference_field_names()`'s docstring ("Callers must expose
exactly these and must not invent defaults for the types that are missing"), and some of them -
the safety escalation chain in particular - are arguably *right* to be non-silenceable. Recorded
only so the gap is visible when someone asks why a given notification has no setting.

## 2026-07-28: `Friendship.muted` is shared by both profiles, not per-viewer

There is exactly one `Friendship` row per pair - `QuerySet.between()` matches either direction
and `Friendship.request()` reuses whatever row already exists - so the `muted` boolean added in
migration `0020_friendship_muted_flag` is a property of the *relationship*, not of one side of
it. If A mutes B, B's own view of that relationship also reads `muted=True`.

This is inherited unchanged from the `status='Muted'` encoding it replaces (a status column is
just as shared), so nothing regressed - but the new flag makes it much easier to surface the
value in a UI or API as "people I have muted", which would be wrong. The correctly shaped
precedent is `DirectMessageMute`, keyed on `(viewer, sender)`.

Fix is either two columns (`from_profile_muted` / `to_profile_muted`, set according to which
side of the row the actor is on) or a small `FriendshipMute(viewer, target)` model alongside
`DirectMessageMute`. Two columns is the cheaper change and keeps the single-row invariant that
`between()`/`request()`/`unique_together` all depend on. Deliberately not done in the schema
pass that introduced the flag: the brief was one boolean plus the data repair, and widening it
to a directional pair would have changed the shape the API batch was told to expect.

## 2026-07-28: `Friendship.muted` is stored but nothing reads it - muting a friend silences nothing

Noted while splitting mute off `Friendship.status` (migration
`0020_friendship_muted_flag`). The bug that split fixed was that muting **un-friended** people;
what it did *not* fix is that friendship-mute has never actually suppressed anything. Grep for
`muted` across `services/notifications/notifications.py`, `services/notifications/notification_delivery.py`,
`services/notifications/notification_text_alerts.py` and `services/notifications/notification_center.py`: no hit. The two
mute features that do work are unrelated models - `DirectMessageMute` (per-sender DM mute) and
`GroupChatMembership.muted` (per-group mute) - and neither consults `Friendship`.

So the profile page's Mute button, and the external API's `POST /friends/{uuid}/mute/`, both
record a preference that no delivery path honours: the muter still receives friend-request,
friend-accepted, pin-share, trip-invite and safety notifications from that profile. Repro: mute
an accepted friend from their profile page, have them share a pin with you -> the notification
still arrives, and `NotificationLog` still has an unread row.

Fix is to make `Friendship.muted` an input to notification delivery in the same place
`NotificationLog` rows are created from a `source_profile` - most cleanly a single
`is_muted_by(recipient, source)` helper in `services/social/friendship.py` that
`services/notifications/notifications.py` consults, so the check cannot be forgotten per notification type.
Deliberately left out of the schema change: wiring a new suppression rule into every
notification producer is a behaviour change of its own size, and the external API's `is_muted`
surface (Batch S) needs the flag to exist first.

## OPEN 2026-07-28: 16 pre-existing failures outside every prior sweep's `-k` filter

Every sweep so far this session used a `-k` keyword filter scoped to pin/wiki/location/friend/
external-API territory. A full unfiltered run (`pytest src/urbanlens/dashboard/tests`, no `-k`,
~70 minutes) surfaces a different, previously-unswept **16 failed, 7990 passed, 2 xfailed**. None
of the 16 files were touched by anything in this session (last-commit dates 2026-07-22 through
2026-07-25, `git log -1` per file) or relate to pins/wikis/locations/friends/the external API -
this is a fresh, disjoint backlog, not a regression from today's work. **Only triaged, not fixed.**

Two entries from the raw 18-failure sweep output are not real and should be discarded outright if
re-seen: `test_zzz_client_probe_tmp.py` was a throwaway file created and deleted mid-session for an
unrelated probe (see the `@given`+`self.client` CLAUDE.md entry) - the long-running sweep's
collection phase had already imported it before the delete, so it ran from memory once, near the
end of the ~70-minute run, off a module that no longer exists on disk. And
`test_spotguessr_geo_bonus.py::BonusPointsForGuessTests::test_geocode_failure_earns_nothing_without_raising`
is order-dependent pollution from the full run - it **passes standalone**, unlike the 16 below,
which were re-verified to fail in isolation too (`16 failed, 1 passed` re-running just this set).

Root-caused from a `--tb=short` capture, not yet fixed:

1. **Not a bug - a run that crossed midnight.** `test_global_search_parser.py::
   ParseQueryStructureTests::test_this_year_ends_today` failed with
   `datetime.date(2026, 7, 28) != datetime.date(2026, 7, 27)` - the assertion computed "today" at
   two different points ~70 minutes apart in a suite run that happened to straddle midnight. Not
   reproducible on demand; would pass on any run that doesn't cross a day boundary mid-execution.
2. **Unmocked live network calls** (the same class of bug fixed repeatedly earlier in this file):
   `test_loopnet.py::FetchTests::test_unconfigured_gateway_gracefully_persists_empty` and
   `test_trip_ai_suggestions.py::ApplySuggestedOrderViewTests::test_valid_permutation_applies` both
   raised `RuntimeError: External network access is disabled during tests` against real IPs
   (`10.2.0.214`, `5.148.170.168`). Check whether each is "test forgot to patch, sibling tests
   show the pattern" (most likely, per every prior instance of this bug this session) or a
   genuine missing gate in the view/service.
3. **Test-only bug.** `test_redata_cid_gateway.py::RedataCidGatewayResolveCidsTests::
   test_non_200_raises_gateway_request_error` - `TypeError: 'Mock' object is not subscriptable`.
   The mock response object isn't shaped to support whatever subscript the code under test uses;
   compare against a sibling test in the same file that mocks correctly.
4. **Possibly Windows-specific.** `test_backup_services.py::BackupFilesTests::
   test_returns_only_files_sorted_by_mtime_descending` (`[] != [WindowsPath(...new.sql),
   WindowsPath(...old.sql)]`) and `CollectBackupStatsTests::test_collects_count_latest_size_and_settings`
   (`0 != 2`) both show the service finding zero files where the test created two - the service's
   discovery glob/pattern likely doesn't match on Windows path separators, or looks in a path this
   test's temp dir isn't under. Worth checking whether this fails in Docker/CI too before assuming
   Windows-only.
5. **Possibly a real permission bug, worth prioritizing.** `test_trip_ai_suggestions.py::
   TripAiSuggestionsViewTests::test_non_member_is_rejected` got `404` where it expected `403` - on
   its face this reads like the uniform-404 privacy pattern used elsewhere in this codebase
   (wiki discovery, trip detail - see the resolved trip-controller entry above), in which case the
   test is stale and the code is behaving correctly. But it could also be a plain "wrong pin/trip
   ownership lookup" bug that happens to 404 for the wrong reason. Read `TripAiSuggestionsView`
   before assuming either way.
6. **Hypothesis-found edge case, may be a real off-by-one.**
   `test_searxng_image_query.py::AssembleImageQueryTests::test_group_count_matches_present_components`
   failed on `aliases=['0'], area=['(']` with `4 != 3` - a generated alias/area combination made
   the query-group count disagree with the number of query components actually present. Worth a
   look at `assemble_image_query`'s group-counting logic directly rather than reverse-engineering
   it from the failing example.
7. **Assertion mismatches not yet read in detail** - each needs its own traceback before triage:
   `test_avatar_colors.py::GroupMemberSearchAvatarColorTests::test_results_get_distinct_colors`
   (`0 != 4`), ~~`test_dm_search.py::SearchDirectMessagesTests::
   test_date_range_phrase_filters_by_created` (`[] != [1]`)~~ **FIXED 2026-08-05, see below**,
   ~~`test_global_search_engine.py::PhotoSearchTests::test_finds_photo_by_generated_keyword`
   (expected string not found in an empty result list)~~ **FIXED 2026-08-05, see below**,
   `test_media_own_photos_preview.py::PhotosMediaPreviewTests::` both
   `test_own_photo_tile_carries_image_id_and_coordinates` and
   `test_own_photo_tile_without_coordinates_renders_empty_lat_lng` (`204 != 200` - both in the
   same class, worth checking whether one shared fixture broke both),
   `test_settings_tos_accepted_display.py::SettingsTosAcceptedDisplayTests::
   test_shows_the_acceptance_date_when_recorded` ("Mar 4, 2025" not found in the rendered page -
   possibly the same CSRF-token/date-formatting class of stale-assertion bug fixed elsewhere in
   this file), and `test_trivia_stall.py::ForceRevealRoundTests::` both
   `test_a_partial_answer_is_revealed_and_only_the_answerer_is_rated` (`'active' !=
   TriviaSessionStatus.COMPLETED`) and `test_advances_to_the_next_round_when_more_remain`
   (`1 != 2`) - both in the same class, likely one shared root cause in the force-reveal flow.

Reproduce with (no `-k` filter, so the full ~70-minute runtime applies):
```
pytest src/urbanlens/dashboard/tests -q --reuse-db --tb=short
```
Re-running just the 16 test IDs above in one invocation reproduces all of them in ~5 minutes.

## OPEN 2026-07-27: ~46 pre-existing test failures on `feature/external-api-mobile-v2` (baseline-verified)

A broad sweep (`-k "pin or wiki or location or boundary or import or share or detail or merge or
restructure or undo or map"`) over `src/urbanlens/dashboard/tests` gives **46 failed, 3484 passed**.
None are regressions from the Place-consolidation phase-0 work: the four files whose failures
could plausibly have been caused by it were run with that change set `git stash`ed and again with
it applied, giving **byte-identical results both ways (12 failed, 117 passed, same test IDs)**.

**33 of the 46 are now FIXED** (2026-07-27, same session). Two patterns dominate:
*tests that depend on ambient machine state or on an implementation shape that has since changed*
(1-6, 11-14 below - they pass on a bare CI box with no credentials and fail on a dev box that has
them, or vice versa), and *test-harness behavior leaking into the thing under test* (7-8). Only two
of the 33 turned out to be product bugs (9-10).

Fixed:

1. `test_legacy_cid_coordinate_fix.py::RepairLegacyPinCoordinatesTests` (7) - the helper's
   `location.cid = ...` goes through `Location.cid`'s setter, which calls `GooglePlaceService`
   with `fetch_if_missing=True` and hits REData's nearby-places search live. Now calls
   `set_cid_for_entity(..., fetch_if_missing=False)`, the service's own bulk-path flag. **Note the
   sharp edge that caused this: assigning a plain model attribute performs synchronous network
   I/O.** Worth revisiting on its own merits.
2. `test_websocket_auth.py` (1) - not a consumer bug at all. Tests ran Django's default PBKDF2
   (~1.2M iterations) because nothing overrode `PASSWORD_HASHERS`; hashing inside the connection
   handshake exceeded `WebsocketCommunicator.connect()`'s 1-second default. `settings/test.py` now
   sets MD5, which also speeds up every test that bakes a User.
3. `test_pin_redata_media_proxy.py` (2) - asserted "unconfigured gateway returns 404 not 500" while
   *assuming* the machine had no REData credentials. With credentials present the gateway built
   fine, made a real call, and died on a DB write from a `SimpleTestCase`. Now forces the
   unconfigured state by patching `__post_init__`.
4. `test_flickr_album_import.py` (1) - same shape: every sibling patches `flickr_is_configured`,
   this one didn't, so it saw "not configured" and never reached the blank-URL branch.
5. `test_property_records_plugin.py` (1) - assigned to `Location.address`, which is a read-only
   composed property; now sets the component fields.
6. `test_pin_model_extra.py::PinEffectiveColorTests` (2) - test/implementation drift.
   `icon_source_label()` sorts in Python via `sorted(self.labels.exclude(kind="user"))` and no
   longer calls `.order_by()`, but the mock still stubbed `exclude().order_by()`. The real call
   iterated a bare MagicMock and got nothing, so the expects-a-colour cases failed and the
   expects-None cases passed **without exercising anything**.

**A further 19 across ten files are now also FIXED** (2026-07-27, same session). That set was
previously listed here as "genuine failures reproducible in isolation" - **that label was wrong for
more than half of them**, and the correction is the useful part. Those ten files now give
**344 passed, 0 failed**. Two systemic causes accounted for twelve:

7. **`@given` + a row-writing `setUp` leaked rows across an entire test class** (10 failures:
   `RoundTripCommentsTests` 8, `ArbitraryChainDepthPropertyTests` 2). `hypothesis.extra.django`'s
   mixin routes `@given` tests through `unittest.TestCase.__call__`, bypassing Django's
   `_pre_setup`/`_post_teardown` wrapper; hypothesis instead calls those per *example*. `setUp` is
   still called once by `unittest`'s `run()` - before the first example, so **outside every
   per-example transaction**. Its rows landed in the class-level atomic and survived to
   `tearDownClass`, so the *next* test in the class died in its own `setUp` on
   `dashboard_locations_latitude_longitude_uniq`. Fixed once for the whole repo in
   `core/tests/testcase.py`: `TestCase` now defers `setUp`/`tearDown` (and drains cleanups) into
   `setup_example`/`teardown_example`. **Any class mixing a `@given` test with a row-writing
   `setUp` was affected**, which is most of `tests/hypothesis/`.
8. **`UL_CELERY_TASK_ALWAYS_EAGER=True` turns "dispatched to a worker" into "ran inline"** (2
   failures). Needed for local non-Docker pytest, but it silently invalidates any test asserting
   that a request *didn't* do background work.
   - `test_direct_messages.py::...::test_second_message_in_same_streak_is_debounced` -
     `create_direct_message` schedules the alert task, which ran eagerly *outside* the test's patch
     and claimed the debounce marker, so both explicit calls were no-ops.
   - `test_location_place_name_lazy.py::...::test_page_render_never_calls_the_live_resolver` - the
     view correctly dispatches `resolve_location_place_name`; eager mode then resolved *during the
     request*, which is exactly what the test forbids. It was **order-dependent, not
     isolation-clean**: it passed when the whole file ran (the preceding class masked it) and
     failed when run alone.
   Both now stub `safely_enqueue_task` so they measure the request path, which is the actual claim.

Two were **real product bugs**, both fixed:

9. `services/map_pin_share_detection.arrow_points_toward` returned a garbage answer when a pin sat
   on an arrow's tail. The boundary centroid lands ~1e-14 degrees off the tail through ordinary
   float error, and `bearing_degrees` turns that ~1-nanometre displacement into a confident angle -
   measured at `106.29` vs the arrow's own `89.36`, inside the 35-degree tolerance. An arrow drawn
   *from* a pin and pointing away therefore recorded a **DETECTED `PinShare` the sender never
   intended**. Now guarded by `_DEGENERATE_TAIL_SEPARATION_DEGREES` (1e-7, far below the 1e-6
   coordinate storage precision), with a property test over arrow headings.
10. Creating a pin through the map's add-pin dialog never set `name_is_user_provided`, so a
    hand-typed name was eligible for the `tasks.upgrade_placeholder_pin_names` sweep that clears
    non-user-provided names - despite that task's own docstring defining the flag as "a user
    actually typed something". `create_pin_for_profile` now takes an explicit
    `name_is_user_provided` (default False, preserving importer/offline-sync semantics) and
    `maps.post_add_pin` passes it. **Left deliberately unchanged:** the external API's pin-create
    still defaults to False, so a name typed in the mobile app is not protected until edited -
    inconsistent with its own PATCH path (`external_api/views.py:746`), and worth a decision.

The remaining five were test bugs of one shape: **substring assertions against a whole page, where
the string also appears inside the page's own inline `<script>`**.

11. `test_pin_edit_controller.py::PinDescriptionEditableTests` (2) - asserted description *markup*
    against the full page, but `#pin-overview` is `hx-get`-loaded, so the markup is only in the
    partial. Both classes' docstrings already documented this split. The assertions moved to
    `PinOverviewEditableDescriptionTests`, which renders the partial. Note
    `assertNotIn("pin-description--empty", full_page)` could **never** pass - the click-to-edit
    script toggles that class by name.
12. `test_profile_hero_meta_editable.py` (2) - same thing; the wiring script builds
    `>Add when you started exploring...</span>` as a string literal, so even the `>...<` idiom the
    file already used elsewhere was insufficient. Now strips `<script>` blocks and asserts against
    the markup.
13. `test_trip_controller.py::...::test_outsider_gets_404_indistinguishable_from_a_missing_trip` -
    compared two responses byte-for-byte including the CSRF token. Django re-masks the token per
    call, so a page holds several *different* strings for one secret and no two renders ever match.
    Now normalizes token-shaped runs before comparing.
14. `test_pin_media_endpoints.py` (1) - a bare `mock.Mock()` has a truthy `is_redirect`, sending
    `fetch_with_revalidated_redirects` down its redirect branch and handing `urljoin` a Mock
    (`TypeError` -> 500). The sibling `test_media_materialize._ok_response` sets it correctly. Also
    removed the test's real-DNS dependency.

15. **`test_external_api_wiki_oracle.py::WikiDiscoveryOracleTests` was 429ing, not 404ing** (12
    SUBFAILEDs). An earlier revision of this file claimed it "passes cleanly in isolation" -
    **that was wrong**; it fails alone too. The class walks all 24 `WIKI_ROUTES` once per
    invisibility case over a single credential (72 requests), which blows past
    `ExternalApiBurstThrottle`, so the tail of the list came back **429 instead of 404**. The four
    routes at the end of `WIKI_ROUTES` are the comment writes, which is why the failures looked
    suspiciously like a comments-specific security hole. It was not one: all three cases returned
    429 *identically*, so the anti-enumeration property held throughout. `setUp` now calls
    `disable_throttling(self)`.

    **The part worth keeping:** because those four routes never got past the throttle, the oracle
    guarantee for `POST comments/`, `DELETE comments/1/` and both reaction routes was **silently
    unverified** - the test looked like it covered them and did not. It now genuinely does, and
    they pass. When a sub-resource is appended to `WIKI_ROUTES`, check it is actually reached.

Measured on `-k "external_api or api_key"` (2026-07-27): **59 failed / 668 passed** before this
session's fixes, **12 failed / 715 passed** once causes 7-8 landed, and **715 passed / 435 subtests
passed / 0 failed** once 15 did. Note the original 59-failure figure was only partially itemized at
the time: the capture had been truncated by a `Select-Object` filter, so ~45 of those were never
inspected individually; they stopped failing across causes 7-8.

Still open:

- The friend-invite privacy cluster documented below (7).

Reproduce a baseline cheaply with `pytest <files> -q --reuse-db` after `git stash`, rather than
re-running the whole 41-minute sweep. Set `UL_TEST_DB_NAME` to something unique per agent and
`UL_CELERY_TASK_ALWAYS_EAGER=True` (see cause 8 for what that costs you).

## Historical detail from the original 2026-07-26 report

Found while building the external-API social domain. **Not caused by that work** - verified by
reverting `controllers/friendship.py` and `controllers/notifications.py` to `f529b0f4` and
re-running the identical selection: 9 failures before the change, and the same 9 after.

```
test_friend_invite_privacy.py::InviteByEmailPrivacyTests::test_existing_user_actually_receives_friend_request
test_friend_invite_privacy.py::InviteByEmailPrivacyTests::test_gmail_variant_of_existing_email_is_matched
test_friend_invite_privacy.py::InviteByEmailPrivacyTests::test_response_identical_regardless_of_target_friend_request_visibility
test_friend_invite_privacy.py::OutgoingRequestWidgetPrivacyTests::test_pending_cards_are_structurally_identical_across_kinds
test_friend_invite_privacy.py::OutgoingRequestWidgetPrivacyTests::test_pending_cards_carry_no_type_revealing_urls_or_ids
test_friend_invite_privacy.py::OutgoingRequestWidgetPrivacyTests::test_registered_and_unregistered_pending_entries_render_identically
test_friend_invite_privacy.py::OutgoingRequestWidgetPrivacyTests::test_registered_target_identity_is_hidden_in_the_pending_widget
test_friend_request_message.py::EmailInviteMessageTests::test_message_is_stored_on_the_friendship_for_an_existing_user
test_external_api.py::PinSyncViewTests::test_child_pins_are_served_with_their_parent_uuid
```

The eight friend-invite ones share a likely root cause: the invite path now gates the
registered-account branch on `Profile.visibility_permits(to_profile.friend_request_visibility,
to_profile, inviter)` (a deliberate security fix - a bare `!= NO_ONE` check previously let
anyone who knew an address bypass a restricted visibility setting). `friend_request_visibility`
defaults to `ANYTHING_IN_COMMON`, and a freshly-baked target profile has no pin/friend/trip in
common with the inviter, so the gate now correctly refuses - but these tests still assert that a
`Friendship` row *is* created. The tests appear to predate the gate and were never updated;
they need to set the target's `friend_request_visibility` to `ANYONE` (or establish something in
common) in setUp.

`PinSyncViewTests::test_child_pins_are_served_with_their_parent_uuid` is unrelated and fails
with `PinCreationError: You already have a pin at this location.` - it looks like the test
creates two pins at coordinates that resolve to the same `Location`.

Whoever picks this up should confirm the intended behaviour before editing the assertions:
if the gate is right, the tests are stale; if the tests encode a real product requirement
("an emailed invite should reach anyone regardless of their visibility setting"), then the
gate needs a documented exception instead.

## Full-codebase audit (2026-07-25): curated high-severity findings

A systematic full-codebase audit (every model/controller/service/template/TS/SCSS file, all
migrations, the full test suite) ran 2026-07-25, tracked in `docs/notes/ai/codebase-audit.md` (35
units, full findings with file:line references for every bug/inefficiency/improvement found — see
that doc for anything not listed here, including all "improvement"-grade and maintainability
findings). This section curates only the highest-severity/highest-impact items into this file's
convention; the full ranked list per feature area is more complete.

**SSRF: Immich server URL is user-controlled with no private-IP/scheme guard.**
`models/immich/model.py:83` (`ImmichAccount.server_url`, via `forms/immich_form.py`) accepts any
well-formed URL with no loopback/private/link-local restriction. `controllers/immich.py:108-110`
(`ImmichSettingsView.post`) pings it server-side before saving, and later flows
(`get_map_markers`/`get_asset_thumbnail`/`get_asset_original`) proxy the response back to the
browser. Any authenticated user can point `server_url` at internal infrastructure
(`http://169.254.169.254/`, `http://localhost:6379/`, an internal admin panel) and use the
ping/thumbnail endpoints as an authenticated blind-SSRF oracle. Self-hosted-by-design feature, so
risk is single-tenant, but worth a scheme/private-IP guard in `clean_server_url`.

**SSRF: `services/security/url_safety.py`'s IP blocklist misses RFC 6598 CGNAT space.**
`is_blocked_address` (`url_safety.py:28-22`) checks `is_private`/`is_loopback`/`is_link_local`/
`is_reserved`/`is_multicast` but not the `100.64.0.0/10` Carrier-Grade-NAT range — verified
`ipaddress.ip_address("100.64.0.5")` returns `False` for every one of those checks. Many cloud
providers route internal-only infra (AWS NAT gateways, GCP internal LBs) through this range, so a
user-supplied URL resolving there sails through `ensure_public_http_url` unblocked. This is the
*only* IP-range guard for AI link extraction (`services/ai/link_extraction.py`), pin-suggestion
photo download (`services/pins/pin_suggestions.py`), and media materialization
(`services/media/media_materialize.py`) — one missed CIDR range is a gap in three subsystems at once.

**Decompression-bomb protection in the full-archive importer only checks forgeable declared
sizes.** `services/import_export/import_data.py:290-296` sums each ZIP member's *declared* `file_size` against a
ceiling, then calls `zf.extractall()` unbounded. `file_size` in ZIP headers is attacker-controlled;
Python's `zipfile` only detects a declared-vs-actual mismatch via CRC32 *after* a member is fully
decompressed and written to disk. A crafted archive well within the 500MB upload cap
(`controllers/tools.py:330`) with forged small `file_size` fields but highly compressible payloads
can decompress to hundreds of GB before the mismatch is caught. The project's own
`services/import_export/archive_extractor.py:_extract_zip` (lines 299-311) already guards this correctly (caps
each member read, rejects on overrun) — `import_data.py` just doesn't reuse it. A companion gap:
`services/import_formats/gpx.py:41` / `gpx_tracks.py:140` parse GPX XML via `gpxpy` with no
`defusedxml` wrapper (unlike `osm_xml.py`, which correctly uses one) — a latent XXE surface since
`lxml` is installed and `gpxpy` prefers it.

**WebSocket consumers crash on any binary frame, leaking channel-layer group membership.**
Every consumer in `dashboard/consumers.py` (lines 54, 133, 409, 599, 733) declares
`async def receive(self, text_data):` with no `bytes_data` parameter. Channels' base
`AsyncWebsocketConsumer` calls `receive(bytes_data=...)` for any binary WS frame, raising an
uncaught `TypeError` that propagates out of the ASGI coroutine — `disconnect()`/`group_discard()`
never runs, so the dead `channel_name` stays registered in whatever group(s) it had joined. Any
client (buggy library, proxy, or deliberate probe) sending one binary frame on `ws/notifications/`,
`ws/messages/`, or either safety-checkin socket kills the connection ungracefully. Affects all five
consumer classes identically — fix once at the base-class level.

**Two rate-limit/quota checks with the same non-atomic check-then-act race**, each independently
discovered:
- `services/core/rate_limiter.py:341-491` — `check_rate_limit` (COUNT) and `log_api_call` (INSERT) are
  separate operations with no locking; concurrent requests can all pass the check before any log,
  breaching even Nominatim's hard 1 req/sec ToS limit under a handful of simultaneous pin-detail loads.
- `services/security/email_safety.py:106-111` (`email_rate_limit_error`) — same shape for outbound
  friend-invite/visit-invite emails (`controllers/friendship.py:533-570`,
  `services/visits/visit_invites.py:82-109`); concurrent requests can exceed the configured hourly/daily/
  monthly cap arbitrarily.
Both need either `select_for_update()` around the count+log pair or an atomic increment
(`cache.add()`-style) rather than read-then-write.

**AI provider token/cost accounting is silently doubled for 2 of 3 wired providers.**
`services/ai/gateway.py:385-391` calls `self.receive_tokens(message)` in the shared
`send_prompt`/`send_prompt_list`, but `services/ai/anthropic.py:117` and `services/ai/openai.py:121`
*also* call `self.receive_tokens(body)` inside their own `_parse_response()` — double-counting the
same response. `services/ai/cloudflare.py`'s parser correctly does *not* self-call, proving the
other two are a regression rather than by design. Every assistant-chat, document-import, vision-keyword,
and auto-tag cost/token estimate for Anthropic and OpenAI is currently ~2x actual.

**~~Friend-request-visibility bypass via the email-invite path.~~ FIXED - this entry was stale
(verified 2026-07-27).** It described `invite_by_email` checking only
`to_profile.friend_request_visibility != VisibilityChoice.NO_ONE`. That logic has since moved out
of the controller into `services/social/friendship.py:invite_by_email`, which runs the full
`Profile.visibility_permits` evaluator - the same one `request_friend` uses. The non-`NO_ONE`
cases this entry correctly flagged as untested are now covered; see the resolved friend-invite
entry above, including the UX consequence the fix carries.

**`common_pin_count` is shown regardless of its own visibility gate.**
`controllers/userprofile.py:157-158` + `templates/pages/profile/index.html:66-77` — the *count* of
shared pins between two profiles is computed and rendered unconditionally; only the link to the
detail page is gated by `can_view_common_pins_with` (default `FRIENDS`). Any two logged-in
strangers see e.g. "3 Places in Common" even when the setting says only friends should know the
overlap exists at all.

**Login-lockout is keyed on the raw submitted string, not the resolved account — brute-force is
bypassable.** `controllers/account.py:51-58,623-634,657-692` keys failed-attempt/lockout counters
by the literal POSTed "username" value, but `EmailOrUsernameModelBackend`
(`services/auth/auth_backend.py:14-36`) resolves that same field against primary email, verified
secondary email, and Gmail dot/plus-normalized variants before authenticating. An attacker can
brute-force one account indefinitely by rotating through equivalent-but-textually-distinct login
strings (`victim@gmail.com`, `vic.tim@gmail.com`, `v.ictim+x@gmail.com`, a secondary email — each
gets its own untripped counter). `test_email_login.py` proves the login path treats these as
identical; no equivalent test exists for the lockout path.

**`Label` kind-conversion has no branch for converting to Category — silently orphans the row.**
`controllers/labels.py:547-478` (`_apply_kind_conversion`) handles converting to Status/Tag but has
no branch for `new_kind == KIND_CATEGORY`. Converting a global Tag to a Category via the standard
edit form leaves `label.profile=None`, but Category lookups use exact-match `.for_profile()` with
no global fallback — the label vanishes from every Organize > Categories listing, and
`_can_modify_label` returns `False` for any non-tag label with `profile=None`, making the row
**permanently un-editable and un-deletable through the UI** (recoverable only via direct DB access).

**Safety check-in escalation can re-email every emergency contact on any partial failure.**
`services/visits/safety.py:1920-981` (`escalate_checkin`) loops all contacts unconditionally (no
`notified_at__isnull=True` filter) and only saves `status`/`escalated_at` *after* the whole loop
completes. If anything raises mid-loop (bad email address, a non-SMTP/OSError mail-backend
exception), the checkin never flips to `OVERDUE`, so the next 5-minute beat tick re-matches it and
re-emails every contact already notified — including real emergency contacts during an actual
safety incident. Compare `_resolve_as_found_safe` (line 1038-1044), which correctly flips status
*before* its own contact loop for exactly this reason. Compounding gap: the three checkin beat
tasks (`tasks.py:1629-1671`) run every 5 minutes with no locking, unlike the `RUN_LOCK_CACHE_KEY`
pattern already established elsewhere in the same file for this exact problem.

**Undefined CSS custom-property references silently disable dark-mode theming in ~10 SCSS files.**
`var(--text, …)`, `var(--text-muted, …)`, `var(--ul-surface-alt, …)`, `var(--ul-accent, …)`, and
several others are used throughout `_e2ee.scss` (13x), `_setup.scss` (12x), `_markup.scss`,
`_webauthn.scss`, `_messages.scss`, `_gallery.scss`, `_games.scss`, `_trivia.scss`,
`_assistant.scss`, `_pin-detail.scss`, `_profile.scss`, and `_wiki.scss` — but none of these custom
properties is ever defined anywhere (`_tokens.scss`/`_surfaces.scss` grepped, zero matches). Every
one of these rules permanently renders its hex fallback and can never respond to
`[data-theme="dark"]`, despite reading as token-driven. This is a broader, previously-uncaught
instance of the color-token issue the 2026-07-23 `_explainer`/`_map`/`_e2ee` review resolved as
"fine" — that review apparently didn't verify the referenced tokens actually exist.

---

## UL-277: pin-detail external-data freshness window is one global knob, not per-source

**PARKED 2026-07-23 at Jess's request ("skip over this one for right now. I need to reassess
this another day").**

Original wording: "Cache time needs adjustments for some pin details data. Load page, wait 10
minutes, reload page, some items are marked as 'fresh'." The mechanism is technically correct
(`LocationCache.set()` bumps `updated` properly); the actual gap is that `LocationCache.is_stale`
compares against a single site-wide, multi-day `SiteSettings.external_data_cache_days` applied
identically to every external-data source. Implementing this properly means a per-source TTL
override (a field on `PanelSource`/`InfoPanelSource`, or a source→days mapping in
`SiteSettings`) defaulting to the existing global value - plus knowing which sources the
reporter considers too slow to refresh.

---

## Authenticated media gate - residual per-family risk (2026-07-23)

`/media/...` is now served through `dashboard.controllers.media.MediaGateView` (nginx `location
/media/` proxies to Django; authorized responses hand back to the `internal`-only
`/_protected_media/` alias via X-Accel-Redirect). Ownership is enforced per path family where it
is cleanly derivable, but several families intentionally fall back to **authenticated-only**
access (any logged-in user can fetch, no per-object check). Marked with `TODO(media-auth)`
comments in `src/urbanlens/dashboard/controllers/media.py`:

- **`pin_custom_icons/` (Pin.custom_icon) and `label_icons/` (Label.custom_icon)**:
  authenticated-only. Strict owner-only enforcement risks breaking any surface that renders
  another user's shared/labeled pin (shared pin views, trip member maps, global labels with
  `profile=None`). Residual risk is low (small decorative icons, not photos), but a determined
  enumerator could fetch other users' custom icons. Fix would be: owner OR global label OR an
  existing share/visibility relationship.
- **Orphan files** (a file on disk under `pin_images/` or `comment_images/` whose owning
  Image/Comment/TripComment row no longer exists, e.g. row deleted without file cleanup):
  authenticated-only, since there is no owner left to check. Residual risk: pre-existing orphans
  from deletions remain fetchable by any logged-in user who knows the filename.

  **Update (chunk 520, 2026-08-15): the orphan *source* is closed for comments.** Swept every
  delete path: all `Image` paths already removed their file (bulk ones with a shared-file
  reference rule), but `comment.delete()` did not - Django stopped deleting `FileField` files in
  1.3 - so every deleted comment-with-photo stranded a file that this branch then served to any
  authenticated user. Both comment delete paths (pin/wiki and trip) now discard the file;
  `attach_existing_comment_image` copies rather than sharing storage, which is what makes that
  safe. Two tests. The residual risk is now bounded to *historical* orphans and crash windows
  rather than accumulating with normal use - a one-time sweep of `comment_images/` against
  surviving rows would close it entirely.

  **Systematic sweep (chunk 521)**: seven file-bearing model fields exist. `Image.image` and both
  comment images are now handled; explicit *clears* of `Achievement.custom_icon` and
  `Label.custom_icon` now delete their files too (a user pressing "remove icon" is the same
  expectation as deleting a photo). **Still stranding files, recorded not fixed**: replacing an
  icon or avatar with a new upload leaves the previous file, and deleting a Pin/Label/Achievement
  row leaves its icon. Those want a `post_delete`/`pre_save` receiver pair rather than per-caller
  code - the right shape, but a signal touching five models deserves an owner's review rather
  than an audit chunk, and the residual is small decorative icons under an already
  authenticated-only branch.
- **Unknown path families** (anything under MEDIA_ROOT outside the cataloged prefixes
  `pin_images/`, `comment_images/`, `avatars/`, `pin_custom_icons/`, `label_icons/`):
  authenticated-only, logged at INFO. Any future `upload_to` prefix must get an explicit branch
  in `MediaGateView._authorized` or it silently inherits this fallback.
- **`avatars/` (Profile.avatar)**: deliberately any-authenticated-user (avatars render site-wide
  next to usernames) - not a gap, but noted for completeness.
- **Safety check-in photos** (`Image.safety_checkin` set) currently follow the generic
  `Image.objects.visible_to` photo-visibility logic rather than the safety feature's own
  contact-sharing rules; if check-ins are ever shared with emergency contacts who fail the
  photo-visibility check, those contacts would be denied the photos (and vice versa: users
  passing `visible_to` but outside the check-in's audience can fetch them).

**Suggested next step**: product decision on icon visibility (owner-only + share-relationship vs.
authenticated-only), a cleanup job for orphaned media files, and a review of safety check-in photo
audience rules.

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

## Overpass deploy-side follow-up: raise the openresty 90s proxy cap (found 2026-07-22; edge box located 2026-07-23)

The self-hosted Overpass instance (`overpass.osm.urbanlens.org`, now the primary endpoint) sits
behind an openresty reverse proxy that cuts every connection at exactly 90s, regardless of the
Overpass `[timeout:N]` the client requested - the benchmark's only self-hosted failures were
region-scale scans hitting this cap, not Overpass giving up (see
`docs/reports/overpass-mirror-test.md`). Until the proxy timeout is raised above the intended
`[timeout:N]` ceiling, any query needing >90s fails at the proxy.

**Narrowed 2026-07-23**: the Overpass container itself runs on chiron
(`overpass`, `wiktorn/overpass-api:latest`, host port 21890), but the openresty is NOT on
chiron (no 80/443 listener, no openresty/nginx service there; the domain resolves to
163.182.80.211, a separate edge box proxying to chiron:21890). Raising the cap means editing
`proxy_read_timeout`/`proxy_send_timeout` (or the openresty equivalent) on that edge box -
access only Jess has.

---

## Deferred from 2026-07-22: aliases/labels aggregation, and boundary voting

The ROADMAP's "Pin Restructure" section asks for two more things deliberately not attempted as
riders on other work:

**Aliases and labels are not yet aggregated across child pins.** The parent detail page's "show
child pin details" toggle now aggregates map markers, the photo gallery, visit history, and
Notes/comments - but `pin_alias_suggestions` (`controllers/pin.py`) and the
category/tag/status membership panel (`controllers/labels.py`'s `LabelPinMembershipView` /
`label_membership_panel.html`) are both strictly per-pin, with no descendant awareness. Both are
shared generic components also used for Wiki and Image label/alias editing - bolting
hierarchy-aware aggregation onto them risks either duplicating the template or polluting a
generic component with a pin-specific concern. Decide whether aggregation means read-only "also
shown on child pin X" listings (cheapest, matches what comments got) or genuine cross-pin
editing before touching the shared templates.

**Boundary-source voting (REData vs. Overpass, weighted by recency) was not started at all.** It
needs a new model (`BoundaryVote` or similar), a weighting/tie-breaking algorithm, a comparison
dialog with a side-by-side map, and a way to surface "cast a vote" once consensus already exists -
a materially larger, standalone feature (see ROADMAP.md's "Pin Restructure" section, last
bullet, which specifies the weighting rule in detail).

---

## `docker compose exec app pytest` can't reach Valkey in the `s1`/`s2`/`s3` dev environments (found 2026-07-24)

Running the hypothesis suite via `docker compose exec app python -m pytest ...` inside any of
the `~/dev/s1|s2|s3/UrbanLens` environments on chiron fails almost every test that touches a
logged-in request or Celery/Channels broadcast (`realtime.broadcast`, channel-layer setup, etc.)
with:

```
RuntimeError: External network access is disabled during tests. Attempted to connect to
'172.23.0.3'; mock this integration or use localhost.
```

Root cause: `src/urbanlens/core/testing_network.py`'s `LocalhostOnlyNetwork` guard only permits
connections to literal `localhost`/`localhost.localdomain` during tests (by design - see its
docstring). But in these dev environments, `UL_VALKEY_URL` resolves to the `urbanlens_valkey`
docker-compose service, i.e. a docker-network IP (`172.23.0.3` in this instance), not
`localhost` - so anything touching Valkey during a test run trips the guard immediately.

Confirmed this is **environment infrastructure, not application code**: a completely unrelated,
untouched test file (`test_games_controller.py`) fails identically. Meanwhile, pure-DB-layer
tests with no client/channel-layer involvement (e.g. `test_spotguessr_eligibility.py`) pass
cleanly in the same run - so the guard itself and the DB-layer test infra are fine; it's
specifically the Valkey reachability-vs-guard mismatch.

Not investigated further (out of scope for the SpotGuessr UX work this was found during): worth
checking whether `docker-compose.yml`'s `app` service should bind-mount/forward Valkey to
`localhost` for these dev boxes specifically (other deployments may already do this correctly,
or CI may run tests a different way that sidesteps it entirely - e.g. a dedicated test compose
profile). Until fixed, verify backend changes on these dev machines via direct DB-layer/service
tests (no Django test client, no `realtime.broadcast`) plus a manual browser walkthrough against
the running `docker compose up` stack, rather than the full `pytest` suite.

## ~~SiteSettings.ai_article_expansion_enabled / ai_article_safety_enabled have no migration~~ (found and fixed 2026-07-25)

While generating a migration for an unrelated new `SiteSettings` field
(`ai_trivia_wiki_incorporation_enabled`, Trivia Phase 4), `manage.py makemigrations` reported
these two fields as pending additions even though both are already defined on the model and have
been in use since the "Implement AI article expansion and safety review features" commit. The
migration squash ("squash into single migration for release") apparently ran before that commit's
own migration was authored, or that migration was never committed - every test that creates a
`SiteSettings` row (effectively the whole suite, via `promote_first_user_if_needed`'s
`SiteSettings.objects.get_or_create`) failed with `ProgrammingError: column
dashboard_site_settings.ai_article_expansion_enabled does not exist` on a freshly migrated DB.
Initially left out of Trivia Phase 4's own migration as out-of-scope, but ended up blocking that
same phase's dev-pod verification (wiki incorporation directly reuses the article-expansion
pipeline), so it was folded into
`0011_trivia_wiki_incorporation_setting.py` alongside the Trivia field rather than filed away
again - see that migration's own comment.

## Setup wizard sidebar reuses inverting `--ul-grey-N` tokens on an always-dark panel (found 2026-07-25)

`_setup.scss`'s `.setup-wizard__sidebar` sets `background: rgba(0,0,0,0.3)` on top of whatever
the parent `@include surface()` background is - like the map filter panel and a few other
"always dark" chrome panels called out in `_tokens.scss`'s comments, it reads as a fixed dark
strip in both themes rather than genuinely inverting. But unlike those other always-dark panels,
its child text (`.brand-name`, `.setup-stepper__item`, etc.) uses the regular inverting
`var(--ul-grey-1)` / `var(--ul-grey-5)` / `var(--ul-grey-6)` / `var(--ul-grey-7)` tokens. In light
mode `--ul-grey-1` is a near-white (`#dfdfdf`), which reads fine against the dark sidebar overlay.
In dark mode `--ul-grey-1` flips to a near-black grey (`$color-grey-8`, `#373737`), which is
low-to-no contrast against that same dark sidebar background - the sidebar text likely becomes
close to unreadable in dark mode. Not fixed here because it's a structural mismatch (the
component assumes a static-dark treatment but was styled with tokens meant to invert), not a
missing/undefined custom property - fixing it means either converting the sidebar's own text
tokens to a fixed light-on-dark scheme (like `_tokens.scss`'s `$ui-fp-*`/`$ui-link-on-dark`
pattern for the map filter panel) or making the sidebar itself genuinely theme-aware. Worth a
manual dark-mode check of `/setup` before shipping.

## A few more hardcoded danger-red controls without dark overrides in `_pin_lists.scss` (found 2026-07-25)

While fixing the `.saved-filter-region-mode-btn--active` include/exclude buttons (raw
`#2e7d32`/`#c62828`, now real `--ul-color-success-text`/`--ul-color-danger-text` tokens), noticed
several sibling controls in the same file with the identical pattern that were **not** in scope
for that fix: `.pin-list-more-menu-danger` (`color: #ef4444 !important`, ~line 325),
`.saved-filter-delete-btn` (`color: #ef4444`, ~line 629), and a related hover state at ~line 521
(`color: #fca5a5`). None has a `[data-theme="dark"]`/`_dark.scss` counterpart. Given
`--ul-color-danger-text` now exists and already resolves correctly in both themes, these three
are likely a quick follow-up: swap the raw hex for `var(--ul-color-danger-text, <original-hex>)`
the same way the region-mode buttons were fixed.

**RESOLVED 2026-08-17 (chunk 582).** All five occurrences across the three controls now use the
token, keeping their original hex as the fallback. `--ul-color-danger-text` is defined in `:root`
and again under `[data-theme="dark"]` (`#f87171`), so it resolves in both themes as this entry
said. The `#fca5a5` hover on `.pin-list-item-remove` was worth a second look rather than a
mechanical swap: it is a *pale* red, chosen for a dark surface, on a control whose rest state
already inverts through `--ul-grey-4` - so it was a light-mode contrast problem, not a deliberate
lighter-on-hover treatment. Compiled clean with `sass`; the built CSS is gitignored
(`.gitignore:9`), so the source change is what ships.

## Safety check-in partners: two residual gaps found during a fresh-eyes feature review (2026-07-25)

A full review of the partner/live-location/post-resolution-encryption feature (two independent
review agents, backend-correctness and frontend-security) found and fixed nine issues directly
in `services/visits/safety.py`/`consumers.py`/`tasks.py`/`models/safety/model.py` (archival payload not
capturing/severing `destination_location`/`trip`/`markup_map`/`markup_maps`, `archive_checkin`
non-atomicity, chat messages postable after archival, three TOCTOU races, a missing index, an
N+1, and no live-connection revocation on partner removal - all covered by new tests in
`test_safety_archival.py`/`test_safety_partners.py`/`test_safety_live_location.py`/`test_safety.py`).
Two narrower items were identified but deliberately left open:

- **Blocking a partner doesn't revoke their existing access.** ~~`Profile.are_blocked` creation has
  no signal wired to `SafetyCheckinPartner` cleanup~~ **RESOLVED 2026-08-17 (chunk 580).**
  `block_profile` now calls `remove_checkin_partner` for every `SafetyCheckinPartner` row between
  the two profiles in either direction, which was this entry's own prescription - that helper
  already deletes the row *and* force-closes any live WebSocket, so nothing new was needed beyond
  calling it. Outstanding invitations are revoked alongside accepted rows, an unaccepted invite
  being an offer of exactly the access in question. Covered by
  `test_block_revokes_safety_partner.py`, including that an unrelated partner on the same check-in
  is untouched. The deferral here was review scope, not principle.
- **A malformed/corrupted `MessagingKeyBundle.public_key` makes `archive_checkin` fail forever,
  loudly but without escalation.** `archive_checkin` now isolates one checkin's failure from
  others in the 5-minute sweep (a bad row no longer blocks the rest of the batch) and every
  attempt is logged via `logger.exception`, but there's still no cap or alerting on repeated
  failures for the *same* checkin - it will silently retry and re-fail every 5 minutes
  indefinitely if a specific owner's key bundle is genuinely corrupt, with only a log line as the
  trail. Not expected to happen in practice (enrollment writes a fresh valid key), but there's no
  guard against it happening anyway (e.g. a future data-migration bug). Worth a "give up and flag
  for manual review after N failed attempts" backstop if this class of bug ever surfaces in
  practice.

## `test_post_without_name_returns_400` is stale against UL-360's optional-name behavior

`test_trip_controller.py::TripCreateViewTests::test_post_without_name_returns_400` (line ~224)
posts `{"name": ""}` to `TripCreateView` and asserts a 400. Found failing during the pod
verification of an unrelated trips-list-page/safety-checkin feature (2026-07-25) - not touched by
that work. `TripCreateView.post` (`controllers/trip.py`) already has: `name = (body.get("name")
or "").strip() or random_trip_name()` - per its own "Name is optional (UL-360)" comment, a blank
submission has deliberately generated a random name instead of rejecting the request since UL-360
shipped (2026-07-24, see the Feature build entry above). The test predates that change and was
never updated; it should either assert `200` + a non-empty generated `trip.name`, or be deleted
if UL-360's own test coverage (`test_trip_names.py`?) already covers the generated-name path.

---

## Full-codebase audit: re-verification pass (2026-07-25)

After the initial 35-unit audit (above) was worked through fix-by-fix in an earlier session, six
independent re-verification passes re-read every finding in `docs/notes/ai/codebase-audit.md`
against the current code (not trusting the earlier session's own claims) and reported per-finding
FIXED/PARTIALLY-FIXED/NOT-FIXED/REGRESSED verdicts. Most findings held up as genuinely fixed; the
handful of regressions and higher-value gaps the re-verification surfaced were fixed directly in
this pass:

- **`services/ai/openai.py`'s `get_client()`** unconditionally passed `base_url=str(self.api_url)`
  to the OpenAI SDK. `OpenAIGateway.setup()` never actually sets `api_url` (unlike the
  Cloudflare/HuggingFace gateways), so this was always `str(None)` - the literal string `"None"` -
  meaning a real OpenAI call would have tried to connect to that instead of the SDK's real default
  endpoint. Pre-existing bug, not a regression from the earlier fix pass; now only passes
  `base_url` when set.
- **SpotGuessr's new reverse-geocode cache (`services/spotguessr/geo_bonus.py`) treated a rate-limit
  failure as a genuine "no result" and cached it for the full 30-day TTL** - a transient Nominatim
  rate-limit hit (the exact failure mode the cache exists to work around) would have silently
  disabled the country/state/city bonus for an entire ~111m cell for a month. `reverse_geocode_admin`
  (`services/apis/locations/nominatim.py`) now lets request/transport failures propagate instead of
  swallowing them to `None`, and `geo_bonus.py` gives a failed lookup a 60-second TTL instead of 30
  days, while a genuine "nothing found" result still gets the long TTL.
- **The undo framework's `stash_for_undo()` calls in `pin_bulk.py`, `detail_pins.py` (×2), and
  `location_wiki.py` ran *before* the `transaction.atomic()` block wrapping the delete**, with a
  comment claiming the atomic wrapper prevented a partial-delete-with-stashed-undo inconsistency -
  it didn't, since the stash (an immediate `UndoAction.objects.create()`) had already committed
  before the atomic block even opened. Moved the stash call inside each atomic block, before the
  delete, so a mid-delete failure now rolls back both together.
- **The storage-quota check-then-create race (`services/media/storage.py`'s `per_profile_upload_lock`)
  was only wired up at 2 of 8 call sites** (`photos.py`, `image_gallery.py`) - `article.py`,
  `direct_messages.py`, `maps.py`, `safety.py`, `tools.py`, and `visits.py` still raced. All six now
  wrap their check-then-create in `per_profile_upload_lock`.
- **`LocationManager.get_nearby_or_create()`** (unlike `PinManager`'s already-fixed version) had no
  `try/except IntegrityError` around its `create()` call, despite `Location` having a real
  `unique_together = ["latitude", "longitude"]` constraint that two concurrent requests creating a
  Location at the exact same coordinates could hit. Now catches it and returns the
  concurrently-created row, matching `PinManager`'s pattern.
- **`services/messaging/direct_messages.py`'s email/text-alert debounce (`is_email_debounced`/
  `is_text_alert_debounced`) was still a plain `cache.get()` check-then-later-`cache.set()`** - the
  same TOCTOU shape already fixed in the sibling `notification_text_alerts.py` via atomic
  `cache.add()`. Ported the same fix; the now-redundant `cache.set()` calls inside
  `send_message_email_now`/`send_message_text_alerts_now` were removed since the marker is claimed
  atomically by the check itself. (`test_direct_messages.py`'s debounce test was rewritten to
  exercise this through the real task entry point, matching how the sibling module's tests already
  verify the same pattern.)
- **`services/ai/assistant.py`'s `_tool_add_trip_activity`** had the identical TOCTOU race
  (`trip.activities.count() >= max_activities` check-then-create) that `_tool_create_trip` and
  `link_extraction.start_link_extraction` had just been fixed for in the same pass - it wasn't
  itself covered. Now locks the `Trip` row (not the profile - the count is per-trip, and other
  members can add activities to the same trip) for the check-then-create.
- **Duplicate, conflicting dark-mode CSS for `.subscription-admin-page .role-pill`** - independent
  fix passes had added a `[data-theme="dark"]` override in both `_admin.scss` and `_dark.scss`,
  with different colors; `_admin.scss`'s `@use` order meant its version always won, making the
  `_dark.scss` copy dead and misleading. Removed the dead copy.
- **The undo framework's `MODEL_LABEL` constants** (added to `handlers/pin.py`, `handlers/wiki.py`,
  `handlers/safety_checkin.py` specifically to stop call sites hand-typing `"pin"`/`"wiki"`/
  `"safety_checkin"` as bare strings) were never actually imported at any of the ~8
  `stash_for_undo(...)` call sites - the fix added the constants but didn't wire them up. Added the
  missing `MODEL_LABEL` constants to `handlers/saved_filter.py`/`handlers/trip.py` and updated every
  call site (`pin_bulk.py`, `detail_pins.py`, `location_wiki.py`, `safety.py`, `saved_filters.py`,
  `trip.py`, `models/pin/viewset.py`) to import and use the shared constant instead of a literal.

**Confirmed still open** (verified genuinely unfixed, not worth blocking on for this pass - listed
here so the next session doesn't have to re-derive them from `docs/notes/ai/codebase-audit.md`'s
full per-unit detail):

- `services/messaging/direct_messages.py`'s TOCTOU fix above only covers the DM email/text debounce; the
  underlying **`quota_error_for_upload`/`per_profile_upload_lock` pattern itself is a "soft" lock**
  (proceeds without the lock if it can't be acquired promptly) - fine for its stated purpose but
  worth remembering it's not a hard guarantee.
- **Unit 08**: `pin.py`'s `media_send_to_wiki` still synchronously downloads up to 20 media items
  in the request handler; no shared upload helper exists despite the sequence being duplicated
  across ~8 call sites now sharing the same lock.
- **Unit 09/10**: bulk-accept/reject's per-item failures still aren't surfaced in the frontend
  toast; both trip-invite paths and calendar-push still loop per-invitee/per-activity without
  batching or debounce; `TripActivity.order` still has no uniqueness constraint or locking.
- **Unit 13/14/19**: `controllers/labels.py`'s `ai_kind_enabled`/`keyword_kind_enabled` duplication
  between `.get`/`.post` is unchanged; `NotificationPreference` still only models 12 of 30
  `NotificationType` values; no admin can see/revoke another admin's subscription grants; no
  restore tooling exists for the Postgres backups.
- **Unit 20**: `PinSerializer.create()` and `parse_for_preview` still make synchronous/blocking AI
  calls in the request cycle rather than via Celery; `services/ai/huggingface.py` is still an
  unwired, `NotImplementedError`-raising stub (now explicitly documented as such, rather than a
  silent dead end).
- **Unit 21/22/23**: `models/pin/viewset.py`'s post-`get_object()` ownership re-check is still dead
  code (queryset already filters it); `GroupMessage` still carries no images/markup_map/
  location_mentions/reply_to fields; `GameSessionConsumer`/`TriviaSessionConsumer` are still
  near-duplicate classes with no shared base, no per-connection rate limiting on any WS `receive()`.
- **Unit 24/25**: the SpotGuessr/Trivia `eligible_locations()`/`eligible_questions()` retry loops
  still re-run the full query on every attempt instead of computing the eligible set once; no
  moderation UI exists for AI-flagged trivia questions (decided against, not just unbuilt - see
  `docs/designs/drafts/trivia.md`'s "Known gaps"); Trivia gained a leave/kick path plus
  stall-handling parity with SpotGuessr on 2026-07-25 (`services.trivia.session.leave_session`/
  `kick_participant`/`force_reveal_round`/`end_session_now`) - SpotGuessr itself still has no
  leave/cancel/kick path once a lobby exists.
- **Unit 31**: `_dark.scss` is still ~1100 lines of per-selector overrides (the role-pill fix above
  removed one duplicate, not the pattern); `_pin_lists.scss` still has 3 sibling raw-hex danger-red
  controls without dark overrides (`.pin-list-more-menu-danger`, `.saved-filter-delete-btn`, and its
  hover state) that PROBLEMS.md already flagged as a follow-up.
- **Unit 34**: only ~30/111 `@given`-using test files import the shared `strategies.py` module
  (up from 8/97, but still a minority); `test_trivia_wiki_incorporation.py` has zero `@given` tests
  despite an obvious property-testing candidate (the upvote-count threshold logic); a prior
  session's claim that hypothesis tests were added to `test_safety.py`/
  `test_safety_checkin_slugs.py`/`test_trip_controller.py` does not hold up under inspection - those
  three files still have zero `@given` tests (only `test_trip_helpers.py` and the two genuinely new
  files, `test_safety_archival.py` and `test_trivia_wiki_incorporation.py`, show real hypothesis
  work, and the latter's own tests are all hardcoded-value examples).

All of the above are maintainability/completeness gaps, not active security or correctness bugs
(those categories were the ones fixed directly, above) - reasonable to pick up as a dedicated
follow-up rather than blocking this pass.

## `docker compose up`'s app container fails `manage.py migrate`'s implicit check with a PinViewSet `AssertionError` on `s2` (found 2026-07-25, not root-caused)

While standing up the Consensus game feature (new models/services/consumer/URLs), tried to verify
against a live stack on the `s2` dev environment (`~/dev/s2/UrbanLens` on chiron). The full
`docker compose up -d --build` failed - the `app` container's entrypoint init script runs
`manage.py migrate`, which runs Django's implicit system checks first, and that failed with:

```
File ".../dashboard/urls.py", line 93, in <module>
    router.register("pins", PinViewSet, basename=PinViewSet.basename)
File ".../rest_framework/routers.py", line 170, in get_default_basename
    assert queryset is not None, '`basename` argument not specified, and could ...'
AssertionError: `basename` argument not specified, and could not automatically determine the name...
```

`PinViewSet.basename = "pins"` is a plain class attribute and `dashboard/urls.py:93` passes it
explicitly (`basename=PinViewSet.basename`) - by inspection this should never reach
`get_default_basename` at all, since DRF's `SimpleRouter.register()` only calls that when
`basename` is `None`. Confirmed installed `djangorestframework==3.17.1` is identical on both `s2`'s
container and the local Windows venv, where the equivalent `manage.py check` (run directly, not via
`migrate`) passes cleanly every time. Root cause not found - ran out of scope budget chasing it
while `s2` was also independently blocked on an unrelated stale-migration-graph issue (below), which
was the one actually relevant to this session's work.

Worked around entirely by bypassing the custom entrypoint (`docker compose run --rm --entrypoint ''
app .venv/bin/python -m pytest ...`), which never imports the full URLconf (pytest doesn't eagerly
resolve URLs unless a test actually calls `reverse()`/hits a view) - this is how the Consensus
DB-backed test suite ended up getting verified despite this. Not confirmed whether this also affects
a genuinely fresh checkout with no other changes (an `s3` attempt at the same thing got stuck at
container state `Created` with zero log output before this could be isolated) - worth a fresh,
focused repro next time someone needs `docker compose up`'s full stack (not just `docker compose
run`) on one of these dev boxes.

## Dev environments (`s1`/`s2`/`s3` on chiron) can silently drift behind `origin` (found 2026-07-25)

`~/dev/s2/UrbanLens` was one full commit behind `origin/@release/v0.6.0` (missing
`0017_spotguessr_participant_rating_delta.py` and everything else in the "Implement multiplayer
enhancements... for SpotGuessr" commit) despite `git status` reporting clean - a `git fetch`/`log`
comparison is needed to actually notice this, since "clean working tree" says nothing about how
current the checked-out commit is. This produced a confusing
`NodeNotFoundError: ... dependencies reference nonexistent parent node ('dashboard',
'0017_spotguessr_participant_rating_delta')` when testing a new migration that (correctly, per this
same file's `makemigrations`-dependency gotcha) depended on the latest *committed* migration - the
dependency was fine, the dev box just didn't have it yet. Fixed for this session by `git fetch` +
`git pull --ff-only` (stashing/resolving trivial conflicts in files also touched by the missed
commit, e.g. `CELERY_BEAT_SCHEDULE` dict entries landing near each other) - worth checking `git log
origin/<branch> -1` vs. local `HEAD` up front, not just `git status`, when picking a "free" dev
environment for migration-touching work.

## Residues left by the TEMPORARY legacy-CID coordinate repair (found 2026-07-25)

`services/apis/locations/legacy_cid_coordinate_fix.py` lets a re-import move a user's
pre-2026-07-25 pins off the coordinates the old S2-decode guess put them on. Two known gaps
that it deliberately does *not* close - both should disappear when that module is deleted,
but re-check them then rather than assuming:

1. **The CID stays on the bad `Location`.** `GooglePlace.cid` is `unique=True`, so the repaired
   pin's new (correct) Location can't claim the CID while the old, wrongly-placed Location still
   holds it - the backfill in `_create_pin_from_confirmed` is skipped for exactly this case.
   Consequence: `Location.objects.by_cid()` keeps resolving that CID to the wrong Location for
   *every* user, and each re-import pays a fresh REData/Places resolution instead of a cache hit.
   Repointing the CID would fix it globally, but it mutates shared cross-user data off the back of
   one user's import, which is why it wasn't done here. Deliberate call, not an oversight.

2. **`GoogleMapsGateway.import_pins_streaming` was left un-repaired.** It's the older one-shot
   `pin.upload.takeout` path, and it still places CID pins from `extract_coordinates_from_url`'s
   S2 decode - i.e. it can still create wrongly-placed pins today. Nothing in
   `templates/dashboard/pages/location/import/csv.html` (or anywhere else) references that URL;
   the UI goes through `pin.import.preview` -> `pin.import.confirmed`, which defers CID pins
   properly. The repair was not wired into it because doing so safely means giving it the same
   deferral machinery, not because it's correct as-is. Either give it the deferral path or delete
   the route and `import_pins_streaming` with it - a live URL that silently mis-places pins is
   worse than no URL.

## `test_spotguessr_geo_bonus.py` leaks a real Valkey cache entry across tests (found 2026-07-26)

Found while running the full Consensus/SpotGuessr regression suite alongside the new Facts
system tests (unrelated to Facts - `services/spotguessr/geo_bonus.py` was never touched).
`BonusPointsForGuessTests::test_geocode_failure_earns_nothing_without_raising` fails with
`AssertionError: 750 != 0` even run alone in its own file (`docker compose exec test-runner
python -m pytest src/urbanlens/dashboard/tests/hypothesis/test_spotguessr_geo_bonus.py`, 1
failed / 8 passed) - so it's not cross-file pollution, it's within-class.

`bonus_points_for_guess` -> `_reverse_geocode_admin_cached` (`services/spotguessr/geo_bonus.py:157`)
caches Nominatim's admin lookup in the real Valkey cache keyed by rounded coordinates, and the
test class never clears that cache between tests. `test_matching_every_offered_tier_stacks_the_bonus`
and `test_no_match_at_all_earns_nothing` run earlier in the same file against the same
`guess_point = Point(-73.75, 42.65, ...)` used by the failing test, so whichever of those wrote a
cached "match" result first is still there when the geocode-failure test runs - the mocked
`NominatimGateway` returning `None` never gets consulted because the cache short-circuits it.
Fix is a `self.enterContext` cache-clearing fixture (or `cache.clear()` in `setUp`) in
`BonusPointsForGuessTests`, matching how other Valkey-cache-touching test classes in this suite
already reset between tests.


- **`dev.urbanlens.org` sits behind Cloudflare, which caches everything under `/static/...`
  (compiled `style.css`, `map-annotations.js`, `article-wysiwyg.js`, etc.) for 4 hours
  (`Cache-Control: max-age=14400`), keyed on the exact URL - these files have no cache-busting
  hash in their filename, so a fresh rebuild+redeploy does NOT make the live site serve the new
  CSS/JS immediately; `curl -sD -` will show `cf-cache-status: HIT` and a `last-modified` older
  than the actual rebuild. A live browser check done shortly after deploying a CSS/JS-only change
  can silently verify the OLD stale asset while looking like a pass (structural/HTML changes are
  unaffected - Cloudflare doesn't cache the dynamic page response, only `/static/` files).
  **Workaround for verification**: fetch the asset with a cache-busting query string
  (`curl "https://dev.urbanlens.org/static/dashboard/style.css?cb=$(date +%s)"`) to force
  `cf-cache-status: MISS` and confirm what's actually at the origin, or use Chrome DevTools
  Protocol's `CSS.getMatchedStylesForNode` (via a Playwright CDP session) to see which rule a
  live page actually applied if a rendered value looks unexpectedly stale. (This applies to the
  public dev/staging/prod deployments, not the local docker-compose stack described above.)

## Messaging / external API (noted 2026-07-26, during the mobile v2 messaging API build)

- **WebSocket credential auth does no per-scope check.** `ApiKeyAuthMiddleware` (see
  `src/urbanlens/dashboard/websocket_auth.py` and its use in `UrbanLens/asgi.py`) authenticates
  a WebSocket connection from any valid, unrevoked credential and then grants blanket access -
  it never consults the connection's scopes. So a token issued with, say, only `pins:read`
  can still open `ws/messages/` and `ws/notifications/` and receive live direct-message and
  notification payloads, which is precisely the data `OAUTH2_ONLY_SCOPES` restricts to
  user-consented OAuth2 tokens on the HTTP side. The HTTP messaging endpoints added in
  `external_api/views_messaging.py` enforce `messages:read`/`messages:write` correctly; the
  socket path is the remaining hole, and it currently undercuts that enforcement because the
  same data is reachable over the socket without the scope.
  **Deliberately not fixed in that pass** (scope control - it touches three consumers and
  their tests, and a mistake there disconnects working *web* clients, whose session auth flows
  through the same middleware). **Fix shape**: give each consumer a required-scope declaration
  and have the middleware/consumer `close()` with a 4403-style code when a credential-authed
  connection lacks it, leaving session-authed connections (`request.auth is None`) untouched -
  the same session-or-credential split `external_api/mixins.py:IsSessionAuthenticated` already
  draws for HTTP. Needs tests for: scoped token accepted, under-scoped token rejected, session
  unaffected, PAT rejected outright on messaging sockets.

- **Markup-map attachments bypass share provenance.** Attaching a `MarkupMap` to a direct
  message (`create_direct_message(markup_map_uuid=...)`, and the `send_message_with_share`
  path in `services/messaging/direct_message_shares.py` when no `shared_pin_id` accompanies it) records
  **no `LocationExposure`**, even though a markup map can depict pin locations and therefore
  can disclose them to the recipient. Sharing the *pin* correctly stamps the chain via
  `create_pin_share` -> `resolve_and_stamp_origin_share` + `record_share_exposure`; attaching a
  map that draws the same place does not, so the location's re-share history silently has a
  hole in it. **Not a regression** - the web composer has always behaved this way and the new
  API endpoint merely matches it, which is why it was documented rather than changed
  mid-build. **Fix shape**: on attach, resolve the `MarkupMap`'s items to the pins/locations
  they reference and record an exposure per distinct location, reusing `record_share_exposure`
  rather than inventing a second provenance path. Decide first whether a hand-drawn annotation
  with no linked pin should count (probably yes if it carries coordinates).

- **Three pre-existing mypy errors surface whenever anything type-checks the external API's view
  module** (found 2026-07-26 while adding the lists/labels external endpoints; none are caused by
  that work, and all three live in files it does not touch):
  - `dashboard/models/boundary/queryset.py:87` - `"GEOSGeometry" has no attribute "exterior_ring"`
    in `buffer_point_by_meters`. `Point.buffer()` is typed as returning the `GEOSGeometry` base
    class, but the code relies on the result actually being a `Polygon`. Wants a narrowing
    `assert isinstance(circle, Polygon)` (or a typed helper) rather than a `cast` - the runtime
    assumption is genuinely unchecked today.
  - `dashboard/forms/search.py:176` - `resolve_reference(...)` is handed `self.profile`, typed
    `Profile | None`, where a non-optional `Profile` is expected. Either `SearchForm.profile`
    should be non-optional or this branch needs an explicit None guard; as written, a form built
    without a profile would fail here at runtime.
  - `dashboard/controllers/trip.py:481` - `existing_ids.add(trip.creator_id)` where `creator_id`
    is `int | None`, so a trip with no creator would insert `None` into a `set[int]`.

  All three look like real latent bugs rather than annotation noise, which is why they are logged
  here instead of silenced. They don't fail today because mypy isn't run across these paths
  together - they only appear once `external_api/views.py` pulls them into one type-check graph.

- **Pin-detail's `wiki_slug` was unusable for navigating to a wiki (FIXED in this pass).**
  `services/pins/pin_detail.py::build_pin_detail` set `payload["wiki_slug"] = wiki.slug`, which reads
  naturally as "the slug to fetch this pin's wiki with". It isn't. Every wiki-scoped route
  resolves through `services.wiki.wiki_access.resolve_visible_wiki`, which takes a **Location**
  slug/uuid - and `Wiki.slug` is an independent `SlugField` on an unrelated model with its own
  value. A client that followed `wiki_slug` to `GET /wikis/{location_slug}/` therefore got a 404
  for a wiki it could plainly see. Fixed by adding `location_slug` (from
  `location.ensure_slug()`) to the payload and to `PinDetailSerializer`; `wiki_slug` is retained
  but documented as informational-only. Regression test:
  `tests/hypothesis/test_external_api_pin_detail_location_slug.py`.

- **The internal wiki edit view silently discards invalid input (NOT fixed - deliberate).**
  `controllers/location_wiki.py::LocationWikiEditView.post` iterates the editable fields and
  `continue`s past (a) a security value not in `SecurityLevel.choices` and (b) a date that fails
  `datetime.strptime(raw, "%Y-%m-%d")`. The user is told `{"ok": True}` and the field simply
  never changes, with no error surfaced anywhere - a submitted-but-dropped edit is
  indistinguishable from a successful one. The shared `services/wiki/wiki_edits.py::apply_wiki_edit`
  extracted in this pass takes a `strict` flag: the external API passes `strict=True` and gets a
  hard rejection, while the internal path keeps `strict=False` to preserve existing HTMX
  behavior. The internal path should be migrated to strict (with proper field-level error
  rendering in the About card) as a follow-up - it needs UI work, which is why it was left alone
  here rather than changed blind.

- **A wiki's "First pinned" date leaked past the low-pin-count privacy fuzz (FIXED).**
  `approximate_pin_count` deliberately refuses to show a number until at least
  `MIN_VISIBLE_PIN_COUNT` (3) distinct users have pinned a place, but the Community card showed
  "First pinned <Mon YYYY>" *unconditionally*. With only one or two pinners, that month is
  effectively "when this specific person pinned it" - exactly what the count fuzzing exists to
  hide. (The template already rendered `|date:"M Y"`, so the day was never displayed; the leak
  was the missing low-count suppression, and the fact that day-precision sat in the template
  context at all.) Fixed by `services/wiki/community_counts.py::wiki_community_summary`, which
  truncates `first_pinned` to the 1st of its month and returns `None` whenever `pin_count_low`
  is true. Both `LocationWikiView` and the external API now read that one function, and
  `wiki.html` renders the pre-truncated date rather than reaching into a Pin instance.

- **`MapController.resolve_place` does not honor the `external_apis_enabled` profile toggle**
  (`src/urbanlens/dashboard/controllers/maps.py:384-408`). Its sibling
  `autocomplete_places` (same file, line ~361) *does* check
  `request.user.profile.external_apis_enabled` and returns `{"disabled": true}` when the user
  has turned external lookups off - but `resolve_place`, which is called the moment the user
  *selects* one of those suggestions, checks only whether an API key/REData is configured. So a
  user who has opted out of external API calls still triggers a Google Places **Details** call
  (billable, and a privacy leak of what they searched for) on selection. Repro: set
  `Profile.external_apis_enabled = False`, GET
  `/dashboard/map/resolve-place/?place_id=<any>` -> still hits the provider. Fix: add the same
  `if not request.user.profile.external_apis_enabled: return ... 403` guard the external API's
  `PlaceResolveView` now applies
  (`src/urbanlens/dashboard/external_api/views.py`, `PlaceResolveView.get`). Noted while
  building the external `locations/resolve/` endpoint, which deliberately does *not* reproduce
  the omission; the internal path was left alone to keep that change out of an already large
  API-surface commit.

- **`test_spotguessr_geo_bonus.BonusPointsForGuessTests::test_geocode_failure_earns_nothing_without_raising`
  fails on a polluted cache, not on the code under test**
  (`src/urbanlens/dashboard/tests/hypothesis/test_spotguessr_geo_bonus.py:102-108`). The test
  patches `NominatimGateway` to return `None` and expects a 0-point bonus, but gets 750 (all
  three tiers). `services/spotguessr/geo_bonus.py::_reverse_geocode_admin_cached` memoizes the
  reverse-geocode result in the Django cache keyed by *rounded* coordinates, and the earlier
  tests in the same class populate that key with a matching admin dict - so the patched gateway
  is never called and the failure branch is never exercised. Reproduces with the file run
  alone (`pytest src/urbanlens/dashboard/tests/hypothesis/test_spotguessr_geo_bonus.py`), so it
  is not a cross-file interaction. Pre-existing: neither `geo_bonus.py` nor its test has been
  touched. Fix is a `cache.clear()` in that class's `setUp` (and arguably a project-wide
  `LocMemCache` reset between tests, since any cache-backed service has this hazard). Noted
  while building the external SpotGuessr API; left alone because the file belongs to another
  work stream.

- **The inbox list serializes each conversation's last message with no reaction/share
  prefetch** (`src/urbanlens/dashboard/services/messaging/direct_messages.py::conversations_for`,
  `src/urbanlens/dashboard/services/messaging/group_chats.py::group_conversations_for`). Each row's
  `last_message` is rendered through `build_direct_message_payload` /
  `build_group_message_payload`, which read `message.reactions.all()` (and, for groups,
  `message.share_for(viewer)`), so a page of N conversations issues ~2N extra queries. Both
  builders are correct; the missing piece is a prefetch on the *selected* last messages.
  It cannot simply be added to the existing queries: `group_conversations_for` scans every
  visible message across every group in order to pick the newest per group, so prefetching
  there would pull reactions for the whole history rather than for the N rows actually
  rendered. The fix is a second, id-bounded `prefetch_related_objects()` call over the
  already-selected `last_message` instances. Pre-existing on the 1:1 side; noted while adding
  reactions/`pin_share_id` to the group payload (`external_api/serializers_messaging.py`),
  which gave the group side the same shape. The thread endpoints - where a page is 50 messages
  rather than 1 - already prefetch, so this is an inbox-only cost.

- **`test_avatar_colors.GroupMemberSearchAvatarColorTests::test_results_get_distinct_colors`
  returns 0 results where it expects 4**
  (`src/urbanlens/dashboard/tests/hypothesis/test_avatar_colors.py:105-111`). The test creates
  four ANYONE-visible profiles named `searchable-user-<n>` and expects
  `GET messages.group.member_search?q=searchable-user` to return all four;
  `response.context["results"]` is empty. The candidate filter in
  `controllers/group_chats.GroupMemberSearchView` (and the `can_direct_message` gate it leans
  on in `services/messaging/direct_messages.py`) is the place to look - the test sets
  `user.username` directly with `save(update_fields=["username"])`, so a search that reads a
  denormalized/`Profile`-side name would match nothing. Both of those modules carry
  uncommitted edits from another work stream, and nothing in the social/avatar/annotation
  change this was found under touches conversation membership or direct-message gating.
  Noted while running `test_avatar_colors.py` as a regression check for the avatar-write
  extraction (`services/profile/avatar.py::set_profile_avatar`); left alone as it belongs to the
  messaging work stream.

- **Blocked `Friendship` rows created before `block_profile` started normalizing direction may
  record the wrong blocker** (`src/urbanlens/dashboard/services/social/friendship.py::block_profile`).
  `Friendship` has no "blocked_by" column, so `from_profile` is the only record of who blocked
  whom, and `block_profile` used to reuse whichever row already joined the pair - a block
  placed on an inbound friend request therefore left the *blocked* party as `from_profile`.
  It now re-points the row so `from_profile` is always the blocker, which fixes every block
  placed from here on, but existing rows carry no signal that could be used to repair them:
  a data migration would have to guess. Impact on a legacy row is bounded and inverted from
  the original P0 - the true blocker gets a 404 from `unblock_profile`/`remove_friend` and
  must re-block to normalize the row, and the blocked party can lift it. Worth a one-off
  audit query (`Friendship.objects.filter(status="Blocked", created__lt=<deploy>)`) rather
  than an automated migration.

## 2026-07-28: `test_loopnet.py::FetchTests::test_unconfigured_gateway_gracefully_persists_empty` makes a real outbound connection on this machine

Noted while running the full test suite as a regression check after adding `api_kinds =
frozenset()` to five plugins' `PanelSource` subclasses (an unrelated change - see Part 6 of
`docs/notes/mobile_app_notes.md`). This test's own docstring says it expects `RedataGateway()` to
raise `ValueError` because it's *unconfigured* in the test environment, and only mocks
`LocationCache.set`. On this machine it instead reaches all the way into `requests` and attempts a
real TCP connection to `10.2.0.214:443`, which the test sandbox's `LocalhostOnlyNetwork` guard
correctly blocks (`src/urbanlens/core/testing_network.py`) - so the symptom here is a hard failure
rather than the silent false-negative the test is nominally guarding against.

Root cause confirmed: this checkout's `.env` sets `UL_REDATA_API_URL=https://redata.urbanlens.org`
(a real, working REData instance, `resolve`d to `10.2.0.214` here) plus `UL_REDATA_API_KEY` -
legitimate for this developer's normal workflow against the real service, but Pydantic's
`app.py` settings load `.env` unconditionally, so it also configures `RedataGateway` under
`pytest`/`UL_ENVIRONMENT=test`. The test's own name ("unconfigured gateway") only holds on a
checkout with no REData credentials in `.env` at all; on this one it isn't unconfigured, so
`fetch()` proceeds past the `ValueError` branch straight into a real HTTP call, which the test
sandbox's `LocalhostOnlyNetwork` guard (`src/urbanlens/core/testing_network.py`) then correctly
blocks. Not fixed here - out of scope for the change that surfaced it (`git diff` confirms
`plugins/builtin/loopnet.py`'s only edit was the additive `api_kinds` class attribute, nowhere
near `fetch()` or `RedataGateway`) - but the test should mock/patch the gateway (or settings
should force REData unconfigured under `UL_ENVIRONMENT=test` the way `settings/_gdal_windows.py`
scopes its own local-only behavior) rather than depending on `.env` being REData-credential-free.

## 2026-07-28: `services/consensus/fields.py` - 9 pre-existing `[has-type]` mypy errors

Found while running a full `mypy src/urbanlens/dashboard` sweep as part of the external-API P2
parity-polish pass's Phase 8 prep (Games polish - SpotGuessr/Trivia/Consensus). Not caused by this
pass - nothing in this session touches `services/consensus/fields.py`, and `git log` shows it
predates this branch's work (Consensus was built 2026-07-25, per a separate session).

All nine errors are `Cannot determine type of "<field>"  [has-type]` on lines 315/316 (`name`),
322/323 (`description`), 329/330 (`indoor_outdoor`), 338/339/339 (`pin_type`,
`pin_type_is_user_provided`) - each inside a lambda (`current_value=lambda w: w.name`, etc.) passed
as a keyword argument to `_wiki_field_strategy(...)` while building the `_STRATEGIES` dict. `w` is a
`Wiki` instance and every one of these is an ordinary model field, so this isn't an obviously wrong
runtime assumption the way the boundary/queryset.py and forms/search.py entries above are - it looks
like a mypy inference limitation on the lambda's implicit parameter type when `_wiki_field_strategy`
itself is generic/`Callable`-typed, rather than a real bug. Left uninvestigated because Phase 8 does
not touch `_STRATEGIES` or the field-strategy machinery, only Consensus's session/eligibility/vote
services - fixing this would mean guessing at `_wiki_field_strategy`'s intended generic signature
without the context of whoever wrote it.

## 2026-07-30: `test_media_auth_mixin.py::MediaAuthResolutionTests::test_session_wins_over_a_credential_header` is flaky (PK off-by-one)

Found while running the media/search/public-pins suites for an unrelated PR #126 review-comment pass
(scoping fixes in `external_api/views_search.py`, `controllers/media.py`, `services/pins/public_pins.py`
- this test file was never touched). Fails both in isolation and alongside other files, non-
deterministically off by exactly one: `AssertionError: '17' != '16'` in one run, `'194' != '193'` in
another. The assertion is `self.assertEqual(response.content.decode(), str(self.profile.pk))` -
comparing the profile pk baked in `setUp` against whatever pk the view actually resolved, so either
an extra `Profile`/`User` row is being created somewhere between `setUp` and the assertion (shifting
the auto-increment sequence out from under the hardcoded expectation), or the mixin under test is
genuinely resolving the wrong profile. Needs a session review of `MediaAuthResolutionTests.setUp`
and `resolve_media_profile`/`CredentialOrSessionMediaMixin` to tell which; not investigated further
since it's unrelated to the search/media/public-pins scoping fixes this session was making.

## 2026-07-30: Two Google API keys are HTTP-referrer-restricted but only ever called server-side - every request gets a 403

Found from production logs: `google_images.py`'s `GoogleImageSearchGateway` (`customsearch.googleapis.com`)
and REData's `redata_places_gateway.py` (`places.googleapis.com`) both started failing with
`403 Requests from referer <empty> are blocked. (forbidden)`. Neither gateway sends or fakes a
`Referer` header - `google.py`/`google_images.py` just does a plain `self.session.get(...)`, and
REData's gateway passes its key via the `X-Goog-Api-Key` header (see
`../REData/src/redata/parcels/services/google_places_details/gateway.py:189-193`) - both textbook-
correct for a server-side key. The 403 is Google's API itself rejecting the request, because
whichever key backs `settings.google_domain_restricted_api_key` (UrbanLens) and `RD_GOOGLE_MAPS_API_KEY`
(REData) is configured in Google Cloud Console with an **HTTP referrers (websites)** application
restriction. That restriction type only works for browser-side calls (Maps JS API, embedded widgets)
where a real `Referer` header is present - a Celery worker calling Google's REST API directly never
sends one, so Google always sees `<empty>` and blocks unconditionally, independent of the key being
otherwise valid/enabled/correctly configured in env vars.

Not fixable in code on either side - short of literally fabricating a `Referer` header to spoof a
browser origin, which would be actively wrong to do. The real fix is a Google Cloud Console change:
open each key's Credentials page and change "Application restrictions" from "HTTP referrers" to
"IP addresses" (the production egress IP(s)) or "None", leaving "API restrictions" (which Google
APIs the key may call) untouched. Requires Cloud Console access neither this session nor the
REData session had. Confirm both keys - UrbanLens's Custom Search/Image Search key and REData's
Places API (New) key may or may not be the same underlying Google Cloud project/key.

## (ORIGINAL) 2026-07-30: SearXNG (`search.jmann.me`) image search 403s after coming back up from an outage

Same production log sweep as the entry above. `search.jmann.me` was confirmed down (DNS resolution
failures) earlier the same evening; once the operator brought it back up, `SearxngGateway.search`/
`search_images` started getting `403` instead. `searxng.py` never set a `User-Agent` (defaulting to
`python-requests/x.y`, a common bot-signature block target), so a `User-Agent` header was added as a
cheap, safe mitigation (`searxng.py`'s `_USER_AGENT` constant, set in `__post_init__`). Not confirmed
as the actual cause - equally plausible is the instance's own `limiter.toml` bot-detection or a
fronting WAF/CDN (e.g. Cloudflare) rule that changed when the service was brought back online. If
403s persist after the User-Agent change, check the instance's own access/limiter logs server-side -
this session had no access to `search.jmann.me`'s host.

## 2026-07-31: REData's `/api/v1/parcels/lookup/` is in an OOM/WORKER-TIMEOUT crash loop on chiron

Found while investigating the `resolve_deferred_pin_locations` retry-forever bug below - unrelated
endpoint, noticed in the same gunicorn log sweep on `redata-production-app-1`. Repeated `WORKER
TIMEOUT` followed by `SIGKILL` and worker respawn, i.e. requests to that endpoint are exhausting
memory or wall-clock badly enough for gunicorn's own supervisor to kill the worker. Not
investigated further - REData is a separate codebase/service another agent maintains (per
`CLAUDE.local.md`), and this session only had read access there. Whether this crash loop
contributed to or is independent of the CID-resolution backlog (both endpoints share the same
gunicorn workers, so one starving the other for memory is plausible) was not determined.

## 2026-07-31: Production celery worker's `.env` has `UL_REDATA_API_URL`... but check `UL_SITE_URL=staging.urbanlens.org`

Noticed while inspecting `redata-production-app-1`'s environment (via scoped, non-secret-exposing
`grep` - see below) during the CID-resolution investigation: a variable read off what's supposed to
be the *production* UrbanLens celery worker's environment showed `UL_SITE_URL=staging.urbanlens.org`.
That looks like a copy-paste/deploy-config leftover from a staging `.env`, which would make any
absolute URL the production worker builds (e.g. notification deep-links via `request.build_absolute_uri`
equivalents, `reverse()`-based URLs sent in emails/notifications) point at staging instead of
production. Not confirmed as a real production `.env` (vs. this session misidentifying which
container/host it was inspecting) and not fixed - purely operational (an env var value on the
deployed host, not a code change) and outside this session's remit. Worth a human checking the
actual production `.env` deploy config directly.

## 2026-07-31: `resolve_deferred_pin_locations` retried every 120s forever against a REData cid stuck behind its own 30-day cache floor - fixed

Root cause of a production incident: importing `sample_data/Google Takeout.csv` deferred ~700 cids
to REData's `POST /places/resolve-cids/` for resolution. REData's `StaggeredCachePolicy`
(`core.services.staggered_cache.py`, `min_ttl_hours=720` i.e. 30 days by default) has a hard floor -
`should_refresh()` returns `False` unconditionally for any row younger than `min_ttl_hours`,
regardless of quota utilization. `needs_refresh(place)` is just `should_refresh(place.last_checked_at)`,
so the instant a `GooglePlace` row gets checked even once (`last_checked_at` stamped) without reaching
the 3-attempt `confirmed_no_location` terminal state, REData will not queue another resolution attempt
for it for weeks - but keeps reporting it as `pending` (HTTP 200, no error) on every subsequent
`resolve-cids` call, since it's neither `resolved` nor `confirmed_no_location`. Confirmed via direct
DB query on chiron: 441 of 723 `GooglePlace` rows stuck `resolved=False, confirmed_no_location=False`
with zero `last_checked_at` activity for 10+ hours.

UrbanLens's `resolve_deferred_pin_locations` (`dashboard/tasks.py`) treated "still pending, REData
responded fine" as forward progress and retried every 120s with `max_retries=None` and no ceiling -
the existing `consecutive_request_failures` cap (added in an earlier pass, commit `e7a10584`) only
covers whole-batch *request* failures, not "REData responded successfully but nothing moved." Fixed
by adding a second, independent `consecutive_no_progress` counter/cap (`_MAX_CONSECUTIVE_NO_PROGRESS_RETRIES`)
that only increments when a retry's `result.pending` is the exact same size as the batch it was given
(i.e. zero cids resolved that round) and `request_failed` is `False`; resets to 0 the moment any cid
resolves or is confirmed unresolvable, so a batch still genuinely working through REData's queue is
never cut off early. See tests in
`dashboard/tests/hypothesis/test_resolve_deferred_pin_locations_no_progress.py`.

### Follow-up (same day): REData's active-request fallback - first attempt was wrong, corrected after live testing

The user separately asked REData to stop leaving an *actively-requested* cid stuck behind its own
30-day staggered floor for weeks - fine for a background prewarm sweep to wait that long, not fine
for a live caller blocked on `resolve-cids`'s response right now. First attempt (this session, same
day): a `GoogleLegacyCidLookupGateway` calling the legacy Place Details endpoint with
`place_id=cid:{cid}` - a real, if undocumented, convention for passing a bare Maps CID that this
session had reason to believe still worked. Shipped with full unit-test coverage (mocked HTTP) but
**never verified against a live Google API before being reported done** - a real gap, caught directly
by the user rebooting both services with the new code and pasting production logs showing every
single lookup failing with `INVALID_REQUEST`.

Live testing on both REData's and UrbanLens's real production API keys (REData's `diagnose_places_api`-
style probing plus UrbanLens's own pre-existing `manage.py diagnose_places_api` command, run live on
jungu) confirmed this decisively: `place_id=cid:{cid}` fails with `INVALID_REQUEST`/"Invalid 'placeid'
parameter"; the older bare `?cid=NUMBER` form (what UrbanLens's `GoogleGeocodingGateway.get_coordinates_by_cid`
used **before** REData's scrape-based resolution existed - confirming the user's recollection that this
used to work) now fails with `NOT_FOUND`/"The provided Place ID is no longer valid. Please refresh cached
Place IDs..." - Google's own wording for a real, external deprecation of old-style Place ID acceptance,
not a request-shape bug either agent could fix. Both UrbanLens's dormant fallback and REData's new one
were affected by the same dead mechanism; UrbanLens's had simply never been exercised in production
recently enough for anyone to notice.

**Corrected fix (REData)**: no working faster official API exists for a bare, never-before-resolved
CID - Places API (New) has no CID lookup at all. Replaced the dead paid-API gateway with a bounded,
forced *synchronous* run of the same real headless-Chromium scrape the async Celery path already uses
(`google_places.lookup.resolve_synchronously_for_active_request`), called directly from
`ResolvePlaceCidsView.post()` for a cid stuck behind `needs_refresh`. Capped at exactly 1 forced scrape
per request (`_MAX_FORCED_SCRAPES_PER_REQUEST`) with an 8-second timeout
(`_ACTIVE_REQUEST_TIMEOUT_MS`, vs. the background path's 20s default) - REData's gunicorn config
(`gunicorn.conf.py`) sets no explicit `timeout`, so its 30s default applies, and that browser
navigation is fully synchronous (not gevent-cooperative), meaning it blocks the *entire* worker
process, not just one request, for its duration; exceeding 30s would SIGKILL the worker mid-response,
dropping the whole batch rather than just leaving one cid pending. 1 call at an 8s cap leaves
comfortable headroom even in the worst realistic case. Removed the dead `GoogleLegacyCidLookupGateway`
and its `google_places_legacy_cid_lookup` rate-limit entry entirely rather than leaving unreachable
code behind.

## CRIS media on a multi-building campus is still only partial coverage (2026-08-05)

Fixed this session (see `plugins/builtin/cris_buildings.py`): the CRIS Media gallery was
returning nothing at all because `RedataGateway.fetch_cultural_resource_detail` handed back
REData's `{"detail_status", "resource"}` envelope while every caller read `attributes`/
`attachments` off it, plus three narrower mismatches (attachment `kind` compared as
`"PHOTO"`/`"DOCUMENT"` against REData's lowercase values, `resource_type` compared as
`"district"` against REData's `"building_district"`, and the *first* building of a lookup
being taken rather than the nearest one).

**Still outstanding**: a parcel-scope pin only aggregates the media of the single nearest
building plus the site-level record. CRIS's own authoritative "every building on this site"
list is a SURVEY resource's `USNs` roster - REData surfaces it via a resource's
`linked_resources`, and its own docs cite survey `12SD00541` as covering all 124 buildings of
the former Hudson River State Hospital campus. Following that roster (and using REData's bulk
`POST /cultural-resources/fetch-details/?lat=&lng=`, which UrbanLens's gateway does not
implement at all, to warm them within one provider's rate budget) is what would give a campus
pin the complete set. Deliberately out of scope of the bug fix: it needs a per-resource
fan-out with its own paging/rate story, not another field-name correction.

**Also worth checking operationally**: `fetch-detail/`, the bulk variant, and
`attachments/{id}/extract/` all require an API key holding `cultural_resources:write`, not
just `:read` - a read-only key 403s on all three and therefore yields zero attachments no
matter how correct this code is.

## `bun run build` fails as a package script on some hosts (2026-08-06)

Not a project bug, and not reproducible where it matters - but it costs an afternoon to
rediscover, so: on a host with Bun installed via `curl -fsSL https://bun.sh/install | bash`,
running the frontend build **through the package script** fails with

```
TypeError: Formats besides 'esm' are not implemented
```

...while the exact same build succeeds when the script file is invoked directly:

```bash
bun run bin/build-frontend.ts        # works
bun run build                        # fails
docker exec -w /app <app-container> bun run build   # works
```

Both Bun installs report 1.3.14, and the container (`oven/bun:1`) runs the identical script
happily - so this is something about how that particular Bun build executes a package.json
script, not about `bin/build-frontend.ts` or the `entries-classic` IIFE group it dies on.
Rewriting the script to use `Bun.build({format: "iife"})` instead of shelling out to
`bun build --format iife` does *not* help: the JS API accepts the format fine in a standalone
probe and still throws inside the package-script context, which is what rules the script
itself out as the cause.

**If you hit this, invoke the file directly or build in the container.** Do not "fix" the
build script - it is not what is broken.

## Codebase audit (2026-08-06, module 1: core infrastructure) - findings & fixes

- **`ApiCallLog` grew forever** - `prune_older_than_days` was written and documented as
  the trim mechanism, but nothing ever called it, while every external API call runs three
  rate-limit COUNTs over the table plus an INSERT. Now pruned daily
  (`tasks.prune_api_call_logs`). Retention is 400 days, set by the table's *longest reader*:
  the public costs page reconstructs a 12-month API-spend chart from these rows, so the
  model helper's 90-day default would have silently zeroed three-quarters of that chart.
  If you add a longer-window consumer of ApiCallLog, raise the retention with it.
- **Slug retry loops retried on any unique violation** - `PublicDashboardModel.save()`/
  `regenerate_slug()` matched any "duplicate key" IntegrityError, so a violation of an
  unrelated constraint (e.g. Pin's one-pin-per-location-per-profile) burned 20 slug
  regenerations and could mask the real conflict behind a uuid-suffixed slug. Now only a
  violation naming a *slug* constraint retries; this makes "slug constraints have 'slug'
  in their name" a load-bearing naming rule (verified: db_pin_unique_slug_per_profile,
  uq_pin_list_profile_slug, uq_album_pin_slug/uq_album_wiki_slug, and the field-level
  uniques on Location/Profile all qualify).
- **Removed `abstract.Serializer`** - a custom DRF base with context-driven
  include/exclude-fields machinery that no serializer ever subclassed (every real one uses
  `serializers.ModelSerializer` directly). Unadopted abstraction; DRF's documented dynamic-
  fields recipe covers the need if it ever materializes.

Still open (bigger than this pass): `check_rate_limit`'s three COUNTs run per external
call; fine while windows are indexed and the table is pruned, but a hot service could
justify a cached counter. `EmailSendLog` is unbounded too - low volume (user-triggered
email), so left alone deliberately.

## Codebase audit (2026-08-06, module 2: pin/location/place) - findings & fixes

- **`PinQuerySet.overlapping()` was O(n²) intersects plus 3-5 queries per pin** - and it is
  user-reachable (the `overlapping_pins` saved-filter criterion), so a big collection turned
  each map filter application into thousands of queries. Now select_relates every relation
  `resolve_for_pin`'s fallback chain touches and sweeps sorted x-extents so only genuinely
  bbox-overlapping pairs run real geometry. Still open: `Boundary.resolve_for_pin` itself is
  per-pin (own row -> wiki row -> place polygon); a batched resolver using the existing
  `rows_by_pin_id` would cut the remaining per-pin queries but changes a subtle fallback
  chain - do it with care, with `map_pin_share_detection.py` as the pattern.
- **`WikiCreationService` lived in `services/locations/creation.py`** - wiki creation filed
  under locations, while every other wiki service lives in `services/wiki/`. Moved to
  `services/wiki/wiki_creation.py`; all imports/docs updated.
- **Stale TODO removed** - `Location.display_name` carried "TODO: assess for deletion" while
  being load-bearing in tasks, comments, and the map controller.

Noted, deliberately not changed: `Location.__setattr__` special-cases a duck-typed
`google_place` for unit tests - test scaffolding in production code; removing it means
fixing several tests to build real GooglePlace rows. `enrichment.py`/`external_data.py`
carry seven "TODO: Catch specific exceptions" blanket handlers - each is a deliberate
"never let one source break the cycle" guard, so narrowing them is safe only with
per-source failure tests.

## Codebase audit (2026-08-06, module 3: tasks/consumers/celery) - findings & fixes

- **Redis visibility_timeout was the default 3600s with acks_late on** - exactly equal to
  both the hard CELERY_TASK_TIME_LIMIT and the longest countdown= this app schedules
  (import/export cleanup 3600s, check-in archival grace 1h). At that boundary a
  legitimately long task, or a countdown sitting unacked in a worker, is redelivered and
  runs twice. Now pinned to 2h via CELERY_BROKER_TRANSPORT_OPTIONS; keep it above
  max(time_limit, longest countdown) if either grows.
- **Two raw `.delay()` calls bypassed `safely_enqueue_task`** (DM address-mention
  detection inside an on_commit callback, and fact-confidence recompute after evidence
  recording). Both fire after their row is durably saved, so a broker outage turned an
  already-successful write into a 500. Routed through the guarded helper; the facts test
  that asserted on `.delay` re-pointed to the enqueue seam.

Verified clean elsewhere: every signal connect carries dispatch_uid; every
`.objects.get(pk=)` in tasks.py is DoesNotExist-guarded; no @shared_task lives outside
tasks.py; consumers never touch the ORM outside database_sync_to_async; the beat schedule
references no missing task, and no task is unreferenced. Queue split (default "celery" +
"panel_fetch") matches the two workers' -Q flags.

Also: the pre-audit full suite completed 10018 passed / 1 failed, the one failure being
the facts test above mid-run (the run's snapshot was taken before that fix) - i.e. the
tree entering this audit was fully green.

## Codebase audit (2026-08-06, module 4: plugins/API gateways) - findings & fixes

- **Digital Commonwealth was registered everywhere except where it mattered** - the
  rate limiter and site-admin category map both knew the service, REData has fronted it
  all along, and a direct-API `DigitalCommonwealthGateway` sat fully dead (never called by
  any plugin) - so Massachusetts pins simply lacked the archive. Wired the missing half:
  `DigitalCommonwealthMediaProvider` (REData `reference-documents/search/`, MA-gated,
  same query-shape flags as its LOC/IA siblings), a `DigitalCommonwealthPlugin`, gallery
  loaders on both pin and wiki pages, and an `ImageSource` value so wiki-sends keep
  attribution. Deleted the dead direct gateway. 43 plugins now discovered.
- **The admin "Search provider" picker was a dead control** - web search moved wholesale
  to REData's own provider chain, so `SiteSettings.search_provider` was written by the
  admin form and read by nothing; an admin changing it changed nothing. Removed the
  picker, the setting (migration 0037), `SearchProviderChoice`, and the never-read
  `mojeek_api_key`/`marginalia_api_key` env settings from the same era.
- **Orphan rate-limit registry entries removed** - `google_search` and `news` had no
  gateway, plugin, or caller anywhere; they only rendered as permanently-zero rows in the
  admin API-usage report.
- **Removed `StaticBoundaryProvider`** - its docstring said "kept for tests and explicit
  callers"; there were neither.

## Codebase audit (2026-08-06, module 5: media/images pipeline) - findings & fixes

- **Eight redundant uuid indexes dropped** (migration 0038) - Pin, Image, Wiki, Trip,
  Label, Comment, PinVisit, SafetyCheckin each declared `Index(fields=["uuid"])` while
  `FrontendDashboardModel.uuid` is already `unique=True`, and a unique constraint *is* a
  btree index in Postgres. Every insert/update on some of the hottest tables maintained a
  second identical index for nothing. If you add a new FrontendDashboardModel, don't
  re-add one.
- **Map-overlay uploads bypassed the upload pipeline** - the overlay controller (added
  earlier this week) did a raw `Image.objects.create`, skipping the quota check and its
  per-profile lock, checksum dedupe, `file_size` (so an overlay sheet never counted
  against quota), and the async EXIF/keyword ingestion. Now goes through the canonical
  `services.photos.photo_upload.upload_photo`. Anything creating an Image from a *user
  upload* must use that service; raw creates are for server-fetched bytes only
  (materialize sets its own size/checksum).

Verified clean: visible_to's per-uploader relationship checks are bounded by the
gallery's distinct uploaders (documented tradeoff); every other Image-creating path sets
file_size; media_source_key/media_item_key lookups are indexed; storage sums use the
single-pass filtered-aggregate helper.

## Codebase audit (2026-08-06, module 6: wiki/community) - findings & fixes

- **Wiki pages had no way up to their parent** - wikis nest themselves (a building's wiki
  becomes a child of the campus's, a documented feature), and `wiki_access.visible_parent_wiki`
  was written specifically to make that link safe, but nothing ever called it: no template
  rendered a parent breadcrumb, so a viewer landing on a nested building wiki was stuck
  there. Wired it into the wiki hero beside "Back to my pin".

  The gating is the point and is now covered by tests: within one access domain (a building
  `PART_OF` its parcel) the parent is always reachable, but across a `MEMBER_OF` edge - a
  campus earned only by holding *every* member parcel - the parent's **name must not reach
  the response at all**, since a breadcrumb to a page that would 404 confirms a place the
  viewer hasn't earned exists. The new test asserts absence from the response body, not just
  absence of a link.

Verified clean: every wiki-scoped controller and external-API view resolves through
`resolve_visible_wiki` (the two `location_slug` users that don't - `maps.py` building a URL,
`pin_edit.py` relinking a pin to a Location - aren't wiki access). No dead functions in
services/wiki, services/comments, or services/consensus once helpers used within their own
module are accounted for.

Left alone deliberately: `place_visible_to` is public but referenced only by tests - it is a
coherent part of the access module's public surface (the place-level twin of
`location_visible_to`), so it reads as API completeness rather than dead weight.

## Codebase audit (2026-08-06, module 7: messaging/e2ee/social) - findings & fixes

- **Notification dropdown was a silent N+1** - `notification_item.html` reads the
  `pin_share`/`visit_suggestion` reverse OneToOnes to decide whether to offer Accept/Decline
  (a shared pin) or the three-way merge choice (a suggested visit), but all six call sites
  selected only `source_profile`. Twenty rows therefore cost up to forty extra queries on
  nearly every page load. The miss is *invisible* by construction: an absent reverse
  OneToOne raises `ObjectDoesNotExist`, which Django templates swallow - so nothing breaks,
  it just gets slow. Added `NotificationQuerySet.for_display()` and routed every
  dropdown-rendering call site through it, so the relation set lives in one place as the
  template grows. `notification_center` (external API) deliberately keeps its narrower
  select: its serializer never reads those relations.

  Worth recording how this was nearly mis-diagnosed: `NotificationLog` has no `pin_share`
  field, so a field-list search says the template references something that doesn't exist,
  which reads as "these buttons never render". They do - the names are the `related_name`s
  of OneToOneFields declared on `PinShare`/`VisitSuggestion`. A reverse accessor is easy to
  miss from the model's own side; check both directions before concluding a template branch
  is dead.

Verified clean: every `PinShare.objects.create` path (five of them - direct share, re-share,
DM address detection, map share, trip share) resolves lineage and calls
`record_share_exposure`, so the `LocationExposure` chain CLAUDE.md calls out is intact.
Native push is fully wired (notification post_save -> on_commit -> `dispatch_native_push`).
Comment mention-gating holds on every surface, including the external API, which serializes
raw text only for comments that already passed the `VisibleComment` gate. No dead functions
in services/messaging, services/social, services/sharing, or services/notifications.

## Codebase audit (2026-08-06, module 8: auth/billing/subscriptions/security) - findings & fixes

- **A redelivered Stripe payment could be credited twice (money bug).** `invoice.payment_succeeded`
  is the one handler with a non-idempotent side effect: `banking.apply_payment` *increments*
  `total_paid_cents`, which drives `granting_access_for` (i.e. how long pay-what-you-want access
  stays granted). `StripeWebhookView` recorded the event, handled it, then marked it processed -
  three separate autocommits. Two ways that double-credits, both of which Stripe's retry policy
  makes routine rather than theoretical (it redelivers on any non-2xx **or timeout**):
  a delivery that applied the credit but died before writing `processed_at` gets retried and
  credits again; and two deliveries arriving at once both read `processed_at` as null and both
  run the side effects. Handling and marking-as-handled now commit together inside one
  `transaction.atomic()`, entered after re-reading the event row `select_for_update()` so
  concurrent duplicates of the same event serialise on it. The audit row is still inserted in its
  own prior transaction, so a delivery whose handler raises leaves its raw payload behind to debug
  from. Regression tests cover both paths, including a handler-succeeds-then-marking-fails
  delivery followed by a retry.

- **TOTP replay protection was a read-modify-write.** `verify_totp_code` read `last_used_step`,
  compared, then wrote the new step unconditionally - so two submissions of one intercepted code
  (a phishing proxy replaying it into a parallel session) could both pass the comparison before
  either wrote, which is the exact thing tracking the step exists to stop. The step is now claimed
  in the same statement that checks it (`filter(last_used_step__lt=step).update(...)`, verdict from
  the matched-row count), so Postgres serialises it and exactly one caller wins.

- **The SiteSettings singleton was re-fetched several times per page.** `get_current()` is called
  from ~80 places, and each was its own `get_or_create(pk=1)` round-trip: three separate context
  processors fetch it on every request, then the controller fetches it again, then every
  `user_has_feature()` check fetches it once more. Added a request-scoped memo
  (`models/site_settings/request_cache.py`), armed by `request_started` and torn down by
  `request_finished`. Deliberately **not** a process-wide or TTL cache: long-lived Celery workers
  would pin a settings row for the life of the worker, and the test suite mutates settings through
  `queryset.update()`, which bypasses `save()` and so cannot invalidate anything. Anywhere without
  a request never arms the memo and reads through exactly as before. `post_save` clears it so an
  admin editing settings sees their own change for the rest of that request.

Verified clean: the Stripe webhook does verify signatures (`stripe.Webhook.construct_event` against
`UL_STRIPE_WEBHOOK_SECRET`, 503 when unset) and is the codebase's only CSRF-exempt endpoint, for a
documented reason. All 22 API scopes are enforced by at least one view; `ExternalApiView`'s
fail-closed default genuinely holds (`credential_grants` returns False for a null credential or an
empty requirement, and `OAUTH2_ONLY_SCOPES` is refused to PAT-style keys regardless of the stored
grant); `UnscopedExternalApiView` is a documented, deliberate exception. API-key auth hashes with
Django's password hashers and looks up by indexed public prefix, so it neither iterates every hash
nor leaks by timing. Login 2FA is lockout-rate-limited. `services/billing/{banking,pricing,
stripe_client}` and `services/security/{redact,malware_scan}` are all wired, with no dead functions
in `services/auth`, `services/billing`, or `services/security`.

Not a gap, though it looks like one: `controllers/comments.py` passes `skip_malware_scan=True`.
The scan is deferred to a Celery task instead, with the comment held `pending_scan` (hidden from
every other viewer) until it clears - a slow/occasionally-unavailable clamd used to fail whole
comment submissions.

## Codebase audit (2026-08-06, module 9: games, trips, safety, memories) - findings & fixes

- **One bad safety check-in could silently suppress everyone else's escalation.** The three
  safety beat tasks (`send_due_checkin_reminders`, `send_final_checkin_warnings`,
  `escalate_overdue_checkins`) each looped a queryset calling a per-check-in service with no
  per-item guard, so any exception aborted the whole run. `SafetyCheckin` has a deterministic
  `ordering`, so a row that fails repeatably - corrupt contact data, an address the mail backend
  rejects, a template that won't render - fails at the same position on *every* tick, and every
  check-in behind it never escalates. The failure is silent and unbounded: the sweep just returns
  early, and the people whose emergency contacts were never called have no way to know. This is
  the most consequential loop in the app to leave unguarded, and the fix was already the
  file's own convention - `sweep_due_safety_checkin_archival` immediately below, and all four
  game/trivia/consensus stall sweeps, already isolate per item. These three were simply missed.
  `escalate_checkin` is per-contact idempotent (`notified_at__isnull=True`), so retrying a failed
  check-in on the next tick reaches only the contacts the failed attempt never got to.

- **Same shape in two billing sweeps**, fixed while here. `advance_pwyw_usage_ledgers` had no
  guard at all - and it is the only thing counting a canceled subscription's banked balance down,
  so one bad row froze every other user's ledger. `sync_stripe_subscriptions` guarded the Stripe
  *fetch* but not the *apply*, and `sync_from_stripe_subscription` indexes `items.data[0]`, so a
  subscription in an unexpected shape aborted the sweep for everyone after it.

- **The Memories feed re-queried up to three times per trip.** `_trips_for_range` annotated
  `_eff_start`/`_eff_end` to filter on, then discarded them and re-derived the same two dates
  through `Trip.effective_start_date`/`effective_end_date` (a query apiece for any trip without
  explicit dates), then called `_trip_representative_point`, which ran a third. Now the
  annotations are used directly and activities arrive via a `Prefetch` carrying the same
  `select_related`/ordering the point helper wants. Pinned with a test asserting the query count
  is flat from one trip to six.

- **The same code filtered and displayed trips by different definitions of "ends".** That
  annotation took `Max(activities__scheduled_at)` while `effective_end_date` takes the later of
  `scheduled_at` and `scheduled_end`. A trip whose final activity *runs past* the last start time
  fell in the gap: filtered out of a window it visibly overlaps. The annotation now uses
  `Greatest(Max(scheduled_at), Max(scheduled_end))` (Postgres's `GREATEST` ignores NULLs, so an
  activity with no end doesn't erase the max), so the two agree by construction.

- **Wired up `trip_name_suggestions`**, written for the create-trip dialog's placeholder rotation
  and never called. The dialog carried a hand-maintained 12-name JS array instead - a second list
  that would drift from the generator that actually names blank-submitted trips. Now exposed
  through a `trip_name_ideas` template tag (matching the existing `subscription_role_choices`
  precedent, since the dialog is included from two pages) and handed to JS via `json_script`.

Verified clean: a definition-level sweep for unwired public functions across all four subsystems
came back empty apart from the above - calendar sync, trip AI suggestions, and the trip signal
receivers are all properly connected (`@receiver` plus the `apps.py` import). Games access control
is systematic: every session-scoped SpotGuessr view resolves through `_participant_session`, which
404s rather than 403s so a non-participant can't confirm a session id exists, and host-only actions
(`begin_session`, `end_session_now`) check `host_profile_id` in the service layer rather than the
view. The memories aggregator's other three sources already `select_related` correctly.

Note on method: an early dead-code sweep that excluded each function's whole defining file produced
17 false positives (`clamp_rounds`, `can_delete_comment`, `invite_members` and more are all called
by their own module's public entry points). Excluding only the `def` line itself cut it to three.

## Codebase audit (2026-08-06, module 10: frontend TS, templates, SCSS) - findings & fixes

- **A multi-line `{# #}` comment was rendering to users on the wiki page.** Django's lexer matches
  comments with a non-DOTALL `{#.*?#}`, so a `{#` whose `#}` sits on a later line is never
  tokenised as a comment at all - it falls through as ordinary text and prints verbatim in the
  middle of the page. `pages/location/wiki.html` had one, three lines of internal notes about
  pin-count truncation, rendered right under the "First pinned" stat. Nothing fails loudly: the
  page still returns 200 and the markup around it is fine, which is exactly why it survived.
  Converted to `{% comment %}`, and added `test_template_comment_lint.py` - a `SimpleTestCase`
  (no DB, runs in under two seconds) that scans all 416 templates for the same shape, so the
  rule CLAUDE.md already states is now enforced rather than remembered.

- **The pin cache's version and key were hardcoded on both sides of a cross-language contract.**
  `pages/map/index.html`'s inline script is the only writer; `pin-cache.ts` is the reader; the
  version (`v: 8`, `c.v !== 8`) and the key (`ul_pins_v5_${uuid}`) were spelled out independently
  in each. A one-sided bump makes every read return `[]` - no error, the features built on it just
  go quiet. This is not hypothetical: the module's own comment records that it already happened
  (the reader sat on v6 after the writer moved on). Exported `PIN_CACHE_VERSION`/`pinCacheKey`
  and added `pin-cache.contract.test.ts`, which parses the template and fails when the two sides
  disagree. Confirmed it genuinely fails on drift by bumping one side before committing.

- **Deleted `shared/parent-search.ts`** - dead on three independent counts: no entrypoint imports
  it (so it is never bundled and the `window.filterParentOptions` global it installs never
  exists), no template calls that global, and the `.tag-parent-option` class it queries appears
  nowhere but inside the file itself. It is a superseded client-side filter; the live UI does
  server-side search against `/map/pins/parent-search/`.

Verified clean: a reachability walk from the 14 real entrypoints found only that one orphan plus
`tools/generate-e2ee-fixture.ts` (a dev tool, legitimately run by hand). Every `|add:` on an id in
all 416 templates already goes through `|stringformat:"s"` - the documented `''`-collapse gotcha
has been fixed thoroughly. No orphaned SCSS partials. No other template/JS data duplication of the
kind the trip-name array was: every hardcoded JS array in a template is an empty accumulator, and
the one enum-shaped list (`_TRIP_TABS`) is a set of DOM tab names, not a mirror of a server enum.
`tsc --noEmit` is clean and all 150 bun tests pass.

Pre-existing, not fixed here: `bun run build` fails at `Formats besides 'esm' are not implemented`
when it reaches the `entries-classic` (iife) step, with the installed Bun. Confirmed identical at
HEAD, so it is not a regression from this work. Note it partially writes output before failing -
it deleted `core.js`/`e2ee.js`/`permissions.js`/`webauthn.js` from the tracked static output and
rewrote the esm bundles. Anyone running it needs to `git checkout` the static js directory
afterwards; the tracked build artifacts should not be committed from a run that aborts halfway.

## Codebase audit (2026-08-06, module 11: settings, urls, labels/saved-filter/search/import-export) - findings & fixes

- **`UL_EMAIL_TLS=true` silently disabled STARTTLS.** `base.py` parsed it as
  `os.getenv("UL_EMAIL_TLS", "True") == "True"` - a literal string comparison, so every spelling
  but exactly `True` evaluated to False. The same variable is *also* declared in `app.py` as a
  pydantic `bool`, which accepts `true`/`1`/`yes`/`on`, so the two readers of one variable
  disagreed - and the disagreement resolved toward sending SMTP credentials and every outbound
  mail in plaintext, while the operator's env file plainly said TLS was on. Nothing surfaces
  that: mail still sends. `.env-sample` ships `True`, which is why it went unnoticed. Added
  `settings/_env.py:env_bool()` (accepts the spellings people actually write, and falls back to
  the *default* rather than False on an unrecognised value, since False is the unsafe direction
  here) and routed both `EMAIL_USE_TLS` and `EMAIL_USE_SSL` through it.

- **Removed three env vars from `.env-sample` that no code reads**: `UL_DPLA_API_KEY`,
  `UL_US_CENSUS_API_KEY`, and `UL_CLOUDFLARE_WORKER_AI_API_KEY`. An operator setting these gets
  silence - the census gateway (`census_tigerweb`) is keyless, DPLA is an unbuilt roadmap
  candidate (it stays documented in `docs/ROADMAP.md`, which is where a not-yet-built integration
  belongs), and the Cloudflare one is a near-miss for the real `UL_CLOUDFLARE_AI_API_KEY` /
  `UL_CLOUDFLARE_WORKER_AI_ENDPOINT` pair, both of which do exist and are untouched.

- **Two URL patterns share the name `404`** (`UrbanLens/urls.py:113` and
  `dashboard/urls.py:1997`), both in the root namespace, so `reverse("404")` is ambiguous and
  resolves to whichever registered last. Left as-is deliberately: nothing reverses that name, and
  both catch-alls are correctly *scoped* (the dashboard one only sees `/dashboard/*`, since the
  app is included under that prefix), so this is a naming wart rather than a routing bug. Noted
  here so the next person who greps for it doesn't re-derive the analysis.

- **`admin_username` / `admin_email` settings are read by nothing.** They export as
  `ADMIN_USERNAME`/`ADMIN_EMAIL`, which are not Django settings and which no code consults. Left
  in place (removing pydantic fields is a crash-loop risk class per CLAUDE.local.md, for no
  functional gain) but recorded as dead configuration.

Verified clean: of 89 pydantic settings fields, only those two are genuinely unread - the other 14
that a naive grep flags are consumed through `app.py`'s uppercase auto-export into Django settings
(`ROOT_URLCONF`, `TIME_ZONE`, `STATIC_URL`, ...), which a lowercase search misses. Route names are
otherwise collision-free across namespaces (the only other same-namespace duplicates are DRF's own
format-suffix pairs). No dead code in `models/labels`, `models/saved_filter`, `services/search`, or
`services/import_export` - the four label signal receivers a definition-level sweep flags are all
`@receiver`-decorated with `dispatch_uid` and connected on import. The export path is thoroughly
prefetched: every `.all()` iteration over an M2M has a matching `prefetch_related` on the queryset
feeding it. The import path bounds decompression against zip bombs. `saved_filter_cache` keys on a
fingerprint of `updated` timestamps, so entries self-invalidate rather than needing explicit
invalidation hooks.

Corrected mid-audit: I initially read `_queryset_for_kind` (labels controller) as an N+1, since it
calls `.with_pin_counts()` where the organize page calls `.with_hierarchy()`, and the shared card
template reads `label.parents.all`. It is not - `with_pin_counts()` prefetches `parents` and
`children` itself. Worth recording because the shape (two call sites, one prefetch helper) is
exactly the module 7 notification bug, and here it turned out fine.

---

## Codebase audit (2026-08-06): consolidated summary of modules 1-11

Eleven modules, one pass each, module-by-module across the whole codebase. 26 distinct defects
fixed. Each module's own section above has the detail; this is the shape of what was found.

**The four that would have hurt most in production**, all silent-failure bugs - the system keeps
returning 200s and the damage is invisible until someone goes looking:

1. *One bad safety check-in could suppress everyone else's escalation* (module 9). Three beat
   tasks looped without per-item guards over a deterministically-ordered queryset, so a single
   repeatably-failing row starved every check-in behind it, on every tick, forever. Emergency
   contacts silently never called.
2. *A redelivered Stripe payment could be credited twice* (module 8). Handling and
   marking-as-handled were separate commits, and the credit is an increment. Stripe retries on
   timeouts, so this was routine, not theoretical.
3. *`UL_EMAIL_TLS=true` silently disabled STARTTLS* (module 11). A literal `== "True"` compare
   against a variable that pydantic elsewhere parses as a bool - SMTP credentials and all mail in
   plaintext while the env file said otherwise.
4. *Redis `visibility_timeout` left at 3600s with `acks_late` on* (module 3). Any task running
   over an hour gets redelivered and runs twice, concurrently.

**Recurring patterns, worth internalising more than any single fix:**

- *Two call sites, one fix.* Repeatedly a helper existed and was used correctly in one place and
  missed in another: notification prefetch (7), safety sweep isolation (9, where the file's own
  archival sweep three functions below already did it right), billing sweeps (9). When fixing a
  N+1 or adding a guard, grep for every caller of the same template/service - the second one is
  usually there.
- *Read-modify-write where the DB could do it atomically.* TOTP replay steps, webhook
  idempotency, storage quota, `get_nearby_or_create` - each a check-then-act that two concurrent
  requests both pass.
- *Duplicated constants across a language boundary.* The pin-cache version (10), the trip-name
  list (9), `EMAIL_TLS` vs `EMAIL_USE_TLS` (11). None of these can be caught by types; they need
  a test that reads both sides, which is now what guards the pin cache.
- *Silent-by-construction failures.* An absent reverse OneToOne that Django templates swallow
  (7), a cache-version mismatch that just returns `[]` (10), a sweep that returns early (9). The
  common thread: no exception, no log, no user-visible error - only a test asserting the
  *positive* behaviour catches them.

**On method.** Two things repeatedly produced false leads and are worth flagging for whoever
audits next. First, definition-level "dead code" sweeps: excluding a function's whole defining
file yielded 17 false positives in module 9 alone (functions called by their own module's public
entry points), and `@receiver`-decorated signal handlers look unreferenced no matter how you grep.
Every "dead" finding here was confirmed by hand before deletion, and three suspected N+1s and one
suspected dead template branch turned out to be fine on inspection - those are recorded too, since
a wrong conclusion re-derived later costs as much as the original bug. Second, this environment's
test suite: running against the dev compose stack trips the localhost-only network guard for every
page-rendering test, so raw failure counts are meaningless. Every module's suspicious failures
were re-run against a HEAD baseline before being attributed; in each case (166, 88, 19) the
baseline was identical and the changes were clean.

**Known-broken, not fixed here:** `bun run build` fails at its `entries-classic` (iife) step with
the installed Bun, and deletes tracked static output before failing (module 10).

## Second pass (2026-08-06): concurrency & data-integrity races - findings & fixes

The first pass kept turning up one shape - check-then-act that two concurrent callers both pass
(TOTP replay steps, the Stripe webhook marker, storage quota, `get_nearby_or_create`). This pass
went looking for the ones it missed, prioritising money, quotas, access grants and safety.

- **Consensus tentative answers were accumulated without a lock.** `record_tentative_answers` is
  find-then-bump-or-create, and two rounds resolving at once for the same wiki is ordinary, not
  exotic - separate sessions play the same popular wiki concurrently. A threaded test reproduces
  it exactly: both reads miss, both insert, and one dies with
  `IntegrityError: duplicate key value violates unique constraint "db_consensus_tentative_unique"`,
  taking round resolution down with it and losing the players' answers. No unique constraint can
  cover this on its own - the coordinate branch dedups by *proximity*, which no constraint can
  express, so there the same race is silent instead: two rows for one location with support split
  across them, meaning a value the community actually agreed on never reaches the promotion
  threshold. That is the feature failing at its whole purpose, quietly. Fixed by locking the
  parent wiki for the upsert, which covers both branches, makes the row-level `+=` safe, and only
  ever contends with another resolution for the same wiki.

- **E2EE key reset applied rewraps computed against a superseded key.** The endpoint read the
  bundle, had the client rewrap every conversation and group envelope against *that* public key,
  and only then opened its transaction - the docstring's "one atomic transaction, no partial
  state" was true of the writes but not of the read they depended on. A second reset landing in
  between (a double-submitted or retried request - the endpoint is slow, so this is the realistic
  path) meant the second request applied its rewraps on top and overwrote the version. Confirmed
  at HEAD: the test drives a competing bump to v6 mid-flight and the request still returns 200,
  logging `now v2` - it clobbers the newer bundle and seals conversations to a key the bundle no
  longer advertises. Those threads are then undecryptable, and there is no recovery path. Fixed
  by re-reading the bundle `select_for_update()` inside the transaction and returning 409 if the
  version moved; the client restarts against the current key. The request has to lose cleanly
  rather than half-succeed.

Verified clean: `services/consensus/points.py` already locks its `ConsensusProfile` row correctly,
and the trivia/consensus/spotguessr round-completion checks all operate on a `locked_round`. The
E2EE *enrollment* check-then-act (`exists()` then `create()`) is backstopped by `profile` being a
`OneToOneField`, so a concurrent double-enroll fails loudly with an IntegrityError rather than
producing two bundles.

Recorded, not fixed: a family of count-then-create limit checks (`max_partners`, `max_upcoming`
trips, `max_members`, `max_activities`) can each be exceeded by one or two under concurrent
requests. Real, but bounded and self-correcting - a user ends up slightly over a soft cap, with no
corruption and nothing unrecoverable - so they are noted here rather than each acquiring a lock.

Method note: this environment cannot run most of the affected suites - the Redis network guard
fails every test whose request path touches the cache, including the *entire* class the E2EE reset
tests live in (36 of 47 fail at HEAD). Both fixes here were verified by pinning that one test's
cache to locmem and running the RED/GREEN pair explicitly against HEAD and against the fix, rather
than by reading a green summary line.

## Second pass (2026-08-06): cross-module interactions - findings & fixes

Looking for bugs that live *between* modules, which a module-by-module pass structurally cannot
see: each module looks correct in isolation and the defect is in the seam.

- **Deleting a pin from its detail page left a ghost marker on the map.** Three correct-looking
  pieces compose into a bug. (1) The map caches pins in localStorage. (2) Its background poll
  decides whether to refresh by comparing `Max(updated)` across the profile's pins - which a
  *deletion* cannot advance, so the poll is structurally blind to deletions; the only signal is
  the `ul_pins_dirty` flag. (3) `deletePinCascade` is a shared helper in `base.html` called from
  both the map page and the pin detail page, and it did not set that flag - the map's call site
  set it separately, and the detail page's call site did not. So a pin deleted from its detail
  page stayed in the map's cache indefinitely, restored on every subsequent load, with a marker
  that 404s when clicked. It only self-corrected in the one case where the deleted pin happened
  to be the most recently updated one, which changes `Max(updated)` and trips the poll's
  not-equal check by accident.

  Fixed in the shared helper rather than at the second call site, so every present and future
  caller inherits it. Guarded by a contract test that brace-matches the helper's body out of the
  template and asserts the flag is set on the success path (and not before the user-cancelled
  bail-outs) - the same approach used for the pin-cache version contract, and for the same reason:
  the invariant spans a template and its callers, where no type or constraint can reach.

  Checked the neighbours: map bulk-delete is map-only and manages the cache directly, and
  undo-restore recreates the row with a fresh `updated`, which the poll does catch. Deletion was
  the unique blind spot.

Verified clean: every `@receiver` in the codebase passes `dispatch_uid`. No Celery task is
enqueued inside a `transaction.atomic()` block without `on_commit` (checked by AST, not grep). No
`@shared_task` calls `.objects.get()` unguarded, so a task dispatched by one module tolerates
another module deleting its subject first. All six user-upload paths that create an `Image`
(gallery x2, safety, consensus, direct messages, and the shared photo-upload service) run the
`image_upload_error` gauntlet. All six `PinShare.objects.create` paths pair with
`resolve_and_stamp_origin_share`/`resolve_origin_share` + `record_share_exposure`, so the
`LocationExposure` provenance chain CLAUDE.md calls out is intact. Every render of
`notification_item.html`, including the single-item re-render after marking one read, goes through
`NotificationQuerySet.for_display()`.

## Second pass (2026-08-06): access control - findings & fixes

- **Relinking a pin was a way to *earn* wiki access rather than discover it.** Wiki visibility is
  deliberately gated on discovery - `location_visible_to` grants on an exact `Location` match - so
  which Location a pin points at is not a neutral preference, it is the thing that confers access.
  `PinRelinkView` scoped the *pin* to the requester (`_pin_for_user`) but resolved the target
  `Location` straight from the URL slug with no visibility check. And a Location's slug is its
  `official_name` (falling back to uuid only when unnamed), so the slug of any notable place is
  guessable: the test's undiscovered location slugs to literally `hudson-river-state-hospital`.
  Any authenticated user could therefore POST `pin/<their-pin>/link/<guessed-slug>/` and read the
  community wiki for a place they had never found - defeating the discovery rule CLAUDE.md calls
  out as deliberate design. Confirmed end to end against current code: before the fix the wiki
  returns 404, the relink succeeds, and the same wiki then returns 200.

  Fixed by requiring `location_visible_to(location, pin.profile)` before relinking. This breaks no
  legitimate flow, which is why the gate is safe rather than merely strict: the location picker
  offers the pin's own location plus `competing_wiki_locations`, which already filters to
  `accessible_domain_ids`, and the wiki page's switch button offers those same candidates - every
  reachable target already passes.

Verified clean, mechanically rather than by eye: an AST sweep for `get_object_or_404` on a
request-supplied id with no scoping kwarg and no pre-filtered queryset found 59 call sites, and
every one either re-checks ownership on the next line (`image.profile_id != profile.pk`,
`suggestion.profile_id != profile.pk`), gates through a service (`is_accepted_partner`,
`is_owner_or_accepted_partner`), is a token credential, or is admin-only. `detail_pins.py` resolves
a Location from a slug but gates it with `location_visible_to` immediately. All three
wiki-scoped `Image` queries apply `.visible_to(profile)`. `views_wiki.py` consistently re-scopes
children to the resolved wiki (`get_object_or_404(WikiEdit, id=edit_id, wiki=wiki)`). The wiki
access helpers are fail-closed on a missing place or domain root.

### Environment: the dev container was 30 tracked files behind the working tree

Found while chasing a `ModuleNotFoundError` for `services.places`: the `app` container image was
built from an older commit and was missing 30 tracked files - the whole `models/place`,
`models/album` and `models/map_overlay` packages, several controllers, and migrations 0026-0038.
Every `docker exec ... pytest` run in this audit before this point therefore executed against a
materially older codebase than the working tree.

The HEAD-vs-fix baselines stand (both sides ran in the same stale container, so the comparison was
like-for-like, and that is what the no-regression conclusions rest on), but any absolute
"the suite passes" reading of those runs was weaker than it looked. `docker cp src/urbanlens/.
urbanlens_devs1_app:/app/src/urbanlens/` resyncs it; the pin-relink fix above was re-verified both
ways *after* syncing. Worth checking the container is current before trusting a test run here.

## Re-verification against the synced container (2026-08-06)

Every `docker exec ... pytest` run in this audit before the resync executed against an image 30
tracked files behind the working tree. Re-ran everything that matters now that `/app/src` matches.

- **All 36 regression tests this audit added still pass** against current code with the full
  migration set (0026-0038 present, so a fresh test database). The bun suite is green at 152/152
  and `tsc --noEmit` is clean. No fix in modules 1-11 or the second pass depended on the stale
  schema.

- **The container also held 46 files that no longer exist in the repo**, since `docker cp` adds but
  never deletes. Five were deleted plugin modules under `plugins/builtin/` - and
  `plugin_registry.discover()` scans that directory, so the container was registering
  `duckduckgo`, `marginalia`, `mojeek`, `searxng` and `opentopomap` as live plugins after module 4
  removed them. Deleted them so discovery matches the codebase. No stale *migrations* were present,
  so the migration graph was never wrong.

- **Correction to the pin-relink fix (a regression I introduced and caught here).** The first
  version gated relinking on `location_visible_to` alone. That was too strict: `PinRelinkViewTests`
  - six pre-existing tests I could not run before the resync - relink to a bare `Location` with no
  `Place` row, which that predicate rejects. Since the Places feature is recent (migrations
  0026-0028) and `Location.place` is nullable, a place-less target is an ordinary production case,
  not a test artifact, so the strict gate would have broken real relinking.

  The gate now accepts a target two ways, matching what the UI actually offers: one the profile can
  already reach, *or* one covering the pin's own coordinate (`get_all_for_point`, which falls back
  to a 50 m proximity check for place-less coordinates). A place the user's own pin sits inside is
  one they discovered by pinning it, so allowing it discloses nothing they could not derive - while
  a Location kilometres away with no relationship, which is the attack, is still refused.

  Four of those six tests placed their target kilometres from the pin, which quietly asserted that
  relinking to an arbitrary Location was allowed - the hole itself, encoded as a contract. Moved
  them inside the pin's own domain, preserving exactly what each test verifies (uuid-slug fallback,
  merge-vs-relink, another profile's pin not triggering a merge), and documented on the class why
  the coordinates are deliberate. All six pass with the corrected gate, as do the five
  pin-relink-access security tests.

Worth stating plainly: the too-strict first version passed every test I could run at the time. It
was caught only by reading the fixtures of tests the environment could not execute. When a test
cannot run, its assertions still encode a contract - read them.

## Recheck of the place/album/overlay suites (2026-08-06)

- **All 34 failures were the test suite's own cache configuration, not defects.**
  `settings/test.py` inherited `base.py`'s Redis/valkey-backed `CACHES`, so any test whose request
  path touched the cache depended on a live external service. Under the suite's localhost-only
  network guard that fails with an opaque "External network access is disabled during tests" -
  a message about the environment that says nothing about the code. Pointing the test cache at
  locmem turned the nine suites from 34 failed / 125 passed into **159 passed**, with no other
  change. That covers module 5's map-overlay upload fix and the albums work, whose models were
  absent from the container when those modules were audited and had therefore never actually been
  exercised; both are now genuinely verified.

  This was worth fixing at the source rather than per test class. It is also correct on its own
  merits: tests previously shared one cache instance, so entries could bleed between them, and
  locmem is per-process and needs nothing running.

- **The container held 10 orphaned test files** for modules this audit deleted earlier -
  `test_abstract_serializer` (module 1 removed `abstract.Serializer`), the five search-provider
  suites (module 4), and the Smithsonian/Internet Archive/NPS gateway suites (migrated to REData).
  None are tracked in the repo; they were leftovers from the old image, and they aborted full-suite
  collection with import errors for modules that no longer exist. Removed, so the container's tree
  now matches the repo exactly.

The through-line of this whole re-verification: for most of this audit, "the suite fails here" was
treated as an immovable property of the environment and worked around one test at a time. It was a
one-line defect in the test settings. Time spent early on making the harness trustworthy would have
paid for itself many times over - every module's verification was weaker than it needed to be, and
one fix (the pin-relink gate) shipped a regression that a runnable suite would have caught
immediately.

## Full-suite baseline (2026-08-06)

First complete run of the test suite in this audit, possible only after pointing the test cache at
locmem. **18 failed, 10,025 passed, 1,428 subtests passed, in 58 minutes** (`pytest src/urbanlens`,
container synced to the repo, fresh test database). That is a 99.8% pass rate and the reference
point future work should measure against - previously there was no number at all, only per-file
runs whose failures were indistinguishable from environment noise.

Triage of all 18, none caused by this audit's changes:

- **12 panel failures** (`test_panel_feature_gate` x3, `test_pin_panel_info` x7,
  `test_pin_panel_live_refresh` x2) - **pre-existing, genuine, and worth a dedicated
  investigation.** They fail with `204 != 200`: the tests seed `LocationCache` for a panel source
  and expect the panel to render, but the view falls through to its pending-loader path and then
  returns 204 from `attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(...)`.
  `MAX_POLL_ATTEMPTS` is 30 and the tests pass `attempt=1`, so the 204 comes from
  `schedule_panel_fetch` returning falsy after a cache lookup that missed data the test just wrote.
  A sibling test seeding `nominatim` the same way passes, so it is source-specific rather than a
  broken mechanism. If the lookup really does miss in production, pin-detail panels silently render
  nothing; if it is only the tests that are stale, they are asserting a contract the code no longer
  has. Either way it needs resolving - flagged here rather than guessed at.

  Confirmed not mine: reverting the SiteSettings request memo in the container reproduced exactly
  the same 5 failures in the two files I re-ran (5 failed / 15 passed both ways).

- **5 infrastructure-stats failures** (`test_site_admin_stats` x4, `test_infrastructure_stats` x1) -
  environmental, and already recorded as such in module 1. They collect live postgres/valkey/celery/
  nginx status, which no test environment can satisfy without those services reachable.

## Unit 08 (deferred item, now fixed): media send-to-wiki no longer downloads in-request

`media_send_to_wiki` looped up to 20 gallery items calling `materialize_media_item`, each a remote
download, inside the request handler. Two problems, both user-visible: a multi-second hang with no
progress indicator, against the project standard that says anything non-instant shows one; and a
request that times out partway attaches some photos and drops the rest silently while the frontend
toast reports success for all of them.

Split the way `cache_media_item_into_album` already splits the same work - validate and enqueue in
the request, download in `tasks.cache_media_item_into_wiki`, which tolerates the wiki or profile
being deleted between enqueue and run. The endpoint now returns `{"queued": N}` and the toast says
the photos will appear shortly rather than claiming they are already there. Eight tests cover it,
including that nothing is materialized in-request, that the 20-item cap still holds, and that a
malformed entry is reported without stopping the rest of the batch.

## The 12 panel 204 failures: stale tests, not a product bug (2026-08-06)

Verdict: **the panels behave correctly; the tests predated the REData migration.** Nearly every
info panel is REData-backed now, and each one's `gate()` ends in `redata_configured()`, which reads
`UL_REDATA_API_URL`/`UL_REDATA_API_KEY` live from app settings. Neither is set in a test
environment, so the gate refuses and `panel_info` degrades to a quiet 204 - exactly right for an
install with no REData, and invisible to a test written when the panel simply rendered. The sibling
test that passes seeds `nominatim`, which is not REData-backed; that is the whole of the
source-specific split.

Fixed by adding a `RedataConfiguredMixin` (`tests/hypothesis/redata_helpers.py`) to the four
affected classes. It patches the two *settings values* rather than the `redata_configured` symbol,
because every plugin module imports that function by name - patching it at its origin would miss
them all, and patching per module means enumerating every module a test happens to touch. The
function reads settings at call time, so setting them covers all of them at once. Tests that
specifically want the unconfigured behaviour keep patching their own module's symbol, which is the
existing convention (see `test_inaturalist_panel.py`).

Two of the twelve survived that fix, for a second and unrelated reason:
`PinPanelLiveRefreshTests.setUp` never called `super().setUp()`, so the mixin - and the project's
own `TestCase.setUp` - never ran for that class. Added the call.

All 50 tests across the three files now pass.

Worth recording how this was found, because the first three attempts were wrong. I hypothesised in
turn that the Celery broker was unreachable (plausible - `safely_enqueue_task` catches the guard's
`RuntimeError` and reports "broker unreachable", and the view treats that as "give up quietly"),
that `render_context` had changed shape, and that the baker recipe placed the pin at null island.
Each was consistent with the symptom and each was wrong. What settled it in one run was a throwaway
probe test printing the actual intermediate values - `gate(pin): False` - rather than more reading.
Reaching for that earlier would have saved three test cycles at ~3 minutes each.

The broker change made while chasing the first hypothesis was kept: pointing `CELERY_BROKER_URL` at
`memory://` in tests is correct on its own merits, for the same reason as the cache - `apply_async`
should not need a live service, and a test asserting that a request only *scheduled* work now sees
that rather than a swallowed connection error.

## Unit 20 (deferred item, now fixed): pin creation no longer blocks on an AI call

`PinSerializer.create()` called `AutoTagService().suggest_for_pin(pin, apply=True)` inline. That
service runs a keyword stage and then, for any label the keywords missed, calls the LLM gateway -
a network round-trip. So every pin created through the REST API, or through an import that goes
via this serializer, waited on an LLM before the response returned.

This is the recurring two-call-sites shape again, and the fix was already sitting there: the task
`tasks.suggest_pin_category` exists and both other creation paths
(`services.pins.pin_creation` and the Google Maps import) already enqueue it. Only the serializer
was left behind. It now enqueues the same task.

Nothing in the response depended on the tagging having run - the previous code swallowed the
service's exceptions, so a failed tagging attempt was already invisible to the caller. Four tests
pin the new behaviour, including that the pin is still created when the broker is unreachable
(`safely_enqueue_task` returns None) and that a create still isn't treated as an explicit rename.

## Unit 09/10 (deferred item, partly fixed): partial bulk actions now say so

`PinSuggestionBulkActionView` skips ids it cannot act on and logs any that raise, then returns
`processed` alongside `requested`. The page reported only `processed`, as an unqualified success -
so accepting 3 of 5 rendered "Accepted 3 suggestions", identical in shape to accepting all 5. The
user is handed a number with nothing to compare it against, and the two that failed are invisible.
Same silent-partial-failure shape as Unit 08's timeout.

The backend contract was already sufficient; only the toast ignored half of it. It now warns rather
than congratulates when `processed < requested`. Four tests pin the contract the toast depends on -
a whole batch, another profile's id being counted but not acted on, one raising item not stopping
the rest, and accept actually marking the suggestion handled.

Still open from this item: `TripActivity.order` has no uniqueness constraint or locking, so two
concurrent reorders can interleave. That is the same check-then-act family the second pass covered,
and is left for a dedicated pass rather than bundled in here.

## Unit 09/10 (second half, now fixed): trip activity ordering was unserialised

Two check-then-act sequences share the `order` column, and a threaded test reproduces both.

`reorder_activities` validated that the submitted ids are an exact permutation of the trip's
non-completed activities, then applied the positions with one `update()` per row, holding nothing
in between. Two members dragging the itinerary at once interleave: the observed result was
`[1, 2, 3, 3]` - one position written twice, another lost entirely, and a final order neither
member asked for. An activity added between the check and the writes also invalidates the
permutation the check just approved.

`create_activity` appended at `order=trip.activities.count()`, so two concurrent adds both read the
same count and both took that position. The same `count()` backs the `max_trip_activities` quota,
so the pair could also both pass a quota only one of them should have.

**A unique constraint on `(trip, order)` is not the fix**, which is worth recording because it is
the obvious first idea and it would break the feature. Positions are applied one row at a time, so
a partial permutation legitimately collides mid-loop; and reordering only covers non-completed
activities, leaving completed ones holding positions that overlap the reassigned range by design.
Both would violate the constraint during normal use.

Serialising on the parent trip - the same shape as the Consensus tentative-answer fix - matches how
the data is actually used. In `create_activity` the lock is entered *after* place resolution, so it
covers only the read-then-write section rather than a geocoding round-trip. The trip controller's
own 92 tests pass alongside the three new race tests.

## Unit 24/25 (deferred item, now fixed): round generation re-ran the eligibility query per retry

`generate_round_content` retries up to `_MAX_LOCATION_ATTEMPTS` (25) times, skipping any location
the mode cannot build a round from. Every attempt re-ran `eligibility.eligible_locations` - a
multi-join across every participant's pins, and optionally their visits, a label filter and a geo
bound - so generating a single round could cost 25 executions of the most expensive query on the
game path. It runs for every round of every session, and a prior fix already had to attack a
related O(pool size) problem inside `pick_next_location` for the same reason (see its comment about
`/start/` being slow-to-timing-out).

Nothing eligibility depends on changes between attempts; only the caller's own exclusion list
grows. The eligible ids are now resolved once and each attempt narrows a plain primary-key
queryset. Deliberately not converted to a list: `pick_next_location` applies a PostGIS proximity
filter (`point__distance_gte`) to the candidates, and reimplementing that distance check in Python
would put a second, divergable copy of the rule in the codebase - a cheap `pk__in` queryset keeps
the existing contract intact.

Verified both ways: the new test reports `4 != 1` against the previous code and passes with the
change; all 321 SpotGuessr tests pass.

Trivia's equivalent path was checked and left alone - `eligible_questions` is called once per round
with a growing exclusion set, not inside a retry loop, so it does not have this shape.

## Final full-suite verification (2026-08-06)

**6 failed, 10,060 passed, 1,428 subtests passed** in 57 minutes, against a recorded baseline of
18 failed / 10,025 passed. Twelve of the baseline's eighteen are fixed, none were introduced, and
the extra 35 passing tests are the regression tests this audit added.

The six remaining were all present in the baseline:

- **5 infrastructure-stats tests** (`test_site_admin_stats` x4, `test_infrastructure_stats` x1) -
  environmental. They collect live postgres/valkey/celery/nginx status and cannot pass without
  those services reachable from the test process. Not a code defect.
- **1 settings subtest** (`show_supporter_badge`) - a genuine, small API bug, now fixed. The field
  was added to `Profile`, to the `SETTINGS_FIELDS` allowlist and to migration 0031, but not to the
  external API's settings serializers, so `read_settings` produced it and the serializer dropped it
  again: a preference an external client could neither read nor write. Added to both the read and
  write serializers, in the position the allowlist declares it. This is exactly the failure mode
  the serializer's own docstring predicts ("a new preference is invisible here until deliberately
  added to SETTINGS_FIELDS *and* to this class") - the fail-closed design worked, the second step
  was just missed.

With that fixed, every remaining failure in the suite is environmental. The audit's cumulative
work - 11 modules, a second pass, and six deferred Unit items - holds against a full run.

## The last 5 failures were a real bug, not an environment limitation (2026-08-07)

I had recorded the five infrastructure-stats failures as environmental - "they need live
postgres/valkey/celery/nginx". Looking properly, the reason they failed is a genuine product
defect: **the site-admin status page 500s when an infrastructure service is unreachable.**

`collect_infrastructure_service_stats` called its four collectors with no isolation. Each collector
handles the failure it expects (Valkey catches `RedisError`, and so on), but anything else - a
malformed URL, a DNS error surfacing as `OSError`, a driver raising something new after an upgrade -
propagated straight out and took the whole page with it. That page exists precisely to tell an
admin which component is unhealthy, so a component being unhealthy in an unanticipated way hid the
state of the three that were fine along with the one that wasn't. Exactly the per-item isolation
gap found in the safety sweeps (module 9).

Each collector now degrades to an "Unavailable" stat of its own. The page always returns four
entries in display order, however badly the services are behaving - and the five tests pass,
because the network guard's `RuntimeError` is now reported as a degraded service rather than
crashing the request. Fixing the product fixed the tests; mocking the tests would have left the
500 in place.

Two new tests cover the resilience directly: one collector raising must not lose the other three,
and all four raising must still return four entries.

Worth noting a mistake in my own first attempt: I put the four collectors in a module-level tuple,
which captured the function objects at import time and silently made them unpatchable - my own new
tests failed because `mock.patch` on the module attribute had no effect. Naming them inside the
function keeps the reference resolving at call time. An abstraction that breaks testability is
worse than the repetition it replaced.

**The suite now has no known non-environmental failures.**

## Same fan-out gap on the Memories feed (2026-08-07)

The site-admin finding generalised on the first place I looked. `get_memory_events` merges four
independent sources - routes, trips, visits, photos - with no isolation, so any one raising (a
corrupt row, a missing relation, a geometry error) discarded the other three and 500'd the feed on
both the HTML page and the external API.

This is the module's own advertised extensibility seam: its docstring says adding a memory type is
one new function in the source list and nothing else changes. Unguarded fan-out makes that seam a
liability - a bug in a newly added source takes out the three that already worked. A Memories page
missing one kind of memory is worth far more than no page at all.

Each source is now drained independently. `extend()` consumes a generator incrementally, so a
source that fails partway keeps whatever it already yielded rather than losing that too - pinned by
a test rather than assumed.

**`_EVENT_SOURCES` had the same import-time capture problem** as the infrastructure collectors, and
this time the tests caught it before the fix landed: two of the four new tests passed/failed in a
pattern that only made sense if `mock.patch` was having no effect, which is exactly what a
module-level tuple of function objects causes. Replaced with `_event_sources()`, resolved per call.
Twice in one session, so it is worth stating as a rule: **a module-level tuple of function
references is a testability trap** - it freezes the bindings at import and silently ignores
patching. Name the functions inside the function that uses them.

Verified clean: the pin detail page's street-view provider fan-out already isolates per provider
and records an `ok=False` result for the admin debug overlay, which is the pattern the rest should
match.

## Two more unguarded fan-outs, found by an AST sweep (2026-08-07)

An AST sweep for `for <source|provider|panel|handler> in ...` loops that invoke the loop variable
with no `try` inside found six candidates; two were real.

- **`panel_readiness` (pin detail page)** - the most consequential of the three. It builds the tab
  strip for the app's busiest page, and it calls `is_ready(pin)` per *bespoke* panel with no guard.
  Panels are the plugin extensibility surface, so a single plugin raising there returned a 500 for
  the whole pin page instead of affecting its own tab. Failures are now logged and treated as "not
  ready", which is the safe default: the tab shows its pending state and polls, exactly as it does
  for a panel whose data genuinely hasn't arrived yet. The cache-backed and slide-backed panels
  were already fine - those resolve through one bulk query rather than a call per source.

- **`get_journal_entries`** - same shape, and its own docstring already argued the case: it
  explains that an *unknown* source key degrades to fewer entries "rather than a 500". A
  *registered* source that raised did 500. Now isolated per source.

Verified clean: the street-view provider fan-out already isolates per provider and records an
`ok=False` outcome for the admin debug overlay. The label-merge and pin-bulk loops the sweep also
flagged iterate the user's own selected rows inside a transaction, where aborting the whole
operation is the correct behaviour, not a bug.

A note on the panel test, which failed on its first run for an instructive reason: I wrote the test
panels as `InfoPanelSource` subclasses, but that inherits `LocationCachePanelSource`, so they were
resolved by the bulk cache query and their `is_ready` was never called - the test exercised a
branch the fix doesn't touch and reported the fix as broken. Checking the class hierarchy rather
than re-reading the fix settled it immediately. Test doubles have to sit in the same branch of the
hierarchy as the code under test, or they quietly prove nothing.

## Query amplification on the map payload (2026-08-07)

Measured rather than read: build N items, count queries, add more, count again, require no growth.
Three of the four measured paths are flat. The map's pin payload was not - **exactly one extra
query per pin**, on the highest-traffic serialization in the app.

The probe named it in one run: `SELECT ... FROM dashboard_wikis`, 3 -> 6 as pins went 3 -> 6.
`serialize()` reads `pin.effective_name`, which falls through to `Location.display_name`, which
reads the reverse OneToOne `wiki`. `prepare_queryset` select_related `location` but not
`location__wiki`. `Location.display_name`'s own docstring asks callers to
`select_related("wiki")` in bulk "to avoid an extra query per row" - the guidance was already
written down, and the busiest caller in the app was the one that missed it. Same family as the
notification-dropdown N+1 from module 7: a reverse OneToOne that reads like an attribute.

Added `test_query_amplification.py` as a standing guard over the map payload and the pin detail
page (flat in both label count and visit count).

Two notes on getting the measurement right, both of which cost a cycle:

- The first harness demanded a delta of exactly zero and reported *-2* on the pin detail page. That
  is warm-up, not a fix: the first request of a test populates per-process caches. The harness now
  measures a discarded warm-up call first and asserts one-sided - cost must not *grow*; a page
  getting cheaper is never the bug being hunted.
- The first version of the map test called `service.all(service.prepare_queryset(qs))`, but `all()`
  applies `prepare_queryset` itself. The doubled `Prefetch("labels", ...)` raises
  `ValueError: 'labels' lookup was already seen with a different queryset` at evaluation. That was
  my error, not the code's - but it is worth knowing the API raises rather than silently
  double-prefetching.

## Query amplification on the external API's trips list (2026-08-07)

Extending the harness to the wiki page, trips detail page and the API list endpoints found the
pages clean - flat in comment, alias, link, edit-history, activity and member count - and one more
real defect. **The API's trips list cost six queries per trip** (24 -> 42 for three more trips), and
the probe split it immediately: five against `dashboard_trip_activities`, one against
`dashboard_trip_memberships`.

- **Five per trip: the effective-date properties.** `TripSummarySerializer` exposes
  `effective_start_date`, `effective_end_date`, `timeline_status` and `duration_days`, and the last
  two each read *both* of the first two - which fall back to querying the trip's activities. This
  is the same defect fixed in the Memories feed in module 9, resurfacing on a different surface
  because that fix was local to the aggregator.

  Fixed at the model this time instead of at each call site: both properties now prefer a
  `_eff_start`/`_eff_end` annotation when the queryset supplied one, and otherwise compute once and
  memoize on the instance. So a list of trips is flat wherever it is annotated, *and* a single trip
  costs one query however many of the four fields are read. `for_list_page` now supplies the
  annotations, matching what the Memories aggregator already annotated - including using the later
  of `scheduled_at` and `scheduled_end`, so the two surfaces agree on when a trip ends.

- **One per trip: the viewer's membership.** `_membership()` ran a targeted query per trip even
  though `for_list_page` prefetches the whole roster - the row was in memory already. It now reads
  the prefetch when one exists (checked via `_prefetched_objects_cache`) and keeps the targeted
  query otherwise, so callers that did not prefetch don't start pulling whole rosters.

The general lesson, now seen three times (Memories trips, the map's `location__wiki`, this): **an
expensive model property is invisible at the call site.** `serializer.field = source="x"` looks
free. Making the property itself annotation-aware and memoized fixes every present and future
caller, which a per-call-site prefetch does not.

877 trip/memories tests pass alongside the new guards.

## The plugin/panel extension surface, from an author's point of view (2026-08-07)

- **`docs/designs/plugins.md` does not exist.** It is at `docs/designs/plugins.md`, moved there by an
  earlier "clean, organize" commit that left every reference behind: CLAUDE.md (the project
  instructions themselves), `docs/FEATURES.md`, `docs/ROADMAP.md`, a design draft, and two source
  docstrings - seven dead paths, so an author following the instructions lands nowhere. Updated all
  seven to the real location rather than moving the file back, since the move looks deliberate.

- **The doc never shows how to write the panel.** Its worked example contributes
  `NpsPanelSource()` and then never defines it, so the one class an author actually has to write is
  the one part not demonstrated. Recorded rather than fixed here - writing that section properly is
  its own piece of work, and worth doing.

- **Three of a panel's required attributes fail quietly when omitted.** `section_id` and `title`
  default to `""` on the base class, and `cache_source` is meaningful only by convention. Get any
  of them wrong and nothing raises: you get a section with no DOM id for HTMX to swap against, a
  panel with no heading, or - the quietest - a cache-backed panel whose fetch writes one key while
  its read looks for another, so it polls forever and never renders. Added
  `panel_source_problems()`, reported once per key from `panel_sources()`, plus a test asserting
  every panel this repo ships is well-formed. That test is the useful half: it turns a silent
  runtime absence into a loud CI failure and keeps working as panels are added.

**The validation had to be calibrated against reality twice, which is the interesting part.** The
first rule demanded `section_id`/`title` of every panel and immediately flagged nine shipped media
panels. They were right and the rule was wrong: gallery media providers render as tabs *inside* the
combined Media gallery, which supplies the surrounding markup. The second rule exempted those and
flagged the core `boundary` panel - also correct, and also not a section: it fetches boundary data
the map and the external API consume, rendering nothing. The rule that survives is the precise one:
only `InfoPanelSource` and `SlidesPanelSource` render their own section, so only they need the
presentation attributes.

Had I "fixed" the nine panels the first run flagged, I would have added meaningless attributes to
correct code and called it an improvement. A validation rule asserted against the whole existing
codebase gets corrected by it; one asserted against a couple of hand-picked examples does not.

## Frontend layer: the inline-JS mass (2026-08-07)

Measured, since the project's stated preference is HTMX first and TypeScript only where HTMX
cannot do the job: **31 templates carry more than 120 lines of inline `<script>` each, totalling
19,638 lines.** The worst is `pages/map/index.html` at 5,052 lines of inline JS in a 5,691-line
template - 89% of the file - followed by `themes/base.html` (1,882), `pages/messages/index.html`
(1,772), `pages/trips/detail.html` (1,380) and `pages/location/index.html` (1,297).

For comparison, the entire typechecked TypeScript tree is 59 files. `tsconfig.json` includes only
`frontend/ts/**/*.ts`, so **none of those 19,638 lines are typechecked, bundled, minified, or
reachable by `bun test`.** Every defect this audit found in that layer - the pin-cache version
drift, the duplicated trip-name list, the pin-delete cache invalidation - lived in inline template
JS, which is consistent with it being the least-guarded code in the repo.

This is the largest maintainability gap found in the audit, but "move it to TypeScript" is a
programme of work rather than a fix, and which parts become HTMX versus bundled modules is a
design call for the maintainer. Recorded with numbers so it can be prioritised, along with the
obvious first candidate: `base.html`'s 1,882 lines are shared by every page.

**Correction to the paragraph above**, which originally also said the dev toolbar's 591 lines
"ship to production for a development-only feature". They do not. `base.html` includes that partial
only under `{% if show_dev_toolbar %}`, and `SiteSettings.show_dev_admin_features` grants it to
site admins solely when the effective environment is development or local, and to non-admins only
when `UL_ALLOW_DEV_TOOLBAR_FOR_NON_ADMINS` is set *and* the environment is
staging/development/local/testing. Production is excluded on both paths. The toolbar is still worth
extracting so it is typechecked, but the reason given for prioritising it was wrong, and correcting
it changes which target is actually valuable: `base.html`, which does render on every page.

Checked and deliberately *not* changed: six templates hardcode the values of a server-side
`TextChoices` in their JS (`MapViewChoice` and `MapCenterMode` fully, `ThemeChoice`,
`MapLayerMode`, `DirectMessageShareKind` partially). It reads like the pin-cache version
duplication, but it is not the same class of defect: those values are stable identifiers used for
genuine branching (keyboard shortcuts, layer switching), and adding a new choice leaves the
existing branches working rather than silently disabling a feature. Coupling worth knowing about,
not a latent bug, and not worth the indirection of emitting them through `json_script`.

### Extracting base.html's inline JS: the pattern and the constraint

The codebase already has the right pattern, and documents it: `entries-classic/core.ts` is a
classic (non-module) IIFE bundle loaded in `base.html`'s `<head>`, installing window globals via
`installGlobalX()` helpers from `shared/` modules. Its own docstring explains why it is not
`type="module"` - module scripts defer until after parsing, and several pages call these globals
from classic `<script>` tags that run synchronously. So extracting base.html's remaining globals is
"add a `shared/x.ts` with an `installGlobalX()`, import it in `core.ts`, delete the inline block".

`base.html` exposes 15 such globals. The dialog group is the obvious first extraction -
`confirmDialog` (used by 10 templates), `deletePinCascade` (4), `urbanlensConfirmExternalLink` (2) -
because it is self-contained, has no Leaflet dependency, and already has a contract test.

**But the naive move is wrong, and this is the part worth writing down.** That block is not just
function definitions: its IIFE resolves `#confirm-dialog` and its buttons *at load time* and
attaches listeners to them, and it sits after the `<dialog>` markup in the body. `core.ts` runs in
the `<head>`, where those elements do not exist yet - so lifting the block as-is yields null
element references and a dialog that never opens. The extraction has to bind lazily (resolve the
elements and attach listeners on first use) before it can move.

Not attempted here for a reason worth stating: this repo has no DOM/browser test layer, so a
behavioural change to a dialog reached from 10 templates could not be verified beyond typechecking,
and `bun test` would report success either way. The contract tests added by this audit only parse
templates for invariants. Specifying the constraint is more useful than a plausible-looking change
nobody can check - and it makes the case that a small DOM-level test setup would pay for itself
before this 19,638-line layer is moved.

## A DOM test harness, and the first extraction it made safe (2026-08-07)

The blocker recorded above was real, so it got fixed first: `bun test` had no document, so
anything touching the DOM could be typechecked but never *exercised*. Added
`@happy-dom/global-registrator` as a dev dependency and a three-line preload
(`frontend/ts/testing/dom-setup.ts`, wired through a new `bunfig.toml`).

It immediately paid for itself twice over:

- `pin-cache.test.ts` carried a hand-rolled `MemoryStorage` polyfill assigned onto `globalThis`,
  which now conflicted with the real one - so those seven tests were rewritten against an actual
  `Storage` rather than a stand-in.
- The `base.html` dialog group moved out: `shared/confirm-dialog.ts` now owns `confirmDialog`,
  `urbanlensConfirmExternalLink` and `deletePinCascade`, installed by `entries-classic/core.ts`,
  binding `#confirm-dialog` **lazily on first use** exactly as the constraint required. 107 lines
  of inline JS left `base.html` (1,882 -> 1,776), and 12 behavioural tests now cover what was
  previously untestable: cancel resolves false, the alt button resolves `"alt"`, the message is
  HTML-escaped, a 409 asks about children and retries with the chosen mode, a failed delete does
  not flag the cache, and - the one that matters most - binding still works when the markup appears
  after the module loads.

  The old `pin-delete-invalidation.contract.test.ts`, which parsed `base.html` looking for the
  string `ul_pins_dirty`, was deleted: the DOM test asserts the actual behaviour it was
  approximating.

**The typechecker found a latent type lie on the way through.** `types/globals.d.ts` declared
`window.confirmDialog` as returning `Promise<boolean>`, but the implementation could already return
`"alt"` when a caller passed `altLabel` - which the pin-delete "keep child pins" path does. Any
caller narrowing on that type was trusting something untrue, and `"alt"` is a truthy string, so it
would have read as a confirmation. Corrected the declaration and made `dialogs.ts`'s
`confirmAction` narrow explicitly rather than by assumption.

That is the argument for the harness in one paragraph: moving 107 lines under the typechecker
surfaced a wrong type declaration that had been sitting in a `.d.ts` unexercised. There are 1,776
lines left in `base.html` alone.

### base.html's remaining 1,776 lines, block by block

Surveyed rather than attacked, so the next extraction starts from evidence. The file has eight
inline blocks left:

| Block | Lines | Globals | Extractable? |
| --- | --- | --- | --- |
| 1 | 158 | `urbanlensMediaThumbFallback`, `urbanlensSizeEditInPlaceInput` | Yes, but carries `{{ csrf_token }}` and Django `messages`, so it needs a `json_script` config first |
| 2 | 251 | none | Probably - no globals to keep in step |
| 4 | 62 | `autosaveGuard` | Yes, small and self-contained |
| 6 | 936 | the comment-map composer group | **Not as one move** - see below |
| 7 | 307 | `ulSectionCollapsed`, `ulRefreshCollapseRestore`, `ulFlyToToolsFab` | Best next target - no template tags, no Leaflet |
| 8 | 24 | none | Trivial, but 9 template tags for 24 lines - leave it |

**Block 6 (936 lines, the comment map composer) should not be moved as a single step.** It is one
IIFE whose functions share closure state, it drives Leaflet through `typeof L` guards, and it
embeds seven `{% url %}` tags plus the viewer's profile uuid. Moving part of it would leave the
group's globals defined in two places, which is worse than leaving it; moving all of it would put
~900 lines of Leaflet-dependent behaviour under a test harness that has no Leaflet. It needs its
own plan, and probably a Leaflet stub, before it is touched.

**Block 7 is the right next one** - 307 lines, no template tags, no Leaflet, mostly `localStorage`
and class toggling that happy-dom tests well. One caveat found while reading it: about 70 of those
lines (`ulFlyToToolsFab` and `_toolsFabTarget`) measure layout via `getBoundingClientRect` and
`getComputedStyle`, which happy-dom returns as zeros - so that part would move unverified. The two
concerns share no closure state, only DOM lookups, so they can go as two modules with the
animation helper's move made deliberately and knowingly untested rather than by accident.

Not started this turn rather than half-finished: a 307-line port left incomplete is worse than one
not begun, and the survey is what the next attempt actually needs.

## Full-suite verification (2026-08-07): 10,087 passed, 0 failed

**10,087 passed, 0 failed, 1,429 subtests passed, in 58:49.** Not one failure line in the output.
The progression across the audit:

| Run | Failed | Passed |
| --- | --- | --- |
| First complete run (2026-08-06) | 18 | 10,025 |
| After the panel-test and settings-serializer fixes | 6 | 10,060 |
| This run | **0** | **10,087** |

Of the original eighteen, twelve were stale panel tests written before the REData gate existed, five
were the infrastructure-stats tests that turned out to be masking a real 500, and one was a settings
field the external API had never exposed. None were flaky; each had a cause.

**One caveat, stated because it affects how much this run proves.** Files were copied into the
container while it was running - the panel-contract validation landed mid-run - so this is not a
pristine single-commit run. pytest imports test modules at collection, but source modules are
imported lazily, so a mid-run copy can produce a mixed state. The container's Python tree is
byte-identical to the repo now, and the affected work (`panel_source_problems` and its ten tests)
was verified separately, but a run that proves a specific commit end to end needs no concurrent
copies. Worth doing before a release; not worth re-running an hour for now.

Also unaffected by that caveat: the frontend work landed today is host-side only (bun), never
copied into the container, so `shared/confirm-dialog.ts` and the happy-dom harness are outside this
run entirely. They are covered by `bun test` (162 passing) and `tsc --noEmit` (clean).

## Two more bugs found extracting base.html's guard/scroll/dialog blocks (2026-08-07)

Both were live in `base.html`'s inline script, both are now fixed with tests.

**1. Scroll-to-hash threw a `DOMException` on any fragment that is not valid CSS. FIXED.**
`_scrollToHash` called `document.querySelector(window.location.hash)` directly. A fragment
is not a selector: ids beginning with a digit, `#/route`, `#foo=bar`, `#!`, `#sec:2` and -
most relevantly - the `#_=_` and `#access_token=...` that OAuth providers append when
redirecting back all throw. This app signs in through Google and Discord, so that path is
reachable in production. Because the handler is bound to `htmx:afterSettle`, it threw again
on *every* HTMX swap for the life of the page.

Verified by probe before fixing: of eight realistic fragments, five threw. Fixed by trying
`getElementById` on the decoded fragment first (it takes a literal id, so it copes with all
of the above), keeping `querySelector` behind a `try` for fragments naming something other
than an id, and guarding `decodeURIComponent` against malformed escapes. A numeric id such
as `#123` now actually scrolls, where before it threw.

*Testing note worth remembering:* the first version of this test wrapped `dispatchEvent` in
`expect(...).not.toThrow()` and passed while the exception was plainly being thrown - a
listener exception is reported, not propagated, so that assertion can never fail. The test
had to call the function directly. Assertions around `dispatchEvent` prove nothing about
what the listener did.

**2. The unsaved-changes guard hijacked ctrl/cmd/shift-click. FIXED.**
The capture-phase click handler checked the href's scheme and `target="_blank"` but never the
modifier keys or mouse button. With unsaved changes present, ctrl-clicking a link to open it
in a background tab was intercepted, `preventDefault`ed, and - on confirming - navigated the
**current** tab via `location.href`. So the one gesture that would have safely left the page
alone instead destroyed the changes the guard exists to protect. Fixed by returning early on
`ctrlKey`/`metaKey`/`shiftKey`/`altKey`/non-primary button.

## The leave-page warning existed three times, with the same two bugs in each (2026-08-07)

Following up the ctrl-click bug found in `base.html`'s auto-save guard: the same
"confirm before leaving" logic had been copy-pasted into two more pages, and both copies
carried the identical defects.

  base.html            auto-save guard (unsaved / in-flight changes)
  safety/detail.html   "nobody would be notified if something happened"
  tools/index.html     an export is still running

**Bug 1 - modifier clicks were hijacked.** All three checked the href's scheme and
`target="_blank"` but none checked modifier keys or mouse button. Ctrl/cmd-clicking a link
to open it in a background tab was intercepted and, on confirming, navigated the *current*
tab via `location.href` - so the one gesture that would have left the page safely alone
instead destroyed what the guard was protecting. Worst on the safety page, whose entire
premise is that leaving means nobody can reach you.

**Bug 2 - the user was asked twice.** The safety and tools copies navigated via
`location.href` without first clearing their blocked condition. That navigation re-enters
`beforeunload`, which was still blocked, so the browser's own "Leave site?" prompt appeared
on top of the one just answered. The safety page even has a `_safetySkipLeaveWarning` flag
for exactly this, set on form submit and status update - but never in the link path. The
auto-save guard avoided it only because it happened to call `allowNavigation()` first.

Fixed by extracting the behaviour to `shared/leave-confirmation.ts`, which each page now
configures with just its condition and wording. The suppression is internal, so no caller
can forget it. Also filters `download` links, which save a file without navigating - they
must not be confirmed, because confirming permanently disarms the guard while the page
stays put.

Net: 39 lines of duplicated template JS removed, 25 tests added covering the behaviour that
previously had none.

## Live location sharing on a safety check-in failed silently (2026-08-07)

Found by sweeping for `fetch` call sites that mutate state without checking the response.
Of 34 such sites, most turned out to be false positives (helpers whose callers check, or
deliberately best-effort calls) - but this one is real, and it is on a safety feature.

`fetch` only rejects on network failure, never on an HTTP error status. Both live-location
handlers had a `.catch` and no `.ok` check, so every server rejection was invisible:

- **Position updates** were sent with `.catch(function () {})`. The update endpoint returns
  **400** when sharing has been turned off or the check-in has already concluded. So if a
  check-in was resolved from elsewhere - a partner marking the owner safe, say - the browser
  kept watching the GPS and posting a position every 30 seconds, each rejected, forever. The
  owner saw no indication. The partner watched a marker frozen at the last accepted fix.
- **The toggle** called `startWatching()` unconditionally and only caught network errors, so
  a refused toggle left the switch on and the GPS running while the server had recorded
  nothing.

The drift was visible inside the same file: the delete-check-in handler thirty lines above
correctly does `if (!r.ok) throw new Error()`.

Fixed by moving the logic to `shared/safety-live-location.ts` (22 tests, including one per
rejection status) which checks every response, reverts the toggle when the server refuses,
and warns the owner after two consecutive failed reports - two rather than one because a
single blip is noise, and once rather than every 30s because a warning that repeats is a
warning people learn to ignore. Recovery resets it, so a later outage can warn again.

**Not fixed, deliberately:** the remaining ~30 unchecked mutating `fetch` sites. Each needs
judging on its own - several are intentional fire-and-forget (the profile "skip setup"
button navigates whether or not the save lands) and a blanket change would be churn. Worth
a pass when someone can weigh them individually.

## Six writes reported success for requests the server had refused (2026-08-07)

Triage of the 34 unchecked mutating `fetch` calls found in the previous pass. The question
asked of each: *if this request fails, does the user believe something saved that did not?*

**Six said yes**, all sharing one shape - `fetch(...).then(r => r.json()).then(showSuccess)`
with no `response.ok` check. `fetch` resolves for 400s and 500s, so the success path ran
regardless:

| site | what the user saw on failure |
| --- | --- |
| `map/index.html` star rating | "Rating saved." - and the local pin cache updated with a rating the server rejected |
| `map/index.html` pin field edit | "Name updated successfully" |
| `location/index.html` media relevance | thumb stays marked; reverts on next load |
| `location/index.html` send-to-wiki | "N photo(s) queued for the wiki" - the inner `.json().catch(=> ({}))` swallowed a 500's HTML page and fell through to the success toast |
| `location/wiki.html` photo vote | vote stays applied; reverts on next load |
| `trips/detail.html` marker drag | marker stays where dropped |

The map page is the clearest case of drift: it already had a private `_fetchJson` doing
`if (!resp.ok) throw`, used for its GET reads - while the two PATCH writes in the same file
did not use it.

That helper is now `shared/fetch-json.ts` (25 tests), which throws on non-2xx carrying the
server's own message where it sent one, handles 204 (calling `.json()` on an empty body
throws, which would turn a successful DRF delete into an apparent failure), and applies the
CSRF header. The map's private copy is a two-line shim over it.

**Judged fine and left alone**, with the reasoning recorded so the next pass need not
re-derive it: geolocation visit tracking, map position and dark-mode preference saves (all
explicitly best-effort - nothing is claimed to the user), the profile skip-setup button
(navigates either way by design), and `e2ee-client.ts` (its `postJson` is a helper whose
callers check; the login path checks `.redirected`). The media-sort preference now warns on
failure since it had no handler at all.

## Bulk writes silently skipped the receivers that maintain derived state (2026-08-07)

First backend unit after five frontend ones. Three checks on the Celery/transaction layer came
back **clean**, which is worth recording so nobody re-runs them: of 71 `safely_enqueue_task`
call sites, **none** dispatch inside a `transaction.atomic()` block without `on_commit`
(verified with an AST walk, after a naive line-based scan proved unreliable for the
`@transaction.atomic` *decorator* case); there are no `select_for_update()` calls outside a
transaction; and no Celery task is handed a model instance instead of a pk.

The real finding was elsewhere. `bulk_update`/`bulk_create` issue raw SQL and never fire
`post_save`, so any receiver maintaining derived state is skipped. Four sites did this:

**1-3. `Label` bulk writes left the map pin cache stale.** `Pin.icon_source_label()` picks the
winning label by `-order`, so a label's *order* decides which icon and colour a pin draws -
not just its icon field. `refresh_map_pin_cache_for_label` exists precisely to invalidate the
server-side pin cache when that changes, and its own docstring says pins would otherwise "keep
serving the old baked-in icon/color until the cache TTL lapsed". All three bulk paths went
straight past it:

  - `controllers/organize.py` - drag-to-reorder labels
  - `external_api/views_labels_bulk.py` - the API's reorder endpoint
  - `external_api/views_labels_bulk.py` - the API's bulk edit, which writes `icon`, `color`,
    `order` and `description` directly. The most direct case of all.

So a user reordering their labels saw the map keep drawing the old icons.

**4. Copying a pin list into a trip never reached the calendar.** `copy_list_pins_to_trip`
uses `bulk_create`, so `sync_trip_on_activity_save` never fired and the new activities never
reached an auto-synced Google Calendar until some unrelated activity was saved.

Fixed by giving the label receiver a reusable `refresh_map_pin_cache_for_label_ids()` (one
query for many labels, `distinct()` so a pin carrying two changed labels is refreshed once)
and calling it from all three bulk paths, and by making `queue_calendar_push` public and
calling it once after the bulk_create. 8 tests, including one establishing the premise that
reordering really does change which icon a pin draws - without that, refreshing the cache
would be pointless.

**Worth generalising:** any new `bulk_create`/`bulk_update` on a model with `post_save`
receivers needs this same audit. `Pin` alone has six.

## A guard for the bulk-write class, which immediately found a fifth site (2026-08-07)

Follow-up to the four bulk writes fixed in 8ed25a93. Whether a bulk write is dangerous is a
property of the *model*, not the call site, so a site that is safe today becomes a bug the
moment somebody adds a receiver to the model it writes - and nothing about that change would
look wrong in review. `tests/hypothesis/test_bulk_write_signal_guard.py` now fails the build
on any `bulk_create`/`bulk_update` targeting a model with live `pre/post_save` or
`pre/post_delete` receivers unless the site is listed in `REVIEWED` with a reason.

Design notes worth keeping:
- Receivers are read from **Django's live signal registry** (`signal.has_listeners(model)`),
  not by grepping for `@receiver`. That is what caught the fifth site: `Image` gets its
  `post_save` connected dynamically from the achievements `_SUBSCRIPTIONS` table, so no grep
  for a decorator would ever have found it.
- The allowlist is keyed by `(file, model, operation)`, **not line number**, which would churn
  on every edit above the call and train people to update it without thinking.
- Two "guard the guard" tests assert the scan still finds call sites and the registry lookup
  still returns receivers. These earned their place immediately: the first version of the test
  had a wrong `PACKAGE_ROOT` and scanned **zero** files, which made the real assertion pass
  vacuously. Without those two tests it would have been committed green and guarded nothing.

**The fifth site (`pin_sharing.py`, `Image.bulk_create`) turned out to be correct as-is** -
firing that receiver would credit the *recipient* of a share with a photo-upload streak day
for photos they did not take. It is recorded in `REVIEWED` with that reasoning. Its one loose
end: the recipient's photo-count metric is not invalidated at copy time, and self-heals only
on their next photo action. Left alone as a product question, not a defect.

### But following that thread found two real bugs in the same function

`create_pin_from_share` builds the recipient's `Image` rows field by field, and any field it
omits silently takes the **model default** instead of the source row's value. Two of those
defaults actively misdescribe the copy:

- **`source` defaults to `UPLOAD`.** Resharing a pin whose gallery held a materialised Yelp,
  Wikimedia or Smithsonian photo filed it as the recipient's own upload. `ImageSource` is what
  drives the Media section's per-source tabs, so the photo also landed in the wrong tab.
- **`media_type` defaults to `PHOTO`.** A shared video was recreated as a photo, which renders
  through `<img>` instead of the player - a broken image.

Both fixed by copying `source`, `media_type`, `media_source_key` and `media_item_key`. The
context FKs it omits (`wiki`, `visit`, `safety_checkin`, `direct_message`, `pin_suggestion`)
are correctly dropped - they belong to the sender's row, not the copy.

## The Pin share copy dropped four properties, and now cannot drop a fifth (2026-08-07)

Same technique as the Image copy: load the model, diff its concrete fields against the
kwargs the copy passes, judge each omission. `create_pin_from_share` names 28 of `Pin`'s 43
fields by hand; 11 were omitted. Seven of those are correct (the recipient's own slug, view
and visit state, dismissal flags, the wiki cache). Four were not:

- **`pin_type_is_user_provided`.** The field's own comment says it guards `pin_type`
  "exactly like `name_is_user_provided` guards `name`" - and `name_is_user_provided` *is*
  copied. It is the only thing that makes `classify_detail_marker` return early, so a
  recipient's copy carried the sender's chosen type with no protection and the automatic
  building/parcel classifier was free to overwrite it. Precisely the outcome the flag exists
  to prevent.
- **`custom_icon`.** `Pin.effective_icon` checks `custom_icon` *before* `icon`, so a pin with
  a custom uploaded icon arrived looking like a different pin - while the function's own
  docstring promises it carries over "every user-visible property (name, icon, ...)".
- **`cover_photo`.** Correctly not copied verbatim (it points at the sender's `Image` row),
  but the recipient lost the hero image entirely even though the photos themselves are
  copied. Now mapped to the recipient's own copy by position, and left null when the sender
  shared only part of their gallery and not the cover.
- **`indoor_outdoor`.** The one place-property that did not travel while every sibling
  (`fences`, `alarms`, `cameras`, `security`, `signs`, `vps`, `plywood`, `locked`, the three
  dates) did. Currently inert - the field is groundwork with no UI - so this is consistency,
  not a user-visible fix. Recorded as such rather than overstated.

**The guard matters more than the four fixes.** All of these came from one structural fact:
the copy names fields by hand, so a field added to `Pin` later is simply absent and takes its
default, and nothing in review connects "add a column to Pin" with "update a function in the
sharing service". `SharedPinCopyCoversEveryFieldTests` now fails on any `Pin` field that is
neither copied nor listed in `NOT_COPIED` with a reason, plus two tests keeping that list
honest (no entries for fields that no longer exist, none claiming to skip a field the copy
actually passes).

Mutation-tested before trusting it: removing `slug` from `NOT_COPIED` makes it fail with
`['slug'] != []`. Worth doing - the first version of the bulk-write guard passed while
scanning zero files, and a guard nobody has seen fail is not yet known to be a guard.

## The field-by-field copy class is exhausted; albums audited (2026-08-07)

**Negative result, recorded so nobody repeats the search.** Task #35 planned to apply the
field-diff technique to trip cloning, wiki forking, album copying and the import paths. A
structural scan for the shape - calls with 6+ kwargs where most values are attribute reads
off one source object - found only **four** across the whole tree, and two are the paths
already fixed (`create_pin_from_share`'s Pin and Image copies). The other two are a
model-bakery recipe and a WebAuthn credential built from a library result, neither a
model-to-model copy. Checked the alternative idioms too: no `pk = None; save()` clone, no
`model_to_dict`, no `deepcopy` of instances; the `Model(**kwargs)` sites are serializer
creates. The features the task guessed at simply do not copy models field by field.

### The album feature is in good shape

Audited as the newest code on the release branch, and it holds up better than most of what
this audit has touched. Recorded because "we looked and it was fine" is worth knowing:

- Authorization is centralised in `_resolve_album_owner` and every view routes through it.
- The add endpoint re-scopes posted ids through `eligible_images_for`, so the picker's filter
  is not the only thing enforcing eligibility - the classic version of this bug.
- Setting a cover re-scopes through the album's own contents.
- `cover_image` is `SET_NULL` (with a comment saying why), *and* removal clears it, *and*
  `cover_from_images` falls back when the cover is not in the viewer's visible set. Three
  independent defences for the same failure.
- Duplicate membership is prevented in code *and* by a `uq_album_item` constraint.
- The broker-unreachable path in `_add_external` falls back to running the download inline -
  the exact failure mode that went unhandled elsewhere in this codebase.

### One real bug: a check-then-act race on adding photos

`add_images_to_album` reads which images are already in the album, then inserts the rest,
with no lock and no transaction. `uq_album_item` correctly prevents the duplicate row, but
the unguarded insert turned the loser of that race into an `IntegrityError` - a 500 rather
than a no-op.

Not hypothetical: there are two callers, and one is the Celery task
`cache_media_item_into_album`. Celery delivers at least once, so a redelivered task races
both itself and the user adding the same photo from the picker once it materialises.

Fixed with `ignore_conflicts=True`, plus counting the rows actually inserted rather than
returning `len(to_add)` - with conflicts ignored, the length over-reports what the call did,
which would have shown "2 photos added" when one was already there.

## Check-then-act sweep: one fix, and a hypothesis that was wrong (2026-08-07)

Following up the album race (42a178fa) by looking for the same shape elsewhere: read what
exists, insert the difference, with a unique constraint behind it.

**A wrong hypothesis, recorded so nobody re-derives it.** The first scan listed 77 models
with a multi-column unique constraint and flagged 17 unprotected creates. It looked like
`add_group_members` was a *deterministic* bug: `GroupChatMembership` is documented as one row
per member *stint*, re-adding is meant to create a brand-new row, and a `(group, profile)`
constraint would make that impossible - so re-adding anyone who had left should 500.

It does not, because that constraint is **partial**: `(group, profile) WHERE left_at IS
NULL`. That is exactly the right schema - one active membership, unlimited historical stints
- and my scan had simply not looked at `condition`. **27 of the 77 constraints are partial**,
so any future sweep must read the condition before concluding anything. The group-chat stint
behaviour is correct, and `test_group_chats.py` already covers both halves of it
(`test_readding_creates_new_stint`, `test_rejoined_member_does_not_see_absence_window`); the
tests I had written were deleted rather than committed as duplicate coverage.

**What actually discriminates a real instance from a theoretical one is a Celery caller.**
The album bug mattered because `cache_media_item_into_album` is a task and Celery delivers at
least once, which makes "two runs at once" ordinary rather than a rare interleaving. Applying
that filter to the 17 candidates leaves exactly one: `generate_keywords_for_image`, whose
only caller is the `generate_photo_keywords` task.

It deletes a provider's existing keywords and inserts the new ones. The delete does not
isolate it from another worker doing the same, so the second insert hit
`uniq_image_keyword_per_source` and failed the task, leaving that provider's keywords
missing until something re-ran it. Fixed with `ignore_conflicts=True`.

The remaining 15 need two genuinely simultaneous user actions (two managers adding the same
person, a double-clicked "add to list"). Left alone deliberately: the failure is a 500 on one
of two racing requests, the constraint keeps the data correct either way, and blanket-editing
15 call sites would be churn of the kind this audit has been avoiding. Worth revisiting only
if one of them acquires a Celery caller.

## Deleting your own photo silently broke it for everyone you shared it with (2026-08-07)

Found while auditing the quota feature (recently changed on this branch). The quota work
itself is sound - usage is always computed live rather than cached, so there is no staleness
bug; `materialize_media_item` really does stamp `EXTERNAL_MEDIA`; `_save_enriched_image`
attaches no profile so it is charged to nobody; and the quota is enforced on 15+ upload
paths. A scan for `Image`-creating functions with no quota guard returned only three, two of
which are correct by design.

The third was `create_pin_from_share`, and following it up found something worse than a
quota gap.

**The bug.** Sharing a pin copies its photos by assigning the *same* storage key
(`image=image.image.name`) - the bytes are deliberately not duplicated, so one file backs
several `Image` rows. But every deletion path called `image.image.delete(save=False)`, which
removes that file outright, with nothing checking whether another row still points at it.

So: share a pin, the recipient accepts, you later delete that photo from your own gallery -
and the recipient's copy becomes a broken image. No error, no log line, and the recipient's
row still exists so nothing looks wrong until the image fails to load.

Fixed with `services.media.images.delete_stored_file`, which removes the file only when no
other row references it, and takes an `also_deleting` set so a bulk delete does not count
rows inside its own batch as references. Routed all seven deletion sites through it
(pin gallery, wiki gallery, bulk gallery delete, organize queue, safety check-in, two
external-API endpoints, and the DM hard-delete task). Five tests, including two that guard
against over-correcting: an unshared photo's file must still be deleted, and the file must
go once the last row referencing it goes.

**Not changed, deliberately.** The recipient of a share is charged full quota for a file that
consumes no new storage, and share acceptance is the one `Image`-creating path with no quota
check at all - so accepting shares can silently push someone over quota, after which their
own uploads are refused with an error about photos they did not upload. Whether to exempt
those rows, charge them, or block the accept is a product/billing decision, and
`QuotaExemption` is explicitly scoped to "storage the whole community benefits from", which a
received share is not. Flagged rather than decided.

## External API audit: no changes warranted (2026-08-07)

A full unit spent on the external API's permission, scope and throttle layers found nothing
to fix. Recording what was checked and how, because the useful output of a clean audit is
knowing not to repeat it.

**Scope coverage is complete.** All 205 view classes under `external_api/` declare
`required_scopes` somewhere in their ancestry. Verified by walking the AST and resolving
inheritance transitively.

**Everything fails closed, in three independent places.** `credential_grants` refuses an
empty requirement rather than reading it as "nothing required"; `HasApiKeyScope` defaults a
missing `required_scopes` to an empty set, which that refusal then denies; and
`ExternalApiView.required_scopes` returns an empty set for a method with no entry. A view
added without a declaration is unreachable to credentials, not open to them.

**The PAT/OAuth2 split is enforced at check time, not at grant time.** `OAUTH2_ONLY_SCOPES`
is rejected in `credential_grants` even if a key somehow carries it, so a hand-edited row or
a future scope-picker bug cannot hand a bearer key access to end-to-end encrypted DMs.

**Object-level ownership holds.** A scan for handlers doing a lookup with no owner-scoping
token returned three; all three are false positives that scope through a base-class helper
(`_get_checkin`, `resolve_solo_session`) and then filter by that already-scoped parent.

**Throttle tiers derive from the same declaration that gates access**, so there is no second
list to drift, and an undeclared method lands in the *tighter* write tier. Only two views
override the tier and both are documented. The one shape worth checking - a read-tier GET
that triggers paid external calls - is `PinPanelDetailView`, and it is bounded twice over:
`schedule_panel_fetch` is single-flight per (source, pin) via a cache marker, and the fetch
itself runs under the per-service rate limiter (`RateLimitExceededError` is caught in
`external_data`).

### A lesson about these scans, learned twice

Two scans in this audit produced confident wrong answers because they matched on syntax
without handling a variant:

- The unique-constraint scan ignored `UniqueConstraint.condition`, so 27 *partial* constraints
  read as absolute - which is what made a correct group-chat design look like a bug.
- This scan resolved base classes from `ast.Name`/`ast.Attribute` only, so six views
  inheriting a *subscripted generic* (`PinSubResourceView[PinNote]`) reported no bases at all
  and looked unprotected.

Both times the fix was to widen the scanner, and both times the first result was plausible
enough to act on. Any structural scan here should be re-run against a known-good and a
known-bad example before its output is trusted.

## The data export disclosed trip members the app masks on screen (2026-08-07)

Audited the import/export paths. Most of it is exemplary and needed no changes - recorded
here so it is not re-audited:

- **Archive handling is properly defended.** No `extractall` anywhere; members are iterated
  and extracted individually. `_safe_basename` keeps only the basename (so there is no path
  left to traverse) *and* rejects `..`; symlinks are skipped in both ZIP (via `external_attr`
  mode bits) and TAR (`isfile()` only); there are per-file, total-uncompressed and file-count
  caps; and both formats read one byte past the declared size to catch a compression-ratio
  attack where the header lies.
- **The import refuses to write on someone else's behalf.** `_resolve_import_target` requires
  a pin target to resolve to the importer's *own* pin and a wiki target to pass
  `location_visible_to`, with the threat named in its docstring.
- **Friendships import as requests, not facts.** `_import_connections` re-creates each
  outgoing row through `Friendship.request` rather than honouring the exported status, so a
  crafted archive cannot forge an ACCEPTED friendship and grant itself friend-level access.
  The stated principle is the right one: "an import may only re-create actions the importing
  user could take themselves through the UI".

**The bug was on the export side.** `_export_direct_messages` passes each conversation
partner through `display_identity_for` and says why in its docstring - "an export never
reveals a partner's name/avatar beyond what the user could currently see on screen (e.g.
after being blocked or a privacy change)". `_export_trips` did not apply that rule: it wrote
`p.user.username` for every trip member and for the creator, while the trip page resolves
those same people through `resolve_visible_identities` and masks the ones the viewer may not
identify.

So a user could export their data and read the username of a co-member whose profile
visibility hides them - `NO_ONE`, or any setting the viewer doesn't satisfy. The codebase
already agreed this was wrong; only this one function had not been told.

Fixed by resolving member and creator names through `resolve_visible_identities`, the same
call the trip page makes. `member_uuids` is still exported for masked members: it carries no
name, and the import's re-invite step reads *that* field rather than `members`, so masking
the names cannot break the round trip - verified against `_import_trips`, which reads
`member_uuids` and never `members`.

Seven tests, including one asserting the DM export still masks (it already did - a guard so
the two exports cannot drift apart again) and one asserting masked members are still *listed*,
since dropping them would leak a different fact: how many people are on the trip.

## Global search named people the rest of the app masks (2026-08-07)

Generalising the trips-export leak (ea1a62db). If one surface had missed the
identity-masking rule, others might have, so this was a sweep rather than a guess: find
every function that emits another party's `username` without calling a masking helper. 48
matches, most correct by design - `__str__` for admin, notification text addressed *to* the
person concerned, URL and query parsing that merely mentions the word.

**Three were real, all in global search.** Search results name other people, and every other
surface that does resolves the name first:

  messages page + DM export      display_identity_for
  trip comments                  resolve_visible_identities
  pin/wiki comment list          resolve_visible_identities (the template branches on
                                 the `is_masked` it sets)

`DirectMessageSearchProvider` built `title=f"{direction} {other.username}"`, and
`CommentSearchProvider` built both its subtitles from `.username` - for pin/wiki comments
and for trip comments. So a user could type a word into the search box and read the username
of a conversation partner or comment author whose profile visibility hides them; the page
rendering those very same rows would have shown "Member 2".

Worth noting the reach: global search is also exposed through the external API, where the DM
provider is gated behind `messages:read` (an OAUTH2_ONLY scope) precisely because it returns
message content. The names were leaking through the same endpoint.

Fixed with one `_display_names` helper used by all three call sites, resolving a whole batch
in a single call - the resolver recomputes the viewer's allowed-subject set per invocation,
so per-row resolution would have been both wrong-shaped and slow. Six tests: two that fail
without the fix, two asserting a *visible* author's name still appears (masking everything
would be a different bug), and one asserting rows are still returned - the searcher is
entitled to find the comment, just not to learn who wrote it.

### Two existing tests were asserting the leak

`test_messages_from_person_finds_conversation_without_text_terms` and
`..._excludes_other_conversations` both checked `self.alice.username in result.title`. With
masking applied they fail with `'alice' not found in 'From Member 1'`.

Those tests are about *filtering* - that a person-scoped query returns only that person's
conversation - and were using the title as a convenient proxy for "whose conversation is
this". The proxy was the leak. They now assert on the result's URL, which carries the
partner's profile slug, so the original intent is tested without requiring the name to be
disclosed. Rewriting a test to match new behaviour deserves suspicion, so to be explicit:
the assertion that changed was incidental to what each test is named for, and the fixture
profiles are genuinely masked from one another (they are unrelated `baker.make` profiles),
which is why the leak was visible there at all.

### One refinement the failures prompted

The DM provider now resolves through `display_identity_for` rather than the generic
`resolve_visible_identities`. Both make the identical visibility decision - the former
delegates to the latter - but it labels a hidden partner "Former contact", which is what the
inbox and conversation header already call them. The generic resolver would have said
"Member 1" in search and "Former contact" in the inbox for the same person.

## Reply/reaction notifications named people the thread masks (2026-08-07)

Third unit on this class, and the first instance where the leaked name leaves the app.

`notify_reply` and `notify_reaction` built `title=f"{actor.username} replied to your
comment"` and `message=f"@{actor.username} ..."` from the raw username, while the comment
list resolves authors through `resolve_visible_identities` and the template renders the
masked name. So the same person was "Member 2" in the thread and their real username in the
notification about that thread.

**Why this one is worse than a page.** A `NotificationLog` insert is picked up by
`enqueue_native_push` and delivered to the recipient's registered devices, and
`notification_text_alerts` builds an SMS body straight from `notification.title`. The name
reaches a lock screen and a text message - places the app's own masking cannot reach back
into.

Fixed with an `_actor_names` helper resolving the actor for that specific recipient. It
returns the name *and* a handle, because `message` used an `@name` mention form: "@Member 2"
reads like a real mention and is not one, so the `@` is applied only when the recipient may
actually see who the actor is. The rule is now stated in the module docstring alongside the
other three it already lists ("never notify someone about their own action", "honour the
delivery preference", "one deep-link builder").

Seven tests, including one that the notification is still *sent* - masking the name must not
suppress the event, since the recipient is entitled to know someone replied - and one that
the deep link is unchanged.

### Where the class ends

The remaining sweep hits were checked and are correct as they stand. Safety check-in contacts
and safety-chat participants are a closed set the owner explicitly established, and removing
a partner revokes their access (`remove_checkin_partner` force-closes the socket), so there
is no lingering-access case of the kind `display_identity_for` exists for. `__str__` methods
are admin/debug surfaces. Notification text addressed *to* the person concerned must name the
other party - that is the message.

Four instances across three units: trips export, three global-search providers, and these two
notifications. The rule is real and applied in six places; it is still not enforced anywhere,
which is what task #38 weighs.

## Plugin/panel data paths: no changes warranted, one latent trap documented (2026-08-07)

Audited the panel data paths, looking specifically for the shape that would matter most:
panel results are cached per **location** (`LocationCache` keyed on `(location, source)`, and
`PanelSource.scope` defaults to `loc{location_id}`), so any panel whose data is *user*-
specific would serve one user's data to another.

**There is no such panel.** All 34 panel sources use the default location scope, and every
one of them fetches a property of the place - elevation, parcel, weather, Wikipedia, imagery.
The per-user-credential integrations that would be dangerous here (Immich, Google Photos,
Google Calendar) are not panel sources at all. Five classes matched a "per-user" grep; four
were the word "credential" appearing in a comment.

Also verified: `panel_visible_to` is the single decision shared by the web tab strip and the
external API - a feature-gated panel cannot be visible on one surface and hidden on the other
- and it only returns True unconditionally when the source declares no required feature.

### The one real hazard, at a seam rather than in current behaviour

`plugins.builtin.nominatim` calls `update_location_name_from_external_sources(location,
profile=pin.profile)` during its fetch, which reaches `default_name_resolver(profile=...)`.
Today that parameter is **unused**, and the resolver's docstring is explicit that name
resolution is "an intentionally system-driven decision - individual users cannot override the
source ordering with their own preference". So nothing is wrong now.

But the parameter is documented as a seam for a future profile-aware resolver, and a future
author consuming it would be walking into a trap: panel fetches are single-flighted per
*location*, so several users viewing the same place produce one fetch, and the profile it
carries is whoever's poll claimed the flight marker first. A resolver honouring that profile
would make a shared location's name depend on which user loaded the page first,
non-deterministically - and it would look perfectly reasonable in review.

The warning now lives in the seam's own docstring, where someone about to consume the
parameter will read it, rather than only here.

## Full-suite verification of the whole session: 10,162 passed, 0 failed (2026-08-07)

**10,162 passed, 0 failed, 1,429 subtests passed, in 59:45.** No failure line in the output.

This one carries **no caveat**, unlike the run recorded earlier (4259df61), which was taken
while files were being copied into the container and so could not claim to prove any single
commit. This time, before starting: the container was synced to HEAD, the one test file
deleted during the session (`test_group_membership_rejoin.py`, removed as duplicate coverage)
was deleted from the container too, `__pycache__` was cleared, and the container's Python file
list was diffed against `git ls-files` to confirm they matched. Nothing was copied in during
the 59 minutes. HEAD was `ae5746dc` at both start and finish, with a clean working tree, so
this run proves that commit.

Frontend, verified host-side and therefore never touching the container: `tsc --noEmit` clean
and **372 tests passing** across 25 files.

For scale, the session as a whole: 66 commits, 258 files changed, +18,349/-4,793 lines, and
33 new test files. The suite grew from 10,087 to 10,162 passing tests.

## WebSocket consumers: no changes warranted (2026-08-07)

Audited the Channels consumers, chosen because real-time auth has a classic failure mode -
permission checked at connect and never again - and because a comment elsewhere in the
codebase mentioned a partner's permission being "only checked at connect time", which read
like a lead.

It is not one. The layer is complete:

- **Group names are derived from the authenticated identity, never from URL input.** The
  notification and DM consumers join `notification_group_name(profile_id)` /
  `direct_message_group_name(profile_id)` built from the connected profile, so a client
  cannot subscribe to someone else's channel by editing a path.
- **Scope is checked before any group is joined**, so a scope-refused connection never
  becomes a member an in-flight broadcast could be delivered to.
- **Every consumer re-validates its credential for the life of the socket.** The mixin's
  `start_credential_revalidation` is called by the notification, DM and game-session
  consumers and paired with `stop_credential_revalidation` in all three `disconnect`s; the
  safety chat consumer runs a richer `_revalidate_access_periodically` that re-checks the
  authorization *relationship* as well, and cancels its own task on disconnect.
  `_credential_is_still_valid` states the principle: "a socket must not outlive the authority
  that opened it", and checks each credential kind the way its own HTTP authenticator would -
  a stamped `revoked_at` for a PAT, a deleted or expired row for an OAuth2 token.

That last point matters most for the DM socket specifically, since `messages:read` is
OAuth2-only precisely so a leaked long-lived key cannot become a path into someone's DMs.
Revocation is the remedy for a leak, and a socket that ignored revocation would defeat it.
It doesn't.

### A narrow grep gave a confident wrong answer, for the third time this session

Searching the consumers for `_revalidate|_is_still_authorized|_credential_is_still_valid`
returned **zero** matches inside the notification, DM and game consumers, which looked like a
real inconsistency against the safety consumer - and would have been a serious one. They call
`start_credential_revalidation`, a name the pattern did not cover.

That is the same mistake as `UniqueConstraint.condition` and subscripted generic bases,
recorded earlier: a scan matched one spelling of a thing that has several. The rule already
written down - validate a scan against a known-good and a known-bad case before believing it -
would have caught all three, and did not get applied here either. Worth treating as a habit
rather than a note: when a scan reports that one component does something and its siblings do
not, the first hypothesis should be that the scan is wrong, not the code.

## Undoing a pin delete crashed when the place had been re-pinned (2026-08-07)

Audited the undo framework - the last substantive feature area untouched this session.
Migrations were checked first and are clean: `makemigrations --check` reports no changes, no
untracked migrations (the gotcha where an uncommitted one leaks into a dependency), and a
single linear chain to 0038.

**Ownership is enforced correctly.** `restore_undo_action` explicitly delegates the check -
its docstring says the caller is responsible for "checking it belongs to the requesting
profile before calling this" - which is the shape that produces a bug when one caller
forgets. All three callers honour it: `controllers/undo.py`, `external_api/views_undo.py` and
`controllers/pin_bulk.py` each resolve through `UndoAction.objects.for_profile(...)`.

**Two real bugs in `PinUndoHandler.restore`,** both from the same constraint:
`db_pin_unique_location_per_profile` - one *root* pin per location per profile. The handler
pre-checks every foreign key the batch referenced, and says why: "since recreating the row
would otherwise fail with an uncaught IntegrityError". It did not check this one.

1. **Delete a pin, drop a new one at the same place, hit undo → IntegrityError → 500.** An
   ordinary sequence, not a contrived one. Now refused cleanly as `UndoExpiredError` with a
   message that says what actually happened, matching how every other unsatisfiable restore
   in this handler behaves.

2. **Restoring a detail pin failed too, for a subtler reason.** Pins were created parent-less
   and adopted in a second pass (parents need their new pks first). The constraint only
   covers root pins - so a detail pin is a root pin for the duration of that gap, and
   collides with whatever root pin stands at its location. Fixed by creating parents before
   children and setting the link at creation, so a pin is never transiently a root pin it was
   never meant to be. Repeated passes rather than a sort, so an arbitrarily deep hierarchy in
   one batch still resolves.

Five tests. Beyond the two failures, they pin down that another profile's pin at the same
location does *not* block the undo (the constraint is per profile), that the replacement pin
survives a refused undo, and that an ordinary undo still restores.

## Two more undo handlers crashed on their own unique constraints (2026-08-07)

Following the pin handler's two crashes (e6e9de7c) into the remaining handlers, since they
share the pattern: recreate a stashed row, having pre-checked whatever would make the recreate
fail. Each restores a model with unique constraints of its own.

**`SavedFilter` - unique(profile, name). Crashed.** The handler had *no* pre-checks at all:
one line, straight to `objects.create`. Delete a saved filter, make another one with the same
name, hit undo - `uq_saved_filter_profile_name`, uncaught IntegrityError, 500. Now checks both
the owning profile's existence (which it also never checked) and the name.

**`Wiki` - location is unique, one wiki per location. Crashed.** This handler *did* pre-check
the location, creator and labels, and its docstring gives the right reason - but it checked
that the location still *exists*, not that it is still free. Delete a wiki, let the location
acquire another, undo - `dashboard_wikis_location_id_key`, 500. Worth noting this one is the
easiest to hit without trying: `Wiki.objects.get_or_create_for_location` creates wikis lazily
from unrelated code paths (photo enrichment, among others), so the location can reacquire a
wiki with no deliberate user action at all.

**`Trip` - slug is unique. Clean.** Tested the same way and it passes: the slug is regenerated
on save, so a reused trip name produces a fresh slug rather than a collision. The test is kept
as a regression guard, written to accept either a successful restore or a clean refusal - just
not an IntegrityError reaching the caller.

**`SafetyCheckin` - only uuid is unique**, which restore regenerates. Nothing to do.

That makes four crashes of one shape across three handlers. The shape is worth stating plainly
for whoever adds the next handler: *an undo handler must pre-check every constraint the
recreate can violate, not only that its foreign keys still resolve.* Three of the four
handlers that needed it got the foreign-key half right and the constraint half wrong.

## Account deletion and the constraint-recreate class: both clean (2026-08-07)

Two checks this unit, both negative.

**The "recreate into a changed world" class is exhausted outside undo.** The four undo crashes
all came from recreating a row whose constraint slot had been taken since. The other creators
of `db_pin_unique_location_per_profile` handle it: `apply_pin_share_response` re-checks
`find_profile_pin_near_location` *inside* its `select_for_update` block and only creates when
nothing is there, and `accept_pin_suggestion` filters on `parent_pin__isnull=True`, matching
the partial constraint exactly. The undo handlers were the gap, not the pattern.

**Account deletion is deliberately designed, and the catastrophic case is avoided.** Every FK
pointing at `Profile` was enumerated. The split is coherent rather than accidental:

- **Personal data cascades** - pins, images, direct messages, labels, albums, notification
  logs, credentials, key material.
- **Contributions to shared or community space are `SET_NULL`** - wiki edits, wiki creators,
  aliases, links, owners, property sales, article revisions, trip creators and activities,
  fact evidence, trivia submissions, group chat creators. A departing user does not erase what
  other people are still using.
- **`Pin.source_share` is `SET_NULL`**, which is the one that matters most: a sharer deleting
  their account would otherwise cascade `PinShare` deletions into *recipients' pins*. It
  doesn't. `PinShare.parent_share` is `SET_NULL` too, so a provenance chain truncates rather
  than corrupting - `resolve_origin_share` simply ends its walk early.

### One asymmetry, surfaced rather than changed

`Comment.profile` is `CASCADE` while `TripComment.author` is `SET_NULL`. Both are comments a
user wrote in a space other people share, and deleting an account therefore erases your pin
and wiki comments while leaving your trip comments in place, authored by nobody. One of the
two is probably not what was intended, but which one is a data-policy question - whether
deletion means "erase what I wrote" or "keep the conversation readable" - and not a call to
make from inside an audit. Recorded here for the owner.

## E2EE group messages: the cryptographic membership boundary depends on the server (2026-08-07)

`models/e2ee/group_key.py` states the design claim plainly: "Versioning is what enforces
membership boundaries **cryptographically**" - a removed member "is excluded from every later
version, so messages sent after their removal are unreadable to them". The server-side half is
well built: `needs_rotation` is computed by comparing the latest version's envelope set against
active membership, and the key endpoint refuses to store a version whose envelopes don't cover
that membership exactly.

**But nothing validates the `key_version` a client sends a message with.**
`create_group_message` checks only `key_version < 1` (alongside the blob checks). It never
verifies that the version exists, belongs to this group, or is the current one. The value is
client-supplied and stored verbatim, on all four send paths - the WebSocket consumer, the
external API, the web controller, and the share-a-pin-in-a-group path.

So a message can be encrypted with a **pre-removal** key version that a removed member still
holds an envelope for. Reachable benignly - a tab open across the removal, an offline outbox
replaying queued messages, an API client caching the version it last fetched - and reachable
deliberately: a remaining member can choose an old version specifically to make a message
readable by someone the group ejected, and the server will accept it.

**What actually protects post-removal messages today is the server**, not the cryptography: a
removed member has no active membership, so `visible_window` and the active-membership checks
never serve them the ciphertext. That is a real defence and the messages are not currently
exposed. It is, however, exactly the dependency end-to-end encryption exists to remove - it
would not survive a database backup, a leak, a server compromise, or a future bug in the
delivery gate, which is the threat model the feature is written against.

### Why this is surfaced rather than fixed

The obvious fix - reject any `key_version` that isn't the latest - is **not sufficient on its
own**. If nobody has rotated yet, the latest version is still the pre-removal one, and its
envelopes still include the removed member. The rule that would actually hold the stated
property is stronger: refuse to accept an encrypted group message while `needs_rotation` is
true, i.e. until some client has stored a version whose envelopes match the active membership.

That trades availability for the property. Rotation is client-driven, so between a removal and
the next client rotating, group messaging would be blocked - and an offline outbox would have
messages rejected on replay and need re-encrypting. Whether that trade is right depends on how
strictly the removal boundary is meant to hold versus how tolerant the product should be of a
lagging or offline client, which is a decision for the owner rather than an audit. `docs/designs/e2ee.md`
already documents a related deliberate trade (recoverability over forward secrecy), so there is
precedent for either answer being the intended one.

## Corrected the E2EE docstring's overstated claim, and 28 dead doc references (2026-08-07)

Two documentation corrections, both factual rather than judgement calls, so neither waits on
the open decision in task #40.

**The security claim now matches what the code enforces.** `models/e2ee/group_key.py` said
"Versioning is what enforces membership boundaries cryptographically", and
`docs/designs/e2ee.md` said the same. As the previous entry establishes, it does not - nothing
validates a message's `key_version`, and what actually keeps post-removal messages from a
removed member is the server's delivery gate. Both now say so, and the model docstring carries
an explicit instruction not to add a code path that relies on the version alone to keep a
removed member out. Leaving the original wording was the real risk: a docstring that promises
a cryptographic guarantee is exactly what stops the next developer from checking.

**28 source references pointed at documents that had moved:**

    docs/designs/drafts/spotguessr.md      -> docs/designs/drafts/spotguessr.md      (21 files)
    docs/designs/redata-cid-resolution.md   -> docs/designs/redata-cid-resolution.md  (6 files)
    docs/reports/overpass-mirror-test.md    -> docs/reports/overpass-mirror-test.md   (1 file)
    docs/designs/e2ee.md                    -> docs/designs/e2ee.md                   (8 files)

Same class as the seven dead `docs/designs/plugins.md` references fixed earlier in this audit: a
pointer that resolves nowhere is a dead end for whoever follows it, and these were pointing
away from documents that do exist.

**12 references left alone deliberately**, because fixing them would mean guessing:

- `docs/api-reference.md` (7) - the same filename is referenced elsewhere in this codebase as
  `../REData/docs/api-reference.md`, a sibling repository. Most of these sit in REData gateway
  files and are probably that, but `plugins/builtin/photon.py` is not a REData integration and
  more likely means Photon's own published API docs. Rewriting them would be inventing intent.
- `docs/notes/ai/completed.md` (4) and `docs/PROBLEMS.md/completed.md` (1, malformed) - no
  `completed.md` and no `prompts/` directory exist anywhere in this checkout, though
  `CLAUDE.local.md` describes them as where prior agents' work notes live. The target is
  genuinely absent, not moved.

### A fourth narrow-pattern error, caught before it did damage

The first scan reported **38** files with a dead `docs/api-reference.md` reference. The regex
`docs/[A-Za-z0-9_./-]+\.md` had matched the *tail* of `../REData/docs/api-reference.md` - a
path into a sibling repo, entirely valid. Re-running it anchored (`(?<![\w./-])`) cut the real
figure to 7. Had the first result been acted on, 31 correct cross-repo references would have
been rewritten into broken ones. That is the fourth time this session a pattern matched one
spelling of a thing with several, and the first where acting on it would have *introduced* the
bug rather than merely missed one.

## Closing verification: 10,174 passed, 0 failed (2026-08-07)

**10,174 passed, 0 failed, 1,429 subtests passed, in 57:59.** No failure line in the output.
Frontend verified separately and host-side: `tsc --noEmit` clean, 372 tests across 25 files.

Run under the same discipline as 4b5ff2bf, and for the same reason - six commits had landed
since that one, including real code changes to three undo handlers, and those had been
verified only by targeted `-k` selections. Container synced to HEAD and its Python file list
diffed against `git ls-files` before starting; `__pycache__` cleared; nothing copied in during
the 58 minutes; HEAD `610f97be` at both start and finish with a clean working tree. The run
proves that commit.

The count moved 10,162 -> 10,174: the twelve undo-restore conflict tests.

### What is left, and who it is left for

The substantive audit is finished. Four findings remain open and every one needs a decision
from the owner rather than more investigation:

- **Share-received photos and quota** (`#37`) - charged full price for a file consuming no new
  storage, on the only Image-creating path with no quota check.
- **Comment deletion semantics** (`#39`) - `Comment.profile` CASCADE vs `TripComment.author`
  SET_NULL; one of the two is probably not intended.
- **E2EE rotation enforcement** (`#40`) - the removal boundary is server-enforced, not
  cryptographic; the sufficient fix costs availability.
- **12 documentation references** - 7 probably point at the sibling REData repository, 5 at a
  `completed.md` that exists nowhere despite `CLAUDE.local.md` describing it.

Three further tasks were assessed and deliberately not done: a structural identity-masking
guard (`#38`) would mostly re-test surfaces already verified correct; routing the remaining
fetch call sites through `shared/fetch-json.ts` (`#32`) is churn with no bug attached; and
`base.html`'s last blocks (`#29`) are template-coupled, with the 937-line comment-map composer
needing a Leaflet stub - a project rather than an audit unit.

### The methodological lesson, stated once more because it recurred

Four times this session a structural scan matched **one spelling of a thing that has several**,
and each time the result looked authoritative:

| scan | missed | consequence |
| --- | --- | --- |
| unique constraints | `UniqueConstraint.condition` | made a correct group-chat design look like a bug |
| view base classes | subscripted generics (`View[T]`) | made six protected endpoints look unprotected |
| consumer revalidation | `start_credential_revalidation` | made three consumers look like they never re-check |
| doc references | `../SiblingRepo/docs/...` | would have broken 31 correct cross-repo links |

The first three wasted effort. The fourth would have *introduced* the bug rather than missed
one, and was caught only because the number - 38 dead references in a codebase this careful -
was implausible enough to check. The rule that would have caught all four is cheap: **validate
a scan against a known-good and a known-bad example before believing its output**, and treat
"one component does X and its siblings do not" as evidence the scan is wrong before concluding
the code is.

## Pin lists are now restorable from Undo History; two gaps remain documented (2026-08-08)

Coverage audit of the undo framework from the missing-feature angle: which user-facing
destructive deletes stash for undo, and which silently do not. Pins (all four delete paths,
including the DRF endpoint the map's delete dialog calls), wikis, trips, safety check-ins and
saved filters all stash. **Pin lists, labels, markup maps, albums and custom fields did not** -
five permanent deletes in an app whose pin-delete dialog promises "You can restore it from
Settings → Undo History".

**Implemented: `PinListUndoHandler`** (`services/undo/handlers/pin_list.py`), wired into both
delete endpoints (web + external API). A list is exactly what undo exists for - deleting one
destroys hand-built curation while the pins survive. Design points, following the rules this
audit established:

- Pre-checks `uq_pin_list_profile_name` and the owning profile before creating anything, per
  the constraint rule from the four handler crashes. The slug constraint cannot fire because
  the slug is regenerated rather than restored.
- Restores leniently where the missing piece was never part of the deletion: member pins
  deleted since are skipped (a list of survivors beats no list), and dead
  `source_saved_filter`/`markup_map` links are dropped - both are SET_NULL on their targets'
  own deletes, so this matches what would have happened to a live list.
- `smart_boundary` (GEOS geometry) round-trips as EWKT so the payload stays JSON-safe.
- The list-delete confirm no longer says "This cannot be undone", which had just become false.

**Documented for follow-up rather than implemented, in judgement order:**

- **Label** - the next most valuable, and more destructive than lists in one way: deleting a
  label silently strips it from every pin carrying it. A handler needs: the label's own fields,
  its `parents` m2m (hierarchy), the pk list from `Pin.labels.through` (which pins carried it),
  and its `LabelCustomization` rows. Restore pre-checks: profile exists, plus whatever
  name-uniqueness Label enforces per kind (check the model - the organize page suggests
  per-profile-per-kind). Pins that vanished are skipped like list members. The complication
  that makes this a deliberate follow-up rather than a quick add: `label_retire_redata_taxonomy
  _on_delete` fires on label delete (REData taxonomy retirement), so restore likely needs to
  *un-retire* or re-sync the taxonomy - understand that signal's contract first.
- **MarkupMap** - hand-drawn annotation data, clearly worth protecting. Complication: markup
  maps are shared (`MarkupMapShare`), attachable to comments/messages/pin lists, and deletes
  happen from three controllers. Serialize the geojson + style fields; decide whether shares
  are restored (probably not - the recipient relationship was severed) and whether
  comment/message attachments should relink (probably yes when the row still exists, same
  SET_NULL-lenient rule as lists).
- **Album / CustomField** - lower value: an album is a grouping of surviving photos
  (`AlbumItem` rows), a custom field's values cascade with it. Same handler pattern applies
  directly with nothing novel; do them if a user ever asks.

## Labels are now restorable from Undo History (2026-08-08)

Second of the two designed follow-ups from the undo coverage audit. The gating question -
whether the REData taxonomy retirement signal makes restore hazardous - resolved cleanly:
the signal pair is self-healing. Deleting queues a retirement
(`retire_redata_taxonomy_on_delete`), and recreating fires `post_save`, whose
`sync_redata_taxonomy_on_save` **upserts** a fresh definition. The parent relinks re-trigger
definition syncs via the m2m signal, and the pin re-assignments refresh the map pin cache the
same way any label add does. No special handling needed in the handler at all.

What the handler captures is the part deletion actually destroys: besides the label's own
fields, the cascade severs both directions of the `parents` self-M2M (children keep existing
but lose their link) and the label's assignment to every pin carrying it - visible state,
since label order decides which icon a pin draws.

One deliberate divergence from the other handlers: `Label` has **no unique constraints**, so
restore never refuses. A name reused since simply yields two labels, which the organize
page's merge tool already handles - refusing would be stricter than the app itself is. The
constraint pre-check rule still holds; there is just nothing to pre-check beyond the owning
profile.

Wired into all three delete sites (single, web bulk, external API bulk). Ten tests, including
the in-batch hierarchy relink (bulk-deleting parent and child together must link their two
*new* rows, not the dead pks - same shape as the pin handler's parent ordering bug).

Remaining from the coverage audit: MarkupMap (design documented in the pin-list entry),
Album, CustomField (low value, pattern applies directly).

## Markup maps are now restorable from Undo History (2026-08-08)

Last of the designed follow-ups from the undo coverage audit. A markup map is hand-drawn
work - shapes, arrows, labels, security indicators placed one by one - and deleting it
cascaded away every `PinMarkup` annotation, arguably the most expensive data any of the
handlers protect.

The shares decision, made and recorded in the handler's docstring: **`MarkupMapShare` rows
are deliberately not restored.** Recreating them would silently re-expose the map to every
past recipient; the delete severed those relationships, and an undo brings back the owner's
work, not other people's access to it. The owner can re-share. Inbound attachments (a
comment's or DM's `markup_map` FK) are SET_NULL and already nulled before any stash could
run, so they are out of scope by construction.

Wired into the explicit delete view and both comment-delete sites that destroy an attached
map as a side effect - the comment itself is not restorable (task #39 pending), but deleting
a comment must not silently destroy the drawing attached to it. Annotations restore with
their original authors; one whose author's account is gone is skipped rather than
misattributed, since its profile FK is CASCADE.

**Undo coverage is now complete for every hand-curated deletable**: pins, wikis, trips,
safety check-ins, saved filters, pin lists, labels, markup maps. Album and CustomField remain
deliberately uncovered (an album is a grouping of surviving photos; a custom field's values
cascade with it) - the handler pattern applies directly if ever wanted.

## The bulk-write guard caught the audit's own new code (2026-08-08)

Full-suite verification of the three undo commits: **10,197 passed, 1 failed** at 43c26dd6,
and the failure was `test_bulk_write_signal_guard` flagging `PinMarkup.bulk_create` in the
markup-map undo handler - the guard built earlier in this audit, doing precisely what it was
built for, against precisely the person who built it.

And it was right on the merits, not just procedurally. The skipped per-item signals defer a
pin-inference resync of the parent map. The map's own `created` save also defers one, which
*looks* like cover - but under autocommit that `on_commit` callback can run before the
annotations are bulk-created, syncing a drawing of zero items, with the skipped per-item
signals never triggering a later pass. An ordering hazard that review would not have seen.

Fixed per the guard's own instruction (do the work explicitly): `signals.py` now exposes
`defer_pin_inference_sync` as a public seam, the handler calls it right after the
`bulk_create`, the guard's REVIEWED entry records the reasoning, and a test proves the resync
is scheduled at a point where the restored items already exist
(`captureOnCommitCallbacks(execute=False)`, then run the callbacks under a mock).

Also corrected `docs/FEATURES.md`'s undo entry, which was stale twice over: it called the
framework "cache-backed" when the payload deliberately lives on the durable `UndoAction` row
(the model docstring explains a cache entry can vanish before its window ends), and it listed
four covered models out of the current eight.

Frontend and ruff verified clean alongside. The one failure is fixed and the affected
selection (265 markup/undo/bulk_write/inference tests) passes; the next full run will confirm
the whole.

## Confirming full run clean; undo restores now refresh the map cache (2026-08-08)

**10,199 passed, 0 failed, 1,429 subtests, in 58:03** at `f8e0c23d`, under the usual
discipline (container synced and file-list-verified first, nothing copied during the run).
This confirms the guard-catch fix: the previous run's one failure is gone and the suite has
grown 10,174 → 10,199 across the three undo handlers and their guard entry.

**One client-side bug found and fixed while the run executed** (host-side only, so the run's
claim is unaffected). Investigating the roadmap's open UL-279 showed its server half already
fixed by 8ed25a93 and the organize/label pages covered by `organize.ts`'s dirty-flagging -
but the **Undo History restore path** was not covered. The map's client pin cache polls only
the newest pin's `updated` timestamp: a restored *pin* advances that and is picked up, but a
restored **label** returns its pin assignments through the M2M without touching any pin row,
and a restored list or markup map likewise moves nothing the poll can see. So a restore from
Settings → Undo History left the map rendering the pre-restore world until the cache expired.
This matters more now than when UL-279 was filed, since label/list/map restores only became
possible this session.

Fixed as `shared/undo-map-refresh.ts`: a delegated `htmx:afterRequest` listener flagging
`ul_pins_dirty` after a successful POST to `/undo/…/restore/`, installed once by `core.js`
rather than inlined in the undo partial - the partial is re-swapped after every restore, so an
inline listener would stack a copy per swap, the exact trap this session's test harnesses hit
three times. Seven tests. Frontend suite 372 → 379.

Also checked and deliberately not touched: UL-277 (per-source freshness windows) is parked at
the owner's explicit request in this file; the nearest open Tier-1 item, but not mine to
unpark.

## Filter-view defects cluster: triaged, 3 of 5 already resolved (2026-08-08)

Roadmap Tier-1 item 5 listed five defects and prescribed one agent owning the page. Static
triage shows the list is mostly stale:

- **Icon picker dead - already fixed.** `entries/saved-filter-detail.ts` exists solely to fix
  it, and its comment names the root cause: the page rendered the shared `_icon_picker.html`
  partial but never loaded anything defining `window.IconPicker`, so the trigger's onclick
  threw silently. The entry installs the global picker.
- **Badge picker parity - already fixed** (2026-07-23, browser-verified; see the label-picker
  extraction entry above). Both picker shapes now come from `shared/label-picker.ts`.
- **Preview doesn't refresh on criteria change - already fixed.** The detail page has a
  debounced live preview on form change/input with a supersession token (the same
  stale-response pattern this audit fixed in mention-autocomplete), and `_sfSaveRegions`
  dispatches a synthetic bubbling `change` precisely because property assignment fires no DOM
  event - region edits refresh the preview too.

**Polygon resurrection - mechanism identified, deliberately not blind-fixed.** The page's own
logic is correct: `draw:created/edited/deleted` all persist, and loading round-trips through
`_sfRegionLayers` properly. The resurrection is stock leaflet-draw semantics: delete mode is
transactional, click-deletions commit only via the sub-toolbar's small "Save" action, and
disabling delete mode (e.g. by clicking the polygon tool to draw next) **reverts** uncommitted
deletions - `draw:deleted` never fires, so the layers genuinely return, exactly matching the
report "deleted polygons resurrect on next draw". The fix is to stop using leaflet-draw's
remove tool (`edit.remove: false`) and implement immediate-commit deletion - a toggle that
removes a clicked layer from the feature group and calls `_sfSaveRegions()` at once. Not
shipped from this environment because it changes live map interaction behaviour, which needs a
real browser to verify; the roadmap entry carries the design.

**Page overflows footer** - CSS-level, needs a browser to reproduce; nothing checkable
statically.

## Verified: this session's frontend work ships on deploy (2026-08-08)

A closing verification worth writing down because the standing "never run `bun run build` on
this host" rule could otherwise imply the opposite. The tracked `static/dashboard/js/*.js`
bundles have not been rebuilt this session, and `core.ts` gained a dozen new shared modules -
so do deploys serve stale bundles missing all of it?

No. `src/bin/init.py` (invoked by the container entrypoint before the healthcheck can pass)
calls `build_frontend()` at every container start: `bun run sass` + `bun run build` (dev) or
`bun run deploy` (staging/production), then `collectstatic`. Bundles are compiled fresh from
the TypeScript source inside the container, with the container's own toolchain. The tracked
static bundles are dev-host artifacts; the host-side "bun run build is broken / clobbers
tracked output" note is about this checkout's host environment, not the deploy path - and the
dev container being up and healthy is itself proof the in-container build succeeds.

## Where the audit stands (2026-08-08)

Every area reachable from this environment has now been covered. What remains needs something
this environment does not have:

- **The owner's decision**: share-quota treatment (#37), comment deletion semantics (#39),
  E2EE rotation enforcement (#40), the 12 unresolvable doc references, UL-34's vague repro,
  and unparking UL-277.
- **A browser**: the leaflet-draw immediate-commit deletion fix (designed, in the roadmap),
  the saved-filter page's footer overflow, and UL-353/UL-271's repro detail.

Nothing on the remaining task list clears the bar of "worth doing without those inputs" -
#38 would re-test surfaces verified correct, #32 is churn with no bug attached, #29's last
blocks are template-coupled or need a Leaflet stub. Stated per the standing instruction to
say so plainly rather than manufacture work.

## OPEN 2026-08-12: HEIC/HEIF uploads cannot have their GPS stripped

Discovered while fixing the TIFF/AVIF GPS-strip leaks (both fixed; see the audit report).

`content_sniffing._IMAGE_EXTENSIONS` accepts `heic`/`heif` uploads, and the codebase notes in
several places that HEICs "reach the gallery routinely". But `pillow-heif` is not installed, so
Pillow cannot open them at all: `PILImage.open` raises inside `_process_photo_upload`, the caller
logs "Image metadata extraction failed" and returns, and the file is stored untouched.

For a user who has turned off `track_pin_visits` (which is what drives `strip_location`), that
means the app accepted a photo, promised not to keep its location, and stored a file with the
full GPS IFD intact — the same failure just fixed for TIFF and AVIF, but not fixable the same way
because the format cannot be decoded at all.

Not fixed here because both routes are the owner's call, not a bug fix:

1. **Add `pillow-heif`** — HEIC then flows through the existing pipeline (it would need adding to
   `_EXIF_REWRITABLE_FORMATS`, and `_FORMAT_EXTENSIONS`). Costs a new dependency with a bundled
   libheif; note the licensing on libheif/x265 before adopting.
2. **Refuse HEIC when a strip is required** — reject the upload for profiles with
   `track_pin_visits` off, with a message telling the user to convert first. No new dependency,
   but it rejects the default iPhone photo format for exactly the privacy-conscious users least
   likely to accept that.

Whichever is chosen, the invariant worth keeping is the one `test_gps_strip_by_format.py` now
encodes: a format the app *accepts* must either be scrubbable or refused, never silently stored
with the coordinates the uploader asked to have removed.

## PARTIAL 2026-08-12: E2EE - the client trusts server-supplied Argon2 parameters (server side fixed; client not)

`password_wrapped_secret` is the user's private key wrapped under a key derived from their
password, and the security claim — stated in `e2ee-crypto.ts`'s own docstring — is that the
server, which learns `authKey`, must remain unable to compute `wrapKey`. How expensive it is to
brute-force that blob offline is decided entirely by the Argon2 `opslimit`/`memlimit` used.

**Fixed here (server side).** `E2EEEnrollView` took both values from the request and validated
only `> 0`, so a caller could enrol with `opslimit=1, memlimit=1`. It now enforces a floor of the
pinned defaults `(2, 64 MiB)`, still accepting stronger values so a future client can raise them
without a server change. No compatibility risk: the server default has never been anything else
(one migration, `0007`), and the real client sends exactly those constants.

**Not fixed — needs a coordinated client+server change.** `e2ee-client.ts` uses
`bundle.kdf_opslimit`/`bundle.kdf_memlimit` verbatim, with no floor, in three places. Two are
unwrap paths, where using the stored parameters is *required* for correctness. The third is a
re-wrap (`e2ee-client.ts`, the "password copy is stale" branch): it derives a fresh wrapping key
using the server-reported parameters and uploads the newly wrapped private key. A server that
reports weak parameters therefore gets the private key re-wrapped weakly, and — because the
`/rewrap` endpoint accepts only `password_wrapped_secret` and `password_wrap_salt` — the stored
parameters stay whatever the server said, so the weakness persists and future unwraps still work.

Fixing that properly means the re-wrap must choose its own parameters (the current pinned
constants, never the server's) *and* send them, with `/rewrap` updating the bundle. That is worth
doing — it would also upgrade any legacy bundle to current strength on its next re-wrap — but it
is a write path where getting it wrong locks a user out of their own key permanently, so it wants
review rather than an unattended change. Clamping the *unwrap* paths is not the answer: it would
make any bundle legitimately below the floor permanently unopenable.

## OPEN 2026-08-12: a password reset does not evict an intruder who minted an API key

Resetting a password invalidates every session (Django rotates the session auth hash), which is
what makes "reset your password" the standard response to a suspected compromise. It does **not**
touch `ApiKey` or django-oauth-toolkit `AccessToken` rows, and nothing else does either - there is
no revocation hook on password change anywhere in the codebase.

That matters because of a second gap: `controllers/api_keys.py::ApiKeyCreateView` mints a key
behind `LoginRequiredMixin` alone, with **no current-password proof**. So a session-only compromise
- a stolen cookie, a borrowed unlocked laptop - is enough to mint a long-lived credential, and the
victim's natural remedy does not remove it. The key keeps working with whatever scopes it was
given until someone notices it in the settings list and revokes it by hand.

Neither half is unusual on its own, and reasonable products differ (GitHub notifies rather than
revoking PATs on reset). What makes this worth recording is the asymmetry: this codebase *already*
demands a current-password proof for the three E2EE key-replacing endpoints - see
`test_e2ee_dual_auth.py::CurrentPasswordProofUnderCredentialAuthTests`, whose rationale is exactly
"an OAuth2 token grants send-and-read-messages, not replace this account's key material". The same
reasoning applies to minting a credential that can read the account's pins, photos and location
history.

**Not fixed here because both remedies are product decisions.** Revoking on password change is the
stronger option and silently breaks any legitimate integration the user has set up; requiring a
password proof to mint a key is the smaller change and matches the existing E2EE precedent, but it
is still a UX change to a settings flow. A middle option is to notify on both events, which this
app already has the notification machinery for.

## OPEN 2026-08-12: bulk-import paths skip the upload quota lock, which is fail-open anyway

`per_profile_upload_lock` exists because `quota_error_for_upload` reads current usage and the
caller creates the `Image` row afterwards - "N concurrent uploads from the same profile can each
pass the check before any of them commits". Its docstring tells callers to wrap the
check-then-create sequence in it.

Nine interactive call sites do (photo upload, DM attachments, article images, safety, tools,
visits, maps, consensus, photo uploads service). **Six do not**, and they are all the background
ones - `tasks.py` never imports the lock at all (four sites: Immich sync, Google Photos, and two
other fetch-and-store tasks), plus `services/pins/pin_suggestions.py` and
`services/import_export/import_data.py`.

Those are the paths where concurrency is *highest*: a bulk import fans out one task per image, so
many workers run the check for the same profile at once.

**Wrapping them is not the fix, which is why this is filed rather than done.** The lock is
deliberately fail-open - a caller that cannot acquire it logs a warning and proceeds - so under the
contention a bulk import actually produces, most workers would simply proceed without it. It
narrows the window for two near-simultaneous uploads; it does not bound a fan-out. Adding it to
these sites would look like protection while changing almost nothing.

The docstring already names the real fix: "true DB-level atomicity, which would need a dedicated
running-total column". A `Profile.storage_used_bytes` counter maintained by the same transaction
that creates the `Image` row would make the check exact for every path at once, and would also
remove the repeated `SUM(file_size)` scan that `get_storage_used_bytes` runs on each upload.
Sizing that (backfill, and keeping it correct across deletions and failed uploads) is a design
decision, not a refactor.

Fixed in passing: the lock released with a bare `cache.delete` guarded only by "did I acquire it",
so an upload slower than the 30s timeout - already having lost the lock to its successor - deleted
*that* upload's lock on the way out. It now uses the token-checked release from
`services.core.locks` (see the 2026-08-12 sweep-lock entry; same defect, same fix).

## URGENT 2026-08-13: commit `c3ae4911` cannot start - it imports five files it did not commit

Worse than, and separate from, the migration gap recorded below. `c3ae4911 audit` committed 139
files but left **five non-test modules untracked**, and 19 committed files import them:

| untracked module | committed importers |
|---|---|
| `models/abstract/labelled.py` | 1 |
| `services/core/locks.py` | 3 |
| `services/geo/distance.py` | 7 |
| `services/geo/longitude.py` | 6 |
| `services/pins/import_failure_guess.py` | 2 |
| `core/tests/oauth.py` | 6 (test files) |

And one **template**, which fails the same way one layer later - the app starts, then 500s the first
time the view renders: committed `controllers/pin_import_failures.py` sets
`_GUESS_PARTIAL = "dashboard/partials/memories/_pin_import_failure_guess.html"`, which is untracked.
`PinImportFailureGuessView` raises `TemplateDoesNotExist` whenever a guess is produced.

One of those sits directly on Django's model-loading path. Committed
`models/abstract/__init__.py:10` reads:

```python
from urbanlens.dashboard.models.abstract.labelled import LabelledModel  # noqa: E402
```

and committed `models/pin/model.py:100` declares `class Pin(..., abstract.LabelledModel)`. So a
fresh checkout raises `ModuleNotFoundError: No module named
'urbanlens.dashboard.models.abstract.labelled'` while importing models - before any view, task or
test runs. **The web app, every management command, both Celery workers and the whole test suite
fail to start.**

This is invisible in any working copy that still has the files on disk, which is every machine this
audit ran on.

**Fix** - add the five modules and both migrations (0040 before 0041):

```
git add src/urbanlens/dashboard/models/abstract/labelled.py \
        src/urbanlens/dashboard/services/core/locks.py \
        src/urbanlens/dashboard/services/geo/distance.py \
        src/urbanlens/dashboard/services/geo/longitude.py \
        src/urbanlens/dashboard/services/pins/import_failure_guess.py \
        src/urbanlens/dashboard/migrations/0040_gotify_token_fail_soft.py \
        src/urbanlens/dashboard/migrations/0041_pin_import_failure_maps_url.py \
        src/urbanlens/core/tests/oauth.py \
        src/urbanlens/dashboard/templates/dashboard/partials/memories/_pin_import_failure_guess.html
```

`core/tests/oauth.py` was missed by the first manual pass, which filtered `/tests/` paths out while
looking for modules that break *startup*. It does not - but six committed test files import
`first_party_application` from it, so they fail at collection. It was found by
`bin/check_imports_tracked.py`, added in the same session precisely to stop this class from
depending on someone remembering to look.

78 new test files are also untracked. They do not affect startup, but without them none of this
audit's regression guards exist in the repository.

Not staged here: this audit does not commit or stage without being asked, and the commit was made
outside it.

## URGENT 2026-08-13: commit `c3ae4911` ships a model field without its migration

`c3ae4911 audit` committed 139 files, including `maps_url` on `PinImportFailure`
(`models/pin_import_failures/model.py`). It did **not** commit the two migrations that were sitting
untracked beside it:

- `0040_gotify_token_fail_soft.py`
- `0041_pin_import_failure_maps_url.py`

Both are still untracked on disk. Verified with Django rather than by inspection: with them moved
aside, `makemigrations --check --dry-run` against the committed model state reports two pending
changes - `+ Add field maps_url to pinimportfailure` and `~ Alter field notify_gotify_token on
sitesettings`.

**Effect on a fresh checkout of this commit:** `migrate` produces a schema with no `maps_url`
column while the model declares one, so every query touching `PinImportFailure` - the import-failure
queue, the per-card guess endpoint, the resolve view - fails with
`ProgrammingError: column dashboard_pin_import_failures.maps_url does not exist`. Existing
developer databases that already ran the untracked migrations are unaffected, which is what makes
this easy to miss: it breaks only for someone cloning fresh or deploying.

**Fix** - add both, 0040 first, since 0041 depends on it:

```
git add src/urbanlens/dashboard/migrations/0040_gotify_token_fail_soft.py \
        src/urbanlens/dashboard/migrations/0041_pin_import_failure_maps_url.py
```

Not done here: this audit does not commit or stage without being asked, and the surrounding commit
was made outside it. Note also that `0040` is not optional even independently of `maps_url` - the
`fail_soft=True` model change it reflects was already committed before this audit began (see the
entry above), so `main` was already missing a migration for it.

## LOW 2026-08-13: two more `get_or_create` sites state a uniqueness they do not enforce

Follow-up to the `Label` work, re-running the corrected sweep (one that understands functional
constraints, `field_id` vs `field`, related managers and `**kwargs` unpacking - the first version was
wrong all four ways). Besides `Label`, which is now fixed, two sites remain where the code's stated
intent is not backed by a constraint:

- `services/visits/safety.py:471` - `SafetyContactOptOut.objects.get_or_create(contact_profile, email,
  scope, owner, checkin)`. The docstring says these calls "don't create duplicate rows"; the model
  has no unique constraint, so two clicks on an opt-out magic link (or an email client prefetching
  it) can insert two.
- `services/import_formats/gpx_tracks.py:266` - `PinVisit.objects.get_or_create(pin, visited_at,
  source)`. Same shape: re-importing the same track is deduplicated by the `get`, but two concurrent
  imports are not.

**Neither is worth changing on its own evidence.** `blocks_notification` answers a boolean from
existence, so a duplicate opt-out row suppresses notifications exactly as one does; and a duplicate
visit needs two imports of the same track running at once. Recorded because both would be caught for
free by a `UniqueConstraint` if either model is touched for another reason, and because the *stated*
guarantee currently rests on nothing.

Not a finding: `services/facts/evidence.py:95` looked identical (`Fact.objects.get_or_create(key=...)`
against constraints on `('key','location')`, `('image','key')`, `('key','wiki')`) but supplies the
second half via `**lookup`, which the static sweep cannot see. It is correct.

## (ORIGINAL FILING) OPEN 2026-08-13: "detach location" on a pin fails with a 500, every time

`controllers/pin_edit.py:631` (the `else` branch of the location-change handler, reached when the
user detaches a pin from its shared `Location`) does:

```python
lat = float(pin.effective_latitude or 0)
lng = float(pin.effective_longitude or 0)
location = Location.objects.create(official_name=..., latitude=lat, longitude=lng)
```

`Pin.effective_latitude` is `float(self.location.latitude)` - the pin's **current** location's
stored coordinate. `Location` is `unique_together = ("latitude", "longitude")` (declared in
`0001_initial`, so this is not a regression from recent work). Creating a Location at coordinates a
Location already occupies is therefore a guaranteed constraint violation.

Reproduced directly, not inferred:

```
pin.effective_latitude=42.1234  location.latitude=42.1234
detach create: IntegrityError -> duplicate key value violates unique constraint
               "dashboard_locations_latitude_longitude_fdb6594d_uniq"
```

Both branches fail identically - the named-location branch above, and the fallback
`_create_location_with_canonical_name(lat, lng)`, which ends in the same bare
`Location.objects.create` (`controllers/maps.py:1156`).

**The fix is a product decision, which is why this is filed rather than patched.** `Location` is a
*shared* record of a physical place, globally unique on its coordinates - so "give this pin its own
Location at the same point" is not expressible. What "detach" should do instead is a design
question with at least three defensible answers:

- nudge the new Location's coordinates by the smallest representable amount, giving a genuinely
  distinct point (changes where the pin sits, slightly);
- keep one Location and express the separation another way - the pin already has marker-coordinate
  fields, which may be what "detach" was reaching for;
- refuse with an explanation, if two users pinning one physical place is *supposed* to share a
  Location and detaching was never coherent.

Whichever is right, the current behaviour - an unhandled `IntegrityError` surfacing as a 500 - is
wrong under all three.

**Why it went unnoticed: the branch has no test.** `PinRelinkView` serves two routes - `pin.link.to`
(relink to a named Location, which *is* covered, by `test_pin_relink_access.py` and
`test_pin_location_conflict.py`) and `pin.link` (detach, no location slug). Searching the whole test
tree for the detach route returns nine hits, and every one is `pin.link.delete` - an unrelated
endpoint for removing a pin's external links. Nothing posts to `pin.link`.

So this is not a subtle conditional that testing missed; the code path is simply never executed.

**Test added 2026-08-14 (audit chunk 326)**, `tests/hypothesis/test_pin_detach_location.py`. It
posts to `pin.link` and is marked `xfail(strict=True)` - it does **not** assert the 500, because
that would cement the bug as intended, and it does not presuppose which of the three fixes is
right. When detach stops raising, the strict marker fails and tells whoever fixed it to replace
the marker with a real assertion. The product decision above is untouched and still yours.
Whatever the fix turns out to be, a test posting to `reverse("pin.link", args=[pin.slug])` belongs
with it - that single request is enough to catch this class permanently.

## OPEN 2026-08-13: ~187 write routes have no test that names them

**Widened again 2026-08-16 (chunks 553-554): the sweep now reaches 486 of 647 named routes (75%),
up from 160.** Chunk 553's parameter measurement drove it - the cheap wins first (`label_kind`,
`profile_slug`, `profile_id`, `checkin_uuid`, `group_uuid`), then multi-parameter routes where every
parameter is known, then `session_id`, the single largest gate at 36 routes.

`session_id` needed a wrinkle worth recording: it names a **different model in each game** (SpotGuessr
`GameSession`, `TriviaSession`, `ConsensusSession`), so no single value satisfies all 36. The sweep
now accepts a *list* of candidate values for a parameter and tries each on single-parameter routes,
so every game family is exercised for real by one candidate and merely 404s for the others - and a
404 passes a sweep that only ever objects to a crash. Multi-parameter routes take the first candidate
of each, keeping the URL count linear.

Those 36 routes came back clean. The remaining 161 need `token`, `activity_id`, `album_slug`,
`round_id`, `image_id` and similar - each a fixture, each a further increment.

**Chunk 555: 532 of 647 (82%), and clean.** Six more fixtures - `album_slug` (14 routes), `token`
(9), `image_id` (8), `activity_id` (8), `alias_id` (6), `comment_id` (5) - each one object. No new
crashes.

That is the first widening increment to find nothing, which is worth noting rather than glossing:
the first three increments each bought a defect, this one bought none. The remaining gates
(`round_id`, `task_id`, `action`, `message_id`, `overlay_uuid`, `layer_uuid`) are smaller and need
more setup per route, so the cost per increment is rising while the yield has fallen. The sweep is
approaching the point where further widening is not the best use of effort - recorded so the next
person does not read 82% as an arbitrary stopping place.

**Extended 2026-08-16 (chunk 552), and it found two more.** The first version only reached routes
taking a single owned-object parameter - 160 of the resolver's 648 named routes. The larger
population was the **230 zero-parameter routes**, easy to overlook precisely because they need no
fixture: there is nothing to build, so nothing prompts you to build it. Sweeping those too turned up:

- **`test_ai` was a dead route.** `urls.py` wired `PinController.as_view({"get": "test_ai"})` to a
  method `PinController` does not have, so every request raised `AttributeError` - a guaranteed 500.
  Nothing in the codebase referenced it. This is the same class as the dead `google_images` route
  that `test_cross_user_route_access.py`'s docstring records finding; a second one had survived since.
  Removed.
- **`saved_filters.new` answered every POST with a 500.** `SavedFilterEditView` backs two routes, and
  its `post()` required `filter_uuid` while `new/` supplies none - so the TypeError fired before any
  application code ran. Not a broken user flow (the form posts to `saved_filters.create`; `new/` is
  only ever `hx-get`), which is exactly why it survived: no UI path exercised it. It now refuses with
  405, since editing without naming what to edit is not a request that view can answer.

`billing.stripe_webhook` also answers 503 in tests, and that is the endpoint **working** - it fails
closed when `UL_STRIPE_WEBHOOK_SECRET` is unset rather than processing an unverifiable payload. Named
in the skip set with that reason, alongside `logout`, which would otherwise end the session and leave
the rest of the sweep measuring login redirects.

Still out of reach: the 258 routes taking multiple parameters or a parameter this fixture set has no
value for. Stated here rather than hidden behind a green test.

**Partly addressed 2026-08-16 (chunk 551) - one property across all of them, rather than one test
each.** This entry says closing the gap route by route "is not a strategy". It is not; but a single
*property* asserted across every write route is, and
`test_write_route_smoke.py` now does that: logged in as the **owner**, it posts a minimal body to
every single-parameter owner-scoped route and asserts the answer is not a 5xx.

The property is deliberately weak - 400, 403, 404, 405 and 409 all pass, because refusing an empty
payload is correct and a generic sweep cannot know what any route is meant to *do*. Only "this
request made the server throw" fails. That is precisely the class this entry was opened for.

It complements rather than duplicates `test_cross_user_route_access.py`, which asks whether a
*stranger* gets in and flags only `200` - a crashing route answers 500 and passes it silently.

**What it found on its first run: exactly one crash, and it is the route that motivated this entry.**
`pin.link` raises `IntegrityError` on every request, which is the open detach-location product
decision. Nothing else in the sweep crashes. That is the instrument validating itself - it reproduced
the known bug from a standing start and produced no noise alongside it.

`pin.link` is exempted by name with its reason, and the exemption is kept honest by
`test_the_known_crash_is_still_crashing`: when the product decision is made and the route fixed, that
test fails and says to delete the entry. An exemption nobody re-checks is how an allowlist rots into
a blindfold (chunk 546).

This does not close the entry. The 186 routes still have no test asserting what they *do*; they now
have one asserting they do not crash.

Prompted by the detach 500 above, which survived because its route had no test while its
*sibling* route did.

*Updated 2026-08-14 (chunk 326):* `pin.link` itself is now covered - `test_pin_detach_location.py`
posts to it via `reverse()`, so the count is **186**. That is one route out of 187, which is the
honest scale of the dent: this entry describes a systemic gap, and closing it one route at a time
is not a strategy. What the detach case does show is the *unit* of progress - a single request
against a never-executed route was enough to pin a 500 permanently. Enumerating every route from the live resolver and matching each name exactly
against the test tree (exact match, because `pin.link` is satisfied in a naive grep by
`pin.link.delete`):

- 841 project routes (excluding Django admin and `oauth2_provider`)
- **301** never referenced by exact name in any test
- **187** of those accept `post`/`put`/`patch`/`delete`

Sampled five to check the number is real - `consensus.vote`, `dev_toolbar.toggle_theme`,
`external_api:messages.groups.read` have no test mention at all; `consensus.answer` and
`external_api:lists.resync` match only coincidental substrings in unrelated code
(`record_consensus_answer_evidence`, `lists_resynced`). All five are genuinely uncovered.

**Known false-positive mode, so treat 187 as an upper bound.** 92 test lines address endpoints by
literal path (`_BASE = "/dashboard/api/external/v1/labels/"`) instead of `reverse()`, and any route
covered only that way looks uncovered here - `external_api:labels` is one, and is well tested. The
other 1,920 URL references in the test tree do use `reverse()`, so the skew is bounded but real.

*Probed 2026-08-14 (chunk 338):* matching each route's static path prefix against the test tree
finds only **8** routes covered by literal path but not by name - so the literal-path
false-positive mode looks like a small correction, not a large one. Treat that as indicative
rather than decisive: the same probe enumerated 971 routes against this entry's 841 and 419
uncovered against its 301, so its route-set and namespace attribution differ from the careful
count above, and it searched only `dashboard/tests`. Where the two disagree, this entry's numbers
are the better ones.

**The authoritative instrument is `coverage.py`** (already installed, 7.15.0): run the suite under it
and report which view callables never execute. That answers the question directly instead of by
proxy, and is the right next step before anyone works through this list.

Worth doing because the one route from this set that *was* investigated - `pin.link`, the pin-detach
endpoint - turned out to fail with a 500 on every request (see the entry above). An untested write
route is not merely unverified; it is where a permanently broken feature can sit unnoticed.

## Database backups have no restore path, and their format defeats the only example

`core/controllers/backups/db.py` produces **plain-SQL** dumps: `pg_dump -U ... -f <path>`, no
`-Fc`, written as `backup_<YYYYMMDD>_<HHMMSS>.sql`. Creation, retention, scheduling, the atomic
temp-file rename, and (as of the 2026-08-14 audit chunk) reaping of abandoned `.tmp` files are all
implemented and tested.

Restoring one is not implemented, not documented, and not tested.

- No code path in `src/` or `bin/` restores a scheduled backup.
- The only `pg_restore` in the repository is `bin/clone_prod_to_staging.sh:158`, which restores
  `/tmp/clone.dump` - a *different* dump that script creates for itself with its own flags. It has
  nothing to do with the backup directory.
- That mismatch is a trap rather than a mere omission. `pg_restore` **cannot read a plain-format
  dump**; it exits with *"input file appears to be a text format dump. Please use psql."* An
  operator under pressure, reaching for the repository's only restore example, hits that error on
  their first attempt at recovering production data.

Restoring these dumps actually requires `psql -U <user> -d <db> -f backup_....sql`, into a database
where PostGIS is already installed (a plain dump's `CREATE EXTENSION postgis` needs superuser, and
the dump does not create the database itself). None of that is written down anywhere.

Worth deciding deliberately rather than defaulting:

1. **Document the procedure** - the minimum. A `docs/BACKUPS.md` with the exact `psql` invocation,
   the PostGIS prerequisite, and whether to restore into a fresh database or an emptied one.
2. **Consider `-Fc`** (custom format). It compresses, allows selective/parallel restore, and makes
   `pg_restore` - the tool the repo already demonstrates - the correct one. This changes the
   filename suffix, so `BACKUP_FILENAME_RE`, `is_backup_temp_filename`, and any existing on-disk
   backups need handling together.
3. **Verify a restore at least once**, into a scratch database, ideally in CI against a seeded
   dump. Everything above is theory until a dump from this code has actually been restored.

Nothing here is a defect in the backup *writer*, which is careful. The gap is that the half of the
system that matters on the worst day has never been exercised.

---

## Session chat WebSockets have no rate limit or frame-size cap

`dashboard/consumers.py` accepts inbound frames on four sockets (`DirectMessageConsumer`,
`SafetyCheckinChatConsumer`, and the three game sessions via `_ParticipantSessionConsumer`). The
authorization on those sockets is thorough - participation is verified before any group is joined,
API-key scope is checked, credentials are re-validated on a timer. What is missing is anything
bounding *volume*.

- No per-connection or per-profile rate limit on `receive()`.
- No frame-size cap. `body` is truncated to `MAX_SESSION_CHAT_MESSAGE_LENGTH` (1000) only *after*
  the whole frame is read and JSON-parsed, so a multi-megabyte frame is fully processed before
  1000 characters of it are kept.
- Each accepted frame is one DB insert plus a channel-layer broadcast to every member of the
  group, so the cost is amplified by the number of connected participants.

This needs an authenticated, verified participant, which is what keeps it in "abuse by a member"
territory rather than an open vector - it is not remotely triggerable. But nothing stops a
participant from filling a session's chat table as fast as their socket allows, and the same
applies to DMs between accepted friends.

Two things to decide, both product calls rather than obvious defaults:

1. **The threshold.** Something like N messages per rolling window per profile per session.
   `services/core/rate_limiter.py` already exists for external API budgets; whether to reuse it or
   use a plain cache counter (`cache.incr` on a windowed key) is an implementation detail, but the
   limit itself is a judgement about what normal chat looks like.
2. **The response to exceeding it.** The consumers' established convention is an `{"type":
   "error"}` frame rather than a close - closing puts the client into a reconnect loop over a
   condition retrying cannot fix (that reasoning is already written down in
   `_ParticipantSessionConsumer.receive`). A throttle should follow it.

Both game chat and DM chat now funnel through shared code - `services/core/session_chat.py` for
games - so the limit can be implemented once per family rather than five times.

A frame-size cap is separately worth setting at the server: Daphne accepts
`--websocket_max_message_size`, which `docker-compose.yml` does not currently pass, so the
truncation above is the only bound and it happens too late to matter.

---

## The API rate limiter fails open, which uncaps spend rather than availability

`services/core/rate_limiter.check_rate_limit` returns `True` (allowed) when it cannot determine
whether a call is within budget. As of the 2026-08-14 audit chunk the handlers are narrowed to
`DatabaseError`, so a *bug* surfaces instead of silently reading as "allowed" - but the deliberate
fail-open on an actual database failure remains, and it is worth an explicit decision rather than
inheriting it.

This limiter is not a security control. It caps calls to **paid third-party APIs** (Google
Maps/Places, OpenAI, and the rest of `SERVICE_REGISTRY`), and the project already tracks a cost
estimate per call. So the failure mode of fail-open is money, not access: whatever quota or budget
the limits encode stops being enforced for as long as the failure lasts.

Two things make this less alarming than it first looks, and are worth knowing before anyone
"fixes" it:

- `record_api_call` calls `check_rate_limit` *inside* a `transaction.atomic()` block that has
  already run `ApiRateLimit.objects.select_for_update().get(service=service)`. A real database
  outage therefore raises at that line, before `check_rate_limit` is ever reached - so the
  fail-open path is much harder to hit from the main gateway flow than reading the function alone
  suggests.
- The path logs with `logger.exception`, so it is noisy rather than silent.

The decision to make: on a database failure, should a paid API call proceed unmetered, or should
it fail? Fail-closed protects the budget and degrades the feature; fail-open does the reverse.
Either is defensible - but it should be chosen, and the choice recorded here, rather than being a
side effect of an exception handler. If fail-closed is chosen, the same question applies to
`service_is_enabled`, which sits next to it in the same conditional.

---

## Colour values interpolated into `style="…"` - resolved for the renderers, open on the server

**Superseded note (corrected 2026-08-14).** An earlier version of this entry listed
`markup-engine.ts:66/84/150` and `markup-toolbar.ts:297/299` as unfixed because a hex-only
validator might blank a legitimate `rgba()`/`none` value. That was half wrong, and the correction
is worth keeping:

- `markup-engine.ts` was **already safe**. It defines its own `safeColor(v, fallback)` and
  `safeOptionalColor` (which returns `"none"` unchanged), and runs the value through them before
  interpolating - e.g. `const color = safeColor(s.color, "#e53e3e")` on the line above the
  `style="color:${color}"` that the grep flagged. The flagged lines were reading
  already-sanitised locals.
- `markup-toolbar.ts` was **not** safe and now is. It imported no validator and interpolated
  `item.color` and `textBackground()`'s `item.border_color` straight into `style="…"`. Those are
  fixed with the shared `shared/color-safety.safeColor`; the `"none"` case that made this look
  risky is handled explicitly, exactly as `markup-engine.safeOptionalColor` already did.

Markup colours are *less* validated than label colours server-side, which is what made this worth
chasing: `MarkupShape.color` is `CharField(max_length=20, default="#e53e3e")` and `border_color`
`CharField(max_length=20, blank=True)` - **no `choices` at all**. `x" onmouseover="a` is 17
characters.

Not changed, and deliberately: the colours passed to Leaflet as *options*
(`markup-toolbar.ts:318, 345, 348` - `fillColor:`, `color:`) are set programmatically as style
properties rather than interpolated into markup, so an invalid value is inert there rather than
injectable.

### The server-side half - RESOLVED 2026-08-14

`services/core/colors.clean_color` now validates every colour write path (32 of them, across
`controllers/labels.py`, `external_api/views.py`, `controllers/markup.py`,
`controllers/detail_pins.py`, `controllers/maps.py`, `controllers/custom_layers.py` and
`controllers/saved_filters.py`). Eight further sites in `controllers/detail_pins.py` were missed on
the first pass and fixed on 2026-08-14: the original sweep matched `color = X.get(...)` assignments
but not dict-literal `"color": body.get(...)` entries, and its field list was built from request
keys, so `detail_bg_color` (populated from `bg_color`) never appeared. Invalid input is coerced to
each call site's existing default
rather than raising, since these come from palette pickers and a non-colour is a malformed
request; `"none"` is permitted only where it means "no border".

Left for a future pass: the model fields themselves are still permissive
(`MarkupShape.color`/`border_color` have no `choices`, `Label.color` has choices Django will not
enforce on `save()`). Validation now happens at every known entry point, but a new write path
added without `clean_color` would reintroduce the gap. A `validators=[...]` on the fields, or a
custom field type, would make it structural rather than conventional.

---

## Inline template JavaScript is structurally untested

`pages/map/index.html` contains several thousand lines of JavaScript inside a single `<script>`
block. `bun test` covers `frontend/ts/`, so none of it can be imported, mocked or exercised - the
helpers added there in the 2026-08-14 audit had to be verified by extracting the functions into a
scratch file and running them separately.

This is why bugs like the two above survive: the code is invisible to every automated check the
project has. Moving that script into `frontend/ts/` (where it would get `tsc --noEmit`, bun tests,
and the same review as the rest of the frontend) is a large job, but the map page is the single
biggest concentration of untested logic in the codebase.

---

## Inline template JS: 21,543 lines, 14 escaping helpers, zero test coverage

Measured 2026-08-14. `dashboard/templates/` contains **21,543 lines of inline JavaScript across
101 templates**, versus 22,684 lines in `frontend/ts/` which `tsc --noEmit` and 394 bun tests
cover. Half the frontend is outside every automated check.

Concentration (top 5 = 49% of the total):

| lines | template |
|---|---|
| 5,175 | `pages/map/index.html` |
| 1,772 | `pages/messages/index.html` |
| 1,377 | `pages/trips/detail.html` |
| 1,294 | `pages/location/index.html` |
| 1,118 | `themes/base.html` |

The concrete cost, beyond "untested": 44 function names are defined in more than one template,
including **14 HTML-escaping helpers under 9 names**, of which 6 escape `&<>` only and 8 also
escape quotes. Nothing in any of the names distinguishes the text-node case from the attribute
case, and the 2026-08-14 audit found two real bugs that existed precisely because the wrong one
was in reach (`memories/index.html`, `map/index.html`).

Suggested order of work, largest payoff first:

1. **Move `pages/map/index.html`'s script into `frontend/ts/`.** One file, 5,175 lines, ~24% of
   the problem, and the page where the audit found the most issues.
2. **Add `frontend/ts/shared/escaping.ts`** exporting `escapeText` and `escapeAttr` (names that
   say which context they are for), and have migrated code import it rather than redefine it.
3. Migrate the next four largest templates.

This is a large job and nothing above is urgent in isolation. It is recorded because every future
bug of this shape in these files will be invisible to CI, and because the duplication means fixing
one instance fixes nothing else.

---

## Nine named routes with no discoverable caller (candidates for review, not confirmed dead)

From a 2026-08-14 sweep of all 753 named routes. 61 have no static reference outside `urls.py`;
30 are reached via `reverse(f"{prefix}.{suffix}")` and 34 live in `external_api/urls.py` where the
callers are API clients. `password_reset_complete` is Django's own. That leaves:

- `add_review`
- `comment.locations`
- `dev_toolbar.toggle_map_dark_mode`
- `label.index`
- `location.wiki.article.restore`
- `location.wiki.article.revision`
- `location.wiki.gallery.image`
- ~~`pin.upload.takeout`~~ - RESOLVED 2026-08-14: superseded duplicate of the
  `pin.import.preview`/`confirmed` wizard flow; handler and route removed
- `safety.checkin.gallery.image`

**Do not bulk-delete these.** Each needs checking individually, because the plausible explanations
differ: `dev_toolbar.toggle_map_dark_mode` is dev tooling that may be invoked by hand;
`pin.upload.takeout` and the two `gallery.image` routes may be hit as literal URLs built in inline
template JavaScript (which this audit has separately measured at 21,543 untested lines, so it is
exactly where a hardcoded path would hide); `location.wiki.article.restore`/`revision` pair with
`services/wiki/articles.restore_revision`, which does exist, suggesting a wired-up feature whose
entry point is somewhere the scan could not see.

A route with genuinely no caller is still worth removing - it is surface area that has to be kept
authorised and tested - but "the grep found nothing" has produced a false positive in every
category this sweep touched.

---

## 46 BEM modifiers applied in templates with no CSS rule

Measured 2026-08-14 against the compiled `style.css` (current at the time - no `.scss`
was newer). Each base class *is* styled, so each of these was written to create a visual
distinction that does not render. Not fixed, because what each should look like is a design
decision. Sorted by how many templates apply it.

| modifier | templates | first use |
|---|---|---|
| `card--secondary` | 8 | `pages/location/index.html` |
| `badge--muted` | 5 | `pages/site_admin.html` |
| `card--primary` | 3 | `pages/location/index.html` |
| `ul-game-hud__group--lead` | 3 | `pages/consensus/index.html` |
| `dm-composer-attachment-chip--share` | 2 | `partials/messages/_group_thread.html` |
| `form-row--map` | 2 | `pages/safety/create.html` |
| `btn--sel` | 1 | `pages/organize/index.html` |
| `btn--trigger` | 1 | `partials/ui/_icon_picker.html` |
| `btn-icon--primary` | 1 | `pages/site_admin_ui_components.html` |
| `cf-value-input--reference` | 1 | `partials/custom_fields/_value_input.html` |
| `cf-value-input--select` | 1 | `partials/custom_fields/_value_input.html` |
| `cf-value-input--url` | 1 | `partials/custom_fields/_value_input.html` |
| `comment-reply-btn--sm` | 1 | `partials/trips/trip_comments_panel.html` |
| `detail-item--abandoned` | 1 | `partials/pins/pin_overview_partial.html` |
| `detail-item--built` | 1 | `partials/pins/pin_overview_partial.html` |
| `detail-item--coordinates` | 1 | `partials/pins/pin_overview_partial.html` |
| `detail-item--last-active` | 1 | `partials/pins/pin_overview_partial.html` |
| `dm-composer-attachment-chip--map` | 1 | `partials/messages/_thread.html` |
| `dm-thread--group` | 1 | `partials/messages/_group_thread.html` |
| `form-row--maps` | 1 | `pages/safety/detail.html` |
| `form-row--message` | 1 | `pages/safety/create.html` |
| `form-row--plan` | 1 | `pages/safety/create.html` |
| `form-row--time` | 1 | `pages/safety/create.html` |
| `form-row--title` | 1 | `pages/safety/create.html` |
| `fp-cf-input--select` | 1 | `partials/custom_fields/_filter_input.html` |
| `fp-cf-input--text` | 1 | `partials/custom_fields/_filter_input.html` |
| `home-widget--stats` | 1 | `partials/home/_widget_stats.html` |
| `inline-sub-form--pricing` | 1 | `pages/site_admin_subscriptions.html` |
| `map-overlay-btn--cancel` | 1 | `partials/layout/_map_annotations_panels.html` |
| `notif-item__icon-wrap--pin_shared` | 1 | `partials/notifications/notification_item.html` |
| `notif-item__icon-wrap--safety_ci_due` | 1 | `partials/notifications/notification_item.html` |
| `notif-item__icon-wrap--visit_suggested` | 1 | `partials/notifications/notification_item.html` |
| `org-bulk-btn--edit` | 1 | `pages/organize/index.html` |
| `org-bulk-btn--merge` | 1 | `pages/organize/index.html` |
| `page-onboarding--wiki` | 1 | `pages/location/wiki.html` |
| `sv-img--fallback` | 1 | `pages/location/street_view.html` |
| `trip-map-marker-num--ghost` | 1 | `pages/trips/detail.html` |
| `trip-panel-empty--all-completed` | 1 | `partials/trips/trip_activities_panel.html` |
| `trip-panel-empty--tab` | 1 | `partials/trips/trip_activities_panel.html` |
| `ul-game-hud__btn--focus` | 1 | `partials/games/_game_hud_controls.html` |
| `visit-item--pending` | 1 | `partials/pins/_visit_history.html` |
| `visit-list--pending` | 1 | `partials/pins/_visit_history.html` |
| `visit-source--pending` | 1 | `partials/pins/_visit_history.html` |
| `wiki-seed-list--aliases` | 1 | `partials/pins/pin_wiki_create_dialog.html` |
| `wiki-stat-row--composite` | 1 | `partials/pins/_wiki_stat_rating_item.html` |
| `wiki-stat-row--mine` | 1 | `partials/pins/_wiki_stat_rating_item.html` |

Worth triaging rather than doing wholesale: `ul-game-hud__group--lead` (the leading
score on all three game pages), the three `visit-*--pending` classes (a pending visit is
indistinguishable from a confirmed one) and the `notif-item__icon-wrap--*` set are user-visible
states; others are cosmetic hierarchy that may simply have been abandoned. Deleting the class
from the template is as valid a resolution as writing the rule.

---

## 1,217 statements of write handlers that no test executes

Measured 2026-08-14 with `coverage.py` over the full suite; full list in
`docs/reports/2026-08-14-view-coverage.md`.

The view layer is 80% covered by statement, which sounds healthy. The shape underneath is less so:
**208 of 1,795 callables never execute**, and **100 of those are `post`/`delete`/`put`/`patch`
handlers totalling 1,217 statements**. Half of the unexercised view code is code that mutates data.

Suggested order, highest risk first (statement counts in brackets):

1. `controllers/labels.py::LabelBulkConvertView.post` [36] and `LabelBulkEditView.post` [33] -
   bulk mutations over many rows, and the label subsystem has already produced several bugs.
2. `controllers/site_admin.py::SiteAdminUsersView.post` [35] - user administration.
3. `controllers/detail_pins.py::LocationWikiDetailPinEditView.post` [34] - wiki-scoped edits, which
   touch the place-domain visibility rules.
4. `controllers/albums.py::AlbumEditView.post` [31], `consensus.py::ConsensusPhotoUploadView.post`
   [31], `visit_suggestions.py::VisitSuggestionRespondView.post` [31],
   `calendar_sync.py::CalendarImportView.post` [30].

`controllers/pin.py::PinController.upload_takeout` [39] is a special case: it is also on the
caller-less route list above, so it should be resolved (deleted or tested) before anything else -
two independent signals agree that nothing reaches it.

Caveats worth keeping attached to this number: coverage measures execution, not correctness, and
the run was scoped to `controllers/` and `external_api/`, so a service called by an uncovered
handler may itself be well tested.

---

## The category-on-pin methods have no production callers

Categories became a `Label` kind (`KIND_CATEGORY`), managed generically by the organize/bulk-edit
paths (`controllers/pin_bulk.py`, `controllers/pin_suggestions.py`). The older per-pin category
helpers were left behind:

- `Pin.change_category()` - after the 2026-08-14 removal of `MapController.change_category` and its
  route, zero callers outside tests
- `Pin.add_category()` - zero callers outside tests
- `Wiki.add_category()` - zero callers outside tests

Each still has tests, so the suite currently exercises code nothing reaches. That is worse than
either extreme: the tests give the appearance of coverage, and any bug found in these methods reads
as a production bug when it is not.

**A correction that belongs with this.** During the label-uniqueness work earlier the same day, a
`MultipleObjectsReturned` bug was found and fixed in `add_category` (the `Label.objects.get_or_create`
lookup was missing `profile=None`, so a case-insensitive match could return both a global and a
personal label). The fix is correct and the tests are real, but the method has no production caller
- so that bug was **not reachable in production**, and it was reported at the time without that
qualification.

Deciding what to do needs a product call rather than a mechanical one: either delete the three
methods with their tests, or wire them back up if per-pin category assignment is still wanted as a
distinct concept from labels. Deleting is the more likely answer, since `KIND_CATEGORY` labels
already do this and have a UI.

---

## API behaviour change: non-hex colours are no longer stored

From 2026-08-14, every colour write path runs through `services/core/colors.clean_color`, which
accepts `#rgb`/`#rrggbb` (plus the literal `none` where a border legitimately means "no border")
and otherwise falls back to the field's default.

This is a **visible change for external API clients**. `PATCH /labels/<uuid>/`,
`POST /labels/bulk/edit/` and the saved-filter endpoints previously stored whatever string was
sent - `{"color": "red"}` was accepted and persisted, and one test asserted exactly that.

It was never a working value:

- `Label.color` declares `choices` that are all hex; `choices` is enforced by `full_clean()`, which
  `save()` does not call, so the invalid value was stored rather than rejected.
- Every renderer appends an alpha suffix (`color + "33"`), so `"red"` became `red33` - not a
  colour, painting nothing. The chip rendered as though no colour had been set.

So the change replaces "stored, then silently ignored by the UI" with "not stored". Clients sending
named CSS colours will see the field come back empty rather than echoing their input.

Worth deciding, and not decided here: whether these endpoints should **reject** an invalid colour
with a 400 rather than coercing it. Coercion matches the HTML form paths (where the value comes
from a palette picker and anything else is a malformed request), but an API arguably owes its
callers an error instead of silent data loss. If that changes, it should change for all 31 sites at
once, in `clean_color`'s callers rather than in the helper.

---

## Two dead queryset methods

`Pin.by_category()` and `Wiki.by_category()` have no callers anywhere - Python, templates or tests.
Both filter `labels__name=<category>` without `distinct()`, so they would return duplicates if used.

Found while tracing the multi-valued-filter candidates (2026-08-14); the rest of that list resolved
- 7 already collapse via `filter_by_criteria`, 2 cannot duplicate (`__isnull=True` tests for the
absence of related rows), and 3 (`rated`/`rated_over`/`rated_under`, test-callers only) were given
the `distinct()` they were missing.

Filed rather than deleted because removing public queryset API is a judgement about whether it is
scaffolding for something planned. If it is not, both should go - dead API with a latent bug is the
worst combination, since the next caller inherits the bug.

---

## Queryset API with no production caller: 70 of 251 (candidate count)

From a 2026-08-14 sweep of every public method on a `*/queryset.py` class:

- **44** are not called from any file other than the one defining them
- **26** are called only from tests

That is 28% of the queryset API with no production consumer, which is worth a look - a custom
queryset method exists to be the one place a piece of domain logic lives, and one nothing calls is
either scaffolding, a leftover, or a piece of logic that got reimplemented inline somewhere else.
The last of those is the interesting case, because it means the same rule now exists twice.

**Known false-positive class, do not treat the 44 as dead.** The scan deliberately ignores calls
within the defining file, so a method used only by its siblings is flagged. `apply_label_groups` is
in the list and is definitely used - `filter_by_criteria` calls it, in the same file, as verified
while tracing the duplicate-row candidates. Such methods are arguably mis-scoped (a `_`-prefixed
helper rather than public API) but they are not dead.

Two entries are confirmed dead by separate inspection: `Pin.by_category` and `Wiki.by_category`
have no callers anywhere, in any file, including their own.

Worth doing properly with a call-graph rather than a name grep, since the test-only 26 in
particular may be exercised through the very `filter_by_criteria`-style aggregators that make them
look unused.

---

## 20 label-kind literals where the named constant is already used 130 times

`models/labels/meta.py` defines `KIND_TAG`/`KIND_CATEGORY`/`KIND_STATUS`/`KIND_USER`/`KIND_MEDIA`,
and the codebase imports them **130 times** outside tests. Twenty query/create sites used the bare string instead; **eighteen are done, two remain**.

The thirteen done are the ones where the import is provably safe: `models/labels/meta.py` contains
*only* constants and imports nothing, so it is a leaf module that any layer can import at module
level without circularity. The two left are both in `models/pin/model.py`'s `to_json()` - `kind="status"` (891) and
`kind="tag"` (896). Unlike the others they sit in a method with no local `Label` import, so each
needs a new line rather than a word added to an existing one. (A third, at the old line 863, went
away with the `__str__` fix - it was the query this audit removed from that method.)

**One entry was withdrawn.** `services/pins/pin_suggestions.py:767` (`source="external_api"`) is
correct as written: `PinAlias.source` has **no** `choices` - it is deliberately free-text so plugin
name providers can attribute aliases to themselves, and `AliasSource` defines only `USER` and
`OTHER`. The scan flagged it because it keyed choices by field *name* across all models rather than
by `(model, field)`, so `source` inherited the choices of `PinSuggestion.source` and `Image.source`,
which do define `EXTERNAL_API`. Same collision class as the earlier value-keyed version, one level
finer. A correct version needs `(model, field)` resolution; nineteen of the twenty original hits
were on `Label.kind`, where the ambiguity happened not to bite.

**The approach for them is already established in the same files, so this is smaller than it
looks.** `models/labels/model.py:26` re-exports every `KIND_*` constant from `labels.meta`, and
`models/pin/model.py` already does function-local imports of both (`labels.meta` at line 534,
`labels.model` at 811 and 829) - the local import is how these modules avoid the `labels.model`
<-> `pin.model` cycle. So:

- Sites at 814 and 833 sit in methods that *already* do `from ...labels.model import Label`.
  Extending that to `import KIND_CATEGORY, Label` is a one-word change with no new statement and
  no new import edge.
- Sites at 863, 886 and 891 are in `__str__` and a serialisation method with no local Label
  import; they need one line each, matching line 534's `from ...labels.meta import ...` pattern.
- Worth noticing while there: line 863 runs a **database query inside `__str__`**
  (`self.labels.filter(kind="status")`). `CLAUDE.md` already forbids `save()` in `__str__`; a query
  there is the same class of problem - it fires on every repr, in admin lists, logs and error
  pages. Exact list, from a scan that resolves each field's own `choices` via
`Model._meta.get_field(name).choices` rather than matching values across all enums:

| file | lines | literal |
|---|---|---|
| ~~`controllers/maps.py`~~ | ~~534, 666~~ | done 2026-08-14 |
| ~~`controllers/pin_lists.py`~~ | ~~90~~ | done 2026-08-14 |
| ~~`controllers/pin_edit.py`~~ | ~~350, 355, 357~~ | done 2026-08-14 |
| ~~`models/pin/model.py`~~ | ~~814, 833~~ | done 2026-08-14 |
| `models/pin/model.py` | 891 | `kind="status"` (863 removed with the __str__ fix) |
| `models/pin/model.py` | 891 | `kind="tag"` |
| ~~`models/pin/signals.py`~~ | ~~200~~ | done 2026-08-14 - the only site whose file already imported from `labels.meta` at module level |
| ~~`models/wiki/model.py`~~ | ~~328~~ | done 2026-08-14 |
| ~~`services/labels/statuses.py`~~ | ~~26, 46~~ | done 2026-08-14 |
| ~~`services/pins/pin_creation.py`~~ | ~~330, 333~~ | done 2026-08-14 |
| ~~`services/visits/visits.py`~~ | ~~502, 520~~ | done 2026-08-14 |

Nothing is broken today - the literals match the constants. The risk is the one already fixed in
`VisitQuerySet.from_takeout` (2026-08-14): the two agree by coincidence, so changing a constant
leaves these silently filtering on a value nothing produces. Several are in `Pin.add_category` /
`change_category`, which this audit separately found to have no production callers - so those
particular ones matter least.

Mechanical but not trivial: each file needs the right import, and `models/pin/model.py` and
`models/wiki/model.py` import from `labels.model` lazily to avoid circularity, so the constants
must follow the same pattern.

---

## CORRECTION: the `to_json()` prefetch work does not affect the map

Claimed repeatedly during the 2026-08-14 audit, and wrong: that fixing `Pin.to_json()`'s prefetch
behaviour reduced the map's per-pin query cost.

**The map does not use `Pin.to_json()`.** Its payload is built by `services/map_pins/payload.py`,
which annotates the rating with a `Subquery`:

    latest_rating = Review.objects.filter(pin_id=OuterRef("pk")).order_by("-created").values("rating")[:1]
    ... .annotate(map_rating=Subquery(latest_rating), child_count=Count("detail_pins", distinct=True))

That is already query-flat and does not touch `Pin.rating` or `Pin.to_json()` at all. The map was
never paying the cost that was measured.

What the work actually did:

- **`Pin.to_json()`** has **no production callers** - the only textual match outside tests is a
  *comment* in `pin_lists.py:100`. The measured 4-queries-per-pin was real, and was being paid by
  nothing. The method is now correct if it is ever used, which is worth something but is not a
  performance win.
- **`Pin.rating`** *is* live: `models/pin/serializer.py:73` exposes it, so the DRF path pays one
  query per pin unless the caller prefetches `reviews`. The `.latest()` -> `max(...)` change makes
  that path *capable* of being flat; whether the serializer's queryset prefetches `reviews` was not
  checked, and is the actual open question.

The measurement was sound; the attribution was not. A query-count test over a method proves what
that method costs, not that anything calls it - and `to_json()` looked like the map's serialiser
because a comment elsewhere said so.

---

## `Image` carries labels but does not inherit `LabelledModel`

`Pin` and `Wiki` both inherit `abstract.LabelledModel`, which supplies `categories`/`tags`/
`statuses` as prefetch-friendly properties over `self.labels.all()`. `Image` declares its own
`labels = ManyToManyField(...)` and does not inherit the mixin.

**Not a defect.** Checked 2026-08-14: `Image` does not reimplement those accessors badly - it does
not have them at all, and no code filters an image's labels by kind inline. Nothing is paying a
per-row query because of this.

It is an inconsistency worth resolving *if* image label access grows: the next person needing
"an image's media labels" will write `image.labels.filter(kind=...)`, which bypasses any prefetch,
rather than inheriting the version that does not. That is precisely how `Pin.to_json()` acquired
the bug fixed earlier the same day.

Care required if adopted: `LabelledModel` may declare the `labels` field itself, in which case
`Image`'s own declaration has to be reconciled rather than simply adding the base class - a
migration question, not a refactor.

---

## PARTIAL: per-row queries inside loops - 12 sites, 3 fixed 2026-08-14, the same root cause

Three instances of this were fixed on 2026-08-14 (`Pin.to_json`'s `.filter()`, `Pin.rating`'s
`.latest()`, and `views_pin_bulk`'s `.exists()` costing len(pins) x len(labels)). An AST sweep for
the general shape - a **related-manager verb that bypasses `prefetch_related`, inside a loop or
comprehension** - finds twelve more. None are fixed; each needs its loop read to judge the
multiplier.

| file | lines | call | note |
|---|---|---|---|
| ~~`services/consensus/fields.py`~~ | ~~256, 260, 298, 302~~ | | **FIXED 2026-08-14** - see below |
| ~~`services/pins/pin_suggestions.py`~~ | ~~888~~ | | **FIXED 2026-08-14** - was one query per date in `suggestion.visit_dates`; now one `__in` query, with the set updated as visits are created so a repeated date is still skipped |
| `services/pins/pin_suggestions.py` | ~~802~~ | `pin.visit_history.filter(visited_at__date__in=days)` | **false positive** - inside a dict comprehension but evaluated once, not per iteration |
| ~~`services/memories/photos.py`~~ | ~~165~~ | | **FIXED 2026-08-14** |
| ~~`services/import_export/export.py`~~ | ~~650, 764~~ | | **BOTH FIXED 2026-08-14** |
| ~~`services/import_export/import_data.py`~~ | ~~1554~~ | `trip.profiles.count()` | **not worth fixing** - see below |
| ~~`services/pins/pin_list_membership.py`~~ | ~~89~~ | `pin_list.items.count()` | **not a defect** - the query is load-bearing, see below |
| ~~`controllers/pin.py`~~ | ~~227~~ | `pin.images.exclude(pk=...)[:20]` | **false positive** - one query; the comprehension iterates an already-sliced queryset |

Discounted as false positives: `export.py:848,850` (`os.path.exists`, which matches the
`obj.attr.verb(` shape).

**The fix is not uniform** - that is why this is a list rather than a patch. `.count()` in a loop
often wants an `annotate(Count(...))` on the outer queryset; `.filter()` over a relation often
wants `prefetch_related` plus a Python filter; `.exists()` often wants a single `__in` query
outside the loop. Each needs its caller read.

### Progress on the worst one

`_alias_find_missing` / `_alias_find_known` (`fields.py:256,260`) are invoked from
`services/consensus/selection.py:106` and `:163` as `strategy.find_missing(pool)`. **The remaining
step is to see how `pool` is built**, because that decides the fix and the two options differ:

**Answered.** `selection.py:71` builds it as `pool = list(eligibility.eligible_wikis(...))` - a
materialised **list**, with no prefetch. So it is the third case, and the fix needs *both* halves:

1. `prefetch_related("aliases", "images")` on the queryset inside `eligibility.eligible_wikis()`
   (and `eligible_wikis_for_all()`);
2. **then** change the helpers to `.all()` + `len()`/truthiness/Python filtering.

Either half alone is wrong. Only (2) makes it *slower* - `.all()` fetches every alias row where
`.count()` fetched a number. Only (1) does nothing, because `.count()`/`.exists()`/`.filter()`
never read the cache.

It is also worse than the table suggests: `_pick_normal_round` calls `strategy.find_missing(pool)`
**once per field kind** (`for kind in fields.all_kinds()`), so the cost is
kinds x wikis x queries-per-wiki, not wikis x 4.

**Done 2026-08-14.** Both halves applied together, since either alone regresses: the prefetches
are on `eligible_wikis()`/`eligible_wikis_for_all()`, and all four helpers now use `.all()`. The
two `wiki.images.filter(latitude__isnull=...)` calls became `any(image.latitude is None and ...)` -
translating a SQL `__isnull` filter into Python needs both fields checked, and getting it wrong
would silently change which wikis become round candidates.

Each file carries a comment naming the other: the coupling is invisible from either side alone, and
an edit to one that ignores the other reintroduces the cost silently. 76 consensus tests pass.

The instrument for confirming any of them is
`dashboard/tests/hypothesis/test_pin_to_json_prefetch.py`: capture queries over 1 and N objects and
assert the per-object delta is zero.


### `memories/photos.py:165` - RESOLVED

The open question was whether `maybe_suggest_photo_visit` creates a `PinVisit`, which would make a
precomputed set of visited dates stale mid-loop. **It does not**: `PinVisit` appears exactly once in
`services/memories/visits.py`, in the module docstring. It creates a `VisitSuggestion` only.

So neither branch adds a date to the set - the "already visited" branch logs another visit on a
date already in it - and one `__in` query up front is correct. Fixed.

The reasoning is recorded because it is not visible from the loop: whether collapsing a
per-iteration query is safe depends on what two *other* functions write, and the answer took
reading both. Same hazard as the Takeout importer and the suggestion-acceptance fix, both resolved
the same day - a per-iteration query is often reading the loop's own writes.
changes behaviour unless that is reproduced.


### `pin_list_membership.py:89` - the query is load-bearing, leave it

`order=pin_list.items.count()` sits inside a loop that **creates** `PinListItem` rows, so each
iteration deliberately reads the previous iteration's write to get the next order value.
Precomputing the count would give every new item the *same* `order`.

A local counter incremented per create looks like the obvious optimisation and is not safe either:
the same loop's `elif` branch calls `existing.delete()`, so the real count moves in both directions
and a local counter drifts from it.

This is the exact inverse of the hazard that made the other fixes tricky. There, a per-iteration
query accidentally read the loop's own writes and collapsing it changed behaviour. Here it does so
**on purpose**, and collapsing it would break ordering. Both cases look identical to a scan for
"query inside a loop", which is why every entry on this list needs its loop read rather than
patched to a pattern.

Cost is one cheap `COUNT` per added pin, on a membership-sync path - worth leaving alone.


### `export.py:650` and `:764` - fixes specified, both need the feeding queryset

Neither is fixable at the loop; both need the queryset that supplies it changed. Unlike the
`.filter()`-on-a-prefetch cases, there is no in-place rewrite that helps.

**`:650` - FIXED 2026-08-14, and the same "both costs" shape as `:764`.** The queryset already did
`prefetch_related("parents", "pins")`. `parents` is read with `.all()` and uses the cache; `pins` was
read with `.filter(profile=profile)`, which bypasses it - so every label fetched *all* its pins
(other profiles' too, for global labels) and then queried again per label. Fixed with:

    Prefetch("pins", queryset=Pin.objects.filter(profile=profile), to_attr="own_pins")

on the label queryset, then `label.own_pins` in the comprehension. `to_attr` matters - without it
`label.pins.all()` would return the filtered set under a name implying otherwise, which is a trap
for the next reader. Strictly better on both axes: fewer rows fetched *and* no per-label query.

Worth noting the two relations sat side by side, one right and one wrong, in the same
`prefetch_related` call - `parents` with `.all()`, `pins` with `.filter()`. That is how easily this
mistake hides.

**`:764` - FIXED 2026-08-14, and it was worse than specified.** The message queryset *already*
did `prefetch_related("images")`, and `message.images` is used for nothing but that count. So it
paid **both** costs: every image row for every message was fetched into a cache, and `.count()`
ignored the cache and issued a per-message COUNT anyway.

Annotated with `Count("images")` and the prefetch **dropped** - no rows fetched, no per-message
query. `len(message.images.all())` would have fixed the query but kept the pointless row fetching;
that is the option to avoid when a relation is only ever counted.

Both are export paths - they run over every label and every message a profile owns, so the
multiplier is the size of the account. Neither was measured; the instrument is
`test_pin_to_json_prefetch.py`'s approach (capture queries over 1 and N objects).

## Closing tally on this list

Fourteen candidates, triaged individually:

| outcome | count | notes |
|---|---|---|
| **fixed and verified** | 8 | `to_json` labels, `rating`, bulk label removal, consensus selection, suggestion acceptance, photo-visit matching, **label export**, **message export** |
| **false positives** | 3 | `export.py:848/850` (`os.path.exists`), `pin_suggestions.py:802`, `controllers/pin.py:227` - all one query, flagged for appearing inside a comprehension |
| **load-bearing, left alone** | 1 | `pin_list_membership.py:89` - reads its own writes deliberately, for ordering |
| ~~specified, not started~~ | 0 | both `export.py` sites were subsequently fixed - see above |
| **remaining** | 0 | list fully triaged |

*(This table said "6 fixed / 2 specified" until 2026-08-14, when the two `export.py` sites were
done and the tally was not updated with them. Third stale count found in this audit's own
documentation, after the colour-site total and the report's verification header - a number written
once and not re-checked is the failure mode these documents keep reproducing while describing it.)*

**Three different wrong answers were available for a scan to give here**: six real, three
false-positive, and one where the "obvious fix" would have broken ordering. A tool that patched
every hit would have been right less than half the time. Each entry needed its loop read - which is
also how the correctness traps inside the six real ones were caught (`get_latest_by` ordering,
`__isnull` conjunctions, and two loops silently reading their own writes).

---

## Lead: 11 relations prefetched in a file, then read with a cache-bypassing verb

The last two N+1 fixes (2026-08-14) shared a shape worth hunting: a relation named in
`prefetch_related(...)` and then read with `.filter()`/`.count()`/`.exists()`, which ignores the
cache. In both cases a *correct* sibling sat in the same call - `parents` with `.all()` beside
`pins` with `.filter()`; `images` prefetched purely to be `.count()`ed. There is no visual
asymmetry.

A file-level sweep for that pairing gives eleven candidates:

| file | line | call |
|---|---|---|
| ~~`controllers/pin_sharing.py`~~ | ~~166~~ | **false positive** - one query filtering submitted ids, not in a loop |
| ~~`controllers/safety.py`~~ | ~~747~~ | **false positive** - a single authorization check on one check-in; `.exists()` is correct there, being cheaper than fetching rows |
| ~~`controllers/safety.py`~~ | ~~1520~~ | **false positive** - one query for one check-in's template context |
| ~~`external_api/views.py`~~ | ~~2006, 3369~~ | **false positives** - a single count in a response, and a single map lookup on one check-in |
| ~~`models/album/model.py`~~ | ~~145~~ | **false positive** - already `len(self.items.all())`; the scan matched `.items.count()` inside the docstring explaining why not to use it |
| ~~`models/pin_list/model.py`~~ | ~~82~~ | **false positive** - same |
| `services/messaging/direct_messages.py` | 281, 398, 1259 | `.images.exists()` x3 |
| ~~`models/pin/model.py`~~ | ~~820~~ | already assessed - a write in `change_category`, once per request, in a method with no production callers |

**This is a lead, not a finding.** The sweep is file-level: it pairs a `prefetch_related("x")`
*anywhere* in a file with a `.x.filter()` *anywhere else*, and those may be different functions over
different querysets. Each needs checking that the prefetching queryset is the one feeding that call.

Two shortcuts from the fixes already done:

- If the relation is **only ever counted** (`.items.count()`, `.images.exists()` look like this),
  the answer is usually `annotate(Count(...))` and **removing** the prefetch - not `len(x.all())`,
  which fixes the query but keeps the row-fetching.
- If the filtered rows **are** used, the answer is `Prefetch(..., queryset=..., to_attr=...)`.

**Verified 2026-08-14: the three `direct_messages.py` sites were real, and the worst case of this
whole pattern.** The conversation-list queryset carries a comment stating that
`prefetch_related("images")` exists specifically to stop the `message_preview` template tag's
`images.exists()` from issuing a query per row - "an N+1 across the sidebar's conversation list".

The diagnosis is correct and the remedy does not work: **`.exists()` never reads a prefetch
cache**. The N+1 was still happening *and* every image row was being fetched as well - strictly
worse than no prefetch, while reading as solved. A future reader sees a prefetch, a comment naming
the exact symptom, and concludes it is handled; only a query count says otherwise.

Fixed by making the prefetch deliver what it promises: `.exists()` -> truthiness on `.all()` in
`templatetags/dashboard_tags.py:message_preview` and the two `direct_messages.py` previews, with
the comment corrected so the more idiomatic-looking `.exists()` is not "restored" later.

**Two more resolved, as false positives of a kind worth knowing about.**
`models/album/model.py:145` and `models/pin_list/model.py:82` already do
`len(self.items.all())`, with docstrings explaining *"rather than `self.items.count()`"* and
citing prefetch reuse - the scan matched that phrase in the prose. The two places in the codebase
that document this rule correctly were flagged for breaking it.

Worth noting for the remainder: a text-matching scan cannot tell code from a comment about code,
and this codebase comments unusually well - so its best-documented spots are the most likely to
appear on such a list.

**Two more dismissed 2026-08-14**: `pin_sharing.py:166` and `safety.py:747` are single-object
contexts, not per-row loops. `.exists()` on one check-in's contacts is the *correct* call - it is
cheaper than fetching rows, and the prefetch-cache argument only applies when the same relation is
read repeatedly across many objects.

### Closed: 11 candidates, 1 real

All eleven triaged. **One real** - the messaging previews, where a prefetch added specifically to
stop an N+1, with a comment naming the symptom, never stopped it. **Ten false positives**, of three
kinds:

- **single-object contexts** (6): one count in a response, one authorization check, one template
  context, one id filter. `.count()`/`.exists()` are the *correct* calls there - cheaper than
  fetching rows. The prefetch-cache argument only applies when a relation is read repeatedly across
  many objects.
- **docstring prose** (2): `album/model.py` and `pin_list/model.py` already do `len(items.all())`
  and were matched on the text explaining why not to use `.count()`.
- **already assessed** (2): a write in `change_category`, and a sliced queryset evaluated once.

~9% precision. That is the honest verdict on this scan: worth running **once**, immediately after
confirming the pattern twice by hand, and not worth building into a linter. The value came from
reading the candidates, not from the list.


### `import_data.py:1554` - assessed, leave it

`remaining = max_members - trip.profiles.count()` is computed **once per trip**, before the loop
over members, as a capacity check that has to reflect current state - memberships are being created
around it.

There is no queryset to annotate: trips are processed one at a time from import rows, so `trip` is
a freshly created or looked-up object rather than a row in an iterable that could carry
`annotate(Count("profiles"))`. One cheap `COUNT` per imported trip, on an import path.

Same category as `pin_list_membership.py:89` - a query inside a loop that is structurally
necessary rather than accidental. **Two of the fourteen candidates on this list were of that kind**,
which is the argument against treating any such list as a work queue to be cleared: a fifth of it
was code that should not change.

---

## `datetime.date.today()` -> `timezone.localdate()` (superseded)

Filed 2026-08-14 in audit chunk 314 as a fresh finding of 4 sites. It was neither fresh nor 4: the
2026-08-12 entry above ("`date.today()` bypasses Django's configured timezone") had already
recorded all **nine**, and had argued against converting them. Chunk 317 then rediscovered the
missing five by AST scan.

Merged into that entry to keep one record. Kept as a pointer rather than deleted, because the
duplication is the finding: two days of prior analysis were sitting in this file, in a section the
audit had been appending to for dozens of chunks, and were not read before acting.

## Workflow: Django logic can be checked on the host without the container

Found 2026-08-14 (audit chunk 320-321). `CLAUDE.local.md` correctly notes that pytest needs the
`app` container, because the project's settings import GeoDjango and this host has no GDAL. That
is true, and it is easy to over-generalise into "no Django at all on the host", which is false.

`django.conf.settings.configure(...)` + `django.setup()` builds a minimal Django without loading
the project settings, so nothing imports `django.contrib.gis`:

```python
from django.conf import settings
settings.configure(USE_TZ=True, TIME_ZONE="UTC", DATABASES={}, INSTALLED_APPS=[])
django.setup()
```

Useful for anything not touching the ORM or geo models - timezone behaviour, template filters,
form/field validation logic, signal wiring, pure service functions. Seconds instead of a
multi-minute container cycle.

It caught a real error this way: a test asserting `date.today()` is unaffected by
`override_settings(TIME_ZONE=...)`, which is wrong because Django's `setting_changed` receiver
also rewrites `os.environ["TZ"]` and calls `time.tzset()`.

## Note: NotificationLog receivers self-guard on `created`

Recorded 2026-08-14 (audit chunk 343) because it is easy to get backwards. The three
`NotificationLog` `post_save` receivers - `push_notification_to_browser`, `enqueue_text_alerts`,
`enqueue_native_push` - all begin `if created and instance.profile_id`. Marking a notification read
via `queryset.update()` is therefore safe for a reason that has nothing to do with `update()`
skipping signals: a plain `save()` would be equally safe. Anyone converting those call sites to
`save()` for signal-correctness reasons should know they are not fixing a re-push hazard, because
there isn't one.

## Note: inline template JS defeats source searches of `frontend/ts`

Recorded 2026-08-14 (audit chunk 347) as concrete evidence for the inline-JS migration item.
Searching `dashboard/frontend/ts` for readers of the `ul_pins_dirty` cache-invalidation flag returns
**zero production hits**, which reads as a dead code path. The actual consumer is inline JS in
`templates/dashboard/pages/map/index.html` (lines ~1444 and ~2012), and two of the five writers are
inline in other templates.

Any audit, refactor, or dead-code sweep scoped to the TypeScript tree will draw wrong conclusions
about client behaviour while looking thorough. Until the inline JS is migrated, searches for
client-side behaviour must cover `templates/**/*.html` as well.

## PARTIAL 2026-08-14: the dev stack's `app` container has been unhealthy for its entire uptime (one branch resolved)

Found by a runtime check (audit chunk 351) rather than by reading code - the first finding in this
audit that no static analysis could have produced.

```
urbanlens_devs1_app   Up 10 days (unhealthy)   FailingStreak=23150
curl http://localhost:$UL_APP_PORT/dashboard/login/  ->  HTTP 000 (connection refused)
```

**The streak count matters for attribution.** 23,150 consecutive failures is on the order of the
container's whole 10-day uptime, so this is not a side effect of this session's activity (a
70-minute test suite and repeated `docker cp` into the same container), which was the obvious
suspicion and is wrong.

What still works: Django itself runs fine *inside* the container - the full 10,781-test suite
executed there. So this is the serving/healthcheck path, not the application code.

Two consequences worth noting:

- `nginx` reports **healthy** while `app` does not, even though `CLAUDE.local.md` documents nginx as
  waiting on `app`'s healthcheck. Either the dependency is not actually gating, or it gated once at
  startup and never re-evaluated. Both are misleading in the same direction: the stack *looks*
  serviceable.
- The documented workflow - "full stack reachable at `http://localhost:$UL_APP_PORT` once healthy" -
  cannot succeed in this checkout. Anyone following it gets a refused connection with no error to
  read, since the app log has been silent for at least 6 hours.

Also noticed: `.env` has `UL_APP_PORT=21810`, while `CLAUDE.local.md` states this slot's port is
21811. One of the two is stale; the connection is refused on 21810 regardless.

**Diagnosed one level further (chunk 352).** The cause is not a missing route or a dead process:

- healthcheck is `curl -f http://localhost:8000/health/`;
- the `health/` route **exists** (`UrbanLens/urls.py:109`, `HealthController.check`);
- `manage.py runserver 0.0.0.0:8000` **is running** (two processes - the reloader parent from Aug 04
  and a child);
- yet `curl` from *inside* the container returns **HTTP 000 for both `/health/` and `/`**.

So the dev server is wedged: alive, consuming CPU, not accepting connections. That rules out the
three cheap explanations (route missing, process crashed, port misconfigured) and leaves a genuine
hang.

One complication for anyone picking this up: the child `runserver` process restarted at 00:33 today,
which is when this session began `docker cp`-ing source into the container - the autoreloader will
have fired on those syncs. The **10-day failing streak predates all of that**, so the wedge is not
caused by the syncs, but the *currently running* process is one they restarted. A clean
`docker compose restart app` is the first thing to try, and would also confirm whether the wedge
reproduces from a fresh start.

**Pinned precisely (chunk 353): port 8000 is never bound.** Reading `/proc/net/tcp` inside the
container, no socket is listening on 8000 (hex `1F40`); the only listening port is `0xAA29` (43561),
an ephemeral socket. So `runserver` is not hung *serving* requests - it has never reached
`bind()`. With ~33 minutes of accumulated CPU on the child process, it is stuck **before** the
server starts: imports, system checks, migration checks, or the staticfiles/frontend build the
Dockerfile runs at boot.

That narrows the search a great deal. The next step is not networking - it is finding what runs
before the bind and can block indefinitely. `docker exec ... py-spy dump --pid <child>` (or
`faulthandler`) would name the exact frame.

**Chunk 354 complicates that story - record the evidence, not a tidy narrative.** Both processes are
`S (sleeping)` with `wchan 0` (an ordinary sleep, not blocked in a kernel call), each with **8
threads**, and the child has accumulated ~33 minutes of CPU since 00:33 - roughly 3% sustained.

That does not fit a single blocking call during startup:

- a process stuck early in imports would not have 8 threads;
- a one-time hang would not burn CPU steadily for 18 hours.

A polling loop fits better - Django's `StatReloader` scans every file each second, and a
crash-reload-crash cycle would keep the inner server from ever holding a binding. But **the app log
has been empty for at least 6 hours**, and a repeatedly crashing `runserver` should be printing
tracebacks. Either output is not reaching `docker logs`, or nothing is crashing.

**Resolved one branch (chunk 355).** Sampling `/proc/<pid>/stat` utime+stime twice, five seconds
apart, the child consumes **11 ticks in 5s - about 2.2% of one core, sustained**. The process is
genuinely *working*, not blocked. That eliminates the single-blocking-call explanation and matches
a poll loop, which is consistent with the ~33 minutes of CPU accumulated over 18 hours.

So the state is: **actively looping, never binding, silent logs.** The most likely remaining
explanation is Django's `StatReloader` polling while the inner server process fails to start or
repeatedly exits - the reloader survives, the child never holds port 8000. Under that reading the
silent logs are the anomaly to chase, since a failing child should print something.

`py-spy dump` on both pids would still name the frame in about a minute, and is the recommended next
step. Note that `/proc/<pid>/io` is not readable even via `docker exec -u root` here, so measure CPU
via `/proc/<pid>/stat` fields 14+15 rather than IO counters.

## OPEN 2026-08-14: the documented `docker cp` resync breaks the app container

**Root cause of the unhealthy-container entry above.** `CLAUDE.local.md` documents

```
docker cp src/urbanlens/. urbanlens_devs1_app:/app/src/urbanlens/   # resync without a rebuild
```

as the way to sync host changes into the container. The host tree contains
`src/urbanlens/logs/`, owned by the host user `apps` (**uid 568**). The container's app runs as
`appuser` (**uid 1001**). `docker cp` preserves the *source* ownership, so every resync hands the log
directory to uid 568 with mode `rw-rw-r--` - no write bit for others - and `appuser` can no longer
open it.

Django's logging config then fails at startup:

```
PermissionError: [Errno 13] Permission denied: '/app/src/urbanlens/logs/django.log'
ValueError: Unable to configure handler 'file'
```

which raises **before** `runserver` binds. That accounts for every symptom recorded above: no
listener on 8000, silent `docker logs` (the file handler never configures), sustained ~2% CPU (the
autoreloader retrying), and a process that is sleeping rather than blocked.

**Why it took so long to see.** `docker exec` defaults to **root**, so every diagnostic and every
`pytest` run in this session wrote to that log file successfully - `django.log` had a fresh
timestamp minutes before the investigation, which reads as "permissions are fine" and is the exact
opposite of the truth for the process that matters.

Ownership has been restored (`chown -R appuser:appuser /app/src/urbanlens/logs`), but the running
`runserver` will not recover on its own - it needs `docker compose restart app`.

**The workflow itself still needs fixing**, or the next resync reintroduces it. Options: exclude
`logs/` from the copy, `chown` after every `docker cp`, move the log directory outside the synced
tree, or make the log path configurable so the container writes somewhere it owns. Until then the
documented command should carry the `chown` as a second line.

## OPEN 2026-08-14: the dev database is 18 migrations behind the code

Found by reading Celery worker logs (audit chunk 359) - another finding no source analysis could
produce.

`manage.py showmigrations dashboard` inside `urbanlens_devs1_app` reports **18 unapplied
migrations**. The consequence is already visible in the worker log, 16 `django.db.utils.
ProgrammingError`s dated **2026-08-04**:

```
column dashboard_wikis.officially_created does not exist
column dashboard_site_settings.public_costs_page_enabled does not exist
column dashboard_profiles.photo_taking_preference does not exist
```

Same date the `app` container went unhealthy, so the two may share a cause (a boot sequence that
stopped part-way through migrate/collectstatic) or merely a trigger.

**Why the test suite cannot catch this.** pytest builds a *fresh* database from the migration files,
so a full green suite - 10,781 passing, run today - says nothing about whether the long-lived dev
database has had those migrations applied. The two are independent, and only the dev DB serves the
running app.

Fix is `manage.py migrate` in the container, but check first whether the boot sequence failing on
2026-08-04 left anything half-applied; the entry above about the wedged `runserver` is the likely
reason migrations stopped running at all.

**The 18 are `0026`-`0043`, and running them is not a routine catch-up (chunk 360).** Most are
schema, but at least three carry data:

- `0027_places_backfill` - backfills the whole `Place` hierarchy;
- `0039_encrypt_contact_and_note_fields` - **encrypts existing column data**, and per
  `docs/DATA_ENCRYPTION.md` key handling here is unforgiving; running it against a database whose
  `UL_FIELD_ENCRYPTION_KEY` differs from the one in use when rows were written is how data is
  orphaned;
- `0042_label_merge_duplicates` - merges duplicate labels, i.e. deletes rows, immediately before
  `0043_label_unique_constraint` adds the constraint that requires it.

**Corrected 2026-08-14 (chunk 364) after reading the migrations rather than their names.** The
claim that `0042`/`0043` "cannot be half-run" was wrong: neither sets `atomic = False`, so on
Postgres Django wraps each in its own transaction - `0042` either fully applies or fully rolls back,
and a failure in `0043` leaves merged data without the constraint, which is retryable.

The real risk is **irreversibility, not partial application**. `0042`'s reverse is a documented
no-op:

> "Merging cannot be undone - the dropped rows are gone. Reversing the migration removes the
> constraint, which is enough to get the schema back; the merged data stays merged."

So `migrate` forward is safe to attempt, but there is no way back to the pre-merge label data via
`migrate` at all. **Take a database snapshot** - that advice stands, for this reason rather than the
one originally given.

**`0039` verified too (chunk 365), and here the original warning was right.** `_encrypt_column`
rewrites each value in place through `_field.get_prep_value()` - i.e. under whatever
`UL_FIELD_ENCRYPTION_KEY` is active *at migrate time* - across 9+ columns including
`dashboard_profiles.phone_number`, `.bio`, `.signal_username`, `.matrix_handle`, `.discord_username`,
`.area`, both Google account tables' emails, and `dashboard_safety_contact_defaults.email`. Its
`reverse_code` is `migrations.RunPython.noop`.

**So both data migrations in the pending batch are irreversible**, which is the single strongest
argument for the snapshot: `migrate` forward is attemptable, but neither `0039` nor `0042` can be
walked back, and between them they rewrite personal contact data and delete label rows.

Confirm the encryption key in the container's environment is the one you intend to keep *before*
running this - per `docs/DATA_ENCRYPTION.md`, changing it afterwards outside the documented rotation
procedure orphans every row this migration writes.

**Ordering matters, and the safe order is not the obvious one (chunk 361).** Celery workers do not
autoreload, so they are still running the code they started with on 2026-08-04 - which matches the
*old* schema, which is why no `ProgrammingError` has appeared since. The container's `/app/src` has
since been resynced with current code, so **the moment the stack is restarted the workers pick up
new code against the old database and the schema errors return**.

So: `migrate` (after snapshotting) **before** `docker compose restart`, not after. Restarting first
to fix the wedged `app` container will also break the workers, which are currently healthy and
processing their hourly tasks normally.

This range matches the container-drift note already in `CLAUDE.local.md` ("30 tracked files behind -
missing `models/place`, `models/album`, `models/map_overlay` ... and migrations 0026-0038", dated
2026-08-06), so the drift has been known for over a week in one form and unrecognised as a
*database* problem.

## Note 2026-08-14: `trip.py`'s masking docstring cites an entry that is not here

`controllers/trip.py:135` (`_mask_trip_identities`, or its equivalent) opens:

> `docs/PROBLEMS.md` gap: ``services/identity_visibility.py`` masked the single-trip render sites
> (member panel, activity/comment attribution) but not the trips list...

**There is no such entry.** The masking gaps recorded here cover the data export, global search, and
reply/reaction notifications (all 2026-08-07); the only trips-list entry is about *query
amplification*, which is unrelated. Searching for "trips list" or trip identity masking finds
nothing matching.

Two readings, and the difference matters to whoever picks this up: either the gap was closed by the
very function carrying the comment and its entry was removed without updating the reference, or it
was never filed and the docstring is the only record. The comment's phrasing ("masked ... but not
the trips list") reads as *describing a gap that still existed when written*, which favours the
second.

Recorded rather than resolved - deciding which requires the history behind that function, and the
answer changes whether this is a stale pointer or an unfiled gap.

## Note 2026-08-14: "remove `docs/notes/ai/` committed secrets" does not describe this repository

`docs/designs/rejected-and-deferred/split-architecture.md` (phase 8, Hardening) lists "remove
`docs/notes/ai/` committed secrets and rotate...". That line will alarm anyone who reads it, so:
**verified, and it does not apply to this repository's history.**

- `git log --all -- 'docs/notes/ai/*'` returns nothing - no file under that path has ever been
  committed on any branch.
- `git ls-files docs/notes/` shows only `mobile_app_notes.md` and `mobile_app_requirements.md`; the
  `ai/` subdirectory is ignored (`.gitignore:49`) and untracked.

So there are no committed secrets from that path here. The line is most likely written against the
*post-split* repository the document is proposing, or it is stale. Either way it currently reads as
an unaddressed security item in this repo and is not one.

Worth leaving the line alone until someone who knows the split plan can date it - but worth having
the verification recorded next to it, because the natural reaction to "committed secrets" is to start
rewriting history, and there is nothing here to rewrite.

## Reference 2026-08-14: where per-viewer visibility is enforced (six mechanisms, six places)

Not a defect - an inventory, recorded because audit chunk 394 established that this codebase
enforces visibility **per subsystem** rather than through one convention. That is a reasonable design
(each subsystem's notion of "who may see this" genuinely differs), but it means no single grep finds
them all, and a reviewer who learns one mechanism will not recognise the others.

| mechanism | where | guards |
|---|---|---|
| `visible()` queryset method | `models/device_scan/queryset.py` | device-scan markers |
| `viewer_hidden_activity_ids` | `services/trips/trip_visibility.py` | trip activity locations |
| `display_identity_for` | `services/messaging/direct_messages.py` | sender names in DMs/group chats |
| `*_for_viewer` helpers | `controllers/safety.py`, `services/trips/trip_access.py` | safety + trip per-viewer reads |
| masking helpers | `services/profile/identity_visibility.py` | profile identity across surfaces |
| place-domain access | `services/wiki/wiki_access.py` | wiki visibility by place domain |

**Adding a new surface that returns another user's data means picking the right one of these six**, and
the audit found at least one historical bug in each of the first four categories' problem space
(reply/reaction notifications naming masked people, the Google Calendar export leaking hidden
coordinates, trip location visibility re-implementing the shared gate more strictly, the data export
disclosing masked members). Those are the recurring shape: a *new* surface that did not consult the
gate its subsystem already had.

## Reference 2026-08-14: audit of all 26 code references to this file

Every source file citing `docs/PROBLEMS.md` was checked (audit chunks 370-405).

| outcome | files |
|---|---|
| resolve to an entry | 16 |
| **dangling** | **8** |
| unresolved (subject may be filed under another description) | 2 |

**Seven of the eight dangling references cite the same thing**: a decision dated 2026-07-23, or
`completed.md` by name. Both live in `docs/notes/ai/`, which is **gitignored** (`.gitignore:49`) and
was never committed. So this is one absent document referenced from eight places - not eight
independent omissions - and the fix is either to promote those decisions into a tracked file or to
stop citing an untracked one from tracked code.

The two unresolved are `services/spotguessr/__init__.py` and `services/trivia/__init__.py`,
describing an import-order failure that celery workers trigger and `manage.py check` does not. The
nearest entry (`PinViewSet.basename` / `get_default_basename`, root cause not found) shares the shape
but not obviously the subject.

**What makes a reference findable**, from the 16 that worked: the comment contains a *distinctive
searchable string* - a symbol (`MapController.resolve_place`), a flag (`strict=True`), a date, a
quoted entry title, or a concrete symptom (`{"ok": true}` and the field never changes). What fails is
describing the problem in general words ("the report", "option (a)", "the trips list"). The single
best example in the codebase is `services/messaging/direct_message_shares.py`, which quotes its
entry's title verbatim.

## Note 2026-08-14: `SECRET_KEY` falls back to a per-process random key with no environment guard

Found by audit chunk 441. **No hardcoded secrets exist** - all 11 secret-named settings read from the
environment, and `EMAIL_HOST_PASSWORD` defaults to `""` rather than a weak value. One line deserves
a second look:

```python
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()
```

A random fallback is the right instinct - far better than a checked-in default someone forgets to
override. But it is **generated per process**, and nothing branches on `ENVIRONMENT_NAME` (defined
eight lines below) to require the variable outside local development. Consequences if
`DJANGO_SECRET_KEY` is ever unset in a multi-process deployment:

- gunicorn workers, the Daphne ASGI process and each Celery worker get **different keys**, so a
  session or signed value created by one is invalid to the others - presenting as random logouts
  rather than a configuration error;
- `EncryptedTextField` derives from `SECRET_KEY` when `UL_FIELD_ENCRYPTION_KEY` is unset (see
  `docs/DATA_ENCRYPTION.md`), so encrypted columns written under a random key become unreadable on
  the next restart.

**Not observed - this is a hazard, not an incident.** Production presumably sets the variable, and
`.env` handling is in place. The cheap hardening is a startup check: if `ENVIRONMENT_NAME` is not
local/development and `DJANGO_SECRET_KEY` is unset, raise `ImproperlyConfigured` rather than
generating one. That converts a silent, confusing failure into a loud one at boot.

## Reference 2026-08-16: why two callers of one function legitimately catch different exceptions

Chunk 537 worked the rest of chunk 536's divergence list - the sweep that found the archived
safety-chat 500 by comparing what each caller of a function catches. Six more candidates read, **six
false positives**, and the reasons are different enough that the list is worth keeping: it is what
makes that sweep re-runnable without re-deriving the same judgements.

Divergence is *expected*, not suspicious. A caller catches less than its sibling when:

1. **It guards before the call instead of after it.** `controllers/visits.py` checks
   `visit_logging_allowed(...)` and answers 403 before ever calling `create_manual_visit`, so
   `VisitLoggingDisabledError` is unreachable - and its comment says exactly why the redundant
   service-side check stays ("403 rather than a confusing 400"). `controllers/userprofile.py` bounds
   the trust rating to the valid range before calling `set_trust`, routing anything else to
   `clear_trust` - out-of-range is that widget's "clear" signal, not an error.
2. **It constructs the payload itself, so the raising branch cannot be reached.** The HTML pin
   editor builds `edits` from a fixed key set, all within `EDITABLE_PIN_FIELDS`, and never passes
   `visited`, so neither of `apply_pin_edits`' two `PinEditError` branches can fire. The API accepts
   a client-supplied field set and must catch.
3. **The arguments make the branch impossible.** `views_messaging.py`'s self-leave endpoint calls
   `remove_group_member(group, profile, profile)`; the `GroupChatPermissionError` branch is about
   removing *other* members, so only the validation branch is reachable - and it catches the shared
   `ValueError` base anyway, which is broader than its sibling's two named types, not narrower.
4. **The catch is doing a different job.** `controllers/pin_suggestions.py` wraps
   `accept_pin_suggestion` in a bare `except Exception` *inside a loop*, to stop one bad suggestion
   aborting a bulk action. The API endpoint handles a single suggestion, where there is nothing to
   isolate it from. `accept_pin_suggestion` declares no `Raises:` at all.
5. **The scan crossed a function boundary.** `join_trip` raises nothing; the `Raises:` attributed to
   it belonged to `leave_trip`, the next function in the file, inside a `grep -A 40` window. The
   same artifact class recorded earlier in this audit.

Add the one from chunk 536 - a handler in a **base class**, one frame above the call, which an
intra-function scan cannot see - and that is six ways to be correct while looking divergent.

The sweep is still worth re-running; it found a real 500 on a safety path. But its yield is roughly
one in ten, and every one of the nine needs reading rather than triage by shape.

## Enforced 2026-08-16 (chunk 557): a view's handlers must accept every parameter its routes supply

Three chunks in a row produced the same shape, and the third made it a class worth sweeping rather
than a coincidence worth noting:

| route | handler | missing | found in |
| --- | --- | --- | --- |
| `saved_filters.new` | `SavedFilterEditView.post` | `filter_uuid` was *required*, only `edit/` supplies it | chunk 552 |
| `pin.link.to` | `PinRelinkView.get` | `location_slug` absent entirely | chunk 556 |
| `pin.link` | `PinRelinkView.post` | (the filed detach product decision, not a signature fault) | chunk 551 |

Django resolves handler arguments at **request** time, so the failure is a `TypeError` that only
appears when somebody requests the mismatched route - and both real instances were on routes no UI
path exercises, which is exactly why they survived.

Swept directly: for every view class wired to two or more routes, does each handler accept the union
of the parameters those routes can pass? **753 view classes, 48 of them multi-route, zero
mismatches.** The class is closed.

`test_view_signature_route_guard.py` keeps it closed, because adding a route to an existing view
re-opens it silently - nothing at import time or in review notices. **Verified to bind**: restoring
`PinRelinkView.get`'s pre-fix signature makes it report exactly that method and parameter.

Two details it inherits from earlier mistakes here: parameters accumulate down the resolver tree
(reading only leaf patterns is what made `test_route_query_scaling`'s second version blind to most
parameterised routes, per its own docstring), and a handler taking `**kwargs` is skipped because it
accepts anything by construction.

## Keyboard-invoked context menu may swallow the next activation (unverified)

`label-picker.ts`'s `isMouseContextMenu` decides whether to arm click-suppression after a
`contextmenu` event:

```ts
const pointerType = (event as PointerEvent).pointerType;
return pointerType ? pointerType === "mouse" : event.button === 2;
```

`contextmenu` is a `MouseEvent`, so `pointerType` is generally absent and the `button === 2`
fallback decides. That correctly distinguishes a mouse right-click (button 2, no suppression) from
a touch long-press (button 0, suppression armed). But a context menu invoked from the **keyboard**
- the Menu key, or Shift+F10 - also reports `button === 0`, so it would arm the suppression with no
follow-up click ever coming. The guard then stays set until some unrelated later click, which is
exactly the failure the function's own docstring describes: "swallowing keyboard (Enter/Space)
activations in the meantime".

Not fixed here because it cannot be confirmed without a real browser, and the plausible
discriminator (`event.detail === 0` for keyboard-invoked menus) is a behaviour I would be asserting
rather than observing. Worth ten minutes with DevTools on the Organize page label picker: press the
Menu key on a label chip, then try to activate any chip with Enter, and see whether the first
Enter is swallowed.

## The planning and handoff documents referenced across the docs do not exist

Three different paths are cited for "what is planned" and "what previous agents did", and none of
them is in the tree:

| Path | Cited by | Status |
| --- | --- | --- |
| `TODO.md` (repo root) | `docs/FEATURES.md:4`, `docs/NOTES.md:344,402`, `docs/ROADMAP.md:4,13,124`, `CLAUDE.local.md` | Existed - 416 lines - deleted in `3f12e875` ("Release v0.5.0b0") |
| `docs/prompts/completed.md`, `docs/prompts/todo.md` | `CLAUDE.local.md` | Never tracked in git |
| `docs/notes/ai/completed.md`, `docs/notes/ai/todo.md` | `docs/ROADMAP.md`, `docs/designs/place-consolidation.md` | **Gitignored, not missing** - see the earlier `completed.md` entry |

This is not cosmetic. `CLAUDE.md` and `CLAUDE.local.md` both instruct contributors (including agents)
to consult these before assuming something is unbuilt or unplanned.

**Corrected 2026-08-17 (chunk 607): the ticket ids are *not* unresolvable, and this entry originally
said they were.** The root `ROADMAP.md` - a separate document from `docs/ROADMAP.md`, and one I had
not opened when filing this - carries 251 `UL-` references, including UL-294, UL-70, UL-360 and
UL-277, each against a one-line description of the planned work. So a reader chasing "see `TODO.md`
UL-294" can find what UL-294 *is*; what they cannot find is the file the citation names, or whatever
additional context it held. That is a smaller problem than the one first written here, and the
difference matters to whoever decides what to do about it. `docs/ROADMAP.md`
says it was itself "generated 2026-07-18 from a full review of `TODO.md`" and tells readers to keep
that file updated alongside it. Anyone following those instructions finds nothing and cannot tell
whether the answer is "not planned" or "the document is missing".

`TODO.md`'s content is recoverable:

```bash
git show 3f12e875~1:TODO.md > TODO.md
```

Whether it *should* come back is the owner's call - it was removed in a release commit, which may
have been deliberate. But the current state is the worst of both: the file is gone and five separate
documents still treat it as live. Either restore it or update those references; the same choice
applies to the two agent-note directories, where the fix may simply be deleting instructions that
point at paths which never existed.

**Corrected 2026-08-17.** The `docs/notes/ai/` row above overstated the case, and an earlier entry
in this same file had already established why: `.gitignore:49` ignores that directory, so those
files are local-only agent notes rather than lost ones. That entry also states the structural
problem better than this one did - *tracked documentation referencing gitignored content* - which
applies to `docs/ROADMAP.md` and `docs/designs/place-consolidation.md` citing them, and is a
different defect from a file being deleted.

What remains specific to this entry, and is not covered there: root `TODO.md` **was** tracked, in
git, and was removed in the `3f12e875` release commit while five documents went on citing it as
live - including `docs/NOTES.md` quoting ticket ids inside it. `docs/prompts/` is a third path,
cited by `CLAUDE.local.md`, that matches neither pattern.

Not actioned here because recreating 416 lines of someone else's planning document, or editing
four documents' cross-references, is a decision about the project's own record rather than a defect
in its code.

## A group message can still be sent under a key version a removed member holds

Removing a member from an encrypted group correctly flags `needs_rotation` (the group-key GET
compares envelope holders against current members with `!=`, so removals and additions both trip
it), and that is now pinned by a test. But rotation is client-driven, and the send path only
validates `key_version >= 1` - it never compares against the group's current version.

So a sender whose client has not yet refreshed - an open tab, a client that missed the rotation
prompt - keeps encrypting under the version the removed member still holds an envelope for. The
group's own members are unaffected; the question is only whether the removed member can read
messages sent after their removal.

In-app they cannot: `GroupMessageQuerySet.visible_window` bounds each member to their membership
stint, so the ciphertext is not fetchable once `left_at` is set. The exposure needs the ciphertext
obtained some other way - captured traffic, a database copy, a compromised host - which is precisely
the threat model end-to-end encryption exists for, so it is not nothing.

**Why this is filed rather than fixed.** The obvious server-side fix is to reject a send whose
`key_version` is behind the current one while `needs_rotation` is set. Any member may rotate (not
just the creator) and the concurrent-rotation race is already handled, so that much is safe. What is
not safe is the failure mode: rotation requires *every* member to be enrolled, and returns 409 when
one is not. A single un-enrolled member would then block the whole group from sending, turning a
confidentiality gap into an availability outage. Trading one for the other is a product decision.

Options, roughly in increasing cost: have the send path warn/log when it accepts a stale version;
have clients re-check rotation state before send rather than on poll; or reject stale-version sends
only when the group is fully enrolled (so the 409 case cannot arise).

## A deleted message's preview survives in the recipient's notification list

Fixed in chunk 572: the *delayed email* and *delayed WhatsApp/SMS alert* for a direct message now
skip a message the app would show as a tombstone, so unsending inside the 120-second delay window
stops the out-of-band copy going out.

Not fixed, because it needs a schema decision: the **on-site notification** raised for the same
message keeps its preview text.

- `services/messaging/direct_messages` stores `message=preview` - up to 120 characters of the body.
- `services/messaging/group_chats` stores `message=f"{sender}: {preview}"`, likewise 120.

Neither is touched by `delete_message_for_everyone` / `delete_group_message`, so after the sender
unsends, the thread shows "Message deleted" while the notification row still quotes what was said.

There is no way to clean it up precisely today: `NotificationLog` has no reference to the message it
was raised for, and its `url` points at the *thread* (the conversation, or the group), not the
message. Matching rows heuristically on profile + type + url + timestamp would be fragile and would
sooner or later delete the wrong notification.

Options:

1. Add a nullable generic reference (or a `message_uuid`) to `NotificationLog`, and clear or redact
   matching rows when a message is deleted. Cleanest, costs a migration.
2. Render notification previews through the message at display time rather than storing them, so a
   tombstone applies everywhere at once. Cleanest conceptually, largest change - and the stored text
   currently doubles as the push/e-mail body.
3. Accept it, and say so in the UI: the notification was already delivered when the message was
   live, which is arguably the same as the recipient having read it.

Worth deciding rather than leaving implicit, because the app currently promises "Message deleted" in
one surface while quoting the message in another.

## An export whose cleanup fails to enqueue is never swept

`run_export` schedules `cleanup_export_artifacts_task` in a `finally`, so every path - success, a
failed user load, an exception mid-export - asks for cleanup. Worker loss is covered too: the Celery
settings deliberately reconcile `visibility_timeout` against the longest countdown this app
schedules, which is this one at 3600s, so an unacked cleanup is redelivered.

The uncovered path is the enqueue itself. `schedule_export_cleanup` uses `safely_enqueue_task`, and
when that returns None (broker unreachable, which the settings above are tuned to fail *fast* on) it
logs `"Unable to schedule cleanup for export directory %s"` and returns. Nothing else ever looks at
that directory, so a ZIP containing the user's entire account - pins, photos, messages, profile -
stays on disk indefinitely, and the only record is one warning line.

Low frequency, but the retention story is "it is deleted an hour later" and in this case it is not.
This codebase already uses periodic backstops for exactly this kind of single-mechanism dependency:
`sync_stripe_subscriptions` is described as a "safety net for missed Stripe webhook deliveries", and
`SafetyCheckinChatConsumer` revalidates every 60 seconds "as a backstop for a dropped
partner_access_revoked broadcast".

The matching fix would be a periodic sweep of export/import working directories older than their
TTL, which needs no per-job bookkeeping - the directory's own mtime is enough. Filed rather than
added because it means introducing a beat task, and how aggressively to reap those directories is
an operational choice.

## Should logging out wipe the cached E2EE keys? (product decision, filed 2026-08-17)

`frontend/ts/shared/e2ee-store.ts` caches decrypted E2EE material in IndexedDB - the identity private
key, and every unsealed conversation and group key - so day-to-day use never prompts for a password
or recovery key. Nothing clears it on logout. `clearProfileKeys` exists and does the right thing, but
its only caller is the key-reset flow.

**This is deliberate and documented**, which is why it is a question rather than a defect. The
module's own header says the cache is keyed by profile slug so two accounts sharing a browser cannot
read each other's rows "by accident", and states the boundary plainly: "same-origin storage is the
trust boundary either way - this is bookkeeping, not isolation."

The question is whether an explicit logout should be treated differently from a page close. Someone
logging out on a shared or borrowed machine would probably expect their decrypted message keys to go
with them; someone logging out and back in on their own laptop would probably not expect to re-enter
a recovery key. Both are defensible, and the tradeoff is a product call rather than an engineering
one, so it is recorded rather than decided.

If the answer is "yes", `clearProfileKeys(selfSlug)` on logout is the whole change. Note also that
its docstring offers "logout-everywhere / key reset" as its purpose while no logout-everywhere
feature exists anywhere in the codebase - worth correcting whichever way this is decided.

(Raised while tracing the messaging/E2EE surface. It was recorded in
`docs/reports/2026-08-11-codebase-audit.md` at the time and carried in that session's running list of
owner decisions, but never written here until now - which is what made it worth catching: a filed
item that lives only in a narrative report is not filed.)

## `clone_prod_to_staging.sh` can leave a production dump on disk, and can report success after failing

Found 2026-08-17 while reading `bin/`, which had not been examined. Two issues, both from the same
cause: the script has `#!/bin/bash` with **no `set -e`, no `set -o pipefail`, and no `trap`**.

**1. A failed run leaves a full production dump on the operator's disk.** The sequence is: dump inside
the prod container (147), `docker cp` it out to `./$DUMP_FILE` (148), remove the container copy (149),
bring up staging (152), *wait for it to become healthy* (153), restore (158), and only then remove the
local file (161-163). `wait_for_healthy` ends in `exit 1` on timeout - which lands between the copy
out and the cleanup. So if the staging database does not come up, a `pg_dump -Fc` of production -
every user's email, phone number, encrypted personal fields, safety check-in locations, messages and
photo metadata - stays in the working directory, and nothing says so.

**2. Failures do not stop it.** Without `set -e`, a failing `pg_dump` still proceeds to `docker cp`
and `pg_restore`, and a failing `pg_restore` still reaches `docker compose up --build` and prints
`Done. Staging now mirrors production as of $TIMESTAMP.` A partially-restored staging environment
that announces success is worse than one that stops.

**This is an omission, not a house style.** Its sibling `bin/deploy.sh` opens with
`set -euo pipefail` at line 20; the clone script, which is the one handling a production data dump,
does not. Same directory, same author, one hardened and one not - the shape this audit kept finding
in application code, here in the ops scripts.

(For completeness, since `bin/` was being read: `bin/deploy_webhook.py` is sound. Both signature
schemes - GitHub's HMAC-SHA256 and GitLab's shared token - are compared with
`hmac.compare_digest`, not `==`.)

The remedy is small - `set -euo pipefail`, and a `trap` that removes `./$DUMP_FILE` on any exit unless
`--keep-dump` was passed - but **not made here**, deliberately: this script drops and replaces a
database, its failure paths are exactly what would change, and there is no way to exercise it in this
environment without prod and staging stacks. An untested edit to the error handling of a script whose
happy path destroys data is a bad trade for the size of the fix.

Worth also deciding separately: the clone copies production personal data into staging **unscrubbed**.
That may well be intended - staging with real data reproduces real bugs - but it is worth being a
decision rather than a default, and it interacts with `UL_FIELD_ENCRYPTION_KEY`: if staging shares
production's key the encrypted columns are readable there, and if it does not, they are permanently
undecryptable in staging (which is fine, but means those code paths are never exercised).

## `deploy.sh` reports success without waiting for the stack to come up

Found 2026-08-17 alongside the `clone_prod_to_staging.sh` entry above, reading `bin/` for the first
time. This one is well-guarded in most respects - `set -euo pipefail`, a refusal to deploy with a
dirty working tree, and an early exit when `origin/$BRANCH` already matches `HEAD`. The gap is at the
end:

```
docker compose down
docker compose up --build -d
log "==> Deploy complete at $(git rev-parse --short HEAD)"
```

`up -d` returns when containers have *started*, not when they are serving. `docker-compose.yml` gives
`app` and `app-ws` healthchecks with 30s and 25s start periods, and this project's own notes describe
the app healthcheck as not passing until migrations, `collectstatic` and the frontend build have
finished - minutes on a cold build. So "Deploy complete" is printed while the site may still be
starting, and prints identically if the new image never becomes healthy at all. `set -e` does not
help: `up -d` genuinely succeeded.

Combined with the `down` on the line before, the failure mode is: site goes down, new stack fails its
healthcheck, script exits 0 reporting success, site stays down. `bin/deploy_webhook.py` shells out to
this script, so an automated deploy answers the Git host with success in exactly that case.

**Same sibling evidence as the entry above, pointing the other way.** `clone_prod_to_staging.sh` -
the *staging* script - defines and uses a `wait_for_healthy` helper, twice. `deploy.sh` - the
*production* one - has no health check at all. Between the two scripts, each has the safety the other
is missing.

The fix is close to free: lift `wait_for_healthy` (or `docker compose ps --format` polling) into
`deploy.sh` after `up --build -d`, and fail the deploy if the app never becomes healthy. **Not made
here** for the same reason as above - the script hard-resets the working tree and rebuilds the running
stack, so it cannot be exercised in this environment, and its failure handling is precisely what the
change would alter.

## `.dockerignore` does not exclude `.env`, so deploy-host image builds bake the secrets in

Found 2026-08-17 reading the Dockerfile, which had not been examined. `.dockerignore` lists caches,
`node_modules`, `docs/`, virtualenvs and editor directories - but not `.env`. The Dockerfile then does
`COPY --chown=appuser:appuser . /app`, copying the whole build context.

`.env` is correctly gitignored (`.gitignore:53`) and is 3.3 KB of real secrets on this host:
`DJANGO_SECRET_KEY`, database credentials, `UL_FIELD_ENCRYPTION_KEY`, Stripe keys, OAuth client
secrets and API keys for the plugin fleet. Keeping it out of git and then copying it into the image
undoes most of the benefit.

**Scope, stated precisely, because it is narrower than it first looks.** Images published to
`ghcr.io` by `.github/workflows/publish.yml` are built from `actions/checkout`, which produces a
clean git checkout - `.env` is gitignored and therefore absent, so **published images do not contain
it**. The exposure is images built *on the deploy host*, which is exactly how `bin/deploy.sh` builds
them (`docker compose up --build`). There, anyone who can read the image can usually already read
`.env` on the same filesystem, so the practical gap is narrow: `docker save`/export of that image, or
Docker-daemon access without filesystem access.

**Not fixed here**, and the reason is worth recording because it is not squeamishness:
`settings/app.py` deliberately loads `Path(DEFAULT_ROOT, ".env")` - the Pydantic settings read that
file from disk, with a comment explaining where it lives relative to `src/`. `docker-compose.yml`
supplies configuration through `environment:` blocks rather than `env_file:`, so the baked copy is
*probably* redundant, but "probably" is not enough to justify a change that could alter how a
production container resolves its configuration, in an environment where I cannot run the real
deployment to find out.

**`.git` is a different matter and is *not* a defect.** It is also copied in, and that is deliberate:
`core/version.py` shells out to git to compare the deployed commit against upstream, and the
Dockerfile configures `git config --system safe.directory /app` for development images so that keeps
working. Excluding `.git` would break the version check. Noting it explicitly so nobody "fixes" it.

## `npm run git-squash` is a force-deploy with none of `deploy.sh`'s guards (minor)

Noted 2026-08-17 while confirming that `gunicorn.conf.py` is actually loaded. `package.json` defines:

```
"git-squash": "pkill gunicorn && git fetch origin && git reset --hard origin/main && npm run start"
```

Two things about it, neither urgent:

1. **It hard-resets to `origin/main` with no dirty-tree check.** `bin/deploy.sh` refuses to deploy
   when the working tree has uncommitted changes, and says so; this one discards them silently. Same
   repository, same operation, and the safety exists in one place only - the pattern already recorded
   for `deploy.sh` versus `clone_prod_to_staging.sh`.
2. **The `&&` chain aborts if gunicorn is not running.** `pkill` exits non-zero when nothing matched,
   so on a host where the server is already stopped the script fetches nothing, resets nothing and
   starts nothing. That direction fails safe, but silently, and the name gives no hint that it stops.

Also worth noting the name: it neither squashes nor touches git history - it is a force-redeploy. A
reader reaching for it expecting a history operation gets a hard reset and a server restart.

Not changed: it is a convenience script in `package.json`, its behaviour may be exactly what its
author wants at a terminal, and `bin/deploy.sh` already exists as the safe path. Recorded so the
difference between the two is a choice rather than a surprise.

### Pin suggestion `hit_count` is a read-modify-write (noted 2026-08-17)

`services/pins/pin_suggestions.py`'s `_upsert_matched_suggestion` and
`_upsert_new_pin_suggestion` do `existing.hit_count += _weight_of(...)` and save, and their caller
`ingest_location_hits` takes no lock. Two concurrent ingests for one profile - a repeated Immich
sweep overlapping a local-scan upload, which the function's own docstring names as the case it
handles - lose an increment, and can also both miss on the check-then-act and create duplicate
pending suggestions for the same pin.

Left unfixed deliberately: unlike the ledger/wiki/rating cases, `PinSuggestion` rows are per-profile,
so contention needs one user running two scans at once, and the damage is an undercounted ranking
signal rather than lost money, a reverted edit, or a discarded rating period. Worth an `F()`
expression plus a unique constraint if suggestion quality is ever reported as flaky.


### Fourteen documentation citations still point at the wrong line (noted 2026-08-17)

`bin/check_doc_line_refs.py --report-drift` lists them. They survived the 2026-08-17 sweep because
they can't be repaired mechanically, and they split into two kinds:

- **The line moved, but the anchor isn't a definition.** `settings/base.py:343` for
  `hard_delete_expired_direct_messages` (now line 374) is cited via its entry in the beat-schedule
  dict, and `tasks.py:1629` for `RUN_LOCK_CACHE_KEY` via an import. Renumbering these is safe but
  needs a human to confirm which usage was meant.
- **The symbol no longer exists at all.** `controllers/trip.py:135` cites `_mask_trip_identities`
  and `services/ai/anthropic.py:117` cites `send_prompt`/`send_prompt_list`; neither name appears
  anywhere in the tree now. The repair is rewriting the sentence around whatever replaced them, not
  changing the number - and guessing at that would put invented history into the record.

The eight that *were* mechanically provable (anchored on a `def`/`class` the tool could locate
uniquely) are fixed, and `check_doc_line_refs.py` now runs in CI to keep past-end-of-file citations
at zero.

## (ORIGINAL FILING) OPEN 2026-08-17: blocking leaves a saved emergency-contact default pointing at the blocked profile

Found alongside the `MarkupMapShare` revocation, and left for an owner's decision because it is a
product question rather than a defect.

`EmergencyContactDefault` is a *template*: it is copied onto each new `SafetyCheckin` as a
`SafetyCheckinContact` snapshot at creation time. Blocking someone does not remove it, so a check-in
created *after* the block still copies the blocked profile in as an emergency contact - and the
safety escalation path will page them.

Both answers are defensible, which is why this is filed rather than fixed:

- **Remove it on block.** Consistent with how blocking already treats safety-partner access, and
  avoids the surprise of paging someone you blocked.
- **Leave it.** The default is the owner's own saved data, and silently deleting a safety contact is
  destructive in a feature whose whole purpose is that someone is notified when you do not check in.
  An owner might block a person socially and still want them called if they go missing.

A third option, probably the best of the three: keep the row, and warn at check-in creation when a
default resolves to a blocked profile - which informs without deciding for them.

Everything else that links two profiles was enumerated while checking this (twenty models). The rest
are either already handled (`Friendship`, `SafetyCheckinPartner`, pending `PinShare`,
`DirectMessageTemporaryAccess`'s read-time veto), private annotations the author owns
(`ProfileNote`, `ProfileNickname`, `ProfileTrust`, `ProfileLabelAssignment`), or deliberately
preserved history (`DirectMessage`, `ConversationKey`).

## OPEN 2026-08-17: two encryption migrations disagree about whether encrypting is reversible

`0007_pinshare_bundled_with_markup_map_removed_flags.py` encrypts credential tokens and carries a
real decrypting `reverse_code` - the guard's own note calls that "exactly the case that must NOT be
noop". `0048_encrypt_preference_and_contact_label.py` encrypts two preference columns and reverses to
`RunPython.noop`, with the opposite reasoning stated just as plainly: a reverse would have to decrypt
under whatever key is active at rollback time, and getting that wrong writes garbage over real data,
so restore from a backup instead.

Both arguments are sound in isolation and they cannot both be the rule. The question is which risk the
project accepts: a reverse that can corrupt data under a rotated key, or a reverse that silently
leaves a column unreadable by the pre-migration code. Whichever is chosen should be written down in
`docs/DATA_ENCRYPTION.md` and applied to both, because the next person encrypting a column will copy
whichever migration they happen to open.

Found by `test_migration_noop_reverse_guard`, which forces exactly this review and had not yet seen
0048 - it arrived in the 2026-08-17 merge.

## Two tests fail only under a randomized full-suite run (2026-08-18)

The full suite on `810edd7b` reported three failures. One was real and is fixed
(`test_share_pin_copy_fidelity` - `Pin.buildings_auto_nested_at` was added without being
listed as copied-or-skipped, which is exactly what that guard exists to catch). The other two
pass in isolation and pass together, so they are order-dependent rather than broken:

- `test_safety_chat.py::SafetyCheckinChatConsumerTests::test_owner_and_contact_exchange_messages`
- `test_migration_0039_reverse.py::Migration0039ReverseTests::test_encrypt_decrypt_round_trips_and_ciphertext_is_discriminable`

Neither touches anything the floorplan/auto-nest work changed, and the same two passed in the
earlier clean full run on `90cf9c97`, so the trigger is whatever ordering `pytest-randomly` chose
that run - a consumer left connected, or key/settings state leaking from an earlier test, are the
two shapes worth looking at first. `-p no:randomly` hides it; reproducing needs the failing seed,
which this run did not record because `-q` suppressed the header.

Worth fixing properly rather than pinning the seed: an order-dependent test is a test that will
fail on someone else's machine for no visible reason. When picking it up, run the full suite with
`-p randomly --randomly-seed=<n>` and bisect with `pytest --randomly-seed=<n> -x`.
