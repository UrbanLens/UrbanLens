# Codebase assessment, 2026-09-23

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: N28` · `status: current` · `updated: 2026-09-23`

A read of the tree on 2026-09-23, looking for defects and structural problems
that are not already an open `P#`. Nothing here is a proposed fix. Line numbers
are from this checkout on that date. No test suite was run, no request was
timed, and no browser session was opened.

The July full-tree audit is [`docs/audits/codebase-audit.md`](../audits/codebase-audit.md)
(`X7`). This pass did not re-walk its 35 units. It read controllers, the
external API, websocket consumers, services, models, Celery tasks, settings,
and the map frontend, then re-opened the claims below in the files themselves.
A parallel read also produced a longer list; several of those claims were
wrong once the surrounding code was read, so only re-read claims are in
"Verified". The rest are in "Not re-opened" so the next reader does not treat
them as established.

## Already tracked — do not refile

These were still true on a spot check, and they already have a live record.
Restating them as new problems would hide that.

| Record | What a spot check still saw |
|---|---|
| `P34` | `messages/index.html` still opens an inline `<script>` at line 145. `map-page.ts` still defines three local HTML escapers (`1637`, `5366`, `6413`) beside `frontend/ts/shared/escape-html.ts`. The copy at `6413` only replaces `&` and `<`. |
| `P69` | Unbounded `{% for %}` lists were surveyed and mostly closed. Friends, safety chat, and the pin-share photo loop were not re-measured against the two items that entry leaves open. |
| `P95` | Import preview still reads a whole entry into memory. Not re-measured. |
| `P20` | `GoogleMapsGateway.import_pins_streaming` is still named as dead weight. Not re-measured. |
| `P113` | Many request-path upstream calls were bounded on purpose. The synchronous calls in "Not re-opened" need a diff against that entry before anyone treats them as new. |
| `D12` | Viewport mode for the map pin catalog is deferred. `map-page.ts` still loads the whole catalog (`_refreshAllPins` around line 991, localStorage write around line 819). |
| `P85` | `DashboardQuerySet` is generic (`models/abstract/queryset.py:24`). `DashboardManager` is still `Manager.from_queryset(...)` (`queryset.py:144`). Whether the "146 mypy errors turned off" half of `P85` is still true was not re-measured. |
| `P29`, `P37`, `PL6` | Write-route and test-quality gaps are already open. This pass did not produce a new coverage number. |

`X28` already measured the 1,000-concurrent-user ceiling as about 4 app cores,
with p95 jumping from 231 ms at 500 users to 6.2 s at 1,000 and nothing
failing. This pass did not re-benchmark that.

## Verified

### A departed Trivia player can still open the session and advance it

`TriviaSessionParticipantStatus.LEFT` is a real status
(`models/trivia/model.py:257-265`), set by `leave_session` / `kick_participant`.
`_participant_session` treats any participant row as authorized
(`controllers/trivia.py:39-48`). `TriviaRoundView.get` then calls
`get_or_create_round` (`controllers/trivia.py:410-414`). That function creates
the next round once the previous one is revealed
(`services/trivia/session.py:310-357`). A GET from someone who already left
can therefore create a round for the people still playing.

The same lookup feeds chat history (`controllers/trivia.py:397-401`) and the
answer view (`controllers/trivia.py:430-432`). Answer submission itself
rejects a non-joined player via `NotJoinedParticipantError`
(`controllers/trivia.py:441-443`). The round GET and the chat GET do not.

The websocket check is the same shape. `_is_participant` is documented as
"any status" and is an `.exists()` with no status filter
(`consumers.py:1424-1435` for SpotGuessr, `1468-1479` for Trivia, `1511-1522`
for Consensus). `receive` re-checks API-key scope and then sends chat
(`consumers.py:1340-1348`). `participant_left` closes the socket that is
already open (`consumers.py:1366-1372`) and the comment there says
participation is not re-checked on later messages. A later connect still
passes `_is_participant`, because a `LEFT` row still exists.

SpotGuessr has no `LEFT` status (`models/spotguessr/model.py:73-82` is only
`INVITED` / `JOINED`). It has the same hole for an invitee:
`SpotGuessrRoundView.get` uses `_participant_session` ("any status",
`controllers/spotguessr.py:52-61`) and then `get_or_create_round`
(`controllers/spotguessr.py:510-514`). `get_or_create_round` creates the next
round when the last one is already revealed (`services/spotguessr/session.py:402-430`).
An invitee who has not joined can trigger that.

`tests/hypothesis/test_trivia_stall.py` asserts that leave sets `LEFT`. No
test in that file asserts that a `LEFT` caller is rejected by the round, chat,
or websocket paths. Not searched beyond that file.

### Escalation can comment on a wiki the check-in owner cannot see

`find_visible_community_wiki` is the access-checked lookup
(`services/visits/safety.py:1333-1350`). The checkbox UI uses it
(`controllers/safety.py:1208`). Creating a check-in stores
`notify_community_wiki` from the raw POST flag
(`controllers/safety.py:587`, `services/visits/safety.py:1442-1492`) and does
not call the visible lookup. `apply_checkin_edit` will also change the
destination and the flag with no visibility check
(`services/visits/safety.py:1202-1236`).

`escalate_checkin` posts when the flag is set (`safety.py:1635-1636`).
`post_checkin_to_community_wiki` looks the wiki up with `find_community_wiki`,
which is a bounding-box match and nothing else (`safety.py:1315-1330`,
`1377-1394`), then `Comment.objects.create` on that wiki and notifies pin
owners at the location (`safety.py:1390-1399`).

Wiki access is supposed to be earned (`models/wiki/CLAUDE.md`). The visible
helper exists specifically so a coordinate with a hidden wiki answers the same
as a coordinate with no wiki. The write path does not use it.

### Anonymous geocoding spends the site's upstream quota

`geocode_address` has no login requirement (`controllers/settings.py:382`,
routed at `urls.py:1194`). The `external_apis_enabled` refusal runs only when
`request.user.is_authenticated` (`settings.py:405-408`). An anonymous caller
falls through to `GoogleGeocodingGateway().geocode_place_name`
(`settings.py:411-415`) and then `nominatim_geocode` (`settings.py:432-436`).
Whether the gateway's own rate limiter keys that traffic per IP or against a
site-wide budget was not re-read.

### The web pin merge is not one transaction; the external API copy is

`PinBulkMergeView.post` promotes the target, then reparents each source in a
loop, with no `transaction.atomic()` (`controllers/pin_bulk.py:162-184`).
`external_api/views_pin_bulk.py` wraps the same steps in `transaction.atomic()`
(`views_pin_bulk.py:131-146`). The module docstring says there is no shared
function, so the API re-implements the view (`views_pin_bulk.py:1-9`). The
two copies have already drifted on atomicity. A failure mid-loop on the web
path leaves some sources reparented and others not. The availability note that
capped this view at `_MAX_BULK_PINS` does not mention the transaction.

### SpotGuessr state bonuses compare two different spellings of the same place

`bonus_points_for_guess` awards the state tier when
`_normalize(admin["state"]) == _normalize(location.state)`
(`services/spotguessr/geo_bonus.py:108-128`). `_normalize` is strip and
casefold only. `Location.state` is `administrative_area_level_1`
(`models/location/model.py:135-136`). The Nominatim helper that fills `admin`
reads only `address["state"]` (`services/apis/locations/nominatim.py:156-161`),
while the city key has fallbacks (`city` / `town` / `village` /
`municipality` / `hamlet`). How often stored states are abbreviations
(`NY`) and Nominatim states are full names (`New York`) was not measured.
Country and city use the same bare string compare.

### Two outbound fetches skip the SSRF helper that exists for this

`services/profile/avatar.py:224-246` (`Avatar.download`) accepts any `http` or
`https` URL and calls `requests.get` directly. It does not use
`fetch_public_url`. Callers were not all enumerated; the social-auth pipeline
is one (`services/social_auth/pipeline.py` was named by the first pass and
not re-opened here).

UnifiedPush endpoints are checked with `getaddrinfo` plus `is_blocked_address`
at registration (`services/notifications/push.py:75-83`, `107-108`). Dispatch
later does `requests.post(device.address, ...)` with no second resolution and
no DNS pin (`push.py:155-166`). A name that resolves to a public address at
registration and a blocked address at dispatch is not rejected.

`fetch_public_url` installs a process-wide `socket.getaddrinfo` wrapper at
import (`services/security/url_safety.py:114-118`). The comment there says a
later reassignment (gevent monkey-patch is the example) silently stops the pin
from applying. Nothing in that module re-installs the wrapper after import.
Whether the running workers actually patch `getaddrinfo` after this import
was not checked. `D11` moved the web tier to `gthread`; this is about other
processes that still monkey-patch.

### Wiki creation is check-then-insert on a one-to-one

`Wiki.location` is a `OneToOneField` (`models/wiki/model.py:87-91`).
`get_or_create_for_location` returns an existing row or `create`s
(`models/wiki/queryset.py:124-137`) and does not catch `IntegrityError`.
`ensure_wiki_for_location` calls it with no integrity handler
(`tasks.py:75`). Pin creation enqueues that task from a signal
(`models/pin/signals.py` around the `ensure_wiki_for_location` enqueue).
Two workers creating a wiki for the same location can both pass the read;
one insert then hits the one-to-one. `tasks.py` has no `IntegrityError`
handler anywhere in the file.

### A bulk suggestion action reports success when some items threw

`PinSuggestionBulkActionView` catches `Exception` per row, logs, and continues
(`controllers/pin_suggestions.py:284-293`). The response is always
`{"ok": true, "processed": ..., "requested": ...}`. A client that only reads
`ok` cannot tell a total failure from a partial one. `processed` is present,
so this is a contract footgun, not a silent drop. The accept-all view was
not re-read past its docstring.

