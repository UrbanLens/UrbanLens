# PROBLEMS

Bugs or quirks identified during other work but out of scope to investigate/fix at the time.
Each entry should have enough detail (repro steps, file:line, symptoms) for a future session
to pick up without re-discovering the problem from scratch.

## RESOLVED 2026-08-05: 38 test failures across search, media-auth, and the REData provider gateways

A sweep over `-k "quota or media or album or photo or storage or upload or relevance or wiki_media
or site_settings or search or redata or dm_search"` went from **38 failed / 1698 passed** to
**1704 passed, 0 failed**. All three causes were the same shape - production code changed
deliberately and its tests were never updated - which is why none of them had an obvious owner.

**1. 36 REData provider tests depended on the test machine's credentials.**
`test_redata_media_gateway.py` and `test_redata_reference_documents_gateway.py` mock the gateway's
`lookup`/`search`, but the providers construct `RedataMediaGateway()` / `RedataReferenceDocumentsGateway()`
themselves, and `RedataGateway.__post_init__` raises `ValueError("UL_REDATA_API_URL must be
configured.")` when the env has no REData credentials. So these passed on a box with credentials
and failed on one without - the exact ambient-state dependency this file documents repeatedly
(see cause 3 in the 2026-07-27 entry, which was the same bug inverted). The shared mixin helpers
now no-op `__post_init__` alongside the mocked call, matching how `test_pin_redata_media_proxy.py`
forces the *unconfigured* state. **Deliberately not fixed by pinning dummy REData settings in
`settings/test.py`**: a dozen `is_configured()` helpers read those values, so configuring them
globally would silently flip behaviour in unrelated tests.

**2. `test_media_auth_mixin.py::test_session_wins_over_a_credential_header` encoded a
since-reversed security decision.** `CredentialOrSessionMediaMixin.resolve_media_profile`'s own
docstring explains at length why a *presented credential now wins over an ambient session*: the
old order let a WebView sharing the site's cookie jar fetch media as whichever account was logged
in, and let a token without `media:read` bypass the scope check entirely whenever a session was
also present. The test still asserted the old order. Rewritten to assert the current behaviour,
plus a new sibling (`test_an_unscoped_credential_cannot_fall_back_to_the_session`) covering the
half with actual teeth. The throttle concern the old test's docstring cited is already covered by
`test_throttle_is_not_charged_to_session_requests`, which sends no header at all.

**3. Two search tests predated deliberate narrowing of the query parser.**
- `test_global_search_engine.py::test_finds_photo_by_generated_keyword` searched `"staircase
  photos"`. `_extract_type_keywords` had been restricted to the query's *first* word (to stop
  "please visit my page" becoming a visits-only search), which also killed the equally natural
  trailing form. **This one was a real product gap, not a stale test** - it now matches a type
  keyword at either end of the query, which restores "abandoned mill photos" while leaving
  mid-sentence matches alone. A trailing keyword that turns out to be part of a real name
  ("Road Trip") is recovered by the engine's existing zero-result fallback, which retries with
  inferred types cleared.
- `test_dm_search.py::test_date_range_phrase_filters_by_created` searched `"reunion 2024"`. The
  parser deliberately requires a preposition before a bare year, because a 4-digit token appears
  in ordinary names ("Building 2024", "Route 2027") - the pattern carries a comment saying so.
  The test now uses `"reunion in 2024"`, with a new sibling asserting the bare form stays a plain
  text search so that narrowness doesn't silently regress.

## OPEN 2026-08-05: `bun run build` (`bin/build-frontend.ts`) fails with "Formats besides 'esm' are not implemented"

Found while verifying a photo-thumbnail zoom-scaling fix in `map-annotations.ts`. `bun run build`
bundles every entry successfully, then errors out on that message and exits 1 without writing the
final static output for at least `achievements.js`/`article-wysiwyg.js`/etc. (the earlier committed
static files stay in place from the last successful build, so the app itself isn't visibly broken -
this is a fresh-build/CI concern, not a runtime one). Confirmed pre-existing and unrelated to any
in-progress change by `git stash`-ing all working-tree edits and re-running: identical failure on
a clean `@release/v_0_7_0` checkout. Not yet root-caused - worth checking whether one of the entry
points (or a plugin in `bin/build-frontend.ts`) requests a non-ESM output format that this Bun
version's bundler no longer supports. `bun run typecheck` and `bun run test:ts` are unaffected and
both still work normally.

## DANGEROUS: `delete_low_engagement_wikis` deletes *every* wiki - its filter is commented out

`management/commands/delete_low_engagement_wikis.py:62` is a commented-out line:

```python
#.filter(Q(pin_owner_count__lte=MAX_PIN_OWNERS) | Q(user_edit_count=0))
```

so the queryset it builds is every `Wiki` in the database. With `--yes` the command deletes all
of them (cascading to child wikis, edits, and related records). The dry-run report still prints
each wiki's real `pin_owners`/`user_edits` counts, so the output *looks* like it selected
correctly - a wiki with `pin_owners=3 user_edits=1` is listed and then deleted.

Committed that way (not a working-tree edit - `git show HEAD` confirms), so it has been live for
a while. Two tests already encode the intended behaviour and currently fail because of it:
`test_delete_low_engagement_wikis.py::DeleteLowEngagementWikisTests::test_no_matches_reports_and_deletes_nothing`
and `::test_wiki_kept_with_enough_pin_owners_and_a_user_edit`.

Found 2026-08-04 during the Place refactor; deliberately not fixed there, since a destructive
command's behaviour should not change as a side effect of an unrelated refactor. The fix looks
like uncommenting the line, but someone should confirm it wasn't disabled on purpose first.

## `.badge--muted` is used everywhere but never defined

Found 2026-07-31 while building the PinImportFailure review queue. `_pin_suggestion_card.html`,
`_pin_merge_suggestion_card.html`, and `_pin_import_failure_card.html` all render
`<span class="badge badge--muted">...</span>` for their origin/reason badges, but grepping the
SCSS source turns up only a bare `.badge` rule (a right-floated "count badge" style) - no
`.badge--muted` modifier is defined anywhere. These badges have likely been rendering with just
the base `.badge` look (or unstyled, depending on cascade) on every card that uses them since
whichever of those templates shipped first. Pre-dates the import-failures feature; not fixed as
part of it since the badges still render (just not visually "muted"), and fixing it means picking
an actual muted color/treatment, which is a design call rather than a bug fix.

