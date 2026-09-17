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

## P5 — Dialog forms still post every field; edit handlers write only the columns that changed, but submits are not dirty-only

`id: P5` · `status: open` · `updated: 2026-09-15`

Previously titled "Dialog forms post every field and handlers save every column, so untouched values
overwrite and re-attribute", before that "forms submit and save every field, not the ones that changed".

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

Two directions, and they compose: make submits dirty-only (the client knows what it prefilled, so
it can send only what differs), and make writes field-scoped. The second is the safety net for
anything that still posts everything, and is the cheaper half to finish first.

### Field-scoped writes, as of 2026-09-15

The 2026-08-25 count (28 files posting a full `FormData`, 23 bare `.save()` calls in `controllers/`,
17 `form.save()` calls) was re-read call by call. The `form.save()` count overstated the gap: the
settings page's fifteen section forms all subclass `forms/settings_form.py::ProfileSettingsForm`,
whose `save` already writes only `Meta.fields`, and `test_settings_form_field_scope.py` guards it.

`models/abstract/field_snapshot.py::FieldSnapshot` records a loaded row's column values, and its
`save_changes` writes only the columns that differ plus `updated`, or nothing at all.
`test_partial_edits_keep_concurrent_writes.py` lands a second writer's change between the load
and the save, and it failed at each of these before they used `FieldSnapshot`:

| handler | why it mattered |
|---|---|
| `controllers/site_admin.py::SiteAdminView.post` | the page autosaves one field per request, and several fields are reassigned from their loaded value whether or not they were sent, so two quick autosaves reverted each other |
| `controllers/markup.py::MarkupEditView.post` | a relabel reverted another editor's colour or geometry on a shared wiki's annotation |
| `controllers/custom_layers.py::CustomLayerEditView.post` | a rename reverted a visibility toggle |
| `controllers/detail_pins.py::DetailPinEditView.post` | the detail panel autosaves style changes, and each one reverted notes written in another tab |
| `controllers/detail_pins.py::LocationWikiDetailPinEditView.post` | a restyle of a child wiki reverted another editor's description |
| `controllers/site_admin.py::SiteAdminApiLimitsView.post` | saving a service's limits reverted the `last_call_at` the rate limiter writes on every call |
| `controllers/boundary.py::WikiBoundaryView.post` | redrawing a community boundary reverted a generated boundary that landed meanwhile |
| `controllers/site_admin_costs.py` (both edit views) | an edit reverted another admin's change to a field the form carried unchanged |
| `controllers/notifications.py::NotificationPreferencesView.post` | the form carries every preference, so a save in one tab reverted a change made in another |

`Pin.save` and `Wiki.save` both key their side effects on `update_fields`, and both still fire: a
move carries `location`, so `Pin._sync_exposures_after_save` propagates exposures, and a rename
carries `name`, so aliases are synced. One behaviour is gone on purpose: `Wiki.save` backfills an
unset `place` only on a whole-row save, so a child wiki's style edit no longer does that as a side
effect.

**Still writing every column on an edit of an existing row:** `controllers/custom_fields.py`'s
value save, on purpose. `CustomFieldValue.set_value` clears every typed column and sets one, so the
row's columns are a single value between them; a scoped write could keep a concurrent writer's
column beside the new one. The remaining bare saves in `controllers/` create new rows, where there
is nothing to revert.

What is left of this entry is the other direction: dirty-only submits, so a client stops asserting
values the user never touched.

A scoped write has one trap of its own, fixed in `models/abstract/model.py::PublicDashboardModel.save`:
a row with no slug generates one on save, and a save naming its `update_fields` used to drop that
slug, so it was regenerated on every later save and never stored (`test_slug_generated_on_scoped_save.py`).
`FieldSnapshot` only deep-copies JSON containers, so a model with a file field is safe to snapshot.

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

## P7 — REData's reconciled building `ref` has no stability guarantee, and UrbanLens persists it as permanent identity

`id: P7` · `status: open` · `updated: 2026-09-15`

Previously titled "performance and ops defects found but not fixed", then "nginx pins its app upstream at
config load and REData's `ref` is stored as permanent identity" - both those halves, and the rest of the
2026-08-19 sweep this entry recorded, are fixed. That history moved to `archive/PROBLEMS-ARCHIVE.md`
(2026-09-15). One item remains:

**The reconciled `ref` is persisted as a permanent identity** (`Place.provider_key`, floorplan
`building_ref`), and REData does not guarantee it is stable across responses. Found while fixing
`ensure_building_places`'s `parent_ref` handling (2026-08-19): `find_matching_place`'s
mutual-centroid-containment fallback applied even to records that *do* carry a stable provider id, so
an L-shaped block and a wing tucked into its corner could merge back into one place - undoing exactly
the reconciliation REData did to keep them apart. Fixed for that specific case, but the underlying
assumption - that a `ref` never changes - is still unverified against REData.

## P9 — REData's `?limit=` param is inert client-side, and land-use-area boundary geometry needs a map-overlay decision

`id: P9` · `status: open` · `updated: 2026-09-15`

Previously titled "REData gaps: mostly closed 2026-09-08", and before that "REData consumption gaps
left after this session's sweep".

A full cross-repo sweep of UrbanLens's REData integration (2026-08-19, expanded 2026-09-08) found and
fixed everything that was *wrong*, and built everything worth building except these two, which need a
decision this session isn't the one to make. The sweep's own changelog moved to
`archive/PROBLEMS-ARCHIVE.md` (2026-09-15).

**`?limit=` is still inert on every REData near-point endpoint** (unchanged - not re-verified this
session). `NearPointQuery` (`REData/src/redata/api/coordinates.py`) parses `lat`/`lng`/
`radius_meters`/`provider`/`force_refresh` and nothing else; the `limit` parsing at :411 belongs to
the *text*-query parser. So every panel that passes `limit=20`/`25`/`30`/`50` caches up to REData's
own server-side cap instead. Not fixed here on purpose: trimming client-side would change the
user-visible counts panels report ("N mapped within 250 m") from REData's floor to our own
arbitrary bound, which is less accurate, not more. The fix belongs in REData - have
`parse_near_point_query` accept `limit` - after which the UrbanLens side needs no change at all.

**Land-use-areas' boundary geometry is still not rendered, on purpose.** The category chips already
shown on the Property Records card come from a different, already-consumed field; rendering the
actual polygon needs a map-overlay UX decision (a new layer? a toggle? which existing
boundary-rendering chain, if any) that wasn't the sweep's to make. Flagging for product input rather
than guessing at it.

## P11 — Frontend TS audit: a few correctness bullets and structural debt found but not fixed

`id: P11` · `status: open` · `updated: 2026-09-15`

Previously titled "84 raw `fetch()` calls bypass `fetch-json.ts`, and 'all the wrappers are gone' was
a count, not a search", and before that "~40 raw `fetch()` calls bypass `fetch-json.ts` and fail
silently; Organize's Media tab is unwired dead UI".

Full-tree audit of `dashboard/frontend/ts/` (every file read, eight passes, starting 2026-08-15). The
`fetch()`-wrapper migration is done - `shared/fetch-json.ts` (`fetchJson`/`sendJson`,
`fetchText`/`sendForText`) now covers every call site except `webauthn-client.ts` and the two E2EE
calls that need raw `Response` semantics (201-vs-200, `redirected`) - and most correctness bullets
this audit found are fixed. That history moved to `archive/PROBLEMS-ARCHIVE.md` (2026-09-15). What's
left:

**Correctness, user-visible:**

- `entries/map-annotations.ts:2047` `placeMediaItemAt` still has no loading indicator for the
  server-side image-materialize step it waits on - not attempted since
  `window.mediaApplyMaterializedDrop`'s own contract (defined in the gallery/organize module) would
  need to be understood first, and this file has no established loading-state convention for the
  drag-and-drop-onto-map interaction to reuse.
- `entries/photo-location-scan.ts` - the photo uploads that run after the "Uploaded" toast still have
  no progress indicator (not re-verified this session; the controller-reuse and cross-scan
  double-counting this bullet used to describe are fixed - see `beginScanState`).
- `shared/markup-toolbar.ts:748` `flushMarkupAutoSave` - nothing flushes a pending autosave on
  unload/tab-close. A `beforeunload` handler can't reliably await an in-flight `fetch`, and this
  app's CSRF header doesn't fit `navigator.sendBeacon`'s simple-request shape, so this needs its own
  dedicated pass.
- `entries/organize.ts:106,311` - the Media tab is **fully dead UI**: the template renders it
  selectable with checkboxes, a filter bar and Edit buttons, but no `OrgTabManager` is built for it,
  `ORG_FILTER_NAMESPACES`/`TAB_FILTER_NS` omit it, and the consolidated dialog opener has no
  `media-label-edit-dialog-body` case, so Edit swaps a form into a dialog nothing opens.
- `entries/spotguessr.ts:840` `reportRoundTimeout` has no error handling, so a failed timeout POST
  hangs the round forever. (**Correction:** this bullet used to also say all three games silently
  null the WebSocket on close with no reconnect - that's fixed: `shared/live-socket.ts` now gives all
  three games heartbeat + backoff reconnect.)
- `shared/organize-priority.ts:69` - "no save sequencing" is still open: each save POSTs the *whole*
  order rather than a delta, so chaining saves one-at-a-time interacts badly with the rollback this
  audit already added - if an earlier queued save fails and reverts to its pre-drag order, a later
  save that already succeeded would re-persist the stale order on its own turn in the chain. Needs
  either a monotonic version per save (reject/ignore a write older than what the server has) or
  reworking rollback to fall through to the next known order - real design work, not a quick addition.
- `entries/article-wysiwyg.ts:532` - the first WYSIWYG keystroke re-serializes the whole article
  through a lossy `tiptap-markdown` parse (`html: false`), rewriting content document-wide, not just
  at the edit point. Needs round-trip tests over real saved articles before it is trusted.