### Map pin polling is never cleared

`map-page.ts` starts `setInterval(_pollForUpdates, _POLL_INTERVAL)` at line
2213. `_POLL_INTERVAL` is two minutes (line 184). There is no `clearInterval`
anywhere in that file. The map element sets `hx-boost="true"`
(`templates/dashboard/pages/map/index.html:19`). Whether a boosted navigation
keeps this module, and therefore the interval, was not checked in a browser.

### Work that grows with the site, not with the viewer

`detect_subdivision` walks every other location in the previous place's domain
and may call `BoundaryProviderChain().get_boundaries` once per row
(`services/places/provisioning.py:213-218`). Cost follows how many locations
share that domain.

`warm_all_for_profile` computes every saved filter's matching pin UUID list
at login (`services/search/saved_filter_cache.py:93-104`). Each filter's
match set is the viewer's pins, so this is per-account, but it is all of the
account's filters on the login path rather than the one filter the user opens.

Concealed-wiki search cannot be a SQL predicate; the code says so and
over-fetches by `_CONCEALMENT_OVERFETCH = 4`
(`services/global_search/providers.py:38-42`). Each surviving candidate then
loads `wiki.aliases` and `place.external_tags` in Python (`providers.py:45-56`).
That second part is one query pair per concealed candidate, on top of the
over-fetch. `P132` already cut global search SQL; this per-row reload was
not part of that measurement.

`resolve_deferred_pin_locations` is `@shared_task(..., max_retries=None)`
(`tasks.py:2719`), and the retry inside that task passes `max_retries=None`
again (`tasks.py:2902-2906`). A permanent upstream failure can requeue that
bulk task without a retry cap.

`upload_failed_at` is a plain `DateTimeField` with no `db_index`
(`models/images/model.py:306`). The stalled-upload sweep filters
`pending_scan=True`, `created__lt`, and `upload_failed_at__isnull=True`,
then slices to a batch (`tasks.py:1693-1694`). The matching partial index is
only `created` where `pending_scan`
(`models/images/model.py:530`, `idxdb_image_pending_created`). The null check
on `upload_failed_at` is not in that index. The slice bounds the rows
returned; it does not change what the filter has to examine. Not measured
against a real pending-scan table.

### Request-path work hung off Pin and Location saves

These run on the save that the user is waiting for, or on the commit of that
save. They were not timed.

- `remember_child_boundary_parent` issues an extra `Pin` read on a full save,
  or whenever `update_fields` touches the hierarchy (`models/pin/signals.py:18-28`).
  `refit_child_boundaries_on_save` then calls `refit_child_pin_boundary` in
  the signal itself, not via `on_commit` (`models/pin/signals.py:31-40`).
- Smart-list membership resyncs on pin save, on commit
  (`models/pin_list/signals.py:13-28`). `sync_pin_against_smart_lists` runs
  one query per smart list and, past `MAX_SMART_LISTS_PER_SYNC`, hands the
  rest to the bulk queue (`services/pins/pin_list_membership.py:37-44`).
  Below that ceiling the work stays on the request.
- The first time a location's Wikipedia cache gains a title, the `on_commit`
  hook walks `location.pins` and seeds an article per pin
  (`models/cache/signals.py:56-73`). Later title changes do not take that loop.
- `create_default_tags` does one `get_or_create` per default label inside
  profile creation (`models/labels/signals.py:105-124`). The exact-name lookup
  is the pattern `models/CLAUDE.md` warns about, but on a brand-new profile
  there is no existing case-variant row, so this is cost, not the uniqueness
  bug, unless something else inserts labels first.

### Hierarchy deletes take the subtree with them

`Pin.parent_pin` is `on_delete=CASCADE` (`models/pin/model.py:194-197`).
`Wiki.parent_wiki` is the same, and its comment says it mirrors the pin
(`models/wiki/model.py:103-112`). Deleting a parent removes every nested pin
or child wiki. Whether the delete UI reparents or warns first was not read.
`FloorplanMarker`'s `post_delete` also deletes `linked_pin`
(`models/floorplans/signals.py:11-19`); the signal docstring says that is
intentional twin cleanup, not an accident.

### Copied subsystems, with the game bug as the evidence

SpotGuessr, Trivia, and Consensus each own a `_participant_session` /
`_is_participant` pair whose docstrings say they mirror each other
(`controllers/trivia.py:39-44`, `controllers/spotguessr.py:52-56`,
`controllers/consensus.py` was not re-opened past the consumer). The `LEFT`
and invitee holes above are the same check copied into each game. Trivia's
ratings module says it mirrors SpotGuessr and imports SpotGuessr's `glicko2`
(`services/trivia/ratings.py:1-10`). Trivia question selection imports
`DEFAULT_RATING` from `models.spotguessr.model`
(`services/trivia/selection.py:9`). A change to one game's session rules does
not land in the others unless someone ports it.

The pin bulk API says the same thing about itself: the logic lives in the
view, so the API copied it (`external_api/views_pin_bulk.py:1-9`), and the
transaction already differs.

`services/import_export/import_data.py` is 2,131 non-blank lines.
`services/visits/safety.py` is 1,584 non-blank lines and owns encryption,
email, wiki comments, and chat. `tasks.py` is 3,928 non-blank lines.
`controllers/pin.py` is 1,723 non-blank lines. `map-page.ts` is 6,112
non-blank lines (content past line 6450) and mixes pin loading, dialogs, bulk
edit, and onboarding. `floorplan-editor.ts` is 3,951 non-blank lines;
`map-annotations.ts` is 2,927. Non-blank counts are
`(Get-Content | Measure-Object -Line).Lines` on 2026-09-23. These sizes are
the reason the copied bugs survive: the next edit has too many places to land.

`dashboard/CLAUDE.md` says slow upstream work belongs on Celery. The verified
geocode view is one place that still does it inline. Other inline upstream
calls are listed below and were not re-opened.

## Verified — batch 2 (auth, sharing, undo)

Read on the same date, after the first batch. Same rule: only claims re-opened in the file.

### Legacy accounts are distinguishable on the anonymous login-params endpoint

`login_params_for_identifier` returns `mode: legacy` and an empty `auth_salt` when the account exists and has no KDF row (`services/security/e2ee.py:136-142`). A missing identifier returns `mode: derived` and a decoy salt (`e2ee.py:142`). `E2EELoginParamsView` is anonymous and sets `throttle_classes` to an empty list (`controllers/e2ee.py:112-119`). The decoy makes a missing identifier look like a derived account. It does not make a legacy account look like either of those. Anyone can ask.

### A failed login tells you an unverified account exists, and includes its email

`AuthenticationForm`'s invalid path resolves the identifier and, when that user exists, is inactive, and has an `email_verification` row, replaces the error with a "hasn't been verified" message whose resend link contains `user.email` (`controllers/account.py:884-891`). A wrong password is enough. Typing a username therefore confirms the account and discloses the address on file. A missing identifier does not take this branch.

Signup does the same kind of oracle on purpose: `clean_email` says the email is taken (`account.py:362-368`) and `clean_username` says the username is taken (`account.py:371-377`). Those strings are the product's validation copy. They are still an unauthenticated existence check.

### Two anonymous rate limits are check-then-set

`suggest_passphrases` and `validate_password_policy` read a cache counter, compare it, then `cache.set` the incremented value (`controllers/account.py:1303-1307` and `1338-1342`). Neither uses an atomic increment. Concurrent requests can all observe the same count and all pass. `validate_password_policy` is an unauthenticated POST that runs the password validators, including the HIBP check the docstring names (`account.py:1322-1326`). The 30-per-10-minutes cap is what is supposed to bound that.

### Rejecting a pin share does not lock the row that accept locks

`apply_pin_share_response` wraps accept in `transaction.atomic()` and `select_for_update`, and refuses to accept unless the locked status is still `PENDING` (`services/sharing/pin_sharing.py:296-314`). Reject writes `REJECTED` with no lock and no status re-read (`pin_sharing.py:316-319`). An accept and a reject in flight together can materialize the recipient's pin and then mark the share rejected, or the reverse. Not exercised under a concurrent test in this pass.

### Pin ownership is not enforced inside `create_pin_share`

The docstring says the sender must own the pin (`services/sharing/pin_sharing.py:45-49`). The function checks self-share and friendship, then creates the `PinShare` (`pin_sharing.py:60-76`). It does not compare `pin.profile` to `sender`. Current web and external-API callers do filter `profile=profile` before calling (`controllers/direct_message_shares.py:71`, `controllers/group_chats.py:616`, `external_api/views_messaging.py:980`). The privacy rule for this package says the share path has to enforce that by construction (`services/sharing/CLAUDE.md`). A later caller that passes a pin it merely fetched will share someone else's pin, and `record_share_exposure` will run.

### Wiki undo reapplies the edit with no access check

`WikiMutationUndoHandler` loads the wiki by the id stored in the payload and moves it, renames it, or rewrites aliases (`services/undo/handlers/wiki_mutation.py:42-63`). It does not receive the profile and does not call the wiki access check. The stack itself is per profile (`services/undo/service.py:247-254`), so this is not "any user undoes any wiki". It is "the profile that stashed the edit can still apply it after they would no longer be allowed to make that edit". `D19` made some access permanent; this handler does not consult whatever access is left. Pin mutation undo is the same shape: `_pin` loads by primary key only (`services/undo/handlers/pin_mutation.py:24-28`, `57-59`). Sibling delete-undo handlers store `profile_id` and refuse when that profile is gone. The mutation handlers do not. Pins are not reassigned in the current share model, so the live case is the wiki.

### Every username check reads the whole user table

`username_is_taken` loads every username and compares confusable-normalized keys in Python (`services/auth/username.py:61-64`). Signup calls it from `clean_username` (`controllers/account.py:376`). Cost follows the number of accounts, not the length of the candidate. Not timed.

