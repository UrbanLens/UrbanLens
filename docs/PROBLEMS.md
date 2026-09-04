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
`see docs/PROBLEMS.md` costs the reader a full-text search of a 3,000-line file,
and in practice they do not do it. There are 33 such bare references in `src/`.
Cite **every** relevant entry, not the nearest one. Ids and dates are stable
here; line numbers are not.

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
`Image.pending_scan` (migration 0038) gates `services/media/access.py::authorize_image` and
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

## P4 — `urbanlens_development_main_test_runner`'s venv is missing five dev deps, silently dropping coverage

`id: P4` · `status: open` · `updated: 2026-08-31`

Previously titled "`urbanlens_development_main_test_runner`'s baked image is missing `django-perf-rec`".

Found while running the full suite before merging PR #143 (`bin/run_tests.sh`, no `--fast`, per
repo convention for a PR merge). Collection aborts the entire run with `ModuleNotFoundError: No
module named 'django_perf_rec'` importing `test_query_records.py:31` - not a code regression: the
package is correctly declared in `pyproject.toml` (`django-perf-rec~=4.31.0`) and `uv.lock`, and
`uv run python -c "import django_perf_rec"` succeeds on this host's own `.venv`. The test-runner
container's image (`/app/.venv`) simply predates that dependency being added and hasn't been
rebuilt since - the same class of drift as [[app-container-not-live-synced]] but for the
*container's own venv*, not `/app/src`. `bin/run_tests.sh`'s tree-hash sync only covers `src/`, not
`.venv`, so it can't catch this.

Worked around for this merge by running with `--ignore=src/urbanlens/dashboard/tests/hypothesis/
test_query_records.py` rather than rebuilding the shared container mid-session (another agent was
concurrently using the same branch/host - see [[verify-attribution-before-reverting-shared-diffs]] -
so an image rebuild felt too disruptive to force unilaterally). Whoever next has a quiet window
should rebuild `urbanlens_development_main_test_runner` (`docker compose --profile test up -d
--build test-runner`) so `test_query_records.py` runs again; until then that one file's coverage is
silently absent from every `bin/run_tests.sh` run, not just this one.

**Still open 2026-09-03, and it is five packages, not one.** `bin/run_tests.sh` now checks the
container's venv against `pyproject.toml`'s dev group on every run and warns rather than letting a
test blame the branch. Pointed at this container it reports:

    diff-cover  django-perf-rec  pytest-randomly  pytest-xdist  schemathesis

`pytest-randomly` and `pytest-xdist` back this script's own `--shuffle` and `--parallel` flags, so
those two have been advertised and broken in this container for as long as they have existed -
worth knowing before trusting either. The rebuild is still the fix, and is still an operator action
on a shared container rather than something a session should force.

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

## P8 — `Friendship.unique_together` permits both `A->B` and `B->A`, so "one row per pair" is convention only

`id: P8` · `status: open` · `updated: 2026-08-20`

Previously titled "reciprocal `Friendship` rows are permitted, and "one row per pair" is only a convention".

`Friendship.Meta.unique_together` is `("from_profile", "to_profile")`, which stops a duplicate in
*one* direction and permits `A->B` **and** `B->A` to both exist. Every reader assumes they cannot:
the model docstring says "there is exactly one `Friendship` row per pair", `QuerySet.between()`
matched either direction and called `.get()`, and the mute columns are per-side *of one row*.

Two ways a reciprocal pair gets created today:

- `services/import_export/import_data.py:875` creates rows directly while restoring a profile
  export; an export holding both directions restores both.
- Two simultaneous requests in opposite directions. `Friendship.request` reuses an existing row via
  `between()`, but neither caller sees the other's row before inserting, and the unique constraint
  does not cover the reversed key.

`test_calendar_sync.CalendarInviteIdentityMaskingTests` builds one deliberately, which is how this
surfaced: with mute wired into notification delivery, `between().get()` raised
`MultipleObjectsReturned` on every notification between such a pair. Before that it was quieter but
not harmless - the same raise sat behind the profile page, the friends API and
`NotificationLog.is_friend_request_pending`.

**Done 2026-08-20 (containment, not the fix):** `between()` now returns the **oldest** matching row
deterministically and logs a warning, rather than raising - a second row is data to repair, not a
reason to refuse to answer, and picking arbitrarily would make the answer depend on query planning.
`notifications_muted` does not go through `between()` at all: it asks the same predicate
`profiles_muting` does, so both mute paths read *either* row and cannot disagree.

**Not done: the constraint itself.** The real fix is a normalised pair key - a
`UniqueConstraint` on `Least(from_profile_id, to_profile_id), Greatest(...)`, or a
`CheckConstraint` forcing `from_profile_id < to_profile_id` with the direction moved to its own
column. Either needs a data migration that merges existing reciprocal pairs, and merging is not
mechanical: the two rows can hold different `status` values (one `Accepted`, one `Removed`), and
which one is right depends on history nothing records. Worth doing with a real look at production
data rather than a guess. Until then, a reciprocal pair is a warning in the logs, not an exception.

## P9 — REData gaps remain - `?limit=` is inert, 15 routes unwired, and a `tile_template` slide is a single 256px tile

`id: P9` · `status: open` · `updated: 2026-08-19`

Previously titled "REData consumption gaps left after this session's sweep".

A full cross-repo sweep of UrbanLens's REData integration on 2026-08-19 (both repos read end to end:
REData's `api/urls.py`, every serializer, `../REData/docs/api-reference.md`, `docs/fields-available.md` and the
whole `CHANGELOG.md` `[Unreleased]` section, against all ~32 `redata_*` gateways and 8 panels).
Everything that was *wrong* was fixed in the same pass - four panels reading keys REData has never
emitted, the places gateway parsing a `{count, results}` envelope as a bare array, `?is_aerial=true`
being a parameter of a different endpoint, CRIS selecting resources that were not CRIS's, Florida's
whole sale-record provider being dropped on attribution. What follows is what was found and
deliberately **not** done, so the next pass starts from here rather than re-deriving it.

**`?limit=` is inert on every REData near-point endpoint.** `NearPointQuery`
(`REData/src/redata/api/coordinates.py`) parses `lat`/`lng`/`radius_meters`/`provider`/
`force_refresh` and nothing else; the `limit` parsing at :411 belongs to the *text*-query parser. So
every panel that passes `limit=20`/`25`/`30`/`50` caches up to REData's own server-side cap instead.
Not fixed here on purpose: trimming client-side would change the user-visible counts panels report
("N mapped within 250 m") from REData's floor to our own arbitrary bound, which is less accurate,
not more. The fix belongs in REData - have `parse_near_point_query` accept `limit` - after which the
UrbanLens side needs no change at all.

**45 of REData's 106 routes have no UrbanLens caller.** 15 are judged worth wiring up, in rough
value order: the `/street-view/` base endpoint and its two mirrored-bytes download routes (the
carousel currently hot-links provider URLs that rot), `GET /places/cid/{cid}/` plus its media
download (UrbanLens already holds the CID and throws the deep-scraped place data away),
`/parcels/{uuid}/coverage/` (the Property Records panel fires four supplementary calls per parcel
blind), four `/parks/{code}/` routes including live closure/hazard alerts, `POST /imagery/capture/`,
`/reference-documents/` near-point (Wikidata's structured heritage claims reach UrbanLens from
nowhere else), and the `land-use-areas`/`demographics`/`national-parks` parcel trio. 23 are
irrelevant by design (nested write CRUD, the readback ViewSets, IIIF).

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

**Open, found while doing it: a `tile_template` slide is one 256px tile, and the pin can be at its
edge.** `_resolve_tile_template` resolves the single tile *containing* the coordinate at zoom 15
(~1.2 km across), so a site near a tile boundary is shown in the corner of its own photograph, or
half out of frame. Tolerable for OpenTopoMap, where the slide is terrain context; wrong for
`s2cloudless` and any future tiled imagery, where the slide is supposed to be a picture of the
place. The fix is compositing a 2x2 or 3x3 block centred on the point, which is a real piece of
work (fetch, stitch, encode) rather than a parameter change.

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

## P10 — `main` is untested against an empty database; the multiple-leaf migration conflict that broke it is gone

`id: P10` · `status: open` · `updated: 2026-08-19`

Previously titled "`main` cannot start from an empty database - conflicting migrations".

Found by the new dev-environment tooling on its first clean run: `bin/dev_env.py create --branch
main` builds the stack, and the app container dies during init with

```
CommandError: Conflicting migrations detected; multiple leaf nodes in the migration graph:
(0002_v0_4_0b0, 0006_v0_4_0_indexes in dashboard).
```

This is a property of the branch, not of the tooling - the environment was correctly isolated
(`ul_<slug>_*` containers, own database) and every other step passed. Any deploy of `main` against a
fresh database fails the same way; existing databases are unaffected, because `migrate` only walks
the graph when it has work to do, which is why nothing has noticed.

The fix is a merge migration (`makemigrations --merge`) on `main`, or removing whichever leaf is
redundant. Worth checking before the next release branches off it.