- `shared/e2ee-client.ts:238` - the `e2ee-busy` class it sets during login has no CSS rule anywhere,
  so the ~1s synchronous Argon2id derivation shows no indicator at all; the unlock dialog (:682) has
  no busy state either, while the reset dialog next to it does it correctly.
- `shared/e2ee-client.ts:1326` - retry storm: a thread with an unreadable key re-fetches the same
  conversation/group key once per message (50 sequential identical failing requests on a 50-message
  thread). `:1459 decryptDom` also strips `data-e2ee-*` *before* attempting decryption, so a
  transient failure is permanently unrecoverable on WS-appended messages.

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

The inline template `<script>` this audit's scope excluded is tracked separately under P34, with a
fresher count.

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

## P14 — Historical `pin_images/` files whose Image row is gone are never removed (disk only)

`id: P14` · `status: open` · `updated: 2026-09-15`

Previously titled "Media gate residue: replaced or deleted pin and label icons strand their files,
and historical orphans remain", before that "Media gate residue: icons are owner-scoped now;
stranded icon files and safety check-in photo audiences remain", before that "Custom pin and label
icons are readable by any authenticated user; narrowing that needs a pin-visibility query nothing
has", before that "Authenticated media gate - residual per-family risk (2026-07-23)".

What is left is a disk-usage question. Nothing here is a disclosure route any more.

**The gate side is closed.** `dashboard.controllers.media.MediaGateView` is default-deny, with the
policy in `dashboard/services/media/access.py` as a registry keyed by `upload_to` prefix, and
`check_media_authorizers` (`dashboard.E002`) refuses to start when a file field stores under an
unregistered directory. An unresolvable file or unknown family is refused. Photo paths are
unguessable. Pin and label icons are served only to the owner of a row using the file, or to
everyone for a global label (`test_custom_icon_media_gate.py`). Safety check-in photos reach only
the check-in's audience (`test_media_gate.py::SafetyCheckinMediaGateTests`,
`SafetyContactTokenPhotoTests`). `avatars/` and `achievement_icons/` stay open to any signed-in
account on purpose, since both render on other members' pages.

**Icon, avatar and comment image files do not strand.** `services/media/file_cleanup.py` deletes
an `Achievement.custom_icon` or `Profile.avatar` file after the save that replaced it or the delete
that removed its row. Pin and label icons are left out of it, because undo restores those rows by
the stored name. `services/media/stored_field.py::sweep_unnamed_files` (P119, hourly beat) removes
any file in `avatars/`, the three icon directories and `comment_images/` that no file field naming
that directory holds, once it is older than the Celery hard time limit and no undo record inside the
retention window mentions it. That covers a replaced or deleted pin or label icon after its undo
expires, and every historical orphan in those directories
(`test_held_upload_recovery.py::AShownFileNothingNamesTests`).

**Still open: `pin_images/`.** `services/media/images.py::delete_stored_file`, run for every deleted
`Image` row by `models/images/signals.py::remove_stored_file`, removes the original and its three
derived files when no other row names them. Nothing sweeps what was stranded before those paths
were fixed on 2026-09-14: every deleted row's analysis thumbnail, and the derived files of an
original another row still shared. The gate refuses those files, because no `Image` row matches
them.

They cannot just be added to `SWEPT_FIELDS`. That sweep lists one flat directory and skips fields
with a callable `upload_to`. `Image` files sit under nested `<bucket>/<token>/` paths in
`pin_images/`, `pin_images/thumbs/`, `pin_images/markers/` and `pin_images/analysis/`, and whether
a file is named depends on all four columns. A sweep there needs a recursive walk (a paginated
prefix listing on S3) and a name set covering every `Image` row. That is a job sized to the table,
not an hourly one.

**Suggested next step**: count the files under `pin_images/` that none of the four columns names,
on production, before deciding whether the disk is worth a one-off sweep.

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

`id: P16` · `status: open` · `updated: 2026-09-15`

Previously titled "aliases/labels aggregation, and boundary voting". **The boundary-voting half
shipped 2026-07-30** - after this entry's last update, so it sat here as "not started at all" long
after it wasn't: `models/boundary_vote/model.py`, `services/geo/boundary_voting.py`
(recency-weighted, tie-break rules), `controllers/location_wiki.py::BoundaryVoteView`, a wired UI
dialog, and two hypothesis test files. Only the aggregation gap below is still open.

The ROADMAP's "Pin Restructure" section also asks for this, deliberately not attempted as a rider on
other work:

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

---

## P19 — Audit re-verification's residual gaps: a 1,100-line `_dark.scss`, a stub AI gateway, and a few maintainability gaps

`id: P19` · `status: open` · `updated: 2026-09-15`

Previously titled "Full-codebase audit: re-verification pass (2026-07-25)".