## RESOLVED 2026-07-31: gunicorn's gevent worker + `async_to_sync(channel_layer.group_send)` corrupted unrelated concurrent requests' `SynchronousOnlyOperation` check

Production intermittently 500'd on completely ordinary synchronous ORM calls - the reported
repro was `location.pins.count()` deep in SpotGuessr's difficulty-weighting code
(`services/spotguessr/selection.py::_proxy_difficulty_rating`), several frames from anything
async. Even the 500 handler's own fallback render died the same way (`SiteSettings.get_current()`
in `context_processors.py`), which was the tell that this had nothing to do with SpotGuessr.

**Root cause**: `package.json`'s `start` script runs `gunicorn ... -k gevent` (see also
`gunicorn.conf.py`, which patches psycopg2 for gevent cooperation via psycogreen). Gevent
cooperatively schedules every in-flight request as a "greenlet," but many greenlets share exactly
one real OS thread per worker process (`WEB_CONCURRENCY` workers, each hosting many greenlets).
Django's `SynchronousOnlyOperation` check reads `asyncio.get_running_loop()`, and asyncio's
"is a loop currently running" flag is tracked at the C level **per OS thread**, not per greenlet -
gevent's monkeypatching can virtualize `threading.local` and friends for greenlets, but it cannot
virtualize that.

The codebase calls `asgiref.sync.async_to_sync(channel_layer.group_send)(...)` throughout the
real-time layer (`services/messaging/direct_messages.py`, `services/messaging/group_chats.py`, `services/visits/safety.py`,
`models/notifications/signals.py`, and the game realtime modules
`services/{spotguessr,trivia,consensus}/realtime.py`). Whenever any of those is mid-flight -
specifically while `run_until_complete` is doing network I/O against the Valkey channel-layer
backend, a cooperative yield point for gevent - the worker's shared OS thread is flagged "inside a
running event loop." If gevent's hub switches to a *different*, completely unrelated greenlet
during that window and that greenlet touches the ORM (as virtually every view does), it incorrectly
trips `SynchronousOnlyOperation`. This is systemic: any concurrent chat message, notification, or
game-session broadcast could poison any other in-flight request on the same gevent worker for the
duration of the `group_send` call.

**Fix**: every `async_to_sync(channel_layer.group_send)` call site now goes through
`services.core.channel_broadcast.send_group_message(group, message)`, which enqueues
`tasks.broadcast_channel_group_message` on `celery-worker`'s prefork pool (a real, separate OS
process per slot - confirmed never gevent-patched, per `celery-worker-panels`'s `--pool=threads`
comment and the plain default `celery-worker` command in `docker-compose.yml`) instead of calling
`async_to_sync` inline in the request. That task performs the actual call and swallows/logs
delivery failures, matching every caller's prior "already durably saved, live delivery is a bonus"
contract. Regression coverage: `tests/hypothesis/test_channel_broadcast.py` (the new dispatch
boundary and task), `tests/hypothesis/test_notification_push.py` (updated to assert
`send_group_message` is called rather than mocking the old inline `get_channel_layer`/`group_send`
path).

Deliberately not done: switching the WSGI worker off gevent entirely, or reimplementing
`channels_redis`'s wire protocol with a plain sync Redis client to avoid asyncio altogether - both
were considered (see the session's discussion) but are larger architecture changes; routing through
Celery fixes the corruption at its only real source (asyncio-in-request) with a contained diff and
fits the codebase's existing "Celery for anything that shouldn't block the request thread"
convention. Broadcasts now pay a small broker round-trip instead of running inline - acceptable for
this app's near-real-time (not hard-real-time) chat/notification/game UX.

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

## 2026-07-28: Satellite/street-view imagery render path re-runs the full provider chain even when "ready"

`services/pins/external_data.py`'s `SlidesPanelSource` (base of `SatellitePanelSource`/street-view
equivalent) tracks readiness with a summary marker (`is_ready`, `ready_key`) set after a background
warm-up pass, separate from each provider's own 24h slide cache (`SLIDES_READY_TTL_SECONDS`,
line ~108). The class docstring (line ~875) confirms "the Celery warm-up task and the request-path
render share this exact function" - `collect()` runs the same per-provider gateway chain
(`collect_satellite_slides`/`collect_street_view_slides`) on the request thread regardless of
whether `is_ready` is true, rather than reading a fully-materialized result. In practice this is
usually cheap (each provider serves from its own warm cache), but there's no short-circuit: a
request landing in the gap where the summary marker has lapsed but not yet re-warmed pays the full
provider-chain latency inline on the request thread instead of getting a fast placeholder.

Fix would be to have `render`/`api_payload` read a materialized slide list when `is_ready` is true
instead of re-invoking `collect()`, reserving the shared function for the warm-up task alone. Not
investigated further - flagged while auditing `docs/notes/mobile_app_notes.md`'s claim (D8) that
this was already logged here, which it wasn't until now.

## RESOLVED 2026-07-28 (documented judgement call): restoring legacy `status='Muted'` friendships

Migration `0020_friendship_muted_flag` has to guess what a `status='Muted'` row was *before* it
was muted, because the old encoding overwrote the previous status and stored it nowhere. It
restores those rows to `Accepted` with `muted=True`. That is a judgement call and is recorded
here so a future audit does not have to re-derive it:

- The only user-reachable mute path is `FriendController.mute_friend` ->
  `services.social.friendship.mute_profile`, and the only template rendering that URL
  (`partials/profile/_profile_hero_body.html`) emits the Mute button **exclusively** inside its
  `friendship_status == 'accepted'` branch. So every mute a real user performed started from
  `Accepted`, which makes the restore faithful rather than a widening of access.