## Verified — batch 3 (profile preview)

### Profile preview treats every HTMX GET from that page as the ghost, and builds a user to do it

`ProfilePreviewMiddleware._in_scope` treats an HTMX request as part of the preview when the `Referer` path equals the stored preview path (`middleware.py:185-187`). The request's own path is not checked. While the preview session is set, a GET that carries `HX-Request` and that referer runs `_respond_as_ghost` (`middleware.py:165-168`, `223-250`).

`create_ghost_viewer` inserts a `User`, loads the profile the post-save signal just created, and may insert a friendship, a pin, and a location (`services/profile/profile_preview.py:61-74`, `97-114`). The middleware wraps that in `transaction.atomic()` and always `set_rollback(True)` (`middleware.py:243-250`), so those rows are not supposed to commit. The inserts and the new-profile label seeding still run on every such request. A profile page that fires several HTMX GETs pays that cost on each of them.

Because scope is the referer and not the URL, those requests are not limited to profile fragments. A layout poll or a partial whose referer is the profile page is answered as the ghost. Writes during preview are rejected (`middleware.py:165-166`). Not exercised in a browser.

## Verified — batch 4 (trips)

### The trips overview and calendar each load every trip the viewer belongs to

`TripOverviewView` materializes `Trip.objects.filter(profiles=profile).with_effective_dates()` into a list (`controllers/trip.py:379`) and passes that list to both the stat tiles and the overview calendar (`trip.py:392-393`). `RECENT_TRIPS_LIMIT` applies only to the two short lists beside that (`trip.py:368-381`). `TripCalendarView` does the same full load (`trip.py:443`). `with_effective_dates` is one annotated query, not a query per trip (`models/trips/queryset.py:48-65`). The row count is still every trip the profile is on. Not timed.

### Trip weather still calls the forecast API inside the request

`_build_activity_forecasts` calls `get_raw_forecast_slots` for each distinct coordinate bucket while building the page (`controllers/trip.py:1413-1434`). Results are memoized per rounded coordinate inside that one request. They are not deferred to a task. A trip with many distant activities holds the worker for that many upstream calls. This was an unverified lead in the first pass; the call site is now read.

### Trip invites can pass the member cap concurrently

`invite_member` counts `trip.profiles` and raises when `current_count >= max_members` (`services/trips/trip_membership.py:212-215`), then `get_or_create`s the membership (`trip_membership.py:233`). Nothing locks the trip between the count and the insert. Two invites that both observe `max_members - 1` both create a row.

## Verified — batch 5 (billing)

### Creating a Stripe customer is check-then-create against the Stripe API

`ensure_customer` returns an existing `BillingCustomer` or calls `stripe.Customer.create` and then `get_or_create` (`services/billing/stripe_client.py:54-65`). Two requests for a user with no row yet both pass the read and both create a Stripe customer. `get_or_create` keeps one local row. The other Stripe customer is orphaned. `ensure_product` is the same shape for a role with no `stripe_product_id`: `stripe.Product.create`, then `filter(pk=role.pk).update` (`stripe_client.py:79-88`). Two callers can create two products; the update keeps whichever id landed last.

### A subscription `updated` event can overwrite a concurrent `deleted`

`_handle_subscription_updated` loads the row and ignores the event only when that in-memory row is already `CANCELED` (`services/billing/webhooks.py:153-170`). `_handle_subscription_deleted` sets `CANCELED` and saves with no `select_for_update` (`webhooks.py:178-183`). The webhook view locks `StripeWebhookEvent`, not `RoleSubscription` (`controllers/billing_webhooks.py:64-74`). If `updated` reads the row before `deleted` commits, the guard does not see `CANCELED`, and `sync_from_stripe_subscription` writes the pre-cancel status back. The comment above the guard describes this ordering. The guard does not lock the subscription row.

## Verified — batch 6 (friends and notifications)

### Accepting a friend request checks the cap and then writes

`Friendship.accept` calls `profile_at_max_friends` for both profiles and then `_set_status(ACCEPTED)` (`models/friendship/model.py:183-188`). `profile_at_max_friends` is a `count()` (`friendship/model.py:164-168`). There is no `select_for_update` on either profile. Concurrent accepts can all observe a count under `max_friends_per_user` and all commit.

### `dismiss_notification` updates by primary key alone

`dismiss_notification` filters `NotificationLog` on `pk` and marks it dismissed (`services/notifications/notification_center.py:119-129`). It does not take a profile. `NotificationMarkReadView` does scope by `profile=profile` (`controllers/notifications.py:192-198`). Current production callers pass `suggestion.notification_id` or `share.notification_id` from a row they already hold (`services/visits/visits.py` around 440, `services/sharing/pin_sharing.py:326`), not an id from the query string. The function itself will dismiss whatever row that integer names.

## Verified — batch 7 (media bytes)

### The places photo proxy caches the full upstream body

`GoogleMapsPhotoProxyView` calls `places_resolution.download_photo` and `cache.set`s the returned bytes (`controllers/media_proxy.py:122-143`). The Google branch asks for `maxWidthPx` (`services/apis/locations/google/places.py:150-165`) and then returns `response.content`. The REData branch returns `response.content` with no width argument and no byte cap (`services/apis/locations/google/redata_places_gateway.py:221-223`). Both results are stored whole in the cache. Encoded size was not measured.

### PDF OCR and video rewrite each read the whole file into one `bytes`

OCR reads the stored PDF with `stored_file.read()` before `convert_from_bytes` (`services/media/documents.py:140-142`). Page count and pixel size are capped. The file size is not. Video rewrite reads the output mp4 with `f.read()` and passes that buffer to `ContentFile` (`services/media/videos.py:230-242`). Both run on the media task, so the cost is worker memory, not the request thread.

### The external-media byte ceiling is a read, then a write

`media_materialize` says so in the comment: the ceiling is re-read just before the insert, and two calls can still both pass (`services/media/media_materialize.py:272-279`). The comment is the behavior. An account can cache the ceiling plus whatever one overlapping burst stores.

### Avatar and achievement-icon fetches do not check that a row owns the path

`authorize_avatar` and `authorize_icon` return `True` and ignore `rel_path` (`services/media/access.py:201-213`, `249-259`). The docstrings say any signed-in user may load those images because they render on other people's pages. Any object stored under those prefixes is fetchable by any authenticated user who knows the storage key. Whether those keys are unguessable was not checked.

## Verified — batch 8 (limits, locks, presence)

### The paid-API limiter fails open when the count query fails, and the check is not a reservation

`check_rate_limit` refuses a billable call when it cannot load the limit row (`services/core/rate_limiter.py:489-502`). When the row loads but the `COUNT(*)` raises `DatabaseError`, it logs and returns `True` (`rate_limiter.py:537-539`). That is the unbounded-bill case the config-read branch says it is avoiding. The success path counts `ApiCallLog` rows and then returns allowed (`rate_limiter.py:505-541`). The log row is written later by `log_api_call`. Two calls that both count under the cap both proceed. Nothing in this function inserts a reservation.

### Releasing a sweep lock deletes whatever is stored under the key

`release_lock` reads the holder and, when it still equals this run's token, calls `cache.delete(key)` (`services/core/locks.py:40-42`). Delete is not conditional on the token. If the TTL expires after the read and another run `cache.add`s a new token before the delete, this release removes the new holder's lock. The branch at `locks.py:48-51` only skips the delete when the value has already changed by the time of the read.

### A cache error removes the WebSocket frame cap

`FrameBudget.consume` returns `True` when `bump_window_counter` raises (`services/core/frame_limits.py:67-71`). The log line says the frame is allowed. A cache outage, which is also when a stampede is most useful to an abusive client, stops charging the budget. `refund` is a separate `get` then `decr` (`frame_limits.py:84-85`) and can race the window, which the comment already says.

### DM presence is a counter with no expiry, and the first increment is not atomic

`mark_profile_online` `cache.incr`s `dm_online_{id}` and, on `ValueError`, `cache.set`s the key to `1` with `timeout=None` (`services/messaging/direct_messages.py:112-116`). Two first connections can both miss the key and both set `1`, so the counter under-counts. `mark_profile_offline` deletes the key when the decremented value is `<= 0` (`direct_messages.py:127-131`). An under-count then makes the profile look offline while a socket is still open. Because the key has no TTL, a process that dies without `disconnect` leaves the profile online until something else deletes the key.

## Verified — batch 9 (email verification)

### Resend-verification confirms an unverified address, and nothing in the view caps it

`ResendVerificationView.post` looks up the address with `active_only=False`, drops the user when they are already active, and otherwise deletes the old `EmailVerification`, creates a new one, sends mail, and stores `pending_verification_email` (`controllers/account.py:564-584`). The redirect is always `verify_email_sent`. The comment on that redirect says the redirect does not reveal whether the email exists.

`VerifyEmailSentView` pops that session key into the template (`account.py:497-508`). The template prints the address when it is set and the words "your email address" when it is not (`templates/registration/verify_email_sent.html:13-14`). An address that matches an inactive account comes back on the page. Any other address does not.

The view has no counter of its own. Each successful post for an unverified account also deletes the previous token (`account.py:576-577`), so a repeat both sends another mail and invalidates the link the account already has.

## Verified — batch 10 (memories API, achievement backfill, map share, billing sweeps)

