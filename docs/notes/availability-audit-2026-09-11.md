# N21 — The availability audit of 2026-09-11, and what it found

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: N21` · `status: current` · `updated: 2026-09-11`

The requirement being audited is the standing one: *"No action a user takes should impact the
availability of the site for other users, ever."* D12 had just made the map payload cheap and
bounded; the question was whether the same shape existed elsewhere.

## Method, and why the first answer was not trusted

Sixteen sweeps, one per hazard dimension (unbounded request work, N+1 and object graphs, write
fan-out, Celery hygiene, connection and lock hazards, Valkey, outbound HTTP, Channels, media
parsing, superlinear Python, response memory, site-wide cost, pagination ceilings, throttling,
template and middleware cost, process and deploy). Each was told to report at most six findings,
each with a trigger, what the cost scales with, and which bounded shared resource it consumes.

A first verification pass **confirmed 100% of 91 claims**, which is not a good sign but a
diagnostic one: a verifier that refutes nothing has not been verifying. Two claims from a sibling
sweep of the same session were disproved by hand shortly afterwards — one asserted
`bin/run_perf_tests.sh` used a block-list of production hostnames when the script's own comment and
`case` statement implement an allow-list, and one asserted `MAP_DOCUMENT_MAX_PINS` did not exist
when it does, under its Django name rather than its `UL_`-prefixed environment name.

So the 58 critical-and-high findings were re-judged by hostile reviewers under three changes:
refute-by-default, a requirement to trace the URL route and permission check before calling
anything reachable, and — the one that mattered most — **the finder's quoted evidence was withheld**,
so a reviewer had to open the file themselves rather than grade the finder's own homework.

That pass returned 54 REAL and 4 OVERSTATED. The four downgrades each name a guard the finder had
missed, which is the shape of correction the first pass produced none of.

## The four downgraded, because the corrections are the useful part

- **H06** (critical → medium). The per-row visibility check does scan both profiles' pin tables,
  but two of the three cited trigger surfaces are already guarded: the wiki and pin photo galleries
  never reach `common_pins.py` at all, going through `ImageQuerySet.visible_to()` instead.
- **H08** (high → medium). The label bulk-edit id list is uncapped, but the analogy to
  `pin_bulk.py`'s `_MAX_BULK_PINS = 500` overstates it: that cap exists because each pin costs an
  individual save, which is a materially more expensive per-item mechanism.
- **H13** (high → medium). The finder quoted the real guards — tile-success and 404 caching, a
  per-service key — and then understated their effect on the `select_for_update` contention.
- **H15** (critical → medium). Accurate as written, but bounded by the actor's own accumulated
  history rather than unbounded per request.

## What the findings actually are

Five families, which matters more than the count: the fixes are per family, not per finding.

1. **Unauthenticated expensive endpoints with no throttle** (6 findings, anonymous-reachable).
   Signup and `/demo/start/` spend PBKDF2 CPU per request — ~1.1s each, measured — with no rate
   limit; `/resend-verification/` and the password reset send SMTP synchronously; `/costs/` runs
   uncached full-table aggregates for anonymous callers. There is **no inbound request throttle
   anywhere in the web tier**: the rich throttling in `external_api/throttling.py` is DRF-only, and
   `services/core/rate_limiter.py` governs *outbound* third-party spend, not inbound load.
2. **One account's work fills the only Celery queue.** 87 of 95 `@shared_task` definitions carry no
   `queue=`, so they all land on the single default queue at `--concurrency=4`. An Immich library
   sweep can hold a slot for up to the 45-minute soft limit, and nothing stops a user pressing the
   button four times. This is P109's mechanism, generalised.
3. **Unbounded writes into the shared 512MB Valkey**, which holds sessions, the Channels layer and
   the Celery broker alongside the cache under `volatile-lru` — so filling it evicts other users'
   sessions. Google Photos previews at 2048px, Immich thumbnails and map tiles are all cached with
   no size or count ceiling.
4. **Request-path loops over one account's data with no ceiling**, the map payload's own shape,
   still present in the photo-map JSON endpoints, the memories feed, bulk merge and region dissolve.
5. **No per-role database limits and no `statement_timeout`**, so any of the above holds a Postgres
   backend and a gunicorn greenlet for as long as it takes. This is D11 phase 3, designed and not
   started.

Families 2 and 5 are **already designed** — D11's per-role connection budget and PL7 phase 6's
Celery queue classes. The audit's main result is not a surprise: it is 54 measured instances of
three families the programme already has a plan for, which is an argument for doing those phases
rather than for 54 individual patches.

## The findings

`reachable_by` is the field that ranks them: an anonymous-reachable hazard needs no account, so it
has no cost to an attacker at all.

| ref | severity | reachable by | file | what it is | smallest fix |
|---|---|---|---|---|---|
| H01 | critical | any-authenticated | `src/urbanlens/dashboard/tasks.py:2231` | The Immich "Scan your library" button enqueues an uncapped, un-deduplicated whole-library sweep that holds one of only four default-queue worker slots for up to 45 minutes while issuing one PostGIS query per photo. |  |
| H02 | critical | any-authenticated | `src/urbanlens/dashboard/controllers/google_photos.py:290` | The Google Photos thumbnail proxy stores a full 2048px preview of every picked photo in the shared 512MB Valkey for an hour, and the number of previews is whatever the user selected in the picker. |  |
| H03 — fixed 2026-09-11 (bounded by `_MAX_BULK_PINS`) | critical | own-data-only | `src/urbanlens/dashboard/controllers/pin_bulk.py:165` | PinBulkMergeView accepts an unbounded list of source pins and re-saves each one individually, and every save re-fits the target's child boundary by convex-hulling the target's entire (growing) child set — quadratic GEOS work plus the full 8 |  |
| H07 | critical | any-authenticated | `src/urbanlens/dashboard/controllers/image_gallery.py:205` | The pin and wiki photo-map JSON endpoints serialize every geo-tagged photo on the pin/wiki with no ceiling, and the page fetches them automatically on load. | Apply the same _GALLERY_PAGE_SIZE-based get_page()/slice to both JSON views that the HTML galleries already use, or cap with a fixed [:N] the way pin_lists.py/saved_filters.py cap their own  |
| H10 | critical | any-authenticated | `src/urbanlens/config/nginx/nginx.conf:7` | Nothing anywhere caps how many WebSocket connections one account may hold open, and every socket on the site is proxied by one nginx (2 workers x 1024 connections, shared with all HTTP) into one daphne process. | Add a per-profile (and/or per-IP) concurrent-connection cap in consumers.py's connect() methods, refusing beyond some ceiling with a close code, plus an nginx limit_conn zone scoped to /ws/  |
| H12 | critical | any-authenticated | `src/urbanlens/dashboard/services/geo/geo.py:113` | `dissolve_polygons` runs an all-pairs GEOS `intersects()` scan over however many polygons the client puts in one region payload, inside the saved-filter create/edit/preview request. |  |
| H14 | critical | anonymous | `src/urbanlens/dashboard/controllers/pin.py:2175` | Four unauthenticated REData media proxies download unbounded third-party bytes inside the request and store them raw in the shared 512MB Valkey for an hour, with a fully client-controlled cache key. |  |
| H16 | critical | any-authenticated | `src/urbanlens/dashboard/services/wiki/wiki_access.py:119` | Every wiki/photo access check re-reads the whole site-wide Place aggregate table, so each media-file fetch costs a full-table scan that grows with total site data | Cache visible_wiki_location_ids (or accessible_domain_ids) in Django's cache framework keyed by profile id with a short TTL and explicit invalidation on pin/grant changes, rather than only p |
| H17 — fixed 2026-09-11 (throttle) | critical | anonymous | `src/urbanlens/dashboard/controllers/account.py:434` | POST /signup/ has no rate limit of any kind and spends ~1.1s of PBKDF2 CPU per request, which blocks an entire gevent worker process (all 20 greenlets) for the duration. | Add a per-IP (and/or global) rate limit or lockout counter to SignupView.post/dispatch, mirroring the existing login lockout machinery (_bump_counter/_is_locked_out in account.py), before fo |
| H19 | critical | any-authenticated | `src/urbanlens/dashboard/services/media/media_materialize.py:275` | Voting an external photo "relevant" downloads up to 20 MB of client-chosen bytes into the shared media volume with the storage quota explicitly disabled, so one user can fill the filesystem Postgres lives on. | Apply a per-profile rate limit (request count and/or cumulative bytes per window) to WikiMediaVoteView's materialize branch, or bring externally-materialized media back under a (separately-t |
| H04 | high | own-data-only | `src/urbanlens/dashboard/controllers/memories.py:459` | The Memories feed endpoint accepts an arbitrary client date window and instantiates the profile's entire history as model objects in one request. |  |
| H09 | high | own-data-only | `src/urbanlens/dashboard/services/apis/immich/nearby.py:102` | The Immich photo picker downloads the requesting user's entire geolocated Immich library inside the request, then caps it to 500 afterwards - and re-downloads it per pin. | Nothing changes the underlying Immich API limitation (no server-side radius filter, per the module's own docstring), but the fetch/sort/measure work could move off the request thread (Celery |
| H11 | high | any-authenticated | `src/urbanlens/dashboard/models/pin/queryset.py:255` | A search's `label_groups` parameter turns each submitted label id into both a sequential descendant-BFS query and an extra M2M JOIN, with no cap on how many ids the client sends. |  |
| H18 | high | any-authenticated | `src/urbanlens/dashboard/services/comments/comments.py:269` | The comment-count badge fetches and re-renders the full text of every comment containing an @loc marker, on every pin and wiki page load, with no limit | Cap the mentioning-comment scan with a reasonable ceiling (e.g. only check the first N mentioning comments, or push the @loc-visibility filter into SQL/materialized-uuid columns) so the coun |
| H20 | high | any-authenticated | `src/urbanlens/dashboard/controllers/tools.py:155` | "Export my data" has no in-flight guard, so each press copies every photo the account owns to disk twice and occupies a default-queue worker slot; N presses cost N full copies of the account. | Check for an existing pending/running export job for request.user (e.g. via ExportJobStatus or a cache-based lock) in ExportStartView.post and reject or reuse it instead of always enqueuing  |
| H21 | high | any-authenticated | `src/urbanlens/dashboard/tasks.py:2649` | resolve_deferred_pin_locations carries the client's entire untruncated pin payload as a Celery argument through Valkey, and re-publishes the still-pending remainder on every retry for up to two days. |  |
| H24 | high | any-authenticated | `src/urbanlens/dashboard/controllers/immich.py:295` | The Immich thumbnail proxies buffer an entire response from a user-supplied server and write it into the shared cache with no size check, so one request can occupy or evict the whole 512MB instance. |  |
| H25 | high | any-authenticated | `src/urbanlens/dashboard/models/aliases/signals.py:98` | Adding one alias to a community wiki writes a PinAlias row into every other profile's pin at that location, synchronously in the request, and each of those writes fires two more receivers. |  |
| H26 | high | any-authenticated | `src/urbanlens/dashboard/controllers/labels.py:1089` | LabelBulkEditView caps neither the label id list nor the proposed-parent list, then runs a database-walking cycle check for every (label, parent) pair and a full account-wide pin UPDATE for every label saved. | Cap ids/add_parent_ids/add_child_ids length in _parse_ids_json/_parse_bulk_payload (mirroring _MAX_BULK_PINS=500 in pin_bulk.py:52), and/or batch the cycle checks with a single bulk ancestor |
| H27 | high | any-authenticated | `src/urbanlens/dashboard/services/pins/pin_list_membership.py:74` | Every Pin.save() re-evaluates the pin against every smart PinList its owner has created, with no cap on how many smart lists a profile may own and a fresh criteria-deserialisation plus filter query per list. | Cap the number of active smart lists per profile (SiteSettings setting, enforced at PinList create/is_smart-toggle time), and/or batch smart-list evaluation across the whole bulk-edit reques |
| H28 | high | any-authenticated | `src/urbanlens/dashboard/controllers/image_gallery.py:532` | The wiki photo-map JSON loops over every geotagged photo on a community wiki with no limit, resolving each uploader's identity through the same uncached full-pin-table privacy chain. | Paginate WikiGalleryJsonView identically to WikiGalleryView, and memoize per-uploader visibility results within one request (a dict keyed by uploader profile id) instead of recomputing `can_ |
| H29 | high | own-data-only | `src/urbanlens/dashboard/controllers/pin_lists.py:248` | Organize's Lists panel prefetches every item and every pin on every one of the profile's lists purely to print a pin count on each card. | Replace the prefetch-based pin_count with an annotated COUNT(*) (e.g. `Count('items', distinct=True)`) computed in one aggregate query per the whole list of PinLists, or paginate the lists p |
| H30 | high | any-authenticated | `src/urbanlens/dashboard/services/pins/common_pins.py:77` | Viewing another user's profile runs one extra Postgres query per place the two accounts share, on top of two whole-account pin scans. | Check `can_view_common_pins_with` before running `common_pin_location_ids` at all (skip the whole computation when the gate would hide the result anyway), and replace the per-place-id `.firs |
| H31 | high | own-data-only | `src/urbanlens/dashboard/controllers/memories.py:1067` | /memories/maps/ serializes every markup map the account has ever drawn, and every annotation inside each of them, into one un-paginated HTML response. |  |
| H32 | high | any-authenticated | `src/urbanlens/dashboard/controllers/albums.py:366` | Opening one album loads every photo in it as a full model instance and builds a dict per photo for the album map, even though the grid beside it is paginated at 48. |  |
| H33 | high | own-data-only | `src/urbanlens/dashboard/external_api/views_labels_bulk.py:181` | External-API label bulk edit runs an O(labels x parents) cycle check - one DB query per graph node - inside a single transaction.atomic(), with no cap on the parent/child lists |  |
| H34 | high | any-authenticated | `src/urbanlens/dashboard/services/core/rate_limiter.py:572` | External-API budgets are enforced app-wide with no per-user dimension, so one user's ordinary use exhausts a feature for every other user - sharpest for the historical-map tile proxy, which silently inherits the 20/min, 500/day default mean |  |
| H35 | high | any-authenticated | `src/urbanlens/dashboard/controllers/basemap_tiles.py:172` | Both tile proxies write raw tile bytes into the shared 512MB Valkey with 1-7 day TTLs and no size or count ceiling, so one user panning the map can evict the instance that also holds sessions, the Channels layer and the Celery broker. |  |
| H36 | high | any-authenticated | `src/urbanlens/dashboard/controllers/flickr.py:216` | The Flickr picker's "visits" mode issues up to 15 sequential 30-second Flickr calls inside one GET, with no wall-clock deadline, and a rate-limit refusal escapes the handler as a 500. | Wrap the `search_by_dates`/`search_near`/`list_recent` calls in `services.core.timeout_utils.call_with_deadline` as pin.py already does for its own external-call fan-outs, and add `except (G |
| H37 | high | any-authenticated | `src/urbanlens/dashboard/controllers/memories.py:1087` | GET /memories/maps/ embeds every markup map the profile owns, with every annotation's full geometry, in one HTML response | Paginate `MemoriesMapsView`'s queryset (mirroring pin_lists.py's existing pagination helper) and/or cap `latlngs` length in `_sanitize_latlngs` and cap MarkupMaps-per-profile / items-per-map |
| H38 | high | any-authenticated | `src/urbanlens/dashboard/controllers/immich.py:295` | A user-supplied Immich server's response is read whole with response.content and written into the shared 512MB Valkey that also holds sessions, Channels and the Celery broker | Check `Content-Length`/stream with a max-bytes cap in `_get_binary` (or specifically in `get_asset_thumbnail`) and refuse to cache (or truncate/reject) anything over a sane thumbnail size be |
| H39 | high | any-authenticated | `src/urbanlens/dashboard/controllers/markup.py:309` | The markup JSON endpoint returns every annotation in a whole pin/wiki subtree in one response, with per-item geometry that has no size limit | Cap subtree markup count (paginate or hard-limit rows per response) and cap point count per geometry at the sanitize-on-write layer (`_sanitize_latlngs`), matching the cap pattern already us |
| H40 | high | any-authenticated | `src/urbanlens/dashboard/websocket_auth.py:100` | Every `?key=` WebSocket connect runs a 1.2M-iteration PBKDF2 on asgiref's process-global single-thread executor - the one thread that runs ALL database work for every WebSocket connection in the daphne process. | Rate-limit or debounce WebSocket `?key=` connect attempts per token/IP (e.g. via the existing rate_limiter.py machinery or a short-TTL cache of recently-verified prefixes) so a reconnect sto |
| H41 | high | any-authenticated | `src/urbanlens/dashboard/services/messaging/group_chats.py:793` | One group-chat message fans out to one Celery task per member (up to 50), each carrying the full body/ciphertext and each spawning a new OS thread, a new event loop and a new Valkey connection. | Batch the fan-out into a single Celery task per message that iterates members inside one worker/event-loop/connection, or route it onto its own queue away from safety-critical tasks; also co |
| H42 | high | any-authenticated | `src/urbanlens/dashboard/controllers/search.py:110` | The web global-search endpoint accepts an unbounded `q` and has no throttle, while the API surface of the same engine caps it at 250 characters specifically to keep a long needle out of the per-row trigram similarity. | Add the same MAX_QUERY_LENGTH=250 cap (or similar) and a throttle to GlobalSearchPanelView.get, matching the API surface of the same engine. |
| H43 | high | any-authenticated | `src/urbanlens/dashboard/services/trips/trip_comments.py:164` | `build_comment_tree` loads every trip comment and reply unpaginated and re-evaluates `can_view_comments_from` per row, which at the default `ANYTHING_IN_COMMON` pulls every pin of both profiles into Python each time. | Paginate top_comments (and replies), and memoize can_view_comments_from per (viewer, author) pair for the duration of one build_comment_tree call - a simple dict keyed on author.pk covers th |
| H44 | high | any-authenticated | `src/urbanlens/dashboard/services/media/access.py:207` | Every /media/pin_images/... request for a photo the viewer does not own re-derives the viewer's whole wiki reach — three full scans of their Pin table plus a site-wide aggregate-Place scan — so a large account makes each gallery tile expens | Call prime_viewer_scope(profile) once per authorize_media/authorize_image call (it only helps within one call unless the caller batches multiple images, so the real fix is either batching ga |
| H45 | high | any-authenticated | `src/urbanlens/dashboard/services/profile/avatar.py:346` | Avatar upload runs a synchronous ClamAV scan in the request: the whole file (up to the 250MB site cap) is copied into a BytesIO inside the gunicorn worker and pushed to the shared clamd daemon in 1KB chunks. | Move the malware scan off the request path for avatars the same way comment images already do (skip_malware_scan=True + async scan via a SANDBOX_QUEUE Celery task, holding the avatar in a pe |
| H46 | high | own-data-only | `src/urbanlens/dashboard/external_api/views_pin_article.py:235` | Article revision-history API endpoints load every revision's full 200,000-character body into memory to compute size deltas, then paginate the resulting list. | Compute size_delta via a DB-side window function (LAG over ordered rows) or defer `content` with `.only(...)` for the list view and paginate the queryset itself (Django slice -> SQL LIMIT/OF |
| H47 | high | any-authenticated | `src/urbanlens/dashboard/external_api/views_wiki.py:1128` | The external comment endpoints materialize a pin's or wiki's entire comment tree — replies, reactions, markup maps, and a per-author permission check — before paginating the built list. | Page the queryset (DB-level LIMIT via top_level_comment_queryset(comments_qs)[start:end]) before calling visible_comment_tree, and cache/batch can_view_comments_from per author across a requ |
| H48 | high | anonymous | `src/urbanlens/dashboard/controllers/costs.py:50` | The public /costs/ page runs uncached full-table aggregates over the 400-day API call log and over every user's pins, for anonymous callers | Wrap CostsView.get in `cache_page` for a few minutes (the figures are inherently trailing-30-day/monthly aggregates, not real-time), or memoize the breakdown/series in Django's cache keyed o |
| H49 | high | any-authenticated | `src/urbanlens/dashboard/controllers/search.py:110` | GET /dashboard/search/panel/ is fired per keystroke with no throttle and fans one query out to ~11 providers, each running an un-indexable trigram-similarity scan over the account's rows — then runs the whole fan-out a second time whenever | Add a per-profile server-side throttle on GlobalSearchPanelView (a simple cache-based rate limiter, matching the pattern already used for external-API throttles), and add GinIndex(fields=[.. |
| H50 — fixed 2026-09-11 (throttle) | high | anonymous | `src/urbanlens/dashboard/controllers/demo.py:59` | POST /demo/start/ is unauthenticated and unthrottled, and each request creates 5 users (5 x PBKDF2 ~= 5.5s of blocking CPU) plus hundreds of content rows inside a single transaction. | Add an IP- or session-based rate limit (django-ratelimit or a cache-based counter) to DemoLoginView.post, and/or move the 5 PBKDF2 hashes off the request path (e.g. a cheap hasher for demo a |
| H51 — fixed 2026-09-12 (throttle + `EMAIL_TIMEOUT`) | high | anonymous | `src/urbanlens/dashboard/controllers/account.py:568` | POST /resend-verification/ and POST /accounts/password_reset/ are unauthenticated, unthrottled, and each performs a synchronous SMTP send with no EMAIL_TIMEOUT configured, so a slow or hung mail server pins greenlets and their Postgres conn |  |
| H52 | high | own-data-only | `src/urbanlens/dashboard/services/trips/trip_comments.py:132` | The trip comments panel renders every comment and reply with no pagination, and re-runs the author-visibility relationship queries per comment - each of which loads both profiles' entire pin sets |  |
| H53 | high | own-data-only | `src/urbanlens/dashboard/controllers/albums.py:366` | Album detail embeds a JSON entry for every photo in the album inline in the page, while the photo grid itself is paginated at 48 |  |
| H54 | high | own-data-only | `docker-compose.yml:575` | One 512MB Valkey holds sessions, the Channels layer, the Django cache and the Celery broker in a single keyspace under volatile-lru, and the Celery broker's keys are the only ones with no TTL - so a user-driven task backlog is paid for by e |  |
| H55 | high | anonymous | `src/urbanlens/config/nginx/nginx.conf:12` | nginx spools up to 200MB of every request body to disk in the container's writable layer before Django sees the request, so unauthenticated POST volume consumes the same host filesystem that holds the Postgres data directory. |  |
| H56 | high | any-authenticated | `package.json:16` | A request that spends >180 s in non-yielding Python/C CPU causes gunicorn to abort the whole gevent worker process, killing up to 19 other users' in-flight requests with it. |  |
| H57 | high | any-authenticated | `src/urbanlens/dashboard/services/core/rate_limiter.py:584` | The outbound-API rate limiter's own daily-window check is a non-indexable count over a 400-day append-only log, executed on every external call while holding that service's global row lock. |  |
| H58 | high | own-data-only | `src/urbanlens/dashboard/services/undo/handlers/pin.py:142` | Undoing a bulk pin delete recreates the whole stashed subtree one row at a time — 5 pre-flight queries per pin, then a full `Pin.objects.create()` cascade per pin — inside a single transaction holding the undo row's lock. |  |
| H05 | medium | any-authenticated | `src/urbanlens/dashboard/controllers/direct_messages.py:864` | The message-recipient and group-member autocompletes run a full privacy evaluation per candidate, and each evaluation reads two accounts' entire Pin tables into Python sets - on every keystroke. |  |
| H06 | medium | any-authenticated | `src/urbanlens/dashboard/services/pins/common_pins.py:44` | Every per-row "may this viewer see that person" check loads both profiles' entire Pin tables into Python, and it is called once per photo/comment rendered. | Give trip_comments.build_comment_tree the same per-author `can_view` memoization comments.py already has, and paginate `trip.comments.filter(parent__isnull=True)` the way the pin/wiki panel  |
| H08 | medium | own-data-only | `src/urbanlens/dashboard/controllers/labels.py:1089` | Label bulk-edit/bulk-delete accept an unbounded id list, and every label saved fires a receiver that UPDATEs every pin carrying it, so one click can rewrite millions of Pin rows inside one request | Add a cap on the ids list (mirroring pin_bulk.py's _MAX_BULK_PINS / _too_many pattern) so a pathological personal label+pin collection can't turn one click into an unbounded number of large  |
| H13 | medium | any-authenticated | `src/urbanlens/dashboard/controllers/basemap_tiles.py:165` | Every basemap/historical map-tile miss serializes on one global SELECT FOR UPDATE row, spends a site-wide 600/min budget, and writes the tile bytes into the shared 512MB Valkey for 7 days — so one user panning the map can blank the map and |  |
| H15 | medium | any-authenticated | `src/urbanlens/dashboard/controllers/memories.py:463` | The Memories feed endpoint has no pagination and no ceiling on its date window, and the page ships an "All time" button that asks for the account's entire history in one response. |  |
| H22 | medium | any-authenticated | `src/urbanlens/dashboard/controllers/basemap_tiles.py:172` | The REData basemap tile proxy caches raw tile bytes in the shared Valkey for seven days, one key per z/x/y a user pans over, with no size or key-count ceiling. |  |
| H23 | medium | any-authenticated | `src/urbanlens/dashboard/services/search/saved_filter_cache.py:91` | Every login and every map/toolbar load writes a full list of a profile's matching pin UUIDs into Valkey under a fingerprint-versioned key, and the superseded copy is never deleted - it lingers for a day. |  |
## What was fixed in the session that found these

Family 1, and the instruments. The other four families are recorded, not fixed — see P113.

**Family 1: the inbound throttle the web tier never had.** `services/security/throttle.py`, applied
to `signup`, `password_reset`, `resend_verification` and `demo.start`. Two of the six findings in
that family dissolved on contact: `/accounts/login/` already has per-identifier and per-IP failure
lockouts, and `/demo/start/` only exists when `demo_mode` is on. The remaining two — the REData
media proxies and nginx's 200MB request-body spooling — are infrastructure rather than application
code and are left with the rest.

Three defects in the safety net itself were repaired, because a finding list is worth nothing if the
gates that should have caught them cannot fail:

- `run_perf_tests.sh` could not fail on connection exhaustion, which is P104's exact mechanism.
  `report_activity.py` returned zero whatever it found and the runner called it with `|| true`, and
  nothing grepped the database log for `53300` although PL7 says both are hard failures.
- `test_map_document_cap.py`'s three `xfail(strict=True)` reproductions could never turn red: the
  first asserted `hasattr(settings, "UL_MAP_DOCUMENT_MAX_PINS")`, the environment variable's name
  rather than the Django setting's, so it went on xfailing after D12's ceiling landed.
- `EndpointScalingMixin` read `len(response.content)`, which raises on a `StreamingHttpResponse`, so
  the repo's most complete instrument was structurally unable to measure `map.document`, the largest
  response the application serves.

### H51 named two things, and the throttle only reached one (2026-09-12)

The entry reads "unauthenticated, unthrottled, and each performs a synchronous SMTP send with no
`EMAIL_TIMEOUT` configured". Family 1 answered *unthrottled*. It did not answer the rest, and the
two are independent: a rate limit bounds how often mail is sent, a timeout bounds how long one send
may hold the worker. With `EMAIL_TIMEOUT` unset Django passes no `timeout` to `smtplib` at all, so
the socket had no deadline of any kind — ten permitted calls per address against a hung mail server
is ten workers held until it answers. Now `app.email_timeout`, default 10s. See P113 for the two
things that fix deliberately does not do.

The general lesson, since it is the second time this shape has appeared in this effort: **a finding
that names two properties needs two fixes, and closing one of them makes the entry look closed.**
Re-read the finding text against the fix, not the finding's ID.

### `test_oauth_client_provisioning` fails under `--reuse-db`, and is not a real failure (2026-09-12)

`FirstPartyClientMigrationTests` (4 tests) fails locally with `--reuse-db` and passes in CI and on a
fresh database — verified, not assumed: 11 passed against a freshly-migrated `UL_TEST_DB_NAME`. The
tests assert on rows a data migration creates, and `--reuse-db` skips migrations on an existing test
database, so the rows are absent. Nothing is wrong with the code.

Recorded because the shape is a time sink: it looks exactly like a real regression, and it only
appears on the flag you reach for to make iteration fast. If these four fail, re-run that one file
without `--reuse-db` before investigating anything.

The opposite trap is in P113, and cost far more: the twelve WebSocket `TimeoutError`s *also* looked
environmental and were not. "It is the test environment" is worth verifying rather than concluding —
in both directions.

### H17, H50 and H03 were already closed when re-checked (2026-09-12)

H17 (`signup` PBKDF2) and H50 (`demo.start`, five hashes) were closed by family 1 in `ab22f74be` the
day before — they are this effort's own work, not audit error. H03's quadratic merge is bounded by
`_MAX_BULK_PINS = 500` in `pin_bulk.py`, which caps the input the hull refit iterates; the residual
O(N²) inside 500 is GEOS work on at most 500 points and is not worth restructuring the save loop for.
Verify against the code before starting a finding: this is now eleven of them.