- `Friendship.mute()` (a classmethod that created a `Muted` row for two strangers) could have
  produced non-accepted rows, but `git log -S "Friendship.mute"` over
  `controllers/`, `services/` and `external_api/` returns nothing across the whole history - it
  was only ever called by its own unit test. It is deleted in this change.
- The external API's `FriendMuteView` *can* mute a non-accepted row, but it exists only on the
  unreleased `feature/external-api-mobile-v2` branch that introduces this migration, so no
  deployed database can hold a row it wrote.

If any of those three assumptions turns out to be false for a given deployment, the affected
rows are ones where two profiles are now treated as accepted friends when they previously were
not. Auditing that is a single query: `Friendship.objects.filter(muted=True, status='Accepted')`
with `created`/`updated` predating the deploy of `0020`.

## FIXED 2026-07-28: Google Calendar export leaked trip-mates' hidden coordinates

**Severity: privacy, cross-user, and irreversible once it fired** - the data went to a third
party (Google), where no later UrbanLens privacy change can reach it.

`services/trips/calendar_sync.py::_activity_location_string` honoured only `TripActivity.location_hidden`
and ignored the adder's `Profile.trip_pin_location_visibility` gate that every other trip surface
applies via `services/trips/trip_visibility.py::viewer_hidden_activity_ids` (the activities panel, the
trip map, AI trip suggestions). A trip is a shared space, so exporting one wrote **other members'**
coordinates - precisely the ones the trip screen deliberately withholds from the exporter - into
the exporter's Google Calendar, as the `location` field of both the all-day trip event
(`trip_to_event_body` -> `_trip_location_string`) and the per-activity timed events
(`activity_to_event_body`).

Repro (pre-fix): two profiles on one trip; the adder sets `trip_pin_location_visibility = no_one`
and adds an activity with a `Location`; the other member exports the trip
(`POST /dashboard/trips/<slug>/calendar/export/`, or the external API's
`POST /trips/<slug>/calendar/`, or any auto-sync push via `push_auto_synced_trip_changes`) ->
the event body carries the address. The trip screen shows that same member no coordinates at all.

Fixed by making `export_trip_to_calendar` compute the viewer's hidden-activity set once
(`_hidden_activity_ids_for`, which runs the shared `viewer_hidden_activity_ids` for
`account.profile`) and thread it through `trip_to_event_body`, `_trip_location_string`,
`_sync_activity_events` and `activity_to_event_body`. A hidden activity still gets its event -
the exporter is committed to be somewhere and a gap in their calendar would be its own bug -
just without a `location`. Regression coverage:
`tests/hypothesis/test_external_api_trip_calendar.py::ExportRespectsAdderVisibilityTests`.

Left open deliberately: the pure-mapping helpers still accept `hidden_activity_ids=None`
("no viewer gate"), which is correct for the property tests that call them with unsaved trips
but means a *new* caller that forgets to pass it reintroduces the leak. Worth revisiting as a
required argument once no caller needs the viewerless form.

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

## RESOLVED 2026-07-27: `test_websocket_auth.py::test_valid_api_key_authenticates_an_anonymous_socket` times out

Was `asyncio.CancelledError -> TimeoutError` (asgiref/timeout.py:108), and was **confirmed
pre-existing** by stashing the Place-consolidation change set (identical `1 failed, 6 passed`
both ways).

The asymmetry that made it look consumer-specific - the structurally identical OAuth2 sibling
passed - was the actual clue, just not in the direction first guessed. It was not a
`database_sync_to_async` deadlock. Nothing in the project overrode `PASSWORD_HASHERS`, so tests
ran Django's default PBKDF2 at ~1.2M iterations; `authenticate_api_key`'s `check_password` ran
*inside the WebSocket handshake* and blew past `WebsocketCommunicator.connect()`'s 1-second
default timeout. The OAuth2 path is a plain indexed token lookup with no hashing, so it never
came close.

Fixed by setting `PASSWORD_HASHERS = ["...MD5PasswordHasher"]` in `settings/test.py` (test-only;
base.py keeps the real hashers everywhere else). Speeds up every test that bakes a User as a
side benefit.

## RESOLVED 2026-07-27: `get_nearby_or_create(threshold_meters=0)` could 500 on sub-precision coordinate collisions

`Location.latitude`/`longitude` are `DecimalField(max_digits=9, decimal_places=6)`, so the
database rounds to 6dp on insert - but `Location.save()` builds the PostGIS `point` from the raw
unrounded float (`models/location/model.py:426-429`). Two coordinates that differ only below 6dp
therefore round to the *same* stored (latitude, longitude) while their stored points sit ~1cm
apart.

With `threshold_meters=0` (`models/location/queryset.py:117-161`) that combination is
unreachable-by-lookup but blocked-on-insert: the `point__distance_lte=(point, D(m=0))` probe
misses the existing row, the insert then trips the `(latitude, longitude)` unique constraint, and
the `IntegrityError` handler re-runs the *same* zero-distance probe, misses again, and re-raises -
surfacing as a 500.

Repro: call it twice with e.g. `42.00000014` then `42.00000006` (same longitude).

Fixed by `Location.objects.get_exact_or_create` (`models/location/queryset.py`), which matches on
the stored coordinates - what the unique constraint actually enforces - rather than a
zero-distance geometry probe. Every exact-coordinate caller now goes through it:
`pin_creation.resolve_child_pin_location`, `pin_edit.move_pin_to_coordinates`, and
`detail_pins._location_for_child_wiki`.

Two adjacent bugs surfaced while fixing it, both also fixed:

- `_location_for_child_wiki` handled "a wiki already owns this Location" by inserting a **second
  Location at the same coordinates**, which the `(latitude, longitude)` unique constraint refuses
  outright - a guaranteed 500 whenever a user dropped a child wiki marker on a point that already
  had one. It now raises `ChildWikiLocationError`, surfaced as a 400 ("place it slightly apart"),
  matching the child-pin rule. The child-wiki *move* path excludes the wiki being moved, so a
  stay-put drag is still a no-op.