`MemoriesTimelineView.get` calls `get_memory_events` with the client’s `start`/`end` and no `limit` (`external_api/views.py:1683-1704`). `MemoriesTimelineQuerySerializer` accepts any two dates (`external_api/serializers.py:2317-2324`). `get_memory_events` drains each source to completion when `limit` is `None` (`services/memories/aggregator.py:323-339`). Photos, visits, and trips are unsliced querysets (`aggregator.py:141-169`, `195-208`, `226-256`). `PaginatedListMixin.paginated_response` pages that already-built list (`external_api/pagination.py:84-127`); `max_page_size` is 100 (`pagination.py:35-37`), which bounds the response body and not the query. The web feed passes `limit=MAX_FEED_EVENTS + 1` (`controllers/memories.py:57`, `475-477`). `P69` records the journal feed as fixed and does not mention this timeline endpoint. Row count and query time were not measured.

`SiteAdminAchievementBackfillView.post` calls `evaluate_achievement_for_all` on the request (`controllers/achievements.py:306-318`). Saving an active award also enqueues `backfill_achievement` (`models/achievements/signals.py:249-264`), and that task calls the same function (`tasks.py:4339-4360`). The function loads every existing grant’s profile id into a set, then iterates every other profile and calls `metric.value_for` (`services/achievements/evaluate.py:184-218`). `sweep_achievements`’s docstring says evaluating every profile in one task would hit `CELERY_TASK_TIME_LIMIT` (`tasks.py:4503-4509`); the nightly path dispatches `sweep_achievements_range`. The backfill path does not. `sweep_achievements` and `sweep_reputation` still `list()` every relevant primary key before slicing (`tasks.py:4461-4467`, `4527-4533`). Duration was not measured.

`MarkupMapShareCreateView.post` always inserts a `MarkupMapShare` and a `NotificationLog` (`controllers/map_sharing.py:63-89`). `MarkupMapShare.Meta` has no unique constraint on map and recipient (`models/markup/share.py:14-45`). `share_markup_map_with_profile` skips a pin that is already shared (`services/sharing/map_sharing.py:35-36`, `54-70`); the map-share row and the notification are not skipped. How often the dialog is submitted twice was not measured.

`sync_stripe_subscriptions` calls `stripe.Subscription.retrieve` once per non-canceled `RoleSubscription` inside one task (`tasks.py:4557-4591`). `advance_pwyw_usage_ledgers` calls `banking.advance_usage_ledger` once per pay-what-you-want subscription in one task (`tasks.py:4595-4619`). Neither dispatches ranges. This is separate from the webhook race in batch 5. Wall time was not measured.

## Verified — batch 11 (overlay import, pin refresh, slide readiness)

`MapAnnotationsImporter._import_overlay` keeps a pasted `image_url` after `ensure_public_http_url` and `is_web_safe` (`services/import_export/import_data.py:2215-2233`). The live form does not: `_image_from_request` downloads the URL through `materialize_media_item` and returns an empty `image_url` (`controllers/map_overlays.py:263-292`). `test_a_pasted_external_url_is_downloaded_not_referenced` says the stored column must never hold the foreign URL because it is handed to every viewer’s browser as an `<img src>` (`tests/hypothesis/test_map_overlays.py:110-113`). `MapImageOverlay.source_url` still returns `image_url` when there is no stored file (`models/map_overlay/model.py:143-154`). The model docstring still describes `image_url` as a supported remote reference (`model.py:44-45`). How many imported archives still carry `image_url` was not counted.

`_refreshAllPins` fetches the full pin catalog and then mutates `_pinStore` and `clusterGroup` with no in-flight flag (`frontend/ts/entries/map-page.ts:991-1062`). Call sites invoke it without sharing a promise, including `promoteChildPins` (`map-page.ts:3242`), and further sites at `3563`, `3763`, `3957`, `6245`, `6380`, `6485`, `6499`, and `6521`. The two-minute poll also calls it (`map-page.ts:184`, `2213`). Whether two of those overlap in practice was not measured.

`collect_satellite_slides` and `collect_street_view_slides` append `ok=False` for `RateLimitExceededError` and for a bare `Exception`, and append nothing for `RequestCancelledError` (`services/pins/external_data.py:702-716`, `737-751`). `RateLimiterUnavailableError` and `ServiceDisabledError` are subclasses of `RequestCancelledError` and are not subclasses of `RateLimitExceededError` (`services/core/rate_limiter.py:744-784`). The limiter docstring says `RateLimiterUnavailableError` fires when `ul_web` is at its connection limit (`rate_limiter.py:747-750`). `SlidesPanelSource.fetch` sets the ready marker for `SLIDES_READY_TTL_SECONDS` (12 hours) when every recorded result is `ok` (`external_data.py:56`, `782-789`). A provider that raised `RateLimiterUnavailableError` is absent from that list, so the marker can be written as complete. Whether `is_ready` then skips a later fetch was not re-read past the marker write.

## Verified — batch 12 (account deletion, encrypted connections, site URL)

`hard_delete_profile` sends the “your account has been deleted” email and deletes stored files before `profile.user.delete()` (`services/profile/account_deletion.py:152-169`). `due_for_hard_delete` still selects on `deletion_requested_at` (`models/profile/queryset.py:32-37`). The sweep comment says that timestamp is not cleared until after the email, which is why a second overlapping run is locked out (`tasks.py:3568-3572`, `3597-3616`). A run that sends the email and then fails leaves the account selected, so the next sweep after the lock expires sends the completion email again. Whether `user.delete()` has failed after the email was not measured.

`ImmichAccountManager.get_for_profile`, `FlickrAccountManager.get_for_profile`, `GooglePhotosAccountManager.get_for_profile`, and `GoogleCalendarAccountManager.get_for_profile` delete the connection when loading it raises `InvalidToken` (`models/immich/model.py:28-35`, `models/flickr/queryset.py:34-42`, `models/google_photos/queryset.py:32-40`, `models/calendar_sync/queryset.py:33-41`). Immich uses raw SQL because a queryset `delete()` instantiates the row and decrypts it again (`models/immich/model.py:44-46`). Flickr, Google Photos, and Google Calendar call queryset `delete()` from inside the same `except`. `rotate_field_encryption` leaves an undecryptable value in place and tells the operator to add the old key to `UL_FIELD_ENCRYPTION_KEY_FALLBACKS` (`management/commands/rotate_field_encryption.py:120-129`). A settings or trips page that calls `get_for_profile` during a bad key deploy deletes the row before that command can keep it. How often a page load races a rotation was not measured.

When `UL_SITE_URL` is unset and `UL_ENVIRONMENT` is not `local` or `development`, startup logs a warning and `SITE_URL` is still `http://localhost:{port}` (`settings/base.py:20-22`, `1039-1066`). The warning says emails and safety alerts will contain those links. Whether staging or production is running with the variable unset was not checked.

## Verified — batch 13 (broker fallback, task time limits, friend-invite UI)

`CELERY_BROKER_URL` is `UL_CELERY_BROKER_URL`, else `UL_RABBITMQ_URL`, else `DRAGONFLY_URL`, else `redis://localhost:6379/0` (`settings/base.py:320-323`). `D16` moved the broker onto RabbitMQ so a full broker no longer shares Dragonfly with cache, sessions, and Channels (`docs/designs/dragonfly-rabbitmq-pgvector-stack-adoption.md:27-30`). Compose sets `UL_RABBITMQ_URL` on the app services (`docker-compose.yml:33`, `59`, `109`, `131`). A process started without `UL_CELERY_BROKER_URL` and without `UL_RABBITMQ_URL` uses Dragonfly for the broker. The result backend is Dragonfly in any case (`settings/base.py:324`). Whether any non-compose process starts without those variables was not checked.

`CELERY_TASK_SOFT_TIME_LIMIT` defaults to 2700 seconds and `CELERY_TASK_TIME_LIMIT` to 3600 (`settings/base.py:344-345`). `tasks.py` declares 100 `@shared_task` callables. Five of those decorators pass a tighter `soft_time_limit`: confirmed import and import preview (`tasks.py:439-449`), CRIS extraction (`tasks.py:1435`, soft limit only), one maintenance task at 3000/3300 (`tasks.py:3261`), and `fetch_panel_source` at 110/130 (`tasks.py:3624-3625`). The assistant turn sets 90/120 in `services/ai/tasks.py:28`. The other tasks in `tasks.py` inherit the global hour. How many of those can actually run that long was not measured.

`renderFriendCheckboxes` is implemented separately in `frontend/ts/entries/spotguessr.ts:567`, `trivia.ts:193`, and `consensus.ts:340`. SpotGuessr and Consensus keep the checked ids in a `Set` and draw `ul-checkbox-wrap`. Trivia draws a plain checkbox and reads `#trivia-friend-list input:checked` at submit (`trivia.ts:784-790`). The three copies have already drifted. No shared module was found.

## Verified — batch 14 (floorplan labels, overlay import cap, friend-request errors)

`FloorplanEditorView.get_context_data` embeds every `Label` for the pin's profile (`controllers/floorplans.py:406`). Photos on the same page are sliced at 60 (`floorplans.py:409`). `LabelCreateView.post` inserts a label after a name-conflict check and does not count existing rows (`controllers/labels.py:636-665`). How many labels one profile can hold was not measured.

`MapOverlayCreateView` refuses a thirteenth overlay (`controllers/map_overlays.py:38`, `484-485`). `MapAnnotationsImport._import_overlay` inserts each exported overlay with no count check (`services/import_export/import_data.py:2122-2126`, `2193-2255`). `MAX_OVERLAYS_PER_MAP` does not appear in `import_data.py`. The floorplan editor then serializes `pin.image_overlays.all()` (`floorplans.py:405`).

`FriendController.request_friend` returns a different sentence for `NO_ONE` and `FRIENDS` than for `COMMON_PIN`, `COMMON_FRIEND`, `COMMON_TRIP`, and `ANYTHING_IN_COMMON` (`controllers/friendship.py:246-259`). A signed-in caller who fails `Profile.visibility_permits` learns which of those policies is set. The pending-cancel path in the same file was written so its response does not reveal which kind of row matched (`friendship.py:399-405`). Whether the distinct sentences are shown in the UI was not checked in a browser.