`bin/check_migration_graph.py` **does** catch it - pointed at main's tree it reports exactly this,
naming both leaves. The check simply postdates `main`: that file does not exist on that branch, and
pre-commit only runs what the checked-out branch carries. So this is not a gap in the check; it is a
branch that has not received it yet, and merging forward is enough to stop it recurring.

(An earlier draft of this entry claimed the checker lacked a leaf check. That was wrong - verified by
running it against the cloned main checkout.)

**The named conflict is gone as of 2026-09-03.** `0002_v0_4_0b0` no longer exists on `origin/main`
at all - the migrations were renumbered, and `0002` is now
`0002_boundary_emailsendlog_externalvisitparticipant_and_more`. Walking the `dependencies` of all 31
migration files on `origin/main` finds a **single** leaf, `0031_v0_7_0_indexes`, and "multiple leaf
nodes" is precisely what that error reports - so it cannot fire.

That is narrower than this entry's headline, which is left open deliberately: it says `main` cannot
*start from an empty database*, and one leaf only rules out this particular cause. Confirming the
whole claim means what found it - `bin/dev_env.py create --branch main` - since a data migration
that fails on empty tables would look nothing like this and is not visible from the graph.

## P11 — ~40 raw `fetch()` calls bypass `fetch-json.ts` and fail silently; Organize's Media tab is unwired dead UI

`id: P11` · `status: open` · `updated: 2026-08-15`

Previously titled "frontend TypeScript audit - remaining findings".

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
  Separately, `_organize_label_card.html:77` references `peopleMergeSingle`, which is defined
  nowhere in the codebase.
- ~~`shared/organize-filter-engine.ts:188` - `countVisibleCards` tests `card.style.display`, but
  tree view sets `display` on the `.tag-tree-item` *wrapper*, so cross-tab match counts and the
  "N categories also match" footer count every card as visible. It duplicates `getOrgVisibleCards`
  (:99), which gets it right.~~ Fixed 2026-08-22: `countVisibleCards` now delegates to
  `getOrgVisibleCards` instead of re-deriving the check.
- `shared/map-image-overlays.ts:209` - corner drag never handles `pointercancel`; an interrupted
  touch gesture leaves `map.dragging` disabled permanently.
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

## P12 — ~10 SCSS files use undefined `--ul-accent`/`--ul-border`/`--text` vars, so dark mode never reaches those rules

`id: P12` · `status: open` · `updated: 2026-07-25`

Previously titled "Full-codebase audit (2026-07-25): curated high-severity findings".

A systematic full-codebase audit (every model/controller/service/template/TS/SCSS file, all
migrations, the full test suite) ran 2026-07-25, tracked in `docs/notes/ai/codebase-audit.md` (35
units, full findings with file:line references for every bug/inefficiency/improvement found — see
that doc for anything not listed here, including all "improvement"-grade and maintainability
findings). This section curates only the highest-severity/highest-impact items into this file's
convention; the full ranked list per feature area is more complete.

**SSRF: Immich server URL is user-controlled with no private-IP/scheme guard.** ~~Any authenticated
user can point `server_url` at internal infrastructure and use the ping/thumbnail endpoints as an
authenticated blind-SSRF oracle.~~ **Already fixed, stale entry** (re-checked 2026-08-25):
`forms/immich_form.py`'s `ImmichAccountForm.clean_server_url` calls the shared
`services.security.url_safety.ensure_public_http_url(url)` and raises on any loopback/private/
link-local/reserved/CGNAT target; `ImmichSettingsView.post` only ever builds the candidate
`ImmichAccount` from the already-validated `cleaned_data`, and no other path constructs one.

**SSRF: `services/security/url_safety.py`'s IP blocklist misses RFC 6598 CGNAT space.**
~~`is_blocked_address` checks `is_private`/`is_loopback`/`is_link_local`/`is_reserved`/
`is_multicast` but not the `100.64.0.0/10` Carrier-Grade-NAT range.~~ **Already fixed, stale
entry** (re-checked 2026-08-25): `url_safety.py` now defines `_CGNAT_NETWORK =
ipaddress.ip_network("100.64.0.0/10")` and checks it explicitly, traced to commit `86e55aa1`.
8 live tests pass in `test_push_endpoint_ssrf.py`, including `test_cgnat_address_is_refused`.

**Decompression-bomb protection in the full-archive importer only checks forgeable declared
sizes.** ~~`import_data.py` sums each ZIP member's *declared* `file_size` against a ceiling, then
calls `zf.extractall()` unbounded... A companion gap: GPX parsing has no `defusedxml` wrapper.~~
**Already fixed, stale entry** (re-checked 2026-08-25), both halves. Extraction now goes through
`_extract_zip_members_bounded`, which streams each member in capped chunks and enforces the
ceiling against actual decompressed bytes, aborting mid-stream on overrun, and skips symlink
entries the same way `archive_extractor.py` does. `gpx.py`/`gpx_tracks.py` both pre-parse with
`defusedxml.ElementTree.fromstring` before handing vetted text to `gpxpy`, matching `osm_xml.py`'s
pattern; no other `gpxpy` call site parses untrusted input. Traced to commits `86e55aa1`,
`a2743a29`, `abb0f30d`/`10121f18`, all already ancestors of HEAD.

**WebSocket consumers crash on any binary frame, leaking channel-layer group membership.**
~~Every consumer declares `async def receive(self, text_data):` with no `bytes_data` parameter...
raising an uncaught `TypeError`.~~ **Already fixed, stale entry** (re-checked 2026-08-25): all
five consumer `receive()` methods in `consumers.py` now declare `bytes_data=None` and begin with
`if text_data is None: return`, so a binary-only frame no longer raises and `disconnect()`/
`group_discard()` runs normally.

**Two rate-limit/quota checks with the same non-atomic check-then-act race**, each independently
discovered. **Already fixed, stale entry** (re-checked 2026-08-25): `rate_limiter.py`'s general
limiter path (the one this entry names, "used e.g. for Nominatim calls") now goes through
`_reserve_call`/`_finalize_call`, locking the service's `ApiRateLimit` row via `select_for_update()`
inside `transaction.atomic()`, and every `Gateway` subclass's session is wired to it; a few direct
AI-service callers not named in this entry (`vision.py`, `trivia/*`, `article_expansion.py`) still
call `check_rate_limit`/`log_api_call` separately. `email_safety.py`'s `email_rate_limit_error` now
reserves an in-flight slot via atomic `cache.add()`+`cache.incr()` before counting it against the
window. Both covered by existing tests (`ReserveCallMinIntervalTests`, `EmailLimitResolutionTests`).

**AI provider token/cost accounting is silently doubled for 2 of 3 wired providers.**
~~`services/ai/gateway.py` calls `self.receive_tokens(message)`... but `anthropic.py` and
`openai.py` *also* call `self.receive_tokens(body)` inside their own `_parse_response()`.~~
**Already fixed, stale entry** (re-checked 2026-08-25): neither provider's `_parse_response` calls
`receive_tokens` any more - both just log and return, matching `cloudflare.py`'s shape. Exhaustive
grep for `receive_tokens|send_tokens` under `services/ai/` finds zero call sites outside
`gateway.py`.

**~~Friend-request-visibility bypass via the email-invite path.~~ FIXED - this entry was stale
(verified 2026-07-27).** It described `invite_by_email` checking only
`to_profile.friend_request_visibility != VisibilityChoice.NO_ONE`. That logic has since moved out
of the controller into `services/social/friendship.py:invite_by_email`, which runs the full
`Profile.visibility_permits` evaluator - the same one `request_friend` uses. The non-`NO_ONE`
cases this entry correctly flagged as untested are now covered; see the resolved friend-invite
entry above, including the UX consequence the fix carries.