- `move_pin_to_coordinates` let a root pin move onto a Location where the owner already had
  another root pin, which violates `db_pin_unique_location_per_profile` and surfaced as an
  unhandled `IntegrityError`. It now raises `PinMoveError` (400 on both the internal and external
  endpoints). Child pins are deliberately unaffected - sharing a parcel is their purpose.

## RESOLVED 2026-07-27: assigning `Location.cid` performed a synchronous Google lookup

`Location.cid`'s setter (`models/location/model.py`) called
`GooglePlaceService().set_cid_for_entity(self, value)` and took that method's
`fetch_if_missing=True` default, so `location.cid = 123` - an assignment that reads like setting a
field - issued a live Google call to resolve a place name for the coordinates. This is what made
`test_legacy_cid_coordinate_fix.py` hit the network (see cause 1 in the big entry above); the test
was fixed at the time, the setter was not.

The setter now passes `fetch_if_missing=False`, matching `place_name`'s documented cache-only
stance a few lines above it. Callers that genuinely want the lookup should call the service
directly, where the cost is visible.

Worth knowing: **every** production caller of `set_cid_for_entity` (`services/apis/locations/
google/maps.py:240,769`) already passed `fetch_if_missing=False` explicitly. The setter was the
only code path anywhere taking the blocking default, so that default currently has no users. It
is left as-is - flipping it is a wider API decision - but a future caller relying on it should
know it is a trap rather than a considered default.

## RESOLVED 2026-07-27: nine pre-existing friend-invite / pin-sync test failures on `feature/external-api-mobile-v2`

**Resolved.** The open question below - "is the gate right, or do the tests encode a real product
requirement?" - was settled in favour of the gate, on three pieces of evidence: the code comment
documents it as a deliberate fix, `request_friend` runs the same evaluator (so exempting the email
path would reintroduce exactly the asymmetry the fix closed), and the bypass it replaced is
recorded as a vulnerability further down this file. Knowing someone's email address is not a
secret worth overriding their stated preference for.

So the eight friend-invite tests were stale. They now use a `make_invitable_user` helper
(`test_friend_invite_privacy.py`) that opts the *target* into `ANYONE`, keeping each test on its
actual subject; `test_response_identical_regardless_of_target_friend_request_visibility` sets both
ends explicitly since it is the one test genuinely about the gate. **29 passed.**

The ninth, `PinSyncViewTests::test_child_pins_are_served_with_their_parent_uuid`, was fixed
independently by the child-pin location work (`resolve_child_pin_location` /
`get_exact_or_create`) - its `PinCreationError: You already have a pin at this location.` was that
exact bug. **10 passed.**

**RESOLVED 2026-07-28**: the "invite a friend by email is a no-op for two already-registered
users" consequence flagged above was decided in favour of option (b) - soften the default.
`friend_request_visibility`'s default is now `ANYONE` rather than `ANYTHING_IN_COMMON`
(`models/profile/model.py`, migration `0018_alter_friend_request_visibility_default.py`), on the
reasoning that having an account should never make a user *harder* to reach by friend request than
not having one - which is what the stricter default did, since `invite_by_email`'s
unregistered-address branch always sends the invitation unconditionally. The migration backfills
existing profiles still at the old default (not ones a user deliberately changed) - see the
migration's own comment for the reasoning, mirrored from the `welcome_onboarding_complete`
precedent in `0002`/`0003`. `test_anything_in_common.py::VisibilityDefaultsTests` updated to match
(every other `ANYTHING_IN_COMMON`-by-default field is unaffected - this was scoped to
`friend_request_visibility` only). 205 passed across every suite touching this setting.

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

## OPEN 2026-07-26: WhatsApp/SMS alerts never fire for safety check-in partner invites

`services/notifications/notification_text_alerts.py:114-115` derives the preference column name from the
notification's own type:

```python
prefix = notification.notification_type
return bool(getattr(prefs, f"{prefix}_whatsapp", False)), bool(getattr(prefs, f"{prefix}_sms", False))
```

That works for 11 of the 12 preference stems, but **not** for the safety-check-in partner
invite. `NotificationType.SAFETY_CHECKIN_PARTNER_INVITE` has the value
`"safety_ci_partner_invite"` (`models/notifications/meta/type.py:26`), while the
`NotificationPreference` columns are named `safety_checkin_partner_invite`,
`safety_checkin_partner_invite_whatsapp`, `safety_checkin_partner_invite_sms`
(`models/notifications/model.py:180-182`). The lookup therefore misses, and the
`getattr(..., False)` default silently reports "user does not want text alerts" - so a user
who explicitly enabled WhatsApp/SMS for partner invites never receives them, with no error
anywhere.

Note the same mismatch does *not* affect `wiki_safety_checkin`, whose type value and column
name do agree.

Fix is a rename on one side plus a migration (and a check for any other consumer deriving
field names from type values). Deliberately not done as a drive-by during the external-API
social/notifications build, since it changes either a stored enum value or three column names.

Guarded meanwhile by
`tests/hypothesis/test_external_api_notifications.py::NotificationPreferenceCoverageTests::test_one_preference_stem_does_not_match_its_notification_type`,
which asserts the mismatch explicitly so that fixing it fails loudly rather than silently
changing the external API's preference field names.

## OPEN 2026-07-26: FCM push transport is registered but never dispatched