## Verified — batch 15 (link archive, social-link probe, Gotify)

Pin and wiki link writes accept any `http` or `https` URL. `controllers/links.py:36-39` and `services/pins/pin_subresources.py:20-23` use `URLValidator` and say they deliberately do not call `ensure_public_http_url`, because that guard is for URLs the server will fetch. Creating a `PinLink` or `WikiLink` with an empty `wayback_url` queues `archive_pin_link_to_wayback` or `archive_wiki_link_to_wayback` (`models/links/signals.py:8-26`, `32-44`). That task calls `WaybackMachineGateway.get_availability` and, when there is no snapshot, `save_url` (`tasks.py:697-706`). `save_url` GETs `https://web.archive.org/save/{url}` with `allow_redirects=True` (`services/apis/locations/wayback_machine.py:111`). The session is the rate-limited gateway session, not `fetch_public_url`. `http://127.0.0.1/` and `http://169.254.169.254/` pass `URLValidator`. Whether a redirect from the save endpoint is followed to that host was not tested.

`SocialLinkVerifyView` builds a URL from a fixed platform template and a validated handle, then `requests.get`s it with `allow_redirects=True` and does not call `fetch_public_url` (`controllers/userprofile.py:864-915`, `services/profile/social_links.py:14-25`, `83-85`). The response body is closed unread. A 4xx or 5xx status is put in an HTMX toast (`userprofile.py:922-932`). `website` is not in `VERIFIABLE_PLATFORMS`. The DNS pin in `url_safety` applies only when a caller sets one (`services/security/url_safety.py:94-111`). A redirect off the platform host is not checked. Whether any of those platforms redirect was not tested.

`SiteSettings.notify_gotify_url` is a plain `CharField` whose default is `UL_GOTIFY_URL` (`models/site_settings/model.py:387-392`). `_send_gotify` POSTs to `{url}/message` with the token in the query string and does not call `fetch_public_url` (`services/notifications/notifications.py:85-97`). The field is on the Django admin site-settings form (`admin.py:106`). Who can save that form was not re-read.

## Verified — batch 16 (label creates, trip-activity locations)

`Label` is unique on `(lower(name), profile, kind)` with `nulls_distinct=False`. `models/CLAUDE.md` says an exact-match `get_or_create` cannot recover from that constraint, because the retry repeats the exact lookup, and that write paths should call `find_conflicting_label`, which also treats a global label (`profile` null) as a collision the database itself allows.

These creates do not call `find_conflicting_label`:

- `controllers/pin_edit.py:334-349` drops the pin's category labels, then `filter(name__iexact=..., profile=pin.profile)` and `get_or_create(name=name, ...)`. A failure after the `remove` leaves the pin without those categories. The lookup is limited to that profile, so a global category of the same name is not seen.
- `services/media/media_labels.py:76-83` does the same iexact-then-exact `get_or_create` for `KIND_MEDIA`, also scoped to `profile=profile`.
- `services/import_export/import_data.py:471-489` matches `name__iexact` on the importer's profile, then `Label.objects.create`. A second row in the same file whose name differs only by case is not the row just inserted if the first lookup ran before that insert; the create then hits the constraint with no handler in this loop.

`services/apis/locations/google/maps.py:1126-1134` and `tasks.py:2669-2677` pass `name__iexact` as the `get_or_create` lookup and `name` in `defaults`. That is the lookup the model note says can see a case variant. They still do not consult global labels. Whether a case-variant race has been observed was not measured.

`_resolve_activity_place` creates a `Location` with `get_or_create(latitude=lat, longitude=lng)` when a trip activity posts geocoded coordinates (`services/trips/trip_activities.py:235-254`). `Location` is unique on `(latitude, longitude)` (`models/location/model.py:413-415`). The `except` covers `ValueError` and `TypeError` only. A concurrent insert raises `IntegrityError` out of this function. How often two activities geocode the same point together was not measured.

## Verified — batch 17 (upload quota lock, duplicate checksum)

`per_profile_upload_lock` yields `False` when `acquire_lock` returns `None`, logs a warning, and still enters the `with` body (`services/media/storage.py:149-172`). `acquire_lock`'s own docstring says a `None` token means the caller must not do the work (`services/core/locks.py:20-29`). No upload caller reads the yielded flag. The ones that wrap the quota check and the insert are `services/photos/photo_upload.py:149-154`, `services/photos/uploads.py:292-294`, `controllers/article.py:375`, `controllers/visits.py:196`, `controllers/safety.py:1429`, `controllers/direct_messages.py:467`, `controllers/consensus.py:483`, `controllers/maps.py:628`, `controllers/tools.py:696`, `services/pins/pin_suggestions.py:616`, and four sites in `tasks.py` (`2380`, `2983`, `3082`, `3187`). The lock TTL is 30 seconds (`storage.py:28`). A second upload that cannot take the lock still runs `quota_error_for_upload`.

`quota_error_for_upload` treats a missing size as 0 bytes and allows the upload (`storage.py:129-142`). The docstring says the true size is recorded once the file is stored.

The duplicate-file check in `photo_upload.py:132-133` and `uploads.py:281-286` runs before that lock. `Image.checksum` is indexed and not unique (`models/images/model.py:298`). Two requests that upload the same bytes can both see no row and both insert. Whether that race has been hit was not measured.

## Verified — batch 18 (Immich server fetches)

`ImmichAccountForm.clean_server_url` calls `ensure_public_http_url` before a server URL is stored (`forms/immich_form.py:23-43`). The form docstring says the live ping and later asset proxies are the requests that matter. Those requests do not use `fetch_public_url`. `ImmichGateway._get`, `_get_binary`, and `_post` call `self.session.get` / `self.session.post` on `{server_url}/api...` (`services/apis/immich/gateway.py:89-90`, `121`, `155`). `requests` follows redirects by default, and the DNS pin in `url_safety` applies only inside `fetch_public_url` (`services/security/url_safety.py:153-184`). A host that was public at save time can later resolve somewhere else, and a redirect is not checked. The same request sends `x-api-key` (`gateway.py:73-74`).

`_get_binary` stops at `max_bytes` because one user's Immich response is paid for by every request on that worker (`gateway.py:101-135`). `_get` returns `response.json()` with no byte cap (`gateway.py:89-96`). Library walks use that JSON path, up to 500 pages of 1000 assets (`gateway.py:25-28`).

The July audit's "no private-IP guard on `server_url`" (`docs/audits/codebase-audit.md`) is not this claim. The form check exists now. The live fetch still does not use it.

## Verified — batch 19 (Gmail aliases, API key writes)

`normalize_email` treats `gmail.com` and `googlemail.com` as the same kind of address for dot-stripping and plus-stripping, then keeps the domain (`services/auth/email_normalization.py:10-28`). `jakesmith@gmail.com` and `jakesmith@googlemail.com` therefore compare as different. `is_email_taken` and `find_user_by_email` use that value (`email_normalization.py:43-60`, `77-80`). Signup rejects only when `is_email_taken` is true (`controllers/account.py:362-368`). `Profile.primary_email_normalized` is indexed and not unique (`models/profile/model.py:335`). The same Google mailbox can open two accounts, and login by one form does not find the other. `test_googlemail_alias_domain_also_normalized` asserts the domains stay distinct (`tests/hypothesis/test_email_normalization.py:33-34`). Whether both addresses have been registered was not checked.

`authenticate_api_key` updates `last_used_at` on every successful check (`services/auth/api_keys.py:95`, `129-135`). The external API then inserts an `ApiKeyUsageLog` row and loads every older primary key past the 20 most recent, with no upper bound on that slice, and deletes them (`external_api/authentication.py:63-65`, `api_keys.py:25-27`, `138-148`). That is two writes and an offset query on every authenticated API call. `generate_api_key` checks that a prefix is free and then inserts it outside that loop (`api_keys.py:52-75`). The comment says this avoids an `IntegrityError`. The insert does not catch one. `prefix` is unique (`models/account/model.py:195`). How often two creates pick the same prefix was not measured.

## Verified — batch 20 (lost upload scans)

Pin photo upload marks the row for a later scan and then ignores a failed enqueue. `controllers/maps.py:619-636` skips the malware scan, creates the `Image` with `pending_scan` set by `prepare_photo_upload`, calls `safely_enqueue_task(process_image_upload, img.pk)`, and returns 200 whether or not that call returned a task. `safely_enqueue_task` returns `None` when the broker is unreachable (`services/core/celery.py:123-135`). The same ignore is in `controllers/comments.py:85-88` for a comment photo, `controllers/article.py:396`, `controllers/direct_messages.py:479`, `services/media/upload_failures.py:120`, and `services/wiki/wiki_share.py:162`.

Recovery does not treat that `None` as a failure. `requeue_stalled_pending_uploads` only selects images with `pending_scan` older than 6 hours (`tasks.py:1581-1582`, `1690-1694`) and runs once an hour at minute 19 (`settings/base.py:481-484`). Comment scans wait until the row is an hour old (`services/media/upload_retry.py:45`, `186`) and the adopt task runs once an hour at minute 53 (`settings/base.py:507-510`, `tasks.py:1656-1663`). A lost publish leaves the file unscanned, and still showing as processing, until that window passes.

The pin upload also checks the checksum before the quota lock (`controllers/maps.py:625-634`). `Image.checksum` is not unique. That is the same race batch 17 recorded on the other photo upload paths.

## Verified — batch 21 (SSO email collisions, password reset lookup)