**`common_pin_count` is shown regardless of its own visibility gate.** ~~The *count* of shared
pins between two profiles is computed and rendered unconditionally; only the link to the detail
page is gated by `can_view_common_pins_with`.~~ **Already fixed by prior, unrelated work** (found
stale on re-check 2026-08-25 - a genuinely resolved defect this doc hadn't caught up to):
`controllers/userprofile.py`'s `_add_common_context` now computes `common_pins_permitted =
profile.can_view_common_pins_with(my_profile)` and sets `context["common_pin_count"]` to `None`
when denied; the template only renders the "Places in Common" block when that's truthy. Covered by
`test_shared_visited_respects_privacy.py`.

**Login-lockout is keyed on the raw submitted string, not the resolved account — brute-force is
bypassable.** `controllers/account.py:51-58,623-634,657-692` keys failed-attempt/lockout counters
by the literal POSTed "username" value, but `EmailOrUsernameModelBackend`
(`services/auth/auth_backend.py:14-36`) resolves that same field against primary email, verified
secondary email, and Gmail dot/plus-normalized variants before authenticating. **The lockout logic
itself was already fixed by unrelated prior work** (`_lockout_key_for_identifier`/
`_resolve_login_user`/`_raw_lockout_key` mirror the backend's normalization exactly); what this
entry correctly flagged as missing - "no equivalent test exists for the lockout path" - **is now
covered, added 2026-08-25** (`cc0e7f42`): an end-to-end test that 5 failed attempts across three
Gmail-equivalent identifiers locks the account against a 6th, never-before-used equivalent
identifier, plus a direct unit test that all three variants collapse to the same lockout key.

**`Label` kind-conversion has no branch for converting to Category — silently orphans the row.**
~~`_apply_kind_conversion` handles converting to Status/Tag but has no branch for `new_kind ==
KIND_CATEGORY`... permanently un-editable and un-deletable through the UI.~~ **Already fixed,
stale entry** (re-checked 2026-08-25): `controllers/labels.py`'s `_apply_kind_conversion` already
has a `new_kind in (KIND_STATUS, KIND_CATEGORY)` branch that sets `label.profile = profile`,
landed in the same 2026-07-30 release-merge commit (`abb0f30d`) this entry itself traces to - the
finding and its fix landed together and the doc was never reconciled. Covered by
`test_label_kind_conversion.py`.

**Safety check-in escalation can re-email every emergency contact on any partial failure.**
~~`escalate_checkin` loops all contacts unconditionally (no `notified_at__isnull=True` filter) and
only saves status *after* the whole loop completes.~~ **Already fixed, stale entry** (re-checked
2026-08-25): `services/visits/safety.py`'s `escalate_checkin` already loops
`checkin.contacts.filter(notified_at__isnull=True)` and saves `notified_at` immediately after each
successful send, inside the per-contact loop - landed in commit `86e55aa1` (2026-08-02), after the
finding but before this doc was reconciled. The "compounding gap" aside about beat-task locking is
also stale: `tasks.py` now has an explicit run-lock guard per checkin type. Covered by
`test_escalate_checkin_notifies_contacts_without_resolving`.

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

## P14 — Authenticated media gate - residual per-family risk (2026-07-23)

`id: P14` · `status: open` · `updated: 2026-07-23`

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
  enumerator could fetch other users' custom icons. Fix would be: owner OR global label OR an
  existing share/visibility relationship.
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
  code - the right shape, but a signal touching five models deserves an owner's review rather
  than an audit chunk, and the residual is small decorative icons under an already
  authenticated-only branch.
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

## P17 — `docker compose exec app pytest` trips the localhost-only network guard because Valkey is a bridge IP

`id: P17` · `status: open` · `updated: 2026-07-24`

Previously titled "`docker compose exec app pytest` can't reach Valkey in the `s1`/`s2`/`s3` dev environments (found 2026-07-24)".

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

## P18 — The setup wizard's always-dark sidebar uses inverting `--ul-grey-N` text tokens, unreadable in dark mode

`id: P18` · `status: open` · `updated: 2026-07-25`

Previously titled "Setup wizard sidebar reuses inverting `--ul-grey-N` tokens on an always-dark panel (found 2026-07-25)".

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

## P19 — Audit re-verification's residual gaps remain: dead ownership re-check, 1,100-line `_dark.scss`, stub AI gateway

`id: P19` · `status: open` · `updated: 2026-07-25`

Previously titled "Full-codebase audit: re-verification pass (2026-07-25)".

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

## P21 — `LocationWikiEditView.post` drops invalid wiki field edits and still answers `{"ok": true}`

`id: P21` · `status: open` · `updated: 2026-07-26`

Previously titled "Messaging / external API (noted 2026-07-26, during the mobile v2 messaging API build)".

- **WebSocket credential auth does no per-scope check.** ~~`ApiKeyAuthMiddleware`... authenticates
  a WebSocket connection from any valid, unrevoked credential and then grants blanket access -
  it never consults the connection's scopes.~~ **Already fixed, stale entry** (re-checked
  2026-08-25): `consumers.py` now has a `CredentialScopeMixin` (L84-172) providing
  `credential_allows(*scopes)`, used identically to `external_api`'s `credential_grants`/
  `OAUTH2_ONLY_SCOPES` logic - exactly the "Fix shape" this entry prescribed. Every consumer's
  `connect()` calls it before joining any channel-layer group and closes with 4404 on failure:
  `UserNotificationConsumer` requires `NOTIFICATIONS_READ`, `DirectMessageConsumer` requires
  `MESSAGES_READ`/`MESSAGES_WRITE`, `SafetyCheckinChatConsumer` requires `SAFETY_READ`/
  `SAFETY_WRITE`, and the shared `_ParticipantSessionConsumer` (Game/Trivia/Consensus) requires
  `GAMES_READ`/`GAMES_WRITE`. Session-authenticated connections are unaffected by design. There is
  also a periodic re-validation loop that closes a socket if its credential is later revoked/
  expired, beyond what this entry asked for. Covered by `test_websocket_credential_scopes.py`
  across exactly the four cases named above.

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
  that work, and all three live in files it does not touch) - **already fixed, stale entry**
  (re-checked 2026-08-25, fresh `mypy --no-incremental src/urbanlens` reports zero errors in 861
  files): `dashboard/models/boundary/queryset.py:85` (`buffer_point_by_meters`) now has
  `if not isinstance(circle, Polygon): raise TypeError(...)` before using the result, the exact
  narrowing check this entry asked for rather than a `cast`; `dashboard/forms/search.py:178`
  (`SearchForm._clean_reference_field`) now has `if self.profile is None: raise
  forms.ValidationError(...)` before the call that needed a non-optional `Profile`; and
  `dashboard/controllers/trip.py` no longer contains the pattern at all - the `creator_id` usages
  left are plain `trip.creator_id == profile.id` comparisons, refactored away rather than merely
  guarded.

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
  (`src/urbanlens/dashboard/controllers/maps.py:384-408`). ~~Its sibling `autocomplete_places`...
  does check `request.user.profile.external_apis_enabled`... but `resolve_place`... checks only
  whether an API key/REData is configured.~~ **Fixed 2026-08-25** (`6bdf7e7e`): added the same
  `if not request.user.profile.external_apis_enabled: return ... 403` guard immediately after the
  missing-`place_id` check, mirroring the pattern `external_api/views.py::PlaceResolveView.get`
  and `streetview_check` already use. Guarded by `test_resolve_place_blocked_when_external_apis_disabled`.

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
  `src/urbanlens/dashboard/services/messaging/group_chats.py::group_conversations_for`) - a page of
  N conversations issued ~2N extra queries reading `message.reactions.all()`/`message.share_for(viewer)`
  on each `last_message`. **Fixed 2026-08-25** (`20507627`): both functions already resolved
  `last_message` via a second, id-bounded query separate from the wider per-group scan, so the fix
  was adding `prefetch_related('reactions__profile')` (plus `'shares'` for groups) onto those
  already-scoped queries, rather than onto the message-history-wide scan. The thread endpoints -
  where a page is 50 messages rather than 1 - already prefetched, so this was an inbox-only cost.

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
  must re-block to normalize the row, and the blocked party can lift it.
  **The suggested audit query is now a real tool, added 2026-08-25** (`6057e154`):
  `manage.py audit_inverted_friendship_blocks --before YYYY-MM-DD`, a read-only management
  command with no default `--before` (deliberately - the fix's actual deploy date to a given
  production database is something only a human can know) that reports `BLOCKED` rows created
  before that date, flagging ones that show a sign of having been reused from a pre-existing
  relationship (a stored `request_message`, or an `updated` timestamp meaningfully after
  `created`) versus rows provably created directly as a block. Never writes - there is no stored
  signal that could prove a row is actually inverted, so an automated migration was never on the
  table. 9 new tests.

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

## P26 — `create_group_message` never validates `key_version`, so a sender can use a key a removed member holds

`id: P26` · `status: open` · `updated: 2026-08-07`

Previously titled "E2EE group messages: the cryptographic membership boundary depends on the server (2026-08-07)".

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

## P28 — The upload quota check is fail-open under a cache lock, so a bulk import's fan-out can still exceed the quota

`id: P28` · `status: open` · `updated: 2026-08-12`

Previously titled "bulk-import paths skip the upload quota lock, which is fail-open anyway".

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

**Partially addressed 2026-08-25** (`bf9c31b0`). The six-call-site asymmetry named above is closed:
all seven background call sites (one more than counted here - `import_data.py` has two distinct
sites, `_import_photos` and `_restore_overlay_image`, not one) now wrap their check-then-create in
`per_profile_upload_lock`, matching the interactive-path pattern exactly. **This is still only the
same partial, fail-open mitigation every interactive path already has** - it narrows the race
window but does not bound a true concurrent fan-out, exactly as this entry says above. The real
fix - a dedicated running-total column - is unchanged and still a design decision, not a refactor:
Jess's 2026-08-25 sign-off on this entry said she didn't follow the "running-total column"
proposal and wants it re-explained before any implementation, so that half stays open pending that
conversation.

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

## P30 — Backups are plain-SQL with no restore path, and the repo's only `pg_restore` example cannot read them

`id: P30` · `status: open` · `updated: 2026-08-13`

Previously titled "Database backups have no restore path, and their format defeats the only example".

`core/controllers/backups/db.py` produces **plain-SQL** dumps: `pg_dump -U ... -f <path>`, no
`-Fc`, written as `backup_<YYYYMMDD>_<HHMMSS>.sql`. Creation, retention, scheduling, the atomic
temp-file rename, and (as of the 2026-08-14 audit chunk) reaping of abandoned `.tmp` files are all
implemented and tested.

Restoring one is not implemented, not documented, and not tested.

- No code path in `src/` or `bin/` restores a scheduled backup.
- The only `pg_restore` anywhere is the `infrastructure` repo's
  `bin/clone_prod_to_staging.sh:158` (moved there from this repo's own `bin/`
  since it was written; see `../infrastructure/docs/OPS_TOOLING.md` there), which restores
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

## P31 — Session and DM chat sockets have no rate limit and cap frame size only after the whole frame is parsed

`id: P31` · `status: open` · `updated: 2026-08-13`

Previously titled "Session chat WebSockets have no rate limit or frame-size cap".

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

## P32 — `check_rate_limit` returns True on a `DatabaseError`, so a database failure uncaps paid-API spend

`id: P32` · `status: open` · `updated: 2026-08-13`

Previously titled "The API rate limiter fails open, which uncaps spend rather than availability".

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

## P33 — `Label.color` has no `save()`-level coercion, so a value bypassing form validation is stored unvalidated

`id: P33` · `status: open` · `updated: 2026-08-13`

Previously titled "Colour values interpolated into `style="…"` - fixed at every entry point; the model fields still have no validators".

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

~~Left for a future pass: the model fields themselves are still permissive (`MarkupShape.color`/
`border_color` have no `choices`)~~ - **`MarkupShape` (`PinMarkup`) side fixed 2026-08-25**,
investigated per a "note if already fine" instruction and found already structurally closed:
`PinMarkup.save()` calls `self.coerce_colors()` (running `sanitize_hex_color`/
`sanitize_optional_color` on both fields) before `super().save()`, and `PinMarkupQuerySet.bulk_create`
- the one write path that bypasses `.save()` - separately calls it on every object before
delegating to Django's real `bulk_create`. This is the *stronger* of the two remedies this entry
proposed: a plain `validators=[...]` would not have closed the gap on its own, since Django only
runs field validators inside `full_clean()`, which none of these write paths call. Covered by
`PinMarkupColorStorageTests`/`PinMarkupBulkCreateColorTests` (`test_markup_colors.py`).

**`Label.color` is still open** - it has `choices=COLOR_CHOICES` but no equivalent `save()`-level
coercion, so a value bypassing form validation could still slip past. Not addressed here; this
entry's title still applies to that one field.

---

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

## P35 — Seven named routes still have no discoverable caller and remain unreviewed authorised surface

`id: P35` · `status: open` · `updated: 2026-08-13`

Previously titled "Nine named routes with no discoverable caller (candidates for review, not confirmed dead)".

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

## P36 — 45 BEM modifiers are applied in templates with no CSS rule, so intended visual states never render

`id: P36` · `status: open` · `updated: 2026-08-13`

Previously titled "46 BEM modifiers applied in templates with no CSS rule".

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

## P37 — 100 write handlers totalling 1,217 statements never execute under the test suite

`id: P37` · `status: open` · `updated: 2026-08-13`

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

## P38 — `Pin.change_category`, `Pin.add_category` and `Wiki.add_category` have no production callers, so their tests fake coverage

`id: P38` · `status: open` · `updated: 2026-08-13`

Previously titled "The category-on-pin methods have no production callers".

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

## P39 — `clean_color` coerces invalid colours to the default, so API clients lose the value silently instead of a 400

`id: P39` · `status: open` · `updated: 2026-08-13`

Previously titled "API behaviour change: non-hex colours are no longer stored".

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

## P40 — `Pin.by_category` and `Wiki.by_category` have no callers and omit `distinct()`, so any caller inherits duplicate rows

`id: P40` · `status: open` · `updated: 2026-08-13`

Previously titled "Two dead queryset methods".

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

## P41 — 70 of 251 public queryset methods have no production caller, so their logic may be duplicated inline elsewhere

`id: P41` · `status: open` · `updated: 2026-08-13`

Previously titled "Queryset API with no production caller: 70 of 251 (candidate count)".

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

## P42 — `_apply_trip_list_identity_masking`'s docstring cites a `docs/PROBLEMS.md` gap entry that does not exist

`id: P42` · `status: open` · `updated: 2026-08-14`

Previously titled "`trip.py`'s masking docstring cites an entry that is not here".

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

## P43 — Tracked source cites `docs/notes/ai/completed.md`, which is gitignored, so those references cannot be resolved

`id: P43` · `status: open` · `updated: 2026-08-14`

Previously titled "audit of all 26 code references to this file".

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

## P44 — `isMouseContextMenu` misreads a keyboard context menu as touch, so the next Enter activation may be swallowed

`id: P44` · `status: open` · `updated: 2026-08-16`

Previously titled "Keyboard-invoked context menu may swallow the next activation (unverified)".

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

## P45 — Five documents cite a root `TODO.md` deleted in `3f12e875`, and `docs/prompts/` was never tracked at all

`id: P45` · `status: open` · `updated: 2026-08-16`

Previously titled "The planning and handoff documents referenced across the docs do not exist".

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

## P46 — A group message can still be sent under a key version a removed member holds

`id: P46` · `status: open` · `updated: 2026-08-16`

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

## P48 — Logging out leaves every decrypted E2EE key cached in IndexedDB, and nothing clears it

`id: P48` · `status: open` · `updated: 2026-08-17`

Previously titled "Should logging out wipe the cached E2EE keys? (product decision, filed 2026-08-17)".

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

## P49 — `npm run git-squash` is a force-deploy with none of `deploy.sh`'s dirty-tree guards

`id: P49` · `status: open` · `updated: 2026-08-17`

Previously titled "`npm run git-squash` is a force-deploy with none of `deploy.sh`'s guards (minor)".

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


### Fourteen documentation citations still point at the wrong line (noted 2026-08-17)

`bin/check_doc_line_refs.py --report-drift` lists them. They survived the 2026-08-17 sweep because
they can't be repaired mechanically, and they split into two kinds:

- **The line moved, but the anchor isn't a definition.** `settings/base.py:343` for
  `hard_delete_expired_direct_messages` (now line 374) is cited via its entry in the beat-schedule
  dict, and `tasks.py:1629` for `RUN_LOCK_CACHE_KEY` via an import. Renumbering these is safe but
  needs a human to confirm which usage was meant.
- **The symbol no longer exists at all.** `controllers/trip.py` line 135 cites `_mask_trip_identities`
  and `services/ai/anthropic.py` line 117 cites `send_prompt`/`send_prompt_list`; neither name appears
  anywhere in the tree now. (Written without the usual `file.py:line` punctuation on purpose: these
  are quoted *examples* of broken citations, and `bin/check_doc_line_refs.py` cannot tell a quoted
  one from a live one.) The repair is rewriting the sentence around whatever replaced them, not
  changing the number - and guessing at that would put invented history into the record.

The eight that *were* mechanically provable (anchored on a `def`/`class` the tool could locate
uniquely) are fixed, and `check_doc_line_refs.py` now runs in CI to keep past-end-of-file citations
at zero.

## P50 — `test_safety_chat` and `test_migration_0039_reverse` fail only under a randomized suite order

`id: P50` · `status: open` · `updated: 2026-08-18`

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

## P52 — `.app-nav-right` runs 40px past a 390px viewport, so every page scrolls sideways at phone width

`id: P52` · `status: open` · `updated: 2026-08-24`

Previously titled "the nav bar, not the map, is what overflows at phone width".

The 2026-08-23 entry recorded "the map page scrolls sideways at 390px" and
guessed the map. It is not the map. Once the overflow probe was taught to ignore
elements clipped by an ancestor - `getBoundingClientRect` reports geometry as if
nothing clipped it, so every Leaflet tile drawn past its own `overflow: hidden`
container looked guilty - the real culprits came out shallowest-first:

    div.app-nav-right       — right edge at 430px (viewport 390px), width 230px
    div#nav-user.nav-user   — right edge at 430px (viewport 390px), width 140px
    button#nav-user-btn     — right edge at 430px (viewport 390px)

The navigation bar's right-hand group runs 40px past a 390px viewport. It is on
every page; the map page is simply where it was noticed, presumably because
other pages clip it somewhere up the tree. Fixing it is a CSS change to
`.app-nav-right`'s layout at narrow widths, which wants doing in front of a
browser rather than blind - but the element is now named.

## P53 — One Private Pin page load fires dozens of concurrent panel requests and can exhaust the DB connection pool

`id: P53` · `status: open` · `updated: 2026-08-24`

Previously titled "one Private Pin page load can exhaust the database connection pool".

Found by `tests/integration/` on 2026-08-24, and only visible because the
console/network guard watches every request a page makes rather than just the
document.

Opening `/dashboard/map/pin/<slug>/` fires roughly **thirty concurrent HTMX
requests** - one per enrichment panel, plus the media and overview fragments -
and each one is a Django request that takes its own database connection
(`CONN_MAX_AGE` is 0, so connections are per-request). On the dev stack, whose
Postgres runs the default `max_connections = 100`, that tipped over: 14 requests
in one hour failed with

    django.db.utils.OperationalError: connection to server at "urbanlens_db",
    port 5432 failed: FATAL: sorry, too many clients already

The failures are spread evenly across seven different panel endpoints, one each
- `azure-maps`, `location-data-overview`, `markup-maps`, `media/cris_building`,
`panel/epa_echo_detail`, `panel/property_records`, `panel/redata_permits` - which
is the signature of pool exhaustion rather than of any one panel being broken.
Whichever panel arrives when the pool is full is the one that 500s.

**How much of this is the test environment.** Some: the suite runs several
browser workers, so more than one pin page was loading at once, and a single
container's Postgres is smaller than a real deployment's. But the shape does not
depend on that - a page that opens thirty connections at once needs only three
simultaneous readers to want ninety, and the panels are the *point* of that page,
so this is what a normal user does rather than a stress case. It is also
user-visible when it happens: `themes/base.html`'s global `htmx:responseError`
handler raises an error toast per failed panel.

Not fixed here, because every fix is a decision rather than a repair: cap the
client-side fan-out so panels load in waves, give the panel views a shared
connection or move them behind one request, raise `max_connections`, or put
pgbouncer in front. The first is the only one that helps a deployment of any
size.

**What has been done, short of fixing it.**
`test_pin_detail_fanout_budget.py` renders the page and asserts the number of
elements that fetch on load stays under a ceiling. It does *not* reproduce the
exhaustion - that needs concurrency against a real pool, which a suite issuing
one request at a time does not have - but it holds the number, which is the
cause, and which creeps up one innocuous panel at a time. The ceiling is set
**at** the current count, so it is a ratchet rather than an endorsement: raising
it should take an argument, and it should come down when the real fix lands.

Two measurements worth recording. The rendered count is **53**, not the ~30 seen
above; the difference is real rather than an error in either, because some
triggers carry a filter (`load[!window.ulSectionCollapsed(...)]`) and stay quiet
for a collapsed section. 53 is the ceiling a user with everything expanded
reaches, which is the number a budget should bound.

Related: this is the concrete instance of the load-testing gap recorded in
`docs/TOOLING.md` under "Evaluated, not adopted" - the integration suite found
it by accident, which is not a substitute for looking on purpose.

## P54 — `docker-compose.hot-reload.yml` crash-loops when the checkout is not the container's uid

`id: P54` · `status: open` · `updated: 2026-08-23`

Previously titled "docker-compose.hot-reload.yml crash-loops when the checkout is not the container's uid (2026-08-23)".

Bringing an agent dev environment up with the hot-reload overlay puts `app` into
a restart loop and the site answers 502:

```
File "/app/src/bin/init.py", line 335, in build_frontend
    frontend_dir.mkdir(parents=True, exist_ok=True)
PermissionError: [Errno 13] Permission denied:
    '/app/src/urbanlens/dashboard/frontend/static/dashboard/css'
```

`docker cp` preserves source ownership and a bind mount exposes it directly, so
the container's `appuser` cannot write anywhere inside the mounted tree. The
overlay already knows this - it redirects `UL_LOG_DIR` out of the tree for
exactly this reason, and its header explains why - but `init.py`'s
`build_frontend()` also writes into the mounted tree on every start, and that
was not accounted for. It only shows up where the checkout belongs to a
different uid than the image's `appuser`, which is every environment
`dev_env.py` creates.

The overlay delegates SCSS to a `sass-watch` sidecar already, so the app
container's own frontend build is redundant under hot reload. Teaching `init.py`
to skip it (an env var the overlay sets) looks like the fix, rather than
loosening permissions on the checkout.

Until then, updating a `dev_env.py` environment means the documented
`docker cp` + `chown` route rather than hot reload - and note that
`STATIC_ROOT` is a *separate* collected tree (`src/urbanlens/frontend/static`,
not `dashboard/frontend/static`) served by whitenoise even with `DEBUG=True`,
so a copied-in JS bundle is not served until `collectstatic` runs.

## P55 — A community quota bonus survives un-sharing the photo that earned it

`id: P55` · `status: open` · `updated: 2026-08-23`

Previously titled "A community quota bonus survives un-sharing the photo that earned it (2026-08-23)".

`services/media/quota_rewards.py` stamps `QuotaExemption.COMMUNITY_CONTRIBUTION`
on an image once it is on a wiki, has an owner, is not cached external media, and
has collected `SiteSettings.community_photo_quota_bonus_votes` relevance votes.
Nothing anywhere clears `quota_exempt_reason` afterwards - grep finds no writer
outside that grant and the 0033 backfill.

So: contribute a photo to a wiki, collect the votes, then remove it from the
wiki. The photo is private again and permanently exempt from your quota. Repeat
for as much free storage as you care to earn.

**The permanence is deliberate and the reason is good**, which is why this needs
a careful fix rather than a revert. The module's own docstring: the reward is
one-way "so a user who is comfortably inside their quota can't be pushed over it
retroactively by other people changing their votes". That protects against *other
people's* later actions. It was never meant to cover the owner withdrawing the
contribution themselves, and those two cases are distinguishable:

- votes fall below the threshold, or a voter leaves -> keep the bonus, exactly as
  now
- the image's own `wiki` link is removed by its owner -> the contribution that
  earned the bonus no longer exists, so neither should the bonus

Not trivially exploitable: step two needs genuine relevance votes from other
people, so this cannot be self-served in a loop. It is a way to convert community
goodwill into permanent private storage, not a way to mint quota from nothing.

Worth deciding alongside it: the exemption is currently a boolean-ish flag on the
row, so a photo either costs its owner nothing or costs full price. Recording the
bonus as an amount tied to the wiki relationship (rather than a flag on the image)
would make withdrawal a cascade rather than a sweep, and would let the UI show a
contributor what their contributions have earned them.

## P56 — `Cross-Origin-Embedder-Policy` is unset, and the third-party host inventory needed to set it does not exist

`id: P56` · `status: open` · `updated: 2026-08-28`

Previously titled "Nuclei scan follow-ups (2026-08-28)".

**`Cross-Origin-Embedder-Policy` is not set.** `require-corp` would block every third-party image
and script that doesn't send its own CORP/CORS header - the Street View iframe, the OSM/ArcGIS/
OpenTopoMap tile hosts, Gravatar, any operator-pasted map-overlay image: needs a report-only-shape. Revisit once there's a real inventory of which third-party hosts
do and don't send CORP.

`cross-origin-embedder-policy` is the fourth thing this template flagged and
is not fixe

## P57 — Test-quality audit follow-ups (2026-08-29)

`id: P57` · `status: open` · `updated: 2026-08-29`

Found while auditing existing unit tests for real positive/negative coverage (see
`docs/notes/ai/test-quality-audit.md`); out of scope for a test-file-only pass, noted here per
convention rather than fixed inline.

**`LocalhostOnlyNetwork` (`core/testing_network.py`) doesn't patch `socket.socket.connect_ex`.**
It patches `.connect` and `.create_connection`, but `connect_ex` is a separate C-level method that
doesn't delegate through the patched `connect()`. Empirically confirmed: with the guard active,
`sock.connect_ex(('8.8.8.8', 53))` returned 0 - a real successful outbound connection. Any test or
third-party library code using `connect_ex` (some non-blocking-connect patterns in DB drivers do)
bypasses the guard entirely and can make genuine external network calls during the test suite,
undetected.

**`make_cache_key` (`core/cache_keys.py`) joins parts with a bare colon before hashing**
(`':'.join(str(part) for part in parts)`), so a part that itself contains a colon can collide with
a differently-shaped call - `make_cache_key('ns', 'a:b')` and `make_cache_key('ns', 'a', 'b')` hash
the same raw string and produce an identical key despite representing different logical arguments.
No current call site (pin lat/lng, location formatting, github repo slugs) passes a colon-bearing
part, so this is latent, not live - worth a length-prefixed encoding if this utility gains more
callers.

**`tasks.hard_delete_expired_accounts()` has no overlap lock, unlike its sibling
`send_account_deletion_reminders()`.** The reminder sweep acquires
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

**`models/achievements/signals.py`'s `on_achievement_saved` re-queues a full profile-table backfill
sweep on every save of an already-active achievement**, not just on creation or reactivation - e.g.
an admin renaming an award or tweaking its icon/color/order re-triggers the same site-wide backfill
task. The docstring frames this as intentional for re-activation, but firing on unrelated field
edits looks unintended. Negligible at current beta scale (~2 users); worth confirming intent before
it matters.

**`Location.address` / `Location.address_extended` leave a dangling trailing comma** when the last
populated component has nothing following it - e.g. a route-only address renders as exactly
`"Elm Ave,"`. The existing tests correctly pin down this behavior as current, so it reads as
intentional, but the trailing comma looks like a real address-formatting defect worth a look by
whoever owns Location/address display.

**`services/ai/assistant.py`'s `_tool_create_trip` / `_tool_add_trip_activity` reimplement the
`SiteSettings` quota checks** (`max_upcoming_trips_per_user`, `max_trip_activities`) and their
`select_for_update` locking inline, rather than calling the existing
`services.trips.trip_crud` / `services.trips.trip_activities` helpers the regular trip views use
(which enforce the identical caps). Duplicated business logic with no shared implementation - the
two enforcement paths can silently drift out of sync over time. Worth consolidating onto the
shared service functions.

**Wiki-owned albums are untested across the entire album test suite.** `test_albums.py`,
`test_album_cover_move_dedupe.py`, `test_album_view_ux.py`, and `test_album_add_race.py` all only
ever construct Pin-owned albums - the community/wiki half of the Album model (`parent_wiki`,
`owner_kwargs`, the concealment-aware `_owner_conceal`/`conceal_rows` path) has zero coverage.
Building that out correctly needs the wiki-access/concealment rules understood well enough to avoid
a shallow test - flagged for a dedicated pass rather than folded into this audit.

**`purge_old_backups`'s count-based retention (deleting the oldest backups beyond
`backup_retention`) has no dedicated test anywhere in the suite.** `test_backup_temp_purge.py` only
exercises the `.tmp`-reaping side effect of `purge_old_backups()` with zero real `.sql` backups on
disk, so the count-deletion loop (`backup_files[self.backup_retention:]`, sorted by mtime
descending) never actually runs in any test - nor does `DatabaseBackup.run()`'s success path
(pg_dump succeeding, `os.replace` to the final name, then `purge_old_backups()` firing). The
existing "Database backups have no restore path" entry above describes retention as "implemented
and tested", which overstates it for this specific branch. Worth a dedicated pass verifying that
with N backups on disk and a lower retention, exactly the oldest excess files are removed (by
identity, not just resulting count) and the newest `retention` survive.

**`RedataBasemapTilesGateway.list_sources()` envelope parsing is untested at the unit level.**
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

**`SubscriptionRole.clean()` doesn't validate `pwyw_minimum_cents` requires `pay_what_you_want`.**
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

**`WikiBoundaryView` has no test coverage at all.** `dashboard/controllers/boundary.py`'s
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

**Map-overlay caption length check is untested even though it's drivable.**
`test_caption_and_setting_length_limits.py`'s class docstring says the map-overlay caption path
can't be tested because it "fetches a remote image first, which the test network guard refuses" -
true for the `media_url`/`image_url` branches of `controllers/map_overlays.py::_image_from_request`,
but its direct-file-upload branch (`request.FILES.get("image")` +
`request.POST.get("name")` as caption, routed through `services/photos/photo_upload.py::upload_photo`)
takes no network call and is a plain multipart POST just like the safety-checkin path this file
already drives. The length check itself is present and correct, so this is a test-coverage gap and
a stale docstring claim, not a product bug.

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

**`TripCommentDeleteView` has zero test coverage.** `services/trips/trip_comments.delete_comment`
is the third call site of the shared `_discard_comment_image` cleanup helper (alongside
`PinCommentDeleteView` and `WikiCommentDeleteView`), and has its own `can_delete_comment`
permission gate (author or trip creator), but no test file anywhere in the suite exercises
`TripCommentDeleteView` or `delete_comment` at all - not the basic delete, the permission gate, or
image cleanup. Would need a full TripComment/Trip fixture setup, not a surgical addition.

## P58 — A photo's grid tile can 404/500 for seconds after upload while async processing renames its file

`id: P58` · `status: open` · `updated: 2026-08-31`

Previously titled "A photo's grid tile can 404/500 for a few seconds right after upload while async processing renames its file".

Found live-verifying Batch 4 (lightbox pin/wiki/album associations) against the `ae97b86` dev
environment - pre-existing (Batch 2's upload/grid work), unrelated to Batch 4 itself, and not a
Batch 4 regression. `tasks.process_image_upload` (the Celery task queued after every upload) can
re-encode the stored file and change its path (observed: `.jpg` -> `.webp`), deleting the original
once the new one is written (see `downscale_stored_image` in `services/media/images.py`, its
`stale_names` cleanup). A grid tile rendered from the upload response, or from a page load that
lands between the delete-old and any client-side refresh, points at the old path - a request for it
404s (`django.views.static.serve` before the delete completes, or racing it) or in one observed case
500s (`FileNotFoundError` mid-request, presumably the file disappearing between the storage
existence check and the actual read). Reproduced directly: `manage.py shell` confirmed a freshly
uploaded, not-yet-processed row's `image.url` serves 200 immediately, but the exact same URL for an
*older* row whose processing had by then completed and renamed the file returned a Django 500 with
`FileNotFoundError: ... lightbox-associations.jpg`. This is a narrow window (observed on the order of
single-digit seconds, worse when the Celery worker has a backlog - this shared dev environment's
worker was visibly behind after repeated test runs, logging one `Downscaled image N` line every few
seconds) but is a real, if minor, UX gap: a user who opens their own gallery moments after uploading
can see a broken image icon on their own new tile until the next refresh. Worth either having the
client not render/link an image URL until processing is confirmed done, or having the server keep the
old file (or redirect) until any in-flight requests for it would reasonably have completed, rather
than deleting eagerly.

**Addendum 2026-08-31**: also hits Vault Documents' lightbox preview (`<iframe>`, Batch 5's
`_setLightboxDocument`) - same race, same root cause (`upload_photo()` queues the identical
`process_image_upload` task regardless of media type), scoped out live-verifying Batch 5 the same
way (`guard.allow()` in `tests/integration/specs/ui/vault-documents.spec.ts`). Not a new instance to
fix separately; the eventual fix above covers both.

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
isolated `--grep`-scoped runs (not just within a single flaky window), and no `Image` row's stored
`image` field matches that path (`Image.objects.filter(image__icontains="S76SWO1keJAXdV")` returns
zero rows), so this looks less like the few-second rename race and more like a thumbnail job that
started, got a path assigned, and never completed or got cleaned up - or a stale reference cached
somewhere between the DB and what's served. Didn't chase further (out of scope for Batch 5, and the
`e2e-primary` account on this ephemeral dev slot is disposable), but worth a look if `vault-photos.spec.ts`
keeps failing on this specific test: check for an orphaned/stuck row in this account's photo library,
or a thumbnail-generation task that errored silently.

## P60 — `vault-photos.spec.ts`'s sort test can tie on a persistent dev DB because it relies on random captions

`id: P60` · `status: open` · `updated: 2026-08-31`

Previously titled "`vault-photos.spec.ts`'s "changing sort re-fetches the grid in the new order" test flakes on a persistent dev DB".

Found running the Vault Photos/albums Playwright specs against the `ae97b86` ephemeral dev
environment after a Batch 3 (Vault albums) fix pass - pre-existing (Batch 2), unrelated to that
batch's changes. The test asserts the grid's first tile differs between "recent uploads" and "name"
sort, on the assumption that 30+ randomly captioned seed photos won't coincidentally sort the same
way both times. Against this environment's `e2e-primary` account (71 accumulated photos from
repeated suite runs against the same persistent database, not a fresh seed), the same photo landed
first under both orderings and the test failed on both the initial attempt and the retry. Matches
the same class of problem noted in this file previously for accumulated E2E test data breaking
scroll/prune assertions - reseeding a clean, modest photo set for `e2e-primary` before this spec
runs would fix it; a sturdier version of the test would also pick two captions guaranteed not to
tie (e.g. by explicitly seeding one photo with a caption that sorts alphabetically first and a
different, more recent one) rather than relying on randomness against an unbounded, growing dataset.
mypy policy.

## P61 — Vault album bulk delete, send-to-wiki and share render hidden forever, because only a `Pin` owner gets URLs

`id: P61` · `status: open` · `updated: 2026-08-31`

Previously titled "Vault album bulk actions (delete, send-to-wiki, share) are silently unavailable".

`controllers/albums.py:513-519` sets `gallery_bulk_url`/`pin_share_dialog_url` only when the album
owner is a `Pin`; a `Profile` (vault) owner falls into the `else` and gets empty strings. Downstream,
`album-items.ts:378-379` only wires the bulk wiki/delete callbacks `if (bulkUrl)`, and
`_bulk_toolbar.html` hides any button without one - so the Delete and Send-to-wiki buttons declared
in `_album_bulk_actions` (`albums.py:543-545`) render `hidden` forever inside a vault album, as do
the equivalent right-click entries (`photo-context-menu.ts:144,146,159`).

Net effect: inside a vault album you can multi-select and add/move/remove/set-cover, but there is no
delete of any kind - you have to leave the album and use the per-tile trash button one photo at a
time. Single-photo share still works from the lightbox, so only *bulk* share is lost.

Unlike the other vault-album omissions (`move_url`, `reposition_base`, external media), which each
carry an explicit "a vault album has none" rationale in the source, this one has no comment marking
it deliberate - it reads as an oversight from widening `Pin | Wiki` to `Pin | Wiki | Profile`. Needs a
decision (wire up a profile-scoped bulk endpoint, or document the refusal) rather than a silent gap.

## P62 — Video uploads are charged to quota but appear nowhere in the Vault

`id: P62` · `status: open` · `updated: 2026-08-31`

`MediaKind.VIDEO` exists (`models/images/model.py:136`), `_resolve_media_type` classifies and
feature-gates videos (`services/photos/photo_upload.py:83-86`), and `tasks.py:913` processes them -
but `ImageQuerySet` has `photos()`/`documents()` and no `videos()` (`queryset.py:271-281`), and the
Vault has no video surface. A video uploaded through the external API
(`external_api/views.py:1303`) counts against `get_storage_totals` and the user's quota, yet
`VaultHomeView` counts only photos and documents and its recent-uploads strip chains only those two.
The user is billed for storage they cannot see, browse, or delete from the Vault.

Either add a Videos page (the third instance of the same copy-paste - see the note below) or, at
minimum, surface videos in the Vault home counts and storage explanation so the number reconciles.

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

## P64 — The integration suite's login setup fails after a successful sign-in, and `diagnose()` hides why

`id: P64` · `status: open` · `updated: 2026-08-31`

Previously titled "the integration suite's login setup fails after a *successful* sign-in".

Cost most of an hour during the Vault review, and the error message actively misleads. `auth.setup.ts`
reported `Sign-in as "e2e-primary" did not happen`, but the attached diagnosis contradicts itself:

```
The page is at https://ae97b86.dev.urbanlens.org/dashboard/map/?lat=41.361607&lng=-74.056177&zoom=13
and no longer shows the sign-in form.
page.waitForURL: Timeout 30000ms exceeded.
  navigated to "https://ae97b86.dev.urbanlens.org/dashboard/map/"
  "networkidle" event fired
```

The sign-in worked - the browser is on the map, authenticated. What timed out is
`submitCredentials`' `waitForURL((url) => !url.pathname.startsWith("/accounts/login"))`
(`lib/pages/login-page.ts:47`), raced inside a `Promise.all` against the click. The post-login map
page then rewrites its own URL client-side to append `?lat=&lng=&zoom=` (a `history` replace, not a
navigation), so under load the predicate can be evaluated either side of a state the wait never
observes. The first attempt failed differently again - `locator.allInnerTexts: Execution context was
destroyed, most likely because of a navigation` from `diagnose()` at `:99`, i.e. the *error reporter*
itself throwing while the page navigated under it, hiding the real cause.

Two separate things to fix: make the wait robust (assert on an authenticated marker in the DOM, or
`waitForURL` outside the `Promise.all` with the click awaited first), and make `diagnose()` tolerate a
navigating page so the reported reason is the real one.

Worth noting for anyone debugging this: it reproduces only when the host is loaded, and this box is
shared - load averages of 100-290 with no single hot process (heavy `kworker/kblockd` I/O wait) were
routine during this session, turning 3-second tests into 8-minute ones. Check `/proc/loadavg` before
concluding a browser failure is a code regression; a direct `curl` of the same page returning in ~1s
while Playwright times out at 30s is the tell.

## P65 — Perf tooling measures query count only, so a 12-second render passes every scaling test

`id: P65` · `status: open` · `updated: 2026-08-31`

Previously titled "perf test tooling has no wall-clock/render-time check - only query count".

Root cause behind every finding below, and worth fixing once rather than per-finding. The Organize
Labels page was reported "still slow" despite `Label.prime_total_pin_counts` already having cut its
query count from ~146 to 3 (`models/labels/model.py`, shipped in `release/v0.7.0`). Profiling it at
500 labels found the real cost: ~12s of wall time against ~0.2s of database time. The page's six tabs
(Tags/Categories/Statuses/People/Media/Display Order) switch purely client-side (a JS click handler
toggling a `hidden` attribute), but the Django view rendered **all six** tabs' full card lists on
every load regardless of which was visible - fixed in `controllers/organize.py`
(`_rows_if_active`)/`templates/dashboard/pages/organize/index.html`, see the "perf: defer Organize
page's hidden label tabs to first reveal" commit.

That bug was invisible to this codebase's entire existing performance-test suite -
`QueryScalingMixin` (`core/tests/query_scaling.py`) and `django_perf_rec` query-fingerprint records
both measure database query count/shape, never Python or template CPU time - so a page can pass every
existing scaling test at 500 rows while still taking 12 seconds to render. A site-wide static survey
(10 parallel agents, one per feature area, read-only) done immediately after found the same defect
class repeated across the app; seven confirmed instances are recorded in the entries below this one.
Two are already fixed alongside the Organize page (wiki.html/location/index.html's subnav tabs, using
`hx-trigger="load"` unconditionally instead of `"revealed"` - see `test_pin_detail_fanout_budget.py`,
which had already ratcheted this exact defect on the pin page as a known, undecided issue).

Worth building a `RenderTimeScalingMixin` sibling to `QueryScalingMixin` - same shape (seed N vs 4N
rows, assert wall time doesn't grow past a tolerance), but timing `time.perf_counter()` around the
request instead of counting queries - so this class of bug fails a test instead of shipping. None of
the entries below have one; each was found by hand.

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

## P67 — "Organize this property" fans out ~6-7 queries per candidate pin, uncapped to 500

`id: P67` · `status: open` · `updated: 2026-08-31`

Previously titled ""Organize this property" dialog fans out ~6-7 queries per candidate pin, uncapped to 500".

`controllers/pin_restructure.py:83-111` (`_nestable_rows`) calls
`services/pins/pin_merge.py:158-207`'s `plan_merge_conflicts()` once per nestable candidate with no
batching - each call does an article lookup, two `Boundary.objects.filter`, and two
`CustomFieldValue.objects.filter`, none prefetched, so N candidates cost roughly 6N-7N queries in one
request (`PinRestructureApplyView.get`/`.post`, url name `pin.restructure.apply`).
`services/pins/pin_restructure.py:222-247`'s `nestable_root_pins()` caps the list at 500, so this
isn't hypothetical: the feature's own use case is consolidating many individually-pinned buildings on
one property (a hospital/asylum/campus complex pinned building-by-building before child-pin nesting
existed) into one, which is exactly a large-candidate-count scenario. This is also brand-new code -
`_nestable_rows` shipped in the same 2026-08-30 commit (`e795f35f`) that added the "Organize this
property" dialog - so it never got the query-scaling scrutiny an older path would have picked up.
`test_pin_restructure.py` covers correctness only; no `QueryScalingMixin`/`assertNumQueries` test
covers this path.

## P68 — N+1s in the site-admin user list, the achievement icon picker and Memories > Maps still have no perf test

`id: P68` · `status: open` · `updated: 2026-08-31`

Previously titled "N+1s elsewhere with no perf-test coverage".

Found by the same survey as the entries above, each independently confirmed against its source:

- **`SiteAdminUsersView`** (`controllers/site_admin.py:1214-1265`) calls `get_quota_bytes()`,
  `get_storage_used_bytes()`, and `active_subscription_roles()` *twice* per row (the second is a
  redundant call for the `roles` context key) - up to 5 uncached queries per user, ~125 extra round
  trips at the page's own `PAGE_SIZE=25`, on a whole-site user directory that only grows.
- **Achievement admin editor** (`controllers/achievements.py:134-239`,
  `templates/dashboard/partials/admin/_achievement_rows.html:74-83,185-189`) nests a full
  `_icon_picker.html` (two `{% for %}` loops over all ~1,288 `ICON_CATEGORIES` entries,
  `models/labels/meta.py:37`) inside a `hidden` div *per achievement row*, re-rendered in full on
  every create/edit/delete/backfill via `hx-swap="outerHTML"`. ~30-60 achievements (a realistic
  near-term catalogue size) means tens of thousands of rendered icon buttons per admin page load -
  the same "hidden UI fully rendered anyway" shape as the tab-deferral entries above, just one row
  wide instead of one tab wide.
- **SpotGuessr and Trivia home pages** (`services/spotguessr/social.py:38-46`,
  `services/trivia/social.py:27-35`, both called from their respective `HomeView.get`) do 2 unbatched
  queries per friend (`friend.spotguessr_preference`/`.trivia_preference`, then a
  `PlayerModeRating`/`PlayerTriviaRating` lookup) with no `select_related`/`prefetch_related` - 2N+1
  queries for N friends on every visit to either game's home page.
- **Memories > Maps** (`controllers/memories.py:903-945`) prefetches `shared_by__user` and `items`
  but not `safety_checkins`/`comments`/`visits`/`direct_messages`; `MarkupMap.attachment`/
  `.attachments` (`models/markup/model.py:164-225`) then cost up to ~11 queries per map card, on an
  unsliced queryset - a profile that draws a route on every check-in/comment/visit (the page's own
  advertised workflow) could plausibly reach several hundred queries here.

None of the four appear in any `QueryScalingMixin` subclass or `django_perf_rec` record.

## P69 — Unbounded lists with no pagination across most of the site, from album pickers to Immich imports

`id: P69` · `status: open` · `updated: 2026-08-31`

Previously titled "unbounded lists with no pagination, found across most of the site".

Same survey, same shape each time: a collection that grows with account age/usage, rendered via a
plain `{% for %}` with no `.filter()[:N]`, `Paginator`, or HTMX-deferred/windowed loading. Grouped
here rather than one entry each since the fix is identical in kind (cap it, paginate it, or defer it)
even though the code paths are unrelated. Roughly ranked by how large the realistic ceiling is and how
heavy the per-row template is - top few are worth prioritizing, the rest are real but currently minor
at this app's beta scale (~2 users):

- **Album detail's "add existing photo" picker** (`controllers/albums.py:342`,
  `eligible_images_for()`) lists *every photo the profile has ever uploaded* across every pin/wiki/
  vault upload, in a `<dialog>` that stays closed until a client click - same "hidden-but-fully-
  rendered" shape as the tab entries above. A photographer with months/years of uploads could reach
  thousands of `<img>` tags rendered into one hidden dialog on every album page view.
- **Immich "nearby" photo import** (`controllers/immich.py:186-241`,
  `services/apis/immich/gateway.py:170-184`) fetches *every geolocated asset in the user's entire
  Immich library* (no radius param sent to Immich at all) and filters to "nearby" in Python after the
  full fetch - a self-hosted library built over years could hold 10k-100k+ assets fetched over the
  network on every picker open. Immich's own `/search/metadata` endpoint accepts lat/lng+radius and
  isn't used here, unlike this file's other two modes (`VISITS`, `ALL`), which are bounded.
- **Wiki edit history & article revision history** (`controllers/location_wiki.py:387-421`,
  `controllers/article.py:149-177`) - no slice anywhere in either chain; a long-lived, actively-edited
  community wiki or personal article could reach hundreds to low-thousands of rows. Now deferred to
  tab-reveal (see the fix above) but still unbounded once that tab is actually opened.
- **Pin-to-wiki share dialog's photo picker** (`services/wiki/wiki_share.py:199-201`,
  `seedable_photos()`) lists every photo on the pin with no cap - contrast with the wiki's own Media
  gallery (`_WIKI_PHOTOS_PREVIEW_LIMIT = 60`) and the visit dialog's photo picker (capped `[:60]` in
  `controllers/visits.py:77`), both of which already learned this lesson.
- **Vault "pin albums" panel** (`controllers/vault_photos.py:206-249`,
  `services/photos/albums.py:242-289`) loads every album and every album item across *all* of a
  profile's pins with no limit, once the (correctly lazy, `<details hx-trigger="toggle once">`)
  section is opened.
- **Settings page's Security and API-Keys tabs** (`controllers/settings.py:128-374`,
  `services/auth/api_keys.py:148-178`) are the two tabs on this page that were never converted to the
  lazy-HTMX-subsection pattern its own Connections/Billing/Undo/Notifications/Custom-Fields tabs
  already use - `security_settings_context()`/`api_keys_settings_context()` run unconditionally on
  every settings page load regardless of which of the 8 tabs is open. The API-key list itself also has
  no cap and revoked keys are never excluded, so it only grows.
- **Memories > Sharing** (`controllers/memories.py:798-890`) queries and renders both the full "sent"
  and full "received" share histories on every load, though only one is visible at a time via a
  client-side (non-HTMX) toggle.
- **Memories > Journal** (`services/memories/journal.py:57-203`) merges four unsliced sources (visits,
  reviews, comments, article edits) with no date-range or windowing at all - no "load more" of any
  kind, unlike this file's sibling paginated views.
- **Pin import-failure queue** (`controllers/pin_import_failures.py:39-134`) has no pagination, and
  directly contradicts itself: the queue view's docstring says failures are "rare," while
  `PinImportFailureGuessView`'s docstring in the *same file* says "a single import can leave hundreds
  of failures." The sibling `PinSuggestionQueueView`/`PinMergeSuggestionQueuePartialView` are both
  paginated at 12; this one apparently was not, on the wrong assumption.
- **Undo history, Safety check-ins overview, "view all friends" page, DM conversation list,
  achievement catalogue, Organize's Lists/Filters tabs, and the pin-list overview map** all follow the
  identical pattern with lower realistic ceilings or lighter per-row templates today:
  `controllers/undo.py:58-112`; `controllers/safety.py:314-431` (no auto-delete by default -
  `SafetyPreference.auto_delete_after_days` is nullable and defaults to "never"); 
  `controllers/friendship.py:451-477` (`SiteSettings.max_friends_per_user` defaults to 0/unlimited);
  `controllers/direct_messages.py:710-734` (query count already proven flat by
  `ConversationListQueryScalingTests`, but that test can't see render-time cost, and the list is
  re-fetched on nearly every DM sent anywhere in the app); `controllers/achievements.py:98-113`;
  `controllers/pin_lists.py:214-246` (also structurally invisible to `test_route_query_scaling.py`'s
  generic sweep, which hits `lists.list` without an `HX-Request` header and only ever exercises its
  redirect branch); and `controllers/pin_lists.py:155-176` (`_items_map_data` plots every matching pin
  on the overview map with no cap, unlike the near-identical `SavedFilterPreviewView`'s explicit
  `_PREVIEW_MAP_PIN_LIMIT = 500`).

## P70 — `settings/test.py` pops `PROMETHEUS_MULTIPROC_DIR` too late, so 8 metrics tests fail wherever it is set

`id: P70` · `status: open` · `updated: 2026-09-04`

`CeleryEventMetricsTests` (8 tests in `tests/hypothesis/test_metrics_endpoint.py`)
fail with `TypeError: expected str, bytes or os.PathLike object, not NoneType`
raised from `posixpath` inside `CollectorRegistry()`.

The test helper's own comment states the contract: "A per-test registry only
isolates because `settings/test.py` pops `PROMETHEUS_MULTIPROC_DIR`". That pop
is real - `settings/test.py:128` - but it happens too late.
`prometheus_client/values.py:95` reads `os.environ.get("PROMETHEUS_MULTIPROC_DIR")`
when `ValueClass` is first resolved, and latches multiprocess mode on. If
anything imports `prometheus_client` before Django settings are loaded, the
library is already in multiprocess mode by the time the pop runs, and every
later registry tries to join a path against `None`.

The failure is therefore conditional on the environment, which is why it is not
seen everywhere: it needs `PROMETHEUS_MULTIPROC_DIR` to be *set* in the
process environment. It is set in the app container
(`/var/run/urbanlens/prometheus`), so `docker exec ... pytest` - the documented
way to run tests on a host without GDAL - hits it every time, while
`bin/run_tests.sh` does not set it and so does not.

Verified pre-existing, not a regression: the same 8 tests fail identically on the
unmodified file from `HEAD` (`git show HEAD:...test_metrics_endpoint.py`, copied
into the container and run alone: `8 failed, 70 passed`).

Not fixed here because it is unrelated to the tooling cleanup that found it. The
likely fix is popping the variable before `prometheus_client` can be imported -
`src/urbanlens/conftest.py` or the settings package `__init__` - rather than in
`settings/test.py`, which runs too late to win the race.

## P71 — The Sphinx setup builds successfully and produces no API documentation at all

`id: P71` · `status: open` · `updated: 2026-09-04`

`docs/conf.py` and `docs/index.rst` exist and `sphinx-build -b html docs <out>`
reports "build succeeded", but the output contains exactly `index.html`,
`genindex.html` and `search.html`. No module pages: `index.rst` has no
`automodule` directives and nothing runs `sphinx-apidoc`, so no docstring in
`src/` is ever read. There is no `.readthedocs.yaml`, and nothing in
`.github/workflows/`, `bin/` or `package.json` builds the docs.

`sphinx` and `sphinx-rtd-theme` are dev dependencies paying for this.

Why it matters beyond the wasted config: the Google-docstring completeness
standard is justified in `CLAUDE.md` by "Sphinx consumes them", and that
pipeline does not exist. The standard is still worth keeping - a complete
docstring is worth writing for the next reader regardless - but it should not
rest on a claim that is checkably false.

Two ways out, neither attempted here because both are product decisions: wire it
up (`sphinx-apidoc`, `myst-parser` for the `.md` files, a CI job) or delete
`docs/conf.py` and `docs/index.rst` and drop the two dependencies.

## P72 — `bun run typecheck` reads 87 TypeScript files fewer than the pre-commit hook fires on

`id: P72` · `status: open` · `updated: 2026-09-04`

`tsconfig.json` sets `include: ["src/urbanlens/dashboard/frontend/ts/**/*.ts"]`.
`git ls-files '*.ts' | grep -vc '^src/urbanlens/dashboard/frontend/ts/'` returns
87 - `frontend/browser/`, `tests/integration/` and the rest are outside the
project and are never checked.

The manual `tsc` hook in `.pre-commit-config.yaml` matches `\.tsx?$`, so editing
any of those 87 files triggers a typecheck that then does not look at the file
that triggered it. Passing means nothing about the change.

Not fixed here because `frontend/browser/` has never been typechecked, so
widening `include` will surface a first wave of real errors that wants its own
pass rather than being folded into a tooling cleanup.

