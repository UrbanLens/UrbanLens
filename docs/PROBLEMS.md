# PROBLEMS

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

Defects found during other work and left unfixed at the time. Every entry here
is still open, still partial, or still worth knowing before touching the area it
describes. Resolved ones live in [`archive/PROBLEMS-ARCHIVE.md`](archive/PROBLEMS-ARCHIVE.md);
search there before concluding a defect is new.

Each entry carries a `P#` id, allocated from [`INDEX.md`](INDEX.md) and never
reused. `grep -E '^\| P12 ' docs/INDEX.md` finds one; `grep '^## P12 ' docs/PROBLEMS.md`
opens it.

**Citing an entry from code:** name the id *and* enough words to survive a
retitle — `see P12 ("forms post every field") in docs/PROBLEMS.md`. A bare
`see docs/PROBLEMS.md` costs the reader a full-text search of a 3,500-line file,
and in practice they do not do it. Roughly half of them carry no date, quoted
phrase or symbol to land on — 48 of 93 on 2026-09-05, counting lines in `src/`
that name `PROBLEMS.md` against whether the three lines around them hold a `P#`,
a date, or a backticked identifier. The ratio is the point; the totals move with
every commit. Cite **every** relevant entry, not the nearest one.

Ids and dates are stable here; line numbers are not. A date-anchored citation
still works, but check `archive/PROBLEMS-ARCHIVE.md` too - an entry that was
open when it was cited may since have been resolved and moved there.

## P1 — VirusTotal scanning is hash-lookup-only, so a file VirusTotal has never seen falls back to ClamAV forever

`id: P1` · `status: open` · `updated: 2026-09-01`

Previously titled "VirusTotal fast-path scanning is hash-lookup-only, never submits an unknown file".

`services/security/virustotal_scan.py` (see also `services/apis/security/virustotal.py`) only calls VirusTotal's
`GET /files/{sha256}` hash-lookup endpoint before falling back to ClamAV - it never calls `POST /files` to submit a
file VirusTotal has never seen. Deliberate scope decision, not an oversight: the upload endpoint returns no immediate
verdict (an uploaded file has to be polled separately via `GET /analyses/{id}` until analysis completes, which can take
some time), so submitting a never-before-seen file buys nothing for the scan that triggered it - by the time an
analysis would complete, this scan has already fallen back to ClamAV. The consequence: a public asset that VirusTotal's
own crawlers haven't already indexed independently (Wikimedia/Smithsonian/LOC/etc. content plausibly often has been;
something more obscure plausibly hasn't) will keep going through ClamAV on every fetch, forever, rather than eventually
warming VirusTotal's cache for future lookups. If the actual ClamAV load reduction from lookup-only turns out to be
smaller than hoped, the next step is submit-and-don't-wait (fire the upload so a *future* lookup of the same hash can
hit, without blocking or polling for the current one) - deferred rather than built speculatively, since it adds a
32MB size gate and competes with the same daily quota the lookups use for no benefit to the request that pays for it.

## P2 — `parse_for_preview` parses archives and KML in the request, blocking `UL_UNTRUSTED_PARSE_POLICY=deny`

`id: P2` · `status: open` · `updated: 2026-08-31`

Previously titled "`prepare_photo_upload` no longer decodes in the request".

Was: `prepare_photo_upload` called the `extract_*` helpers (Pillow decode) inside the upload
request for every one of its ~9 call sites, so a crafted upload's header parser ran in gunicorn
before the sandbox tier existed to catch it.

Fix: `prepare_photo_upload` now stores the raw upload untouched and returns
`{"pending_scan": True}` as its `metadata` - every caller already splats that dict into
`Image.objects.create(...)`, so all ~9 call sites picked it up with no change of their own.
`Image.pending_scan` (migration `0032_v0_8_0`, which the v0.8.0 squash folded 0038 into) gates `services/media/access.py::authorize_image` and
`ImageQuerySet.visible_to` the same way `Comment.pending_scan` already gated comment images: the
uploader always sees their own row; nobody else can read or list it until
`tasks.process_image_upload` has read its EXIF and downscaled it, which is also what now clears
the flag. A stored file that cannot even be opened retries a few times (a genuine storage hiccup
is worth that), and only once retries are exhausted is the row deleted outright
(`tasks._reject_image_upload`) rather than cleared-and-served - an adversarial review of this
batch caught an earlier version of this fallback clearing `pending_scan` on the very first
attempt (a comment mis-stated that Celery's own retries had already run; they hadn't, since
`_process_photo_upload` swallows the exception internally instead of letting `autoretry_for`
see it), which would have served the raw, never-validated file to the uploader's whole sharing
audience on a single transient failure - precisely the leak this mechanism exists to prevent.

`services/media/metadata_strip.py`'s byte-walk stripper is now unused in the live pipeline (its
one caller was inside `prepare_photo_upload`) - `pending_scan` closes the same "raw file is
briefly servable" window through access control instead. Left in place: it has its own tests, is
decode-free so it stays outside the sandbox boundary if some future in-request use wants it, and
removing a working, harmless module is a separate decision from this one.

Four things blocked `UL_UNTRUSTED_PARSE_POLICY=deny`. Three are now closed; the first is not.

1. **`controllers/pin.parse_for_preview` - STILL OPEN, and now the only blocker.** It runs
   `extract_archive` (zipfile/tarfile), `GoogleMapsGateway.parse_for_preview`
   (fastkml/lxml/gpxpy/GDAL/Shapely) and `extract_text` (python-docx) in the request path. All
   of those are decorated now, so `deny` *would* stop them - which is exactly why flipping the
   policy still breaks this endpoint.

   It resisted the treatment the other three got, and the reason is worth writing down:
   `GoogleMapsGateway.parse_for_preview` is **not a pure parse**. Its CSV branch
   (`_csv_row_iter`) geocodes, and `_preview_pins` resolves places - both outbound calls, in a
   container that deliberately has neither internet nor API keys. Moving the method wholesale
   into the sandbox would break CSV import; moving only the file parsers means splitting parse
   from resolve inside the gateway first. `extract_pins_from_document` has the same shape one
   level up - its parse half (`extract_text`) can be sandboxed, its AI half cannot - which is
   why the decorator sits on `extract_text` rather than on the outer function.

   Fix shape: split the gateway's per-format parsing (pure, sandboxable) from its
   geocode/place-resolution pass (needs the network), route the first half to a sandbox task
   with the uploaded bytes staged in the cache rather than the broker (the pattern
   `previews.request_sandbox_render` now establishes), and do the second half in the web
   process on the parsed dicts. The frontend already shows a "Reading files..." step, so a
   longer round-trip needs no UI change.
2. ~~**`render_preview` has two request-path callers**~~ - fixed, then reworked once more:
   the first fix blocked on the result via a helper that does not exist in the final code.
   `tasks.render_media_preview` does the decode on the sandbox queue; both callers
   (`media_preview.MediaPreviewView`, `pin.RedataMediaProxyMixin`) call
   `previews.request_sandbox_render()`, which is deliberately fire-and-forget - `cache.add()`
   gates the enqueue to once per key, the source travels through the cache/media volume rather
   than a 60MB broker message, and the caller returns 404 immediately instead of waiting. A
   blocking wait would hang every test run (`CELERY_TASK_ALWAYS_EAGER` is off by default in
   test settings) and pin a web worker per tile for as long as the sandbox is behind - twenty
   tiles on one gallery page, times the wait, whenever `media-worker` is down. Self-healing
   instead: the frontend's `urbanlensMediaThumbFallback` retries the same URL twice (2s, 4s)
   before falling back to the icon tile, and a slow first render still warms the cache for
   that retry or the next page load.
3. ~~**`manage.py strip_exif_from_stored_photos`**~~ - fixed, with the exemption written down
   rather than implied: an `allow_untrusted_parse` block scoped to the loop, whose reason string
   says why it is legitimate (already-stored, already-scanned files; a scrub, not an ingest).
   This is `allow_untrusted_parse`'s first production call site.
4. ~~**`services/photos/photo_enrichment.py`**~~ - fixed, and *not* with an exemption, which is
   what an earlier version of this entry proposed. An exemption would have left a Pillow decode
   of provider bytes running in the container that holds every third-party API key - the exact
   process the sandbox tier exists to keep decoders out of. `_save_enriched_image` now creates
   the row `pending_scan=True` and enqueues `process_image_upload`, which needed one new
   parameter: these rows are profile-less, so the task had no subscriber plan to read a
   downscale policy from and had been skipping them entirely.

Also closed alongside these: every parser that had no decorator now has one - `_extract_zip`/
`_extract_tgz`, `takeout_kml_to_dict`, `gpx_to_dict`, `gpx_tracks_to_routes`, `osm_xml_to_dict`,
`wkt_to_dict`/`wkb_to_dict`, `shapefile_to_dict`, `extract_text`. `warn` therefore logs a
complete worklist now rather than a partial one, which it did not before: an undecorated parser
is invisible to the guard, so "no warnings" meant "nothing decorated is misplaced", not "nothing
is misplaced".

## P3 — The pin-detail hero no longer links to `PinRelinkView.get`, orphaning the `pin.link` wiki picker

`id: P3` · `status: open` · `updated: 2026-08-31`

Previously titled "the pin-detail "switch wiki" GET picker is now UI-orphaned".

Replacing the pin-detail hero's single-wiki-plus-switch-button with a list of every linked wiki
(`_pin_detail_hero_body.html`, `services.places.ambiguity.linked_wiki_locations`) removed the
hero's only trigger for `PinRelinkView.get()` (the `hx-get="{% url 'pin.link' %}"` button that
swapped `pin_location_picker.html` into `#pin-location-picker`). The route, view, and partial are
untouched and still reachable directly (and `PinRelinkView.post` / `pin.link.to` is still wired
from `location/wiki.html`'s "other properties this location falls inside" list), but nothing in
the UI links to the GET picker anymore.

Left as-is rather than removed: a separate, not-yet-actioned note already flags this whole
"switch"/"detach" pair as likely deprecated and worth a dedicated look (including that "switch"
was surfacing inappropriate suggestions, e.g. a building's own parent parcel) - resolving that
should also decide this route's fate rather than deleting it unilaterally here.

## P5 — Dialog forms post every field and handlers save every column, so untouched values overwrite and re-attribute

`id: P5` · `status: open` · `updated: 2026-08-25`

Previously titled "forms submit and save every field, not the ones that changed".

Surfaced by the concealment work, where it caused real data loss, but the concealment case is one
instance of a general pattern and fixing that instance did not fix the pattern.

The shape: a dialog is prefilled from the current record, `new FormData(this)` serialises **every**
field on submit, and the handler writes all of them back. Nothing distinguishes "the user set this
value" from "this value was already there and the form carried it along".

Why it is worth auditing before offline access and merging land, which is the point at which it
stops being cosmetic:

- **Overwrites.** Two people editing different fields of the same record round-trip each other's
  values. Last writer wins on fields they never looked at. Today this is mostly masked because
  `apply_wiki_edit` and friends re-diff server-side; anything that saves the payload directly does
  not.
- **Merge noise.** A field-level or CRDT-style merge cannot tell an unchanged carried-along value
  from a deliberate re-assertion of the same value, so every save becomes a conflict candidate on
  every field. Offline clients replaying a queue of these generate merges with no content in them.
- **Modified times.** `updated` moves on records nothing changed, which corrupts recency ordering,
  "what changed since" sync cursors, and any notification keyed on a record having been touched.
- **Field provenance.** `models/abstract/versioned.py` records a write per field per save. A form
  re-asserting fourteen fields records fourteen writes and re-attributes all of them to the
  submitter - which is the re-attribution leak already fixed once inside `VersionedModel.save` by
  diffing against a `from_db` snapshot. That snapshot defends the substrate; it defends nothing
  that writes through another path.

The concealment instance, for concreteness: the suggest-edits dialog is prefilled from the
concealed projection and posts all fourteen fields, so a viewer changing one date submitted the
placeholder name, an empty description and all eight security indicators as though they had typed
them. Fixed in `2f9885db` by diffing against a baseline - the record as the submitter saw it -
rather than by making the form honest, because the form is one of many.

**Scope of the audit** (counted 2026-08-25, `src/urbanlens/dashboard`):

- 28 files build a full `FormData` payload on submit;
- 23 bare `.save()` calls in `controllers/` write every column, against 88 that scope
  `update_fields` - so the good pattern is already the majority and the outliers are findable;
- 17 `form.save()` ModelForm calls, which write every field in the form by default.

Two directions, and they compose: make submits dirty-only (the client knows what it prefilled, so
it can send only what differs), and make writes field-scoped (`update_fields`, which most of the
codebase already does). The second is the safety net for anything that still posts everything, and
is the cheaper half to finish first.

## P6 — Production REData still 404s `/api/v1/public-locations/`, so a fresh dev environment seeds no catalog pins

`id: P6` · `status: open` · `updated: 2026-08-21`

Previously titled "production REData 404s on `/api/v1/public-locations/` (and `/capabilities/`)".

Verified live against a fresh dev environment (`a962bf8`, `--redata production`, the default as of
this session): `bin/dev_env.py create` correctly reported credentials (`demo / demo-a962bf8`,
confirming the seed-summary parser fix), but seeding logged `REData request to
/api/v1/public-locations/ failed (404)` and fell back to zero catalog pins - only the Hudson River
State Hospital landmark pin was seeded.

Confirmed with `curl` directly against `https://redata.urbanlens.org/api/v1/public-locations/`
(plain HTML 404, not a DRF JSON 404 - the route itself isn't matched) and
`https://redata.urbanlens.org/api/v1/capabilities/` (same). Both routes exist in the local REData
checkout (`../REData`, `src/redata/api/urls.py`, HEAD `6273443` 2026-08-20) and both are the routes
the 2026-08-21 session's work built against - `parcels/lookup/` on the same host returns 401
(route matched, auth/params rejected), so this isn't a credentials problem. Production REData is
answering from a build older than both routes.

This means "production REData by default" for new dev environments currently seeds no real
catalog pins - not a UrbanLens-repo defect, but worth knowing before trusting a fresh environment's
seeded pin count. Resolves itself once REData's production deployment picks up the commit that adds
these routes; nothing to do here in the meantime beyond this note.

## P7 — nginx pins its app upstream at config load and REData's `ref` is stored as permanent identity

`id: P7` · `status: open` · `updated: 2026-08-19`

Previously titled "performance and ops defects found but not fixed".

Found during the 2026-08-19 sweep, verified by reading both the query definition and every call
site. The three worst (`group_conversations_for` materialising every message in every group,
`_notify_group_message` loading a group's whole history on every send, and the rate limiter reading
one row four times per outbound call) were fixed in the same pass; these were not.

~~**The navbar messages dropdown builds the entire inbox to render at most 8 rows.**~~ Fixed
2026-08-19. `conversations_for` takes `only_unread`, which becomes a HAVING on the aggregate it
already computes, so the partner/last-message/identity lookups are sized by what will be shown.
`unread_conversations_for` merges that with the group half (bounded by memberships, filtered in
Python). The empty-state flag no longer needs the inbox either: `has_any_conversation` is two
`exists()` calls, where it used to test the length of the list the view had just built.

~~**The homepage runs ~12 aggregate/list queries for widgets the user may have disabled.**~~ Fixed
2026-08-19. Worth stating precisely, because most of that context was never the problem: nearly every
entry is an unevaluated queryset, so a disabled widget costs nothing already. Exactly two were eager
- the ten counts behind `home_stats`, and `home_recent_comments`, forced by the `sorted()` that
merges pin and trip comments. Both are now built only when their widget is enabled, guarded by a test
that compares actual query counts with the widgets on and off.

~~**The pin-list detail page costs two extra queries per pin (rating and wiki), unpaginated.**~~
Fixed 2026-08-19. `_list_items_with_labels` now selects `pin__location__wiki` and prefetches
`pin__reviews`. Worth noting how it hid: the function's docstring already claimed it "matches the
same prefetch shape the main map's bulk pin endpoints use ... without N+1 queries", and both model
properties involved (`Pin.rating`, `Location.display_name`) document in their own docstrings exactly
which prefetch they need. Three accurate comments, and the code between them still missed two
relations. ~~The page remains unpaginated.~~ **Fixed 2026-08-25** (`582458d3`): item rows now
paginate at 50/page via the same height-based "revealed" HTMX pattern the Memories gallery uses, a
trailing sentinel div lazy-loading the next page through a new `PinListItemsPageView`. The overview
map still plots every pin on the list regardless of pagination, since map data was never the
expensive part.

~~**Pin merge suggestion cards issue 8 `COUNT` queries each.**~~ Fixed 2026-08-19 by annotating the
four counts on `pending_merge_suggestions`, with `distinct=True` on each - the four joins multiply
one another, so plain counts would report visits x photos.

**Verified end to end 2026-08-19.** `bin/dev_env.py create` produced a working
`https://e2e-check.dev.urbanlens.org` (HTTP 200 direct and through the router), 17 healthy
containers and its own checkouts on disk; `list` reported both environments running; `destroy`
removed containers, files, registry entry and route completely. `--no-redata` skipped the REData
steps cleanly.

**RESOLVED 2026-08-21: a dev environment no longer costs a second REData stack.** A default
`create` used to start *two* stacks - nine UrbanLens containers and eight REData ones (app,
celery-worker with Playwright/Chromium, celery-beat, flaresolverr, tor, searxng, valkey, db), the
REData half alone taking roughly eight minutes on a cold image cache and then sitting idle for any
agent not working on REData integrations. The ops call has been made: `create` now takes
`redata="production" | "own" | "none"` and defaults to **production**, writing the host's
`UL_REDATA_API_URL`/`UL_REDATA_API_KEY` into the environment's `.env` and building nothing.
`--own-redata` opts back into a private stack; `--no-redata` still means neither.

The containers were never the main cost. REData exposes almost no write surfaces - most of its
"write" operations do not store what we send, they make REData go *fetch* external data about a
location - so a throwaway instance spends third-party quota pulling data that is destroyed with the
environment. Production serves most of the same calls from its cache without contacting anything,
and caches whatever is genuinely new for good. See `../infrastructure/docs/OPS_TOOLING.md` for the modes and
`REDATA_MODES` in `bin/opslib/devenv.py` for the reasoning next to the code.

**The cold-boot fix is unexercised.** `docker compose up -d` exits non-zero when a dependent service
gives up on the app's healthcheck, and the app legitimately takes minutes on a first boot (migrate,
collectstatic, frontend build); the run then *skipped* the fifteen-minute health wait that would have
seen it succeed. That is fixed - the wait now runs whenever the app container exists - but the
verification run had warm images and `up` succeeded outright, so the new branch never fired. The
evidence for the bug is the previous run's own log ("dependency failed to start: container
ul_afb299e_app is unhealthy") followed by a healthy stack.

**`UL_CONTAINER_NAME=agent_<slug>` names nothing.** The isolation override pins every
`container_name` and `_compose` passes `-p ul-<slug>`, so both things that variable would have set
are overridden; it is a collision guard only. The comment above the start step asserted the opposite
("No -p: UL_CONTAINER_NAME ... already sets both"), and that stale comment is what `list_envs` was
written against. Both now say what is true.

**RESOLVED 2026-08-19: four ops-tooling paths that reported failure as success.**

- `dev_env.py destroy` set `containers: True` unconditionally and deleted the registry entry whatever
  `docker compose down` returned, so a failed teardown left containers running with nothing recording
  that they existed. It now reports from the exit codes, keeps the entry marked `orphaned` when the
  teardown failed, and `bin/dev_env.py` exits 1 with the compose output.
- `dev_env.py list` looked for `agent_<slug>`, a prefix nothing creates - so every environment read as
  not running. All three sites that need this name now share `devenv.container_name`.
- `dev_env.py create` wrote its registry entry before cloning and `run_step` records failures rather
  than raising, so a failed clone crashed the next step on a missing directory *after* claiming the
  environment existed. It now marks the entry failed and stops.
- The staging **data-preservation check passed vacuously**, in two independent ways: `_VERIFIED_TABLES`
  named `dashboard_pins`, which no model owns (`Pin.Meta.db_table` is `dashboard_user_pins`), and both
  database containers were addressed as `urbanlens_<UL_ENVIRONMENT>_db` while compose names them
  `urbanlens_${UL_CONTAINER_NAME:-${UL_ENVIRONMENT:-production}}_db`. Either makes a count come back
  `-1`, and the comparison skipped `-1` silently while reporting "5 tables match". An uncountable
  table is now a failure in its own right, the summary counts what was actually compared, and both
  container names come from one helper.

**And a fifth, found by the tests written for those four:** `bin/run_tests.sh` synced `src/` but not
`bin/`, while `tests/hypothesis/test_ops_tooling.py` imports `bin/opslib` directly. Every ops-tooling
test was running against whatever was baked into the image - the exact failure the script's own header
describes for `src/`, unnoticed here because the imports kept working while the code behind them
aged. The sync and both halves of the parity check now cover `bin/` too.

~~**Still open:** every dev environment is configured with a REData URL its own app container cannot
reach.~~ Fixed 2026-08-20, and it had **two** halves - the second only found by testing the first
against the live `e2e-check` environment rather than reasoning about it.

1. The URL was `http://127.0.0.1:<redata_port>`, which inside the app container is the app
   container; REData publishes on a *host* port. Confirmed live: `URLError [Errno 111] Connection
   refused` from inside `ul_e2e-check_app`. The isolation override now gives the five application
   services (`app`, `app-ws`, and the three celery services)
   `extra_hosts: host.docker.internal:host-gateway`, and the env file points at that alias.
2. Routing to it is not enough. REData's seed `.env` sets
   `RD_ALLOWED_HOSTS=localhost,127.0.0.1,redata.urbanlens.org`, so a request arriving with any other
   `Host` gets **400 DisallowedHost**. Measured: over the gateway the container got 400 where the
   host got 401, and 401 once the `Host` header was forced to `localhost`. `create` now writes
   `RD_ALLOWED_HOSTS` including the alias, and `redata_api_url`/`redata_allowed_hosts` are held to
   each other by a test.

3. And with routing and hosts both fixed, every call still came back **401**. `UL_REDATA_API_KEY` is
   seeded from the host's `.env` along with the other secrets - but it is not that kind of secret.
   It names a *row in a database*, and a private REData starts with an empty one, so the inherited
   key authenticates against nothing. `create` now mints a key in the new instance
   (`_provision_redata_key`, via `manage.py shell -c` - REData has no key-issuing command, the key
   is normally created through the admin) and writes it into the UrbanLens `.env` before that stack
   starts. Best-effort: a failure there leaves the environment usable for everything that is not
   REData, and says so in the step log.

Each failure is close to invisible on its own, and they get worse in order: connection-refused is
obviously wrong; a 400 from a service that is plainly up reads as a bad request; a 401 reads as a
credentials problem with the *host's* REData rather than as "this instance has never heard of you".

**Verified live 2026-08-20** by repairing the running `e2e-check` environment in place rather than
trusting the reasoning: from inside `ul_e2e-check_app`, `GET /capabilities/?lat=&lng=` now returns
21 domains through the authenticated gateway. That answer also validated the satellite work below -
this instance reports `mapbox`/`bing_maps`/`azure_maps` as *not* applicable (no vendor keys), so the
hardcoded list had been asking three providers it could never serve.

**One more thing that repair surfaced: recreating the `app` container 502s the stack until nginx is
restarted.** nginx resolves its upstream once, at config load, so a recreated app comes back on a new
container IP and nginx keeps dialling the old one - `connect() failed (111: Connection refused) ...
upstream: "http://172.25.0.5:8000/"` while the app itself answers 200 on `127.0.0.1:8000` and *every
container reports healthy*. `create` never hits this because it brings the whole stack up together;
any in-place repair of a running environment does, and the symptom points at the app rather than at
nginx. A `resolver`-based upstream in the nginx config would fix it properly; `docker restart
<slug>_nginx` is the one-line workaround, and is what the live environment needed.

**RESOLVED 2026-08-20: sending a group message cost a `Friendship` lookup per member, twice.**
Measured first: an 8-member send ran 18 queries, 8 of them `Friendship.between(member, sender)`. The
per-member cost was a *recorded decision* (2026-07-23) rather than an oversight - the payload carries
the sender's name, so it has to pass each recipient's own visibility, and the alternative leaks a
masked name over the live channel. What was missing was a way to ask the question in bulk.

`Profile.visible_profile_pks` batches many subjects for one viewer. A group message is the mirror:
one subject, many viewers - which that function cannot express, so both halves of a send
(`_notify_group_message` for the bell title, `broadcast_group_message` for the live payload)
resolved it a row at a time. `Profile.viewers_who_can_see(subject, viewers)` is the mirror, with
`DirectMessageTemporaryAccess.granting_viewer_pks` under it and
`identity_visibility.resolve_identity_for_viewers` on top; group create and add-members use it too.
Both directions are now held to `can_view_profile` by `test_identity_visibility_batch` across every
`VisibilityChoice` and relationship - the same treatment the original batch got, and for a sharper
reason: this one decides whether a *whole room* sees a name.

`_notify_group_message`'s docstring had promised a fixed query count since the unread check and the
preference lookup were batched. It was true of two of the three per-member things it did. There is
now a test holding it, and it counts reads only - one INSERT per notified member is the work itself.

**Same class, four more sites, fixed in the same pass:** the external API's group-member roster and
friend-ratings list, the group sidebar's last-sender previews, and global search's DM results all
resolved identity one row at a time when `visible_profile_pks` already existed for exactly that. The
sidebar one is worth naming: it *had* a query-scaling test, and the test passed, because every group
in its fixture had the **same** last sender and the function's own dedup cache hid the cost. The
test now uses distinct senders.

~~**The Building Attributes card picks the nearest building without excluding envelope parents,
ambiguous overlaps, or off-property records.**~~ Fixed 2026-08-19. `_nearest_building` now applies
`buildings_on_property`/`countable_buildings`/`confident_buildings` before ranking. Each is a
*preference*, not a hard filter: when nothing survives, the next-weakest set is ranked instead, so a
parcel whose only record is ambiguous still shows what is known rather than going blank.

~~**`ensure_building_places` ignores `parent_ref`.**~~ Fixed 2026-08-19 - nesting is resolved
topologically, with a cycle break, and a parent outside the list falls back to the parcel rather
than losing the building. The same pass found a second defect the fix exposed: `find_matching_place`
applied its mutual-centroid-containment fallback to records that *do* carry a stable provider id, so
an L-shaped block and a wing tucked into its corner could merge back into one place - undoing
exactly the reconciliation REData did to keep them apart. **Still open:** the reconciled `ref` is
persisted as a permanent identity (`Place.provider_key`, floorplan `building_ref`) and REData does
not guarantee it is stable across responses.

~~**`flatten_timeline` reads `capture_date_resolved` one nesting level too high.**~~ Not a defect on
the current tree - re-verified 2026-08-19. `_resolved_flag` reads the `attributes` blob first and
falls back to the top level, and has since commit `8bf86daf`; the finding described the code before
that.

## P9 — REData gaps: mostly closed 2026-09-08; `?limit=` is REData-side, land-use-area geometry needs a map-overlay decision

`id: P9` · `status: open` · `updated: 2026-09-08`

Previously titled "REData consumption gaps left after this session's sweep", then "REData gaps
remain - `?limit=` is inert, the 15-route list is already stale (missed a post-sweep route), a
`tile_template` slide is one 256px tile".

A full cross-repo sweep of UrbanLens's REData integration on 2026-08-19 (both repos read end to end:
REData's `api/urls.py`, every serializer, `../REData/docs/api-reference.md`, `docs/fields-available.md` and the
whole `CHANGELOG.md` `[Unreleased]` section, against all ~32 `redata_*` gateways and 8 panels).
Everything that was *wrong* was fixed in the same pass - four panels reading keys REData has never
emitted, the places gateway parsing a `{count, results}` envelope as a bare array, `?is_aerial=true`
being a parameter of a different endpoint, CRIS selecting resources that were not CRIS's, Florida's
whole sale-record provider being dropped on attribution. What followed was what the sweep found and
deliberately did not build - kept below so the fix-it-later history stays legible - and this section
records that **every item worth building has now been built** except one that needs a product
decision first.

**`?limit=` is still inert on every REData near-point endpoint** (unchanged - not re-verified this
session). `NearPointQuery` (`REData/src/redata/api/coordinates.py`) parses `lat`/`lng`/
`radius_meters`/`provider`/`force_refresh` and nothing else; the `limit` parsing at :411 belongs to
the *text*-query parser. So every panel that passes `limit=20`/`25`/`30`/`50` caches up to REData's
own server-side cap instead. Not fixed here on purpose: trimming client-side would change the
user-visible counts panels report ("N mapped within 250 m") from REData's floor to our own
arbitrary bound, which is less accurate, not more. The fix belongs in REData - have
`parse_near_point_query` accept `limit` - after which the UrbanLens side needs no change at all.

**Re-audited 2026-09-08: of the 15 routes this entry originally judged worth wiring up, all but one
are now built**, and one (the street-view base/download routes) turned out on inspection to already
be a non-issue rather than a gap. In value order, as originally written, with what actually happened
to each:

1. ~~The `/street-view/` base endpoint and its two mirrored-bytes download routes (the carousel
   currently hot-links provider URLs that rot).~~ **Not a gap.** `/street-view/timeline/` (a superset
   of the base endpoint - every capture at every date, not just current) was already consumed
   (pre-existing, `services/apis/locations/redata_street_view_gateway.py`), and the download routes
   were already a considered, documented tradeoff rather than an oversight:
   `RedataStreetViewGateway`'s own module docstring explains `download_url` needs REData API auth,
   so the browser is deliberately served the network's own `image_url`/`thumbnail_url` instead, which
   attribution requires linking to anyway. The "rot" framing in the original title was speculative,
   not measured.
2. **Built:** `GET /places/cid/{cid}/` plus its media download. New "Google Maps Details" info card
   (name, category, rating/review count, price level, hours summary, phone, website, up to 3 photos)
   for any pin whose `Location.cid` is already known, reading REData's already-run deep scrape
   instead of discarding it - `RedataCidGateway.get_place_detail`/`download_media`,
   `plugins/builtin/redata_place_details.py`, a server-side media proxy
   (`PinPlaceCidMediaView`) so REData's key never reaches the browser. Commit `069f43e8e`.
3. **Built:** `/parcels/{uuid}/coverage/`. `RedataGateway.lookup_coverage` now gates the Property
   Records panel's `assessments`/`sale-records` calls (the two supplementary domains coverage
   actually reports on - `liens`/`tax-payments` aren't coverage-registry domains at all, so those
   two stay unconditional as before), falling back to "always call" if the precheck itself fails.
   Commit `a12827a6f`.
4. **Built:** three of the four `/parks/{code}/` routes - `alerts` (the live closure/hazard one,
   surfaced as icon-led, safety-critical `facts` ahead of routine park info, on both the JSON API and
   now the web panel - see below), `visitor-centers`, `campgrounds`.
   `RedataNationalParksGateway.get_alerts`/`get_visitor_centers`/`get_campgrounds`,
   `plugins/builtin/nps.py`. The bare park-detail route (`GET /parks/{code}/`) and `places`/`webcams`/
   `media` were left unbuilt: detail's fields are already embedded in the `/parks/nearby/` row this
   panel already caches, and places/webcams/media resolve to the same generic `PointOfInterest`/
   `MediaItem` rows other panels already surface, at lower value than the four built. Commit
   `4d01e40aa`.
5. **Built:** `POST /imagery/capture/`, for materializing one date of a `time_series` (NASA GIBS)
   layer - previously skipped entirely (`if delivery == "time_series": return None`), now
   materializes the most recent published date per layer via
   `RedataImageryGateway.capture_time_series`. Bundled with a second, related imagery fix: the
   satellite carousel's `tile_template` slides (see the still-open finding this entry used to lead
   with, below) also went from one raw, badly-framed 256px tile to REData's own composited
   `GET /imagery/{uuid}/download/` image. Commit `3d97e6d36`.
6. **Built:** `/reference-documents/` near-point (Wikipedia + Wikidata). New free/ungated panel
   surfacing the nearest Wikipedia article (as the card's description/footer link) and the nearest
   Wikidata entity's structured claims (what it is, when built, designer, architectural style,
   heritage designation) - data no other panel in this app surfaces.
   `RedataReferenceDocumentsGateway.get_reference_documents` (added to the *existing* gateway class,
   which already served the unrelated by-name `/reference-documents/search/` endpoint - do not
   confuse the two), `plugins/builtin/redata_reference_documents_nearby.py`. Commit `a34e1c197`.
7. **Partly built, one part deliberately deferred:** the `land-use-areas`/`demographics`/
   `national-parks` parcel trio. `demographics` (census-tract population/income/home-value/rent/
   owner-renter-split) and `national-parks`' `containing_park` (a real point-in-boundary check, more
   precise than the existing `nps.py` panel's nearest-by-coordinate search - kept deliberately
   decoupled from that panel rather than wired together, to avoid a cross-plugin coupling that wasn't
   asked for) both now render on the Property Records card.
   `RedataGateway.lookup_demographics`/`lookup_national_parks`. Commit `a12827a6f`. **Still not
   built, on purpose:** `land-use-areas`' boundary *geometry* (as opposed to the category chips
   already shown from a different, already-consumed field) - rendering an actual polygon needs a
   map-overlay UX decision (a new layer? a toggle? which existing boundary-rendering chain, if any)
   that wasn't this pass's to make. Flagging for product input rather than guessing at it.

Of the original "45 of 106 routes with no UrbanLens caller" count, 23 were already correctly judged
irrelevant by design (nested write CRUD, the readback ViewSets, IIIF) and are still irrelevant; the
7 items above accounted for the rest that were worth a look. That 45/106 count itself was already
stated as stale the moment it was written (REData adds routes continuously - see the
`/historical-features/` entry below) and should not be re-cited as current without re-deriving it.

**Web panel follow-up, same day:** the alerts JSON-API wiring above (item 4) only reached
`NpsPanelSource.api_payload` - `PinController.nps_info` (the actual web-rendered `pin_nps.html`
partial a park explorer sees) still called only the pre-existing `park_facts()`, never the new
`alert_facts()`, so a live closure/hazard alert reached API clients but not the page itself. Fixed
in the same pass that wrote this entry: `nps_info` now passes `alert_facts(data)` into the template
context, rendered as its own icon-led list ahead of the routine facts block, mirroring the API's own
"safety-critical first" ordering.

**`GET /weather/history/` is consumed on trips and on visit history; the bulk Memories lists are
deliberately left out.** (Visit history added 2026-08-20.)

Each visit row on a pin's Visit History tab now says what the weather actually was that day -
`68° / 50°F · 1.00 in rain · gusts 31 mph` - which is the fact that makes a photograph of a flooded
basement mean something. Three things about how, since the obvious implementation is wrong in each:

- **The panel never makes the call.** The first version did, on the reasoning that the panel is
  already loaded by `hx-trigger="load"` behind a spinner. That reasoning was wrong twice over: a
  spinner does not make it acceptable for a slow REData to hold up an entire visit list for a
  decorative line of text, and it put an outbound call inside a page render, which is a thing no
  other external-data surface in this app does. Two *unrelated* tests found it, by tripping the
  suite's localhost-only network guard the moment the panel started fetching. It now reads the cache
  (`recorded_days(..., allow_fetch=False)`) and queues the gap
  (`tasks.fetch_recorded_weather`) - the same fetch-behind/render-from-cache split every pin-detail
  panel uses. Weather appears on the second view, which for this is the right trade. Days inside
  ERA5's publication lag are never queued: they are not missing, they are unanswerable until
  published, and queueing them would retry forever.