Password signup, settings, and the profile email form call `is_email_taken` (`controllers/account.py:362-368`, `forms/settings_form.py:291`, `controllers/userprofile.py:435`, `793`). The SSO pipeline does not. It is the stock `create_user`, `associate_user`, and `user_details` steps (`settings/base.py:1001-1015`). `user_details` writes the provider email onto the user. `create_user_profile` then stores `normalize_email` of that address with no uniqueness check (`models/profile/signals.py:14-25`). `Profile.primary_email_normalized` is indexed and not unique (`models/profile/model.py:335`). `User.email` is not unique either. A Google or Discord login can create a second account on an address a password account already uses, and a later provider email change can move an SSO account onto an address another profile already has. Nothing in the pipeline reads a provider "verified" flag before that write. `find_user_by_email` returns the first profile (`services/auth/email_normalization.py:47-50`). `SsoAwarePasswordResetForm.get_users` yields every active user whose `email` matches case-insensitively (`controllers/account.py:687-688`), so a reset for a duplicated address mails every match.

That reset lookup is not `normalize_email`. Login is. `EmailOrUsernameModelBackend` calls `find_user_by_email` (`services/auth/auth_backend.py:27`), which strips Gmail dots and plus tags (`email_normalization.py:13-28`). A password reset of `jakesmith@gmail.com` does not find a user stored as `jake.smith@gmail.com`. Whether anyone has hit that mismatch was not measured.

## Verified — batch 22 (device-scan trust and lost uploads)

`resolve_device_type` returns the client-supplied type whenever `device_type_guess` is set (`services/device_scan/type_guessing.py:98-99`). The upload serializer allows any `DeviceType`, including camera, sensor, and tracker (`external_api/serializers_device_scans.py:48`). Those three are `SECURITY_RELEVANT_TYPES` (`models/device_scan/model.py:40`). Processing then writes a marker on every wiki whose official geometry contains the client-supplied point, with no check that the uploader can see that wiki (`services/device_scan/pipeline.py:42-47`, `services/device_scan/wiki_lookup.py:13-26`). The nearby read is scoped to visible wikis (`external_api/views_device_scans.py:98-101`). The write is not. The upload requires `device_scans:write` (`views_device_scans.py:48-49`). Whether a default key has that scope was not re-checked in this pass.

The view still returns 202 when `safely_enqueue_task` returns `None` (`views_device_scans.py:67-74`). Nothing in the beat schedule requeues a `PENDING` device-scan upload. `process_device_scan_upload` flips the row to `PROCESSED` before the work (`tasks.py:4272-4288`). A worker that dies after that claim does not run it again, because a later delivery sees the row is no longer pending. The task docstring says a row left `PENDING` means the task never ran (`tasks.py:4267-4270`).

## Verified — batch 23 (credentials that survive a password change)

A password reset updates the Django password and, only if the form box is checked, revokes API keys (`controllers/account.py:756-784`). The in-app password rotation sets a new password and updates the current session hash (`controllers/e2ee.py:857-861`). Neither path deletes OAuth2 access tokens or refresh tokens. `REFRESH_TOKEN_EXPIRE_SECONDS` is 90 days and refresh tokens rotate (`settings/base.py:1189-1191`). A refresh token issued before the reset still exchanges for access until it expires or is rotated by use. Browser sessions pick up the new password hash. These tokens do not.

The same in-app rotation does not call `revoke_all_api_keys`. An API key issued before the change keeps working. The reset page can revoke them, and only when that box is checked.

## Verified — batch 24 (fact confidence)

The first observation for a `(subject, key)` pair uses `get_or_create` and does not catch `IntegrityError` (`services/facts/evidence.py:86`). `Fact` is unique on `(location, key)`, `(wiki, key)`, and `(image, key)` when that subject is set (`models/facts/model.py:204-206`). Two first writes for the same pair can both miss the row and one insert then fails.

`recompute` loads every non-superseded evidence row and saves the fact with no lock (`services/facts/confidence.py:193-225`). Two workers for the same fact can each snapshot the table and the slower one writes the older confidence back. `record_evidence` ignores a failed enqueue and says the next write or a sweep will redo it (`services/facts/evidence.py:143-146`). The only caller of `recompute_fact_confidence` is that enqueue (`tasks.py:4250`). There is no sweep. A publish that returns `None`, or a recompute that loses the race, leaves confidence where the last save put it until another observation arrives.

## Verified — batch 25 (blocks and group chats)

A direct message refuses a pair when either profile has blocked the other (`models/profile/model.py:1128-1129`, `services/messaging/direct_messages.py:705`). `block_profile` also drops safety-partner rows, pending pin shares, and map shares (`services/social/friendship.py:390-392`). It does not touch group chats.

Creating a group and adding a member only ask whether the actor may message that one person (`services/messaging/group_chats.py:182-184`, `291-293`). A block between two other members is not consulted, so a third profile who can message both can put them in one group after the block. Sending only checks that the sender is still a member (`group_chats.py:566-568`). Identity resolution for those messages does not read `are_blocked`. A blocked member who is still in the group keeps receiving messages, including ciphertext under a group key they still hold.

## Verified — batch 26 (list pages, descendant walks, calendar, map centre, probes, export)

The trips list page loads every trip the profile belongs to, prefetches every membership, and masks identities in Python (`controllers/trip.py:415-418`, and again at `493`). Sorting by start date ascending then sorts that full list in Python (`models/trips/queryset.py:121-122`, `126-146`). There is no trip cap. The calendar import action rebuilds the same full list after it returns (`controllers/calendar_sync.py:303-305`). A pin-list detail page also loads every trip, plus every saved filter (`controllers/pin_lists.py:280-281`). Batch 4 was the overview and the calendar page, which use `with_effective_dates` for stats. This is the list itself.

`PinQuerySet.with_descendants` and `WikiQuerySet.with_descendants` walk children one level at a time in Python, then filter `pk__in` the collected set (`models/pin/queryset.py:60-75`, `models/wiki/queryset.py:36-38`). Request paths that do this before they page or render include the photo gallery and albums when children are included (`controllers/image_gallery.py:123-124`, `controllers/albums.py:83-85`), pin and wiki comments (`controllers/comments.py:322-323`, `434`), markup and overlays (`controllers/markup.py:345`, `controllers/map_overlays.py:140`), detail pins (`controllers/detail_pins.py:255`), and pin bulk (`controllers/pin_bulk.py:98`). A deep or wide tree is one query per level plus an `IN` list of every id.

Calendar import asks for a year of events (`services/trips/calendar_sync.py:32`, `431-434`) but `list_events` requests one page of 100 and does not read `nextPageToken` (`services/apis/calendar/google.py:226-252`). The preview and the import each call `get_event` once per posted id, on the request, with a 30 second timeout and no cap on how many ids were posted (`controllers/calendar_sync.py:280-295`, `335-340`, `google.py:201-208`). Exporting a trip then writes one Calendar request per scheduled activity inside a single task (`calendar_sync.py:652-656`). Saving a trip or activity enqueues that push and ignores a `None` result (`models/trips/signals.py:30-34`). Nothing in the beat schedule retries a push that never left.

`get_map_center_template_context` is rendered from the map, memories, trips, pin lists, and saved filters. In automatic mode, and as the GPS fallback, a centre whose `map_center_stale_since` is older than seven days is recomputed on that request (`models/profile/model.py:72`, `846-857`, `879-891`). The recompute reads every pin coordinate for the account and clusters them in Python (`804-820`). Creating a pin tries to keep that work on a task and only logs a failed enqueue (`models/pin/signals.py:51-78`). After seven days the next page that needs a centre does the scan itself.

The readiness and primary health probes are unauthenticated and, on every call, construct a `MigrationExecutor` and ask it for a plan, and when the database answered they also count `pg_stat_activity` (`controllers/health.py:83-128`, `152-175`, `215-228`). The module says a probe must not be the thing that loads a worker.

A data export builds one in-memory list of every pin, with labels, aliases, and article text, then writes it as one JSON document (`services/import_export/export.py:548-592`). The Takeout path holds the same pin set in a `StringIO` (`595-614`). That runs as one bulk task under the global hour limit.

## Verified — batch 27 (rows nothing deletes)

Sessions use `django.contrib.sessions.backends.cached_db` (`settings/base.py:286`). The beat schedule prunes API-call logs, pin tombstones, undo rows, and expired direct messages. It does not run `clearsessions`. Expired session rows stay in Postgres, and the cache copy stays until its own TTL.

`NotificationLog.message` allows 50,000 characters (`models/notifications/model.py:40`). Nothing in `tasks.py` deletes notification rows. The history view pages them (`controllers/notifications.py:176`). The table itself only grows. Map-share creation already inserts one of these rows per share (batch 10).

A device-scan upload stores every entry and every raw signal reading (`models/device_scan/model.py:105-180`). The reading model says the samples are kept for later clustering. No task deletes uploads, entries, or readings. `client_session_uuid` is stored and the model says the server does not dedupe on it (`model.py:111-113`), so a retried upload inserts another copy. One upload may carry 200 devices and 500 readings each (batch 22). Those rows remain after the upload is marked processed.

The visit-history panel pages visits, then loads every pending visit suggestion for that place with no slice (`controllers/visits.py:133-140`).

## Verified — batch 28 (public pins, backups, group inbox, invite mail)

The nightly public-pin task has no lock (`tasks.py:3755-3766`). For every passed place it loads every suggestion's profile id and every pin's profile id into sets, then every opted-in profile, then builds one `PinSuggestion` per recipient in memory and `bulk_create`s them (`services/pins/public_pins.py:306-335`). `PinSuggestion` is indexed on `(profile, status)` and is not unique on profile and location (`models/pin_suggestions/model.py:142-146`). Two overlapping runs both miss the new rows and insert them twice. The same task also walks every open candidate and compares it to every already-passed coordinate in Python (`public_pins.py:274-293`).