Six independent re-verification passes re-read every finding in `docs/audits/codebase-audit.md`
against the current code (not trusting the earlier session's own claims) and reported per-finding
FIXED/PARTIALLY-FIXED/NOT-FIXED/REGRESSED verdicts. Most findings held up as genuinely fixed, and
the regressions/gaps this pass surfaced were fixed directly in it - that changelog moved to
`archive/PROBLEMS-ARCHIVE.md` (2026-09-15). What's left, verified against the current code rather
than trusted from the original audit text:

- `services/messaging/direct_messages.py`'s TOCTOU fix only covers the DM email/text debounce; the
  underlying **`quota_error_for_upload`/`per_profile_upload_lock` pattern itself is a "soft" lock**
  (proceeds without the lock if it can't be acquired promptly) - fine for its stated purpose but
  worth remembering it's not a hard guarantee.
- **Unit 09/10**: bulk-accept/reject's per-item failures still aren't surfaced in the frontend
  toast; both trip-invite paths and calendar-push still loop per-invitee/per-activity without
  batching or debounce; `TripActivity.order` still has no uniqueness constraint or locking.
- **Unit 13/14/19**: `NotificationPreference` still only models 12 of 30 `NotificationType` values;
  no admin can see/revoke another admin's subscription grants; no restore tooling exists for the
  Postgres backups. (The `labels.py` `ai_kind_enabled`/`keyword_kind_enabled` `.get`/`.post`
  duplication this bullet used to list is fixed - consolidated into `AutoTagService`.)
- **Unit 20**: `services/ai/huggingface.py` is still an unwired, `NotImplementedError`-raising stub
  (documented as such). `PinSerializer.create()` and `parse_for_preview`'s blocking-AI-call half of
  this finding is fixed - both are async now.
- **Unit 21/22/23**: `models/pin/viewset.py`'s post-`get_object()` ownership re-check is kept
  deliberately (2026-09-06) - unreachable today since `get_queryset` scopes to `profile__user` and a
  stranger's pin 404s first, but a backstop for the day that filter widens (shared pins, an admin
  view); both sites carry a comment saying so. `GroupMessage` still carries no
  images/markup_map/location_mentions/reply_to fields. (`GameSessionConsumer`/`TriviaSessionConsumer`
  now share a base class with per-connection rate limiting - struck from this list.)
- **Unit 24/25**: no moderation UI exists for AI-flagged trivia questions (decided against, not just
  unbuilt - see `docs/designs/drafts/trivia.md`'s "Known gaps"); SpotGuessr still has no
  leave/cancel/kick path once a lobby exists (Trivia gained one 2026-07-25).
- **Unit 31**: `_dark.scss` is still ~1100 lines of per-selector overrides; `_pin_lists.scss` still
  has 3 sibling raw-hex danger-red controls without dark overrides (`.pin-list-more-menu-danger`,
  `.saved-filter-delete-btn`, and its hover state).
- **Unit 34**: only ~30/111 `@given`-using test files import the shared `strategies.py` module (up
  from 8/97, but still a minority); `test_trivia_wiki_incorporation.py` has zero `@given` tests
  despite an obvious property-testing candidate (the upvote-count threshold logic).

All of the above are maintainability/completeness gaps, not active security or correctness bugs.

## P20 — The legacy-CID repair leaves the CID on the wrong `Location`, so `by_cid()` resolves it wrongly for everyone

`id: P20` · `status: open` · `updated: 2026-09-14`

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

2. **`GoogleMapsGateway.import_pins_streaming` survives its route.** It still places CID pins from
   `extract_coordinates_from_url`'s S2 decode. The `pin.upload.takeout` route that reached it was removed as a superseded
   duplicate (P35, 2026-08-14), and on 2026-09-14 nothing outside `tests/` calls the method
   (`grep -rn import_pins_streaming src/ --include=*.py`), so it can no longer misplace a pin. It is ~280 lines of dead
   code (`services/apis/locations/google/maps.py:583`) held up by `test_import_pins_streaming.py` and one case in
   `test_label_style_suggestions.py`.

   Not deleted, because it is the only production caller of `services/labels/style_suggestions.suggest_label_style` -
   the AI-chosen icon and colour for a tag made from an imported file's name. The live import
   (`iter_confirmed_import_events`) creates a category from the file stem without it. Deleting the method retires that
   feature; keeping the feature means moving the call into the confirmed path. That is the decision left here.

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

## P34 — Two of the five biggest inline-JS templates are now cacheable files; ~96 templates and the duplicated escaping helpers are not

`id: P34` · `status: open` · `updated: 2026-09-16` · `partially addressed 2026-09-16, see X21`

Previously titled "22,636 lines of inline template JS sit outside every automated check, with
duplicated escaping helpers", and before that "Inline template JS: 21,543 lines, 14 escaping
helpers, zero test coverage".

Measured 2026-08-14: `dashboard/templates/` held **21,543 lines of inline JavaScript across 101
templates**, versus 22,684 lines in `frontend/ts/` which `tsc --noEmit` and 394 bun tests cover.
**The 101-template, 21,543-line total was not re-measured this session** - only the two rows below
were, by `git show <commit> --numstat` against the two extraction commits.

Concentration, as measured 2026-08-14 (top 5 = 49% of the then-total):

| lines (2026-08-14) | template | 2026-09-16 |
|---|---|---|
| 5,175 | `pages/map/index.html` | **done** - `git show 23a861765 --numstat` removed 5,389 lines to `frontend/static/js/map-page.js`; 33 lines of page wiring remain |
| 1,772 | `pages/messages/index.html` | unmoved |
| 1,377 | `pages/trips/detail.html` | unmoved |
| 1,294 | `pages/location/index.html` | unmoved |
| 1,118 | `themes/base.html` | **partly done** - `git show 3c924327e --numstat` removed 957 lines (the comment-map/image-attachment composer) to `frontend/static/js/comment-map.js`; 242 lines remain (an `html-root` flash-of-theme IIFE, a hotkeys JSON block, passwordless-account wiring) |

The concrete cost, beyond "untested": 44 function names are defined in more than one template,
including **14 HTML-escaping helpers under 9 names**, of which 6 escape `&<>` only and 8 also
escape quotes. Nothing in any of the names distinguishes the text-node case from the attribute
case, and the 2026-08-14 audit found two real bugs that existed precisely because the wrong one
was in reach (`memories/index.html`, `map/index.html`). Not re-checked this session - neither
extraction touched an escaping helper.

**Suggested-order-of-work item 1 is done.** See X21 for the move and the two defects the mechanical
extraction introduced along the way (a template tag inside a quoted string; two extracted files
silently colliding on one top-level `const CFG`), both now caught by
`src/urbanlens/core/tests/inline_scripts.py`. Remaining, largest payoff first:

1. `frontend/ts/shared/escaping.ts` exporting `escapeText` and `escapeAttr` (names that say which
   context they are for), with migrated code importing it rather than redefining it.
2. `pages/messages/index.html`, `pages/trips/detail.html`, `pages/location/index.html` - the next
   three largest as of 2026-08-14, all still untouched.
3. The remaining templates below the top 5, and `themes/base.html`'s leftover 242 lines.

Extraction to a file is necessary but not sufficient for P92's TypeScript-checked-bundle goal:
X21's two moves produced plain `.js` files fed by a JSON config element, not `tsc`-checked bundled
entries, so a script still cannot `import` from `frontend/ts/`. This is still a large job and
nothing above is urgent in isolation. It is recorded because every future bug of this shape in
these files is invisible to CI, and because the duplication means fixing one instance fixes
nothing else.

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
  unreachable, and only `test_query_scaling.py:77` names it.
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

## P37 — A 2026-08-14 coverage run found 100 write handlers no test executed; its top roster is tested now, the rest are unmeasured

`id: P37` · `status: open` · `updated: 2026-09-14`

Previously titled "A 2026-08-14 coverage run found 100 write handlers no test executed; all but one
of its top roster are tested now", before that "100 write handlers totalling 1,217 statements never execute under the test suite",
and before that "1,217 statements of write handlers that no test executes".

Measured 2026-08-14 with `coverage.py` over the full suite; full list in
`docs/reports/2026-08-14-view-coverage.md`.

The view layer is 80% covered by statement, which sounds healthy. The shape underneath is less so:
**208 of 1,795 callables never execute**, and **100 of those are `post`/`delete`/`put`/`patch`
handlers totalling 1,217 statements**. Half of the unexercised view code is code that mutates data.

Caveats worth keeping attached to this number: coverage measures execution, not correctness, and
the run was scoped to `controllers/` and `external_api/`, so a service called by an uncovered
handler may itself be well tested.

### The highest-risk roster, as of 2026-09-14

The entry ranked its uncovered handlers by risk. Each was re-checked by searching the tests for a
request to its route, not by re-running coverage, so the other ~90 handlers are not re-measured.

| handler [statements] | now |
|---|---|
| `controllers/labels.py::LabelBulkConvertView.post` [36] | exercised: `test_label_bulk_convert_conflict.py` runs a real convert, and `test_every_stored_user_image_is_reencoded.py` runs one alongside an icon publish |
| `controllers/labels.py::LabelBulkEditView.post` [33] | `test_label_bulk_edit_applies.py` (2026-09-14): only the fields sent are written, other profiles' labels, other kinds and protected statuses are untouched, parents and children are added, and a cycle is refused. Before it, only the ceiling tests reached this handler, and they are refused before any write |
| `controllers/site_admin.py::SiteAdminUsersView.post` [35] | exercised by `test_site_admin_user_deletion.py` and `test_request_id_lookups.py` |
| `controllers/detail_pins.py::LocationWikiDetailPinEditView.post` [34] | `test_wiki_detail_pin_edit.py` (2026-09-14): the visibility gate, a child of another wiki, style fields sent and kept, a move and its `WikiEdit`, and a refused move that saves nothing. The `delete` verb was already covered by `test_undo.py` and `test_quota_rewards.py` |
| `controllers/visit_suggestions.py::VisitSuggestionRespondView.post` [31] | exercised by `test_notification_inbox_dismissal.py` |
| `controllers/calendar_sync.py::CalendarImportView.post` [30] | `test_calendar_import_view.py` |
| `controllers/albums.py::AlbumEditView.post` [31] | exercised by `test_wiki_albums.py` since `01e1b5988` |
| `controllers/pin.py::PinController.upload_takeout` [39] | deleted, along with its route |
| `controllers/consensus.py::ConsensusPhotoUploadView.post` [31] | `test_consensus_photo_upload_view.py` (2026-09-14): the alpha gate, a non-participant, an unaccepted invite, a round from another session, no file, a non-image, the quota and a duplicate checksum each store nothing and award nothing; a success lands on the round's wiki, queues processing once and awards the bonus |

"Exercised" means a test sends a request that reaches the handler. It does not mean every branch is
asserted. `docs/reports/2026-08-14-view-coverage.md` (X12) stays as the dated measurement.

---

## P41 — The queryset API's unused half, by call graph: 29 methods deleted, 27 test-only ones left

`id: P41` · `status: open` · `updated: 2026-09-14`

Previously titled "The queryset API's unused half, by call graph: 26 methods deleted, 27 test-only
ones left", before that "68 of 249 public queryset methods have no production caller, so their logic may
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
`services/places/splits.py:86` and `models/place/queryset.py:156`, but both of those filter on a
*specific* parent (`parent_id=place.pk`, `aggregate.children`), while the methods mean "any place
whose parent edge is PART_OF". Routing either call site through the method would have added a
redundant `parent__isnull=False` to make a worse fit look like reuse. `LocationQuerySet.in_domain_of`
is the same shape against a migration, which must not call a queryset method at all.

**Still open, in rough order of how much judgement each needs:**

- **27 called only from tests.** The largest remaining group and the one this entry's earlier
  warning is about: several are the `filter_by_criteria`-style aggregators' building blocks, and a
  test that exercises the aggregator does reach them - just not by name. `by_name`, `by_priority`,
  `by_tag`, `rated`, `rated_over`, `rated_under` and `overlapping` on `PinQuerySet` are the bulk.
- **Names that appear only in a string or a template: none left in production.** Re-run 2026-09-14
  by AST over `src/`, `bin/` and every template, the group was 8, not 9. Three had no reference
  but a string collision and were deleted: `ApiCallLogQuerySet.successful` (htmx's
  `event.detail.successful`), `ApiCallLogQuerySet.service_disabled` (the enrichment skip reason
  `"service_disabled"`) and `StripeWebhookEventQuerySet.unprocessed` (`held_upload.py`'s
  `HELD_PREFIX`). No call site had written their filters out by hand. The other five -
  `LocationManager.get_for_point`, `VisitQuerySet.manual`, `PlaceExternalTagQuerySet.matching`,
  `ApiCallLogQuerySet.rate_limited` and `PinQuerySet.rated` - are called from tests, so they belong
  to the group above.
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

## P49 — Doc citations drift silently, and CI's past-end check is red on 92 citations in dated records

`id: P49` · `status: open` · `updated: 2026-09-14`

Previously titled "Doc citations drift silently", before that "Doc citations drift silently, and a pin-suggestion race can still duplicate a row",
before that "`npm run git-squash` is a force-deploy with none of `deploy.sh`'s dirty-tree guards", and
before that "... none of `deploy.sh`'s guards (minor)". The git-squash and pin-suggestion items are
fixed; the documentation-citation item below is what is left.

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

### Pin suggestion ingest was a check-then-act (noted 2026-08-17) - fixed 2026-09-14

`services/pins/pin_suggestions.py`'s `ingest_location_hits` read a profile's pending suggestions, then
created or extended them, with no lock. Two concurrent ingests for one profile (a repeated Immich sweep
overlapping a local-scan upload) could both miss the read and both create a pending suggestion, or both
extend one row and each save over the other's merged `visit_dates`, `sample_assets`, aliases and links.
`hit_count` alone had been made atomic with `F()` on 2026-08-25.

The whole ingest now runs in a transaction that first takes `pg_advisory_xact_lock` on a per-profile key,
so a second ingest for the same profile waits for the first to commit; other profiles are unaffected.
`test_pin_suggestion_ingest_races.py` holds two threads between the check and the write: before the
change a new place and a matched pin each got 2 suggestions and a merged date was lost, and after it
all three pass. The callers are one Immich sweep per profile, one local-scan upload request, and one
external-API hit, so the lock is held for one bounded batch.

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
uniquely) are fixed, and `check_doc_line_refs.py` runs in CI.

**It does not keep past-end-of-file citations at zero, and CI's step is red** (found 2026-09-14). It
reported 95, and its pre-commit hook is `stages: [manual]`, so no commit ever ran it. Five were in
`PROBLEMS.md`: four are renumbered to where the code sits now, and the fifth was a frame in a pasted
traceback, which the check now skips along with everything else inside a fenced block, as quoted
output (two more such frames were in the archive). The other 92 are prose in dated records - 65 in
`designs/`, 15 in `archive/`, and the rest in `audits/`, `notes/`, `reports/` and one handoff.

That is the conflict the next paragraphs describe from the other side. The check treats a citation
nobody can follow as broken wherever it is, struck text included, while the argument below says a
dated record should keep the line it was written against. One has to give: exempt the dated
directories from the past-end check, or rewrite each of those citations to drop its line number or
name the commit it was read at. That is a choice about what those records are for, so it is left
open, and CI stays red until it is made.

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

## P57 — The test-quality audit's follow-ups: all done but one owner decision

`id: P57` · `status: open` · `updated: 2026-09-14`

Previously titled "The test-quality audit's follow-ups: 15 done; one untested surface and two decisions remain", and before that "Test-quality audit follow-ups (2026-08-29)".

Found while auditing existing unit tests for real positive/negative coverage (see
`docs/notes/test-quality-audit.md`); out of scope for a test-file-only pass, noted here per
convention rather than fixed inline.

**Thirteen were fixed by 2026-09-06** - among them the `connect_ex` guard (which turned out to be two holes), the
`make_cache_key` collision, the hard-delete overlap lock, the `SubscriptionRole.clean()` gap, and
`PinAliasView.post` (same-day, 2026-08-29). Each is struck through below with what the fix found. The
other two, the webhook-event row lock and the sweep path's ledger lock, were proven under real threads
on 2026-09-14 and needed no fix.

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

`CalendarImportView`, the carousel's "no imagery available" branch and the multi-level nesting prefix
are covered as of 2026-09-14, and both stale-documentation items are settled; none of it found a defect.

What remains: **one decision** for whoever owns Location/address display - whether the trailing comma
`Location.address` leaves after a route-only address is intended. Every other item below is fixed, covered or refuted.

Worth noting about this entry's own hit rate: it filed the AI trip tools as tidy-up ("duplicated
business logic ... can silently drift"), and they were a live permission bypass. Two of the five
"untested surface" items covered so far turned out the same way. An entry that says only "this is untested" is not
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

~~**Sweep-path locking on `advance_usage_ledger` has no real-concurrency coverage.**~~ **Covered
2026-09-14** in `test_billing_ledger_sweep_lock.py`. `test_billing_ledger_lock.py` reaches
`advance_usage_ledger` only through `apply_payment`, whose outer lock already holds the row; the
daily sweep (`advance_pwyw_usage_ledgers`) calls it directly, where its own lock is the only one.
The new test pauses the sweep inside the ledger - after its locked read, before its save, by holding
its first `role_pwyw_threshold_cents` call - while a payment on a second connection tries to land.
It asserts the payment had not finished when the pause ended, which is the lock holding it off, and
that coverage ends at 60 days rather than the 30 a stale save would rewind it to. Because the pause
sits after the read, it would also fail a version that kept the re-read but dropped
`select_for_update`. That is argued from the ordering, not observed: no mutant was run, per the audit's
rule against mutating production code. Under correct locking the test always waits out the pause, one
second.

~~**`SubscriptionRole.clean()` doesn't validate `pwyw_minimum_cents` requires `pay_what_you_want`.**~~
**Fixed 2026-09-06**, as the symmetric half of the `pwyw_dynamic_threshold` rule beside it. `0`/`None`
stays valid - that is "unset", not "set to nothing". The original text follows.

**Was:**
`clean()` (`src/urbanlens/dashboard/models/subscriptions/model.py`) only ties
`pwyw_dynamic_threshold` back to `pay_what_you_want`; it never checks that a nonzero
`pwyw_minimum_cents` is meaningless when `pay_what_you_want=False`. An admin can save a role with a
static minimum pledge set but pay-what-you-want turned off, and `clean()` raises nothing - the
field is simply inert.

**Webhook-event row lock: covered 2026-09-14.** `StripeWebhookView.post` takes
`StripeWebhookEvent.objects.select_for_update()` so two concurrent deliveries of one event id cannot both read
`processed_at` as null and both credit the payment, but every test of the view ran on one connection, where the lock
never contends. `test_billing_webhook_event_lock.py` posts two deliveries from separate threads and connections, through
the real handler and ledger, with `handle_event` held until the other delivery arrives: one event credits 1000 once, and
two distinct events credit 400 and 700 together. The distinct pair is what shows both threads do reach the handler at
once when nothing stops them. The test was not run against a view with the lock removed, since that means editing
production code to prove a test; the defect it guards against was never present.

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

~~**Stale `update_or_create`/`auto_now` rationale in boundary voting docs.**~~ **Already gone,
checked 2026-09-14.** Neither `services/geo/boundary_voting.py` nor `test_boundary_vote_recency.py`
still explains the refresh through `update_fields`. The entry's reading of Django holds: 6.0.6's
`update_or_create` adds every field with a custom `pre_save` to `update_fields` itself, so
`updated` needs no mention in `defaults`.

~~**Stale "draft wiki" language around the building-mirror path.**~~ **Fixed 2026-09-14.** There is
no draft state: `Wiki` has no `officially_created` field (it survives only in old migrations), and
`get_or_create_for_location` is the one creation path. Four places still described the retired
concept and are rewritten - `pin_restructure.mirror_buildings_to_wiki`'s comment,
`concealment.py`'s naming comment (which cited two functions that do not exist),
`docs/LOCATION_DATA_TESTS.md`, and `wiki_share.share_from_pin`'s docstring. That last one was
wrong about behaviour rather than names: it said chosen fields were ignored once a wiki was
"official", when they are recorded as the sharer's stat votes on every share. `test_building_wiki_mirror.py`'s
docstring had already been corrected.

~~**`CalendarImportView` has no test coverage at all.**~~ **Covered 2026-09-14** in
`test_calendar_import_view.py`: the no-account dialog and 400, blank `event_ids`, the per-event
`create_activity_<id>`/`invite_<id>`/`auto_sync_<id>` parsing (including the digit-only invite
filter, which drops `-3`), one real import through a mocked gateway, the toast wording for
invitations and for one or several skips, and both failure branches - an expired grant deletes the
account, a gateway failure keeps it, and neither shows the upstream error text. The view was correct.

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

~~**Missing coverage for the carousel "no imagery available" branch.**~~ **Covered 2026-09-14** in
`test_carousel_single_slide_arrows.py`: with no slides, both `street_view.html` and
`satellite_view.html` render `view-unavailable` with the caller's `error`, or their own default
message without one, and no slide or arrow markup.

~~**Multi-level pin/wiki nesting prefix is undocumented and untested.**~~ **Covered and documented
2026-09-14.** `test_child_slugs.py` now pins it for pins and wikis: `Boiler Room` under
`hrsh-powerhouse` is `powerhouse-boiler-room`, and `ph-bldg-boiler-room` when the parent has that
alias. `docs/NOTES.md` records it. Shallow prefixing follows from the prefix's 3-8 character bound
rather than being a choice made separately from it - a parent slug that already carries a prefix
is almost always longer than 8, so chaining would mean dropping the bound.

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

## P83 — The map page's inline share fell from 72% to 37%; pin-detail and Settings are still moving half their HTML as script every load

`id: P83` · `status: open` · `updated: 2026-09-16` · `re-measured 2026-09-16 after X21; supersedes the 2026-09-06 table`

Found 2026-09-06 while measuring whether the Settings page's Security tab was worth deferring
(P69). It is not - those queries are 5 of 22 and under 3ms of 71ms - but the same measurement
found where that page's weight actually is, and it is not the database.

**Re-measured 2026-09-16**, same three pages, same method (`inline_blocks()` from
`src/urbanlens/core/tests/inline_scripts.py` against a logged-in `Client(SERVER_NAME="localhost")`
response, counting only `<script>` tags with no `src`), run via `manage.py shell` in
`urbanlens_development_main_app` against the dev database - a different account and pin count than
2026-09-06's run, so totals are not a clean diff, and this pass ran with `DEBUG=True` where the
original was `DEBUG=False`:

| page | HTML (09-06 → 09-16) | inline script (09-06 → 09-16) | share (09-06 → 09-16) |
|---|---|---|---|
| `/dashboard/map/` | 550,852 → 240,779 | 400,066 → 90,113 | 72% → **37.4%** |
| `/dashboard/map/pin/<slug>/` | 343,443 → 300,005 | 190,886 → 144,353 | 55% → 48.1% |
| `/dashboard/settings/` | 309,647 → 262,747 | 169,194 → 121,946 | 54% → 46.4% |

**What moved: the map page's own 265,146-byte block (X21) is gone**, replaced by
`frontend/static/js/map-page.js`. Its largest remaining block on all three pages is now 22,347
bytes - shared chrome, not decomposed further this session. Pin-detail's own largest block is now
**59,463 bytes**, larger than anything left on the map page and untouched by X21; that block is the
next obvious target, not the shared 22KB one. Settings' largest is 28,694 bytes, also unexamined.

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

**The map page's block is done** (X21). Not decomposed or attempted this session: pin-detail's
59,463-byte block, Settings' 28,694-byte block, or the ~22KB still shared by all three pages. Per
P92, X21's fix was a plain `.js` file behind a config element, not a `tsc`-checked bundle, so
whether the answer for what remains is `frontend/ts/entries/` or something narrower is still an
open design question - unchanged by X21 even for the part of this entry that is now done.

## P85 — Every manager is a dynamic base class, so `Model.objects` is `Any` and 146 mypy errors are turned off to hide it

`id: P85` · `status: open` · `updated: 2026-09-14`

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

- `spotguessr/overview.py:91` - `Cannot resolve keyword 'participant_count'`. It is an
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

**The rest, triaged 2026-09-14.** Re-measured with `--enable-error-code misc`: 190 errors, 146 the
dynamic base class, 16 the `EnrichmentSource` ClassVar-vs-instance-variable pattern
(`MediaPanelSource.__init__` assigning `key`/`cache_source` is the same thing seen from the instance
side). None of the remaining 27 is a defect in itself: django-stubs cannot see the self-referential
M2M through model's `from_label`/`to_label`, `.annotate()` names, a `date` in `visited_at__date__in`,
or that `pk=None` from `safe_int_or_none` is a deliberate zero-row lookup; the `dispatch` findings
are mixins with no declared base; `ConsentPreferenceWording`'s `WITHOUT_FACE` is shadowed by an enum
member on purpose; `except (*STORAGE_ERRORS, ...)` is a typed tuple mypy will not unpack; and
`AppSettings.__getattr__`'s `super()` is pydantic's, defined only outside `TYPE_CHECKING`.

One of them pointed at a real bug anyway. `controllers/map_overlays.py::_image_from_request` returned
`object | None`, and the `Image` path behind that loose type passed the raw `image_id` POST string to
`filter(pk=...)` - `ValueError`, a 500, for any non-numeric id. Sweeping for the same shape found it
on nine routes: pin and markup-map share sends (`profile_id`), the photo and markup-map custom-field
saves (`field_id`), `LabelMergeView` (`target_label_id`), pin, wiki and trip comments (`parent_id`
and `existing_image_id`, through `attach_existing_comment_image`, `_wiki_comment_addressable_by` and
`services/trips/trip_comments.py::add_comment`), the overlay picker, and `ConsensusVoteView` (`answer_id`).
`LabelMultiMergeView` had the `OverflowError` gap described above plus an `AttributeError` for a JSON
body that is not an object. All reproduced first (15 failures) and fixed through `safe_int_or_none`;
`test_non_numeric_posted_ids.py`. The game invite and kick views already caught `ValueError` and were
left alone.

The label JSON endpoints had the same gaps without an id lookup in between. `_parse_ids_json` (bulk
edit and bulk convert) and `LabelReorderView` answered `Infinity`, an id field that is not a list, or
a body that is not an object with a 500, and `controllers/labels.py` carried a private `_safe_int`
copy without the `OverflowError` catch. A finite 30-digit `order` parsed cleanly and then overflowed
`Label.order`'s 32-bit column on save, on create, edit and bulk edit alike; `_label_order` now
clamps it to `services/core/numbers.py`'s `DB_INTEGER_MIN`/`DB_INTEGER_MAX`
(`test_label_malformed_bodies.py`). Label create and edit also passed the raw `parent_ids` list to
`filter(id__in=...)`; `_posted_label_ids` drops the entries that are not integers and keeps the
rest. A 30-digit id in a *lookup* is not a crash: Django answers it with no rows, which
`test_non_numeric_posted_ids.py` pins.

The same overflow reached the style columns. `controllers/detail_pins.py` and `controllers/markup.py`
wrote a posted opacity through `safe_int` with no bound, so 150 or -20 was stored and rendered as
sent and a 30-digit value failed the save; markup's `stroke_width` likewise, where import already
clamped it to `[1, 200]`. Both files' `_opacity` now clamp to `[0, 100]` and stroke width to import's
range, matching `saved_filters.py::_clamp_opacity`, `map_overlays.py::_clamped_opacity`, the
profile settings form and the external API serializer (`test_style_opacity_bounds.py`).

The final tally is three false positives in the first sample, 27 benign in the second, and 32
reproduced defects reached from them - the argument against leaving the code off: a blanket disable of the code
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

## P91 — Four of eight security integration specs have never run against a live deployment

`id: P91` · `status: open` · `updated: 2026-09-15`

Found 2026-09-08 during the pre-merge audit of `release/v_0_8_0`; confirmed on an independent
adversarial pass. Previously titled "seven of eight" - the pending run this entry called for landed
the same day and moved three more: `authorization.spec.ts` (`70327552c`, fixed a real 500-vs-400
bug), `input.spec.ts` (`f7a66438d`, "CRLF header-injection test never reached the server... verified
against the live deployment"), and `surfaces.spec.ts` (`e8aa7daf7`, media-gate race fix) all now
carry real fix commits from live runs, alongside `isolation.spec.ts`, which already had one before
this entry was filed.

`3547deb11` ("security related integration tests, not yet run -- needs review and expansion") added
eight spec files under `tests/integration/specs/security/`. Per-file `git log --oneline`, the four
still byte-identical to their initial commit - no evidence either has ever executed - are
`assumptions.spec.ts`, `disclosure.spec.ts`, `session.spec.ts`, `transport.spec.ts`.

Same risk class `docs/archive/PROBLEMS-ARCHIVE.md`'s P75 already documents shipping:
`disclosure.spec.ts:32` asserted `/dashboard/this-path-does-not-exist-91b2c/` returns 404 while the
`dashboard/` catch-all answered 200 for an unknown span of time, because - per that entry - "That
spec has only ever run when someone triggered it by hand - `integration.yml` is `workflow_dispatch`
only, deliberately, because it drives a deployed instance - so an assertion encoding the correct
behaviour sat next to code that could not satisfy it, and nothing said so." The underlying 404 bug
is fixed (P75, resolved 2026-09-05), but it was found by a different investigation (P35's
hardcoded-URL audit), not by running this file - so `disclosure.spec.ts` remains one of the four
with no evidence it has ever caught anything by being executed, the exact gap P75 describes. These
four need the same live run the other four already got.

## P92 — `map-clusters.ts`'s cluster badge constants are duplicated, not shared, by the main map's cluster layer

`id: P92` · `status: open` · `updated: 2026-09-16` · `citation refreshed 2026-09-16, see X21`

Found 2026-09-08 while closing out the pre-merge audit of `release/v_0_8_0` (the "audit wasn't done yet" tail of
that pass, not a new sweep - see the audit's own confirmed finding "map-clusters.ts's shared cluster-icon module
was never wired into the main map it claims to cover").

`shared/map-clusters.ts`'s module docstring used to claim it was shared by "the main map's inline cluster layer,
and the pin-detail / wiki maps." That was only half true: `entries/map-annotations.ts` (the pin-detail/wiki map
entry) does import and use `createPinClusterGroup`/`pinClusterIconParts` from it (`detailPinLayer`), but the main
map page's own cluster layer never imports this module at all - it hand-rolls its own `L.markerClusterGroup` call
with its own copy of the badge sizing table and `iconCreateFunction`. **Re-cited 2026-09-16:** X21 moved this code
out of the template - it is no longer `templates/dashboard/pages/map/index.html:979-999`, it is
`frontend/static/js/map-page.js:343-364` - but it moved as raw text, not as a bundled TypeScript entry, so nothing
about this problem changed. The duplication below is exactly as unresolved as it was on 2026-09-08:

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
duplication is unfixed. Not fixed here because the real fix isn't a one-liner: the main map's clustering code was
inline template JavaScript, which could not `import` a TS module - unifying it needs either (a) a mechanism for a
plain script to read shared constants/functions from a bundled entry (no such mechanism exists anywhere else in
this codebase today, per a search for `window.UL =`/`globalThis.UL =`), or (b) migrating the main map's script into
a proper bundled TS entry the way `map-annotations.ts` already is for pin-detail/wiki maps.

**2026-09-16: (a) is now the live blocker, not (b).** X21 gave the script a `<script src>` and a cache header, which
is the part of P83/P34 this entry used to point at, but it is still a hand-written `.js` file fed by a generated
config element, not a `tsc`-checked bundle - `map-page.js` cannot `import` from `shared/map-clusters.ts` any more
than the inline block could. Closing this now needs (a) or (b) specifically, not just "finish P83/P34".

## P95 — One import preview entry is still read whole at up to 1 GB, and what parsing it costs is unmeasured

`id: P95` · `status: open` · `updated: 2026-09-14`

Previously titled "An import preview can hold 2 GB of extracted bytes in a sandbox worker that has
3 GB for two jobs", and before that "`ExtractionBudget` cannot bound a single file's decompression,
and nothing prices what parsing one costs".

Related to P2 (sandboxing the preview parse) - cross-referenced rather than duplicated: this is about
the resource cost of the parse, wherever it runs.

**Where the parse runs, re-checked 2026-09-14.** Not in a gunicorn worker any more.
`services/pins/import_preview.py::start_import_preview` stores the upload and enqueues
`tasks.py::parse_import_preview_task` on the sandbox queue, which `media-worker` consumes with
`--concurrency=2` under a 3 GB `mem_limit` by default (`docker-compose.yml`). `guard_key`'s
single-flight claim allows one preview per account and answers a second with 409, so the old concern
that one account could start 600 near-2 GB extractions a minute under the DRF `user` throttle no
longer holds.

**The limits, as they stand.** nginx's `client_max_body_size 200m` (`config/nginx/django.conf`) bounds
the compressed upload. `_read_uploads` builds one `ExtractionBudget` for the whole upload, nested
archives included: 2 GB uncompressed and 1000 files. `_MAX_SINGLE_FILE_BYTES` caps one entry at 1 GB;
this entry's old title said no per-file bound existed, and one did.

**Fixed 2026-09-14, three ways a preview outgrew the worker.**

- **The budget was charged after the read.** `_extract_zip` and `_extract_tgz` read every entry up to
  the 1 GB cap and only then deducted it, so an upload with 1,000 bytes of allowance left still asked
  for 1,073,741,825 bytes. `ExtractionBudget.read_limit` bounds the read to what remains
  (`test_extraction_budget_before_read.py`).
- **Every entry was held until the parser returned**, up to the whole 2 GB. `_read_uploads` now hands
  `GoogleMapsGateway.parse_for_preview` a generator (`import_preview.py::_uploaded_files`, over
  `archive_extractor.py::iter_archive`), so each entry is parsed and let go before the next is
  extracted. Shapefile sidecar parts are the exception - a bundle is parsed once all its parts are in -
  so they are parsed after every other file: their lists come last, and they are what is dropped when
  a preview reaches `MAX_PREVIEW_PINS`. Ten 8 MiB entries peaked at 100.8 MB under `tracemalloc`
  before; `test_import_preview_memory.py` holds the peak under half of what was extracted.
- **Two previews could run side by side** in `media-worker`'s two slots. `parse_import_preview` now
  claims one of `IMPORT_PREVIEW_MAX_CONCURRENT_PARSES` site-wide slots first
  (`import_preview.py::_claim_parse_slot`, default 1). A preview that finds none stays "pending" and
  `parse_import_preview_task` retries it every `PARSE_SLOT_RETRY_SECONDS`, until `STALL_AFTER`, when
  it ends itself and frees the account. A slot outlives the parse's hard limit by a minute, so a
  killed worker's slot frees itself.

**Still open: one entry and its parse.** An entry is still read whole, up to `_MAX_SINGLE_FILE_BYTES`
(1 GB), and the parsers build their own structures from it, so a single preview can still outgrow
`media-worker` - and the slot keeps a second preview off the worker, not a photo job. What a large
KML or shapefile costs to parse is unmeasured; the task's time limits bound its wall time, not its
memory. No adversarial upload has been run, so the risk is by arithmetic, not observation. Lowering
the per-entry cap is a product call - a heavy account's Google Takeout location history is large, and
nobody has measured how large. With one slot, a preview also waits behind every preview ahead of it,
each up to its 110-second soft limit.

---

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

## P105 — A Valkey outage 500s every request after 32 seconds, including the readiness probe - fixed except the probe's verdict

`id: P105` · `status: open` · `updated: 2026-09-13`

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

**Fixed 2026-09-13, and measured the same way it was found.** `core/cache_backend.py`'s
`ResilientRedisCache` replaces the stock backend, on the principle that **a cache that cannot be
reached behaves like a cache with nothing in it** — which covers `exists()` and `delete()`, the two
Django leaves bare, and every direct `cache.get` in this codebase, in one place rather than at
thirty call sites.

The half that decides whether the site is up is the breaker, not the swallowing. One failure holds
the store off for `UL_CACHE_BREAKER_SECONDS` (10s) and every later call in that window answers
immediately without touching a socket, so a request pays one timeout rather than one per call. One
probe is let through when the window ends, so recovery needs no signal and nothing has to notice
Valkey came back.

Measured on the dev stack with Valkey paused, against the 32s this entry recorded:

```
  GET /                        200 in 0.04s        (was 500 in 32.17s)
  session create+load+flush    OK   in 4.25s       (was: raises - login and logout)
  16 consecutive cache reads        in 0.00s       (was ~32s)
```

Three answers are deliberately not "as if empty", and each is a decision rather than an oversight:

- **`add` reports False.** It is the claim half of every lock here (`services/core/single_flight`),
  and a lock granted by a store that cannot hold it is not a lock.
- **`incr` raises `ValueError`** — what Django raises for an absent key, which is the honest answer.
  `account._bump_counter` already catches that and restarts the window, so a failed-login counter
  degrades instead of exploding.
- **A full store (`OutOfMemoryError`) degrades that one write without tripping the breaker.**
  `volatile-lru` is still perfectly able to answer reads; holding them off for ten seconds because a
  write did not fit would turn H54's fill case into an outage it is not.

**The abuse controls fail open, and this entry's own note asked for that to be an explicit
decision.** It is the project's existing one: `services/security/throttle.py` and
`socket_budget.py` both allow when they cannot read their counter, on the reasoning that an outage
which also locks everyone out is strictly worse. The login lockout now inherits it by the same
argument. The residual, stated rather than left implied: for the length of an outage, and only then,
failed-login counting stops.

**The first version of this passed 18 unit tests and did nothing at all.** redis-py's
`ConnectionError` and `TimeoutError` derive from `RedisError`, **not** from the builtins of the same
name, so a backend catching the builtins caught none of them — and every mock in the suite raised a
builtin, so the suite agreed. Found in about a minute by pausing Valkey against a real stack, and
not findable any other way: the mock and the code shared the same wrong assumption. Every
degradation case now runs against all four classes a real outage raises, named in one tuple at the
top of the test file.

**One thing this changes without settling: `/health/ready` answers 503 while the site serves 200s in
40ms.** A verdict rather than a timeout is progress on its own, and the endpoint now returns in
about 4s rather than 32. But the controller's own `_is_degraded` docstring already says an instance
whose cache is down "still serves pages", and after this fix that is simply true — so the 503 and
the code beside it now disagree, where before they agreed.

Deliberately not changed here, because the reason it was right has moved but the consumers have not
been looked at. `test_health.py` records the question as open in as many words, and both consumers
live in the infrastructure repo: `deployment-web.yaml` uses `/health/ready` as its **startupProbe**
(so a 503 during a Valkey outage blocks a rollout, which is conservative rather than wrong), and
ingress-watch fetches it every minute and pages. If that pager keys on the status code rather than
on `degraded`, flipping to 200 silences the alert for a real outage — which is a worse failure than
the one being fixed. PL7 phase 2 specifies "stays 200 when degraded"; doing it needs the alert rule
changed in the same breath, and that is an owner call across two repositories.

See D11 for the Valkey split this sits inside; the chaos scenario is the reproduction.

## P110 — The Overture OOM fix is best-effort, and Overture rate-limiting us is what turns it off — the request-rate gap is closed in code and unit-tested, not yet re-verified live

`id: P110` · `status: open` · `updated: 2026-09-17`

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

**Not fixed by the outbound guard added the same day.** The reads happen inside `pyarrow`/`S3FileSystem`, not
through `self.session`. `OvertureMapsGateway` also sets `service_key = None` ("no HTTP endpoint of ours to
rate-limit"), but that does not opt it out: `ServiceMeta.__new__` replaces any falsy key with one derived from the
class name, so the class carries `overture_maps` and its unused session is wrapped under that key (found by P93). Nothing in `rate_limiter` sees them: the guard reported zero successful
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

**Fixed 2026-09-17: the reads are no longer invisible to the rate limiter.**
`OvertureMapsGateway._fetch` (`services/apis/locations/boundaries/overture_maps.py:110-138`) now
calls `_reserve_call_budget(overture_type)` (`overture_maps.py:140-175`) at the very top — before
`_require_narrowing`'s STAC lookup and before the pyarrow/geopandas read itself. That method calls
`_reserve_call(service, endpoint=overture_type)` from `services/core/rate_limiter.py`: the same
atomically-locked (`transaction.atomic()` + `select_for_update()`) reserve-then-finalize pair
`_RateLimitedSession._do_request` already uses for every ordinary `self.session` call. It returns
the pk of a reserved `ApiCallLog` row, or raises `RequestCancelledError` — re-raised as
`GatewayRateLimitedError` — if the service is disabled or its budget is spent for the window,
mirroring `_require_narrowing`'s existing refusal contract, which callers already catch.
`_finalize_call(entry_pk, success=..., response_ms=...)` records the outcome afterward on both the
success and exception paths, so a real Overture/pyarrow failure is logged rather than swallowed. A
new `SERVICE_REGISTRY["overture_maps"]` entry (`rate_limiter.py:187-198`) gives it a budget —
`calls_per_minute=20`, `calls_per_day=500`, `billable=False`, the same generic-fallback numbers
already used by its open-dataset siblings (e.g. Microsoft Building Footprints) since Overture
publishes no documented quota either; not independently tuned against a real one.

**Deliberately not the codebase's own documented simpler pattern.** `dashboard/CLAUDE.md`'s
convention for `self.session`-bypassing code is `service_is_enabled()` → `check_rate_limit()` →
call → `log_api_call()`, and four AI-service files (`vision.py`, `article_expansion.py`,
`article_safety.py`, `assistant.py`) use it as-is. `_reserve_call_budget`'s own docstring explains
why Overture instead uses the stronger atomic `_reserve_call`/`_finalize_call` pair: those four
callers make one call per task, but Overture is reached from concurrent per-pin enrichment fan-out —
the exact burst this entry is about — and a plain check-then-log gap would let every concurrent
caller pass the check before any of them logs, defeating the budget precisely when it matters most.

**Verified by unit tests only — not re-run live.** 19 tests pass across the three relevant files
(`docker exec ... pytest src/urbanlens/dashboard/tests/hypothesis/test_overture_call_budget.py
src/urbanlens/dashboard/tests/hypothesis/test_overture_maps_stac_narrowing.py
src/urbanlens/dashboard/tests/hypothesis/test_overture_stac_is_required.py`, confirmed this session:
19 passed in 214.84s): 7 of them new (`test_overture_call_budget.py`), covering a call within
budget reaching Overture and being logged successful, a budget-exceeding call refusing *before* the
STAC lookup runs at all, a refusal logged rate-limited, a failed read logged unsuccessful rather
than swallowed, and a disabled service refusing without touching Overture at all. `ruff check` and
`mypy` both pass clean on the two touched source files (confirmed this session).

**Not verified: whether bounding the request rate actually stops Overture from rate-limiting us.**
Unlike the 2026-09-10 fix above, this has not been run against the load suite — there is no
before/after neighbour-p95, queue-drain, or 429-count table for it, the way there is for the circuit
breaker. Nothing here confirms that 20 calls/minute keeps enrichment under whatever threshold
actually earns Overture's 429, or that a bulk import's fan-out (P109) now produces bounded
refusals instead of retriggering the loop this entry is named for — only that the code path exists,
is reachable, and behaves as designed in isolation. That live measurement is what would close this
entry; it has not been attempted this session.

Still open in this entry, and the reason it is not archived: **live confirmation, under the same
load-suite shape as the 2026-09-10 table above (import phase + cooldown), that this budget actually
prevents the 429-triggered fallback loop** — plus whether 20/minute is the right number for a real
bulk-import fan-out rather than just a plausible default carried over from an unrelated service.

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

## P113 — 54 verified places where one account's ordinary use can degrade the site for everyone else - 1 still open

`id: P113` · `status: open` · `updated: 2026-09-16` · `supersedes the 2026-09-13 "2 still open" count: H54 closed by D16`

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

## The work list

Re-verified against the code on 2026-09-13; H54 additionally closed 2026-09-16 (see below). Of the
65 tracked findings, everything is closed except the row below and the four parked decisions. The
closed items' fix narrative - what each finding actually was, the corrections found while fixing it,
and the reasoning behind each choice - is in `archive/PROBLEMS-ARCHIVE.md` under 2026-09-15 (H54's
own narrative is dated 2026-09-16 within that entry) rather than repeated here;
`notes/availability-audit-2026-09-11.md` (N21) has the original findings and the four downgrades from
the hostile-review pass.

**H54 closed 2026-09-16**: D16 moved the Celery broker off Dragonfly onto RabbitMQ, which removes the
broker's unbounded, no-TTL keys from the shared keyspace entirely rather than bounding them in place.
Narrative moved to `archive/PROBLEMS-ARCHIVE.md`; see
[`docs/designs/dragonfly-rabbitmq-pgvector-stack-adoption.md`](designs/dragonfly-rabbitmq-pgvector-stack-adoption.md)
(D16). H35/H38's separate size-limit risk on the same store is unaffected - see D16 for why.

**Open:**

| ref | severity | what remains | why it is not done |
|---|---|---|---|
| H56 | high | Under gevent a request that spends its timeout in non-yielding CPU takes the whole worker down. `--worker-connections 20` bounds the blast radius to 19 requests; it does not remove it. Both requests known to run that long are fixed (P108, P96), so it is latent rather than reachable | D11 phase 3a (gthread), designed and unbuilt |

**Parked by decision, not forgotten:**

| ref | parked because |
|---|---|
| H21 | Bounded at `MAX_PREVIEW_PINS`. The two real fixes are a schema change or PL7 phase 4; choosing here pre-empts that plan |
| H34 | The attribution half is built; the fair-share limiter is designed as D14 and not started |
| H44 | Authorizing one photo: 15 queries → 11 (→7 anyone/anyone, →3 when the uploader's setting refuses outright). Cutting the reach computation further (11→8) would make the gallery path - which deliberately resolves the viewer's sets once and reuses them across N uploaders - worse; a trade-off to decide, not an optimisation to apply |
| H47 | Only the pagination half. `visible_comment_tree` filters in Python, so paginating the queryset returns short pages and changes the API contract |

The sweep was not exhaustive: family 4 (request-path loops with no ceiling) was worked through
per-endpoint rather than to completion, so more instances of it likely exist beyond these findings.

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

## P122 — A refused external call returns a 500 from views that catch only `GatewayRequestError`

`id: P122` · `status: open` · `updated: 2026-09-15`

Every gateway call passes through `rate_limiter._reserve_call`, which refuses a call by raising a
`RequestCancelledError`:
- `RateLimitExceededError`;
- `ServiceDisabledError`;
- `RateLimiterUnavailableError`, when the limit cannot be read.

That family subclasses `DashboardError`, not `GatewayRequestError`. So a view that handles a failed call
with `except GatewayRequestError` lets a refused one escape as a 500. Refusals are routine:
- development and the demo refuse most services outright;
- under R29, `RateLimiterUnavailableError` fires whenever `ul_web` is at its connection limit.

Still a 500 on a refusal, found by review on 2026-09-15 (read, not run):
- **Settings geocoding:** `controllers/settings.py` `geocode_address` catches `(ImportError, OSError, ValueError)`
  around `GoogleGeocodingGateway.geocode_place_name`.
- **Immich connection check:** `controllers/immich.py` `ImmichSettingsView.post` calls `ImmichGateway.ping()`
  with no handler, and `ping` catches only `GatewayRequestError`.
- **Immich thumbnails:** `controllers/pin_suggestions.py`'s thumbnail proxy catches only `GatewayRequestError`.
- **Google Photos picker:** `controllers/google_photos.py`'s session, polling and download steps catch only
  `GatewayRequestError`.

The Flickr and Immich search pickers catch both (`test_a_refused_call_degrades_its_picker.py`).

**Likely fix:** make `RequestCancelledError` a `GatewayRequestError`, so every existing handler covers
refusals. Audit the 48 production `except GatewayRequestError` sites first, looking for two things:
- a handler that shadows a more specific `except RateLimitExceededError` below it;
- a handler that treats a failure as grounds to retry or to disconnect an account.

Catching refusals site by site is the alternative, and the next view will forget it again.

## P124 — Seven tests still assert inline `<script>` text that left the HTML in `23a861765`, and ROADMAP.md cites one of them as proof of a privacy property

`id: P124` · `status: open` · `updated: 2026-09-16`

A targeted run of the map-view test set on 2026-09-16 gave **7 failed, 200 passed, 2 subtests
passed in 139.11s**. The seven, all `AssertionError: ... not found in '<!DOCTYPE html>...'` on an
`assertIn(<js source string>, body)`:

- `src/urbanlens/dashboard/tests/hypothesis/test_search_history_cache_scoping.py::MapAddressSearchHistoryScopingTests::test_history_key_is_scoped_to_the_viewing_profile`
- `...test_search_history_cache_scoping.py::MapAddressSearchHistoryScopingTests::test_stale_unscoped_key_is_cleaned_up`
- `...test_search_history_cache_scoping.py::ComposerSearchHistoryScopingTests::test_history_key_is_scoped_to_the_viewing_profile`
- `...test_search_history_cache_scoping.py::ComposerSearchHistoryScopingTests::test_stale_unscoped_key_is_cleaned_up`
- `src/urbanlens/dashboard/tests/hypothesis/test_map_gps_recenter_flash.py::GpsRecenterGuardRenderedTests::test_geolocation_success_callback_guards_the_live_recenter`
- `...test_map_gps_recenter_flash.py::GpsRecenterGuardRenderedTests::test_had_cached_location_is_captured_before_the_async_geolocation_call`
- `src/urbanlens/dashboard/tests/hypothesis/test_bulk_edit_rating_ui.py::BulkEditRatingUiTests::test_confirm_handler_reads_the_rating_select_into_the_payload`

Example: `assertIn("localStorage.removeItem('ul_addr_history_v1')", body)` against a response body
that no longer contains any inline `<script>` for the map program at all.

**Cause, by commit archaeology, not inference.** `git log -S "ul_addr_history_v1" -- src/urbanlens/dashboard/templates/`
and the same pickaxe over `src/urbanlens/dashboard/frontend/` both land on **`23a861765`** ("perf:
the map page's program is a file the browser keeps, not 275 KB re-sent on every visit"): that
commit is where the string left the templates and entered
`src/urbanlens/dashboard/frontend/static/js/map-page.js`. The page now loads the script as a
cacheable static file instead of inlining it, so a `body` string-search can no longer match it. The
three test files were last touched by `5b9fced01` ("continuation of comment stripping"), not by
`23a861765` - they were never updated for the move.

**Not the same finding as X21.** `d860d1b9a` recorded "the two defects [`23a861765`] introduced" in
X21 (`docs/notes/inline-script-extraction-map-and-theme.md`): a template tag sitting inside a
string quote delimiter, and a global `const CFG` name collision between `map-page.js` and
`comment-map.js` that briefly broke the map page. Neither of those is this. This is a third
consequence of the same commit that X21 did not capture: X21 checked the moved *code*, not the
*tests* asserting against the template it was moved out of. The only other existing mentions of
these three files in `docs/` are a bare inventory listing in `docs/notes/test-quality-audit-files.txt`
and generated mirrors under `docs/_build/` - neither is a defect record.

**Why this entry is worth more than "some stale tests": `docs/ROADMAP.md:374` cites dead coverage
as proof of a privacy property.** UL-239 (per-user localStorage search-history keys) is marked
resolved there with "Verified with `test_search_history_cache_scoping.py`." That file has been
failing since `23a861765` on the exact assertions that would demonstrate the scoping. The sentence
is false as written: it names as verification a test file that cannot currently pass. Not edited
here - record the problem and let a human correct that line, per house convention for a document
this task did not ask this entry to rewrite.

**The underlying behaviour still looks correct on a source read - a source read, not a
verification.** `frontend/static/js/map-page.js:4288` builds the key as
`'ul_addr_history_v1_' + MAP_CFG.profileId + ''`, and `:4285` does the one-time
`localStorage.removeItem('ul_addr_history_v1')` cleanup of the old unscoped key. Nothing in this
session ran that code in a browser or exercised it in a passing test. Do not read this entry as "the
scoping is broken" - it is specifically "the thing that was supposed to prove the scoping does not
run", which is a different and narrower claim.

### Candidate direction (unverified, not implemented)

Rewrite the seven assertions against what now carries the behaviour instead of the HTML body: the
served static JS (`frontend/static/js/map-page.js`) for the code itself, and the per-request
`#map-page-config` `json_script` block (`MAP_CFG.profileId`) for the value it's keyed on. Also open:
how many *other* tests across the suite assert inline JS text in rendered HTML and broke the same
way when `23a861765` and `3c924327e` shipped - not surveyed this session.

**An eighth, found incidentally on 2026-09-16:**
`test_map_document_head_reads_the_vocabulary.py::TheDocumentHeadTests::test_the_map_page_reads_every_kind_of_line_the_document_sends`
fails with `sent - read == {'end', 'head', 'labels', 'pin'}` - that is, its `read` set is *empty*. It builds
`read` by running `re.findall(r"obj\.t === '(\w+)'", MAP_TEMPLATE.read_text())` over
`templates/dashboard/pages/map/index.html`. That pattern occurs **0 times** in the template and **4 times** in
`frontend/static/js/map-page.js`, and `git log -S "obj.t === 'pin'"` on the template lands on `23a861765` - the
same commit. So the test asserts the document and the page agree about line kinds while actually comparing
against nothing, and would keep passing if they disagreed. It differs from the seven above in shape (a regex
over a template file on disk, not an `assertIn` against a response body), which is why a search for the
`assertIn` shape alone will not find the rest of this family.

Not fixed. Not re-measured beyond the 2026-09-16 targeted run and the source read cited above.

## P125 — The population capacity harness collapses at 500 concurrent users on the app container's CPU; production now has a 4-core override to deploy, not yet applied or re-measured

`id: P125` · `status: open` · `updated: 2026-09-17`

**This is a different axis from P113 and P123.** Those are the "neighbour" question - does one
account's action cost a *different* account anything at all (D11, `tests/perf/k6/neighbour.js`).
This is D15's question: how many ordinary concurrent users, each just browsing, can the whole site
serve before it stops meeting its own budget. A design can pass one and fail the other -
D15 says so explicitly. This entry is about the second one, measured, and still failing.

**The evidence has never been in `docs/` and is not in git.** `tests/perf/results/` is entirely
gitignored (`tests/perf/.gitignore:3`, "Nothing here is reproducible or safe to commit") because run
output holds live session cookies. That means every number below exists only on whichever machine
ran it - this entry is the only durable record of it, so treat the run directories as reproducible
inputs to redo, not archives to expect being there.

### Harness and population (`tests/perf/README.md`, `bin/run_capacity_tests.sh`, `k6/population.js`)

1,000 accounts, heavy-tailed pin counts (60% at 10-100, 30% at 100-1,000, 9% at 1,000-5,000, 1% at
10,000-20,000), minted sessions rather than passwords so signing them in doesn't measure PBKDF2.
`tests/perf/results/capacity-population/manifest.json`: **1,000 accounts, 471,756 pins total**,
median 62 per account, max 17,720. Each VU browses, polls unread counts and the map's meta, and
holds a notification socket - modelled on the templates, not guessed (see D15 for the full model).
Budgets are D15's: page p95 < 1,000 ms, fragment/poll/JSON p95 < 500 ms, requests failed < 0.5%,
socket handshakes > 99.5%.

### Five runs exist locally, all against the same 471,756-pin population, in this order (2026-09-15)

| run | time | requests failed at u1000 | what it shows |
|---|---|---|---|
| `capacity-smoke` | 17:16 | - (only ran to u40) | sanity check, not a capacity result |
| `capacity-baseline` | 17:39 | 26.14% | fails hard before the day's fix pass |
| `capacity-fixes` | 18:21 | 24.18% | after a first round of fixes; still fails hard |
| `capacity-gthread` | 19:09 | 0.00% | gthread worker class instead of gevent; hard failures gone, latency still collapses |
| `capacity-content` | 22:53 | 0.00% | latest; hard failures gone, latency still collapses |

Between `capacity-baseline` and `capacity-content`, roughly forty fixes landed the same day, driven
directly by this harness - representative ones: nginx moved from refusing past 500 sockets/worker to
holding ten thousand (`77b458b2c`), a web request reuses its thread's Postgres connection instead of
logging in fresh (`534055e5c`), a page's header badges arrive with the page instead of three requests
after it (`04a2c7cb2`), the wiki-reach check became a subquery instead of shipping every pinned
location id (`a1ca8c134`), the global-search semi-join replaced a join-and-deduplicate
(`c354caabe`), and a list's pin count is counted in the database instead of fetched row by row
(`7835838c2`). `capacity-content` is the state after all of it, and is the number that matters below.

### `capacity-content` (2026-09-15 22:53, the current best-known state) — meets D15 through u250, collapses at u500

| hold | requests failed | ws handshakes ok | fragment p95 (budget 500ms) | page p95 (budget 1000ms) | verdict |
|---|---:|---:|---:|---:|---|
| u100 | 0.00% | 100.00% | 19-268 ms | 89-356 ms | **meets every measured D15 ceiling** |
| u250 | 0.00% | 100.00% | 25-305 ms | 99-469 ms | **meets every measured D15 ceiling** |
| u500 | 0.00% | 100.00% | 902-4,733 ms | 1,345-5,944 ms | fails both latency budgets, on every endpoint |
| u1000 | 0.00% | 100.00% | 23,094-23,880 ms | 23,218-24,720 ms | fails both latency budgets, on every endpoint, by ~50x |

(`tests/perf/results/capacity-content/report.md`; full per-endpoint table has 22 rows per hold, all
consistent with the ranges above - none is an outlier carrying the rest.)

**The bottleneck moved, and moved somewhere a query fix cannot reach.** In `capacity-fixes`, Postgres
was the constraint: DB CPU throttled 114.58% at u500 and 163.21% at u1000 (`containers.csv`;
`ul_perf_db` mean cores 1.64-1.70 against its 2-core limit), and the edge proxy logged **9,100×502 +
1,505×504 out of 16,945 requests at u1000** - over 60% of requests failing outright. In
`capacity-content`, after that round of fixes, DB CPU is *low* (0.50-0.63 mean cores, 1.36-2.65%
throttled) and the **app container** is now pegged instead: 2.00 mean cores against its 2-core limit,
84.41% throttled at u1000. `docker-compose.yml:195` sets that limit -
`cpus: ${CPU_LIMIT__APP:-${CPU_LIMIT:-2}}` - a repo-wide default, not a perf-environment-only
setting. Nothing failed outright because nothing timed out hard the way a starved DB connection pool
does; requests simply queued behind a full CPU allocation and answered 20-50x slower.

**This means the fixes worked and ran out of runway at the same time.** Query-level and N+1 fixes
made the DB cheap enough that it is no longer the limit; what remains is that one 2-core app
container cannot serve 500-1,000 concurrent browsers' worth of Python/Django work inside budget, no
matter how cheap each individual query is. The next lever to test is more app capacity - a higher
`CPU_LIMIT__APP`, more app replicas behind nginx, or both - not another per-endpoint query fix, since
the endpoints that are over budget are not the ones with expensive queries; they are all of them,
uniformly, which is the signature of a shared resource being out of headroom rather than any one
endpoint being slow.

### What this does not establish

- **Not re-run since 2026-09-15.** Whatever landed in this repository afterward (including this
  week's P123 work) has not been measured against this harness.
- **Writes are entirely out of scope**, per D15: nobody in this population imports, uploads, edits a
  label, or sends a message. A real 1,000-user population does some of that concurrently with the
  reads measured here; this number is a ceiling on reading alone, and could be worse under a mixed
  load.
- **The Postgres-pool and signed-in-session ceilings from D15 were not checked here** - `report.md`
  doesn't carry connection-pool percentage or session-survival numbers; `pg_activity.csv` has the raw
  samples but wasn't reduced to that figure for this entry.
- **Run on chiron's perf environment, load generator beside the target** (`tests/perf/README.md`),
  not on damballa and not with a dedicated load-generation host. Says where the current design
  breaks, not what production hardware would do.
- **10,000 users has not been attempted.** D15 sets it as the eventual target; nothing here speaks to
  it, and the 1,000-user collapse would need to be understood and fixed first regardless.
- Neither `capacity-gthread`'s worker-class change nor whatever changed between `capacity-fixes` and
  `capacity-content` was captured as a design decision or a diffable commit range in this entry - the
  runs are dated and can be told apart, but "what exactly changed between them" would need
  reconstructing from the day's full commit list, not just the ones cited above.

### 2026-09-17: production's app tier gets more CPU, on the config side only

`capacity-content`'s bottleneck is the app container's own 2-core allocation, uniform across every
endpoint - not a query. `src/urbanlens/config/env/production.sample.env` now sets
`CPU_LIMIT__APP=4`, following the same mechanism `staging.sample.env` already uses for the opposite
direction (P114): the shared `docker-compose.yml` default stays at 2 cores, so local/dev/testing and
staging are unaffected, and only production's app tier gets the increase, via its own override.
`test_production_app_cpu_allocation.py` pins the floor at 4 cores, pins the shared default at 2, and
fails if staging's app limit ever rises to meet production's.

**Not yet applied anywhere.** Like every `*.sample.env`, this has to be copied into production's
`.env` on damballa and `urbanlens_production_app` recreated - a running container's `--cpus` is
fixed at creation. **Not yet re-measured.** Nothing here confirms 4 cores actually pushes the
capacity ceiling past 500 concurrent users; only `capacity-content`'s original measurement exists,
and it was against 2 cores, on the perf environment - never on damballa. Re-running
`tests/perf/k6/population.js` after the real deploy is what would confirm or refute that.

**This is a cap on today's real production, not a raise from it - checked 2026-09-17**:
`docker inspect urbanlens_production_app` on damballa shows `NanoCpus: 0`, same root cause as
P114's `CpuShares: 0` - the container predates the `cpus:` key entirely, so it is **currently
unbounded** on a 16-core host, not sitting at the compose file's 2-core default the way the perf
environment (and every other environment) is. Applying this fix does not repeat P125's
2-core-to-4-core story on production itself; it moves production from no cap at all to a 4-core
cap. That is very likely still an improvement - unbounded means it can be starved by whatever else
lands on the same host, same complaint as P114 - but it is a different claim than "production gets
more like the perf environment did," and worth the deployer knowing before they apply it. It also
means recreating production for P114's fix *without* this file would regress the app container from
unbounded to the bare 2-core default, which is worse than either state - the two fixes should be
deployed together.

Found and fixed in passing: `staging.sample.env` was stale against the current
`docker-compose.yml` - D16's Dragonfly/RabbitMQ broker migration added `CPU_SHARES__DRAGONFLY`,
`CPU_LIMIT__DRAGONFLY`, `MEM_LIMIT__DRAGONFLY`, and the three `_RABBITMQ` equivalents, and removed
the `_VALKEY` ones the sample file still carried. That silently broke
`test_staging_limits_are_below_production.py`'s own coverage guarantee (P114) - it was failing on
`main` before this batch touched it, unrelated to CPU_LIMIT__APP. Fixed by removing the three stale
`_VALKEY` entries and adding the six `_DRAGONFLY`/`_RABBITMQ` ones, each following the file's
existing halve-the-default convention.

Not fixed: the live measurement. This entry stays open.