- **Sparse days are not a range.** `recorded_range` fetches `min..max` in one request - right for a
  trip's activities, wrong here: a page of visits to the same ruin can span decades, and the range
  form would fetch *and cache* every day in between to display ten. `recorded_days` clusters instead,
  merging days within a month and splitting beyond it.
- **Grouped by `Location`, not by pin.** `?children=1` lists a whole subtree, and those are different
  places; one request per location, not per visit.

**Not done, and not an oversight: the Memories timeline and Visits subpage.** Both are bulk lists
spanning a whole account - hundreds of visits across hundreds of locations - so a fetch per location
is not something a list render may do. The cache-only alternative (`allow_fetch=False`) would show
weather on whichever rows a user had happened to open the pin page for and nothing on the rest,
which reads as broken data rather than as a partial feature. Doing it properly needs a background
enrichment source that fills the cache per Location, which is a different piece of work.

The original entry, kept for the trip half: A finished
trip's weather panel was empty - the view filtered to activities scheduled today or later, so a
forecast-only panel had nothing to say about a trip that had happened. Past activities now show what
the weather actually was (`controllers.trip._build_activity_history`), fetched as one range per
location rather than one call per day, and converted to the units every other weather surface uses.
Days inside ERA5's ~6-day publication lag are never requested, so they cannot be cached as blank.
What remains is the surface the design doc named first: the shared visit dialog and Memories.
`visit_weather.recorded_weather(location, day)` is the single-day entry point for it.

**Fields fetched, cached, and never shown.** ~~`special_land_use_areas` (military installation /
correctional facility / national park / campus), `flood_zone_code`, `deed_document_links`~~ - shown
on the Property Records card as of 2026-08-20. The land-use categories are chipped rather than
listed, and ahead of "Delinquent taxes"/"Boundary available": two of the four describe ground where
being present is a different statute rather than a trespass question, which is a fact about the
*visit* and not another attribute of the property. A category present but unnamed still renders -
TIGERweb rows are confirmed to omit fields per category, and dropping the row would turn "inside a
correctional facility" into silence.

Still unread: `raw_attributes` on the parcel record (per-jurisdiction keys, no display shape that
generalises - a deliberate skip rather than an oversight).

~~The sheet thumbnail, library landing page and georeference accuracy on the historical-map
picker~~ - shown as of 2026-08-20. The thumbnail is the one that mattered: choosing between a dozen
scanned sheets of one neighbourhood is a *visual* task, and the picker offered eleven rows all
reading "Sanborn Fire Insurance Map of ...". Both the thumbnail and the catalogue page are the
**institution's** own public URLs, not REData-authenticated ones, so unlike the tile template they
need no proxy - that distinction is why they were safe to link directly and worth stating, since the
tile template two lines away must never reach the browser.

Accuracy needed a judgement rather than a field read. `rmse_meters` is the fit's own residual, and
REData's model docstring warns that a thin-plate spline interpolates its control points *by
construction*, so its residual is ~0 whatever the placement is actually like. Printing "±0 m" for one
would advertise a perfect fit for possibly the worst sheet in the list, so splines report nothing;
so does anything under 25 m, which on a scanned historical map is noise. What survives is the case
worth disclosing before somebody traces a building off the overlay: "placed to ±60 m (4 control
points)".

**Also fixed in passing: the picker's rows had no thumbnail slot at all**, so this needed the row
layout as well as the data - a fixed 2.5rem box, because the list scrolls inside a 14rem window and
one tall scan would otherwise push every other sheet out of view.

~~Entrance fees, real operating hours, directions and weather guidance on the national-park
panel~~ - shown as of 2026-08-20, via `plugins.builtin.nps.park_facts`, which the web panel and the
API payload now share (they rendered different hand-built subsets of the same payload before).

The hours case was the sharpest instance of this whole category: the template rendered "Standard
hours vary - check NPS.gov" **whenever `standardHours` was present** - that is, precisely when it
did not have to say that. Consecutive days with identical hours are now grouped
("Mon-Fri: 9:00AM - 5:00PM; Sat-Sun: Closed"), and a week NPS has only partially published renders
nothing rather than collapsing an unknown day into a range, which would read as "closed that day".

Fees needed the same care in the other direction: `cost` is a *string* in NPS's API and is sometimes
free text ("varies"), so an unparseable fee is skipped rather than guessed, and **absent fees are
not reported as free** - "Free" has to mean free. `weather_info` is deliberately still unread: it is
a paragraph of seasonal prose and the pin already has a weather panel showing the actual forecast.

**Found while doing it: the NPS panel had no stylesheet at all.** Every `nps-*` class in
`pin_nps.html` matched nothing - the card rendered with only the generic `.card` chrome. That is one
concrete instance of the "46 BEM modifiers applied in templates with no CSS rule" entry further down
this file, and it now has one. The fact grid mirrors `.simple-info-panel`'s `.simple-info-meta`
values rather than sharing the selector: hoisting that rule out of its parent changes its
specificity for every panel that uses it, which is a bigger change than this panel is worth.

**Correction 2026-08-20 to this entry's own last item.** It named "`residual_geometry` and each
source's `attributes` (the assessor's sqft/stories/condition) on the reconciled building record" as
an UrbanLens gap. Checked against REData: `BuildingRecord` promotes `name`, `address`,
`building_number` and `year_built` and nothing else - sqft, stories and condition are **not**
standardized per building, they sit in each source's raw `attributes` under whatever that county's
GIS layer calls them. Consuming them from here would mean guessing column names per jurisdiction,
which is the exact trap `cris_buildings` is stuck in. The parcel-level equivalents *are*
standardized (`BuildingCharacteristics`) and the Property Records card already shows them. So this
is a REData-side gap - promote the per-building CAMA fields there - not an unread field here.

`residual_geometry` (a parent envelope's footprint minus its polygon-bearing children) is genuinely
unread, and on inspection has no consumer worth building: it does *not* fix Place-tree overlap,
because a parent Place containing its children is correct hierarchy rather than the sibling overlap
that was the actual bug. What it would support is a map annotation for "building mass no mapped wing
accounts for", which is a real but narrow thing to want.

**Hardcoded maps that filter out new REData providers.** ~~`satellite_imagery`'s
`_REDATA_PROVIDER_NAMES` both restricts the request and gates rendering, so REData's `s2cloudless`
provider is invisible~~ - fixed 2026-08-20. ~~`cris_buildings` reads one inventory's raw column names
and so cannot show the other cultural-resource providers at all~~ - addressed 2026-08-20, and the
count was low: REData registers **25** historic inventories and UrbanLens read one.

`cris_buildings` was not the place to fix it. It renders CRIS's own raw ArcGIS columns (`USNName`,
`USNNum`, `EligibilityDesc`), so it *has* to name its provider - handing it an NRHP row blanks the
card, which is a bug that already happened once and is why the request was restricted in the first
place. Widening it would have meant either re-introducing that bug or rewriting a working NY-only
panel, its media gallery and its enrichment source.

`plugins.builtin.redata_historic_registers` is the other half instead: one card over the whole
registry, rendering only the fields REData standardizes (`name`, `resource_type`, `scope`, `status`,
`year_built`, `architectural_style`, `use_type`) and never a provider's `attributes`. It discovers
its providers from `/capabilities/` and excludes only `ny_cris`, which has the richer panel. New
`RedataCulturalResourcesGateway` keeps the `{count, complete, results, providers}` envelope that
`property_records.RedataGateway.lookup_cultural_resources` flattens away, so `RedataInfoPanelSource`'s
outage rule applies - one inventory being down must not be cached as "this place is on no register".

Two details worth keeping:

- The register **name map is not a gate**. A provider missing from it renders under a title-cased
  tag. Reading a display-name map as a permission list is precisely what hid `s2cloudless`, and the
  test says so.
- Only the kept fields are cached. `attributes`, `detail_payload` and `geometry` are per-provider,
  large, or both, and cached payloads are read on every pin-detail render.

**Still unread:** attachments outside CRIS. `nps_nrhp` can fetch a nomination *document*, and other
providers declare their own `detail_fetchable_types`; this panel is search-tier only, so those
never reach the Media gallery the way CRIS's survey photographs and inventory forms do. That is the
natural next step and a bigger one - it needs a per-provider detail fetch and a proxy route per
provider, not another panel.

The satellite half, since the fix is not simply "call capabilities":

- `s2cloudless` is **one global cloud-free Sentinel-2 mosaic per year since 2016**, delivered as a
  tile template with a `captured_on` per year. For this app's subject that is the single most useful
  source in the carousel - a yearly sequence is how you see a roof come off or a building
  disappear - and the timeline endpoint was already fetching the captures, which the carousel then
  dropped for having no entry in a dict in this repo. It was excluded with no recorded reason.
- The provider list now comes from `GET /capabilities/`; what stays written down is
  `_SHOWN_ELSEWHERE`, the handful another *UrbanLens* panel covers better (Esri's direct gateway,
  the USGS topo panel, the historical-map picker) plus the `loc_` prefix for loc.gov's scanned-map
  collections, which are generated on REData's side. A display name is looked up if we have one and
  title-cased from the tag if we do not, so a new source appears rather than being dropped.
- A capability outage falls back to the curated list rather than to nothing, unlike the
  points-of-interest panel: `/imagery/` takes an explicit provider list either way, so the fallback
  is a bounded request rather than a fan-out. **But an empty list means "all providers" at REData's
  end**, so "everything applicable belongs to another panel" has to mean *no request at all* - there
  is a test for that, because the two empty cases read identically at the call site.

**RESOLVED 2026-09-08: a `tile_template` slide used to be one 256px tile, and the pin could be at
its edge.** `_resolve_tile_template` resolved the single tile *containing* the coordinate at zoom 15
(~1.2 km across), so a site near a tile boundary was shown in the corner of its own photograph, or
half out of frame - tolerable for OpenTopoMap, where the slide is terrain context, wrong for
`s2cloudless`, where the slide is supposed to be a picture of the place. The compositing this entry
guessed would be "a real piece of work (fetch, stitch, encode)" turned out to already exist
server-side: `GET /imagery/{uuid}/download/` composes exactly that from the covering tiles. The fix
was a client-side call to it, not the pipeline this entry expected to have to build. Commit
`3d97e6d36` (bundled with `POST /imagery/capture/`'s wiring, item 5 above).

**RESOLVED 2026-08-19: the points-of-interest registry is consumed.** It was the largest
unconsumed surface and the one most relevant to this app - agency surveillance-camera registers,
`osm_surveillance` (worldwide, and outside Chicago and Austin the only camera source there is),
`fcc_asr` antenna structures, FAA facility groups, EPA contamination programmes, storage tanks,
school layers - reachable only one provider at a time, with only `yelp` and `epa_echo` called.

`plugins.builtin.redata_site_features` now surfaces them as one "Cameras & Structures" panel. Three
things about *how*, since the obvious implementation would have been wrong:

- **No provider list is hardcoded.** Most of these providers are generated on REData's side from
  dataset tables, so their tags are not knowable to a client and a list here would silently stop
  growing. The panel asks `GET /capabilities/?lat=&lng=` which providers cover the point - a bounds
  test, no upstream call - and requests exactly those. That also answers the "capabilities is fetched
  only to render one admin card" finding: it is now on the pin-detail path, cached an hour per coarse
  coordinate, with its own rate-limit budget.
- **Failed discovery asks nothing, not everything.** A request with no `provider` fans out across the
  whole registry, which is the one outcome the capability lookup exists to prevent, so the failure
  direction had to be the safe one.
- **The two exclusion sets are kept apart.** `_SHOWN_ELSEWHERE` (providers with their own UrbanLens
  panel) is a fact about this app's UI, so a test holds every entry to a registered panel key.
  `_TOO_GENERIC` is one judgement about REData's taxonomy - `osm`'s generic point set, which would
  make the panel about nothing in particular - and is the only entry a REData change could
  invalidate. Writing them as one set hid that difference, and the test caught it.

An earlier draft of this entry said `yelp` is billable, as a reason to curate. It is not:
`billable=True` appears 11 times in REData and none are in this registry. The real cost is upstream
queries and quota, not money.

**Added 2026-09-08: `/historical-features/` is now consumed - and the 45/15 counts above were
already stale in a way this sweep did not anticipate.** REData's `/api/v1/historical-features/`
(retrospectively-mapped buildings, roads, water, railways, land use, places and venues near a
point, self-hosted OpenHistoricalMap/Overpass-backed) did not exist as of the 2026-08-19 sweep -
it is REData's single newest endpoint (`parcels/migrations/0094_historicalfeature.py`, the top
commit in REData's `git log`, first bullet in REData's `CHANGELOG.md` `[Unreleased]` section) - so
it was never on either count above. Wiring it up therefore does not shrink the "15 unwired" list;
it demonstrates that the list ages in a second direction nobody had reason to check for at the
time: REData adds endpoints continuously (the entry above already says "a summary claim ... ages
badly against a service that adds endpoints weekly"), so any route diff is stale the moment new
routes ship, not only when an old one finally gets consumed. New:
`services/apis/locations/redata_historical_features_gateway.RedataHistoricalFeaturesGateway`
(follows the same `RedataLocationContextGateway.near_point()` pattern as every other REData
near-point gateway) and `plugins/builtin/redata_historical_features.HistoricalFeaturesPanelSource`,
a free/ungated "Historical Features" card on the Private Pin page - REData's own docs are explicit
that `start_year` is frequently the date of the *source map* a feature was traced from, not a
construction year, and the panel never presents it as an age.

**Also added 2026-09-08, and not a route-consumption change: `/incidents/` gained a second, paid
consumer.** `IncidentHistoryPanelSource` (`plugins/builtin/redata_incidents.py:89-153`) calls the
same `RedataIncidentsGateway.get_incidents` the free `PoliceIncidentsPanelSource` already called,
at REData's full `years=25` ceiling (`_HISTORY_YEARS`, REData's own max) instead of the free
panel's 3, rendering a year-by-year trend instead of a short recent-rows list, and requires the new
`SiteFeature.INCIDENT_HISTORY` (`models/subscriptions/model.py:58`). The existing free "Reported
Incidents" panel is untouched and deliberately stays free: `SiteFeature.NEARBY_RESEARCH`'s own
docstring (`models/subscriptions/model.py:41-51`) already named it as one of several panels
"deliberately left free," and gating it now would take away something users already have, which
was never the ask - the new panel is purely additive. Why a dedicated flag rather than reusing
`NEARBY_RESEARCH` (which already gates EPA ECHO's nearby-facilities panel) is recorded as its own
decision - see [D10](designs/incident-history-feature-gate.md).

## P11 — 84 raw `fetch()` calls bypass `fetch-json.ts`, and "all the wrappers are gone" was a count, not a search

`id: P11` · `status: open` · `updated: 2026-09-06`

Previously titled "~40 raw `fetch()` calls bypass `fetch-json.ts` and fail silently; Organize's Media
tab is unwired dead UI", and before that "frontend TypeScript audit - remaining findings".

Full-tree audit of `dashboard/frontend/ts/` (every file read, eight passes). The four
security/safety items were fixed in the same pass; everything below was found but **not** fixed.
Line numbers are as of 2026-08-15 and will drift.

**Fixed 2026-08-19** (strike these when reading the list below):

- The one-shot `AbortController` in `entries/photo-location-scan.ts`. Every scan now begins with a
  fresh controller and cleared hits/clusters/selection (`beginScanState`), so the Firefox/Safari
  `webkitdirectory` path survives a Stop and a re-scan no longer double-counts photos into
  clusters. The directory-walk path passes `alreadyBegun` so the walk and Stop stay on *one*
  controller - resetting twice would have made Stop silently do nothing.
- The missing `pointercancel` in `shared/map-image-overlays.ts`. An interrupted touch drag on an
  overlay corner left `map.dragging` disabled until reload; `pointercancel` and
  `lostpointercapture` now run the same release handler, which is idempotent.
- The dead people-label Merge button (`_organize_label_card.html`, `peopleMergeSingle`). Note the
  fix is **not** the one the entry implies: wiring it to `label.merge` would 404, because
  `KIND_USER` and `KIND_MEDIA` both set `enable_single_merge=False`. The control was for a
  capability the server refuses, so it is gone. Guarded by
  `tests/hypothesis/test_label_card_merge_affordance.py`.

Also **not** a defect, checked 2026-08-19: `flatten_timeline`'s `capture_date_resolved` read. A
later report claimed it looked at the wrong nesting level; `_resolved_flag`
(`services/locations/imagery_timeline.py`) already checks `attributes` first and the top level
second, and has since commit `8bf86daf`.

**Highest-value single change:** raw `fetch()` call sites bypass `shared/fetch-json.ts`
(`fetchJson`/`sendJson`), several with no `response.ok` check at all. ~~Six hand-rolled wrappers
exist beside it~~ **- those six are gone as of 2026-09-06, and there was a seventh.**
`shared/photo-context-menu.ts` had its own `postJson` (raw `fetch`, hand-built CSRF header,
hand-rolled `response.ok` check) and was never in the list, because the list was a count carried
forward from the original audit rather than a fresh search. It is the same substitution
`album-items.ts` got - `sendJson(..., { reportsItsOwnErrors: true })`, since all four call sites
already catch and toast the server's own sentence - and the CSRF header is byte-identical either
way (`shared/csrf.ts`'s `getCsrfToken()` is `window.csrftoken ?? ""`, which is exactly what
`writeInit` inlines). Migrated 2026-09-06. **Before claiming the wrapper set is empty again, search
for the shape rather than checking the names off** - a local function that calls `fetch` and then
`.json()`. `postForm`/`getJson` (triplicated across
the three games) became `shared/session-request.ts`; `postJson` (album-items) and `savePosition`
(album-map) were straight substitutions, since `fetchJson` already reads an `error` key out of a
refusal and falls back to `HTTP <status>`, which is what both did by hand.

`postForHtml` (organize-tab-manager) was **not** a straight substitution, and that is why it
survived two earlier passes: it wants the response *body*, because Organize's bulk
delete/edit/merge answer with the re-rendered row list, and `fetch-json.ts` had no text-returning
sibling. `fetchText`/`sendForText` are that sibling. Worth knowing before migrating the next such
call site: the old wrapper threw `new Error(await response.text())`, so a Django debug page went
into the toast verbatim - `errorMessage()` discards markup, which is a behaviour change in the
right direction but a behaviour change. Two deliberate exceptions to keep:
`webauthn-client.ts` (self-contained for the minimal auth layout, already ok-checked) and the two
E2EE calls that need raw `Response` semantics (201-vs-200, `redirected`).

**Two corrections, both found 2026-09-06 while doing the first slice.**

- **"~40" was low. It was 99**, counted across `frontend/ts/` excluding tests and `fetch-json.ts`
  itself (`grep -rn '\bfetch(' --include=*.ts`, minus `globalThis.fetch`/`window.fetch`). **85 as of
  2026-09-06.** Two files still hold 43 of them: `entries/map-annotations.ts` (22) and
  `shared/e2ee-client.ts` (21), and the second is mostly the raw-`Response` exception this entry
  already names. The rest are single-digit tails across ~20 files - `markup-toolbar.ts` (6),
  `location-search-engine.ts` (4), `entries/floorplan-editor.ts` (4).
- **"a non-2xx dies in a `void`-ed promise with no toast" is not quite right, and the reason
  matters.** `themes/base.html:204-231` wraps `window.fetch` globally and toasts
  `Request failed (HTTP 503).` for every non-ok response - so there *is* a net, it is just a
  generic one, and it does not stop the caller then calling `.json()` on an error page and throwing
  a `SyntaxError` into that voided promise. Migrating a call site therefore has to opt *out* of the
  generic message or the user gets told twice: once usefully, once not. `fetchJson` sets
  `__ulReported` on the init object the wrapper reads, which is what lets the rest of this migration
  happen one call site at a time. `fetch-json.test.ts` holds the two sides together, since the
  template is inline JS that `tsc` cannot see.

**Done 2026-09-06: the three game clients.** `shared/session-request.ts` replaces the triplicated
`postForm`/`getJson`, and `entries/trivia.ts` no longer has a fourth unchecked `fetch` for
`urls.start`. Both helpers keep resolving with the parsed body rather than throwing, because that is
the contract 62 call sites already have; a refusal comes back as `{ error: "<the server's own
message>" }`, which is exactly what every `postForm` caller already tests for - so those call sites
started handling non-2xx responses without being touched. `getJson` additionally toasts and
`postForm` does not, which is asymmetric on purpose: **all 33 `getJson` call sites ignore the
result's shape entirely** (`data.friends ?? []`, so a failure rendered an empty list and said
nothing), while the `postForm` ones test `.error` themselves.

Verified in a browser against the running dev stack, not only in unit tests: the trivia page's
friends fetch returns 200 and renders, and with the same route stubbed to 503 the user gets one
toast carrying the server's own sentence, with no page errors. One `fetch` remains in those three
files - Consensus's multipart photo upload, which is already ok-checked and is not form-encoded.

`postForHtml` (organize-tab-manager) and `postJson` (album-items) still exist by name, and that is
fine: both are now three-line delegates to `sendForText`/`sendJson` that exist to carry the
`reportsItsOwnErrors` flag and a docstring saying why. `savePosition` in `entries/map-annotations.ts`
was never a wrapper at all - it is a local closure over one URL that returns the raw `Response`
because the caller branches on a 409. The two big files are untouched.

**The `fetch(` count is measured with a grep that also matches prose.** `shared/csrf.ts` is seven
lines with no request in it and appears in the per-file tally because its docstring says
"`fetch()` calls". The number is a bound, not an inventory; the two files that dominate it
(`entries/map-annotations.ts` at 22, `shared/e2ee-client.ts` at 21, most of the latter being the
raw-`Response` exception this entry already names) are what a next slice should read directly.

**Correctness, user-visible:**

- ~~`entries/map-annotations.ts:2264` - right-click-to-delete-vertex is dead code:
  `m.on("contextmenu.rcdelete" as never, ...)` is jQuery-style event namespacing that Leaflet does
  not support, so it binds a literal event name that never fires - while the toast at :2338 tells
  the user to right-click. The `as never` casts were the compiler flagging exactly this.~~ Fixed
  2026-08-22: binds the real `"contextmenu"` event instead.
- ~~`entries/map-annotations.ts:1371` - `loadDetailPins` has no ok-check and **clears the existing
  pin layer and list** on failure (console.warn only). :2558 `flushDpAutoSave` swallows validation
  errors, so autosaved edits are silently lost. :2047 `placeMediaItemAt` has no ok-check~~ ...
  :1643/:1712 bulk promote/delete `Promise.all` paths have no `.catch`, so one failure is an
  unhandled rejection with the selection never cleared. Fixed 2026-08-22, except the last part was
  already half done: `doDeleteSelectedDp` (the `:1712` delete path) already had its `.catch(() =>
  false)` from an earlier, unrelated change - only `doPromoteSelectedDp` (`:1643`) still needed it,
  and now has the identical fix with a comment pointing at its already-fixed sibling. **Still
  open**: `placeMediaItemAt` still has no loading indicator for the server-side image-materialize
  step it waits on - not attempted here since `window.mediaApplyMaterializedDrop`'s own contract
  (defined in the gallery/organize module) would need to be understood first, and this file has no
  established loading-state convention for the drag-and-drop-onto-map interaction to reuse.
- `entries/photo-location-scan.ts:207` - the `webkitdirectory` fallback path (Firefox/Safari)
  reuses an already-aborted `AbortController`, so after one Stop click **every** later scan halts
  on the first file. Also: hits accumulate across scans (re-scanning double-counts into clusters),
  and the photo uploads that run *after* the "Uploaded" toast have no progress indicator.
- ~~`shared/map-export.ts:270` + `themes/base.html:816` - `download()` awaits tile fetches (up to 8s
  each) but no caller awaits it: no spinner, no toast, unhandled rejections, and the save flow
  closes the composer mid-export so shapes project against a map being torn down.~~ Fixed
  2026-08-22: all three call sites in `base.html` now await the promise, toast on failure, and
  disable their trigger for the duration (the read-only viewer's button gets the real `.is-loading`
  spinner since it already has `.btn`; the composer's own bespoke `cmc-download-btn` just disables,
  since it doesn't participate in that class and this fix didn't attempt to move it onto one). The
  save flow now chains `_closeComposer()` onto the download's own completion instead of firing the
  download and closing the composer in the same tick - it was the composer's `deactivate()`
  tearing down `_composerMap` itself while `download()` was still awaiting tile fetches from it,
  exactly contradicting the comment already in that code explaining why the close had to wait.