Database backup runs `pg_dump` of the whole database to a plain SQL file, with a 30-minute timeout and no lock against a second dump (`core/controllers/backups/db.py:24`, `125-175`). The task retries on `OSError`, and the scheduled task calls the same runner (`tasks.py:3237-3252`). The database password is placed in the dump process environment (`db.py:170-175`). The dump shares the application database.

A group caps members at 50 (`services/messaging/group_chats.py:111`). Nothing caps how many groups one profile can belong to. The inbox loads every membership, ORs one visibility clause per group, and sorts the merged direct-message and group list in Python (`group_chats.py:833-857`, `services/messaging/direct_messages.py:1106-1117`). The direct-message half of that list was measured and left in `P69`. The group half was not.

Inviting someone by email saves the invitation, then calls `EmailMultiAlternatives.send` on the request (`controllers/friendship.py:546`, `services/social/friendship.py:680-712`). SMTP is allowed 10 seconds (`settings/app.py:128`). A send failure is logged and not shown, so the page looks the same whether the message left or not.

## Verified — batch 29 (mail on the request, channel buffer, profiles, map counts)

Friend requests, friend accepts, trip-member adds, pin shares, comment replies, and visit suggestions call `send_notification_email` in the same call that writes the row (`services/social/friendship.py:155-156`, `services/trips/trip_membership.py:150`, `services/sharing/pin_sharing.py:104`, `services/notifications/comment_notifications.py:114`, `services/visits/visits.py:378`). That builds the message and calls `EmailMultiAlternatives.send` before returning (`services/notifications/notification_delivery.py:33-40`). SMTP is allowed 10 seconds (`settings/app.py:128`). A failure is logged and swallowed (`notification_delivery.py:45-48`). The email body is the notification message, which may be up to 50,000 characters (`models/notifications/model.py:40`). The join-by-email path in batch 28 is a separate send.

Live toasts and chat broadcasts are enqueued and a failed enqueue is ignored (`services/core/channel_broadcast.py:12-24`). The channel layer is Redis-protocol Dragonfly, the same URL as the cache, with `capacity` 1500 and `expiry` 60 (`settings/base.py:290-304`). A message that sits in that buffer longer than a minute is no longer delivered. The Celery broker was moved off this store (batch 13). This layer was not.

`Profile.user` is a one-to-one field (`models/profile/model.py:620-623`). Creating a user calls `Profile.objects.get_or_create(user=instance)` and does not catch `IntegrityError` (`models/profile/signals.py:16-20`). The same call, also uncaught, runs at the start of map, trip, overlay, and list views, including `controllers/maps.py:644` and `1107`.

Serving map payloads counts every matching pin, then serializes up to `MAP_DOCUMENT_MAX_PINS` (30,000) (`services/map_pins/filter_results.py:74-97`, `services/map_pins/document.py:141-147`, `controllers/maps.py:1113-1115`). The paged JSON endpoint defaults to 500 pins and allows 1,000 (`services/map_pins/payload.py:261-262`, `476`).

## Verified — batch 30 (reputation ledger, custom fields, wiki history, API pages)

Reputation events are never deleted. Recomputing a total sums every scored row for that profile (`services/reputation/scoring.py:301-316`). The first event for a profile calls `ProfileReputation.objects.get_or_create` and does not catch `IntegrityError` (`scoring.py:289-298`). `profile` is one-to-one (`models/reputation/model.py:133`). Two first events can both miss the row. The later recompute locks with `select_for_update` (`scoring.py:319`). This first write does not.

A custom field's text is a `TextField` with no length check (`models/custom_fields/model.py:438`, `662-663`). A URL field accepts any `http` or `https` string from `URLValidator` and stores it (`644-650`). It does not call `ensure_public_http_url`. Nothing in the beat schedule deletes these values.

`apply_wiki_edit` reads the wiki, writes the changed columns, and inserts a `WikiEdit` with no lock on the wiki (`services/wiki/wiki_edits.py:100-148`). Two edits of the same field both read the old value; the slower save keeps its copy and records that stale value as `from`. `WikiEdit.changes` is a JSON field with no size limit (`models/wiki_edit/model.py:22`). Nothing deletes edit rows. A boundary change stores the geometry text in that JSON (`wiki_edits.py:166-178`).

External API list endpoints use page numbers (`external_api/pagination.py:27-37`). `page_size` stops at 100. The page number does not. A high page is still a `COUNT` plus an `OFFSET` of `page * page_size` before the empty page is rejected.

## Verified — batch 31 (geolocation pings, trivia generation, saved filters)

The map records a geolocation visit on the request (`controllers/maps.py:154-169`). It loads every root pin within 5 km, then for each one resolves a property polygon and, when there is none, runs another distance query (`services/visits/visits.py:638-658`, `583-599`). The view has no rate limit. Same-day dedupe is a read of today's visits followed by `PinVisit.objects.create` (`645-655`). Two overlapping pings can both miss that set and both insert. Each new visit then updates the pin and its status labels one at a time (`655-657`).

Hourly trivia generation considers 25 wikis and, for each, sends the whole description to the model, then classifies every returned question with a second model call, before the next wiki starts (`services/trivia/generation.py:29`, `61-84`, `102-127`, `services/trivia/classifier.py:77-86`). Approved questions are inserted and never deleted. The candidate query excludes every location that already has an AI question (`generation.py:112-114`). A failure on one wiki is skipped. The task lock is 3300 seconds (`tasks.py:3906-3914`). The global task limit is an hour (batch 13).

Applying saved filters on the map takes every posted filter id, with no cap (`controllers/maps.py:140-149`). A cache miss loads every matching pin uuid into a list and uses it as an `IN` filter (`services/search/saved_filter_cache.py:60-64`). The cache key includes a fingerprint of the whole pin collection (`saved_filter_cache.py:28-33`). A replaced entry stays until its TTL, on the same cache as sessions (`saved_filter_cache.py:21-24`). Login already warms those caches (batch 1). A miss, or a filter newer than the warm, does the full scan on the request.

## Verified — batch 32 (photo maps, push delivery, assistant calls, upload locks)

Pin, wiki, and album map layers plot at most 500 photos (`services/geo/sampling.py:33`, `156-160`). Crossing that count loads up to 50,000 coordinate rows into a Python list and then filters the photos back down (`sampling.py:44`, `135`, `157-160`). The pin and wiki gallery JSON endpoints do this on the request (`controllers/image_gallery.py:197-204`, `493-501`). An album page first materializes every membership id (`services/photos/albums.py:307-309`, `controllers/albums.py:310-311`) even though the grid is paged, then runs the same cap on that full id list (`albums.py:330-332`).

A profile can register any number of push devices. The only uniqueness is `(profile, address)` (`models/push_device/model.py:48-49`). `dispatch_native_push` walks every active device and POSTs them one after another with a 5 second timeout (`tasks.py:3996-4016`, `services/notifications/push.py:27-31`, `146-166`). The blocked-address check runs at registration (`push.py:70-83`) and is not repeated at send. The POST does not disable redirects.

An assistant turn checks a 75 second deadline only before the next provider call (`services/ai/assistant.py:30-33`, `130-140`). The HTTP client waits 90 seconds (`settings/app.py:733-734`, `services/ai/inference_client.py:170-174`). The task's soft limit is 90 seconds and its hard limit is 120 (`services/ai/tasks.py:28`). One call that is already in flight is not cut off at 75 seconds.

Logging a visit holds the profile upload lock across every attached photo: hash, quota sum, and save (`controllers/visits.py:196-231`). The lock expires after 30 seconds (`services/media/storage.py:28`, `163-172`). The release path is written so a request that outlives the lock must not delete the next holder's lock. Quota for that batch is then no longer serialized. Batch 17 is the checksum that runs outside this lock on a different upload path.

## Verified — batch 33 (album panels, text alerts, article history, place search)

The Photos tab lists every album by loading every membership and its image (`controllers/albums.py:377`, `services/photos/albums.py:202-219`, `239-244`). The grid of loose photos is sliced (`controllers/albums.py:395-399`). The album list is not. The same page builds move targets by walking parent pins one query at a time and then listing the whole tree (`controllers/albums.py:422`, `services/photos/albums.py:552-569`). Reordering an album accepts every posted id (`controllers/albums.py:1098-1099`, `450-454`) and loads every membership row (`services/photos/albums.py:472-500`). Pin-list and trip reorders are capped at the container's own limit. Albums have no such ceiling. One album's map sample is batch 32. The descendant walk used when children are included is batch 26.

WhatsApp and SMS alerts for a direct message and for a site notification are queued with `safely_enqueue_task`, and the return value is ignored (`services/messaging/direct_messages.py:515-536`, `services/notifications/notification_text_alerts.py:92-105`). Nothing in the beat schedule calls those tasks again. A failed enqueue is a missed alert. The send itself runs later on a worker (`tasks.py:3680-3709`, `3773-3796`) and is not the request-path mail in batches 28 and 29. Scan retries in batch 20 do not cover this.

Every article save stores another full copy of the text, up to 200,000 characters (`services/core/text_limits.py:10`, `services/wiki/articles.py:281-294`, `models/article/model.py:124-130`). Nothing in `tasks.py` deletes `ArticleRevision` rows. The history view pages them at 25 (`controllers/article.py:35`, `443-444`). Wiki field history is batch 30. This table is the article body.

Map place search calls Google or REData on the request for each query of two or more characters (`controllers/maps.py:394-417`, `services/map_pins/autocomplete.py:256-270`, `services/apis/locations/places_resolution.py:164-189`). `GooglePlacesGateway.autocomplete` does not pass its own timeout (`services/apis/locations/google/places.py:180-188`). The shared session then waits up to 5 seconds to connect and 30 seconds to read (`services/core/rate_limiter.py:721-725`). The view has no throttle of its own. `P113` records a set of request-thread upstream calls as fixed apart from four parked ones. This autocomplete path is not one of those parked names.

