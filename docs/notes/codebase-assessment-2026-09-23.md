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
  `UpstreamSlots`. Diff them against `P113` before filing.
- `services/pins/external_data.py` satellite/street-view completeness when a
  provider is cancelled.
- `services/memories/aggregator.py` when `limit` is `None`.
- `services/achievements/evaluate.py` backfill over every profile, and the
  Stripe / pay-what-you-want sweeps in `tasks.py` (around 4340, 4577, 4611).
- Global Celery time limit of 3600s with many tasks that set no tighter limit
  (`settings/base.py:344-345`).
- `CELERY_BROKER_URL` falling through to Dragonfly when RabbitMQ is unset
  (`settings/base.py` near the broker assignment). `D16` already says the
  broker should not share Dragonfly. Whether a deployment still hits the
  fallback was not checked.
- `DEBUG` defaulting on for `local` and `development`, and `csp_enforce`
  defaulting to report-only (`settings/base.py:57`, `857-862`;
  `settings/app.py` field default). `P56` is the COEP report-only entry, not
  this CSP default.
- `UL_SITE_URL` unset outside dev logging a warning and still emitting
  localhost links (`settings/base.py` near 1056).
- Login-time and HTMX paths that install document-level listeners without an
  install-once guard: `mention-autocomplete.ts`, `popup-dismiss.ts`,
  `organize-filter-engine.ts`.
- `_refreshAllPins()` call sites that do not coalesce in-flight fetches
  (`map-page.ts` around 3242 and 3563).
- Friend-invite checkbox UI copied across `spotguessr.ts`, `trivia.ts`, and
  `consensus.ts`.
- `import_data.py` overlay URL handling commented as diverging from the live
  upload path (around line 2215).
- OAuth avatar and UnifiedPush were verified above; other `requests.get` /
  `requests.post` sites that skip `fetch_public_url` were not enumerated.

## What this pass did not do

No pytest run, no load test, no browser pass, no migration-graph check, no
SCSS pass, no encryption-key review, no plugin-by-plugin read. `docs/PROBLEMS.md`
was searched for the claims that were about to be repeated, not read end to
end. Archive entries were not all re-checked for a fix that later regressed.