`services/notifications/push.py` accepts and stores FCM device registrations, but only the UnifiedPush
transport actually dispatches; FCM rows are skipped at send time until a Play-flavor client
exists (see that module's docstring). This is server-side dispatch infrastructure requiring a
Google service-account credential - it is *not* a missing external-API endpoint, and
`push-devices/` already registers such devices correctly. Recorded here because the gap was
previously documented only in a module docstring, so a user registering an FCM device today
gets silence rather than an error.

## OPEN 2026-07-26: notification "friend accepted" loses its source_profile on one path

`services/social/friendship.py::accept_friend_request` (ported verbatim from the old
`FriendController.accept_friend`) creates the `FRIEND_ACCEPTED` notification **without**
`source_profile`, whereas `request_or_accept_friendship` and
`FriendController.friend_request_respond` both set it. The external API's
`NotificationSerializer` exposes `source_profile`, so a mobile client sees a null actor for
notifications produced by that one path and cannot link back to the profile. Left as-is
during the extraction to keep the refactor behaviour-preserving; setting
`source_profile=actor` there is almost certainly correct but should be done with a test that
pins the intended behaviour on all three paths.

**Status as of 2026-07-23 (cleanup)**: all fully-resolved entries have been removed from this
file - resolution details live in git history (this file's prior revisions) and
`docs/notes/ai/completed.md`. Recently closed, for orientation: the whole PR #111 cluster
(CodeQL triage, both SSRFs, E2EE password-policy endpoint, opaque rotation member IDs,
per-recipient WebSocket payloads, media-proxy URL signing), the WhatsApp/SMS delivery wiring
for every notification toggle, trip-comment `comment_visibility` gating, campus-aware
Wikipedia search (UL-354), the Overpass pool overhaul (UL-355 + self-hosted primary +
empty-result cross-validation), Internet Archive `texts` tiles, the child-pin terminology
sweep, the compose test "pod" for DB-backed tests, and `schedule_panel_fetch`'s broker-outage
handling.

**Closed in the post-cleanup round, same day**: **PinTombstone pruning** (daily
`prune_pin_tombstones` beat task, 400-day retention in
`services.pins.pin_sync.TOMBSTONE_RETENTION`; `pins/deleted/` now returns **410 Gone +
`full_resync_required`** when `deleted_since` predates the retention floor, so pruning can
never cause a silent miss - 3 new tests in `test_external_api.py`), and the **four export
importers** (see the struck entry below for the design decisions that shaped them - 24 new
round-trip tests in `test_export_import_completeness.py`). Everything below is genuinely
still open.

**Feature build, 2026-07-24** (from the ROADMAP.md feature analysis, five of the six
recommended items - see ROADMAP.md for full RESOLVED notes and commit hashes): public pins
by community vote (UL-58), trip-planning OSRM drive-time legs + optional/generated trip
names (UL-60 partial, UL-360), an AI chat assistant with an allowlisted tool loop (UL-293),
KML/GPX/GeoJSON/CSV quick exports + emailed full exports (UL-382, UL-373), and
recency-weighted boundary voting (Pin Restructure section). All five pod-tested green
(60 + 30 = 90 new tests) and browser-verified on dev.urbanlens.org. Offline maps (UL-287,
the sixth recommended item) was intentionally skipped this round. Explicitly **not** built:
UL-377's search/list-scoped targeted exports (blocked on lists, which don't exist yet),
UL-60's AI-driven schedule-timing suggestions and inline "AI suggests pins for this trip" UI
(the assistant can add a specific pin to a trip on request, which covers part of this in a
chat-driven form only), and UL-163's broader AI-sandboxing ticket (MCP security, local
models) - the assistant's allowlist-only tool loop is a first answer to the same concern but
doesn't close that ticket. The boundary-voting dialog auto-opens only while zero votes exist
(not, as the spec's prose could be read, until consensus forms) - a deliberate simplification
worth knowing about if the UX is revisited.

**RESOLVED 2026-07-25**: the `TripMembership.rsvp` choices drift noted above is now migrated -
`0029_alter_tripmembership_rsvp.py` carries the `AlterField` for the `"Going"`/`"Not Coming"`/
`"Maybe"` labels. This checkout briefly had two different, unrelated `0027_*` migrations as
sibling leaves off `0025` (the indoor_outdoor/rsvp work here, and `0027_safety_checkin_partners.py`
from a separate concurrent session); since nothing had been pushed anywhere migration state is
persisted, this was resolved by resequencing instead of a merge migration - the indoor_outdoor
migration was renumbered to `0028` and now depends on `0027_safety_checkin_partners`, with the
rsvp `AlterField` as `0029` after it. Single linear chain, no merge migration needed.

---

## ~~Verification debt~~ RESOLVED 2026-07-23 (pod ran; all session-added tests pass) → 17 PRE-EXISTING full-suite failures triaged below

**The debt itself is cleared**: the test pod ran for the first time (it works - two workflow
gotchas found and documented in CLAUDE.md: the runner bakes source at build time, and
rebuilding it orphans test-db/test-valkey's shared namespace). The 2026-07-23 rounds' own
test files were executed and now **all pass** - the run surfaced 14 findings (2 real code
bugs in that day's work: the photo-proxy signature was computed over the raw name while
Django delivers the percent-encoded path segment, and same-instance comment re-imports
duplicated once the uuid was taken; plus 6 stale/fragile pre-existing tests) - all fixed in
`06de47fd`/`35ac4100`.

**The FULL suite then ran end-to-end for the first time ever: 6,277 passed, 17 failed
(34m45s).** None of the 17 touch code changed on 2026-07-23; they are pre-existing test debt
that had simply never executed against a real DB. Triage (each verified from the run log,
`/tmp/pod-full.log` on chiron):

- **`test_site_admin_stats` (4) + `test_infrastructure_stats` (1)** - the stats collectors
  probe the real infra services and trip `LocalhostOnlyNetwork` on the dev stack's
  container-bridge IPs (`172.18.0.10`). These tests need the probes mocked (per the repo's
  own testing policy) - they can never pass inside the pod as written.
- **`test_avatar_colors::GroupMemberSearchAvatarColorTests`** - `0 != 4`: member search now
  filters through `can_view_profile`, and the test's baker profiles keep the default
  `profile_visibility` (ANYTHING_IN_COMMON) with nothing in common → 0 results. Stale since
  the member-search masking hardening; fix by setting candidates' visibility (mirror
  `_profile()` in test_identity_visibility.py).
- **`test_flickr_album_import::test_blank_url_shows_an_error`** - the pod has no Flickr
  keys, so the view short-circuits to "Flickr integration is not configured" before the
  blank-URL branch; the test must stub the settings keys.
- **`test_media_own_photos_preview` (2)** - endpoint returns 204 where the tests expect
  200-with-tiles; mechanism not yet dug into (likely fixture gap - files/coords - or a
  moved gate).
- **`test_pin_edit_controller::PinDescriptionEditableTests` (2)** - the rendered page no
  longer carries `data-raw-description=""` / carries `pin-description--empty` unexpectedly;
  description-editor markup drift.
- **`test_profile_hero_meta_editable` (2)** - "Add your area..." placeholders NOW render
  where the tests expect them hidden; either deliberate own-profile placeholder behavior
  change (update tests) or a regression in the hidden-when-empty rule (check intent first).
- **`test_settings_tos_accepted_display`** - "Mar 4, 2025" not found though the label
  renders; date-format drift.
- **`test_pin_media_endpoints::test_media_relevance_route_reaches_the_post_handler`** -
  `TypeError: Cannot mix str and non-str arguments` (an os.path/reverse join receiving a
  Mock/None); needs its traceback read.
- **`test_property_records_plugin`** - `test_the_locations_address_is_passed_through_as_the_situs_search_key`
  assigns `location.address`, which is now a read-only property (`AttributeError: no setter`).

**Suggested next step**: one focused session over these 9 files - none looks like a
production bug on its face (env coupling, fixture rot, template drift), but
`test_media_own_photos_preview`'s 204 and `test_profile_hero_meta_editable`'s
placeholder-visibility change deserve a real look at intent before the tests are edited to
match current behavior. The pod is left running on chiron for it.

---

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
`is_blocked_address` (`url_safety.py:20-22`) checks `is_private`/`is_loopback`/`is_link_local`/
`is_reserved`/`is_multicast` but not the `100.64.0.0/10` Carrier-Grade-NAT range — verified
`ipaddress.ip_address("100.64.0.5")` returns `False` for every one of those checks. Many cloud
providers route internal-only infra (AWS NAT gateways, GCP internal LBs) through this range, so a
user-supplied URL resolving there sails through `ensure_public_http_url` unblocked. This is the
*only* IP-range guard for AI link extraction (`services/ai/link_extraction.py`), pin-suggestion
photo download (`services/pins/pin_suggestions.py`), and media materialization
(`services/media/media_materialize.py`) — one missed CIDR range is a gap in three subsystems at once.

**Decompression-bomb protection in the full-archive importer only checks forgeable declared
sizes.** `services/import_export/import_data.py:275-289` sums each ZIP member's *declared* `file_size` against a
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
- `services/core/rate_limiter.py:279-491` — `check_rate_limit` (COUNT) and `log_api_call` (INSERT) are
  separate operations with no locking; concurrent requests can all pass the check before any log,
  breaching even Nominatim's hard 1 req/sec ToS limit under a handful of simultaneous pin-detail loads.
- `services/security/email_safety.py:89-111` (`email_rate_limit_error`) — same shape for outbound
  friend-invite/visit-invite emails (`controllers/friendship.py:649-720`,
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
`controllers/labels.py:467-478` (`_apply_kind_conversion`) handles converting to Status/Tag but has
no branch for `new_kind == KIND_CATEGORY`. Converting a global Tag to a Category via the standard
edit form leaves `label.profile=None`, but Category lookups use exact-match `.for_profile()` with
no global fallback — the label vanishes from every Organize > Categories listing, and
`_can_modify_label` returns `False` for any non-tag label with `profile=None`, making the row
**permanently un-editable and un-deletable through the UI** (recoverable only via direct DB access).

**Safety check-in escalation can re-email every emergency contact on any partial failure.**
`services/visits/safety.py:940-981` (`escalate_checkin`) loops all contacts unconditionally (no
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

## ~~UL-255: "Remember last map position"~~ (RESOLVED 2026-07-23 - browser-verified WORKING, recommend closing)

**RESOLVED 2026-07-23**: reproduced the exact suspect scenario in a real browser against dev
(Playwright, REMEMBER mode enabled, remembered position cleared first): two real mouse-drag
pans fired 2 debounced POSTs to `settings/map-position/` with the panned coordinates, and a
**fresh navigation to the bare map URL** (no `?lat/lng/zoom`) restored the view to the exact
remembered position - delta 0.00000°/0.00000°, zoom matched. The REMEMBER chain works
end-to-end on fresh navigation, and Jess confirmed the other scenario (same-tab reload where
URL params win) is intended behavior. Both possible readings of the original report are
therefore accounted for; recommend closing UL-255. If it recurs, capture the exact
navigation path - the repro script is `ul255.js` in this session's scratchpad pattern
(login → pan → fresh goto → compare `map.getCenter()`).

---

## ~~Saved-filter include/exclude label picker: no drag-reorder or formula mode~~ (RESOLVED 2026-07-23, browser-verified on dev)

**RESOLVED 2026-07-23** - the authorized extraction is done and verified live:

- **`frontend/ts/shared/label-picker.ts`** (installed globally as
  `window.UrbanLensLabelPicker` by core.js) now owns both picker shapes:
  `createFilterPicker` (the map sidebar's full engine - include/exclude columns, chip
  dragging, AND/OR combinator, formula bar with tokenizer/parser/suggestions,
  `label_groups` serialization) and `createChipPicker` (the flat search+chips component the
  bulk-edit dialog and saved-filter scripts each used to duplicate). One deliberate
  improvement over the inline original: label names are HTML-escaped in generated
  chip/suggestion markup (the old code interpolated them raw - a UL-362-class XSS vector).
- **Main map**: the ~650-line inline engine is gone; the page instantiates the module
  against the existing fp-* DOM (inline on* handlers removed - the module wires delegated
  listeners, which also covers labels appended later by the create-label dialog).
  `applySavedFilter` merges via `mergeIncludeIds`, reset via `clear()`.
- **Bulk-edit dialog**: `_makeLabelChipPicker` is a thin id-based wrapper over
  `createChipPicker`. The rich include/exclude pairing deliberately does NOT apply there -
  add-labels and remove-labels are separate actions with separate candidate pools.
- **Saved-filter dialog + detail page**: the two flat pickers became ONE rich picker
  (`_saved_filter_label_picker.html`, sf-* ids, reusing the global fp-* styles). It
  serializes structured `label_groups` into the form (the create/edit endpoints already
  parsed that field) AND mirrors flat `tags`/`exclude_tags` hidden checkboxes; it seeds
  from stored groups (falling back to flat sets), so formulas round-trip and the "advanced
  rules will be replaced" warning was removed as no longer true.

**Browser-verified on dev.urbanlens.org** (Playwright in the official image on the chiron
VM, driving a real login): 22/22 checks - click-include, right-click-exclude, AND/OR
toggle, chip drag include→exclude, chip-click removal, formula `(Visited / Rooftop) -
Demolished` parsing to `[{or,[..]},{not,[..]}]`, filter POSTs firing, and on the
saved-filter page: seeding from flat criteria, hidden-input sync, formula entry, save, and
byte-identical `label_groups` round-trip after reload (map preview showed exactly the 2
matching pins). Screenshots reviewed. Remaining follow-up: the two updated template tests
(`test_saved_filter_detail.py`, `test_region_filter.py`) run in the compose test pod with
the rest of the verification-debt list.

---

## ~~Data export: comments/photos/trips/direct_messages have no importer~~ (RESOLVED 2026-07-23)

**RESOLVED 2026-07-23** - all four built (`_import_comments`/`_import_photos`/`_import_trips`/
`_import_direct_messages` in `services/import_export/import_data.py`, wired into `_IMPORT_ORDER` between
visit_history and connections). Export shape fixed first: `_resolve_target` now emits a
`target_uuid` (pin or wiki uuid; names are matched never), photos metadata gained
`media_type`, trips gained `is_creator` + `member_uuids`, and DM rows gained `partner_uuid`
(withheld whenever the partner's identity is masked from the exporter). Design decisions,
recorded because they're deliberately narrower than "import everything":

- **Comments**: uuid-idempotent; pin targets must resolve to the importer's OWN pin (via
  `pin_uuid_map` or direct lookup) and wiki targets must pass `location_visible_to` - a
  user-supplied archive can neither attach content to someone else's pin nor to a wiki its
  owner can't see. Unresolvable targets skip with a warning (an orphan comment renders
  nowhere). Exported `created` timestamps are preserved via post-create `update()`.
- **Photos**: files re-enter storage through the same `file_size_error_for_upload` /
  `quota_error_for_upload` checks as a fresh upload (archive contents were already
  malware-scanned at extraction); metadata filenames are `basename()`-neutralized against
  traversal; unresolvable targets still import as unattached uploads (the file is the user's
  own data regardless); labels reattach via `label_uuid_map`.
- **Trips**: requests-not-facts, mirroring `_import_connections` - only trips the user
  *created* are rebuilt (`is_creator`), an existing uuid is never claimed, and exported
  members are re-invited only when they're the importer's current connections, as
  `STATUS_INVITED` (with the standard added-to-trip notification), capped by
  `max_trip_members` / the upcoming-trips limit.
- **Direct messages**: only the user's own SENT PLAINTEXT rows are restored - received rows
  would let a crafted archive fabricate messages "from" a real user, and encrypted rows are
  sealed to the exporting account's key material the server can't re-wrap (the ciphertext
  stays readable in the archive itself; decision adjusted from "import ciphertext rows"
  during implementation for exactly that reason). Restores require the partner to exist,
  `can_direct_message` to still permit, and no mute either way; rows are inserted directly
  (never through `create_direct_message`) so restoring history pushes no live events, bell
  notifications, or text alerts at the partner; exported read state and timestamps are
  preserved so nothing lands as new/unread.

24 round-trip tests in `test_export_import_completeness.py` (DB-backed - see the
verification-debt entry above).

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

## ~~Hardcoded (non-theme-aware) `#2563eb`/`#4f46e5` blue in `_explainer.scss`, `_map.scss`, `_e2ee.scss`~~ (RESOLVED 2026-07-23 - browser-verified acceptable in both themes, no change needed)

**RESOLVED 2026-07-23**: the browser verification the entry was waiting for happened - the
components were rendered in BOTH themes (real login on dev; the explainer/toggle/E2EE-button
composite via an injected exact-markup probe, plus the map onboarding card observed live
during the label-picker verification) and none is a legibility bug:

- **Explainer** (`.ul-page-explainer` + the (?) toggle): legible in light and dark; the blue
  "TIP" kicker on the dark glass panel is the lowest-contrast piece but reads clearly (bold,
  uppercase, short) - deliberate branding, not breakage.
- **Map onboarding card** (`_map.scss` gradient icon + `FAST START` eyebrow): verified live
  in dark mode during the picker work - legible.
- **E2EE** (`#4f46e5`): a solid indigo button with white text (theme-independent by
  construction) and a title-icon accent - fine in both themes.

Per Jess's decision these were left untouched; converting them to `--ul-primary-color`
tokens remains optional polish, not a defect. Screenshot evidence: `blues-probe-dark.png` /
`blues-probe-light.png` from this session's verification run.

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
`docs/overpass-mirror-test.md`). Until the proxy timeout is raised above the intended
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

## SpotGuessr: down-voted photos permanently shrink a small pin pool's playable rounds, with no expiry (found 2026-07-25)

Reported symptom: after playing one full solo session against a ~10-pin pool, starting a new
session sometimes shows the empty state ("Nothing to play yet") even though the profile clearly
has pins and no restrictive settings are active.

Root cause: `services.spotguessr.photos.candidate_image_for_location()`'s default
(`allow_arbitrary_external_photos=False`) excludes any externally-sourced candidate photo whose
`services.media.media_relevance.effective_relevance()` score is negative. That score is fed by
`GamePhotoFeedback` rows (`services.spotguessr.relevance`) - thumbs-down/report reactions from
*any* past session, against *any* profile - and those rows never expire or get reset. For a
small pin pool where most or all locations have exactly one candidate photo, a handful of
thumbs-down votes accumulated during ordinary play can permanently knock that pool's only
playable photos below the eligibility threshold in *every future session*, with no way for the
player to know that's what happened (the empty-state copy just says nothing matched their
settings).