## Verified — batch 34 (trip weather, nearby places, custom fields, safety home)

The trip weather panel loads every activity (`controllers/trip.py:1553`, `services/trips/trip_activities.py:84-102`). Upcoming forecasts call the weather provider once per distinct coordinate, on the request, with no deadline around the view (`controllers/trip.py:1571`, `1401-1437`, `services/apis/locations/weather_resolution.py:85-113`). Past activities are grouped the same way. The historical lookup builds every calendar day from the earliest activity to the latest (`services/locations/visit_weather.py:320-324`, `controllers/trip.py:1501-1506`) and asks for that whole span (`visit_weather.py:162-181`, `services/apis/locations/redata_weather_gateway.py:44-65`). `scheduled_at` has no minimum (`models/trips/model.py:249`). Recorded days are kept from 1940 (`visit_weather.py:27`). The stored cache for a place is one JSON document, loaded in full (`models/cache/location_cache.py:23`, `visit_weather.py:48-61`).

A map click for nearby places, on a cache miss, calls Google or REData, then park search, then Wikipedia, one after another, before it responds (`controllers/maps.py:892-944`, `949-1042`). Place details is a separate request-path fetch of one place (`maps.py:1045-1088`). The shared gateway session waits up to 30 seconds to read (`services/core/rate_limiter.py:721-725`). Wikipedia's own call uses 10 seconds (`services/apis/assets/wikipedia.py:140`). This is the places layer. The search-box autocomplete is batch 33. Street-view and satellite carousels go through `call_with_deadline` (`controllers/pin.py:820-827`, `906-936`) and are not this finding.

Creating a custom field checks the name and then inserts (`controllers/custom_fields.py:262-265`). There is no count. The pin panel and the settings panel load every field for that profile (`custom_fields.py:277-287`, `380-388`, `87-96`). Each reference field then loads up to 500 choices (`models/custom_fields/model.py:273-289`, `services/custom_fields/custom_field_references.py:31`, `155-172`). Unbounded value text is batch 30. This is how many fields a pin page walks.

The safety overview loads every check-in the profile still has, with its contacts, and the stats helper turns that queryset into a list (`controllers/safety.py:408-418`, `313-326`). Auto-delete only applies when the profile set a window. A null window means never (`models/safety/queryset.py:71-84`, `tasks.py:3463-3467`).

## Verified — batch 35 (floorplan reads, map shell, labels, historical maps)

The floorplan map reads every wall on the selected floors, with every opening and lock, before it applies the bounding box (`services/floorplans/features.py:122-166`). The response stops at 2,000 features (`features.py:16`, `111-118`), and the wall loop breaks after that (`165-166`). The query is not sliced, so the break happens after the rows are loaded. A save may store 250 floors and 2,000 walls on each (`services/floorplans/serialization.py:247-248`). Omitting `level` selects every floor (`controllers/floorplans.py:251-265`).

Opening the map embeds every visible non-user label into the page (`controllers/maps.py:183-188`) and every pin list, with a count annotation (`maps.py:199`). The same view lists every pin custom field (`maps.py:190-192`). There is no cap on how many labels or lists a profile can have. The organize screen does the same for one kind: it materializes every matching label (`controllers/labels.py:322-323`) and every parent edge owned by those profiles or by nobody (`models/labels/model.py:169-178`). Global labels are part of `visible_to` (`models/labels/queryset.py:61-65`).

Browsing historical maps for a pin calls REData on the request and waits up to 30 seconds (`controllers/map_overlays.py:689-706`, `services/apis/locations/redata_historical_maps_gateway.py:23-69`, `services/apis/locations/redata_context_gateway.py:17`, `209-217`). The tile proxy that follows uses a fetch slot and a cache (`controllers/historical_map_tiles.py:99-114`). The browse list does not. Pin web search is wrapped in `call_with_deadline` (`controllers/pin.py:820-831`) and is not this finding.

## Not re-opened

A parallel read flagged the following. Spot-checks of the same pass refuted
other claims (comment replies are prefetched in `top_level_comment_queryset`;
`extra_html|safe` on the map toolbar is a hardcoded span in
`templatetags/map_components.py`, not plugin HTML; `RequestCancelledError` in
the satellite collector is commented as intentional; `CELERY_TASK_REJECT_ON_WORKER_LOST = False`
is commented as the choice that avoids an unbounded requeue). Because those
were wrong, the items in this list are leads, not findings.

- Map click "nearby places", trip weather, REData and Google photo proxies,
  Immich thumbnails, historical tiles, pin web search, Flickr/Immich panels,
  and Street View metadata were described as synchronous upstream calls on
  the request thread. Several are already wrapped in `call_with_deadline` or
  `UpstreamSlots`. Diff them against `P113` before filing. Place autocomplete
  on the map search box is batch 33. The nearby-places layer and trip weather are batch 34. Pin web search uses `call_with_deadline`. Historical-map tiles use a fetch slot. The browse list is batch 35.
- Satellite/street-view completeness when a provider is cancelled was
  verified in batch 11.
- `services/memories/aggregator.py` when `limit` is `None`, the achievement
  backfill, and the Stripe / pay-what-you-want sweeps were verified in batch 10.
- Global Celery time limit and the Dragonfly broker fallback were verified
  in batch 13. Compose sets `UL_RABBITMQ_URL`; a process without it was not
  checked.
- `DEBUG` defaulting on for `local` and `development`, and `csp_enforce`
  defaulting to report-only (`settings/base.py:57`, `857-862`;
  `settings/app.py` field default). `P56` is the COEP report-only entry, not
  this CSP default.
- `UL_SITE_URL` falling back to localhost outside dev was verified in batch 12.
- Login-time and HTMX paths that install document-level listeners without an
  install-once guard: `mention-autocomplete.ts`, `popup-dismiss.ts`,
  `organize-filter-engine.ts`.
- `_refreshAllPins()` call sites were verified in batch 11.
- Friend-invite checkbox UI copied across the three game pages was verified
  in batch 13.
- Overlay URL handling on import was verified in batch 11. The file is
  `services/import_export/import_data.py`, not a top-level `import_data.py`.
- OAuth avatar and UnifiedPush registration were verified above. Send-time fan-out is batch 32. Batch 15 covered the
  social-link probe, the Wayback save of a stored link, and the Gotify POST.
  Other `requests.get` / `requests.post` sites were not enumerated.

## What this pass did not do
Batch 12 covered the deletion-completion email, undecryptable OAuth rows, and `UL_SITE_URL`. Plugins were sampled for user-supplied
URLs and did not show a `fetch_public_url` gap in that sample; they were not
read plugin by plugin. Batch 2 covered sharing, undo, login, signup, and
passphrase/password-check limits. Batches 4–7 covered trip loading and
invites, Stripe customer/subscription races, friend-cap accepts, notification
dismiss, and in-memory media reads. Batch 8 covered the paid-API limiter,
sweep-lock release, WebSocket frame budgets, and DM presence. Batch 9 covered
verification resend. Batch 10 covered the external Memories timeline, achievement backfill, map-share duplicates, and the Stripe and pay-what-you-want sweeps. Batch 11 covered imported overlay URLs, overlapping pin refreshes, and slide-panel readiness after a cancelled provider. Batch 13 covered the Celery broker fallback, inherited task time limits, and the copied friend-invite checkboxes. Batch 14 covered the floorplan label embed, the overlay import cap, and friend-request error text. Batch 15 covered stored link archival, the social-link probe, and the Gotify POST. Batch 16 covered label creates that skip the case-insensitive conflict check, and trip-activity location inserts. Batch 17 covered the upload quota lock and the checksum check that sits outside it. Batch 18 covered Immich server fetches after the connect-time URL check. Batch 19 covered Gmail and Googlemail addresses comparing as different, and the per-request API key usage write. Batch 20 covered photo and comment scans whose enqueue failure is only retried hours later. Batch 21 covered SSO account creation skipping the email-taken check, and password reset not using the same email normalization as login. Batch 22 covered device-scan markers written from a client-supplied type and point, and uploads whose processing task is never retried. Batch 23 covered OAuth refresh tokens and API keys that keep working after a password change. Batch 24 covered fact rows created without an integrity retry, and confidence recomputes that can persist a stale snapshot with nothing scheduled to correct them. Batch 25 covered group chats that still deliver messages between profiles who have blocked each other. Batch 26 covered the unbounded trips list, descendant walks done in Python on request paths, calendar import and export fan-out, map-centre recomputes that fall back onto the request after seven days, health probes that plan migrations, and exports that hold every pin in memory. Batch 27 covered session rows, notification logs, device-scan readings, and pending visit suggestions that nothing deletes or bounds. Batch 28 covered public-pin suggestion fan-out, unlocked database dumps, the uncapped group inbox, and invitation email sent on the request. Batch 29 covered notification email sent on the request, the channel-layer buffer, profile creation races, and map payloads that count every pin. Batch 30 covered the reputation ledger, unbounded custom-field text, wiki edits with no row lock, and external API page offsets. Batch 31 covered geolocation visit checks on the request, sequential trivia generation, and saved-filter matches that load every pin uuid. Batch 32 covered photo-map sampling, push delivery, assistant call deadlines, and the visit-upload lock. Batch 33 covered album panels, dropped text-alert enqueues, article revision copies, and place autocomplete on the request. Batch 34 covered trip weather fan-out, the nearby-places layer, uncapped custom fields, and the safety overview. Batch 35 covered floorplan feature reads, the map shell's label and list payload, label-hierarchy edges, and historical-map browse. GPX dwell detection and the location-history and My Activity streams are only reached from `import_pins_streaming`, which P20 already records as unused outside tests, so that cost was not filed. `docs/PROBLEMS.md`
was searched for the claims that were about to be repeated, not read end to
end. Archive entries were not all re-checked for a fix that later regressed.