- ~~`shared/markup-toolbar.ts:748` - `flushMarkupAutoSave` never checks `r.ok`, so a 400 (e.g.
  over-long label) reports success; the single pending-save slot also means editing item A then B
  inside the 500ms debounce silently discards A's changes~~ Fixed 2026-08-22: added the ok-check,
  and replaced the single shared slot with a per-item-uuid map, since it turned out reachable
  without even editing two items in sequence - `setItemLayer` (the sidebar's inline layer picker)
  shares the same autosave path and can target a completely different item than whatever the edit
  panel has open. **Still open**: nothing flushes pending autosaves on unload/tab-close - a
  `beforeunload` handler can't reliably await an in-flight `fetch`, and this app's CSRF header
  doesn't fit `navigator.sendBeacon`'s simple-request shape, so that half needs its own dedicated
  pass rather than a quick addition here.
- `entries/organize.ts:106,311` - the Media tab is **fully dead UI**: the template renders it
  selectable with checkboxes, a filter bar and Edit buttons, but no `OrgTabManager` is built for
  it, `ORG_FILTER_NAMESPACES`/`TAB_FILTER_NS` omit it, and the consolidated dialog opener has no
  `media-label-edit-dialog-body` case, so Edit swaps a form into a dialog nothing opens.
  ~~Separately, `_organize_label_card.html:77` references `peopleMergeSingle`, which is defined
  nowhere in the codebase.~~ Fixed: the merge button is an `hx-get` at `merge_url` now, so the
  dangling handler name is gone rather than merely still undefined.
- ~~`shared/organize-filter-engine.ts:188` - `countVisibleCards` tests `card.style.display`, but
  tree view sets `display` on the `.tag-tree-item` *wrapper*, so cross-tab match counts and the
  "N categories also match" footer count every card as visible. It duplicates `getOrgVisibleCards`
  (:99), which gets it right.~~ Fixed 2026-08-22: `countVisibleCards` now delegates to
  `getOrgVisibleCards` instead of re-deriving the check.
- ~~`shared/map-image-overlays.ts:209` - corner drag never handles `pointercancel`; an interrupted
  touch gesture leaves `map.dragging` disabled permanently.~~ Fixed: the release handler is bound to
  `pointercancel` and `lostpointercapture` as well as `pointerup`, and is idempotent because a cancel
  is sometimes followed by a capture-loss event for the same gesture.
- ~~`entries/spotguessr.ts:1491` - `submitGuess` has no in-flight guard, so a double-click posts
  twice and double-counts the session score~~ Fixed 2026-08-22: disables the submit button for the
  duration of the request, re-enabling only on failure. **Still open**: `:840 reportRoundTimeout`
  has no error handling, so a failed timeout POST hangs the round forever; all three games silently
  null the WebSocket on close with no reconnect and no "connection lost" notice.
- ~~`entries/trivia.ts:856` and `entries/consensus.ts:1051` - missing the round-id guard spotguessr
  has (`lastRevealedRoundId`), so the last player to answer double-counts HUD points.~~ Fixed
  2026-08-22, differently in each game because their reveal broadcasts aren't the same shape:
  trivia.ts gets a `lastRevealedRoundId` guard matching spotguessr's, since `submitAnswer`'s own
  response can independently credit the same round `showBroadcastReveal` will also see.
  consensus.ts needed a **resolution-aware** guard instead
  (`{roundId, resolution}`, not just `roundId`) - `services/consensus/session.py`'s competitive-round
  disagreement sub-phase broadcasts `round.revealed` for the *same* round_id twice by design (once
  `vote_open` with zero points, again once the tiebreak vote resolves with the real ones), so a
  bare round-id guard would have silently discarded every vote-winner's actual points instead of
  fixing anything.
- ~~`shared/organize-priority.ts:69` and `shared/album-items.ts:118` - optimistic reorder with no
  rollback on failure~~, so a failed save left the DOM showing an order the server never actually
  got. Partially fixed 2026-08-22: both now capture the order at drag-start (also covers the
  priority list's non-drag reorder paths - the order-editor and the top/bottom jump buttons) and
  restore it if the save fails. **The "no save sequencing" half is still open, and is not just a
  missing debounce**: since each save POSTs the *whole* order rather than a delta, chaining saves
  so they reach the server one-at-a-time interacts badly with the rollback above - if an earlier
  queued save fails and reverts to *its* pre-drag order, a later save that already succeeded would,
  on its own turn in the chain, read that just-reverted DOM and re-persist the stale order as if it
  were correct. Fixing this properly needs either a monotonic version per save (reject/ignore a
  write older than what the server has) or reworking rollback to fall through to the *next* known
  order rather than always "what preceded this specific drag" - either way, real design work, not
  a quick addition.
- ~~`shared/confirm-dialog.ts:90` - re-entrancy: opening a second dialog while one is open
  overwrites `resolveCurrent` (first promise pends forever) and `showModal()` on an open dialog
  throws into the promise executor.~~ Fixed 2026-08-22: a call while the dialog is already open
  now settles the earlier one as cancelled first, the same as a backdrop click would.
- ~~`shared/scroll-to-hash.ts:50` - re-scrolls on *every* `htmx:afterSettle` for the page's life, so
  any later swap yanks the reader back to the original anchor.~~ Fixed 2026-08-22: remembers the
  hash it already scrolled to and only re-arms if the hash itself changes.
- ~~`shared/onboarding-tour.ts:87` - auto-dismiss hooks bind only to elements present at init; HTMX
  swaps orphan them, so dismissed cards reappear.~~ Fixed 2026-08-22: re-runs registration on every
  `htmx:afterSettle` (a `WeakSet` keeps that idempotent rather than stacking a second listener onto
  an element that survived the swap unchanged). Found and fixed the same fix's own prerequisite bug
  while here: the doc comment says `retryEvent` fires *in addition to* `htmx:afterSettle`, but the
  code was an `if/else` between them - Organize's own `retryEvent` (a tab-switch, not an HTMX event)
  meant that page never listened to `htmx:afterSettle` at all, so re-registration would have gone
  in but never actually run for any HTMX-driven update there.
- ~~`shared/organize-header.ts:113` - a transient window resize below 768px *permanently* overwrites
  the stored gallery view preference.~~ Fixed 2026-08-22: the mobile fallback is now purely a
  display-time computation (`effectiveView()`) layered over the stored preference rather than a
  call to `setSharedView()` that persisted "list" over it - widening back past the breakpoint
  restores "gallery" automatically since the stored value was never actually touched.
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
- ~~E2EE keys persist in IndexedDB across logout - `clearProfileKeys` is called only from
  `resetKeys`. Possibly intended (documented same-origin trust boundary), but the logout gap looks
  unconsidered rather than chosen; decide it explicitly and add a "forget this device" action.~~
  Fixed 2026-09-05, tracked separately as P48 (`docs/archive/PROBLEMS-ARCHIVE.md`): `wireSignOutForm`
  now clears a signed-out profile's keys too, so this bullet was stale leftover text from before
  that landed - found still asserting the opposite of current behavior during the pre-merge audit
  of `release/v_0_8_0` (2026-09-08). Removed rather than left standing next to its own resolution.

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

## P13 — Pin-detail external-data freshness is one site-wide `external_data_cache_days` knob, not per-source

`id: P13` · `status: open` · `updated: 2026-07-23`

Previously titled "UL-277: pin-detail external-data freshness window is one global knob, not per-source".

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

## P14 — Custom pin and label icons are readable by any authenticated user; narrowing that needs a pin-visibility query nothing has

`id: P14` · `status: open` · `updated: 2026-09-05`

Previously titled "Authenticated media gate - residual per-family risk (2026-07-23)".

`/media/...` is now served through `dashboard.controllers.media.MediaGateView` (nginx `location
/media/` proxies to Django; authorized responses hand back to the `internal`-only
`/_protected_media/` alias via X-Accel-Redirect). Ownership is enforced per path family where it
is cleanly derivable, but several families intentionally fall back to **authenticated-only**
access (any logged-in user can fetch, no per-object check). Marked with `TODO(media-auth)`
comments in `src/urbanlens/dashboard/controllers/media.py`:

**Largely closed 2026-08-29.** The gate is now default-deny and the policy lives in
`dashboard/services/media/access.py` as a registry keyed by `upload_to` prefix. What changed:

- **Thumbnails were readable by any logged-in account.** `_authorize_image` resolved the owning
  row with `Image.objects.filter(image=rel_path)`, but a thumbnail is stored in the separate
  `thumbnail` column, so that lookup never matched, `image is None` was always true, and every
  preview took the permissive orphan branch below. `visible_to`, the DM participant rule and
  share revocation were unreachable code for every thumbnail, including DM attachments and
  safety check-in photos. Both columns are matched now.
- **An unresolvable file is refused, not served.** The orphan and unknown-family fallbacks both
  returned True; they return False. An orphan is indistinguishable from a live file whose owner
  the viewer may not learn about, and nobody holds a URL for a real orphan except by guessing.
- **Forgetting an authorizer is now a startup error.** `dashboard/checks.py`
  (`check_media_authorizers`, id `dashboard.E002`) fails `manage.py check` when a model field
  stores files under a directory the registry does not cover, so fail-closed cannot silently
  break image loading in production instead.
- **Stored paths are unguessable.** `pin_image_upload_path`/`pin_image_thumbnail_path` file each
  upload under `<2-char bucket>/<random token>/`, so a URL cannot be derived from the filename it
  was uploaded under. `pin_image_upload_path` deliberately preserves the camera stem for the
  attribution heuristic, which had made `IMG_4821` -> `IMG_4822` a working enumeration. Existing
  rows keep their paths; the gate is what protects those.

The two families below stay deliberately authenticated-only, now as registered decisions rather
than fallbacks. The rest of this entry records them and the file-stranding work around them.

- **`pin_custom_icons/` (Pin.custom_icon) and `label_icons/` (Label.custom_icon)**:
  authenticated-only. Strict owner-only enforcement risks breaking any surface that renders
  another user's shared/labeled pin (shared pin views, trip member maps, global labels with
  `profile=None`). Residual risk is low (small decorative icons, not photos), but a determined
  enumerator could fetch other users' custom icons.

  **Attempted 2026-09-05 and stopped at a missing primitive**, which is the useful thing to
  record. The intended rule is "owner OR global label OR an existing share/visibility
  relationship", and its middle and last clauses need a *pin* visibility queryset that does not
  exist. `Image.objects.visible_to` exists; `Label.objects.visible_to` is global-or-owned, which
  is too narrow, because another member's personal label legitimately renders on a pin you can
  see. There is no `Pin.objects.visible_to`, so "which icons does this viewer render" cannot be
  asked - and inventing that rule inside a media gate is how one ends up either leaking or
  blanking icons on legitimate pages. The next step is that queryset - own pins, shared pins,
  trip member maps, wiki-linked - built and tested on its own, with this gate as one caller.
- **Orphan files** (a file on disk under `pin_images/` or `comment_images/` whose owning
  Image/Comment/TripComment row no longer exists, e.g. row deleted without file cleanup):
  now **denied** (2026-08-29). The stranding paths below still matter for disk usage; they are
  no longer a disclosure route.

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
  code. **Partly done 2026-09-05**, after the owner's review the entry asked for.
  `services/media/file_cleanup.py` deletes the previous file after a successful save that replaced
  it, and a row's files when the row goes - for `Achievement.custom_icon` and `Profile.avatar`.

  **`Pin.custom_icon` and `Label.custom_icon` are deliberately excluded, and that is the finding.**
  Both models are restorable by the undo framework, which stashes the icon as its stored *name*
  rather than its bytes (`services/undo/handlers/pin.py`, `.../label.py`). Unlinking on delete - or
  on replace - would leave an undo within its window restoring a row that names a file no longer
  there: a broken icon with nothing to explain it, which is worse than the stranded file this was
  meant to stop. They want the unlink deferred until the `UndoAction` is pruned, so the file
  outlives the row for exactly as long as the row can come back. That is a different mechanism, not
  a longer list, and it is the remaining work here.

  Three things the receivers deliberately are not, each of which the obvious version gets wrong:

  - They do not cover `Image`'s columns. `services/media/images.py`'s `delete_stored_file` handles
    `image`, `thumbnail` and `marker_thumbnail` and knows when two rows legitimately share a file.
    **`analysis_thumbnail` is handled by neither** - a smaller, separate leak, recorded here rather
    than fixed in passing because the shared-reference rule needs the same treatment.
  - They are connected *per sender*. A sender-less receiver makes every model in the project report
    listeners, which disables Django's fast-delete path repo-wide and trips
    `test_bulk_write_signal_guard`.
  - They never unlink before the write commits. `post_save`/`post_delete` fire *inside* the
    transaction, so deleting there survives a rollback that puts the row back;
    `transaction.on_commit` defers each unlink until the write is real.

  Still open: **historical** orphans. This stops new ones for two of the four fields; a one-time
  sweep against surviving rows would close what is already there. For the icon families that is a
  disclosure item as well as a disk one, since `authorize_icon` is unconditional.
- **Unknown path families**: now **denied** and logged at WARNING (2026-08-29), and
  `check_media_authorizers` refuses to start with an unregistered family, so a new `upload_to`
  prefix cannot inherit a fallback either way.
- **`avatars/` (Profile.avatar)**: deliberately any-authenticated-user (avatars render site-wide
  next to usernames) - not a gap, but noted for completeness.
- **Safety check-in photos** (`Image.safety_checkin` set) currently follow the generic
  `Image.objects.visible_to` photo-visibility logic rather than the safety feature's own
  contact-sharing rules; if check-ins are ever shared with emergency contacts who fail the
  photo-visibility check, those contacts would be denied the photos (and vice versa: users
  passing `visible_to` but outside the check-in's audience can fetch them).

**Suggested next step**: product decision on icon visibility (owner-only + share-relationship vs.
authenticated-only), a cleanup job for orphaned media files (a disk-usage question now rather than
a disclosure one), and a review of safety check-in photo audience rules.

---

## P15 — openresty's 90s proxy cap cuts any Overpass query needing longer, whatever `[timeout:N]` asked for

`id: P15` · `status: open` · `updated: 2026-07-22`

Previously titled "Overpass deploy-side follow-up: raise the openresty 90s proxy cap (found 2026-07-22; edge box located 2026-07-23)".

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

## P16 — Aliases and label membership are still strictly per-pin, with no aggregation across child pins

`id: P16` · `status: open` · `updated: 2026-07-22`

Previously titled "aliases/labels aggregation, and boundary voting".

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

## P19 — Audit re-verification's residual gaps remain: a 1,100-line `_dark.scss`, a stub AI gateway, blocking AI in the request

`id: P19` · `status: open` · `updated: 2026-09-06`

Previously titled "Full-codebase audit: re-verification pass (2026-07-25)".

After the initial 35-unit audit (above) was worked through fix-by-fix in an earlier session, six
independent re-verification passes re-read every finding in `docs/audits/codebase-audit.md`
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
here so the next session doesn't have to re-derive them from `docs/audits/codebase-audit.md`'s
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
- **Unit 21/22/23**: ~~`models/pin/viewset.py`'s post-`get_object()` ownership re-check is still dead
  code (queryset already filters it)~~ - **kept deliberately, 2026-09-06, and now says so.** It is
  unreachable: `get_queryset` scopes to `profile__user`, so a stranger's pin 404s before either
  check runs. Deleting a redundant authorization check on a *write* path to satisfy a dead-code
  note is the change that ages badly - the day that filter widens (shared pins, an admin view) is
  the day a handler with no check of its own becomes the bug. Both sites carry a comment saying
  that, so the next reader files it as a backstop rather than as dead code again.
  `GroupMessage` still carries no images/markup_map/
  location_mentions/reply_to fields; `GameSessionConsumer`/`TriviaSessionConsumer` are still
  near-duplicate classes with no shared base, no per-connection rate limiting on any WS `receive()`.
- **Unit 24/25**: ~~the SpotGuessr/Trivia `eligible_locations()`/`eligible_questions()` retry loops
  still re-run the full query on every attempt instead of computing the eligible set once~~ -
  **already fixed, stale entry** (re-checked 2026-08-25): resolved well before this audit, by
  commit `d02fce8a` (2026-08-06, "Unit 24/25: resolve SpotGuessr round eligibility once instead of
  per retry"). `services/spotguessr/session.py`'s `generate_round_content` resolves
  `eligible_locations(...)` once into a plain id list before its retry loop, narrowing each
  attempt against `Location.objects.filter(pk__in=eligible_ids)` rather than re-running the
  multi-join query; Trivia's `get_or_create_round` already called `eligible_questions()` exactly
  once per round outside any loop. No moderation UI exists for AI-flagged trivia questions
  (decided against, not just unbuilt - see `docs/designs/drafts/trivia.md`'s "Known gaps"); Trivia
  gained a leave/kick path plus stall-handling parity with SpotGuessr on 2026-07-25
  (`services.trivia.session.leave_session`/`kick_participant`/`force_reveal_round`/
  `end_session_now`) - SpotGuessr itself still has no leave/cancel/kick path once a lobby exists.
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

## P20 — The legacy-CID repair leaves the CID on the wrong `Location`, so `by_cid()` resolves it wrongly for everyone

`id: P20` · `status: open` · `updated: 2026-07-25`

Previously titled "Residues left by the TEMPORARY legacy-CID coordinate repair (found 2026-07-25)".

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

## P21 — A shared markup map stamps provenance only for places its sender has pinned

`id: P21` · `status: open` · `updated: 2026-09-05`

Previously titled "`LocationWikiEditView.post` drops invalid wiki field edits and still answers
`{"ok": true}`", and before that "Messaging / external API (noted 2026-07-26)". Nine of this entry's
eleven sub-items are resolved and were removed on 2026-09-05 rather than left to be re-read - git
history has them. Two are live.

### A markup map only records what its sender already had a pin for

The original claim - that attaching a `MarkupMap` to a direct message records no `LocationExposure`
at all - stopped being true in `57a4a90af` (2026-08-27), a month after this entry was last touched.
All three attach paths now stamp the chain: the DM (`services/messaging/direct_messages.py` ->
`share_markup_map_with_profile`), the standalone map share, and the pin-share dialog. Group chats
cannot attach a map at all, so there is no second hole there.

What survives is the sub-question the original entry deferred, and it is the more interesting half.
`detect_shared_pins` matches the map against **the sender's own pins** (`_candidate_pins` filters
`profile=sender`). A map that marks a place the sender has never pinned produces no share and no
exposure - so the recipient learns the location and their onward share resolves `parent_share=None`,
ending the chain there. That is exactly the laundering pattern `share_provenance.py`'s own docstring
says the design exists to defeat: receive a share, never pin it, redraw it, forward it. It applies to
a cloned map too, whose new owner usually has no pin at the depicted place.

The asymmetry with the sibling path is the argument for fixing it: `dm_location_detection` already
mints a location-only `PinShare` (`pin=None`) plus an exposure for bare coordinates *typed* into a
chat. Drawing a marker on that same spot and attaching the map is the more precise disclosure,
arrives in the same message, and records nothing. `PinShare.pin` is already nullable and documented
for location-only shares, `place_label` already falls back to the location, and the Memories >
Sharing page already renders rows of that shape - so the model layer needs nothing.

**Two decisions to make before writing it**, which is why this is filed rather than done:

- **Which item types assert a place.** A placed marker or text label asserts one spot; a circle
  asserts its centre, but only below some radius (a 5 km circle asserts nothing). A line, arrow,
  square or polygon has no single defensible coordinate - a centroid is not what the sender pointed
  at - and those already contribute by matching against real pins. Minting locations for them would
  fill the chain with noise and inflate `chain_share_count`, which counts rows.
- **Whether the saved viewport counts.** `detect_shared_pins` already treats "zoomed in past the
  threshold, pin in the central quarter" as a share, so the map itself asserts its centre and the
  recipient can read it off the snapshot. Including it is what makes the record independent of the
  sender's own bookkeeping - and it changes behaviour for every map ever sent, so it wants its own
  decision rather than riding along.

A cap belongs on whatever ships: a map can hold hundreds of items, and `dm_location_detection`
already caps mentions at five per message for the same reason.

### Legacy `BLOCKED` rows may still record the wrong blocker

`Friendship` has no "blocked_by" column, so `from_profile` is the only record of who blocked whom,
and `block_profile` used to reuse whichever row already joined the pair - a block placed on an
inbound request left the *blocked* party as `from_profile`. It normalises direction now, so every
block placed since is right, but existing rows carry no signal a migration could use: it would have
to guess.

Impact on a legacy row is bounded and inverted from the original defect - the true blocker gets a 404
from `unblock_profile`/`remove_friend` and must re-block to normalise the row, and the blocked party
can lift it. `manage.py audit_inverted_friendship_blocks --before YYYY-MM-DD` reports the candidates
read-only, with no default `--before` on purpose: the fix's deploy date for a given production
database is something only a human knows.

## P22 — REData's `/api/v1/parcels/lookup/` crash-loops gunicorn workers with OOM/WORKER TIMEOUT on chiron

`id: P22` · `status: open` · `updated: 2026-07-31`

Previously titled "2026-07-31: REData's `/api/v1/parcels/lookup/` is in an OOM/WORKER-TIMEOUT crash loop on chiron".

Found while investigating the `resolve_deferred_pin_locations` retry-forever bug below - unrelated
endpoint, noticed in the same gunicorn log sweep on `redata-production-app-1`. Repeated `WORKER
TIMEOUT` followed by `SIGKILL` and worker respawn, i.e. requests to that endpoint are exhausting
memory or wall-clock badly enough for gunicorn's own supervisor to kill the worker. Not
investigated further - REData is a separate codebase/service another agent maintains (per
`CLAUDE.local.md`), and this session only had read access there. Whether this crash loop
contributed to or is independent of the CID-resolution backlog (both endpoints share the same
gunicorn workers, so one starving the other for memory is plausible) was not determined.

## P23 — The production celery worker's env sets `UL_SITE_URL=staging.urbanlens.org`, so built URLs point at staging

`id: P23` · `status: open` · `updated: 2026-07-31`

Previously titled "2026-07-31: Production celery worker's `.env` has `UL_REDATA_API_URL`... but check `UL_SITE_URL=staging.urbanlens.org`".

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

## P24 — A campus pin aggregates only the nearest CRIS building's media, not the survey's full USN roster

`id: P24` · `status: open` · `updated: 2026-08-05`

Previously titled "CRIS media on a multi-building campus is still only partial coverage (2026-08-05)".

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

## P25 — `Comment.profile` CASCADEs but `TripComment.author` SET_NULLs, so account deletion erases only some comments

`id: P25` · `status: open` · `updated: 2026-08-07`

Previously titled "Account deletion and the constraint-recreate class: both clean (2026-08-07)".

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

## P26 — A group message can still be sent under a stale key version, and refusing one risks an availability outage

`id: P26` · `status: open` · `updated: 2026-09-06`

Previously titled "E2EE group messages: the cryptographic membership boundary depends on the server (2026-08-07)".

`models/e2ee/group_key.py` states the design claim plainly: "Versioning is what enforces
membership boundaries **cryptographically**" - a removed member "is excluded from every later
version, so messages sent after their removal are unreadable to them". The server-side half is
well built: `needs_rotation` is computed by comparing the latest version's envelope set against
active membership, and the key endpoint refuses to store a version whose envelopes don't cover
that membership exactly.

**Half of this is fixed as of 2026-09-06, and P46 - the same defect filed again on 2026-08-16 - is
merged in here.** `create_group_message` now rejects a `key_version` that names no `GroupKey` for
this group, so the arbitrary-integer half is gone: a version the group never had, or one belonging to
a different group, is refused. One indexed lookup, on the `(group, version)` unique constraint. See
`test_group_key_version_is_real.py`.

**What remains open is the stale-but-real version, and it is a product decision rather than a
missing check.** The obvious fix - refuse a send whose version is behind the current one - has a
failure mode worse than the gap: rotation requires *every* member to be enrolled and answers 409 when
one is not, so a single un-enrolled member would stop the whole group from sending. That trades a
confidentiality gap for an availability outage. Options, in increasing cost: log when the send path
accepts a stale version; have clients re-check rotation state before sending rather than on poll; or
refuse stale-version sends only when the group is fully enrolled, so the 409 case cannot arise.

Note also what is *not* at risk in-app: `GroupMessageQuerySet.visible_window` bounds each member to
their membership stint, so a removed member cannot fetch the ciphertext however it was encrypted. The
exposure needs the ciphertext obtained another way - captured traffic, a database copy, a compromised
host - which is exactly the threat model end-to-end encryption exists for.

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

## P27 — Saved-filter regions use leaflet-draw's transactional remove tool, so deleted polygons resurrect on the next draw

`id: P27` · `status: open` · `updated: 2026-08-08`

Previously titled "Filter-view defects cluster: triaged, 3 of 5 already resolved (2026-08-08)".

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

## P86 — Deleting a contribution outright leaves its reputation points standing; the fix is a weight, not a retraction

`id: P86` · `status: open` · `updated: 2026-09-06`

Found 2026-09-06 while fixing P55's withdrawal half, and reproduced before being filed.

`ReputationEvent.target_id` is a plain `IntegerField`, not a foreign key, and
`models/reputation/signals.py` subscribes to **`post_save` only** - there is no `post_delete`
handler anywhere in the ledger. So deleting the row a contribution is about leaves its scored event
behind, still counting: a profile keeps points for a photo, comment, pin or wiki edit that no longer
exists.

`score_event`'s existing `retract_event(event, reason="target_deleted")` does not cover this. It
fires only when `resolve_target` comes back empty *during scoring* - i.e. when the target was
deleted inside the window before the deferred scoring task ran. That is a race, not a lifecycle
hook; a target deleted a day later is never revisited.

Reproduced with a scored `photo_upload` event whose `Image` is then deleted: the row survives, is
not retracted, and `total_value()` still counts it. That test is not in the suite - it would be a
red test for a known-open entry - but it is four lines against the fixture in
`test_reputation_withdrawal.py`.

**Why this is filed rather than fixed with the withdrawal half.** The obvious fix is a `post_delete`
subscription mirroring the `post_save` ones - the `_SUBSCRIPTIONS` tuple already maps model to rule
key, so it would be one loop and no new source of truth. It is wrong as stated, because
**`post_delete` does not know who deleted the row**, and this ledger's one-way rule turns on exactly
that: a contributor ending their own contribution loses the standing, and somebody else ending it -
a moderator removing a photo, a sweep, a cascade from a parent - must not. That is why
`detach_image_from_wiki` takes a keyword-only `withdrawn_by_contributor` with no default, and why
`revoke_community_bonuses_on_wiki_delete` is scoped to `deleted_by`, rather than either of them
inferring intent from the row.

**Answered 2026-09-07: yes, but only slightly, and reversibly.** See D9,
[`designs/reputation-removal-weighting.md`](designs/reputation-removal-weighting.md). This entry is
no longer blocked on a decision - only on someone implementing it.

**The answer rules out what this entry proposed.** `retract_event` is a boolean: it removes the
contribution's whole value. That is right for a withdrawal (P55) and is the opposite of "only very
slightly". Re-scoring is out too - `score_event` values an *unscored* row and re-running it would
re-apply diminishing returns and the caps.

What fits is a per-event **weight** applied where the total is summed rather than where the value is
written: a `weight` column defaulting to 1, `counting()` summing `value * weight`, and a removal
setting it near 1. Reversal is setting it back; re-weighting later is changing one constant, with no
migration and no lost history; and a future model can set per-event weights, which is the
granularity Jess asked for. `lifetime_earned` must be excluded from the weight for the same reason
it already ignores retraction - a moderator's removal should cost standing, never already-granted
access.

Both sub-questions are answered too. The number stays 0.9 ("nothing that isn't open to
re-assessment... no pressing need to research this currently"). A **cascade strips nothing** - "I'd
rather err on the side of keeping positive benefits awarded to users who contributed rather than
stripping them" - and `CascadeKeepsBenefitsTests` pins that, so a future blanket `post_delete`
cannot reverse it quietly.

**Built 2026-09-07**, and two of the premises turned out to need correcting - see D9's Status
section. In short: users *can* still delete a wiki (a detail pin **is** a child `Wiki`, and both
`Wiki.parent_wiki` and `Comment.wiki` are `CASCADE`), so the cascade case is live rather than
hypothetical; and no path currently exists by which anyone removes somebody else's scored
contribution, so the weight has no caller yet - it is the mechanism, in place for the first
moderation path that needs it.

What the work actually fixed was a different asymmetry it exposed: a contributor **deleting their
own wiki comment** kept its points, while withdrawing a photo retracted them. Same act, two
answers. `WikiCommentDeleteView` is author-only, so every deletion through it is a withdrawal, and
it retracts now.

**Still open here:** the general "an event outlives its target" case for deletions that are neither
a withdrawal nor a moderation - a cleanup command, say. Per the sign-off, anything that deletes in
future "can handle the fallout of what that means", erring toward keeping benefits.

**What is already fixed**, so this entry is not read as covering it: the *withdrawal* case -
`detach_image_from_wiki(..., withdrawn_by_contributor=True)` - retracts as of 2026-09-06 via
`services.reputation.scoring.retract_events_for_target`, and is covered by
`test_reputation_withdrawal.py`, including that somebody else's removal does **not** retract and
that `lifetime_earned` is unaffected.

## P29 — 186 write routes have no test naming them; the smoke sweep proves only that they do not 5xx

`id: P29` · `status: open` · `updated: 2026-08-13`

Previously titled "~187 write routes have no test that names them".

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

`pin.link` *was* exempted by name, with the exemption kept honest by
`test_the_known_crash_is_still_crashing`: when the product decision was made and the route fixed,
that test would fail and say so. **That is what happened.** As of 2026-08-18 the route answered 400
with an explanatory message instead of raising `IntegrityError` (and since 2026-08-30 the detach
action and its button are gone entirely - `pin.link` is GET-only and answers 405 to a POST), and
`tests/hypothesis/test_write_route_smoke.py` now reads `_KNOWN_CRASHES: set[str] = set()` - the
allowlist is empty and every write route is held to the no-5xx property with no exceptions. (Read as
written, the paragraph above sent a reader to an exemption that no longer exists; the mechanism it
describes worked exactly as intended, which is the point worth keeping. An exemption nobody
re-checks is how an allowlist rots into a blindfold - chunk 546.)

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

## P34 — 22,636 lines of inline template JS sit outside every automated check, with duplicated escaping helpers

`id: P34` · `status: open` · `updated: 2026-08-13`

Previously titled "Inline template JS: 21,543 lines, 14 escaping helpers, zero test coverage".

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

## P35 — Two named routes have no production caller; the other five the sweep flagged are reached by hardcoded path

`id: P35` · `status: open` · `updated: 2026-09-05`

Previously titled "Seven named routes still have no discoverable caller and remain unreviewed
authorised surface", and before that "Nine named routes with no discoverable caller".

From a 2026-08-14 sweep of all 753 named routes. 61 had no static reference outside `urls.py`; 30
are reached via `reverse(f"{prefix}.{suffix}")`, 34 live in `external_api/urls.py` where the
callers are API clients, and `password_reset_complete` is Django's own. The remainder were reviewed
one at a time on 2026-09-05, which is what the entry asked for. Three outcomes, and the middle one
is the interesting result:

**Not a route.** `add_review` is a commented-out line (`urls.py:398`, commented since `549c22537`).
`reverse("add_review")` has never resolved. Struck.

**Called, but by a path the sweep cannot see.** Each of these has a live caller that builds the URL
as a string rather than through `{% url %}`:

- `comment.locations` - `frontend/ts/shared/mention-autocomplete.ts:133`,
  which fetches `/dashboard/comments/locations/?q=...`
- `location.wiki.article.revision` and `location.wiki.article.restore` -
  `templates/dashboard/partials/articles/_article_history.html:30,37`, which append
  `{{ row.revision.id }}/` and `.../restore/` to a URL passed in as `scope.urls.history`
- `location.wiki.gallery.image` and `safety.checkin.gallery.image` -
  `templates/dashboard/partials/pins/_photo_gallery.html:192` builds `REPOSITION_BASE` from
  `{% url "location.wiki.gallery" %}` / `{% url "safety.checkin.gallery" %}`, and lines 429 and 519
  fetch `REPOSITION_BASE + imgId + '/'`. The `{% url %}` names the *collection* route; the detail
  route is reached by concatenation, which is why a search for its own name finds nothing

That is the finding worth carrying forward, and it cuts both ways: a route reached only by a
hardcoded path is invisible to this kind of sweep *and* breaks silently when the route moves. The
`{% url %}` tag exists so a rename is a build error rather than a 404 nobody notices.

**Genuinely no production caller** - two, both still open:

- `label.index` (`urls.py:1203`, `LabelKindIndexView`) - a whole page view. Its siblings
  `label.create`/`label.rows`/`label.edit` are live from the Organize page; only the index itself is
  unreachable, and only `test_query_scaling.py:96` names it.
- `dev_toolbar.toggle_map_dark_mode` (`urls.py:2134`) - dev tooling with no button; plausibly
  invoked by hand, which is why it is listed rather than deleted.

Both are authorised surface that has to be kept working and tested, so the choice for each is a
button or a deletion. Not made here: `label.index` is user-facing behaviour, and deciding a page
should not exist is the owner's call, not a sweep's.

**This entry got the two `gallery.image` routes wrong on 2026-09-05 before getting them right**, in
exactly the way it warns about: a per-route review that greps for the route's own name reproduces
the original false positive, because a concatenated URL never contains it. The tell was that both
had a *test* naming them and no caller - a shape that means "reached some other way" far more often
than "dead".

## P36 — 50 BEM modifiers are applied in templates with no CSS rule, so intended visual states never render

`id: P36` · `status: open` · `updated: 2026-09-05`

Previously titled "45 BEM modifiers applied in templates with no CSS rule", and before that "46".

`class="card card--secondary"` where `.card` is styled and `.card--secondary` is not renders as a
plain card. Each of these was written to create a distinction that does not appear, and nothing
errors, logs, or reviews badly - the modifier is spelled right and the base class really exists.

**The list now lives in `bin/check_bem_modifiers.py`, not here.** It is `_KNOWN_UNSTYLED`, the
check fails on anything outside it *and* on an entry that no longer reproduces, and it runs in
`bun run check` and in CI's frontend job. Transcribing the list into this file is what let it drift.

Re-measured 2026-09-05, against the 46 rows this entry used to carry:

- **8 added**: `album-card--readonly`, `assistant-msg--pending`, `detail-item--address`,
  `detail-item--official`, `detail-item--place`, `dm-conv-item--group`, `notif-item--friend-req`,
  `page-footer--map`.
- **1 fixed**: `badge--muted` now has a rule.
- **3 were never this**: `sv-img--fallback` and both `trip-panel-empty--*` are JavaScript selector
  hooks (`slide.querySelector(".sv-img--fallback")`), not visual states, and need no rule.

Three things the original measurement got wrong, each of which the check now handles:

- It read the compiled `style.css`. That is a build artifact, and it was five days behind the
  `.scss` sources on the day this was re-measured. The check compiles the sources, and refuses to
  fall back to an artifact older than them.
- Its tokenizer split `class="..."` on whitespace, so a class written flush against a tag was
  invisible to it: `class="page-footer{% if map_attribution %} page-footer--map{% endif %}"` yields
  the token `page-footer{%`. **Five** of the eight additions are that shape - `detail-item--address`,
  `detail-item--official`, `detail-item--place`, `dm-conv-item--group` and `page-footer--map` - which
  is more than half of the drift, and more than the three this entry first said (that count was taken
  from the literal `{% if %}`-adjacent pattern rather than by re-running the tokenizer).
- It counted JS hooks as missing styles, which is the opposite error and inflates the number.

Still open because what each should look like is a design decision, and deleting the modifier from
the template is as valid a resolution as writing the rule. Two of them are not even a fixed list:
`notif-item__icon-wrap--{{ n.notification_type }}` and `visit-source--{{ visit.source }}` generate a
modifier per value, so a new notification type arrives unstyled by construction. Every
`notif-item__icon-wrap--*` rule is scoped under `.notif-item--unread`, so no notification type is
coloured once read - which makes "three types are missing a rule" a larger question than it looks.
(An earlier revision of this entry also said those rules were all under `[data-theme=dark]`. That was
wrong, read off a truncated grep: of 42 selector lines in the compiled CSS, 24 are dark-scoped and 18
are theme-independent, from `_nav.scss:751`. Light theme does colour them.)

Worth doing first: the three `visit-*--pending` classes (a visit awaiting confirmation is
indistinguishable from a confirmed one), `ul-game-hud__group--lead` (the leading score, on all three
game pages), and `btn-icon--primary` - which is applied in `pages/site_admin_ui_components.html`,
the component gallery whose entire purpose is to show what each variant looks like.

---

## P37 — 100 write handlers totalling 1,217 statements never execute under the test suite

`id: P37` · `status: open` · `updated: 2026-09-08`

Previously titled "1,217 statements of write handlers that no test executes".

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
4. `consensus.py::ConsensusPhotoUploadView.post` [31], `visit_suggestions.py::VisitSuggestionRespondView.post`
   [31], `calendar_sync.py::CalendarImportView.post` [30].

`controllers/pin.py::PinController.upload_takeout` [39] is a special case: it is also on the
caller-less route list above, so it should be resolved (deleted or tested) before anything else -
two independent signals agree that nothing reaches it.

Caveats worth keeping attached to this number: coverage measures execution, not correctness, and
the run was scoped to `controllers/` and `external_api/`, so a service called by an uncovered
handler may itself be well tested.

**Corrected 2026-09-08:** item 4 previously also listed `controllers/albums.py::AlbumEditView.post`
[31]. `01e1b5988` ("test: wiki-owned albums, including the concealment path that had no coverage")
added `WikiAlbumBySlugScopingTests::test_a_concealed_viewer_cannot_rename_another_contributors_album`
and `::test_an_unconcealed_viewer_can` in
`src/urbanlens/dashboard/tests/hypothesis/test_wiki_albums.py`, both of which POST a rename through
`AlbumEditView`, so that handler is exercised now. Removed from the roster rather than left to imply
it is still uncovered; the other three items in this bullet and the rest of the 100-handler count are
not re-measured this session, so treat only this one line as updated. The underlying snapshot in
`docs/reports/2026-08-14-view-coverage.md` (X12) is left as-is - it is a dated measurement, not a
live roster.

---

## P41 — The queryset API's unused half, by call graph: 26 methods deleted, 27 test-only ones left

`id: P41` · `status: open` · `updated: 2026-09-06`

Previously titled "68 of 249 public queryset methods have no production caller, so their logic may
be duplicated inline elsewhere", and before that "Queryset API with no production caller: 70 of 251
(candidate count)".

The 2026-08-14 name-grep sweep found 70 of 251 and this entry asked for a call graph instead. Done
2026-09-06, by AST over every `.py` in `src/`, `bin/` and `tests/` plus every template and every
string constant. It disagrees with the grep in both directions, which is the point of the exercise:

| | |
|---|---|
| public methods on a `*QuerySet`/`*Manager` class under `models/` | **424 definitions, 278 distinct names** |
| called from production | 210 |
| called only from tests | 27 |
| used only inside its own file | 11 |
| name appears only in a string or a template | 9 |
| **no reference anywhere in the repo** | **21 names, 28 definitions** |

The first sweep counted 251 methods by scanning `queryset.py` only. Widening it to every module
under `models/` adds `ImageAttachmentQuerySet` and its siblings - and one of the additions,
`for_image`, turned out to be a *second* dead copy of a name already dead in `facts/queryset.py`.
Four more names were dead in two or three places each: `by_latitude`, `by_longitude`,
`by_created_year` and `by_updated_year` are defined on `PinQuerySet`, `LocationQuerySet` and (for
the year pair) `WikiQuerySet`, and none of the eleven definitions had a caller. So 21 dead names are
28 dead definitions, and a name-keyed count understates the cleanup by a third.

**26 definitions deleted.** Every one was a single `filter()` or `exclude()` wrapper. The eleven
`by_*` ones carried no type hints and no docstrings.

**Two were kept, because they are the case this entry was really about** - a method whose logic
somebody rewrote by hand somewhere else, so the rule now exists twice:

- `ProfileQuerySet.pending_deletion` is `filter(deletion_requested_at__isnull=False)`, and its two
  siblings in the same class - `due_for_deletion_reminder` and `due_for_hard_delete` - each opened
  by writing that predicate out again. Both chain the method now.
- `ArticleQuerySet.with_content` is `exclude(content="")`, written out by hand in
  `services/global_search/providers.py`. That call site uses the method now.

**Three near-misses, deleted anyway, and the distinction is worth keeping.**
`PlaceQuerySet.part_of_children`/`member_of_children` look like they were reimplemented at
`services/places/splits.py:113` and `models/place/queryset.py:207`, but both of those filter on a
*specific* parent (`parent_id=place.pk`, `aggregate.children`), while the methods mean "any place
whose parent edge is PART_OF". Routing either call site through the method would have added a
redundant `parent__isnull=False` to make a worse fit look like reuse. `LocationQuerySet.in_domain_of`
is the same shape against a migration, which must not call a queryset method at all.

**Still open, in rough order of how much judgement each needs:**

- **27 called only from tests.** The largest remaining group and the one this entry's earlier
  warning is about: several are the `filter_by_criteria`-style aggregators' building blocks, and a
  test that exercises the aggregator does reach them - just not by name. `by_name`, `by_priority`,
  `by_tag`, `rated`, `rated_over`, `rated_under` and `overlapping` on `PinQuerySet` are the bulk.
- **9 whose name appears only in a string or a template.** Four were spot-checked and all four are
  false positives of the string check rather than real reuse: `cloned_from` is also a model *field*
  name, `search_visible_to` appears in a docstring, and `rate_limited` collides with an unrelated
  constant in two gateways. They are probably deletable; each needs its own look.
- **11 used only inside their own file.** Not dead - `apply_label_groups` is the example this entry
  already carried - but arguably mis-scoped as public API rather than `_`-prefixed helpers.

**What the call graph does not see**, checked rather than assumed: this tree has no `getattr(qs, ...)`
dynamic dispatch and no django-filter `method="..."` naming a queryset method (the only `method=`
strings in `src/` are `"get"` and `"post"`), so the trap that hid `Pin.by_category` from the original
sweep is not present any more.

## P47 — A deleted message's preview survives in the recipient's notification list

`id: P47` · `status: open` · `updated: 2026-08-16`

Fixed in chunk 572: the *delayed email* and *delayed WhatsApp/SMS alert* for a direct message now
skip a message the app would show as a tombstone, so unsending inside the 120-second delay window
stops the out-of-band copy going out.

Not fixed, because it needs a schema decision: the **on-site notification** raised for the same
message keeps its preview text.

- `services/messaging/direct_messages` stores `message=preview` - up to 120 characters of the body.
- `services/messaging/group_chats` stores `message=f"{sender}: {preview}"`, likewise 120.

Neither is touched by `delete_message_for_everyone` / `delete_group_message`, so after the sender
unsends, the thread shows "Message deleted" while the notification row still quotes what was said.

**Two things narrow this, both checked 2026-09-05 and neither stated above.** An encrypted message
never had a plaintext preview to leak: both services branch on `is_encrypted` first and store
`"🔒 Encrypted message"` (`direct_messages.py:392`, `group_chats.py:453`), so this is a plaintext-DM
defect, not an E2EE one. And a DM notification is raised only when there is no *other* unread
message from the same sender (`already_unread`, `direct_messages.py:388`), so a conversation holds at
most one stale preview per sender rather than one per deleted message.

That does not make it a non-issue - one quoted sentence is the whole of what "unsend" is supposed to
undo - but it does mean the fix is smaller and less urgent than "every deleted message leaks".

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

## P49 — Doc citations drift silently, and a pin-suggestion race can still duplicate a row

`id: P49` · `status: open` · `updated: 2026-09-05`

Previously titled "`npm run git-squash` is a force-deploy with none of `deploy.sh`'s dirty-tree
guards", and before that "... none of `deploy.sh`'s guards (minor)". That half is fixed; what is
left is the two sub-items below.

### `npm run git-squash` (noted 2026-08-17) - fixed 2026-09-05 by deleting it

`package.json` defined:

```
"git-squash": "pkill gunicorn && git fetch origin && git reset --hard origin/main && npm run start"
```

The original entry called this "neither urgent" and left it, on the grounds that the behaviour might
be exactly what its author wanted at a terminal and `bin/deploy.sh` was the safe path. Both halves of
that turned out to be wrong.

`bin/deploy.sh` is not in this repository. It moved to the sibling `infrastructure` repo in
`ff332e484`, along with the rest of the host-side ops tooling, so the comparison the entry drew was
against something that had already left - and the script it compared was the last of that tooling
still here.

And `pkill gunicorn` matches by process name across the entire host, not within a project. Run on
this development box it matches five gunicorn masters, every one of them belonging to a *different*
application; on a host running the compose stack it reaches the ones inside the containers, because
container processes are visible in the host's PID namespace. The remaining `&&` chain then hard-resets
whatever checkout it happens to be run from - a tree where, per `CLAUDE.local.md`, more than one agent
session works at once. The failure the entry did note (a non-zero `pkill` aborting the chain when
nothing matched) is what had been hiding the rest: on a host with no gunicorn at all it stopped
harmlessly at the first command, which is not the same as being safe.

Deleted rather than guarded. Nothing referenced it, `infrastructure/bin/deploy.sh` does the same job
with a lock, a branch check, a dirty-tree refusal and a health wait, and re-adding a host-side deploy
script here would undo the move that `ff332e484` made deliberately.

### Pin suggestion `hit_count` is a read-modify-write (noted 2026-08-17) - lost-increment half fixed 2026-08-25

`services/pins/pin_suggestions.py`'s `_upsert_matched_suggestion` and
`_upsert_new_pin_suggestion` did `existing.hit_count += _weight_of(...)` and save, and their caller
`ingest_location_hits` takes no lock. Two concurrent ingests for one profile - a repeated Immich
sweep overlapping a local-scan upload, which the function's own docstring names as the case it
handles - could lose an increment, and can also both miss on the check-then-act and create
duplicate pending suggestions for the same pin.

**The lost-increment half is fixed** (`996b481f`): both call sites now do `existing.hit_count =
F("hit_count") + _weight_of(...)`, compiling to a single atomic UPDATE, with
`existing.refresh_from_db(fields=["hit_count"])` immediately after per Django's guidance for
F()-expression fields (the object is reused across multiple clusters within one
`ingest_location_hits` call). **The duplicate-pending-suggestion half from the same check-then-act
race is still open** - scope was deliberately limited to the named `hit_count` pattern; the other
merged fields (`visit_dates`, `sample_assets`, `suggested_aliases`/`links`) are still Python-side
list/JSON read-modify-writes, unaddressed. Left unfixed deliberately, as originally: `PinSuggestion`
rows are per-profile, so contention needs one user running two scans at once, and the remaining
damage is a duplicate low-stakes suggestion row rather than lost money or a discarded rating
period.


### Documentation citations still point at the wrong line (noted 2026-08-17)

`bin/check_doc_line_refs.py --report-drift` lists them. They survived the 2026-08-17 sweep because
they can't be repaired mechanically, and they split into two kinds:

- **The line moved, but the anchor isn't a definition.** `settings/base.py:343` for
  `hard_delete_expired_direct_messages` is cited via its entry in the beat-schedule dict, and
  `tasks.py:1629` for `RUN_LOCK_CACHE_KEY` via an import. Renumbering these is safe but needs a
  human to confirm which usage was meant - and the number moves faster than the repair does: this
  entry recorded 374 as the answer for the first one, which is now 552.
- **The symbol no longer exists at all.** `controllers/trip.py` line 135 cites `_mask_trip_identities`
  and `services/ai/anthropic.py` line 117 cites `send_prompt`/`send_prompt_list`; neither name appears
  anywhere in the tree now. (Written without the usual `file.py:line` punctuation on purpose: these
  are quoted *examples* of broken citations, and `bin/check_doc_line_refs.py` cannot tell a quoted
  one from a live one.) The repair is rewriting the sentence around whatever replaced them, not
  changing the number - and guessing at that would put invented history into the record.

The eight that *were* mechanically provable (anchored on a `def`/`class` the tool could locate
uniquely) are fixed, and `check_doc_line_refs.py` now runs in CI to keep past-end-of-file citations
at zero.

The "fourteen" in this entry's original title was never a count of what needs repairing, and the
number the report prints is not one either. Re-measured 2026-09-05 at 569 suspected drifts, of which
**29 are in `PROBLEMS.md`** - the live file someone acts on. The rest are in `archive/`, `audits/`,
`reports/` and `designs/`, which record what was true on a date; renumbering one of those would make
it cite code that did not exist when it was written, so the right number of repairs there is zero.
The same argument applies one sentence wide, to a citation inside `~~struck~~` text, and the drift
report now skips those (the past-end-of-file check still covers them - being unfollowable is a
different problem from being out of date).

Two shapes of false positive were in the count and are gone: the report used to pair a citation with
whatever backticked names shared its *line*, which on wrapped markdown is usually not its subject,
and it could not see an anchor written `plan_merge_conflicts()` or `MediaKind.VIDEO` at all. Reading
the whole wrapped block instead surfaced real drift the old version missed - including this entry's
own worked example.

## P50 — `test_safety_chat` and `test_migration_0039_reverse` fail only under a randomized suite order

`id: P50` · `status: open` · `updated: 2026-09-05`

Previously titled "Two tests fail only under a randomized full-suite run (2026-08-18)".

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

**Probed 2026-09-05, did not reproduce - which is not the same as fixed.** A shuffled run of both
named files together with the neighbours most likely to leak into them (`test_field_encryption_rotation`,
`test_two_factor`, `test_e2ee`) passed: 155 tests, 3 subtests, no failures. Two things that probe
does not settle. The original was seen in a *full-suite* shuffle, and the interfering test may
simply not be in this subset. And one plausible cause was closed in between: the safety-chat half
was a `TransactionTestCase` carrying its own `@override_settings(CHANNEL_LAYERS=...)`, and that
decorator is gone as of 2026-09-05 - `settings/test.py` sets the in-memory layer globally now (see
the archived P17 note), so there is no longer a per-class override to interact with anything.

~~Also worth knowing before the next attempt: **`pytest-randomly` is not installed in the
test-runner container** (see P4), so `bin/run_tests.sh --shuffle` cannot reproduce this at all.~~
Fixed 2026-09-05: `bin/run_tests.sh` now installs the dev-group packages its own container is
missing rather than warning about them, and `pytest-randomly 4.1.0` is present. `--shuffle` works,
so the next attempt is a full shuffled run - `bin/run_tests.sh --shuffle` without `-q`, so the
header records the seed this entry says the original run lost.

## P51 — Native `<select>` popups stay light-on-light in dark mode despite `color-scheme: dark`

`id: P51` · `status: open` · `updated: 2026-08-22`

Previously titled "Native `<select>` popup stays light-on-light in dark mode despite `color-scheme: dark` (2026-08-22)".

The floorplan editor's opening-type `<select class="form-input">` (and likely every other bare
`<select>` on the site) is illegible in dark mode: unselected `<option>` rows render as light grey
text on a light grey popup background. `_dark.scss` already had `[data-theme="dark"] select {
color-scheme: dark; }` for exactly this (the standard fix - native option-list popups are
browser/OS chrome, not the page's own CSS box, and `color-scheme` is the documented hook for
telling the browser to use its dark native-widget palette there). Verified live that the rule
does land - `getComputedStyle(select).colorScheme` reports `"dark"`, and `data-theme="dark"` is
present on `<html>` - yet the popup still rendered light in both `--headless=old` and
`--headless=new` Chromium.

Added `color-scheme: dark;` directly on `[data-theme="dark"]` itself too (not just nested under
`select`), on the chance an engine reads the document root's scheme rather than each control's for
this - still no visible change under either headless mode.

This didn't budge across two different Chromium rendering paths with the CSS verified correct, so
the remaining suspect is Linux/GTK-specific: Chromium's `<select>` popup on Linux is known to
sometimes defer to the system GTK theme for the dropdown list chrome rather than the page's
`color-scheme`, independent of what the page declares. If that's what's happening, no page-level
CSS can fix it - the actual fix would be replacing the native `<select>` with a custom-styled
dropdown (`<ul>`/`<div>`-based combobox), which is a real UI component to build, not a styling
tweak, and wasn't attempted here since it's out of scope for what looked like a small dark-mode
fix.

Left the `color-scheme: dark` additions in place (correct regardless, and may well fix this on
Windows/macOS Chrome, which are more likely to honor it than Linux Chromium's GTK-backed popup) -
someone should verify on a non-Linux browser whether this is actually resolved there before
deciding whether the custom-dropdown rewrite is worth doing.

## P82 — At exactly 768px the nav needs 837px, so a tablet-width viewport still scrolls sideways

`id: P82` · `status: open` · `updated: 2026-09-06`

The phone half of this is fixed (see the archived P52). What remains is one breakpoint up.

`$breakpoint-sm` is 768px, and `down()` compiles to `max-width: 767px` - so at exactly 768 the seven
primary links appear and the hamburger does not. Measured in Chromium against the running
`development_main` stack, on `/dashboard/`: `document.documentElement.scrollWidth` is **837** in a
768px viewport. The bar spends 18px of padding, 116 on the brand, about 470 on the links and 227 on
the right-hand group.

It predates the P52 fix rather than being caused by it: 837 reproduces on the unmodified stylesheet,
measured by stashing the change and rebuilding. Below 768 the brand absorbs the shortfall, which is
why the phone widths are now clean; at 768 the links are what would have to absorb it, and
truncating or scrolling a navigation menu is worse than the overflow.

**Not fixed because the ways out are product calls, not layout fixes.**

1. **Raise the hamburger breakpoint to `$breakpoint-md`.** One line, and it works - the links hide
   and the bar collapses to the phone layout, which fits with room to spare. It also means a
   1000px-wide laptop window gets a hamburger, which is a real change to how the application reads
   on the machines most likely to be running it.
2. **Shorten the link row.** Seven top-level sections is what makes it 470px wide. Which of them are
   top-level is an information-architecture decision.
3. **Let the link row scroll horizontally inside itself.** Keeps every link reachable and stops the
   page scrolling, at the cost of an affordance nobody sees - a scrollable row with no visible edge
   is a row whose last item does not exist as far as most users are concerned.

`specs/ui/responsive-overflow.spec.ts` covers 320-414 only, deliberately: adding 768 would ship a
red test for a decision nobody has made. Extend `PHONE_WIDTHS` when this is resolved.

## P53 — The Private Pin page's opening burst is bounded now, but its tail is 15 seconds longer

`id: P53` · `status: open` · `updated: 2026-09-06`

Previously titled "One Private Pin page load fires dozens of concurrent panel requests and can
exhaust the DB connection pool", and before that "one Private Pin page load can exhaust the database
connection pool".

Found by `tests/integration/` on 2026-08-24, and only visible because the console/network guard
watches every request a page makes rather than just the document. Opening
`/dashboard/map/pin/<slug>/` fired every enrichment panel at once, and each one is a Django request
taking its own database connection (`CONN_MAX_AGE` is 0). On the dev stack, whose Postgres runs the
default `max_connections = 100`, 14 requests in one hour failed with `FATAL: sorry, too many clients
already`, spread evenly across seven different panel endpoints - the signature of pool exhaustion
rather than of any one panel being broken.

**Re-measured 2026-09-06 in Chromium against the running `development_main` stack, and it was worse
than "roughly thirty": 95 requests in total, 61 of them within the first two seconds, peaking at
**58 simultaneous**.** Two readers is enough to want 116 connections against a pool of 100.

**Bounded 2026-09-06 with `hx-sync` lanes**, which is htmx's own answer and needs no JavaScript:
requests naming the same element with `queue all` run one after another. The 27 enrichment panels on
that page - the external-data panels, the collapsible sections, and the media gallery's 13
per-provider loaders - are spread across four panel lanes and three media lanes. Measured after: the
same page peaks at **27**, and reaches an identical settled state (same gallery contents, same
visible panels, no console errors).

**Two things that were tried first and cannot work, recorded so the next person does not spend the
same afternoon on them.**

- **`revealed` / `intersect` deferral is impossible here.** Every one of these panels renders with
  the `hidden` attribute and only unhides once its content arrives, so it never intersects the
  viewport and would never fire at all. Measured: of 46 load-triggered elements on a real page, 40
  were hidden and 0 were in the viewport. The five tab panels that *were* converted to `revealed`
  earlier are a different shape - they are laid out, just off-tab.
- **A `delay:` stagger bounds the rate, not the concurrency.** Starting four panels every 400ms
  still leaves fifty in flight if each takes five seconds, which on a cold cache they can.

**Still open, and this is now the interesting half: the tail.** Serialising makes the last panel
arrive later. Measured on a cold cache: the unlaned page had settled by 30 seconds and the laned one
needed 45. The end state is identical, so nothing is lost - but a first visit to a location nobody
has opened before now takes noticeably longer to fill in, and that trade was made here without
anyone deciding it was the right one. Three ways to shorten it, none free:

1. **More lanes.** Six panel lanes instead of four cuts the tail by about a third and raises the
   peak by two. Cheap, and a straight dial between the two costs.
2. **Fewer panels.** Twelve of the 27 are plugin panels that 204 on most locations - a page that
   asked one endpoint "which of these have anything?" could skip the rest entirely.
3. **Make the panels cheaper.** The tail is a cold-cache figure; a warm one collapses it. Which
   suggests the external-data cache, not the fan-out, is what a returning user actually feels.

**What holds it.** `test_pin_detail_fanout_budget.py` still ratchets the count, but the count is no
longer the concurrency and the lane assertion is what matters now: every enrichment panel must name
a lane, because one added without `hx-sync` re-opens the problem while leaving the count green.
Verified to gate - 27 unlaned before the change, 0 after.

The remaining ~27 concurrent requests are the page's own content (overview, gallery, boundary,
markup and detail-pin JSON), the site chrome (notifications, undo stack, safety banner), and the
five off-tab panels. Laning those would delay the page itself, which is a different trade.

## P55 — A withdrawn contribution keeps its reputation event, and the wiki gallery's delete strings are false there

`id: P55` · `status: open` · `updated: 2026-09-06`

Previously titled "Deleting a whole wiki still withdraws a contribution without ending its quota
bonus", and before that "A community quota bonus survives un-sharing the photo that earned it".
Both of those halves are fixed; these two adjacent ones are what is left.

**What the quota rule now is**, since the next reader will want it in one place.
`QuotaExemption.COMMUNITY_CONTRIBUTION` is taken back exactly when a contributor ends their own
contribution, and kept in every other case - votes withdrawn, the photo removed by someone else, the
low-engagement sweep. Those are indistinguishable at the column (each ends as `wiki_id IS NULL`,
with no record of who did it), so intent is stated by the caller and never inferred:
`detach_image_from_wiki` takes a keyword-only `withdrawn_by_contributor` with no default, and
`revoke_community_bonuses_on_wiki_delete(wiki_ids, deleted_by=...)` scopes a whole-wiki delete to
`profile=deleted_by` so every other contributor's bonus survives it.

The wiki-delete half closed on 2026-09-06. It needed three things beyond the one-line revoke, which
is why it was filed rather than folded into the original fix, and all three are now in place: the
revoke covers the cascaded subtree (`with_wiki_descendants`, not just the named wiki); it runs after
the undo stash and before the delete, since the delete nulls the FK it reads; and
`WikiUndoHandler.serialize` records which photos held the bonus (`bonus_image_ids`) so `restore`
re-grants exactly those and invents none. Re-granting from the vote count instead was the obvious
alternative and is worse: a threshold raised during the seven-day undo window would silently not
restore a bonus, which is the same "somebody else's action" the one-way rule exists to prevent.

The fourth thing is what the first pass at this missed, and it is the shape to expect from any
delete whose fix does more than delete: **redo is a second copy of the delete**, and the inherited
`UndoHandler.redo_delete` only re-deletes rows. Delete, undo, redo left the photo private with the
bonus standing - the original defect, three clicks in instead of one. `restore` therefore records
what it actually handed back (`regranted_image_ids`, written into the stashed entry, which
`restore_undo_action` persists) and `WikiUndoHandler.redo_delete` takes back exactly that; by then
a re-granted exemption and one that was never revoked are indistinguishable in the column.

~~**Still open, one.** The reputation ledger keeps the `ReputationEvent` for a withdrawn
contribution.~~ **Fixed 2026-09-06.** `retract_events_for_target` (in `services.reputation.scoring`,
beside the `retract_event` it builds on) is called from `detach_image_from_wiki` when
`withdrawn_by_contributor` - the same intent flag, at the same point, as the quota revoke beside it,
so the two halves of one withdrawal cannot drift apart. Both branches are covered: the photo is kept
when it is still on a pin and deleted when it is not, and the contribution has ended either way.

The two tests that constrain the fix matter more than the one that detects the bug: **somebody
else's removal must not retract** (`withdrawn_by_contributor=False`), which is the one-way rule this
entry states, and **`lifetime_earned` must not fall**, since `recompute_total` is explicit that
reverting contributions cannot take away access a user already had.

**And it is not the only way an event outlives its subject.** `target_id` is a plain `IntegerField`
and the ledger subscribes to `post_save` only, so *deleting* a contribution outright leaves its
points standing too - reproduced, and filed as P86 rather than fixed here, because `post_delete`
cannot tell a contributor's own deletion from a moderator's and this entry's one-way rule turns on
exactly that distinction.

**Still open, two.** In the wiki context the shared `galleryDelete` handler still prompts "This
cannot be undone - the file is removed permanently" and toasts "Photo deleted." for a dual-owned
photo the server only unlinks. Both strings are false there, and correcting them means changing a
response shape three contexts share, so it wants a browser.

**Not doing the "record the bonus as an amount" redesign**, and the reason is worth keeping because
it reads like the obvious fix: it does not fix anything here. A per-wiki credit row with the same
missing revoke survives an unlink identically - the defect was that nothing revoked, not that the
exemption is a flag. It also costs: `get_storage_used_bytes`, `get_exempt_bytes` and
`get_storage_totals` each answer in one aggregate over `Image` served by `idxdb_image_profile_quota`,
and a credit table makes all three a second aggregate plus a join. And an amount frozen at grant time
drifts from the file that earned it, because `file_size` is rewritten later
(`strip_exif_from_stored_photos` does exactly that). It becomes genuinely necessary only if a credit
should outlive the photo that earned it - a balance that survives its photo cannot be a flag on that
photo's row - which is a product question, not an implementation one.

## P56 — `Cross-Origin-Embedder-Policy` is report-only pending one measurement; `require-corp` is ruled out

`id: P56` · `status: open` · `updated: 2026-09-05`

Previously titled "`Cross-Origin-Embedder-Policy` is unset, and the third-party host inventory needed
to set it does not exist", and before that "Nuclei scan follow-ups (2026-08-28)".

The inventory this entry was waiting on, measured 2026-09-05 by requesting a representative asset
from every host `_CSP_DIRECTIVES` admits and reading the response headers:

| host | used for | under `require-corp` |
|---|---|---|
| `code.jquery.com`, `cdnjs.cloudflare.com`, `unpkg.com`, `cdn.jsdelivr.net` | scripts | `CORP: cross-origin` |
| `maps.googleapis.com` (the JS API itself) | script | `CORP: cross-origin` |
| `fonts.googleapis.com`, `fonts.gstatic.com` | styles, fonts | `CORP: cross-origin` |
| `www.google.com` (favicons), `maps.gstatic.com` | images | `CORP: cross-origin` |
| `tile.openstreetmap.org`, `basemaps.cartocdn.com`, `tile.opentopomap.org`, `server.arcgisonline.com`, `services.arcgisonline.com` | map tiles | no CORP, `ACAO: *` - needs `crossOrigin` on the Leaflet layer |
| `www.gravatar.com` | avatar preview | no CORP, `ACAO: *` - needs `crossorigin` on the `<img>` |
| `en.wikipedia.org`, `nominatim.openstreetmap.org` | `fetch()` | no CORP, `ACAO: *` - already fine, `fetch` is CORS-mode by default |

So every scripted resource already passes, and the nine that do not are all `ACAO: *` and reachable
with an attribute change. **That is not the blocker, and the entry was wrong about what was.**

**The blocker is `img-src: https:`.** Map image overlays are a paste-any-URL feature
(`_map_overlays_list.html`, `map-image-overlays.ts`), which is why that directive is deliberately
wide open - see the comment on it. Under `require-corp` every overlay whose host sends neither CORP
nor CORS stops rendering, and that host set is unbounded by design. No inventory can close this,
because the inventory is "whatever a user pasted".

That points at `Cross-Origin-Embedder-Policy: credentialless` rather than `require-corp`:
credentialless sends no-cors subresource requests without credentials instead of demanding CORP, so
a pasted image still loads. ~~Evaluating it - and its browser support, which is narrower - is the
next step~~ **- evaluated 2026-09-06, and it is deployed report-only.**

**`credentialless` is the variant this app could enforce, and sending it costs nothing today.**
Support is 79% globally (caniuse, 2026-09-06) and Safari does not implement it on any version -
desktop through 27, iOS through 26.6. That is survivable rather than disqualifying, because the
HTML spec's "obtain an embedder policy" fails open: a token the browser does not recognise leaves
the policy at `unsafe-none`. So Safari users get no COEP and nothing breaks, which is exactly where
they are now.

**The nine attribute changes this entry lists are a `require-corp` requirement, not a prerequisite.**
Under `credentialless` a no-cors tile loads as-is. Measured in a browser against the dev stack on
2026-09-06 with `Cross-Origin-Embedder-Policy-Report-Only: credentialless` live: the map page loaded
48 Leaflet tiles from `server.arcgisonline.com` and `*.tile.openstreetmap.org` with `crossorigin`
**unset**, plus scripts from unpkg/cdnjs/code.jquery.com and fonts from Google, with zero violation
reports and zero failed requests. Do not spend a batch adding `crossOrigin` attributes for this.

**Report-only, not enforced, and the reason is a measurement nobody has taken.** The one behaviour
`credentialless` changes that the probe above cannot see is the Street View embed iframe, which
would load without the viewer's Google credentials - and it needs a valid Maps API key to observe at
all, the same key the two unmeasured rows below need. `CROSS_ORIGIN_EMBEDDER_POLICY_REPORT_ONLY`
(settings/base.py) is what to flip, and `SecurityHeadersMiddleware` is where it is attached.

Worth stating plainly, since a scanner is what raised this: COEP buys defence in depth here, not a
fixed vulnerability. Nothing in this app asks for cross-origin isolation - no `SharedArrayBuffer`,
no `crossOriginIsolated` - so the header's value is confining what a compromised subresource could
read, not unlocking a capability.

**Two things could not be measured** and need a valid Google Maps API key: the Street View embed
iframe (`https://www.google.com/maps/embed/v1/streetview`, which answered 403 to a keyless probe),
and the imagery hosts the Maps JS API picks at runtime (`khms0.googleapis.com` 404,
`streetviewpixels-pa.googleapis.com` 403). The `img-src` comment already flags that this set is
"the known set rather than a proven-complete one". A report-only COEP deployment is what would
settle both.

## P57 — The test-quality audit's follow-ups: 13 done; three untested surfaces, two unproven locks and two decisions remain

`id: P57` · `status: open` · `updated: 2026-09-06`

Previously titled "Test-quality audit follow-ups (2026-08-29)".

Found while auditing existing unit tests for real positive/negative coverage (see
`docs/notes/test-quality-audit.md`); out of scope for a test-file-only pass, noted here per
convention rather than fixed inline.

**Thirteen are fixed as of 2026-09-06** - the `connect_ex` guard (which turned out to be two holes), the
`make_cache_key` collision, the hard-delete overlap lock, the `SubscriptionRole.clean()` gap, and
`PinAliasView.post` (same-day, 2026-08-29). Each is struck through below with what the fix found.

Three of the "untested surface" entries are covered as of 2026-09-06 too - `WikiBoundaryView`,
`purge_old_backups`'s count-based retention, and `RedataBasemapTilesGateway.list_sources` - and
**writing those tests found two live defects neither this entry nor anything else had noticed**:

- **`parse_multipolygon_geojson` turned most malformed GeoJSON into a 500.** Its
  `except (GEOSException, TypeError, ValueError)` did not name `GDALException`, which is not a
  `GEOSException` subclass and is what `GEOSGeometry` actually raises for a bare `{}`, an unknown
  `type`, or a `Polygon` with no `coordinates` - because it parses GeoJSON through OGR. Seven
  features reach that parser, including `external_api/views_wiki.py` and
  `external_api/serializers.py`, so this was a 500 on the public API for an ordinary malformed
  request. Fixed, along with `{"type": "Polygon", "coordinates": []}`, which parses *cleanly* into
  an empty geometry - the exact trap `dissolve_polygons` documents two functions below, where an
  empty polygon in a `__within` lookup matches zero rows instead of imposing no restriction.
- **The AI-gateway guard mocked out the method its own test file exists to test.** `ai_guard.py`
  (added 2026-09-06 for P78) patches `LLMGateway.send_with_tools` for the whole session, so
  `test_ai_gateway_tool_calling.py`'s six tests asserted against a call that never happened and had
  been failing since. `real_ai_chokepoint(target)` restores one named chokepoint for a test whose
  subject *is* that method, leaving the rest of the guard - and the socket guard and the placeholder
  credentials - standing. `test_ai_gateway_guarded` still passes, which is what proves it.

What remains: **three untested surfaces** (`CalendarImportView`, the carousel's "no imagery
available" branch, and the multi-level pin/wiki nesting prefix), two stale-documentation items, two
locks with no real-concurrency proof, and two that need a decision from whoever owns the area.

*(An earlier version of this line claimed every untested surface was covered. That was written
after reading only the first half of this entry and is wrong - the five above are all listed
below it. Corrected 2026-09-06.)*

Worth noting about this entry's own hit rate: it filed the AI trip tools as tidy-up ("duplicated
business logic ... can silently drift"), and they were a live permission bypass. Two of the three
"untested surface" items turned out the same way. An entry that says only "this is untested" is not
a statement that the code is correct.

~~**`LocalhostOnlyNetwork` (`core/testing_network.py`) doesn't patch `socket.socket.connect_ex`.**~~
**Fixed 2026-09-06, and it was two holes rather than one.** `connect_ex` is a separate C-level
method that does not delegate through the patched `connect()` - and a **UDP `sendto` never connects
at all**, so it was equally invisible to a guard watching `connect`/`create_connection`. Both are
patched now. Both were reproduced first: against the old guard,
`test_blocks_external_connect_ex` and `test_blocks_external_udp_sendto` fail while their
localhost anti-vacuity siblings pass.

~~**`make_cache_key` (`core/cache_keys.py`) joins parts with a bare colon before hashing.**~~
**Fixed 2026-09-06** with the length-prefixed encoding this entry suggested. It was latent rather
than live - none of the five call sites (pin lat/lng, location formatting, github repo slug) passes
a colon-bearing part - so the only cost of the change is that every entry cached under an old key
misses once.

Worth keeping from the fix: **the first property test written for it passed against the broken
code.** It drew two independent tuples and asserted their keys differed, which hypothesis has no
reason to satisfy by drawing `["a:b"]` and `["a", "b"]` in one example. Rewritten to *construct*
the colliding partner from each draw - joining the parts on each candidate separator - it fails on
the first example. A property test that searches for a coincidence is not a guard against it.

~~**`tasks.hard_delete_expired_accounts()` has no overlap lock, unlike its sibling
`send_account_deletion_reminders()`.**~~ **Fixed 2026-09-06**, with the sibling's lock treatment
verbatim (`_HARD_DELETE_LOCK_CACHE_KEY`, 3300s - both sweeps are on the same hourly beat,
`crontab(minute=27)` and `crontab(minute=32)`). The original text follows.

**Was:** The reminder sweep acquires
`_DELETION_REMINDER_LOCK_CACHE_KEY` specifically because two overlapping Celery beat runs could
both select and email the same profile - the hard-delete sweep is on the same hourly beat
(`settings/base.py`) and has the identical hazard: two overlapping runs can both select the same
due profile and both call `hard_delete_profile` on it, sending a duplicate "your account has been
deleted" email (the second `User.delete()` just affects 0 rows, not a crash - but the duplicate
final email is a real, avoidable user-facing defect). Worth the same lock treatment as its sibling.

**`PinAliasView.post` did not sanitize before its emptiness check** (`controllers/aliases.py`),
unlike its wiki-side sibling `LocationAliasView.post`. A name that sanitizes to nothing (emoji-only,
`"<>"`) passed the raw non-empty check, then `create_pin_alias` raised an uncaught `ValueError`,
producing a 500 instead of the intended 400. **Fixed same day** while reviewing the audit finding:
`PinAliasView.post` now sanitizes first, mirroring the wiki view. Guarded by
`test_create_alias_that_sanitizes_to_empty_is_rejected` in `test_alias_views.py`.

~~**`models/achievements/signals.py`'s `on_achievement_saved` re-queues a full profile-table backfill
sweep on every save of an already-active achievement.**~~ **Fixed 2026-09-06.** No intent needed
confirming in the end: the handler's own docstring said "newly defined or re-activated" and the code
did neither check - it never looked at `created` or at what had changed.

**The fix this entry proposed would have been wrong, though.** "Only on creation or reactivation"
drops a case that genuinely needs the backfill: `metric` and `threshold` decide *who qualifies*, so
an admin lowering a threshold from 50 to 3 has to reach the users that newly covers. The gate is a
change to a **qualifying field** (`metric`, `threshold`, `is_active`), not to creation - tracked
with the `from_db` idiom `Pin`, `Location` and `Wiki` already use here rather than a new mechanism.

Three tests: a cosmetic edit (name, colour, order, secrecy) enqueues nothing, and two anti-vacuity
ones - a lowered threshold and a changed metric still do. Only the first fails against the old
code, which is the point: the other two passed before *and* after, and they are what stops the gate
being narrowed too far.

**`from_db` alone was not enough**, which the tests caught. It only sets the markers on an instance
*read from the database*, so a second save of an instance built by `objects.create` compared against
absent markers and enqueued anyway. `Pin.save` already solves this here - it re-baselines
`_loaded_name` after saving - and `Achievement.save` now does the same. Ordering matters: `post_save`
fires inside `super().save()`, so the signal still sees the pre-save values, and the re-baseline
happens after.

**`Location.address` / `Location.address_extended` leave a dangling trailing comma** when the last
populated component has nothing following it - e.g. a route-only address renders as exactly
`"Elm Ave,"`. The existing tests correctly pin down this behavior as current, so it reads as
intentional, but the trailing comma looks like a real address-formatting defect worth a look by
whoever owns Location/address display.

~~**`services/ai/assistant.py`'s `_tool_create_trip` / `_tool_add_trip_activity` reimplement the
`SiteSettings` quota checks and their `select_for_update` locking inline.**~~ **Fixed 2026-09-06,
and "can silently drift out of sync over time" understates it - both had already drifted, in
opposite directions.** (The code had also moved: it is `services/ai/tools/trips.py` now, not
`assistant.py`.)

**`_add_trip_activity` was a permission bypass.** It gated on
`Trip.objects.filter(slug=..., profiles=profile)` - bare membership - where the shared
`trip_activities.create_activity` gates on
`require_perform(actor, trip, trip.allow_add_activities, ...)`. Two separate rules the AI path never
applied:

- **`allow_add_activities`.** A creator sets it to "Organizers" or "No one (creator only)" precisely
  to stop ordinary members editing the itinerary. Through the assistant, a plain joined member could
  add anyway.
- **Joined-ness.** `Trip.profiles` is a `ManyToManyField` through `TripMembership` with no status
  filter, so it matches members who were *invited and never accepted* - the case `has_joined`'s own
  docstring says "cannot contribute ... until they accept the invitation".

Both reproduced before the fix: three failing tests, alongside three anti-vacuity ones (an organizer
*can* add to an organizers-only trip, the creator can always add, a joined member can add to an
"everyone" trip) that passed throughout.

**`_create_trip` had drifted the other way: it held a lock the shared service did not.** Its
check-then-create ran under `select_for_update` on the creator's profile row; `trip_crud.create_trip`
counted upcoming trips with no lock at all, so two concurrent creates through any *other* path could
both pass a limit only one should have. Consolidating naively onto the shared function would have
deleted that guard. The lock moved into `create_trip` instead, where every caller gets it, and the
tool now calls it.

The general lesson for the next consolidation: a duplicate is not automatically the weaker copy.
Diff both before deleting either.

~~**Wiki-owned albums are untested across the entire album test suite.**~~ **Covered 2026-09-06**
in `test_wiki_albums.py`: the ownership half (`parent_wiki` exclusive with the other two owners,
per-owner slug uniqueness, `for_wiki` scoping) and the concealment half.

This entry was right that it needed the rules understood first - two things decide whether the test
means anything:

- **`concealment_active` is hardcoded False today.** A concealment test that does not force it
  passes against any implementation, including one with the narrowing deleted. Every concealed
  assertion patches it True (the idiom `test_concealed_render.py` uses) and has a gate-off
  counterpart, so the difference is demonstrably caused by the flag rather than by nothing being
  listed at all.
- **The actor field is `profile_id`.** `Album` is in `concealment._ACTOR_FIELDS` keyed on it; a test
  written against a `created_by` that does not exist on this model would have passed while
  exercising nothing.

The one worth having is the by-slug case: `visible_rows`' docstring records nine call sites once
scoped to the wiki instead of the viewer - "an existence oracle ... and, on the mutating routes,
lets it act on one" - so the test POSTs a *rename* of another contributor's album and asserts both
the 404 and that the name is unchanged. A first version used GET, got 405 from the method check
before any lookup ran, and had an anti-vacuity assertion loose enough (`in (200, 405)`) to accept
that 405. Both halves were inert; only the hard `== 404` exposed it.

~~**`purge_old_backups`'s count-based retention has no dedicated test anywhere in the suite.**~~
**Covered 2026-09-06**, asserting by identity as this entry asked - which of the files survive, not
how many - so an implementation that kept the oldest and deleted the newest would fail. Includes the
`len(files) > retention` boundary and a stray non-backup file, which must be neither counted toward
retention nor deleted. The original text follows.

**Was:** `test_backup_temp_purge.py` only
exercises the `.tmp`-reaping side effect of `purge_old_backups()` with zero real `.sql` backups on
disk, so the count-deletion loop (`backup_files[self.backup_retention:]`, sorted by mtime
descending) never actually runs in any test - nor does `DatabaseBackup.run()`'s success path
(pg_dump succeeding, `os.replace` to the final name, then `purge_old_backups()` firing). The
existing "Database backups have no restore path" entry above describes retention as "implemented
and tested", which overstates it for this specific branch. Worth a dedicated pass verifying that
with N backups on disk and a lower retention, exactly the oldest excess files are removed (by
identity, not just resulting count) and the newest `retention` survive.

~~**`RedataBasemapTilesGateway.list_sources()` envelope parsing is untested at the unit level.**~~
**Covered 2026-09-06** - the bare-list/`sources`/`results` shapes, the fallback order, the empty-
`sources`-falls-through-to-`results` case, and the id filter. Mocked at `get_json` rather than at
`session`, which this entry suggested: `get_json` is the seam between "talk to REData" and "make
sense of the answer", and only the second half was untested. The original text follows.

**Was:**
`test_basemap_tile_proxy.py` only ever mocks `RedataBasemapTilesGateway.list_sources`/
`download_tile` at the controller boundary, so the gateway's own body-shape handling (bare list vs
`{"sources": [...]}` vs `{"results": [...]}` dict envelopes, and the
`if isinstance(row, dict) and row.get("id")` row filter) has no direct test anywhere in the
codebase - a regression there (e.g. swapping the `sources`/`results` fallback order, or dropping
the id-filter) would only be caught if it happened to also break one of the controller-level
fixtures, which all use the `sources` key and well-formed rows. Worth a dedicated pass that
instantiates the gateway directly (with `base_url`/`api_key` kwargs and a mocked `session`) rather
than mocking the gateway's own methods.

**Sweep-path locking on `advance_usage_ledger` has no real-concurrency coverage.**
`test_billing_ledger_lock.py` proves `_locked`'s `select_for_update` under real threads only via
`banking.apply_payment`, whose internal `advance_usage_ledger` call is nested inside the already-
held outer lock (so removing just that nested lock changes nothing observable). The one call site
where `advance_usage_ledger`'s own lock is load-bearing - the daily sweep
(`advance_pwyw_usage_ledgers`) calling it directly and unnested - is untested under real threads;
the only test of a sweep racing a payment
(`test_billing_ledger_concurrency.py::test_a_payment_is_not_rolled_back_by_the_daily_sweep`)
deterministically sequences two in-memory snapshots and explicitly disclaims exercising the
database's actual lock. A real-thread version is possible (worked through by inspection: under
correct locking both thread orderings converge to the same final ledger state, so it wouldn't be
flaky-when-correct) but needs an actual run to confirm it reliably catches a lock-removal mutant.

~~**`SubscriptionRole.clean()` doesn't validate `pwyw_minimum_cents` requires `pay_what_you_want`.**~~
**Fixed 2026-09-06**, as the symmetric half of the `pwyw_dynamic_threshold` rule beside it. `0`/`None`
stays valid - that is "unset", not "set to nothing". The original text follows.

**Was:**
`clean()` (`src/urbanlens/dashboard/models/subscriptions/model.py`) only ties
`pwyw_dynamic_threshold` back to `pay_what_you_want`; it never checks that a nonzero
`pwyw_minimum_cents` is meaningless when `pay_what_you_want=False`. An admin can save a role with a
static minimum pledge set but pay-what-you-want turned off, and `clean()` raises nothing - the
field is simply inert.

**Webhook-event row lock has no real-concurrency proof.** `StripeWebhookView.post` takes
`StripeWebhookEvent.objects.select_for_update()` specifically so two truly concurrent deliveries of
the same event id serialize instead of both reading `processed_at` as null and both crediting the
payment - but every existing test for this view (`test_billing_webhook_idempotency.py`,
`test_billing_webhook_view.py`) drives it sequentially through Django's test client on one
connection, where `select_for_update()` is a no-op. This is the same class of gap
`test_billing_ledger_lock.py` was written to close for the ledger's row lock, after a mutation-
testing run showed a dropped `select_for_update()` survived every non-threaded test. Closing it
needs a `TransactionTestCase` + real-thread test (as `test_billing_ledger_lock.py` does via
`core.tests.concurrency.run_concurrently`).

~~**`WikiBoundaryView` has no test coverage at all.**~~ **Covered 2026-09-06** - the area limit,
the `WikiEdit` audit write on both save and clear, the `just_drawn` concealment bypass, and request
validation. Writing it found the `parse_multipolygon_geojson` 500 described at the top of this
entry. The original text follows.

**Was:** `dashboard/controllers/boundary.py`'s
`WikiBoundaryView` (GET/POST `/location/<slug>/wiki/boundary/`) - the community boundary-editor
endpoint with its area-limit check against `SiteSettings.max_bbox_area_km2`, its `WikiEdit`
audit-trail write, and the `just_drawn` concealment-bypass logic documented in
`_wiki_boundary_payload` - is exercised by no test anywhere in the suite (only its sibling
`BoundaryController`, the pin-scoped endpoint, is tested in `test_boundary.py`). Worth a dedicated
test file/class.

**Refuted: a fruitless boundary refresh does NOT leave staleness stuck.** An audit agent
(2026-08-29) reasoned from reading `generate_location_boundaries` → `ensure_place_for_location` →
`provision_places_for_coordinate` (`services/places/provisioning.py`) alone that a refresh whose
provider chain comes back with no polygon might leave `Place.geometry_generated_at` /
`Location.place_resolved_at` both unstamped, so `boundary_generation_stale()` would keep returning
`True` forever for that Location - and flagged `test_a_fruitless_refresh_leaves_existing_geometry_alone`
in `test_boundary_generation_staleness.py` as likely to fail on a real run. It doesn't: the
consolidated verification pass for this batch ran the real suite against Postgres and the test
passed cleanly (`2 failed, 277 passed` that run, neither failure this one - see the batch's commit).
Recorded here so nobody re-derives the same false alarm from a source read alone: this is NOT a
real problem, a plausible-sounding defect inferred from code reading turned out wrong once actually
run.

**Stale `update_or_create`/`auto_now` rationale in boundary voting docs.** Both
`services/geo/boundary_voting.py`'s module docstring and `test_boundary_vote_recency.py`'s header
explain the re-affirm-refreshes-`updated` behavior as depending on `cast_boundary_vote`'s
`defaults={"boundary": choice}` explicitly including the field whose `auto_now` timestamp needs
bumping ("Django only refreshes an `auto_now` field when that field is included [in
update_fields]"). That's no longer how `update_or_create()` behaves: Django 6.0.6 (pinned in
`.venv`) unconditionally folds every field with a custom `pre_save` - i.e. every
`auto_now`/`auto_now_add` field - into `update_fields` for backward compatibility, regardless of
what's in `defaults` (see `update_or_create` in `django/db/models/query.py`). The test's protective
value is unaffected (it still catches a regression away from `update_or_create`, e.g. a raw
`.filter().update()`), but the prose misdescribes the current mechanism and could mislead a future
contributor into thinking they must hand-add `updated` to `defaults`.

**Stale "draft wiki" language around the building-mirror path.**
`pin_restructure.mirror_buildings_to_wiki`'s docstring/comments and `test_building_wiki_mirror.py`'s
own module docstring describe the wiki a building import mirrors into as an "invisible draft...
until claimed," citing `tasks.ensure_draft_wiki_for_location` and a
`WikiManager.get_or_create_draft_for_location` - neither exists on disk (the real names are
`ensure_wiki_for_location` and `get_or_create_for_location`), and `WikiManager`'s own docstring
states plainly: "Wikis are published on creation now, and there is one question again" - there is
no draft/official field left on `Wiki` found during this audit. `services/wiki/wiki_share.py`
("Ignored when the wiki is already official... a still-unofficial draft is fair game") and
`services/wiki/concealment.py` reference the same apparently-retired concept. Either a draft/
official distinction exists somewhere this audit pass didn't locate, or this is stale documentation
spanning at least three production files describing removed behavior - worth a follow-up look.

**`CalendarImportView` has no test coverage at all.** `dashboard/controllers/calendar_sync.py`'s
`CalendarImportView` (GET renders the upcoming-events dialog via `list_importable_events`, POST
parses per-event form fields into `import_events_as_trips` selections and handles
`GoogleAuthExpiredError`/`GatewayRequestError`/empty-selection 400s) is reached by no test in the
suite - only its underlying service functions are unit-tested. The view's own request-parsing
(`create_activity_<id>`, `invite_<id>`, `auto_sync_<id>` field names, digit-filtering of invite
ids) and error-branch responses are unverified end-to-end, unlike its sibling
`CalendarImportPreviewView` which does have a `CalendarImportPreviewViewTests` class.

~~**Map-overlay caption length check is untested even though it's drivable.**~~ **Covered
2026-09-06**, and this entry was right on both counts: the check is correct, and the docstring
saying it could not be driven was wrong. That claim is gone, with a note in the module docstring
recording what it got wrong - it is true of `_image_from_request`'s `media_url`/`image_url`
branches and not of its direct-upload branch, which reaches `upload_photo` with no network call at
all.

The shape of the refusal is the part worth knowing, and a first draft of the test got it wrong in
the opposite direction to the docstring: an over-width caption answers **200**, not 400.
`map_overlays.fail()` returns 400 only to the JSON caller (the lightbox's "use as floorplan
overlay"); the HTMX dialog gets 200 with the message swapped into the list partial. Asserting on
the status alone would have filed a bug against working code, so the test asserts that neither the
`Image` nor the `MapImageOverlay` is created, and a second one drives the JSON caller to pin the
400 half.

**Missing coverage for the carousel "no imagery available" branch.**
`test_carousel_single_slide_arrows.py` is the only test file touching
`street_view.html`/`satellite_view.html`, and neither template's `{% else %}` branch (rendered when
`slides` is empty, showing `view-unavailable` and the `error` message) has any test coverage
anywhere in the repo.

**Multi-level pin/wiki nesting prefix is undocumented and untested.** `_slug_parent_prefix()`
derives a child's prefix only from its *immediate* parent (name/official_name/slug/aliases), so a
grandchild nested two levels under an aliased root picks up a prefix derived from the immediate
parent's own name/slug, not the top-level acronym, unless that immediate parent itself has an
alias. This may be intentional (shallow, not chained, prefixing) but it's unverified either way and
worth a deliberate look if 3+ level nesting is a real use case.

~~**`TripCommentDeleteView` has zero test coverage.**~~ **Covered 2026-09-06** in
`test_trip_comment_delete.py`. No defect: the view was already correct, and is now guarded - the
author's own delete, the trip creator's override, a joined member who is neither, a non-member, and
the attached `MarkupMap` going with it.

Two cases worth having beyond "the author can delete their own". `TripComment.author` is `SET_NULL`
(the asymmetry P25 records), so a comment outlives its writer's account and
`can_delete_comment`'s set becomes `{None, creator}` - a bug letting `None` match would hand every
orphaned comment to any member. And `get_comment(trip, comment_id)` must scope by trip, not just by
id, which is the same existence-oracle shape `concealment.visible_rows` warns about.

## P58 — A renamed photo's old URL still 404s for the uploader who just uploaded it

`id: P58` · `status: open` · `updated: 2026-09-06`

Previously titled "A photo's grid tile can 404/500 for seconds after upload while async processing
renames its file", and before that "... for a few seconds right after upload".

`tasks.process_image_upload` re-encodes an upload and stores it under a new name (`.jpg` ->
`.webp`, `downscale_stored_image`). A grid tile rendered from the upload response, or from a page
load that lands before the client refreshes, points at the old path. Found live-verifying Batch 4
against the `ae97b86` dev environment; pre-existing, and it also hits Vault Documents' `<iframe>`
lightbox preview (`_setLightboxDocument`), since `upload_photo()` queues the same task for every
media type.

**Two of the three defects behind it are fixed (2026-09-06); the third is what is left.**

1. ~~**The 500.**~~ `LocalMediaSource.response()` opened the file that `resolve_media_path` had
   just stat'ed, and a `FileNotFoundError` in between escaped as a 500 - against the abstract
   `MediaByteSource.response`'s own documented contract, which `ObjectMediaSource` honoured and the
   local branch did not. It raises `Http404` now.
2. ~~**The window that made it reachable.**~~ The rewrite deleted the superseded file immediately,
   while the row still named it - and `services.media.access.authorize_media` answers from the row,
   so for the rest of the task (three thumbnail passes, seconds under load) every request for that
   path was *authorized* and then missing. All three rewrites (`downscale_stored_image`,
   `process_uploaded_video`, `convert_to_pdf`) now return a `StoredFileReplacement` naming the
   superseded file instead of deleting it, and the caller discards it after persisting the new name
   (`discard_superseded_file`). Deleting late costs one orphaned file if the process dies in
   between; deleting early cost a row that permanently named a file which no longer existed, which
   is the better candidate for P59's durably-broken thumbnail than that entry's own theory.
3. **The 404 itself - still open, and not a bug in the delivery path.** Once the row names the new
   file, the old path has no owning row, so `authorize_image` refuses it: 404 is the *correct*
   answer, and no amount of keeping the old file around changes it. What is wrong is that the
   client is still holding a URL the server has stopped honouring. That needs one of:

   - a stable per-row media URL (`/media/image/<uuid>/`) that resolves to whatever file the row
     currently names, so a rename is invisible to anything already rendered - the durable fix, and
     the one that also covers the document lightbox; or
   - the client not rendering a URL until processing is confirmed done. `Image.pending_scan` already
     marks exactly that state and is already false for everyone but the uploader, so the uploader's
     own tile is the only surface that needs it; or
   - `urbanlensMediaThumbFallback` re-fetching the row rather than retrying the same URL twice
     (2s, 4s) - the cheapest, and it leaves the stale URL in the page.

   Not chosen here: the first costs a route and a template sweep, and picking between them without
   measuring how often a tile actually lands in the window would be guessing.

## P59 — A `lightbox-associations.webp` thumbnail on the `ae97b86` dev account is durably broken, not just racing

`id: P59` · `status: open` · `updated: 2026-08-31`

Previously titled "a specific `ae97b86` dev-account thumbnail (`lightbox-associations.webp`) is durably broken, not just racing".

Found live-verifying Batch 5's regression run of the pre-existing `vault-photos.spec.ts` pruning
test (`scrolling loads further pages and prunes off-screen thumbnails`) - unrelated to Batch 5's
own changes (this test predates it, and nothing touched this session runs anywhere near
`photo-virtual-grid.ts`'s pruning/restore path). The grid's first tile after scroll-to-top
consistently fails to restore its `<img src>`, always pointing at
`.../pin_images/thumbs/5v/S76SWO1keJAXdV/lightbox-associations.webp` - the same filename pattern as
the async-rename race documented above, but this one reproduces identically across two fully
isolated `--grep`-scoped runs, not just within a single flaky window.

**The evidence for "durably broken rather than racing" was measured against the wrong column, and
that is worth fixing before the next attempt.** The entry ran
`Image.objects.filter(image__icontains="S76SWO1keJAXdV")` and read zero rows as "no row owns this
path". A `pin_images/thumbs/` path is what `pin_image_thumbnail_path` (`models/images/model.py:130`)
produces, and it is stored in `Image.thumbnail`, not `Image.image` - so that query could not have
matched however healthy the row was. Re-run it against `thumbnail__icontains` (and
`marker_thumbnail`/`analysis_thumbnail`, which share the prefix) before concluding anything.

A likelier cause than the "thumbnail job that got a path assigned and never completed" this entry
guessed at: until 2026-09-06 every stored-file rewrite deleted the superseded file *before* its row
was updated (see P58), so a process that died in between left a row permanently naming a file that
had already been removed - durably broken, by construction, and indistinguishable from this. That
ordering is fixed; a row already in that state stays in it, and needs a re-run of the thumbnail
backfill (`backfill_image_thumbnails`) or a repointed row to recover.

Didn't chase further (out of scope for Batch 5, and the `e2e-primary` account on this ephemeral dev
slot is disposable), but worth a look if `vault-photos.spec.ts` keeps failing on this specific test.

## P63 — Adding a third Vault media type means copying ~600 lines for ~90 lines of difference

`id: P63` · `status: open` · `updated: 2026-08-31`

`controllers/vault_documents.py` is largely a rename of `controllers/vault_photos.py`'s gallery
half (`:32-36 / :80-120 / :123-156` vs `:38-50 / :261-302 / :305-332`), `pages/vault/documents.html`
duplicates `photos.html`'s inline upload/delete/lightbox script (110 lines byte-for-byte identical),
and `vault-document-grid.ts` shares ~68 near-identical lines with `vault-photo-grid.ts` differing in
four string literals. A `MEDIA_KIND_SPECS` registry - the same frozen-dataclass + dict + lookup shape
this codebase already uses three times (`ALBUM_KIND_SPECS`, `ALBUM_SORT_SPECS`, `GALLERY_SORT_SPECS`)
- plus `ImageQuerySet.of_kind(kind)` and a `kind` URL kwarg (the pattern `urls.py:2042-2050` already
uses to serve pin/wiki/vault albums from one view class) would reduce that to one spec entry, one
tile renderer, and the SCSS.

Related: `pages/vault/photos.html` carries 371 lines of inline `<script>` and `documents.html` 168 -
539 lines total that `bun run typecheck` and `bun test` cannot see, against 40+ `*.test.ts` files
covering `shared/`. The 135-line confirm-pin block (`photos.html:140-274`) is the worst of it: it
owns Leaflet lifecycle across dialog opens, does its own bbox fetch, builds popup HTML by string
concatenation, and defines an `_esc()` helper found nowhere else under `templates/`. Extracting it to
`shared/photo-pin-confirm.ts` and the uploader to a shared `initVaultUploader` would bring the whole
Vault client surface under typecheck and test.

## P66 — Organize's active label tab still renders its full card list unpaginated

`id: P66` · `status: open` · `updated: 2026-08-31`

Previously titled "Organize's *active* label tab still renders its full card list unpaginated".

The tab-deferral fix (see the entry above) stopped the other five tabs from rendering, but did
nothing to cap the *active* tab's own row count - `controllers/labels.py:370-406`
(`_rows_ctx`/`_render_rows`, `list(_queryset_for_kind(kind, profile))` with no slicing) feeding
`templates/dashboard/partials/labels/_organize_label_card.html` is the same per-row template
profiled at ~2s of pure render time for 500 rows (`label.rows`, the `hx-trigger="revealed"` endpoint
every tab - including the active one - now defers to). A profile whose *single* busiest tab (tags
carries every global category/status too, via `Label.visible_to`) reaches that scale is still going
to feel this page as slow, just for one tab instead of six.

Not fixed here because it isn't a quick swap: `organize-filter-engine.ts`'s client-side search/filter
assumes every row for a kind is already in the DOM, so naively paginating the server response would
break "type to filter" without a matching client-side redesign (fetch-as-you-type, or a windowed grid
like Vault's `photo-virtual-grid.ts`/`bindPhotoGrid` - see the tooling entry above for why the latter
wasn't reused as-is: it's built for JSON tile grids, not server-rendered card rows wired into the
existing bulk-select/merge/convert machinery in `organize-tab-manager.ts`).

## P69 — Unbounded lists across the site: 9 of 11 fixed; one argued against by measurement, one group deliberately left

`id: P69` · `status: open` · `updated: 2026-09-08`

Previously titled "unbounded lists with no pagination, found across most of the site".

Same survey, same shape each time: a collection that grows with account age/usage, rendered via a
plain `{% for %}` with no `.filter()[:N]`, `Paginator`, or HTMX-deferred/windowed loading. Grouped
here rather than one entry each since the fix is identical in kind (cap it, paginate it, or defer it)
even though the code paths are unrelated. Roughly ranked by how large the realistic ceiling is and how
heavy the per-row template is - top few are worth prioritizing, the rest are real but currently minor
at this app's beta scale (~2 users):

**Nine of the eleven bullets below are fixed as of 2026-09-06.** What is left is one half-bullet the
measurement argues against doing (the Settings tabs' lazy-loading half) and one group deliberately
left because each of its items is bounded by something other than account age. Read those two before
concluding there is work here.

The recurring lesson across the nine, worth having before starting the tenth: **the slice is rarely
the whole fix.** Every one of them had something else the survey had not seen - a numbered list whose
numbering, "current" marker and per-row delta are all defined against the whole set; a group-by that
had to move into the database before a slice could mean anything; a per-row query that made the
render cost survive the cap; a count that gated an empty state and so had to stay exact; a partial
shared with a mutating action whose pagination links would otherwise point at it.

- ~~**Album detail's "add existing photo" picker**~~ **fixed 2026-09-06.** It listed every photo
  eligible for the album inside a `<dialog>` that stays closed until a click - the same
  "hidden-but-fully-rendered" shape as the tab entries above. Bounded for a pin or wiki album, which
  can only hold that place's photos; unbounded for a Vault album, which is scoped to the whole
  profile. The picker now fetches its photos a page at a time from `AlbumEligibleImagesView` when the
  dialog opens, a sibling of the `AlbumItemsView` the virtualized grid already used.

  **Paginated rather than capped, and that was the decision.** A cap was the smaller change and the
  wrong one: this picker's purpose can be "find the photo from last year", which is exactly what a
  newest-first slice removes - unlike the wiki-share picker below, where seeding from recent photos
  is the whole point. Two things the survey did not record. The page still needs the *count*, because
  it gates whether the "Add from this place" affordance appears at all and which of two empty-state
  sentences the album shows - a version that dropped it would ship a picker with no way to open it,
  which looks fine on any album that happens to be full. And the tiles are built with DOM calls
  rather than interpolated markup, so no seventh copy of an HTML-escaping helper joins the fourteen
  P34 counts.

  Verified in a browser: zero picker tiles in the page before the dialog is opened, one request on
  open, tiles rendered with their images, and no console errors. The direct `/albums/<slug>/` URL is
  the HTMX partial endpoint and loads no scripts - the panel has to be reached through the Private
  Pin page (`?album=<slug>`) for any of its JavaScript to exist, which is worth knowing before
  concluding a picker is broken.
- ~~**Immich "nearby" photo import**~~ **fixed 2026-09-06**, as far as it can be. Immich exposes no
  coordinate-radius filter on any endpoint - ~~its `/search/metadata` accepts lat/lng+radius~~ **it
  does not**, checked 2026-09-06 against the upstream OpenAPI spec and against what this repo's own
  gateway sends: `MetadataSearchDto` has no geographic field beyond the geocoded
  `city`/`country`/`state` strings, and `/map/markers` takes only date and archive/favourite
  filters. So the fetch-everything shape stays until that changes; what was fixed is that it
  happened *seven times*. `_immich_picker_dialog.html` puts `hx-trigger="change"` on the radius
  `<select>`, so each of its six options re-downloaded the whole library, as did switching modes
  away and back. The measured, distance-sorted neighbourhood is cached per pin for five minutes
  (`services/apis/immich/nearby.py`) and capped at the nearest 500 - matching the app's own two map
  caps, and the widest radius offered is 5km. The cache key carries the account's `updated`
  timestamp at microsecond precision, because reconnecting to a different server inside the same
  second is exactly what it is there to catch. Re-confirm the no-radius-filter claim against the
  pinned server version before deleting the fetch-everything shape. The other two modes (`VISITS`,
  `ALL`) *are* bounded server-side, by date and page size respectively - so the docstring in
  `services/photos/photo_import.py` is right about them and wrong only about geography.
- ~~**Wiki edit history & article revision history**~~ **fixed 2026-09-06.** Neither had a slice
  anywhere in its chain. Both page at 25 now. Three things the survey did not record. The article
  history is a *numbered* list, so a plain slice is wrong in three ways at once: it renumbers every
  page from 1, marks the top of each page "current" - which also withholds its Restore button, from
  a revision the user is entitled to restore - and reports the oldest row on each page as an edit
  that wrote the article from empty; rows carry `is_current` and an absolute number now, and the
  boundary delta is measured against one extra row fetched from the next page. `_render_history` is
  shared with the wiki's revert and expunge actions, which POST to their own URLs, so a pagination
  link built from `request.path` would swap a revert into the list; both actions also send the page
  they were fired from, so acting on a row does not drop the user back to page one. And concealment
  has to narrow *before* the slice, or a page is short by however many of its rows concealment then
  removes.
- ~~**Pin-to-wiki share dialog's photo picker**~~ **fixed 2026-09-06** (`637a47bd1`).
  `seedable_photos()` caps at 60 - matching the two comparators below - ordered newest-first, because
  a LIMIT over an unordered queryset may return a different slice per call. Two things the survey did
  not record: the picker rendered `image.image.url`, the *original* rather than the thumbnail, so its
  per-row cost was far higher than the capped pickers it was measured against (`thumb_url` now); and
  the cap is safe to add without a "load more" only because `WikiShareService` re-scopes the
  submitted ids through `pin.images` on the POST, so the cap bounds what is *offered*, not what is
  shareable. Contrast with the wiki's own Media gallery (`_WIKI_PHOTOS_PREVIEW_LIMIT = 60`) and the
  visit dialog's photo picker (capped `[:60]` in `controllers/visits.py:77`), both of which had
  already learned this lesson.
- ~~**Vault "pin albums" panel**~~ **fixed 2026-09-06.** It loaded every album and every album
  item across all of a profile's pins once the (correctly lazy) section was opened. Paginated at 24.
  `albums_listing` could not be handed a narrowed set of albums, so `describe_albums` is split out of
  it. Two things the survey did not record: the membership rows are the expensive half, since they
  are what counts photos and picks covers, and a page-sized *card* count is what the unfixed version
  already produced - so the regression test asserts on the membership query's own `IN` list. And the
  panel was going through `albums_listing`'s owner filter, which builds one `OR` term per owner: it
  passed every pin the profile has, so an account with a lot of pins - the account this panel is
  about - generated a `WHERE` clause with a term each. One join on `parent_pin__profile` replaces it.
- **Settings page's Security and API-Keys tabs.** ~~The API-key list has no cap and revoked keys
  are never excluded, so it only grows~~ **- fixed 2026-09-06**, paginated at 10 with working keys
  first (an account that had revoked a page's worth of keys would otherwise have to page forward to
  reach the key it uses), under its own `api_keys_page` parameter.

  ~~These are also the two tabs never converted to the lazy-HTMX-subsection pattern the page's own
  Connections/Billing/Undo/Notifications/Custom-Fields tabs use~~ **- and converting them is the
  wrong change, measured 2026-09-06.** `security_settings_context()`/`api_keys_settings_context()`
  are 5 of the settings page's 22 queries and under 3ms of its 71ms; the "pattern" is
  `hx-trigger="load"`, which fires on every page load anyway, so it would trade three milliseconds
  for two extra HTTP round trips and two more concurrent connections - the shape P53 was about. It
  would also break the one-time plaintext-key reveal, which
  `test_non_htmx_create_reveals_the_key_once_on_the_redirected_settings_page` pins to the full page
  render. The same measurement found what actually makes this page heavy, which is not queries at
  all: 170KB of its 297KB is inline `<script>`, uncacheable and re-sent on every load - 52KB of map
  preview, 29KB of theme preview, 22KB of dev toolbar. Recorded as P83.
- ~~**Memories > Sharing**~~ **fixed 2026-09-06.** It queried, grouped and rendered both the full
  "sent" and the full "received" history on every load, though a client-side toggle shows one at a
  time. The received half is its own partial now, fetched the first time that button is clicked, and
  both halves page at twenty places. Three things the survey did not record. Grouping had to move
  into the database first: the old code fetched every share and grouped in Python, so a slice on the
  *shares* would have cut groups in half rather than dropping whole places. The chain count is a BFS
  *per group*, so an unpaginated list was also an unbounded number of query round trips, not just a
  long render. And the toggle buttons name the totals, which also gate the empty state, so those are
  counted rather than measured off the lists. Measured on the dev stack at 23 sent and 23 received
  places: 20 cards on load instead of 46, and the 19KB received half absent from the page entirely.
- ~~**Memories > Journal**~~ **fixed 2026-09-06.** It merged four unsliced sources with no windowing
  of any kind. Two things the survey did not record. The merge was only half the cost: each rendered
  entry's title is `Pin.effective_name`, which falls through to `Location.display_name`, which reads
  the linked `Wiki`, and every source selected only the pin - thirty entries cost 65 queries. The
  page is 21 now, and flat (`test_query_scaling_memories_journal.py`). And the external API's
  journal endpoint had the same problem, with a comment saying it had no way around it: pagination
  needs a `count`, and the only count available was `len()` of the fully-built list. `JournalFeed` is
  a sliceable sequence whose length is counted rather than measured, which is what `Paginator` and
  DRF's paginator actually ask for. The per-source limit is exact, not approximate - the newest N of
  a union can only contain entries in some source's own newest N.
- ~~**Pin import-failure queue**~~ **fixed 2026-09-06.** It had no pagination and contradicted
  itself: the queue view's docstring said failures are "rare," while `PinImportFailureGuessView`'s
  docstring in the *same file* says "a single import can leave hundreds of failures." The second is
  the one borne out by how imports work. Paginated at 12, matching the sibling
  `PinSuggestionQueueView`. Two things the survey did not record. Each card fetches its own geocoder
  guess on reveal, so an unpaginated queue of hundreds was also hundreds of pending lookups - the
  cost was never only the cards. And this partial renders inside the full Memories > Locations page
  *beside* the suggestion queue, which pages on `page`, so it needed a parameter of its own
  (`failures_page`) or one next-page click would have moved both lists; `get_page` takes a `param`
  now. An existing test asserted the unpaginated behaviour by name and was rewritten rather than
  deleted - its ownership half is stronger walking every page, since a slice applied before the
  ownership filter would leak on a page a single-page assertion never looks at.
- ~~**...and the pin-list overview map**~~ **fixed 2026-09-06.** `_items_map_data` had no cap, unlike
  the near-identical `SavedFilterPreviewView` in the same feature, and each of its markers carries
  far more than that one's does - name, address, description, rating, last-visited and every tag
  chip - so it was the heavier of the two per row as well as the unbounded one. Capped at the same
  500, with a notice, and the cap is on the fetch: `_paginated_items_context` materialized every
  item on the list to serve both the map and one page of rows, behind a comment saying that cost no
  more than the unpaginated render - true only while the map genuinely needed all of them.

- **Undo history, Safety check-ins overview, "view all friends" page, DM conversation list,
  achievement catalogue, and Organize's Lists/Filters tabs** all follow the identical pattern with
  lower realistic ceilings or lighter per-row templates today. **Deliberately left, 2026-09-06**:
  each is bounded by something other than account age - the achievement catalogue by how many awards
  the *site* defines, undo history by its 7-day window, the friends list and the conversation list
  by friend count - and paginating a chat sidebar or an awards catalogue trades a theoretical
  ceiling for worse browsing. Worth revisiting with a `RenderTimeScalingMixin` subclass each, which
  would answer "is a row cheap next to the page" with a number instead of a guess:
  `controllers/undo.py:58-112`; `controllers/safety.py:314-431` (no auto-delete by default -
  `SafetyPreference.auto_delete_after_days` is nullable and defaults to "never"); 
  `controllers/friendship.py:451-477` (`SiteSettings.max_friends_per_user` defaults to 0/unlimited);
  `controllers/direct_messages.py:710-734` (query count already proven flat by
  `ConversationListQueryScalingTests`, but that test can't see render-time cost, and the list is
  re-fetched on nearly every DM sent anywhere in the app); `controllers/achievements.py:98-113`; and
  `controllers/pin_lists.py:214-246` (also structurally invisible to `test_route_query_scaling.py`'s
  generic sweep, which hits `lists.list` without an `HX-Request` header and only ever exercises its
  redirect branch).

  **Corrected 2026-09-08:** this bullet previously also listed `controllers/pin_lists.py:155-176`
  (`_items_map_data`) as deliberately left uncapped, contradicting the "...and the pin-list overview
  map" bullet above, which records that exact function as fixed the same day. Checked against the
  code: `_MAP_PIN_LIMIT = 500` (`controllers/pin_lists.py:56`) is applied at
  `controllers/pin_lists.py:182` (`_items_map_data(items[:_MAP_PIN_LIMIT])`) - the cap is real, so
  the citation was stale leftover text from before that fix landed, not a second unbounded case.
  Removed rather than left standing next to its own contradiction.

## P83 — Over half of every page's HTML is inline `<script>`, re-sent uncached on every load

`id: P83` · `status: open` · `updated: 2026-09-06`

Found 2026-09-06 while measuring whether the Settings page's Security tab was worth deferring
(P69). It is not - those queries are 5 of 22 and under 3ms of 71ms - but the same measurement
found where that page's weight actually is, and it is not the database.

Measured against the dev stack, logged in, `DEBUG=False`, counting only `<script>` tags with no
`src`:

| page | HTML | inline script | share |
|---|---|---|---|
| `/dashboard/map/` | 550,852 | 400,066 | 72% |
| `/dashboard/map/pin/<slug>/` | 343,443 | 190,886 | 55% |
| `/dashboard/settings/` | 309,647 | 169,194 | 54% |

Nine of the seventeen blocks are the same on all three - 111KB from `themes/base.html` and the
layout partials, on every page in the app. 22KB of that is the dev toolbar, which
`show_dev_toolbar` gates to dev, so production pays roughly 89KB. The rest is per page, and the
map page's share is one block of **265,146 bytes** out of a 305KB `pages/map/index.html`.

Why it matters: a `<script src>` is fetched once and cached for the life of its hash; an inline
block is re-sent on every navigation, is not shared between pages that duplicate it, cannot be
minified by the bundler, and is invisible to `bun run typecheck` - which is how the P81 defect
(`core.js` dead site-wide for four days) survived: the code that broke was in a bundle, but
nothing that consumed it was type-checked together.

This is the accumulated other half of P11. That entry counts raw `fetch()` calls that bypass
`fetch-json.ts`; this one is why they are hard to migrate - they are not in TypeScript files, so
there is nothing to import into. `dashboard/CLAUDE.md` already says "Use Typescript only when HTMX
cannot accomplish the interaction" and "Every existing JS interaction is a candidate for HTMX
refactoring"; this measures how far the code is from that.

Not started, and not a one-batch job. The obvious first cut is the map page's single 265KB block,
which is also the file P53 and P68 both had to edit around - twice now a fix has been written once
in `ts/shared/` and then a second time by hand into that template, because the template cannot
import. Whether the answer is moving it into `frontend/ts/entries/` or something narrower is a
design question, not a mechanical one.

## P85 — Every manager is a dynamic base class, so `Model.objects` is `Any` and 146 mypy errors are turned off to hide it

`id: P85` · `status: open` · `updated: 2026-09-06`

`models/abstract/queryset.py` builds each manager by subclassing a call:

```python
class DashboardManager(django_models.Manager.from_queryset(DashboardQuerySet)): ...
```

mypy cannot follow a base class that is a function call. It says so - `Unsupported dynamic base
class "django_models.Manager.from_queryset"  [misc]` - and `[tool.mypy]`'s
`disable_error_code = ['misc', 'annotation-unchecked']` turns that message off. The class therefore
resolves to `Any`, and so does every one of the 146 managers built the same way. That is almost the
whole surface: of the 137 `objects = ...` declarations under `models/`, 134 name one of those
managers directly and two more name a per-app `Manager` that is itself `from_queryset`-derived. The
one exception is `abstract/versioned.py`'s `objects = Manager()`, a real `django.db.models.Manager`
declared on the abstract base precisely so the resolver's `.objects` is typed.

**What that costs, measured rather than reasoned.** With the tree's own settings, none of these is
an error:

```python
x: int = Trip.objects                    # no error
y: int = Trip.objects.all()              # no error
w: int = Trip.objects.all().first()      # no error
Trip.objects.all().first().no_such_field # no error
```

Assigning a manager to an `int` is accepted, so nothing downstream of `.objects` is checked at all.
The control: an ordinary `x: int = "str"` in the same directory *is* reported, so the file is in
scope and the checker is running.

**The queryset generics are a smaller, separate half of the same subject.** 107 of 148 queryset
classes under `models/*/queryset.py` are declared bare (`class TripQuerySet(abstract.DashboardQuerySet)`)
where 41 are parameterized (`abstract.PublicDashboardQuerySet["Achievement"]`), even though the base
is generic and its own docstring asks subclasses to parameterize it. Parameterizing does work, where
code names the queryset type:

```python
def f(qs: AchievementQuerySet) -> None:
    qs.first().no_such_field_at_all   # error: "Achievement" has no attribute ...  [attr-defined]

def g(qs: TripQuerySet) -> None:
    qs.first().no_such_field_at_all   # accepted - element type is Any
```

But only ~15 annotations in non-test `src/` name a concrete project queryset; the rest of the tree
reaches the ORM through `.objects`, which the manager problem has already made `Any`. So fixing the
107 alone buys those 15 sites and nothing else. **The manager is the load-bearing half.**

**What is behind the `misc` disable.** Turning it back on for one run: 181 errors, 146 of them the
dynamic base class above. Of the other 35, three were checked and all three are django-stubs
limitations rather than defects:

- `spotguessr/overview.py:143` - `Cannot resolve keyword 'participant_count'`. It is an
  `.annotate()` name that `participated_sessions` adds; the stubs cannot see runtime annotations.
- `abstract/versioned.py:298,392,443` - `target_id` on `AbstractFieldRevision`. The abstract base
  names a column its concrete subclasses declare.
- `services/photos/uploads.py:202` - `exif_data` "expected `str | Combinable | None`". The field is
  `EncryptedJSONField`; the stub sees its text base, not the JSON it actually stores.

Two more were checked and **both were real**, which settles the argument about whether the disable is
purely noise suppression. `Incompatible type for lookup 'pk': (got "str | None", expected "str | int")`
at `controllers/site_admin.py:1365` and `services/billing/webhooks.py:149` are
`filter(pk=<raw request value>)`: Django raises `ValueError: Field 'id' expected a number but got ''`
for `""` and for any non-numeric string, and only `None` degrades to a zero-row `IS NULL`. Every one
of those sites had an "it did not resolve" branch on the very next line that a malformed id skipped
straight past, into a 500. Fixed 2026-09-06 with `services.core.numbers.safe_int_or_none`, across
seven sites - the two mypy could see plus five it could not, including
`controllers/userprofile.py`'s two email actions, whose `request.POST.get("email_id", "")` default
made the crash the behaviour of an *omitted* field rather than a hostile one. Reproduced first: 20
failures and two logged `Internal Server Error: /dashboard/profile/edit/` against the unfixed code.

Fixing those seven sites turned up a third defect in the helper they now use.
`services.core.numbers.safe_int` caught `(TypeError, ValueError)` and not `OverflowError`, and
`int(float("inf"))` raises exactly that - reachable, because Python's `json.loads` accepts the bare
literals `Infinity`, `-Infinity` and `NaN`, and `controllers/detail_pins.py` and
`controllers/markup.py` parse their bodies with it directly. Proven end to end rather than at the
helper: a `POST` of `{"bg_opacity": Infinity}` to `pin.detail_pin.edit` raised `OverflowError` out
of the view. DRF's own parser refuses the literal, so the four `int()` guards in
`controllers/e2ee.py` carrying the same narrow `except` are **not** reachable this way - a widening
of those was written and then reverted, along with a test that asserted the 400 DRF was already
returning for its own reason and would have passed either way. `safe_int_or_none` now holds the
parsing rule and `safe_int`/`clamp_int` are built on it, so the three cannot drift apart again.

The remaining 30 are unaudited. About a dozen are more of the `EnrichmentSource`
ClassVar-vs-instance-variable pattern; `Expected iterable as variadic argument` at
`forms/settings_form.py:50` and `"dispatch" undefined in superclass` at `controllers/games.py:45`
and `controllers/labels.py:611` have not been looked at. The tally so far is three false positives
and two real bugs, which is the argument against leaving the code off: a blanket disable of the code
that reports 146 known-benign findings also silences whatever else `misc` covers.

**Why this is filed rather than fixed.** The fix is not one line, and the obvious shortcut does not
work. django-stubs *can* type `Manager.from_queryset(SomeQuerySet)` when the result is bound to a
name; what it cannot follow is a `class` statement whose base is that call. So the mechanical form of
the fix is

```python
-class LabelManager(abstract.FrontendDashboardManager.from_queryset(LabelQuerySet)):
-    """Manager for Label."""
+LabelManager = abstract.FrontendDashboardManager.from_queryset(LabelQuerySet)
```

which drops the class body. Counting how far that goes: **146** manager classes are declared with a
`from_queryset()` base, **125** of them have nothing but a docstring and convert this way; the other
**21** have real bodies and need a decision each (`BoundaryManager` is the largest at 10 statements;
`LocationManager` and `WikiManager` have 4 each).

That is only the concrete half. The three abstract managers have to be fixed first and bottom-up -
`DashboardManager` is itself a dynamic base, so everything deriving from it is `Any` no matter how
the derived class is spelled - and they exist precisely to forward custom queryset methods onto
`Model.objects`, which is what `from_queryset` generates at runtime and what a hand-written
`Manager[_ModelT]` subclass would have to re-declare. Doing all of that and *then* re-enabling
`misc` is a real change to how every model in the tree is typed. It will surface errors that have
never been reported here, which is the point, and also why it should not ride along inside an
unrelated commit.

Found while resolving P84; two querysets (`GeocodedLocationQuerySet`, `WikiQuerySet`) were
parameterized there because their unused model import was the symptom of the missing type argument.

## P89 — `MarkupJsonView`'s `?children=1` wiki path skips concealment; dormant only because `concealment_active()` is hardcoded False

`id: P89` · `status: open` · `updated: 2026-09-08`

Found 2026-09-08 during the pre-merge audit of `release/v_0_8_0`; confirmed on an independent
adversarial pass.

`MarkupJsonView.get()` (`controllers/markup.py`) builds its `items` queryset two different ways, and
only one of them applies wiki concealment. The single-wiki path - `_resolve_owner()`'s wiki branch
(`controllers/markup.py:170-180`, function starts at line 136) - resolves through
`resolve_visible_wiki()` and then explicitly narrows:
`return wiki, visible_rows(PinMarkup.objects.for_wiki(wiki), wiki, profile)` (line 180).
The `?children=1` aggregation path, inside `MarkupJsonView.get()` 94 lines later in the same file, replaces that
already-concealed `items` with a raw, unfiltered query:

```python
elif include_children and isinstance(owner, Wiki):
    subtree = Wiki.objects.filter(pk=owner.pk).with_descendants()
    items = PinMarkup.objects.filter(parent_wiki__in=subtree).select_related("parent_wiki__location", "layer")
```

(`controllers/markup.py:272-274`). `.filter(parent_wiki__in=subtree)` never calls `visible_rows`, so
a concealed viewer requesting `?children=1` would get every descendant wiki's markup items, not the
subset `conceal_rows` would let through.

**Not a live leak today.** `concealment_active()` (`services/wiki/concealment.py:153`) is hardcoded
to return `False` site-wide - "the threshold is a reputation score scaled by the wiki's
community-voted vulnerability, and cannot be chosen before there is real score data" - so
`visible_rows()` (`services/wiki/concealment.py:396`:
`return conceal_rows(queryset, viewer) if concealment_active(wiki, viewer) else queryset`) is
currently a no-op everywhere, including on the correctly-guarded single-wiki path directly above.
The two paths behave identically right now for exactly that reason - this is a landmine, not an
active leak.

**It becomes a live concealment bypass the moment `concealment_active` starts returning `True` for
anyone.** `?children=1` is reachable by any authenticated request naming a pin or wiki slug/uuid
with descendants - no elevated privilege needed. The fix `_resolve_owner()` already demonstrates at
line 180 is one call: wrap the `.filter(...)` result at `controllers/markup.py:274` in
`visible_rows(items, owner, profile)` the same way. (The `Pin` branch immediately above,
`controllers/markup.py:269-271`, needs no equivalent fix - concealment is a wiki-only concept, per
`_current_layer_is_visible`'s docstring at `controllers/markup.py:224-225`.)

Worth flagging now rather than after concealment ships: later in the same `get()` method, the
wiki-owner layer-visibility computation (`visible_layer_ids`, `controllers/markup.py:284-289`) *does*
call `visible_rows` correctly. So this is an inconsistency within one view - concealment applied
correctly a few lines below the exact spot it was skipped - rather than a case where nobody thought
to apply the filter at all, which is worth knowing before assuming this needs a wider audit than
just this one queryset.

## P90 — `backfill_wiki_edit_points`, extracted from its migration specifically to be testable, has no test

`id: P90` · `status: open` · `updated: 2026-09-08`

Found 2026-09-08 during the pre-merge audit of `release/v_0_8_0`; confirmed on an independent
adversarial pass.

`migrations/0032_v0_8_0.py` (the v0.8.0 squash of migrations 0032-0056, `9b3bb298a`) carries three
`RunPython` data migrations:

1. `_0049_backfill_friendinvitation_email_normalized` (line 14, wired line 319) - tested by
   `tests/hypothesis/test_friend_invitation.py`.
2. `_0052__backfill` (line 32, wired line 324) - calls
   `services.consensus.points.backfill_wiki_edit_points(WikiEdit)`. **No test references it
   anywhere in the tree**: `grep -rln "backfill_wiki_edit_points" src/urbanlens/dashboard/tests`
   returns nothing.
3. `_0054_merge_reciprocal_rows` (line 82, wired line 330) - tested by
   `tests/hypothesis/test_friendship_pair_uniqueness.py`, which caught a real bug before release
   (`21a48e652`, "migration 0054 aborted on exactly the rows it exists to merge").

Item 2's own docstring (`services/consensus/points.py:315-317`) says why it is a standalone function
at all: "Extracted from the migration that calls it so it can be exercised by a test - this repo has
no migration-test harness, so logic left inline in a `RunPython` is logic nothing runs until
deploy." Nobody wrote that test. Both siblings, extracted or already standalone for the identical
reason, got one - and sibling 3 is the proof the extraction pays for itself: its test found a real
ordering bug that would otherwise have shipped.

What this migration does, unreviewed by any test: marks every `WikiEdit` that is some other row's
`reverted_by` target as `is_revert=True`, then sets `consensus_points=MANUAL_EDIT_POINTS` on every
`WikiEdit` with an editor, no `consensus_round`, and `is_revert=False`
(`services/consensus/points.py:329-335`). It runs once, irreversibly - `_0052__backfill`'s
`reverse_code` is `RunPython.noop` (line 324) - against every production `WikiEdit` row that
predates `consensus_points` existing.

It reads `MANUAL_EDIT_POINTS` (`services/consensus/points.py:45`), the same constant
`points_for_changes()` prices ordinary edits with, and that function itself carries `TODO: reassess
this whole scheme once there is real usage to look at` (`services/consensus/points.py:150`) -
flagging the constant as a provisional first cut. An untested, irreversible, one-shot backfill
reading a constant its own module already says needs reassessment is exactly the combination the
extraction-for-testability was meant to catch before it reached production.

Suggested test shape, matching the siblings: call `backfill_wiki_edit_points` directly against real
`WikiEdit` rows (its docstring implies this was the intent of extracting it), seed a revert chain
plus a mix of consensus-scored, non-consensus, and already-scored rows, run it, and assert
`is_revert` and `consensus_points` land where `services/consensus/points.py:329-335` says they
should - in particular that a revert row's *own* award is left standing, per the docstring's explicit
"draining points people have already been shown is a bigger change... and is not what this is for."

## P91 — Seven of eight new security integration specs have never run against a live deployment

`id: P91` · `status: open` · `updated: 2026-09-08`

Found 2026-09-08 during the pre-merge audit of `release/v_0_8_0`; confirmed on an independent
adversarial pass.

`3547deb11` ("security related integration tests, not yet run -- needs review and expansion") added
eight spec files under `tests/integration/specs/security/`: `assumptions.spec.ts`,
`authorization.spec.ts`, `disclosure.spec.ts`, `input.spec.ts`, `isolation.spec.ts`,
`session.spec.ts`, `surfaces.spec.ts`, `transport.spec.ts`. Per-file `git log --oneline`:

```
assumptions.spec.ts    3547deb11 only
authorization.spec.ts  3547deb11 only
disclosure.spec.ts     3547deb11 only
input.spec.ts          3547deb11 only
isolation.spec.ts      3547deb11, 72890b5a4, 463a87eba
session.spec.ts        3547deb11 only
surfaces.spec.ts       3547deb11 only
transport.spec.ts      3547deb11 only
```

Only `isolation.spec.ts` shows evidence of a real run: `72890b5a4` added live regression coverage for
the media-gate and trip-photo fixes, and `463a87eba` ("fix: correct three false-positive marker
checks found running the new suite live") corrected assertions the suite itself proved wrong once
someone actually executed it against a deployed instance. The other seven are byte-identical to
their initial commit - never edited since being authored - which, given what `463a87eba` found in
their sibling, is what "never run" looks like: a spec that would need at least one correction if it
had ever been executed, and no evidence any of the seven has been.

Same risk class `docs/archive/PROBLEMS-ARCHIVE.md`'s P75 already documents shipping:
`disclosure.spec.ts:32` asserted `/dashboard/this-path-does-not-exist-91b2c/` returns 404 while the
`dashboard/` catch-all answered 200 for an unknown span of time, because - per that entry - "That
spec has only ever run when someone triggered it by hand - `integration.yml` is `workflow_dispatch`
only, deliberately, because it drives a deployed instance - so an assertion encoding the correct
behaviour sat next to code that could not satisfy it, and nothing said so." The underlying 404 bug
is fixed (P75, resolved 2026-09-05), but it was found by a different investigation (P35's
hardcoded-URL audit), not by running this file - so `disclosure.spec.ts` remains one of the seven
with no evidence it has ever caught anything by being executed, the exact gap P75 describes.

**In progress, not newly discovered as unaddressed.** The integration suite, including this security
directory, is being run against a live dev instance as part of this same release audit, with results
pending separately. This entry is not claiming nobody is acting on it - it records that, as of the
state on disk, seven of the eight files have no evidence of ever having executed, so whoever reviews
the pending run's results should close or narrow this entry against what that run actually finds,
rather than leaving it open by default once the run completes.

## P92 — `map-clusters.ts`'s cluster badge constants are duplicated, not shared, by the main map's inline script

`id: P92` · `status: open` · `updated: 2026-09-08`

Found 2026-09-08 while closing out the pre-merge audit of `release/v_0_8_0` (the "audit wasn't done yet" tail of
that pass, not a new sweep - see the audit's own confirmed finding "map-clusters.ts's shared cluster-icon module
was never wired into the main map it claims to cover").

`shared/map-clusters.ts`'s module docstring used to claim it was shared by "the main map's inline cluster layer,
and the pin-detail / wiki maps." That was only half true: `entries/map-annotations.ts` (the pin-detail/wiki map
entry) does import and use `createPinClusterGroup`/`pinClusterIconParts` from it (`detailPinLayer`), but the main
`/map/` page's own inline `<script>` (`templates/dashboard/pages/map/index.html:979-999`) never imports this
module at all - it hand-rolls its own `L.markerClusterGroup` call with its own copy of the badge sizing table and
`iconCreateFunction`:

```js
var clusterGroup = L.markerClusterGroup({
    ...
    iconCreateFunction(cluster) {
        const n   = cluster.getChildCount();
        const siz = n < 10 ? 's' : n < 100 ? 'm' : 'l';
        // Must match the width/height of .pin-cluster--{s,m,l} in _map.scss - a
        // mismatch here makes the flex-centered wrapper squash into an oval.
        const px  = { s: 34, m: 42, l: 50 }[siz];
        return L.divIcon({ html: `<div class="pin-cluster pin-cluster--${siz}"><span>${n}</span></div>`, ... });
    },
});
```

versus `map-clusters.ts`'s `PIN_CLUSTER_PX = { s: 34, m: 42, l: 50 }` and `pinClusterIconParts()`, which produce
the identical `html`/size. The two are hand-kept in lockstep today (both docstrings separately say "must match
`.pin-cluster--{s,m,l}` in `_map.scss`"), but nothing enforces that agreement - a future change to either the
threshold counts (`< 10`/`< 100`) or the pixel sizes in one place silently stops matching the other, and CSS is
the only place both would visibly disagree (a squashed-oval badge on one map but not the other).

Docstring corrected in the same pass this entry was filed (no longer overclaims shared coverage), but the actual
duplication is unfixed. Not fixed here because the real fix isn't a one-liner: the main map's clustering code is
inline template JavaScript, which cannot `import` a TS module - unifying it needs either (a) a mechanism for an
inline `<script>` to read shared constants/functions from a bundled entry (no such mechanism exists anywhere else
in this codebase today, per a search for `window.UL =`/`globalThis.UL =`), or (b) migrating the main map's inline
script into a proper bundled TS entry the way `map-annotations.ts` already is for pin-detail/wiki maps - which is
the broader, already-tracked P83/P34 initiative ("over half of every page's HTML is inline `<script>`"), not a
scoped fix for this one badge. Left open and cross-referenced from both rather than attempted piecemeal here.

## P93 — Nine REData plugins declare no rate-limit defaults for their own gateway's service key

`id: P93` · `status: open` · `updated: 2026-09-08`

Found 2026-09-08 in the pre-PR audit of `release/v_0_8_0`, while fixing the same gap on the release's new
`HistoricalFeaturesPlugin` (`redata_historical_features.py`, fixed in the same commit as this entry - its
`get_service_defaults()` now declares `redata_historical_features` and is **not** one of the plugins below).

`dashboard/CLAUDE.md`'s "API Integrations" section says a plugin subclass "declares its rate-limit defaults." Nine
pre-existing REData plugins don't: they define a gateway with its own `service_key`, but no
`get_service_defaults()` override, and that key appears in neither `rate_limiter.SERVICE_REGISTRY` nor any
plugin's declared defaults. `rate_limiter.get_limit_config()` never sees these keys as configured, so the first
call to any of them silently creates an `ApiRateLimit` row from the generic fallback baked into
`get_limit_config()` itself (`calls_per_minute=20`, `calls_per_day=500`, a `.title()`-cased display name, no
`notes`) instead of a number anyone actually reasoned about for that integration.

Affected plugin files (`dashboard/plugins/builtin/`) and the ungoverned service key(s) each one's gateway declares:

- `redata_air_quality.py` → `redata_air_quality`
- `redata_underground.py` → `redata_underground`
- `redata_hydrology.py` → `redata_hydrology`
- `redata_permits.py` → `redata_permits`
- `redata_incidents.py` → `redata_incidents`
- `hazard_history.py` → `redata_hazards`
- `usgs_earthquakes.py` → `redata_hazards` (same key as `hazard_history.py` - two plugins share one ungoverned
  budget)
- `open_elevation.py` → `redata_elevation`
- `redata_site_conditions.py` → `redata_land_cover`, `redata_soil`, `redata_walkability` (three keys from one
  plugin)

Not fixed here: each of these ten keys needs its own considered `calls_per_minute`/`calls_per_day` pair and
`notes` explaining the choice (per-endpoint, referencing REData's `api-reference.md` "Rate limiting" section, the
way `redata_historic_registers.py` and the now-fixed `redata_historical_features.py` do) rather than a single
mechanical pass copying the same numbers into all nine files - that judgment call belongs with whoever does the
fix, not rushed to close this entry out. `dashboard/tests/hypothesis/test_plugin_rate_limit_coverage.py` will not
catch this class of gap on its own: it asserts every key present in `all_service_defaults()` has *a* limit, but a
key that was never registered at all - like these - is simply absent from that mapping rather than showing up
`unlimited`, so the existing test passes today with all nine still ungoverned.

## P95 — `ExtractionBudget` cannot bound a single file's decompression, and nothing prices what parsing one costs

`id: P95` · `status: open` · `updated: 2026-09-10`

Related to P2 (`parse_for_preview` runs archive/KML/GPX/OSM/WKT/shapefile parsing in the request,
blocking `UL_UNTRUSTED_PARSE_POLICY=deny`) - cross-referenced rather than duplicated: P2 is about
sandboxing that code path, this is about the resource cost of it regardless of sandboxing.

`controllers/pin.py:1181` `parse_for_preview` builds one `ExtractionBudget()`
(`services/import_export/archive_extractor.py:63-84`, 2 GB / 1000 files) shared across every
uploaded file and every nested archive - closing the "an outer ZIP holding N nested bombs costs N
x 2 GB" hole the budget's own docstring names. It does not close the shape one level up: the
budget caps *total* uncompressed bytes across the whole upload, not what one file, one KML, one
shapefile, or one `.docx` can cost by itself. `GoogleMapsGateway.parse_for_preview`'s CSV/geocode
branches and `extract_pins_from_document`'s AI branch (`services/ai/document_import.py`) are
separately capped by `MAX_PREVIEW_PINS = 20_000` (`services/apis/locations/google/maps.py:946`) -
a pin-*count* backstop against the eventual output, not a cost bound on the parse that produces it.

nginx bounds the compressed body to 200 MB (`config/nginx/django.conf:42`, `client_max_body_size
200m`), which bounds bytes *in transit*, not bytes *after decompression* - up to the 2 GB
`ExtractionBudget` ceiling, entirely inside one authenticated gunicorn worker, per POST. The only
rate control on this endpoint is the global DRF `user` throttle at `600/minute`
(`settings/base.py:1288`) - a request-*count* budget, not a cost-scoped one, so an account can
submit 600 near-2GB extractions a minute exactly as cheaply as 600 single-KB ones, as far as the
throttle is concerned.

Not fixed: needs either a per-file byte/complexity cap inside `ExtractionBudget` or a cost-scoped
throttle (e.g. keyed to declared upload size) alongside the existing count-based one. Not measured
this session - no benchmark run against a 2 GB adversarial upload; the risk is by inspection of the
cap values above, not an observed timeout.

## P96 — `import_confirmed`'s SSE import creates as many Pins as the client claims, synchronously in the web worker

`id: P96` · `status: open` · `updated: 2026-09-10`

`controllers/pin.py:2051` `import_confirmed` reads `request.data["lists"]` with no length cap and
streams `GoogleMapsGateway.import_preview_streaming` (`services/apis/locations/google/maps.py:1094`)
back as an SSE response - each list's `pins` array (also uncapped at this layer) is created as a
`Pin` row in the same request/worker that opened the stream. The only upstream limit is
`parse_for_preview`'s `MAX_PREVIEW_PINS = 20_000` (`services/apis/locations/google/maps.py:946`) on
the *preview* step. `import_confirmed` is a separate endpoint that trusts whatever JSON body the
client posts back to it, not the server's own preview output, so nothing stops a client from
replaying or hand-building a `lists` payload past that cap. A large confirmed import ties up one
gunicorn worker (see P104/R28 on why that worker is gevent, not threads) for the duration of every
`Pin.objects.create()` plus its `post_save` signal fan-out (`models/pin/signals.py`; the
O(pins carrying a label) part of that was P102 and is fixed, but each created pin still pays its own
receivers).

**Measured 2026-09-10, and the number is worse than "ties up a worker".** Against the development
stack, otherwise idle:

| import | account it went into | result |
|---|---|---|
| 500 pins | 20,000 existing | **200, 116.6 s** |
| 500 pins | empty | **504 at 120.0 s** — while the server finished all 500 anyway |

**233 ms per pin, and the cost does not come from the account.** Two imports of the same size into a
20,000-pin account and an empty one both took about two minutes, so this is per *imported* pin, not
per existing one. There is no size at which it is fast.

**nginx cuts it off before any realistic import can finish.** `config/nginx/django.conf:50` sets
`proxy_read_timeout 120s` on the app location, so at 233 ms/pin the ceiling is about **510 pins** -
and the failure is silent in the worst direction. The empty-account run above returned nginx's 504
page to the client and *then went on to create all 500 rows*: a fully successful import that reports
a gateway error. A user would reasonably retry, spending another two minutes re-matching the pins
they already have, and get another 504.

For scale, `parse_for_preview`'s own cap is `MAX_PREVIEW_PINS = 20_000`. At this rate that is
**78 minutes in one request** - which nginx would end at two minutes, twenty times over, while the
work continued.

So the cap this needs is not only a guard against an adversarial payload; the ordinary path is
already past the point where the endpoint can report its own success. Whatever replaces it has to
return before the work does - the task-plus-progress shape in PL7 phase 6 - rather than being made
faster.

Reproductions in `dashboard/tests/hypothesis/test_import_confirmed_cap.py` are `xfail(strict=True)`
and turn red when a cap lands.

## P97 — `dissolve_polygons` is O(n^3) GEOS work over an uncapped user-supplied polygon count

`id: P97` · `status: open` · `updated: 2026-09-10`

`services/geo/geo.py:84` `dissolve_polygons` merges intersecting polygons by restarting an O(n^2)
pairwise scan (`for i in range(len(clusters)): for j in range(i + 1, len(clusters))`,
`geo.py:111-119`) after every merge found, so a fully-chained input (each polygon touches the next)
costs O(n^3) `GEOSGeometry.intersects()`/`.union()` calls. `saved_filters.py:83`'s
`_dissolve_regions` (`saved_filters.py:63-83`) calls it once per `include_regions`/`exclude_regions`
key parsed from `SearchForm.parse_region_geojson`, with no cap on how many polygons that GeoJSON
form field may contain. Reachable via the saved-filter create/update endpoints.

**Measured 2026-09-10, and the severity does not hold.** In the app container: 400 disjoint
polygons dissolve in 0.030s, 400 fully-chained overlapping polygons in 0.030s, and two overlapping
200,000-vertex polygons in 0.026s (an 8.65 MB GeoJSON payload parses in 0.803s, which dominates).
The restart converges much faster than the bound suggests, because each merge does `del clusters[j]`
and breaks, shrinking the working set - a chain of n collapses in far fewer than n full scans.

What was *not* tested is an adversarial ordering that forces the intersecting pair to be found last
on every pass, which is what the O(n^3) bound actually requires. So the bound stands as a bound and
the input remains uncapped, but no realistic or chained input reaches it, and the practical exposure
is the GeoJSON payload size (bounded by nginx `client_max_body_size 200m`) rather than the polygon
count. Downgraded from a hazard to a latent bound; not worth a rewrite at these numbers.

Worth recording for whoever does revisit it: GEOS answers this natively.
`MultiPolygon(polygons, srid=4326).unary_union` was verified to produce identical results on the
cases this function's own tests cover - chained overlaps merge to one component, disjoint stay
separate, touching merge, SRID preserved - in one call instead of the pairwise loop.

## P98 — The site-admin system panel re-walks the whole media tree on every load, gated only by admin permission

`id: P98` · `status: open` · `updated: 2026-09-10`

`controllers/site_admin.py:1617` `SiteAdminStatsSystemPartialView.get` calls
`_dir_size_mb(media_root)` (`site_admin.py:85-93`, called at `site_admin.py:1637`) - an uncached
`os.walk` plus `os.path.getsize` per file - on every HTMX poll of that partial, gated only by
`_AdminPermissionMixin` with no additional dev-only or rate gate. gevent's monkey-patching covers
sockets, not filesystem syscalls, so `os.walk`/`os.path.getsize` block the worker's shared OS
thread for their full duration regardless of the WSGI worker class (see P104/R28). Cost scales
with total files under `MEDIA_ROOT` - the whole site's stored media - not with anything scoped to
the admin viewing the page.

Not fixed. Not measured this session - no timing taken against a production-sized media tree.

## P100 — Map search-box autocomplete runs 8 leading-wildcard `ILIKE`s with zero trigram indexes to serve them

`id: P100` · `status: open` · `updated: 2026-09-10`

`services/map_pins/autocomplete.py:48` `search_local`'s pin branch (`autocomplete.py:87-100`) ORs
nine `icontains`/leading-wildcard lookups (`name`, `aliases__name`, `description`,
`labels__name`, `location__official_name`, `location__wiki__name`, `location__wiki__aliases__name`,
`location__wiki__description`, plus `tag_match_q`) across a `select_related`/`prefetch_related`
spanning `location__wiki`, `parent_pin`, `parent_pin__location`, and finishes with `.distinct()` -
fired on every keystroke, scoped to `profile=profile` so cost scales with the viewer's own pin
count. `pg_trgm` is installed (its `CREATE EXTENSION IF NOT EXISTS` appears in every dump per
`docs/BACKUPS.md`), but no `GinIndex`/trigram index exists anywhere in the migration history
(confirmed: zero matches for `GinIndex`/`trigram`/`pg_trgm` across
`dashboard/migrations/*.py`) - a leading-wildcard `icontains` cannot use a plain b-tree index
regardless, so every one of these OR branches is a sequential scan whether or not `pg_trgm` is
present. **Measured 2026-09-10, and it is not the next 504.** Against a seeded 10,000-pin profile (with
`ANALYZE` run), `search_local` costs 141-217ms per keystroke across four search terms, of which only
0.070s is SQL across 4 queries; the slowest single query runs in 0.035s and its
`EXPLAIN (ANALYZE, BUFFERS)` shows 394 buffer hits and 0.794ms actual time. The claim that every OR
branch is a sequential scan is wrong: the planner serves it with an Incremental Sort off the
presorted `dashboard_user_pins.id` key under the `Limit`, so it never materialises the full match
set. The missing trigram index is real and would still be the right thing if this ever grows, but
adding one now buys a fraction of 70ms at the cost of a migration. Left open as an accurate
observation, downgraded from a hazard.

Not fixed. Not measured this session.

## P103 — `MEDIA_PIPELINE.md`'s "every parser is now guarded" was false; a label-icon resize decodes unsandboxed in-request

`id: P103` · `status: open` · `updated: 2026-09-10`

`docs/MEDIA_PIPELINE.md:46` stated "Every parser is now guarded" - corrected in place this session
(see `MEDIA_PIPELINE.md`'s guard table and N-record on doc corrections). It was false:
`controllers/labels.py:253` `_resize_custom_icon` imports `PIL.Image` directly and calls
`Image.open(uploaded_file)` with no `@untrusted_parse` decorator, no sandbox routing, and no
size/pixel-bomb guard beyond Pillow's own defaults - it runs inline in whatever request calls it
(`_apply_custom_icon_upload`, `labels.py:556`), decoding and re-encoding any icon file over
`_ICON_MAX_PX` on the label create/edit path. Two further undecorated Pillow call sites were named
by this investigation but not re-confirmed independently this session - re-grep
`dashboard/services`/`dashboard/controllers` for `from PIL import`/`PIL.Image` sites lacking
`@untrusted_parse` before trusting the count is still exactly three.

Not fixed: `_resize_custom_icon` needs the same `@untrusted_parse`/sandbox routing as the other
Pillow call sites, or an explicit, documented reason it is exempt.

## P104 — Celery can starve the web tier by exhausting Postgres connections, not CPU; this already caused an 11-hour outage

`id: P104` · `status: open` · `updated: 2026-09-10`

See R28 (`docs/notes/wsgi-worker-model-and-connections.md`) for the WSGI-worker-model history this
sits alongside - that reference frames a decision still open; this records a defect already proven
live.

No `cpu_shares`, `cpuset`, or reservation exists anywhere in `docker-compose.yml` - every service's
`cpus:` is a CFS *ceiling*, not a floor, and the ceilings sum to ~20.75 CPU across `app` (2,
`docker-compose.yml:325`), `app-ws` (1, `:411`), `nginx` (2, `:458`), `media-nginx` (2, `:518`),
`db` (2, `:564`), `celery-worker` (2, `:618`), `celery-worker-panels` (1, `:668`), `media-worker`
(2, `:734`), `media-worker-batch` (1, `:766`), `celery-beat` (0.5, `:808`), `celery-metrics` (0.5,
`:867`), `clamav` (2, `:903`), `valkey` (1, `:951`), `egress-proxy` (0.25, `:997`), `ai-inference`
(0.5, `:1082`), `ai-worker` (1, `:1150`). A CPU limit on Celery would not have prevented the
incident below, because CPU was never the shared, exhaustible resource - **Postgres connections
are**: `max_connections` is the stock 100 (no override anywhere in the tree - confirmed via the
`db` service's environment block, `docker-compose.yml:544-546`, and `settings/base.py`),
`CONN_MAX_AGE=0` (`settings/base.py:272`), there is no connection pooler anywhere in the tree, and
every container connects as the same `POSTGRES_USER` role (`docker-compose.yml:544`), so there is
no per-role cap even in principle. `docker-compose.yml`'s own comment on the `db` service
(`:560-563`) estimates "~25-30 simultaneous backends" against `WEB_CONCURRENCY` gunicorn workers
plus daphne plus both Celery workers; the web tier is additionally unbounded on top of that because
gevent's per-worker greenlet count is not itself capped by `WEB_CONCURRENCY` (that variable sets
*worker process* count, not concurrent-request count within a worker).

**This already happened**: an 11-hour production outage with the database reporting 97/100
connections idle and 3,360 `FATAL: sorry, too many clients already` (Postgres error 53300) events
logged. Not re-investigated this session for the incident's own postmortem/timeline - recorded here
as the evidence that the connection ceiling, not CPU, is the resource that actually ran out.

**Half of it is measured as fixed, 2026-09-10.** This entry names the web tier's unbounded greenlet
count as what makes the demand "additionally unbounded" on top of the ~25-30 estimate. That is now
bounded and the bound was measured, not assumed: under a 60-user filter storm on a staging-model
environment, Postgres peaked at **61/100 backends, 60 of them `urbanlens-web`, 0% idle** — exactly
`--worker-connections 20 × 3 workers`, to the connection (X15). The same work on the unbounded
development server reached 75. The web tier can no longer be the thing that fills the pool.

`cpu_shares` weights landed at the same time, which this entry correctly says would not have
prevented the incident; they are there for a different reason and are not a fix for this.

**Still not fixed, and the entry's conclusion stands for the rest:** there is no pooler, no per-role
`CONNECTION LIMIT`, and every container still connects as the same role, so the remaining ~40 slots
are shared by daphne, four Celery workers, beat and the AI tier with no budget between them. One of
them can still starve another; the web tier just is not the one doing it any more.

**And the outage's own shape has not been reproduced.** The chaos run that tried (X16) held slots as
an external superuser rather than as the application's own role, so the app's existing connections
were never the ones taken and it kept serving at 100/100. `chaos.py`'s `connection-exhaustion`
scenario holds them *as the application's role*, which is the faithful version; it could not be run
because that tool cannot dispatch (N20).

## P105 — A Valkey outage 500s every request after 32 seconds, including the readiness probe

`id: P105` · `status: open` · `updated: 2026-09-10`

**Measured 2026-09-10, and it is much worse than this entry originally claimed.** The heading used to
read "locks every user out of logging in, while already-signed-in browsing keeps working". The
source reading below is still correct about *which* code paths are wrapped. The conclusion drawn from
it was wrong, because it reasoned about correctness and not about time.

Valkey paused on a staging-model environment, requests made with an **already-established** session:

```
  /health/ready                    500 in 32.16s
  /dashboard/map/pins/?limit=5     500 in 32.15s
  /dashboard/map/                  500 in 32.17s
```

Not "keeps working". Not even a fast failure. **Every request 500s after about half a minute**, and
that includes the readiness endpoint, which is supposed to be the thing that still answers when
nothing else does.

Two consequences that the source reading could not have produced:

- **32 seconds is the number that matters, not the 500.** `socket_connect_timeout: 1` and
  `socket_timeout: 2` are configured (`settings/base.py:320-321`), so one cache call fails in ~2s.
  Reaching 32 means roughly sixteen cache operations per request, each waiting its own timeout —
  serially. With `--worker-connections 20 × 3 workers`, whole-site throughput during a Valkey outage
  is about two requests a second.
- **`/health/ready` fails the same way.** It answers 500 after 32s, so a readiness probe with any
  sane timeout records a timeout rather than a verdict, and orchestration removes the instance. This
  is the concrete case behind the warning sent to infrastructure in N20: the failure mode is a
  *timeout*, which a probe may treat differently from a non-200.

The chaos probe that produced this is `bin/perf/chaos_probe.py`; it was run by pausing the container
directly, because `chaos.py inject` cannot dispatch (N20).

What the fix has to achieve is therefore larger than "wrap the unwrapped paths": a request that
cannot reach the cache must give up in about the time one call takes, not sixteen. The session cache
wrapper in D11 §2.6 is still right and is no longer sufficient on its own.

The original reading, still accurate:

`SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"` with `SESSION_CACHE_ALIAS =
"default"` over the stock `django.core.cache.backends.redis.RedisCache`
(`settings/base.py:289-307`). The settings comment there says "cached_db writes through to the
database so sessions survive a cache flush", which is true for a *flush* and, it turns out, for
most of an *outage* too - but not all of it.

Django wraps the two paths people assume break, and leaves three unwrapped
(`django/contrib/sessions/backends/cached_db.py`, Django 6.0.6):

- `load()` catches bare `Exception` and falls through to `_get_session_from_db()`. A signed-in
  request therefore keeps working with Valkey down.
- `save()` catches bare `Exception` and logs. Session writes keep working.
- `exists()` does `(prefix + session_key) in self._cache` with **no** guard. `_get_new_session_key()`
  calls it in a loop, and `create()` calls that - so `cycle_key()`, which Django's login does, raises.
- `delete()` calls `self._cache.delete(...)` with **no** guard, and `flush()` calls `delete()` - so
  logout raises.

The login path fails even earlier, in this codebase's own code rather than Django's: `LoginView`
calls `_is_locked_out()` (`controllers/account.py:873` → `:67-68`), which is a bare `cache.get()`.
The brute-force counters around it (`:92-97`, `:197`, `:261`, `:286`, `:310`, `:343`) and the
passphrase/password rate limiters (`:1395-1398`, `:1441-1444`) are the same shape. Other unguarded
request-path callers: `controllers/immich.py:96,100,286,295` (scan status, thumbnail proxy) and
`controllers/flickr.py:126` (OAuth request token).

So the outage profile is: **existing sessions browse fine; nobody can log in or out; a signed-out
user cannot get in at all.** Worth stating because the intuition ("Valkey is a cache, the site
degrades") is right about pages and wrong about the door.

Note before fixing: `_is_locked_out` failing *open* would be worse than failing closed - it is the
brute-force gate. A wrapper that turns cache errors into misses must not be applied blindly to the
lockout keys; those want an explicit decision (fail closed with a 503, or fall back to a DB-backed
counter), not a silent miss.

One thing to know before reproducing this by pausing Valkey: that does not isolate the cache.
`CELERY_BROKER_URL` falls back to `VALKEY_URL` (`settings/base.py:374`) and `UL_CELERY_BROKER_URL` is
unset by default, so the same instance is the cache, the session store, the channel layer, the
result backend *and* the broker. Pausing it exercises task enqueueing as well, and a run that reads
as "P105 plus something else" is that coupling rather than a second defect. Found by the
infrastructure repo while building the chaos scenarios (N17); it is the sharper half of the argument
for D11's Valkey split, which had been justified on the fill case alone.

Not fixed. See D11 for the Valkey split this sits inside; the chaos scenario is the reproduction.

## P107 — The saved-filter count badges read every pin in the account to draw a number

`id: P107` · `status: open` · `updated: 2026-09-10`

`SavedFilterMatchCountsView.get` (`controllers/saved_filters.py:314`) returns one count per saved
filter — a body sized by how many filters a profile has, which does not grow with the pin table at
all. It builds that body by materialising every root pin's uuid in Python:

```python
base_uuids = {str(u) for u in base_query.values_list("uuid", flat=True)}
```

and then intersects that set with each filter's own cached uuid set. The map page calls this endpoint
on every filter change, so the cost of drawing a badge is one full read of the account's pin table.

The set-intersection design above it is deliberate and good — the comments record that it replaced
`O(F^2)` chained `.filter(uuid__in=...)` queries for F saved filters, and the projection is the right
call. What was not reconsidered at the same time is that the *base* set is unbounded.

**Why nothing caught it.** This is the first defect found by the rows-fetched axis, and it is
invisible to all three older instruments: `QueryScalingMixin` sees one statement at every size
(correctly — there *is* one), a bytes-per-row budget sees a body that never moves (correctly — it
never does), and `InstantiationScalingMixin` sees no model objects (correctly — `values_list` builds
none). Only "rows read per rendered row, under the capped budget" separates it from a healthy list
endpoint, which reads the same one row per row and renders them.

Guarded by `dashboard/tests/hypothesis/test_saved_filter_counts_scaling.py`, whose reproduction is
`xfail(strict=True)` and turns red when this is fixed.

Not fixed, and not measured against a large account this session — the complexity is read from the
code. The fix is to count in the database (a `COUNT(*)` per filter, or one grouped query over the
filter/pin join) rather than by intersecting Python sets, at which point the per-filter uuid cache
this depends on may stop earning its keep too.

## P109 — One import's task fan-out fills the only Celery queue for hours, and a safety task waits behind it

`id: P109` · `status: open` · `updated: 2026-09-10`

Importing **1,000 pins** left **2,644 tasks** on the default `celery` queue — about 2.6 tasks per
pin, from the `Pin` post_save signal chain. A 400-task sample of the queue:

| tasks | name |
|---|---|
| 152 | `enrich_wiki_location` |
| 152 | `suggest_wiki_category` |
| 27 | `score_reputation_event` |
| 27 | `evaluate_achievements_for_profile` |
| 26 | `ensure_wiki_for_location` |
| 3 each | `warm_saved_filter_cache`, `sweep_stalled_{consensus,trivia,spotguessr}_sessions` |
| **1** | **`escalate_overdue_checkins`** |

That last row is the problem. **A safety-escalation task was queued behind ~2,640 tasks generated by
one user's import**, on the one queue everything shares. PL7 §2.5 predicted this ("the safety
check-in chain is interactive and must never queue behind a backup"); this is it measured.

**It drains at 0.2 tasks a second** — measured over 30 s, 2,644 → 2,638. That is **220 minutes** to
clear. The enrichment tasks are network-bound on rate-limited external providers, not CPU-bound; the
worker log is full of `429` from Places and Overture while it waits.

**The worker runs out of memory doing it.** `celery-worker` sat at its full **3 GiB** limit, and the
log carries `WorkerLostError: Worker exited prematurely: signal 9 (SIGKILL)` — a child OOM-killed
mid-task, which loses that task's work.

**The tail outlives the request that caused it, and destabilises what comes next.** This was found
because it killed something else: a later load run was terminated for host memory pressure *during
its idle phase*, before it had done any work at all, because the backlog from an import two runs
earlier was still resident and still draining. The user who ran the import saw a 504 two minutes in
(P96) and has been gone for hours.

**Not a defect in `ensure_wiki_for_location` itself.** Wiki enrichment is community data about a
`Location` rather than the importing user's own lookups, so it deliberately is not gated on that
profile's `external_apis_enabled` — which is correct, and also why an integration account's
cost protections do not cover it. The defect is that there is one queue, no class of service on it,
and no bound on how much of it one action may occupy.

Worth noting for the harness: `perf_seed.seed_heavy_account` uses `bulk_create` and so fires no
signals at all — 20,000 seeded pins produce zero tasks. Every task counted above came from the
import path. That is the right behaviour for a load fixture and it is why the seeded account is not
also carrying a backlog.

The fix is PL7 phase 6's queue classes: `interactive` / `bulk` / `maintenance` with separate workers,
so an import's fan-out cannot delay a safety escalation, plus a bound on how many tasks one request
may enqueue. Until then the mitigation is operational — an import of any size should be assumed to
cost hours of background work on a shared queue.

**Half of it is fixed for development, 2026-09-10.** `rate_limiter.outbound_calls_permitted` now
refuses outbound provider calls on `development` and `local` deployments unless
`UL_ALLOW_OUTBOUND_APIS=true`. Same code path everywhere — a refused call raises
`ServiceDisabledError` from the same place a rate limit does, which every caller already handles,
so nothing is mocked and no fixture has to track a provider's real response shape.

Measured before and after, importing 50 pins on the same stack:

| | before | after |
|---|---|---|
| tasks enqueued | 141 | 150 |
| successful outbound calls | many | **0** of 247 attempts |
| drain rate | 0.2/s | **≥2.5/s** |
| queue cleared | ~220 min (never observed to finish) | **under 60 s** |

That does *not* fix the shape of the problem, and it does nothing for staging or production, where
the calls are supposed to happen: one queue, no class of service, and one action able to fill it are
all still true. What it removes is a development box quietly spending a real budget, and the
several-hour tail that made a dev stack unusable after any import.

Two traps found while doing it, both recorded in
`dashboard/tests/hypothesis/test_outbound_api_policy.py` as tests rather than comments:

- **`app_settings.environment_name` is not `UL_ENVIRONMENT`.** It is a separate Pydantic field
  defaulting to `local`, and the dev stack reports `development` for one and `local` for the other.
  A production deployment that never sets `UL_ENVIRONMENT_NAME` reports `local` too, so a guard
  reading it would have refused every outbound call in production. The guard reads
  `django.conf.settings.ENVIRONMENT_NAME`, which `settings/base.py` already branches on.
- **The suite inherits its container's `UL_ENVIRONMENT`**, which is a development one, so without an
  explicit exemption every gateway in every test would have been disabled. Four rate-limiter tests
  failed exactly that way before `settings.TESTING` was added to the guard. Making the suite
  hermetic by force is worth doing and is a different change.

## P110 — The Overture OOM fix is best-effort, and Overture rate-limiting us is what turns it off

`id: P110` · `status: open` · `updated: 2026-09-10`

A recurrence of
[the problem resolved 2026-08-31](archive/PROBLEMS-ARCHIVE.md), under a condition that resolution
did not consider. That fix passes `stac=True` to `overturemaps.geodataframe()` so a bbox resolves
against Overture's small STAC index to the handful of S3 partitions that intersect it, instead of
opening the whole theme. It works — when the STAC index answers.

**The library falls back to the unbounded path on any STAC failure, silently.**
`overturemaps/core.py:185-187` catches every exception, prints, and returns `None`; the caller then
does:

```python
dataset = ds.dataset(
    intersecting_files if intersecting_files is not None else path,   # path = the entire theme
```

So `stac=True` is a request, not a guarantee, and the failure is a `print` rather than a raise.

**The trigger is self-inflicted, which is what makes it a loop.** Enrichment tasks call Overture per
location; enough of them earn `HTTP Error 429: Too Many Requests` from
`https://stac.overturemaps.org/2026-08-19.0/collections.parquet`; the 429 disables the narrowing;
the un-narrowed reads then allocate gigabytes each. The more enrichment is queued, the more certain
the mitigation is to be off exactly when it is needed.

Observed 2026-09-10 on the development stack, during the load suite's import phase:

```
Thread 327 (idle): "MainThread"
    arrow_to_geopandas (geopandas/io/_geoarrow.py:476)
    from_arrow (geopandas/geodataframe.py:952)
    _fetch (urbanlens/dashboard/services/apis/locations/boundaries/overture_maps.py:130)
    get_buildings (.../overture_maps.py:154)
    generate_location_boundaries (urbanlens/dashboard/services/locations/boundaries.py:328)
    enrich_wiki_location (urbanlens/dashboard/tasks.py:157)
```

with the worker's four children at **1,743 MB / 831 MB / 342 MB / 233 MB** against a 512 MiB
`CELERY_WORKER_MAX_MEMORY_PER_CHILD` and a 3 GiB container limit. That setting is checked *between*
tasks, so a single task that allocates 1.7 GB is never caught by it — the archived entry called it
"defense in depth, not a fix", and this is the case it does not defend.

**What it costs, measured.** The load suite's `import_confirmed` phase (X15) with this happening:

| phase | neighbour p95 |
|---|---|
| idle | 241 ms |
| during the import | 11.2 s |
| **cooldown, after the import finished** | **60.0 s (timeout)** |

Cooldown is worse than the acting phase. 19.2% of the neighbour's requests failed and
`/health/ready` itself began timing out — *after* the user who ran the import had received their
504 and gone.

**Not fixed by the outbound guard added the same day.** `OvertureMapsGateway` sets
`service_key = None` ("no HTTP endpoint of ours to rate-limit"), so `Gateway.__post_init__` never
wraps its session, and the reads happen inside `pyarrow`/`S3FileSystem` rather than through
`self.session` at all. Nothing in `rate_limiter` sees them: the guard reported zero successful
outbound calls while this was reading from S3 throughout. That is the documented bypass
(`dashboard/CLAUDE.md`: "code that bypasses `self.session` — a bare `requests.*` call, an SDK
client"), and it is worth knowing that the largest consumer of both memory and egress is on it.

**Fixed 2026-09-10.** `OvertureMapsGateway._require_narrowing` resolves the file list itself before
reading and raises `GatewayRateLimitedError` when the index cannot answer — the error that already
means "the provider's budget is exhausted, stop early", which scheduled enrichment already catches.
Falling back to scanning the planet is never what we want, and nothing in the library's API can
express that to it.

A refusal opens a **short per-process circuit** (120s). Without one, every queued enrichment task
would keep probing an index that is refusing us, which is the loop that earned the rate limit — the
breaker turns a self-amplifying failure into a self-limiting one. Per process rather than shared, so
it still works when Valkey is down, which is exactly when a lookup storm is least welcome; a pool of
four children probes at most four times a window instead of once per task.

Known cost: one extra read of the (small) index on the healthy path, because the library re-resolves
it and will not accept a resolved file list. Worth paying to never scan the theme, and it disappears
if `overturemaps` grows a strict mode or takes the files.

**A second defect, found by shipping the first.** `_get_files_from_stac` calls `urlopen(stac_url)`
with **no timeout at all**, so a stalled connection parks the calling thread indefinitely — and this
is reached from the request path (the pin-detail panels), not only from tasks. Adding the
precondition without a deadline wedged the development app immediately: every worker thread parked,
`/health/ready` timing out, **0% CPU** — a different signature from P108's spin, and the same
outcome. The lookup now runs under `call_with_deadline` at 15s with `default=None`, which joins the
timeout to the refusal path: an index too slow to answer is as useless as one that refuses, and both
stop the read rather than let it widen.

**Verified end to end on the development stack**, importing 50 pins:

| | before | after |
|---|---|---|
| import request | 504 at 120 s (app wedged) | **200 in 12.3 s** |
| queue drain | 0.20/s | **2.57/s — 154 tasks to zero in 60 s** |
| worker memory | pinned at 3 GiB, children SIGKILLed | **896 MiB** |
| `/health/ready` | timing out | 200 in 58 ms |

Of 50 building lookups, **4 probed the index and 46 were refused by the circuit without touching the
network** — which is the breaker doing exactly its job. No `arrow_to_geopandas`, no `WorkerLostError`.
Note that `stac.overturemaps.org` is not reachable from this box, so the *narrowed* path was
exercised only in tests; what was verified live is that being unable to narrow now costs a fast
refusal instead of a planet scan.

Still open in this entry, and the reason it is not archived: **the reads remain invisible to the rate
limiter and to the outbound-call guard.** `service_key` is still `None` and the parquet reads still
go through `pyarrow`/`S3FileSystem` rather than `self.session`, so nothing bounds how often
enrichment reaches Overture in the first place. The circuit breaker bounds the *damage* of a refusal;
it does not bound the request rate that earns one.

## P111 — A gunicorn worker's memory is set by peak concurrent response size, and it never gives it back

`id: P111` · `status: open` · `updated: 2026-09-10`

Measured on a `--environment staging` dev environment — the first time this project's real process
model has been run and looked at.

**A fresh worker is 347 MB**, and that is reproducible from the import path alone:

| stage | RSS |
|---|---|
| bare interpreter | 12 MB |
| `import django` | 12 MB |
| `django.setup()` — settings, apps, 58 plugins | 181 MB |
| URLconf resolved (`post_worker_init`'s warm) | 343 MB |
| reverse dict populated | 348 MB |

`docker-compose.yml`'s sizing note for the service says **"~140MB/worker idle"**. It is 347 — wrong
by 2.5x, and the URLconf warm is half of it: resolving 31 root patterns imports every view in the
project and everything they import.

Worth ruling out, because it is the obvious suspect: the heavy geospatial stack is **not** loaded at
startup. After `django.setup()` only `numpy` and `PIL` are in `sys.modules`; `pyarrow` (+27 MB),
`pandas` (+47 MB), `geopandas` (+18 MB) and GDAL (+43 MB) are all imported lazily. That is already
right and is not where the memory goes.

**The growth is the actual problem, and it tracks concurrency rather than volume.** On the same
worker pool:

| | total worker RSS |
|---|---|
| fresh | 1,103 MB |
| after 5 sequential `map.search` POSTs | 1,277 MB |
| after 10 | 1,300 MB |
| after 15 | 1,300 MB — **plateaus** |
| after **24 concurrent** `map.search` POSTs | **1,910 MB** |

Sequential requests plateau: the allocator keeps one request's peak and reuses it. Concurrency does
not, because *n* requests in flight need *n* copies at once. 24 concurrent added **610 MB** and it
stayed there after every request finished — the allocator does not return it to the OS, so the pool's
footprint is a high-water mark of concurrency, permanently.

That is why the earlier figure in this entry was wrong. It recorded 790 MB "idle", measured on
workers that had already served a load run; they were not idle, they were holding a high-water mark.
The correction matters because it changes the fix: this is not a fat baseline to trim, it is a
per-request cost multiplied by how many can be in flight.

**The arithmetic that does not close.** `--worker-connections 20` across 3 workers permits 60
concurrent requests. A `map.search` on a 20,000-pin account is 11.3 MB on the wire (X15) and more in
flight — the payload dicts, the JSON string and the rendered HTML all exist at once. 3 x 347 MB of
base is comfortable inside `mem_limit: 2g`; 60 concurrent large responses on top of it is not, and
the failure is not graceful: under `-k gevent` a container OOM kills the whole worker process, so
every greenlet it was serving dies with it — not the one that allocated too much.

**Order of operations.** The response size is the problem and the memory limit is the symptom.
D12's data contract exists to stop `map.search` building an 11.3 MB HTML document at all; with a
bounded response the base plus modest headroom fits 2 GiB comfortably. Raising the limit first would
buy room for a payload that should not exist. If it turns out more memory is genuinely wanted after
that — the host has plenty spare — the number to ask for is roughly `3 x 400 MB` of base plus
`worker_connections x workers x peak response size`, which is a formula rather than a guess only
once the payload is bounded.

Also worth fixing regardless of any of that: the "~140MB/worker idle" comment, which is the number
the current limit was reasoned from.

Found while trying to run the neighbour suite on the real process model.

## P112 — `bun run codeql:gate` fails with 26 untriaged findings, so nobody runs it

`id: P112` · `status: open` · `updated: 2026-09-10`

`bun run codeql:gate` exits 1. Not from any recent change: **0 of the 26 findings are in the 45 files
touched by the availability programme** — every one predates it. Which is the problem. A gate that has
been red long enough that its redness carries no information is a gate that gets skipped, and
`CLAUDE.md` lists it among the manual checks to run before a PR.

By rule:

| n | rule |
|---|---|
| 6 | `py/bad-tag-filter` |
| 4 | `js/insecure-randomness` |
| 3 | `js/xss-through-dom` |
| 3 | `py/path-injection` |
| 2 | `py/clear-text-logging-sensitive-data` |
| 1 each | `js/xss`, `js/incomplete-multi-character-sanitization`, `js/client-side-unvalidated-url-redirection`, and 5 others |

The ones worth looking at first, because of what they touch rather than what CodeQL scores them:

```
py/path-injection                    controllers/media.py:249, :377, :385
js/xss + client-side-url-redirection templates/.../messages/index.html:765
py/clear-text-logging-sensitive-data services/undo/handlers/pin_mutation.py:37
js/xss-through-dom                   templates/.../saved_filter_detail.html:95
                                     templates/.../themes/base.html:1382
                                     ts/entries/floorplan-editor.ts:4106
```

`media.py` is the one that parses user-supplied bytes and serves them back (`docs/MEDIA_PIPELINE.md`),
so three path-injection findings there are worth reading properly even if they turn out to be
false positives — and one `index.html` line carrying both an XSS and an open-redirect finding is
worth reading before assuming either is.

**Untriaged is the honest status.** None of these has been confirmed or dismissed; CodeQL has false
positives and the counts above are what the tool says, not what is true. The work is per-finding:
read it, decide, and where it is real write the exploit test before the fix — this repo's convention
is that a vulnerability gets a failing test reproducing the attack first, always.

Recorded rather than fixed because 26 findings across five rule families is its own piece of work,
and doing it badly — dismissing in bulk to make a gate green — is worse than leaving it red.

## P113 — 54 verified places where one account's ordinary use can degrade the site for everyone else

`id: P113` · `status: open` · `updated: 2026-09-11`

A sixteen-dimension sweep of the application, re-judged by hostile reviewers who were given the
claim but not the finder's evidence, returned **54 real findings**: 10 critical, 41 high, 3 medium.
Six need no account at all. The full table, the method, and the four findings that were downgraded
on review are in [`notes/availability-audit-2026-09-11.md`](notes/availability-audit-2026-09-11.md)
(N21).

The count is the least interesting part. They fall into five families, and the fixes are per family:

| family | findings | already designed as |
|---|---|---|
| Unauthenticated expensive endpoints with no throttle | 6 | — nothing; there is no inbound throttle in the web tier at all |
| One account's work fills the only Celery queue | ~8 | PL7 phase 6 (queue classes) |
| Unbounded writes into the shared 512MB Valkey | ~7 | PL7 phase 4 (Valkey split) |
| Request-path loops over one account's data, no ceiling | ~25 | per-endpoint caps; the map's own shape (R27, D12) |
| No per-role DB limits and no `statement_timeout` | ~8 | D11 phase 3 |

Three of those five already have a written design that has not been built. That is the finding worth
acting on: this is not 54 unrelated bugs, it is mostly three unbuilt phases, measured.

**The one with no existing design** is the first. `external_api/throttling.py` is rich and
DRF-only; `services/core/rate_limiter.py` caps *outbound* third-party spend, not inbound load. So
`POST /signup/` and `POST /demo/start/` spend PBKDF2 CPU per request — measured at ~1.1s each, and
`/demo/start/` does five — with nothing in front of them, and nginx carries no `limit_req` zone
either. An anonymous caller can occupy the web tier with a loop.

**Family 1 is fixed** (2026-09-11). `services/security/throttle.py` adds the inbound limit the web
tier did not have, and `signup`, `password_reset`, `resend_verification` and `demo.start` carry it
at ten calls per five minutes per address — far above what those actions take and far below what a
loop costs. It **fails open**: a throttle that refuses when it cannot read its counter turns a
Valkey outage into a site-wide lockout, and P105 already records that a Valkey outage 500s every
request. Losing an abuse control for the length of that outage is the cheaper failure, and
`test_request_throttle.py` asserts it rather than assuming it.

Two corrections to the audit came out of building it, both of which would have been wasted work:

- **`/accounts/login/` needs nothing.** `CustomLoginView` already carries per-identifier *and*
  per-IP failure lockouts, configured from `SiteSettings`. A second limiter in front of it would be
  two gates disagreeing about one rule.
- **`/demo/start/` is registered only when `demo_mode` is on** (`UrbanLens/urls.py:131`), so on a
  normal deployment the route does not exist. Throttled anyway, because a demo instance is still a
  deployment.

**H44: the gate resolved the viewer's whole social graph before asking whether it mattered**
(2026-09-12). Authorizing one media file cost **15 queries**, measured by capturing the SQL: four
friendship lookups (both directions, viewer *and* uploader), three `user_pins` scans, two
trip-membership lookups, a site-wide aggregate over `dashboard_places`, and the final check. A
gallery tile is its own HTTP request, so a 30-photo gallery was ~450 queries, each holding a
connection.

`prime_viewer_scope` does not help here and cannot: it memoises on the `Profile` instance, which
lives for one request, and a media request authorizes exactly one image. There is nothing to share
within the call. The audit's own suggested fix concedes as much — worth reading a suggested fix for
what it admits.

Done in two steps, because this is a visibility gate and the failure mode of getting it wrong is
someone seeing a photo they should not:

1. **The answer was pinned first** (`test_photo_visibility_matrix_agreement.py`). Eight scenarios,
   each asserting the exact set of (uploader setting, viewer filter) pairs that grant the photo,
   every expectation measured rather than predicted. The structure that dominates it was not what
   reading suggested: the container gate is independent of both settings and comes first, so with no
   reach the entire 7x7 table is False.
2. **Then the internals moved.** `visible_to` loaded the viewer's friends, pinned locations and trip
   memberships up front, then evaluated the settings that decide which of the three the answer
   depends on — `ANYONE` needs none, `FRIENDS` needs one, `ANYTHING_IN_COMMON` stops at the first
   match. Now a `_ViewerScope` resolves each on first use, and the reach clause is skipped entirely
   when no uploader passed the settings, since it could only ever match nothing.

Measured after: **15 → 11** at the default settings, **→ 7** for anyone/anyone, **→ 3** when the
uploader's setting refuses outright. The agreement test is unchanged throughout, which is the claim
that matters.

**Still open, with its measurement, so the next person does not re-derive it.** The remaining cost is
the reach computation. `visible_wiki_location_ids` builds the viewer's whole reachable set in 4
queries; `location_visible_to` answers the same question for one location in **1**. Using the cheap
one on the media path would take 11 to about 8.

**Correcting the estimate above**, now that the per-query breakdown is visible: swapping the reach
form saves 2 queries, not 3, and it is no longer where the cost is. At the default settings the 11
divide into roughly seven relationship lookups and, only when the uploader is *allowed*, four for
reach. Making the relationship half cheaper means replacing "load both full friend sets and
intersect" with targeted `EXISTS` queries — which would make the **gallery** path worse, since that
one deliberately resolves the viewer's sets once and reuses them across N uploaders. The two call
shapes want opposite things, and picking between them is a trade-off to decide rather than an
optimisation to apply. Left for an owner call, with the numbers above.

The prerequisite for the reach swap — an agreement test between the two forms, since they are
separate implementations of one rule — is now written (`test_wiki_reach_implementations_agree.py`),
and it immediately earned its keep by failing: **the equivalence has a precondition.** The set form filters `wiki__isnull=False`,
so it lists wikis that *exist*; `location_visible_to` answers "may you reach a wiki here" whether or
not one was created. For a location with no wiki they disagree by construction, which is not a defect
— there is nothing to show — but it does mean the substitution is sound only where a wiki exists.
That happens to be exactly the media path's case, since a photo is attached to one. The boundary is
asserted rather than left as folklore, so whoever does the swap knows what it rests on.

**Found by sweeping for the defect class rather than the finding** (2026-09-12). With the N21 list
closed, the remaining scope is the standing one — *find other places where one user's action can
degrade the site*. Two sweeps, both starting from a defect this effort had already fixed once:

- **Outbound network calls with no deadline** (the `EMAIL_TIMEOUT` class). Clean: all nine `requests`
  call sites pass a timeout, and the one third-party library that calls `urlopen` with none —
  Overture's STAC index — is already wrapped in `_STAC_LOOKUP_TIMEOUT_SECONDS` plus a per-worker
  circuit breaker, wired and tested rather than merely declared. Recorded because a negative result
  is worth as much here as a positive one: nobody needs to re-run this sweep.
- **A ceiling on one door of an action but not the other** (the H39/H33 class). **One hit.**
  `LabelReorderSerializer` caps `uuids` at a literal 1000 and its docstring says it mirrors
  `OrganizePrioritySaveView`'s semantics — but that view had no ceiling at all, and it is the door
  the application's own UI posts to. Two things scaled with the submitted list, neither bounded by
  what the caller owns: `filter(id__in=item_ids)` carried every id into one statement for Postgres to
  parse and plan on a held connection, and `skipped_global_ids` returned every unresolved id, making
  the response as large as the request.

  Fixed with one shared `LABEL_REORDER_MAX_IDS` that both doors read, not a second literal. Two
  ceilings written independently for one action is how they drift, and this pair had already drifted.
  The test asserts the two agree rather than trusting whoever edits one to remember the other.

The sweep is the reusable part. "Where else does this exact defect live" found a real one in an area
the sixteen-dimension audit had swept and missed, which is an argument for running the class outward
from each fix rather than only working down a finding list.

**A cache whose key never repeats is not a cache** (2026-09-12, H01's last half). The Immich
"Scan your library" sweep named three things and two were already closed: it declares `Queue.BULK`
since D13, so it no longer holds an interactive worker slot, and `ImmichLibraryScanStartView` claims
a `single_flight` lock *before* the enqueue, so a double-click cannot start two sweeps.

The third was real. `_match_hits_to_pins` prefilters candidate pins with an indexed `near_point`
query and caches the result — and its docstring correctly explains that this exists to avoid the
O(hits x pins) class of bug that once blew past nginx's upstream timeout in production. But the
cache was keyed on the hit's **exact float coordinates**. Two photos taken standing in the same spot
differ in the sixth decimal place, so the key was effectively unique per photo and the cache never
hit. Measured: 200 hits cost 202 queries.

This is the interesting part — the fix was already written, correctly reasoned, and documented; only
its key was wrong, and nothing about reading the code says so. A cache's hit rate is not visible in
its source. It needs a test that counts.

Now keyed to a 0.01-degree grid cell, with the search radius widened by the cell's half-diagonal
(0.79km at the equator, where a degree of longitude is longest) so one cell's shared query still
returns every pin within the real radius of any point in that cell. Safe because the prefilter only
has to return a **superset**: the exact `polygon.contains(point)` check afterwards runs on the hit's
real coordinates. The candidate list is re-sorted per hit by true distance, so the original tie-break
— where two boundaries overlap and both contain a hit, the nearer pin wins — is unchanged.

Measured after: 200 hits → 4 queries, 2,000 hits → 4 queries. Flat rather than merely smaller, which
is the property worth having: the cost no longer tracks the size of anyone's photo library.

A review pass caught the cost this moved rather than removed. Re-sorting a cell's candidates per hit
converts each pin's `Decimal` coordinates to float again every time: 2,000 hits against 400 nearby
pins cost **2.92s of CPU**. Converting once per cell instead, and skipping the sort when there is
nothing to reorder, brings that to **1.96s** for the same 45 queries and the same answers.

Left there deliberately. Removing the sort entirely would mean tie-breaking on distance from the cell
centre rather than from the hit, which is a semantic change — small, since a cell is 11m — and this
work claims the tie-break is exact. 2s of bulk-worker CPU for 2,000 distinct places is a fair price
for that claim, and the DB load, which is the genuinely shared resource, is down from 2,002 queries
to 45 either way.

**And the same sweep held one object per photo, for a fix that also already existed** (2026-09-12).
`sweep_immich_library_locations` appended a `LocationHit` per geotagged asset and kept them all until
the sweep ended, so peak memory tracked the size of someone's library rather than the number of
places in it. The worker it runs on has no `mem_limit`, so that is host memory, and a host OOM is
everyone's outage.

`LocationHit.weight` exists for exactly this, and its docstring records the same bug being fixed on
the *other* ingest path: the local-scan upload used to expand a cluster's `count` into that many
identical synthetic hits, and "a scan with a few hundred clusters averaging hundreds of photos each
could balloon into hundreds of thousands of hits". `controllers.tools._parse_cluster` now emits one
weighted hit per cluster. The Immich sweep never got the same treatment.

So this is not a new ceiling, it is the existing mechanism applied to the path that was missed.
Assets sharing a place are folded into one weighted hit as they arrive, keeping up to
`MAX_SUGGESTION_PHOTOS` (3) sample asset ids so the review queue still has thumbnails. Nothing is
dropped that anything downstream reads: `weight` carries the count and `extra_dates` the date spread,
which is what `_dates_from_hits` and every `hit_count` derivation actually consume. Bucketed at four
decimal places (~11m), chosen to sit well inside `CLUSTER_RADIUS_M` (50m) — collapsing points that
clustering would merge anyway cannot change which cluster they land in.

Two of these three H01 findings were a fix that existed elsewhere in the same file and had not been
carried across. That is worth more than either fix: when a defect class has already been solved once
here, the question is not how to solve it but **where else it still lives**.


**The twelve WebSocket `TimeoutError`s were a broken fixture, not the environment**
(2026-09-12). They survived several CI runs being called environment-sensitive, and at one point
were explicitly cleared of any connection to the broadcast batching. Both of those were wrong.

`core/tests/celery_inline.py` patched `services.core.celery.safely_enqueue_task` - where the
function is defined. Every caller binds the name into its own module at import
(`from ... import safely_enqueue_task`; twelve modules do), so the patch replaced an attribute none
of them read. The real enqueue ran, the task went to a broker with no worker draining it, and was
dropped. The test then waited for a broadcast that was never coming, and `WebsocketCommunicator`
raised `TimeoutError`.

Three things made this read as an environment problem for longer than it should have:

- **The symptom is a hung socket**, which looks exactly like host contention — and this host is
  genuinely contended, so the wrong explanation was always available and always plausible.
- **It only fails where a test *awaits* the broadcast.** In `test_spotguessr_socket_scopes.py` the
  two tests that wait failed and the three that do not passed, in the same file, on the same
  fixture. A fixture that breaks a strict subset of one file does not look like a fixture.
- **It is a test-only defect in an effort about production availability**, so the reflex was to
  clear production and stop, rather than to ask what else could hang a socket.

The helper now resolves the holders from `sys.modules` and patches each, rather than naming them —
a list would rot on the next module to import it. `test_celery_inline_harness.py` asserts the mock
reaches every holder, that there is more than one holder to reach (with one, the bug cannot exist),
that unselected tasks are still dropped, and that the original is restored.

No production code was wrong here. The cost was the signal: twelve failures normalised into
"expected", which is what a persistently failing set does to a suite.

A review pass then found the same shape once more in the fix itself. The holders are resolved from
`sys.modules` when the block opens, so a module imported for the first time *inside* it binds
whatever `celery.safely_enqueue_task` held at that moment — the mock — and `mock.patch` restores only
the modules it was handed. Three of the twelve holders are not imported at Django startup, so this is
reachable, and the symptom is identical to the original bug: not an error, just every later enqueue
through that module quietly doing nothing. The helper now restores any module still holding the mock
when the block exits.

Worth noting that the original code had this defect too, so it is not a regression — but it is the
third time in this effort that the interesting failure has been *silence* rather than an exception.
A test that asserts work happened is worth more here than one that asserts nothing raised.

**A rate limit bounds frequency, not duration** (2026-09-12). The throttle closed H17 and H50, and
H51's first half, but H51 named two things and the second survived it: every one of those endpoints
sends mail synchronously, and `EMAIL_TIMEOUT` was never set, so Django passed **no** `timeout` to
`smtplib` at all and the socket inherited the process-wide default — no deadline. Ten permitted calls
per address against a mail server that hangs rather than refuses is ten workers held for as long as
it stays silent, and the identity is an IP, so the ten is per attacker rather than in total. This
needs no attacker: a filtered port, a provider throttling a sender, or DNS pointing at something that
swallows packets all produce the same hang. `EMAIL_TIMEOUT` is now `app.email_timeout`, default 10s.

Two things that fix does *not* do, recorded so neither is mistaken for done:

- **It is a per-operation deadline, not a budget for the send.** The socket timeout restarts on each
  blocking call, so a server that trickles one byte every nine seconds holds the connection
  indefinitely. Exploiting that means controlling the mail server this site sends *to*, which is our
  own configured host — so the realistic failure, an ordinary hang, is bounded, and the residual
  needs the mail provider compromised first. The same shape as the per-call-timeout finding in the
  picker work: N calls at a per-call timeout is still N × timeout with no wall clock over it.
- **Mail is still on the request path.** The deadline caps the damage; it does not move the work. The
  real fix is sending it from a Celery task, which is now possible since D13 gave the queues classes
  — the interactive class is the right pool. Not done here because signup's failure branch writes a
  `debug_verify_url` into the session, so moving the send changes what the user sees when mail fails,
  and that is a product call.

**The other four families are not fixed.** Each needs its own decision about where the ceiling goes,
and several change what a user sees when they hit it — which is a product call, not a technical one.
Recorded so the choices are made deliberately rather than discovered during an outage.

**Family 2 is fixed** (2026-09-11), as D13. All 96 tasks now declare a class, `celery-worker` drains
`-Q interactive` and nothing else, and a new `celery-worker-bulk` drains `bulk,maintenance,celery` at
`--concurrency=2` with a low `cpu_shares`. `dashboard.E010` fails startup for a task that names no
queue, which is what keeps the classification from decaying — the failure mode being silent, not
loud: a task with no `queue=` goes on working, on the wrong pool. Building it turned up two panel
sources that had opted out of the thread pool by naming the *default* queue; that now means the slow
pool, so they name `Queue.INTERACTIVE` instead, which is the pool they were always asking for. See
D13 for the table and for what the split deliberately does not do.

**Family 4 is being worked through per endpoint.** Six done:

| endpoint | was | now |
|---|---|---|
| undo panel | read every `payload` blob it never displays | `defer("payload")` at the three panel call sites |
| photo map layers (pin, wiki, album) | one serialized row per photo, unbounded | `MAX_MAP_PHOTOS = 500`, chosen for spatial coverage rather than the first N, with a note that appears only when capped |
| memories feed | "All time" built an event per route, trip, visit and geotagged photo ever recorded | `MAX_FEED_EVENTS = 500`, every source ordered newest-first and islice'd, paginated on a timestamp cursor |
| `map.search` | every matching pin's full payload, 11.45MB for 10,000 pins, per filter change | identifiers only when the client can prove its store is current, and `MAP_DOCUMENT_MAX_PINS` over both answers |
| `map.init` | the same unbounded document | bounded by the same ceiling, via `map_data_context` |
| pin-list and saved-filter maps | capped at 500 by a slice of the list's own order | the same spatial sampler, and a note saying how many of how many |

Each carries its own regression test, and the ceiling tests assert that the constant under test is
the one the serving code reads — the failure mode found in `test_map_document_cap.py`'s first draft,
where an `override_settings` invented a name nothing looked at.

`pin_lists` and the saved-filter preview are done too. Both capped their maps with a first-N slice,
which is bounded but keeps whichever 500 pins sort first — and a pin list's order is whatever the
owner dragged to the top, so a 5,000-pin list whose first 500 entries are one city drew a map of
that city and offered it as a map of the list. Both now use the same sampler the photo maps do.

That sampler gained a ceiling of its own in the process (`MAX_SAMPLE_POINTS = 50,000`). Choosing for
coverage means reading every candidate's coordinates, which is three columns and no joins — cheap
per row, and still O(account) if nothing stops it, which is precisely the shape this family is
about. The read that carries the marker payload stays restricted to the chosen rows either way, and
`test_pin_list_map_cap.py` asserts the two by their columns rather than by the presence of a
`LIMIT`: the coordinate read carries one now, so a test keyed on that would pass against either
query and mean nothing.

**Six more of family 4 are fixed** (2026-09-11). All six were verified against the code first, since
several audit findings have turned out to be overstated:

| finding | the shape | now |
|---|---|---|
| H25 | one alias added to a community wiki wrote a `PinAlias` into every other profile's pin at that location, in the committing request, each write firing two further receivers | the fan-out is a `Queue.BULK` task; the request is flat in how many people pinned the place |
| H18 | the comment-count badge fetched and re-rendered the full text of every `@loc` comment on every pin and wiki load | `COMMENT_COUNT_SCAN_LIMIT`, and `is_visible_to` rather than `render_comment_text` - the badge needs the decision, not the HTML |
| H11 | `label_groups` expanded each client-supplied id with a query *per label visited* and no ceiling on how many ids | one recursive query, and `SEARCH_MAX_LABEL_GROUPS` / `_FILTER_IDS` / `_EXPANSION` |
| H30 | viewing a profile ran a query per place the two accounts share, and ran the whole computation before checking whether the viewer may see it | one pass for the representatives, and the permission asked first |
| H09 | the Immich picker downloaded the whole geolocated library per *pin*, because the cache key carried the point | the download is keyed on the account, bounded by `IMMICH_MARKER_CACHE_MAX_ASSETS`; only the measuring stays per point |
| H27 | every `Pin.save()` evaluated the pin against every smart list its owner has, twice per matching list | `MAX_SMART_LISTS_PER_SYNC`, past which the whole sync moves to the bulk queue, and one evaluation instead of two |

Two of those ceilings are deliberately not refusals. An over-long label filter is trimmed and a
thread over the comment ceiling reports a floor with a `+`, because a saved filter written before the
ceiling existed must still return results, and a badge that reveals less than the thread reveals
nothing - a badge that reveals *more* is the existence oracle `services/comments` is built to deny.
H27's ceiling drops nothing at all: past it the same work runs, on the queue.

**Three more** (2026-09-11), and two findings that were already fixed:

| finding | the shape | now |
|---|---|---|
| H42/H49 | the global-search panel fires per keystroke, hands an unbounded `q` to ~11 providers each running an un-indexable trigram scan, and had no throttle - while the *API* surface of the same engine caps the query at 250 characters for exactly that reason | the same cap, truncating rather than refusing, and a per-account throttle counting GET |
| H26 | `LabelBulkEditView` capped neither the id list nor the parent/child lists, then ran a graph-walking cycle check per (label, parent) pair and an account-wide pin touch per label saved | `LABEL_BULK_EDIT_MAX_IDS`, refused rather than trimmed, checked before the first save |
| H41 | one group message enqueued one Celery task per member, each building its own event loop and channel-layer connection to push a single frame | one task carrying the batch, delivered in one loop |

H26 refuses where the filter ceilings trim, and the difference is worth keeping straight: a bulk
*edit* that silently applied to some of what somebody selected is worse than one that says no,
while a *filter* that returns results for part of itself is better than one that errors.

**H20 and (mostly) H41 were already fixed** when checked. The export view claims a single-flight
lock before it creates anything and releases it if the enqueue fails - which is the debounce the
approved decision asked for, already in place. H41's group-chat half already built its payloads
once per broadcast rather than once per member; what was left was the delivery, which is the part
fixed above. Both went on the list because the audit was read rather than the code.

**H39 is fixed** (2026-09-12), and it had a third door the finding did not name. Markup `geometry`
is written from the request body into a JSONField after a type check and nothing else, so one shape
could carry any number of coordinate pairs; and the reader answered for a whole pin/wiki subtree at
once. On a community wiki both are read by everyone who opens the page, so one person's drawing set
what every later viewer downloaded. Geometry is refused past `MARKUP_MAX_GEOMETRY_POINTS` at *both*
write doors (create and update take the same body), and the listing is cut at
`MARKUP_MAX_ITEMS_PER_RESPONSE` with a `truncated` marker — a silent cut reads as "this pin has five
drawings", which is a different claim.

The third door: `SafetyContactMarkupJsonView` builds its own listing, so capping `MarkupJsonView`
would have left the *less* guarded of the two unbounded — that route is reachable by anyone holding
the magic link, with no account at all. Both now read through one helper.

**A fourth and fifth door, and H37 with them** (2026-09-12). Re-reading H39's own recommendation
found it had named `_sanitize_latlngs` — a function the first fix never touched, because the
controller was where the finding pointed. The snapshot composer reaches the same table by a
different route entirely: `sanitize_map_data` → `replace_items_from_snapshot`, capping neither how
many shapes a snapshot carries nor how many points each one does. Six callers share it — pin and
wiki comments, visits, memories, trips and lists — and `replace_items_from_snapshot` saves row by
row, each save firing the two `PinMarkup` receivers, so N shapes is N inserts plus 2N receiver runs
inside one request. `MARKUP_MAX_SHAPES_PER_SNAPSHOT` and the existing point ceiling now bound both.

Trimmed rather than refused, which is the opposite of the geometry door's choice and deliberate:
the callers read a `None` snapshot as "no map was submitted", and `materialize_markup_map` responds
to that by **deleting the map the user already had**. Refusing an oversized snapshot would have
destroyed data. `_sanitize_markup_shapes` was already a sanitizer that silently drops malformed
entries, so returning fewer shapes extends what it already did; trimming markup is direction-safe
in a way trimming a `not` filter is not, because a shape omitted is only a shape not drawn.

The fifth door is the read side, and the one other people actually pay for. `MarkupMap.to_snapshot()`
is a separate path from `MarkupJsonView` with no ceiling of its own, and `Comment.map_snapshot`
calls it per comment — so a wiki thread embedded one unbounded snapshot per comment for everyone who
opened the page. It is sliced at `MARKUP_MAX_ITEMS_PER_RESPONSE` now. Rows written before any of
these ceilings existed are still stored, which is the second reason the read side needed its own.
This closes H37, which named the same function from the memories-maps end.

Capping the read introduced a way to lose data, which the fix had to close rather than ship: items
are created one at a time and nothing counted how many an owner held, so a map could pass the
listing ceiling item by item — and then the composer prefill or `clone_markup_map`, both of which
round-trip through `to_snapshot()`, would write back only what they could read and delete the rest.
The create door now refuses past the same ceiling for a standalone map, so no map can outgrow its
reader and the round-trip is lossless. Cloning inherits the ceiling deliberately: the alternative is
one button press copying an unbounded number of rows, and the recipient still gets everything the
reader showed.

Scoped to `parent_map` on purpose, and the first attempt got this wrong. Capping every owner kind
would have put a ceiling on a *community wiki* — a surface many people draw on — so one person
filling it would lock everybody else out, which is the exact harm this work exists to remove, just
relocated. Pin and wiki markup is never written back from a snapshot, so neither can lose anything,
and each create is one cheap insert against a read that is already capped.

`_coordinate_count` was checked against the obvious evasions and holds: padding a payload with
nested empty lists does not get pairs past the ceiling, and walking the largest body Django will
accept costs ~0.08s. `json.loads` raising `RecursionError` on a deeply nested body did escape
`_parse_body`, which caught only `JSONDecodeError`/`ValueError` — a 500 rather than a fallback.

Still open here: the `truncated` flag the listing returns is read by nothing. Three consumers do
`(data.markup_items || []).forEach(...)` and ignore the rest of the payload, so the cut is reported
in JSON and invisible to the person looking at the map. The photo layer already solved this
(`map-annotations.ts` renders a note only when capped); markup should match it.

**H29 and H48 are fixed** (2026-09-12). The Organize lists panel ran
`prefetch_related("items__pin")` and the template used exactly one thing from it -
`pin_list.pin_count`, which is `len(self.items.all())`. So every pin on every list the profile owns
was fetched and built into a model instance to produce an integer the database can count without
sending a row, and the `pin_count` sort materialised the whole queryset in Python on top of that.
Measured before the fix: 54 extra model objects for 27 extra pins - exactly two per pin, the
`PinListItem` and the `Pin`. `with_pin_counts()` annotates the count and the sort moves into SQL.

The test states the invariant as "adding pins must not change what the panel costs" rather than as
a fixed object budget, because the panel renders one card per list either way - the axis that
matters is pins, and a fixed budget would need recalibrating whenever a card grows a field.

H48: `/costs/` is anonymous, gated only by a SiteSettings flag, and every GET ran seven
cost-tracking helpers - `active_user_count` joins every user against every pin they own, and
`cost_per_user` and `cost_per_supporter` each call `effective_monthly_cost` again. Twelve queries
per view, uncached, for a page a crawler can hold open. Only the figures are cached, never the
response: `cache_page` would have kept serving a page after an admin switched the toggle off, so the
gate runs every request and the window only covers numbers that are trailing aggregates anyway.

**H40 is fixed** (2026-09-12). `ApiKeyAuthMiddleware._resolve` was decorated
`@database_sync_to_async`, whose `thread_sensitive` defaults to True - verified against the
installed asgiref rather than taken from the finding - so it ran in the single shared executor
thread that every other `database_sync_to_async` call in the daphne process uses for its database
work. Inside it, `authenticate_api_key` calls `check_password`, a deliberately expensive hash. One
client reconnecting in a loop with `?key=` occupied that thread a hash at a time and every other
socket's database work queued behind it.

The cost is not the problem; where it is paid is. The hash must stay expensive, so it moved rather
than shrank: the lookup is one indexed query by the key's public prefix and stays where the
connection handling is, and only the hash runs on a plain worker thread. `api_key_candidate` and
`touch_api_key` split the two halves out, and `authenticate_api_key` is rebuilt from them so the
HTTP path still runs the same parsing and lookup rules. `api_key_candidate`'s docstring says in as
many words that a row it returns is **not** authenticated.

Deliberately not fixed by caching the verdict: a cached credential check means a revoked key keeps
working until the entry expires, which trades an availability problem for a security one.

**A test whose stated reason was the opposite of what the code does** (2026-09-12).
`test_required_feature_is_only_set_where_the_web_gates_too` asserted the gated panel set was exactly
`{"epa_echo"}`, and its docstring said membership of `PinController._NEARBY_RESEARCH_TABS` was what
kept the web and the API agreeing about who may see a panel. The comment on that dict says the
reverse explicitly - it "decides *ordering and labels only*", and the gate is each source's own
`required_feature`, read through one shared `panel_visible_to` by both surfaces. Three panels
legitimately became gated, the hardcoded set drifted, and the docstring would have sent the next
reader to edit the wrong file. Replaced with the property: every gated panel must be refused to a
viewer without its feature, so new gated panels are covered the day they are added.

Worth recording how close that came to being reported as a leak. The property assertion failed for
all four gated panels, which reads alarmingly - but `user_has_feature` grants every feature to site
admins, and this codebase auto-promotes the *first* user. The fixture created its user without the
throwaway other tests in the repo use, so it was testing an admin. `default_features` is blank by
default, so the promotion was the whole explanation. The gate is enforced.

**H35/H22/H13 are fixed** (2026-09-12) - three audit lines about one `cache.set`. The basemap tile
proxy stored raw vendor bytes with no size check, into the same 512MB Valkey that holds sessions,
the Channels layer and the Celery broker. One oversized tile, or a vendor answering a tile request
with something that is not a tile, evicts other people's sessions to make room for itself.

`bounded_cache.set_if_small` already existed for this and the Immich thumbnail proxy already used
it, so the fix was to stop having two answers to the same question rather than to invent a third.

It also closed an unhandled failure path that has nothing to do with size. A bare `cache.set`
against a full or unreachable Valkey **raises**, and here nothing caught it - so a cache problem
became a 500 on a map tile. The helper catches it and serves the tile uncached, which is the right
answer: refusing to cache must never mean refusing to answer. There is a test for it, and it was red
before the change.

**The same default-binding trap as H33, in the helper itself.** `set_if_small` took
`max_bytes: int = MAX_CACHED_BODY_BYTES`, and a module constant used as a parameter default is fixed
when the function is *defined* - so the ceiling could be neither configured nor overridden in a
test, and the first attempt at the size test failed for that reason rather than for the defect. It
now resolves at call time. Worth noticing that this is the second place in one session where a
ceiling was written as a default argument and was therefore inert.

**H21 is verified but deliberately not started.** `resolve_deferred_pin_locations` re-serialises the
still-pending payload into the broker on every retry, and retries continue for up to two days. It is
*bounded* - `MAX_PREVIEW_PINS` caps an import at 20,000 pins - so the exposure is a few megabytes per
retry rather than unbounded, but into the shared instance. The two real fixes are persisting the
payload behind an id (a schema change) or the Valkey split already planned as PL7 phase 4; choosing
one here would pre-empt that plan, so it is recorded rather than done.

**H58 is fixed** (2026-09-12). Undoing a bulk pin delete validated the batch one row at a time:
for every pin, a separate `.exists()` for its profile, its location, its wiki, a `.count()` for its
labels, and another `.exists()` for the root-pin collision - with `_resolved_parent_pk` adding a
sixth for a parent outside the batch. A bulk delete is capped at 500 pins, so an undo could issue
thousands of queries before recreating the first row, inside the transaction holding the undo row's
lock. Measured: three extra queries per pin on a batch with no wikis or labels, which is the floor
rather than the typical case.

The pre-flight is now one query per relation for the whole batch, extracted as
`assert_restorable` so the cost has a seam that can be measured. The extraction was made first and
run unchanged, so the query-count test failed on the real defect rather than on a missing method -
worth doing deliberately, because an extraction that goes straight to the batched form proves only
that the new code is fast, not that the old code was slow.

**What this does not cover:** the create loop still calls `_resolved_parent_pk` per entry, so a
parent *outside* the batch is still resolved one at a time there. For a bulk delete of a subtree
most parents are in-batch and cost nothing, so this is a smaller residue than the pre-flight was -
but it is a residue, not an absence.

The refusals are the part that matters more than the count: they are what stands between an undo
and an `IntegrityError` on `db_pin_unique_location_per_profile`. Fifteen existing tests across
`test_undo_restore_conflicts` and `test_undo_pin_restore_conflict` already pin those semantics, and
this file adds a case per refusal plus an intact-batch case, so a batched check that quietly stopped
refusing fails here too.

**H45 is fixed** (2026-09-12). `set_profile_avatar` runs the shared `image_upload_error` gauntlet
without `skip_malware_scan`, so the antivirus scan happens inside the request - the file is copied
into a BytesIO in the worker and streamed to the shared clamd daemon. The only size bound on it was
`file_size_error_for_upload`, the *site-wide photo/video* cap: 250MB by default, and an admin may
raise it to 900MB. One person changing their profile picture could hold a worker, and the clamd
daemon every other upload shares, for as long as a quarter-gigabyte scan takes.

The asymmetry is the argument for the number. `_download_avatar_from_url` - the OAuth path, fetching
from Discord or Google - already refuses anything over 512KB. The same product concept was bounded
two orders of magnitude apart depending on which door it came through, and the *tighter* door was
the one where the size is not even the user's choice.

**Bounded rather than deferred, and that is a deliberate trade.** Moving the scan off the request
the way comment images do would mean storing, and potentially serving, a picture nothing has scanned
yet. Comment images only get away with that because `pending_scan` hides them from everyone but
their author until it clears; a profile picture has no equivalent gate. So the fix makes the scan
cheap rather than late - the cost is proportional to the bytes, and an avatar has no business being
250MB. Adding a `pending_scan` equivalent would be the alternative, and it is a schema and render
change rather than a ceiling.

The test that carries the weight asserts the scan is never *called* for an oversized upload. Before
the fix that test failed with `AvatarMalwareDetectedError` - the scan had already run on the
oversized file, which is the defect stated exactly.

**H36 is fixed, on both pickers** (2026-09-12). The finding named Flickr; the Immich picker has the
identical shape and would have been left holding a worker after the other was capped.

Both "visits" modes issue one search per visit date, because both providers' taken-date filters take
a range rather than a set of days. `MAX_VISIT_DATES` is 15 and each gateway's own
`_REQUEST_TIMEOUT` is 30 seconds - so the inner timeouts bound each call and nothing bounded their
sum. **Every per-call timeout could be honoured and the request still run for 450 seconds.** That is
the shape worth remembering: a per-call timeout is not a request budget, and a fan-out of N calls
needs its own.

`call_with_deadline` already existed for this and `controllers/pin.py` already used it for its own
provider fan-out. Both pickers now wrap all three modes - including Immich's "nearby", which
measures against a whole library download - and degrade to the same error card the gateway-failure
path already showed.

What this does *not* do is stop the abandoned call: Python cannot kill a blocked thread, which is
why the helper keeps a small dedicated pool. It stops the *request* waiting on it, which is what the
worker is for. The tests assert the view returns promptly under a hanging gateway (the red run took
37 seconds; the green one takes 8), and pair it with a prompt-search case so a view that simply
always errored could not pass.

**The read half of H24/H38 is fixed; the cache half was already done** (2026-09-12).
`bounded_cache.set_if_small` already refuses to store an oversized body, with a comment noting the
server is the user's own and `size=thumbnail` is a request rather than a guarantee - the ninth audit
entry to turn out already fixed. What that never touched is the read: `_get_binary` called
`response.content`, buffering the whole body in the gunicorn worker before any size was known. So
the cache was safe and the worker was not, and the server on the other end is configured by the
account holder - which makes the response size something one user picks and everyone sharing that
worker pays for.

Streamed now, and refused as soon as the ceiling is passed, because a refusal *after* buffering
saves nothing. The test that matters asserts a 2,000-chunk response is refused within 20 chunks;
a fix that checked `len(content)` at the end would pass a naive size assertion and fail that one.

Two ceilings, because one helper serves two doors, and neither is an invented number.
`IMMICH_MAX_THUMBNAIL_BYTES` bounds the thumbnail. The original is bounded by
`max_upload_file_size_bytes()` - the site's own upload limit, already clamped to the ingress cap -
on the reasoning that a file the site would refuse from a browser is not one it should accept from
someone's Immich. `max_bytes` is a required keyword so a third caller has to state its own ceiling
rather than inherit one chosen for a different door.

**The trip half of H43/H52 is fixed, and it led to a defect in a shared helper** (2026-09-12).
`visible_comment_tree` - the pin and wiki path - builds a `can_view` dict keyed by author precisely
because, in its own words, "each call runs up to 3 extra query pairs, which is redundant when one
author appears several times in a thread". `trips.trip_comments.build_comment_tree` copied the gate
and not the dict; its comment even says the check works "exactly as pin/wiki comments already do",
which is true of the gate and false of the memo beside it. Measured before the fix: five extra
queries per comment. The memo took it to one.

That remaining one was the interesting part. `_aggregate_reactions` called
`.select_related("profile")` on whatever queryset it was handed - and on a prefetched relation that
is not a refinement but a **clone**, and a clone does not carry the result cache a prefetch
populated. So every one of its six call sites, across the pin, wiki and trip comment surfaces, was
paying a query per comment despite carefully prefetching `reactions`. `.all()` has the same effect
for the same reason, which is why the first attempt at the fix changed nothing.

The join was never needed: the aggregate reads `emoji` and `profile_id`, both columns on the
reaction row. It now iterates the queryset exactly as given. Worth remembering as a shape: a
prefetch is only free if nothing downstream refines the queryset, and `.all()` counts as refining.

**H47's memo half was already fixed** - `visible_comment_tree` has had the dict all along. That is
the eighth audit entry to turn out already done. Only its pagination half is open, and that needs a
decision rather than a patch: `visible_comment_tree` filters in Python, so paginating the queryset
would hand back short pages and change the API contract.

**H33 is fixed** (2026-09-12) - the external twin of H26, which was capped at the internal door in
an earlier batch and left this one open. `LabelBulkEditSerializer` gave `uuids` a literal
`max_length=500` and gave `add_parent_uuids`/`add_child_uuids` no ceiling at all, so the real cost -
for every label, for every proposed parent, a `would_create_cycle` walk of the label graph in the
database - was one capped number multiplied by an uncapped one, inside a single `transaction.atomic()`.

All three lists now read `LABEL_BULK_EDIT_MAX_IDS` at validation time rather than binding a field
`max_length` at import, which could be neither configured nor overridden in a test.

**The first version of the test was worthless and passed 6/6 against unfixed code.** It built the
parent list from `baker.prepare` uuids, which do not exist - and the view already refuses a uuid it
cannot resolve, so the 400 it asserted came from that pre-existing check rather than from any
ceiling. The rewrite uses real, resolvable labels, and adds a test asserting the unresolvable-uuid
400 *separately*, so the two refusal paths cannot be confused for one another again. Worth recording
as a pattern: when a ceiling test asserts a status code, check what else can produce that code.

**H46 is fixed, and it had a second door** (2026-09-12). Both revision-history endpoints - the pin
article's and the wiki article's - ran `list(article.revisions...)` and built a row dict per revision
*before* handing the result to `paginated_response`, so the page size bounded the response body and
nothing else. Each row carries `size_delta`, which is `len(content)`, so every revision's complete
source (200,000 characters is the field's own ceiling) was read out of the database to produce one
integer per row. Measured: 48 extra model objects for 16 extra revisions behind a two-row page, on
each door.

`paginated_response`'s own docstring already described the anti-pattern and offered `row_builder` as
the fix - "Passing a queryset plus this is strictly better than pre-building a list of dicts and
paginating that" - so the tool was there and neither caller used it.

`size_delta` is why this could not be a plain slice: a row's delta is measured against the revision
before it, which on the last row of a page sits on the *next* page. A window function
(`Lag(Length("content"))`) computes it against the true neighbour before `LIMIT` applies, so paging
never changes a number - there is a test that pages to the second page and checks the deltas rather
than only counting rows. `content` itself is deferred, so the bodies are never loaded at all.

Two smaller things fell out: `select_related("restored_from")` was a join for a value read as
`restored_from_id`, already on the row; and `masked_editor_name` calls `resolve_visible_identities`,
a helper written to take a batch, once per revision with a single-item list - memoised per editor
like H28.

**H28's second half is fixed, and H07 and H28's first half were already done** (2026-09-12).
`bound_map_layer` already caps both photo-map JSON endpoints with a `truncated` marker, which is
what those entries mostly asked for - the sixth and seventh audit lines found already fixed. What
was left is the part the cap does not touch: every surviving photo still called
`_visible_uploader_name` -> `Profile.can_view_profile`, and at the `COMMON_PIN` setting that reaches
`_have_common_pin`, which reads *both* accounts' entire pin sets into Python. One viewer opening a
community wiki paid that twice per photo, and a wiki collects photos from everyone who has been
there.

Memoised per uploader for the life of one response. Scoped to the response deliberately rather than
cached across requests: a visibility setting a user has just changed must take effect on their next
page load, not when a TTL expires - this is a privacy gate, and a stale yes is a leak.

Writing the test taught the domain something worth recording: **photo visibility and identity
visibility are two independent gates.** `Image.objects.visible_to(profile)` decides whether the row
appears at all; `can_view_profile` only decides whether it carries a name. The first draft of the
test conflated them - it removed the uploader's shared pin to make the identity invisible, which
dropped the rows entirely and made the assertion fail for a reason that had nothing to do with
masking. The tests now hold photo visibility fixed and vary only identity.

**H31 and H37 are fixed** (2026-09-12) - the same page from two angles. The Maps subpage built a
card per `MarkupMap` the profile owns with no pagination and no ceiling, and each card calls
`to_snapshot()`, so the response carried every annotation of every map. The page's own advertised
workflow - draw a route on a check-in, a comment, a visit - is what makes an account have many of
them. The per-map half was already bounded by the `to_snapshot()` ceiling above; what was left was
the number of maps.

Paginated rather than capped: a map the user drew and can no longer reach is worse than a second
page, and unlike a map layer a list of cards has an obvious place to put a Next button. The slice
happens before any card is built, so an off-page map costs nothing rather than being built and
discarded. The reusable `_pagination_controls.html` has a `page_links` mode written for exactly this
case - a full-page view, where an `hx-get` would swap a whole HTML document into one div, and where
plain links keep the back button working.

**A stale performance record, not a regression** (2026-09-12). `test_pin_list_query_fingerprint` was
failing on the branch before any of this work - confirmed by reverting to HEAD and re-running. Two
committed improvements changed the external-API pin list's SQL and nobody re-recorded the fixture:
`0fab2b35a` moved `child_count` from `Count(distinct=True)` (which makes the planner sort the whole
join product) to a scalar subquery, and `07e608367` inlined the media-relevance exclusion as
`~Exists` instead of a separate SELECT. The re-recorded fixture is two queries shorter with no
GROUP BY and nothing else changed. Worth stating because the first reading of it looked like
`child_count` had been silently dropped from the API - it had not; it moved.

**H32 and H53 were already fixed** when checked — the third and fourth audit entries to turn out
that way, after H20 and H41. Album detail caps its map through `bound_map_layer`, which counts
first and only projects coordinates when the count is over the limit, keeps the area covered rather
than the first N, and reports `map_truncated`. That is the same shape the approved decision asked
for on photo maps, already in place. Both entries went on the list because the audit was read
rather than the code, which is now the most common reason an entry is wrong.

Still open in family 4: the findings N21 lists beyond these, most of which are request-path
loops over one account's data in surfaces nobody has measured yet.

**H19 is fixed** (2026-09-11), and it is the one that could have taken the site down rather than
slowed it. Upvoting an external photo materializes it - downloads up to 20MB and stores it on the
shared media volume, which is the filesystem Postgres lives on - with `QuotaExemption.EXTERNAL_MEDIA`
set, so the storage quota does not apply. The exemption is right and deliberate: the person who
upvoted someone else's photo did not author it and should not be charged for caching it. It was also
the *only* bound, as `media_materialize`'s own docstring said in as many words ("Only the per-item
`_MAX_DOWNLOAD_BYTES` cap applies") - and per-item is not a bound on an account.

`EXTERNAL_MEDIA_DAILY_BYTES` (512MB, rolling 24 hours, per profile) is the missing half. The
exemption decides who is charged; the ceiling decides how fast. It is checked after the dedupe
lookup, so re-voting a photo already cached is free and never refused, and before the download, so a
refusal does not arrive with the bytes already spent.

`WikiMediaVoteView`'s docstring claimed the materialize "costs the *voter's* storage quota". It never
did - that is the exemption - and the claim is corrected rather than left as the first thing a reader
would check.

**H12 and H14 are fixed** (2026-09-11), both by the same move: a ceiling at the door the
client-supplied thing comes through.

- `dissolve_polygons` compares every remaining pair of components and restarts after each merge, so
  its cost is quadratic per pass with up to one pass per merge - in GEOS `intersects()` calls, inside
  the request. The component count came from a POST body and nothing bounded it.
  `MAX_REGION_POLYGONS` (200) and `MAX_REGION_VERTICES` (50,000) bound both halves, checked in
  `parse_multipolygon_geojson` rather than in the dissolve, because a ceiling checked after something
  has stored the shape is on the wrong side of the write. One consequence is worth knowing: on the
  map filter an unusable region drops that criterion rather than failing the search, so a region over
  the ceiling *widens* the result. That is the pre-existing contract for a corrupt region, now
  written down.
- The four REData media proxies cached whatever they downloaded, unbounded, into the shared Valkey,
  with no login required and a cache key built from path parameters. Both halves are bounded now: a
  4MB per-entry ceiling (larger than `bounded_cache`'s thumbnail default, named at the call site,
  because these are scanned PDFs) and a rate. `throttled` had to learn to count GETs first - it
  counted only unsafe methods, which was right for the signup POSTs it was written for and useless
  for a download proxy.

**H10 is fixed** (2026-09-11), in two places because one cannot cover the other.

`InboundVolumeMixin` bounds how fast an account may *send* on a socket, and its docstring said the
shared tier "is what stops one account opening fifty sockets" - which is true of flooding from fifty
and not of holding them. An idle socket sends nothing, so it was charged nothing, while occupying one
of nginx's `worker_connections` (1024 per worker, shared with every HTTP request) and a slot in the
single daphne behind them. Holding them is the cheap attack; sending on them was the one already
bounded.

`services/security/socket_budget.py` caps connections per *account* -
`WEBSOCKET_MAX_SOCKETS_PER_ACCOUNT`, default 20 - claimed before any group is joined, because
Channels fires `disconnect()` only for a connection that reached `accept()` and a refusal after
`group_add` would leak the membership permanently. Two properties matter more than the number:

- **It fails open.** A cap that cannot read its counter allows, exactly as the request throttle does.
  A Valkey outage already degrades the site; turning it into "nobody may open a socket" makes an
  outage worse rather than safer. This is the opposite of the single-flight guard, and the difference
  is which way the failure hurts.
- **A crashed worker must not lock an account out.** A plain counter would be incremented and never
  decremented. The claims are a sorted set scored by time, so a dead worker's leftovers age out on
  their own; the worst a crash costs is a smaller allowance until `STALE_AFTER_SECONDS`.

The application cap runs *after* authentication, so it cannot see the cheapest attack of all: a
handshake that never authenticates still occupies a connection and a daphne slot before Django closes
it. `limit_conn ws_conn 60` on the `/ws/` location is what bounds that, keyed on the real address the
vhost establishes first - and the test asserts that ordering, since keying on the front door's
address would give every visitor behind the tunnel one shared budget.

Reviewing the first version turned up the defect that would have mattered most, and it was on the
client. `live-socket.ts` stopped for good on 4404 and reconnected on everything else, so a 4429
became a retry - at a one-second backoff that `retryNow` reset on every tab focus and every `online`
event. A browser one socket over the allowance would have hammered the refusal on every window
switch, each attempt a handshake, an auth resolution and a store round trip. A limiter that provokes
the load it prevents is not a limiter. A capacity refusal now waits the full ceiling and is not
brought forward by those triggers, and clears once a place comes free; three of the five new tests
fail against the previous version.

That fix covered `live-socket.ts`, which the three game consumers use - and nothing else. The
notification, direct-message and safety-chat sockets are written by hand in their templates, and the
notification one is the socket every logged-in page opens. All three reconnected on any code but
4404, and the notification one reset its backoff on every tab focus *and* escalated to HTTP polling
after two failures, so a refused socket would have been answered with a stream of requests. Found by
grepping for `new WebSocket(` rather than by reasoning about which page opens what; a test now
discovers them the same way, so a fifth hand-rolled socket fails there rather than in production.

A second leak, and a permanent one rather than a temporary one: Channels fires `disconnect()` only
for a connection that reached `accept()`, so a failure between the claim and the accept held the
place - and the renewal task kept that claim fresh for the life of the process, so it would never
even age out. Every connect-failure path releases now.

The claims also renew. They expire so a worker that went away stops costing an account part of its
allowance, but these sockets live as long as their tab - hours - so without a renewal a long-lived
one would quietly stop counting, which is lenient rather than dangerous but would make the cap
meaningless for exactly the connections it bounds. With a renewal every 5 minutes the window can be
15 rather than the 2 hours it would otherwise need, so a dead worker's claims clear in minutes.

**H16 is overstated.** The audit says "every wiki/photo access check re-reads the whole site-wide
Place aggregate table, so each media-file fetch costs a full-table scan". The scan is real -
`_earn_aggregates` reads every aggregate `Place` and then every member of those - but the media path
does not reach it. `services/media/access.py` authorizes an ordinary photo through
`Image.objects.filter(pk=...).visible_to(profile)`; only `authorize_comment_image` calls
`location_visible_to`, and `visible_wiki_location_ids_cached` already memoises the result on the
`Profile` instance for the life of the request. What remains is that the cost grows with total site
data rather than with the requester's, which is worth fixing when the aggregate count justifies it -
and is not the per-fetch full-table scan the finding describes.

## P114 — Staging outranks production for CPU on the host they share

`id: P114` · `status: open` · `updated: 2026-09-11`

Measured on damballa, 2026-09-11, with `docker inspect` against the running containers:

| container | `NanoCpus` | `CpuShares` | `Memory` |
|---|---|---|---|
| `urbanlens_production_app` | 0 | **0** | 0 |
| `urbanlens_staging_app` | 2 | **2048** | 2g |
| `urbanlens_production_db` | 0 | **0** | 0 |
| `urbanlens_staging_db` | 2 | **2048** | 2g |
| `urbanlens_production_celery_worker` | 6 | 0 | 24g |
| `urbanlens_staging_celery_worker` | 2 | 256 | 3g |

`CpuShares: 0` means the key was absent when the container was created, and the kernel uses 1024.
So under contention on a host that runs both stacks, staging's web tier and database each carry
**twice** production's weight. Every other production container is unlimited and unweighted too;
only `celery_worker` carries any limit at all.

This is not a compose bug. Staging's numbers are exactly `docker-compose.yml`'s defaults, so staging
sets none of these variables and simply runs what the file says. Production's containers predate the
`cpu_shares` keys entirely - they were created from a compose file that did not have them. The
inversion is an artefact of deployment order, which means it will recur on any host where the two
stacks are recreated at different times.

**It cannot be fixed from this repository alone.** Production has to be recreated from the current
compose file for its `cpu_shares` to exist at all. Until then no repo-side default can put it above
staging, because an absent key is 1024 and any positive staging value at or above that wins.

### What this repository can do

`config/env/staging.sample.env` holds a full set of limits below the base defaults, so bringing
staging up with them is a copy rather than a judgement call, and
`test_staging_limits_are_below_production.py` fails if any variable `docker-compose.yml` reads is
missing from it or is not lower. That makes the *intent* enforceable here even though the fix is a
deploy.

### "Staging shouldn't be running when not needed"

Three options, and they are not exclusive:

| option | buys | costs |
|---|---|---|
| **Idle detector** - a timer that stops the stack after N hours with no nginx access-log entry | Matches the actual requirement ("when not needed") rather than a guess at it. Nothing to remember. | Needs a definition of idle that a health check or uptime monitor does not satisfy - both hit nginx. A cold start is then the first visit's latency. |
| **Scheduled stop** - stop at a fixed hour, start on demand | Trivial, no state | Wrong whenever someone works outside those hours, and they will |
| **Alert only** - page when staging has been up and idle for N hours | No surprise shutdowns | Relies on somebody acting on it, which is the thing that already did not happen |

Recommendation: the idle detector, with the alert as its fallback for when the detector itself has
not run. The definition of idle that avoids the health-check problem is an access-log line whose
`$http_user_agent` is not the monitor's and whose path is not `/health/`; nginx already logs both
fields. It belongs in the `infrastructure` repo beside the other host timers, not here.

Not recommended: relying on staging's limits alone. Lower limits bound what staging can take when it
is busy; they do nothing about it being up at all, and an idle Postgres plus Valkey plus ClamAV is
still several gigabytes of a host production also lives on.