This is a real, separate bug from the "nothing to play yet" UX/response-shape issues fixed
alongside this note (see `docs/designs/spotguessr.md` and `controllers.spotguessr
.SpotGuessrStartView`) - it's a photo-inventory/relevance-decay *policy* question (should
`GamePhotoFeedback`'s influence decay over time? should a location with zero remaining eligible
photos fall back to `allow_arbitrary_external_photos`-style leniency automatically rather than
requiring the player to discover and toggle it? should thumbs-down carry less weight than it
currently does for small pools specifically?) that's bigger than a UX pass should decide
unprompted. Not investigated further here; worth a dedicated look before it's reported again as
"the game stopped working."

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

## Safety check-in partners: two residual gaps found during a fresh-eyes feature review (2026-07-25)

A full review of the partner/live-location/post-resolution-encryption feature (two independent
review agents, backend-correctness and frontend-security) found and fixed nine issues directly
in `services/visits/safety.py`/`consumers.py`/`tasks.py`/`models/safety/model.py` (archival payload not
capturing/severing `destination_location`/`trip`/`markup_map`/`markup_maps`, `archive_checkin`
non-atomicity, chat messages postable after archival, three TOCTOU races, a missing index, an
N+1, and no live-connection revocation on partner removal - all covered by new tests in
`test_safety_archival.py`/`test_safety_partners.py`/`test_safety_live_location.py`/`test_safety.py`).
Two narrower items were identified but deliberately left open:

- **Blocking a partner doesn't revoke their existing access.** `Profile.are_blocked` creation has
  no signal wired to `SafetyCheckinPartner` cleanup - blocking someone who is already an accepted
  partner on one of your check-ins leaves that `SafetyCheckinPartner` row (and any open WebSocket
  connection) intact. `remove_checkin_partner` now correctly force-closes a live connection and
  is the right mechanism to call, but nothing currently calls it from the blocking flow. Fix would
  be a signal/hook on block-creation that calls `remove_checkin_partner` for every
  `SafetyCheckinPartner` row between the two profiles (either direction).
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

`bonus_points_for_guess` -> `_reverse_geocode_admin_cached` (`services/spotguessr/geo_bonus.py:79`)
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

## 2026-07-28: `_StoredRangeValidationMixin._resolve_range` fails mypy (`serializers.py:2741`) - RESOLVED

**Resolved 2026-07-28** by the session that owned the in-progress work (the PR #124 Codex-review
pass). Diagnosis below was correct, including that a `cast` was the wrong answer. The fix was to
make the mixin a real `serializers.Serializer` subclass rather than a bare mixin over `object`:
its only correct use *is* as part of a serializer (it reads `self.context` and chains through
`super().validate()`), so the base list is the honest place to say so, and it types `context` and
`validate` together. It declares no fields, so `_declared_fields` is unaffected.

A `TYPE_CHECKING`-conditional base (`_Base = Serializer if TYPE_CHECKING else object`) was tried
first and rejected by mypy - `Variable ... is not valid as a type [valid-type]` - in both the
conditional-expression and statement-level `if`/`else` forms. Worth knowing before reaching for
that idiom here again.

Original report follows.


Noted while running `mypy` on `external_api/serializers.py` as a regression check after the
memories-journal/safety-maps pagination-envelope fix and the OAuth consent screen (unrelated
changes - see Part 7 of `docs/notes/mobile_app_notes.md`). `git diff` confirms neither of those
touched `_StoredRangeValidationMixin`, `TripUpdateSerializer`, or `TripActivityUpdateSerializer` -
this is uncommitted, in-progress work on trip/activity range validation, presumably from a
concurrent session on this same checkout (per `CLAUDE.local.md`'s note that multiple agents may be
working simultaneously).

`_resolve_range` reads `self.context.get("instance")`, but the mixin is a plain class (not a
`serializers.Serializer` subclass) - mypy has no way to know `self` will actually be a `Serializer`
at the point it's mixed in via `class TripUpdateSerializer(_StoredRangeValidationMixin,
serializers.Serializer)`. The fix is a type hint at the mixin boundary (e.g. a `Protocol` with a
`context: dict` attribute, or having the mixin only ever appear via a small typed base), not a
`cast`. Left alone rather than fixed here, since it belongs to a feature this pass didn't touch and
guessing at its intended shape risks colliding with whoever is actively editing it.

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

## 2026-07-30: SearXNG (`search.jmann.me`) image search 403s after coming back up from an outage

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

## Pre-existing test failures found while fixing CRIS media (2026-08-05) - FIXED

15 tests in `dashboard/tests/hypothesis/` failed on a clean `9a8c0f14` checkout, unrelated to that
work (confirmed by running them from a detached worktree at HEAD). All three causes are now fixed:

- **`test_delete_low_engagement_wikis.py` (11 tests)** - every one died on
  `CommandError: Unknown command: 'delete_low_engagement_wikis'`. The management command simply did
  not exist; the tests specified it completely (two independent criteria - at most 2 distinct pin
  owners, or no surviving user edit - dry run by default, `--yes` to delete, cascade to child
  wikis) and it has now been written to match. Its docstring references a
  `services.visits.safety.destination_wiki_activity` precedent that also does not exist anywhere in
  the tree, so the "active user edit" rule was instead mirrored from `WikiEditQuerySet.active` and
  `services.achievements.metrics`, which agree on it.
- **`test_sun_times.py` (3 tests)** - written before the weather chain gained its REData-first
  chokepoint (`services.apis.locations.weather_resolution`). They patched only the direct
  Open-Meteo/OpenWeatherMap gateways, so on any machine with REData credentials configured the view
  made a real outbound call and tripped `core/testing_network.py`'s guard. They now switch REData
  off explicitly, pinning the direct-provider branch they were always about; REData's own branch
  stays covered in `test_weather_resolution.py`.
- **`test_panel_api_interface.py::ParcelBuildingsApiPayloadTests`** - the fixture placed the parcel's
  "second building" 200 km away. `building_rows` correctly drops a building outside the property's
  real boundary, and a boundary gets *derived* as soon as the pin has a child - so the moment a test
  added a covering child pin, the far-away building vanished from the payload and `unpinned_count`
  read 0. Product code was right; the fixture now puts both buildings on the parcel.


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
