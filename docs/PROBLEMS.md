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

`id: P7` · `status: open` · `updated: 2026-09-23`

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

The automatic building sweep no longer depends on it (2026-09-23): `services.pins.auto_nest`
recognises the child pins and wikis it made by where they stand (`Pin.auto_nested_buildings`,
matched through `building_clusters.match_clusters`), so a renamed ref re-pins nothing
(`ResweepTests.test_a_ref_that_changes_between_responses_neither_duplicates_nor_merges`).
`ensure_building_places` still keys `Place` rows by `provider_key=ref`, and `find_matching_place`
refuses a geometry match already claimed by another ref from the same provider, so a renamed ref
leaves a second `BUILDING` place for the same footprint - reproduced by the strict xfail
`test_a_ref_that_changes_between_responses_reuses_the_building_place`. Floorplan lookups send a
place's `provider_key` as `building_ref` (`services/floorplans/resolution.py`), so they inherit the
same assumption; not re-tested here.

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

`id: P11` · `status: open` · `updated: 2026-09-18`

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
- ~~`shared/e2ee-client.ts:238` - the `e2ee-busy` class it sets during login has no CSS rule
  anywhere...`~~ **Stale, checked 2026-09-18.** `e2ee-busy` exists only in the built, uncompiled
  `frontend/static/dashboard/js/e2ee*.js` bundles now - the current `wireLoginForm` (`:237-246`)
  reuses `shared.btn.is-loading` instead (`:253`'s own comment says so), which does have a rule
  (`_buttons.scss:211`). Whatever fixed this did so as a byproduct of something else; the login form
  itself is fine now. **The unlock dialog half is not stale, though**: `showUnlockDialog`'s password/
  recovery-key path (`:960-984`, the `attempt()` closure) still gives no busy feedback at all during
  the ~1s Argon2id derivation - the submit button stays enabled and unstyled the whole time. Only the
  passkey button gets so much as `disabled = true` (`:952`), with no visual change to go with it.
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
- ~~`shared/map-layers.ts:198` has no `destroy()`...`~~ **Fixed 2026-09-18.** The reachable case
  traced to `shared/album-map.ts`'s show/hide toggle (`album-items.ts:679-680`, plus its filter-swap
  reload at `:864-865`), not a comment composer: every `initAlbumMap()`/`destroyAlbumMap()` cycle left
  a `document` click listener (closing the layers flyout) and, in `darkMode: "system"`, a
  `window.matchMedia` change listener - both rooted on long-lived globals `map.remove()` never
  touches, so each toggle added one more, forever. `createMapLayers` now names those handlers (plus
  the `layeradd`/`layerremove` pair and the previously-discarded `bindMapContextMenu` unbind) and
  exposes them as `MapLayersInstance.destroy()`, modeled on `shared/photo-map.ts:204`'s own
  `destroy()`; `album-map.ts` captures the return value and calls it in `destroyAlbumMap()`. The
  other four `createMapLayers` callers (`map-annotations`, `consensus`, `spotguessr`,
  `floorplan-editor`) each build their map from a one-shot `DOMContentLoaded`/`readyState` boot, not
  an HTMX-swappable entry point, confirming (not just assuming) they build one map per page load
  rather than repeatedly - nothing to leak, left as they were. **Checked and deliberately left
  uncleaned:** the `opts.loadingTarget` block's `layer.on("loading"/"load"/"tileerror", ...)`
  registrations sit on the tile-layer objects themselves (`streetLayer`/`topographicLayer`/
  `satelliteLayer`/`darkLayer`), which `createMapLayers` creates fresh per call and never exposes -
  once `destroy()` drops this closure and the caller drops the returned `MapLayersInstance`, nothing
  keeps those layer objects alive, so their listeners die with them. `album-map.ts` doesn't even pass
  `loadingTarget`, so this is moot for the one caller that calls `destroy()` today regardless. Test
  coverage was widened past the original 5 cases to also cover the context-menu unbind (all 5
  originals passed `contextMenu: false`, so that path had zero coverage) and the attribution
  animation-frame cancellation.
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

## P20 — `GoogleMapsGateway.import_pins_streaming` is ~280 lines of dead code, kept alive only because it's the sole caller of the AI label-style-suggestion feature

`id: P20` · `status: open` · `updated: 2026-09-17`

Previously titled "The legacy-CID repair leaves the CID on the wrong `Location`, so `by_cid()`
resolves it wrongly for everyone" - and before that "Residues left by the TEMPORARY legacy-CID
coordinate repair (found 2026-07-25)". Retitled because the CID-misplacement half described under
that title is now fixed; the decision below, about `import_pins_streaming` itself, is what remains.

`services/apis/locations/legacy_cid_coordinate_fix.py` lets a re-import move a user's
pre-2026-07-25 pins off the coordinates the old S2-decode guess put them on. It deliberately left
two gaps open when this module was first written. The first is now closed; the second - what to do
about `import_pins_streaming` - is not:

1. ~~**The CID stays on the bad `Location`.**~~ — **fixed in commit `8c16ffee2`**
   ("fix: P20 - repairing a legacy pin repoints its Google CID"). `GooglePlace.cid` is
   `unique=True`, so the repaired pin's new (correct) Location couldn't claim the CID while the
   old, wrongly-placed Location still held it - the backfill in `_create_pin_from_confirmed` was
   skipped for exactly this case, leaving `Location.objects.by_cid()` resolving that CID to the
   wrong Location for *every* user, and each re-import paying a fresh REData/Places resolution
   instead of a cache hit. The reason it wasn't done originally - repointing the CID mutates
   shared cross-user data off the back of one user's import - is answered by doing it inside the
   same transaction as the repair rather than not doing it: `legacy_cid_coordinate_fix.py` gained
   `repoint_cid_to_corrected_location(legacy_location, correct_location, cid)`, which clears the
   old `GooglePlace.cid` before setting it on the corrected Location, and
   `services/apis/locations/google/maps.py`'s `_create_pin_from_confirmed` CID-backfill guard now
   calls it instead of skipping the backfill when a `legacy_cid_location` exists. Covered by
   `tests/hypothesis/test_legacy_cid_coordinate_fix.py` (`RepointCidToCorrectedLocationTests`,
   `CidRepointOnRepairTests`), all passing at the time this landed; not re-run by this edit.

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


## P24 — A campus pin's CRIS coverage stops at the site footprint and per-pass caps, not the survey's full USN roster

`id: P24` · `status: open` · `updated: 2026-09-23`

Previously titled "A campus pin aggregates only the nearest CRIS building's media, not the
survey's full USN roster" (2026-08-05), and before that "CRIS media on a multi-building campus is
still only partial coverage".

**Narrowed 2026-09-23.** A site-scope pin's CRIS fetch (`plugins/builtin/cris_buildings.py`) no
longer stops at the nearest building. It searches 500 m, widens the search to the site record's
own footprint (the bounding box of its `geometry`, clamped at 1.5 km), asks REData to warm the
whole radius with the bulk `POST /cultural-resources/fetch-details/`
(`RedataGateway.queue_cultural_resource_details`), and adds every CRIS building inside the
footprint plus any building the site record's `linked_resources` names. Each attachment carries
the `subject` it documents, and Article > Sources lists the PDFs with `data-source-building`. A
lookup row that REData has already detailed (it has `attachments` or `detail_retrieved_at`) costs
no request.

**Still outstanding:**

- **The survey roster is not followed.** CRIS's own "every building on this site" list is a
  SURVEY resource's `USNs`, reached in two hops: a building's `linked_resources` names the survey,
  and the survey's own detail names the buildings as USN stubs with no position. REData's docs
  cite survey `12SD00541` as covering all 124 buildings of the former Hudson River State Hospital.
  It is left out because a survey can be a town-wide reconnaissance, and its positionless stubs
  cannot be checked against the site footprint, so following one would put unrelated buildings on
  the campus. It needs a way to tell a site survey from an area survey first.
- **Per-pass caps.** One pass live-fetches at most `_MAX_SITE_DETAIL_FETCHES` (12) undetailed
  buildings, inside a 50 s budget under the task's 110 s soft limit, and considers at most 40. The
  bulk queue warms the rest, but they only appear when the cache row is next refetched, because
  nothing re-polls a row that is already `documents_ready`.
- **A footprint-less site filters nothing.** With a point-only listing, every CRIS building in the
  500 m radius counts as the site's.

Fixed 2026-08-05: the CRIS Media gallery returned nothing at all, because
`RedataGateway.fetch_cultural_resource_detail` handed back REData's `{"detail_status", "resource"}`
envelope while every caller read `attributes`/`attachments` off it. Three narrower mismatches were
fixed at the same time: attachment `kind` was compared as `"PHOTO"`/`"DOCUMENT"` against REData's
lowercase values, `resource_type` as `"district"` against REData's `"building_district"`, and the
*first* building of a lookup was taken rather than the nearest one.

**Also worth checking operationally**: `fetch-detail/`, the bulk variant, and
`attachments/{id}/extract/` all require an API key holding `cultural_resources:write`, not
just `:read`. A read-only key gets 403 on all three and therefore yields zero attachments, however
correct this code is. The bulk call's 403 is tolerated: aggregation still proceeds on live fetches.

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

## P36 — 45 BEM modifiers are applied in templates with no CSS rule, so intended visual states never render

`id: P36` · `status: open` · `updated: 2026-09-18`

Previously titled "50 BEM modifiers are applied in templates with no CSS rule, so intended visual
states never render", before that "45 BEM modifiers applied in templates with no CSS rule", and
before that "46".

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

**Fixed 2026-09-18 (`45b5d7430`), the 5 this entry called "worth doing first"** - the three
`visit-*--pending` classes (a visit awaiting confirmation was indistinguishable from a confirmed
one), `ul-game-hud__group--lead`, and `btn-icon--primary`, the one applied in
`pages/site_admin_ui_components.html`, the component gallery whose entire purpose is to show what
each variant looks like:

- `visit-item--pending`, `visit-list--pending`, `visit-source--pending`
  (`_pin-detail.scss`): a pending row now gets an amber left-border and tinted background, and the
  "Pending" badge text goes amber. The pending list also loses the 15rem adaptive-pagination
  min-height it was inheriting from the confirmed-visits list - that reserve is for a paginated
  list, and this one is never paginated, so a single suggestion no longer sits in a mostly-empty
  box. Colors reuse the `$color-amber-500`/`700` tokens already used by the codebase's other
  `--pending` states, e.g. `friend-status-badge--pending`.
- `ul-game-hud__group--lead` (`_game_shell.scss`): given an explicit `justify-content: flex-start`
  alongside its `--center`/`--trail` siblings. This is a no-op by rendered pixels - flex's own
  initial value already produces `flex-start` - so nothing looked different before or after; it
  just stops relying on that coincidence. **Correcting this entry's own earlier gloss**: this
  modifier is not "the leading score" - checked against the markup, the `--lead` group wraps the
  game's identity badge (icon + game name), not a score. "Lead" is positional, the leftmost of
  lead/center/trail, matching its siblings' naming.
- `btn-icon--primary` (`_buttons.scss`): reuses `var(--ul-primary-color)` /
  `var(--ul-button-hover-bg)` / `var(--ul-button-hover-text)` - the same custom properties
  `.btn--primary` itself resolves through - so it inherits the app's one existing definition of
  "primary" instead of inventing a new color relationship.

Verified by removing all 5 from `bin/check_bem_modifiers.py`'s `_KNOWN_UNSTYLED` first and
confirming the check failed and flagged exactly those 5 and nothing else, then adding the rules,
recompiling (`bun node_modules/.bin/sass`), and confirming the check passed clean. Browser-verified
against the running `development_main` stack with a seeded throwaway pending `VisitSuggestion`:
computed style for background/border/color on the pending-visit elements matched the written rules
in both `data-theme="light"` and `data-theme="dark"`; the trivia/spotguessr/consensus HUD pages all
resolved `--lead`/`--center`/`--trail` to `flex-start`/`center`/`flex-end`; the button gallery's
`.btn-icon--primary` resolved to the same primary-blue as the page's existing `.btn--primary` and
swapped to the purple hover accent on `:hover`. `bun run check`'s `bem-modifiers` hook passed; two
unrelated pre-existing failures in that same run (`doc-line-refs` docs drift, `ruff-format-check` on
`apps.py`) are untouched by this change.

**45 remain.** This entry has not re-picked a "worth doing first" set for what's left; the next
session doing so should re-check `_KNOWN_UNSTYLED` rather than trust this sentence's count, since
the list drifts as other work adds or removes entries.

---

## P37 — A 2026-08-14 coverage run found 100 write handlers no test executed; its top roster is tested now, the rest are unmeasured

`id: P37` · `status: open` · `updated: 2026-09-18`

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

**2026-09-18, outside the dated roster:** `controllers/userprofile.py`'s `ProfileNoteView.post`,
`ProfileNoteEditView.post` and `ProfileNoteDeleteView.post` also had zero coverage - a later ad-hoc
check, not a re-run of the 2026-08-14 measurement. `test_profile_notes.py` now exercises all three:
content is created only when non-empty, a profile can't annotate itself, and - the real behavior
worth a regression test - `ProfileNote.objects.for_pair(author, subject)` keeps edit/delete scoped
to the acting author's own note about that specific subject, so neither another author's note about
the same subject nor the same author's note about a *different* subject is reachable through the
wrong URL.

**2026-09-18, same file, three more:** `ProfileTrustView.post`, `ProfileNicknameView.post` and
`ProfileLabelToggleView.post` were also zero-coverage - checked directly against the current URLs
(`profile.trust`, `profile.nickname`, `profile.label_toggle`), not against the stale 2026-08-14
report, since the report predates September's coverage work on this file.
`test_profile_trust_nickname_label_toggle.py` now exercises all three: trust rating set/update, a
zero or out-of-range rating clearing rather than erroring (the widget's own "clear" signal, not a
rejected value), nickname set/update/clear-on-blank, an over-length nickname refused before it
reaches the database, and self-annotation refused on all three. The label toggle's two real
regression tests: a `Label` another author owns is not reachable even by its correct id, because
`Label.objects.visible_to(author)` excludes it before `get_or_create` ever runs; and a label of the
wrong `kind` (a category, not a person-label) is refused the same way, so a category or status label
cannot be attached to a profile as if it were a person annotation.

**2026-09-18, external API this time:** a fresh survey of `docs/reports/2026-08-14-view-coverage.md`
against the current suite (the report itself is stale - several of its other listed handlers turned
out already covered by unrelated feature work since) found `external_api/views_messaging.py`'s
`GroupMembersView.delete` (route `messages.groups.members`) and `MessageDetailView.delete` (route
`messages.detail`) still genuinely uncovered: `test_external_api_messaging.py` exercised `POST`
and `GET` on the first but never touched the second at all. Added to that file rather than a
new one, since its `MessagingBaseTestCase` fixture already fit both. `GroupMemberRemovalTests`
covers the real permission split - the creator can remove anyone, a member can remove only
themselves (leaving), a non-creator cannot remove someone else, and removing a non-member is
refused - and `MessageDeleteTests` covers the sender-only `?scope=everyone` vs. recipient-only
`?scope=self` split (including the default scope, an invalid `scope` value refused with 400, and
an unknown message id refused with 404), so a sender cannot hide a message only for themselves
and a recipient cannot delete it for the sender too.

**2026-09-18, two more in the same file:** the same survey named `GroupsView.post` (route
`messages.groups`, create a group) and `GroupDetailView.patch` (route `messages.groups.detail`,
rename a group) as further candidates - checking the whole test tree, not just
`test_external_api_messaging.py`, mattered here: a sibling file, `test_external_api_group_controls.py`,
already covers `GroupMessageReactionView`, `GroupMessageDetailView.delete`, `GroupLeaveView`,
`GroupMuteView`, and `ConversationMuteView` in full, which a single-file grep would have missed.
`messages.groups` (POST) and `messages.groups.detail` (PATCH) genuinely had zero coverage anywhere,
though - `GroupCreateTests` covers creation (a named member is added, an unknown slug is refused,
naming only yourself is refused since the creator doesn't count as a member, a whitespace-only name
is refused, and a read-scope-only token is refused with 403), and `GroupRenameTests` covers the
rename path (any active member may rename, a non-member gets 404 rather than 403 since the view
checks membership before calling the service, an unknown group uuid is 404, a whitespace-only name
is refused, and read scope alone cannot write). `GroupPinShareView.post` (route
`messages.groups.share.pin`) is still uncovered anywhere and was left for a later batch.

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

`id: P56` · `status: open` · `updated: 2026-09-18` · `corrects a "zero violation reports" claim measured with the wrong browser API`

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
| `basemaps.cartocdn.com`, `tile.opentopomap.org`, `server.arcgisonline.com`, `services.arcgisonline.com` | map tiles | no CORP, `ACAO: *` - needs `crossOrigin` on the Leaflet layer |
| `www.gravatar.com` | avatar preview | no CORP, `ACAO: *` - needs `crossorigin` on the `<img>` |
| `en.wikipedia.org`, `nominatim.openstreetmap.org` | `fetch()` | no CORP, `ACAO: *` - already fine, `fetch` is CORS-mode by default |

`tile.openstreetmap.org` dropped from this row 2026-09-17: P126 moved every in-app tile reference off
it onto `basemaps.cartocdn.com`, which already shared this row's identical no-CORP/`ACAO: *` shape -
the finding is unaffected (`grep -rn "tile.openstreetmap.org" src/` now matches only a comment and a
regression test in `map-layers.ts`/`map-layers.test.ts`, not a live reference).

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
48 Leaflet tiles from `server.arcgisonline.com` and `*.tile.openstreetmap.org` (the latter no longer
one of the app's tile hosts as of P126, 2026-09-17 - its replacement, `basemaps.cartocdn.com`, carries
the same no-CORP/`ACAO: *` shape, so this measurement's conclusion is unaffected) with `crossorigin`
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

**Re-attempted 2026-09-18, on this checkout's `development_main` dev stack: "no key configured" was
never actually the blocker, and one existing claim in this entry needed correcting.**

A key is configured (`UL_GOOGLE_PUBLIC_API_KEY`/`UL_GOOGLE_UNRESTRICTED_API_KEY`, both wired
through `settings.google_public_api_key`/`google_unrestricted_api_key`, confirmed non-empty and in
the right format for a real Google API key - `AIza`-prefixed, 39 characters). Whether it's *valid*
for these two specific APIs (unexpired, unrestricted to the wrong referrers/APIs) is still untested
- the attempt below never got far enough to make that call. What it does settle is narrower but
still useful: this environment isn't missing a key entirely, which the "needs a valid key" framing
above could be read as implying. What actually blocked a full re-measurement of the Street View
embed and Maps JS runtime imagery is unrelated to COEP or to the key either way:
`pin.street_view`/`pin.satellite_view` (`controllers/pin.py::_render_media_carousel`) poll a
placeholder until a Celery task warms the provider cache for that pin's coordinates
(`panel_sources()[service_key].is_ready(pin)`), and this checkout's running containers
(`docker ps -a`) include none of `docker-compose.yml`'s `celery-worker`, `celery-worker-bulk`,
`celery-worker-panels`, or `celery-beat` - only `celery_metrics` (a metrics exporter, not a task
consumer). The task queues and nothing drains it, so the panel polls forever - a property of which
containers happen to be up on this dev slot right now, not a bug in the panel or the COEP work.
Left unmeasured again, but for a documented and actionable reason instead of a stale one: starting
`celery-worker-panels` on this stack (not done here - it wasn't this checkout's call to make
unprompted) is what the next attempt needs first, whatever it then finds about the key.

**One thing was measured, and it corrects a specific claim in the "measured in a browser... zero
violation reports" paragraph above.** That 2026-09-06 measurement listened for
`securitypolicyviolation` DOM events, which only ever fire for CSP - the browser never dispatches
one for a COEP/CORP mismatch. The correct listener is the Reporting API
(`new ReportingObserver(..., {types: ["coep", "coop"]})`), and using it against the same map tiles
this session (`basemaps.cartocdn.com`, `server.arcgisonline.com`) under the same
`Cross-Origin-Embedder-Policy-Report-Only: credentialless` shows a `"corp"`-type COEP report fired
for every single tile load - dozens per page load, not zero. **This does not change the
recommendation.** Every one of those requests still resolved (confirmed: no failed request, no
network error, the tile still renders) - `credentialless`'s fail-open behavior for a no-CORP
resource is exactly what the earlier measurement's "zero failed requests" half already established,
and that half holds up. What's corrected is narrower: reports are not silent the way "zero
violation reports" implied. That only becomes practically relevant if this app ever wires up a
`Reporting-Endpoints` header to actually collect these - whoever does that should expect routine,
harmless COEP noise from every map-tile load, not silence, so as not to mistake expected volume for
an incident.

(Unrelated to COEP, found while isolating this measurement and not chased further because it's
already tracked: loading the full pin-detail page transiently hit `FATAL: too many connections for
role "ul_web"` against this shared dev Postgres. That's P53's already-open finding - the same page's
panel fan-out - reproducing again, not a new problem.)

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

`id: P59` · `status: open` · `updated: 2026-09-18`

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

**Checked 2026-09-18: the disposable environment is gone, and re-provisioning a fresh one can't
substitute for it.** `dev_env.py list` no longer shows an `ae97b86` slot - it's been torn down, so
the specific row this entry names can't be directly re-queried even with the corrected
`thumbnail__icontains` lookup. Re-provisioning a new ephemeral account and re-running the spec
wouldn't answer the same question either: the mechanism this entry's leading hypothesis names
(P58's defect #2 - deleting the superseded file before the row was updated to stop naming it) was
fixed 2026-09-06, so no upload made after that date can enter this broken state - only data written
before the fix could be, and a fresh account has none. A real answer needs a database that actually
has pre-2026-09-06 history (staging or production), which this diagnostic pass deliberately didn't
touch - CLAUDE.local.md treats both as production, and a "how many rows are broken" count doesn't
justify that on its own for a `status: open`, already-explained, low-priority entry. Left open; the
concrete next step, if anyone wants to spend a production/staging read on it, is a `thumbnail`/
`marker_thumbnail`/`analysis_thumbnail` sweep cross-checked against whether the named file actually
exists in storage.

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

`id: P69` · `status: open` · `updated: 2026-09-18`

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

- **Safety check-ins overview, DM conversation list, achievement catalogue, and Organize's
  Lists/Filters tabs** all follow the identical pattern with lower realistic ceilings or lighter
  per-row templates today. **Deliberately left, 2026-09-06**: each is bounded by something other
  than account age - the achievement catalogue by how many awards the *site* defines, the
  conversation list by friend count - and paginating a chat sidebar or an awards catalogue trades a
  theoretical ceiling for worse browsing. ~~Worth revisiting with a `RenderTimeScalingMixin`
  subclass each, which would answer "is a row cheap next to the page" with a number instead of a
  guess: `controllers/safety.py:314-431`, `controllers/direct_messages.py:710-734`,
  `controllers/achievements.py:98-113`, `controllers/pin_lists.py:214-246`.~~ **All four measured
  2026-09-18 - see below.**

  ~~`controllers/undo.py:58-112` (undo history, bounded by its 7-day window) was on this list~~
  **measured 2026-09-18, not just theorized: it's fine.** `test_undo_history_render_scaling.py`'s
  `UndoHistoryRowCostTests` seeds 3 then 9 more active, undoable `UndoAction` rows on top of a
  baseline empty render and asserts one row's marginal cost stays under 10% of the page's own
  zero-row render time - it passed. The panel's per-row template (`undo_history.html`) only touches
  plain deferred-payload fields (`model_label`, `object_repr`, `kind`, `created`, `expires_at`,
  `uuid`), matching `test_undo_history_payload.py`'s existing proof that the query itself defers the
  one expensive column. The pass doesn't hand back exact milliseconds - `assert_row_cost_bounded`
  only prints numbers when it fails - so what's recorded here is the verified conclusion (bounded,
  not paginated, and now checked rather than assumed), not invented figures.

  ~~`controllers/friendship.py:451-477` (the "view all friends" page, bounded by friend count) was
  on this list~~ **measured 2026-09-18, also fine.** `test_friends_page_render_scaling.py`'s
  `FriendsPageRowCostTests` seeds 3 then 9 more accepted `Friendship` rows, each to a profile with a
  populated `area` field, and passed the same 10%-of-baseline budget. `area` is an
  `EncryptedTextField` (`services/social/connections.py`'s `get_connections` selects it in one query
  with no N+1, but *decrypting* it is real per-row CPU work a query-count test alone would not
  catch) - worth seeding on purpose rather than leaving it null, since a null field skips the
  decrypt path entirely and would measure nothing.

  ~~`controllers/safety.py:314-431` (no auto-delete by default -
  `SafetyPreference.auto_delete_after_days` is nullable and defaults to "never") was on this
  list~~ **measured 2026-09-18, also fine.** `test_safety_home_render_scaling.py`'s
  `SafetyHomeRowCostTests` seeds 3 then 9 more check-ins, each with two emergency contacts, and
  passed the same 10%-of-baseline budget. The per-row card (`_checkin_card.html`) only reads plain
  fields and boolean properties (`status`, `title`, `checkin_by`, `contacts_locked`,
  `contact.display_name`) - none of it encrypted, unlike the friendship.py row above - so this
  confirms cheap template work rather than hiding a decrypt cost behind an all-null seed.

  ~~`controllers/direct_messages.py:710-734` (query count already proven flat by
  `ConversationListQueryScalingTests`, but that test can't see render-time cost) was on this
  list~~ **measured 2026-09-18, also fine.** `test_conversation_list_render_scaling.py`'s
  `ConversationListRowCostTests` seeds 3 then 9 more conversations (one message each way) and
  passed the same budget. Each row's `display_identity_for` -> `resolve_visible_identity` still
  calls `reverse()` per row even with the viewer's visible-profile set pre-resolved to avoid a
  query per row - that per-row Python cost is exactly what a query-count test can't see, and it
  stayed cheap.

  ~~`controllers/achievements.py:98-113` was on this list~~ **measured 2026-09-18, also fine.**
  `test_achievement_catalogue_render_scaling.py`'s `AchievementCatalogueRowCostTests` seeds 3 then
  9 more achievements, deliberately all on the same metric (`pins_created`) rather than a random
  mix - `progress_for_profile`'s `compute_values` computes each *distinct* metric once for the
  whole catalogue, so holding the metric fixed isolates the per-row list/template cost from the
  separate, real cost of a brand-new metric, which this test does not claim to measure.

  ~~`controllers/pin_lists.py:214-246` (also structurally invisible to
  `test_route_query_scaling.py`'s generic sweep, which hits `lists.list` without an `HX-Request`
  header and only ever exercises its redirect branch) was on this list~~ **measured 2026-09-18,
  also fine.** `test_pin_lists_panel_render_scaling.py`'s `PinListsPanelRowCostTests` passes
  `HTTP_HX_REQUEST="true"` through `assert_row_cost_bounded`'s `**extra` to reach the real panel
  render instead of the redirect branch, seeds 3 then 9 more of the profile's lists, and passed
  the same budget.

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

`id: P85` · `status: open` · `updated: 2026-09-18`

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
`misc` is a real change to how every model in the tree is typed, and it will surface errors that
have never been reported here, which is the point. What this did not anticipate: a 2026-09-18
attempt at exactly this plan (below) found that the conversion itself, with `misc` left untouched,
already surfaces new mypy errors against the tree's currently-clean baseline through a different
mechanism - django-stubs' related-manager resolution, not the dynamic-base diagnostic this entry
has been measuring throughout. Whether that mechanism is a regression the conversion introduces or
a pre-existing gap the conversion un-masks is not established (see the caveat below) - either way,
this should not ride along inside an unrelated commit, and it should not be treated as safely
separable from the `misc` re-enable either - there is no inert first step here.

**Attempted 2026-09-18 as a diagnostic, not a fix - reverted before commit, and it changes the risk
picture.** Converted the three abstract managers in `models/abstract/queryset.py`
(`DashboardManager`, `FrontendDashboardManager`, `PublicDashboardManager`, all docstring-only
bodies, i.e. exactly the "125... convert this way" shape above) from the class-statement form to
the assignment form this entry recommends. Scoped `mypy --enable-error-code misc
models/abstract/queryset.py` does exactly what the mechanical fix promises: 3 "Unsupported dynamic
base class" errors go to 0. But a full `mypy src/urbanlens` under the tree's actual config (`misc`
still disabled - not yet re-enabling it, exactly as this entry says not to do) went from the
current clean baseline (1128 files, 0 errors, confirmed via `git stash`) to **20 errors in 8
files** - from converting only the three abstract managers, nothing concrete.

Four are the queryset-generics gap this entry already names in "a smaller, separate half":
`get_for_profile` on `ImmichAccountManager`, `GooglePhotosAccountManager`, `FlickrAccountManager`
and `GoogleCalendarAccountManager` now returns the generic `_T | None` instead of the concrete
model, an "Incompatible return value type" that does not reflect a runtime bug - these managers are
genuinely bound to their models. One is `ImmichAccountManager._delete_undecryptable`'s
`self.model._meta.get_field("profile").column`, where `get_field` can return `ForeignObjectRel`
(no `.column`) - `[union-attr]`. A dict.get overload issue in `services/media/media_relevance.py`
and four union-attr/arg-type issues in `management/commands/backfill_place_external_tags.py` look
like the same generics-gap category; not independently verified further.

**The other 8, all `[django-manager-missing]`, are the new finding.** "Couldn't resolve related
manager" on reverse-FK relations into `Floorplan`/`FloorplanFloor`/`FloorplanWall`/
`FloorplanOpening`/`Image` (`source_pool`, `reference_pool`, `floors`, `walls`, `rooms`, `markers`,
`openings`, `locks`, `floorplan_sources`, `floorplan_references`) - reached through
`FrontendDashboardManager`, one of the three converted here. The *concrete* managers on those
models were untouched: `class FloorplanManager(FrontendDashboardManager.from_queryset(FloorplanQuerySet)): ...`
is unchanged, still a class statement. Converting only the abstract base, with no change to any
concrete manager, is enough to make django-stubs report that it cannot resolve these related
managers, for models built on top of it.

Extending the conversion to check whether finishing the job (as the mechanical plan above intends
next) clears this instead correlated with more, not fewer, occurrences: additionally converting
`FloorplanManager` and `ImageManager` (also docstring-only, diagnostic only, reverted, never
committed) took the `[django-manager-missing]` cluster from 8 to 25+, newly reported against `Pin`,
`Wiki`, `Place`, `Profile`, `Location`, `SafetyCheckin`, `PinVisit`, `PinSuggestion`,
`DirectMessage`, plus two new "Could not resolve manager type for ..." errors not seen before. One
narrow, two-file `mypy models/floorplans/model.py models/images/model.py` run against that same
state also crashed internally (`NotImplementedError: Cannot serialize PlaceholderNode instance`,
mypy 2.1.0); the full-tree run against the identical code state completed normally with the 25+
errors above, so this looks like a partial-invocation/incremental-cache artifact rather than a
second confirmed blocker - flagged, not chased further.

**Open question, flagged rather than answered: revealed or introduced?** Everything above is
phrased as correlation, deliberately - "the conversion breaks related-manager resolution" is a
stronger claim than what was actually measured, and this entry should not be read to assert it.
Two explanations fit the same observation equally well. One: the class-statement form is genuinely
load-bearing for `mypy_django_plugin`'s related-manager transformer - the plugin's manager
detection keys off a `ClassDef` AST node, the assignment form doesn't produce one, and the
transformer silently fails to register the manager, which is a real defect the conversion
introduces. Two: these reverse relations were never resolvable to begin with - `DashboardManager`
and its descendants have been `Any` this whole entry's premise, so the plugin may already have been
falling back to some other, wrong path for `.objects` on every model in this hierarchy, silently
producing right-looking output for a reason unrelated to the fields it now can't find; converting
the abstract managers gave the checker enough real type information to notice a gap that was always
there. The growth from 8 to 25+ occurrences when the concrete managers were also converted is
consistent with either story - more of the hierarchy losing whatever mechanism worked before, or
more of the hierarchy finally being checked for real. Nothing measured this session distinguishes
them, and neither should be assumed pending the investigation below.

**This corrects "why this is filed rather than fixed" above.** That section's risk model was:
the syntactic conversion is mechanical and inert, and the only real risk arrives later, when `misc`
is re-enabled and starts reporting genuinely new findings across the tree. That is wrong for the
three abstract managers, which are the entry's own prerequisite for all 125 concrete ones:
converting just those three, with `misc` untouched, already changes mypy's output against the
currently-clean tree-wide baseline, and through a mechanism this entry had not measured -
django-stubs' related-manager resolution, not the `[misc]` dynamic-base diagnostic. Read "125...
have nothing but a docstring and convert this way" as a description of the AST shape, not of the
blast radius or the risk level; it is not evidence this is safe to batch. Before attempting this
again, abstract or concrete, it needs its own investigation into `mypy_django_plugin`'s
manager/related-manager transformer - specifically whether it keys off a `ClassDef` AST node rather
than a name-bound assignment (which would explain why the class-statement form, dynamic base and
all, is what currently makes the reverse relation resolvable), and separately, for each affected
relation, whether it was ever soundly typed before this session touched anything. Not yet
investigated further this session.

Found while resolving P84; two querysets (`GeocodedLocationQuerySet`, `WikiQuerySet`) were
parameterized there because their unused model import was the symptom of the missing type argument.

## P95 — One import preview entry is still read whole at up to 1 GB, and what parsing it costs is unmeasured

`id: P95` · `status: open` · `updated: 2026-09-18`

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
`media-worker` - and the slot keeps a second preview off the worker, not a photo job. Lowering
the per-entry cap is a product call - a heavy account's Google Takeout location history is large, and
nobody has measured how large. With one slot, a preview also waits behind every preview ahead of it,
each up to its 110-second soft limit.

**Measured 2026-09-18: the KML parse alone can exceed `media-worker`'s entire memory budget.**
`maps.py::takeout_kml_to_dict` is not streaming - it regex-copies the whole input
(`_KML_NAMESPACE_RE.sub`), runs a full defusedxml pre-parse purely to reject hostile documents (tree
discarded), then has fastkml build a complete lxml-backed object tree (`kml.KML.from_string`) before
a single placemark is read back out. That reading, not a guess from the code, is what the earlier
"risk is by arithmetic, not observation" line understated.

A synthetic Google-Takeout-shaped KML generator produced two files, and `takeout_kml_to_dict` was
called directly against each (not through `parse_import_preview_task` - a deliberate deviation,
noted below, since the point was to isolate the parse's own cost):

- **10 MB / 24,914 placemarks: completed.** `tracemalloc` measured a 45.0 MB peak inside the call -
  only 4.5x the input, but `tracemalloc` tracks Python's own allocator, not the C-level buffers
  `lxml` (fastkml's backing library) allocates. The process's own RSS, which does see those buffers,
  grew by 153.8 MB over the same call - a 15.4x ratio, and the more representative number.
- **100 MB / 247,959 placemarks: did not complete.** This run was made directly in the `app`
  container (2 GiB `mem_limit`, not `media-worker`'s 3 GB - see the deviation note below) so its
  memory could be watched with `docker stats` as it ran. Usage climbed from roughly 615 MB to 1.78
  GiB - 89% of that container's own limit - over at least two minutes (the shell call driving it hit
  its own 120-second foreground timeout before this finished) and was still rising when it was
  killed. The partial delta by that point - about 1.2 GB for a 100 MB input, a 12x ratio - had not
  yet caught up to the 10 MB run's 15.4x, but a completed parse at that same ratio would land near
  2.1 GB total, past this container's entire 2 GiB budget on its own. It was killed at that point
  rather than let run further, since this is the live development container, not a disposable one.

Extrapolating from the completed 10 MB point - a ratio the unfinished 100 MB run's own trajectory is
consistent with, not in tension with - a full 1 GB entry, the actual `_MAX_SINGLE_FILE_BYTES` cap,
plausibly costs on the order of 10-15 GB of peak RSS to parse. `media-worker` has a 3 GB `mem_limit`
shared across `--concurrency=2` workers, several times smaller than that. A single upload anywhere
near the 1 GB cap would very likely exceed the container's entire memory budget and get OOM-killed
by the kernel outright - not a slow parse, a crashed worker - and with two concurrency slots sharing
one cgroup limit, an unrelated concurrent job would be collateral damage.

Two things keep this from being the definitive number: (1) the 1 GB case itself was never run - after
the 100 MB trajectory, continuing to push a shared, live container toward its limit stopped being a
reasonable way to get a more precise figure, so the estimate above is an extrapolation from two
smaller points, not a measurement of the actual cap; (2) both runs called `takeout_kml_to_dict`
directly, bypassing `parse_import_preview_task` and the sandbox queue entirely, so they measured the
parse in the `app` container instead of `media-worker` (the guard that enforces this - `guard.py`'s
`check_untrusted_parse` - is in `warn`, not `deny`, mode, so it logged rather than blocked). The two
containers differ in memory limit (2 GiB vs. 3 GB) but not in what code runs or how; nothing about the
parse itself changes between them, so the ratios measured should carry over, but the two data points
are not from the container this would actually happen in.

---

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

`id: P111` · `status: open` · `updated: 2026-09-17` · `corrects the 2026-09-10 "worth fixing: the comment" note below - the comment was already gone`

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

**Correction, checked 2026-09-17: there is no comment left to fix.** `docker-compose.yml`'s app
service (`docker-compose.yml:190-199`) carries no sizing comment of any kind today - not the wrong
~140MB one, not a corrected one. History: the ~140MB comment was itself corrected to this entry's
measured ~347MB figure in commit `cebd6e5a3` (2026-09-10, 20:36 UTC, the same commit that rewrote
this entry), then removed entirely along with every other comment in the file two hours later by
commit `8f7772461` (2026-09-10, 22:38 UTC, "most, if not all of the comments were unnecessary...
If any comments truly are necessary here, reintroduce them individually"). So this entry's "worth
fixing" line has been stale since the day it was written. Nothing was changed in `docker-compose.yml`
this session - if a sizing comment is reintroduced, it should cite this entry and ~347MB, not the
superseded ~140MB figure.

Found while trying to run the neighbour suite on the real process model.

## P113 — 54 verified places where one account's ordinary use can degrade the site for everyone else, all fixed except 4 parked by decision

`id: P113` · `status: open` · `updated: 2026-09-17` · `supersedes the 2026-09-16 "1 still open" count: H56 closed by the D11 phase 3a gthread switch`

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

Re-verified against the code on 2026-09-13; H54 closed 2026-09-16, H56 closed 2026-09-17 (both below).
Of the 65 tracked findings, everything is closed except the four parked decisions. The closed items'
fix narrative - what each finding actually was, the corrections found while fixing it, and the
reasoning behind each choice - is in `archive/PROBLEMS-ARCHIVE.md` under 2026-09-15 (H54's own
narrative is dated 2026-09-16, H56's 2026-09-17, within that entry) rather than repeated here;
`notes/availability-audit-2026-09-11.md` (N21) has the original findings and the four downgrades from
the hostile-review pass.

**H54 closed 2026-09-16**: D16 moved the Celery broker off Dragonfly onto RabbitMQ, which removes the
broker's unbounded, no-TTL keys from the shared keyspace entirely rather than bounding them in place.
Narrative moved to `archive/PROBLEMS-ARCHIVE.md`; see
[`docs/designs/dragonfly-rabbitmq-pgvector-stack-adoption.md`](designs/dragonfly-rabbitmq-pgvector-stack-adoption.md)
(D16). H35/H38's separate size-limit risk on the same store is unaffected - see D16 for why.

**H56 closed 2026-09-17**: *"Under gevent a request that spends its timeout in non-yielding CPU takes
the whole worker down"* - D11 phase 3a (gthread) is built, not "designed and unbuilt" as this row
said. `package.json`'s `start` script already runs `-k gthread --threads 4` (landed in commit
`534055e5c`, 2026-09-15 - one day before this entry's own 2026-09-16 update, so the row was already
stale the day it was last touched). `bin/run_tests.sh -k "test_connection_budget_wiring"` passes (22
passed), including `test_the_worker_runs_threads`, which asserts the worker class is gthread. A
non-yielding request now blocks only its own OS thread, not the whole worker process. Narrative moved
to `archive/PROBLEMS-ARCHIVE.md`.

No rows remain in a "still open" table - the only findings left unfixed are the four parked by
decision below.

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

## P125 — This deployment's ceiling is between 500 and 1,000 concurrent users, and every wall it has hit so far was a container CPU limit: 175 on 2 app cores, 350 on 4, 500 on a 2-core database, and 1,000 on the same 4 app cores once the database was given 4 of its own

`id: P125` · `status: open` · `updated: 2026-09-20`

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


### 2026-09-21: the database got four cores, and the wall went back to the app tier

`CPU_LIMIT__DB` was raised to 4 and `c46b0c9f5` gave Postgres a configuration (`shared_buffers`,
`effective_cache_size`, `random_page_cost`, all env-driven). A full ladder then ran 100 → 250 →
500 → 1,000 concurrent users against the same 1,000-account, 471,756-pin population. **X28 has the
measurement and the arithmetic**; the part that changes this entry:

| hold | app mean cores (of 4) | app throttled | db mean cores (of 4) | db throttled | worst page p95 |
|---|---:|---:|---:|---:|---:|
| u250 | 1.04 | 0.54% | 0.55 | 0.00% | 179 ms |
| u500 | 2.01 | 2.92% | 1.13 | 0.08% | 505 ms |
| u1000 | 3.68 | 32.89% | 1.75 | 0.16% | 8,613 ms |

**500 concurrent users now pass**, with `search_panel` (P132) the only endpoint over any budget.
The sentence above this section - "the binding resource is Postgres' own `CPU_LIMIT__DB` of 2
cores" - was right about the cause and is now spent as a limit: at *twice* that load the database
is at 1.75 of 4 cores and throttles 0.16%.

**1,000 users fail on the app tier's 4 cores.** App CPU is linear at ~0.40 cores per 100 users
through the whole measured range, so 1,000 users demand ~4.0 cores against a 4-core limit; the 3.68
above is the ceiling with the bursts shaved off, not the demand. Nothing errored - 0.00% requests
failed, 100% socket handshakes at every level - it is entirely queueing.

So the lever is `CPU_LIMIT__APP=8` / `WEB_CONCURRENCY=12` / `MEM_LIMIT__APP=4g` (peak memory is
~290 MB a worker and flat in users), with `CPU_LIMIT__DB=6` alongside it: database CPU is linear at
0.226 cores per 100 users, so 1,000 users actually served want ~2.3 mean database cores - fine
against 4 - but peak/mean is 1.9, which puts the bursts near 4.3. None of that has been measured;
it is an extrapolation from a linear region.

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

### 2026-09-19: the map's tiles were never in the model, and they cost a third of the ceiling

Every run above measures a map load that asks for **zero basemap tiles**. `map.view` +
`map.document` + `map.pins.meta` is the page and its metadata; the ~24 images the viewport actually
draws were missing, and they are the most numerous request the site has. `tests/perf/k6/population.js`
now draws them (`drawViewport`, `lib/capacity.js:viewportTiles`/`tilesToFetch`), modelling the
browser cache the proxy's new `Cache-Control` earns: a tile this VU already holds is not re-asked.
`bin/run_capacity_tests.sh` seeds the grid first (`manage.py seed_basemap_tile_cache`, grid read out
of the k6 source so the two cannot drift), and `setup()` refuses to start if the first grid tile is
not a 200 - a run against an unseeded cache measures the vendor, not this deployment.

Three ladders, same host, same hour, same 471,756-pin population, 2-core app container:

| users | page p95, tiles drawn | page p95, no tiles | tile p95 | app cores (mean) | throttled |
|---:|---:|---:|---:|---:|---:|
| 125 | 458 ms | - | 70 ms | 0.59 | 5.1% |
| 150 | 417 ms | - | 47 ms | 0.56 | 2.1% |
| 175 | 372 ms | 329 ms | 53 ms | 0.66 | 3.1% |
| 200 | **2,485 ms** | 774 ms | **779 ms** | 0.91 | 16.6% |
| 250 | **4,450 ms** | 395 ms | **650 ms** | 1.09 | 25.3% |
| 500 | **10,641 ms** | - | 8,884 ms | 1.87 | 70.4% |
| 1000 | **39,536 ms** | - | 34,248 ms | 1.97 | 78.3% |

(`--no-tiles` runs the same journey with the tile leg off, which is how the two columns are one
difference rather than two dates. Requests failed stays at 0.00% until u1000's 0.03%; socket
handshakes stay at 100%. Nothing fails - it queues.)

**Tiles move the ceiling from 250 to 175.** The knee is sharp: 175 is comfortable and 200 is already
2.5x over budget with tiles drawn, while without them 250 still passes - reproducing
`capacity-content`'s September figure on today's tree, which is what makes the comparison a tile
result rather than a four-day drift.

**It is burstiness, not average load.** At u200 the app container averages 0.91 of its 2 cores -
46% - and is throttled 16.6% of the time; the tiles-off run at u250 averages more wall-clock work per
second and is throttled 3.4%. A viewport asks for its two dozen tiles in one instant, so the
container hits its quota inside a single 100 ms CFS window and everything behind it - other people's
pages included - waits out the rest of that window. Mean utilisation says there is headroom; the
tail says there is not.

**What a tile costs, measured** (`request_costs.txt`, whole 1,000-user run): 31,559 requests, 2.2x
the next most numerous view, 13.1% of all app CPU, 5.5 ms CPU and 2.0 queries each (`auth_user` for
the session, `dashboard_profiles` for `WriteSourceMiddleware`). A cold map open is therefore
~246 ms of app CPU - `map.view` 64.5 + `map.document` 38.6 + `map.pins.meta` 11.3 + 24 tiles at 5.5 -
against ~114 ms for one whose browser already holds the tiles. On 2 cores that is ~8 cold map opens
per second against ~17 warm ones, so 1,000 people opening the map in the same instant is ~123
seconds of queue cold and ~57 warm.

**Still not the database.** `pg_stat_activity` peaked at 16 of 100 backends (14 web), 44% idle, and
`ul_perf_db` never passed 1.10 mean cores. The 2026-09-15 conclusion holds: the constraint is the app
container's CPU allocation, and now also how unevenly a map load arrives at it.

### What this does not establish

- **The 4-core production override is still unapplied and unmeasured**, exactly as the 2026-09-17
  section leaves it. Everything above is 2 cores.
- **Tiles are served from cache here, never fetched.** The grid is seeded and `UL_REDATA_API_URL`
  points at a closed port in the perf environment, so no run touches REData. A deployment whose
  viewers pan onto uncached ground pays an upstream fetch that this says nothing about (`P131`).
- **The tile grid is one square of 1,024 coordinates** and each VU walks its own patch of it, so the
  tiles are always a cache hit somewhere warm. A real population spread over a continent has a colder
  cache than this.
- **`search_panel` is over its 500 ms fragment budget at every level measured, tiles or no tiles**
  (589-745 ms), including at 125 users where nothing else is close. That is an endpoint to fix, not a
  capacity ceiling - 34.9 queries and 997 mean rows, worst case 67 queries (`P132`).

### 2026-09-20: 4 cores, then a query per tile, then enough threads to use them - 175 to 350

Four changes, each measured on its own against the run before it, same host, same population, same
seeded grid, tiles drawn throughout. The point of doing them one at a time is that three of the four
would have been credited to the first.

| users | 2 cores, 12 threads | 4 cores, 12 threads | + deferred write actor | + 24 threads (`WEB_CONCURRENCY=6`) |
|---:|---:|---:|---:|---:|
| 175 | 372 ms | - | - | - |
| 200 | **2,485 ms** | - | - | - |
| 250 | **4,450 ms** | **2,132 ms** | **1,034 ms** | 391 ms |
| 350 | - | - | **2,724 ms** | 395 ms |

(Page p95, against D15's 1,000 ms budget. Bold is over it.)

**Cores were necessary and nowhere near sufficient.** The 2-core container was throttled 16.6% at
u200 and 78.3% at u1000; at 4 cores throttling falls under 1% at every level. It bought u250 from
4,450 ms to 2,132 ms - still over budget, and now for a different reason: `pg_stat_activity` showed
14 web backends, which is every one of the 12 request threads busy plus the pool's own. The
allocation was no longer what the app was waiting on; the number of threads allowed to use it was.

**A tile stopped costing a profile row.** `WriteSourceMiddleware` bound the signed-in profile for
every request, so each tile paid a `dashboard_profiles` query to name a writer it was never going to
have. Resolving that lazily (`current_write_actor`, commit `880c73077`) took a 30-tile viewport from
60 queries to 30 and a tile's SQL from 4.04 ms to 2.48 ms - and page p95 at u250 from 2,132 ms to
1,034 ms, which is more than the query itself costs, because it is 24 fewer round trips arriving in
the same instant. DB throttling at u500 fell from 22.68% to 13.60% in the same step.

**Then the threads.** `WEB_CONCURRENCY=6` (24 threads, gunicorn's 4 per worker) took u250 to 391 ms
and u350 to 395 ms - the first configuration in this entry that meets D15 at 350 concurrent users
with tiles drawn. Tile p95 is 27-32 ms. Cost: 26 Postgres backends and 1,618 MB resident against the
2 GB shared default, which is why `production.sample.env` now also raises `MEM_LIMIT__APP`.

**The wall moved to the database.** At 24 threads `ul_perf_db` is throttled 23.79% at u500, 84.78%
at u750 and 95.53% at u1000, against its own `CPU_LIMIT__DB` of 2 cores. Everything above u350 is now
a Postgres allocation question, not an app one - the opposite of where this entry started.

### What this does not establish

- **u500 is marginal and u750+ is contaminated.** The measuring host was saturated at those levels
  (idle 2-14%, load average 19-22) with the load generator beside the target, so those rows say the
  database is throttled, not by how much. They need a run with a separate generator before any number
  from them is quoted.
- **Production still has none of this.** `CPU_LIMIT__APP=4`, `WEB_CONCURRENCY=6` and
  `MEM_LIMIT__APP=3g` are in `production.sample.env` and deployed to the perf environment only.
  Production's app container is still uncapped (`NanoCpus: 0`) and on the default 3 workers.
- **The database's 2-core limit was not raised and not tested.** It is the identified next lever,
  untried.
- **A tile still costs one query** - `auth_user`, from `LoginRequiredMixin`. Removing it is a
  decision about how tiles are authorised rather than an optimisation, and the measured delta above
  suggests it is worth roughly 1.5 ms of a tile's ~4 ms. *(Done, 2026-09-20 - see below.)*

### 2026-09-20: a tile stopped asking who was asking, and the wall is now unambiguously the database

Two things loaded the viewer's row on every tile, so removing either alone would have measured
nothing: `LoginRequiredMixin`, and `WriteSourceMiddleware` asking whether there was a user to
attribute writes to. The middleware now defers the whole decision rather than only the actor, and
the tile view gates on the session plus a remembered verification
(`services/map/tile_authorisation.py`, `TILE_AUTH_TTL` 30 minutes).

Two runs 2.5 hours apart, same host, same 1,000-account manifest, same levels, nothing else
between them:

| per tile, whole run | before | after |
|---|---:|---:|
| share of app CPU | 8.3% | 5.6% |
| mean CPU | 3.1 ms | 2.2 ms |
| mean SQL | 0.8 ms | 0.1 ms |
| mean queries | 1.0 | 0.1 |
| p95 wall | 19 ms | 15 ms |

Mean queries of 0.1 rather than 0 is the shape the change was for: a session establishes its tile
access once and spends it for the rest of the viewport. The unit budget measures the same thing at
the other end - a cached tile went from 1 query to 0, and a warm 30-tile viewport from 30 to 1.

**What it bought was headroom, not latency** - tile p95 was already 27-32 ms and stayed there. At
the same concurrency the same VUs simply got through more:

| level | page views before | after | tiles before | after | app cores (mean) | throttled |
|---|---:|---:|---:|---:|---|---|
| u250 | 1,063 | 1,445 | 3,817 | 4,636 | 1.06 -> 1.01 | 0.22% -> 0.38% |
| u350 | 1,482 | 1,909 | 3,258 | 3,872 | 1.32 -> 1.18 | 1.48% -> 0.50% |

29% more page views at u350 on *less* app CPU and a third of the throttling. `map_view` p95 fell
167 -> 145 ms and `map_document` p95 494 -> 251 ms. Everything meets D15 at both levels except
`search_panel` (`P132`).

**u500, measured on its own** so nothing above it saturates the measuring host - which is what
contaminated the earlier ladder:

| | mean cores | limit | throttled |
|---|---:|---:|---:|
| `ul_perf_app` | 1.89 | 4 | 3.25% |
| `ul_perf_db` | 1.15 | 2 | **32.21%** |

It fails - `map_view` p95 1,622 ms, `basemap_tile` p95 1,144 ms - with nothing erroring (0.00%
requests failed, 100% socket handshakes) and the app container under half its allocation. The tile
itself holds up under that load at 0.1 queries and 2.1 ms CPU, so the queueing is not coming from
it. **The ceiling is between 350 and 500 concurrent users, and the binding resource is Postgres'
own `CPU_LIMIT__DB` of 2 cores.** Raising it is the next lever and has still not been tried.

### What this does not establish

- **Nothing between 350 and 500 was measured**, so "the ceiling is between them" is exactly as
  precise as it sounds. The 2026-09-21 ladder below measures 500 as passing on a 4-core database,
  which supersedes that reading.
- **Production has none of this.** `production.sample.env` now carries the app tier *and* the
  database sizing, and is deployed to the perf environment only. `urbanlens_production_db` runs
  bare `postgres` with no arguments and `NanoCpus=0` (verified read-only 2026-09-21), so it has
  neither the limits nor the `shared_buffers`/`jit=off` tuning; both need a recreate, not a
  restart.
- **The 30-minute window is a real trade.** A revocation that leaves the session record intact - an
  admin disabling an account, a password change invalidating other sessions - keeps drawing tiles,
  and nothing else, until the entry expires. Signing out, a flush or an expiry revokes immediately,
  because the gate re-reads the session every time.

## P132 — A global search read the whole site's rows to answer one viewer's question; fixed, and the ceiling moved off the database

`id: P132` · `status: open` · `updated: 2026-09-21`

Measured by the capacity harness (P125), not by a synthetic benchmark: `search.panel` was the only
endpoint over its D15 budget at *every* level the ladder ran, including 125 concurrent users where
nothing else was close, and including the runs with the map's tiles switched off. It was therefore
an endpoint cost rather than a symptom of the app tier being saturated.

**What was wrong.** Each expensive predicate was driven from the searched model across a to-many
relation, so the planner was free to start from the far side and read every alias, note, label or
trip comment on the site before discarding the ones the viewer cannot see. The same class of
defect as the resolved P100: cost set by how much *other people* have, not by the viewer's own
data. It was not the fan-out, and it was not an N+1.

| statement | before | rows discarded |
|---|---:|---:|
| pins, `aliases__name` probe | 52 ms | 55,084 |
| pins, `location__wiki__aliases__name` probe | 46 ms | 51,001 |
| pins, `notes__text` probe | 19 ms | - |
| pins, `labels__name` probe | 14 ms | - |
| comments, trip comments | 30 ms | 50,000, by `Seq Scan` |

**The fix: ask the crossing table, bounded by the viewer's own primary keys.**
`core/semijoin.py` resolves the table a path crosses - a reverse FK, or either side of an M2M
`through` - restates the `Q` against it, and runs it bounded by a concrete list of the outer
model's pks. The answer comes back as ids, so the statement never joins the relation and a row
matching through several related rows stays one row without `DISTINCT`. Shapes with no crossing
table fall back to a bounded join from the searched model. Reachable from any queryset as
`DashboardQuerySet.semijoin(path, condition)`, alongside `match_ids`, `by_ids`, `bounded_by` and
`own_pks`.

**How the bound is sent matters as much as what it asks.** `core/lookups.py` registers `__anyof`,
which emits a literal `= ANY('{1,2,3}'::bigint[])` for integer ids. With `server_side_binding`
on (`settings/base.py`), a bound array *parameter* is opaque at plan time, so the planner falls
back to a default selectivity estimate and flips small-table plans to a scan of the table the
filter names - that regressed nine P123 label tests before the literal form replaced it. A literal
array is one parse token *and* visible to the planner. Measured on 1,859 ids:

| bound form | planning | bound alone | a bounded alias probe |
|---|---:|---:|---:|
| `IN (N placeholders)` | 5.17 ms | 14.71 ms | 22.99 ms |
| `= ANY(%s)` parameter | 2.87 ms | 5.21 ms | 3.99 ms |
| `= ANY('{…}'::bigint[])` literal | **1.32 ms** | **2.91 ms** | **1.82 ms** |

**Result.** Whole-search SQL on the capacity population, HEAD against the working tree in the same
process against the same database, median of six rounds each:

| term | before | after | |
|---|---:|---:|---|
| `perf` | 239.5 ms | 136.5 ms | −43% |
| `river` | 274.0 ms | 106.0 ms | −61% |
| `pin 42` | 109.5 ms | 40.5 ms | −63% |
| `north` | 237.5 ms | 64.0 ms | −73% |
| **total** | **860.5 ms** | **347.0 ms** | **−60%** |

Plan-and-execute for one search went 121.1 ms to 69.5 ms. Result fingerprints were identical
across every term, and `probe_relation` and `probe_statement` were checked to agree on every
crossing path the app uses.

**The earlier claim that `enable_seqscan = off` costs about 2x is no longer true, and was rewritten
rather than corrected underneath.** It was measured against the *old* probe shape, where the hint
forced a full index scan of a 55,084-row alias table. In the bounded shape the probe reads only the
viewer's rows, and the hint costs about 0.23 ms - while still being what keeps the small-table
P123 case on its index. It is now entered by `DashboardQuerySet.semijoin()` itself rather than left
to callers: `apply_label_clause` ran outside any scope and read all 403 through-table rows instead
of 4, which only a rows-read measurement would have caught.

**Duplicate queries were never the cost, despite the count.** One search issued 27 repeated
statement shapes; they were 11.7 ms of 332. Commit `d581f2c9e` removed 13 of the 60 statements and
SQL time did not measurably change. Worth having for connection occupancy; not a latency fix.

**Ruled out, measured - do not retry.** The pin provider's match-then-fetch two-step: 5.92 ms
one-step against 1.44 + 4.43 = 5.87 ms two-step, a wash at this population.

### Regression cover

- `test_search_panel_cost_is_the_viewers_own.py` drives the whole engine as the panel does and
  fails if a stranger's rows change what the viewer's search reads, or if the statement count
  tracks anybody's row count. It covers every provider at once, including ones added later.
- `test_semijoin_probe_shapes.py` checks `resolve_crossing`, `restate`, the two probes agreeing
  per shape, and that `__anyof` sends a literal array the planner can see.
- `test_search_does_not_read_another_accounts_{commented_trips,pin_comments,visits}.py` cover the
  three access scopes that were rewritten.

### What this does not establish

- **Whether the ladder's p95 has moved.** The endpoint's SQL is down 60%, but at 1,000 users the
  app container is CPU-throttled and the database is not the binding constraint (P134), so the
  latency the ladder reports is not this endpoint's cost alone.
- **Whether a trigram index on the alias/note/comment text would help.** Still untested at this
  population, and now much less likely to matter: the probe no longer reads rows the viewer does
  not own, which is what made the leading-wildcard scan expensive.
- **The pin provider's remaining 27.65 ms statement.** It reads the viewer's own 1,859 pins. That
  is a cost proportional to the viewer's own data, which is the shape this problem was about
  removing - not a capacity defect.

## P133 — Every page inlined its JavaScript, so half the compressed bytes a logged-in user downloaded were re-sent on every navigation and could never be cached

`id: P133` · `status: fixed` · `updated: 2026-09-21`

**Fixed.** Seven blocks moved to files under `dashboard/frontend/static/js/`: the navbar and
drawer, the notification push listener, the search dialog, the check-in banner, the page
explainer, the tooltips, and the media-thumbnail fallback (which stays synchronous in `<head>`,
because its `onerror` fires during parsing). Every value that used to be interpolated into the
script now travels in the DOM instead - `data-dropdown-url`, `data-panel-url`,
`data-commit-url`, `data-csrf-token`, `id="ul-favicon-link"` - and the E2EE bootstrap reads its
URLs from `{{ e2ee_urls|json_script:"e2ee-urls" }}`, matching what `comment_map_config` and
`keyboard_shortcuts` already do.

Verified by rendering `/dashboard/map/` in `ul_perf_app` under the production staticfiles
manifest: all seven resolve to hashed URLs, all seven appear in the page, and every carried
value is present. 49 KB of inline script remains, of which 22.3 KB is the dev toolbar (admins in
a non-production environment only) and 9.1 KB is `json_script` data that is per-request by
nature.

Measured 2026-09-21 against the capacity population, rendering as production would (the site's
`environment_override` flipped to `production` for the probe and restored after, because the dev
toolbar alone is 27.5 KB of any other measurement):

| page | raw | gzipped | inline `<script>`/`<style>`, raw | the same, gzipped | share of the gzipped page |
|---|---:|---:|---:|---:|---:|
| map | 238,374 | 39,687 | 72,420 | 19,559 | **49%** |
| pin detail | 261,865 | 51,075 | 121,824 | 31,740 | **62%** |
| home | 103,487 | 23,810 | 61,701 | 17,186 | **72%** |
| organize | 431,477 | 42,623 | 54,022 | 15,193 | 36% |

The templates hold 793,722 bytes of inline script across 129 `<script>` bodies. 148,668 of those
bytes are in bodies containing no template tag at all - static JavaScript, movable to a file
verbatim - and another 122,885 in bodies with one to three, movable behind a data attribute or a
JSON island. `dashboard/partials/ui/_page_explainer_script.html` is 10,354 bytes with zero
interpolation and is included on every page; `_notification_push.html` is 9,260 with one
(`{% static "favicon.ico" %}`).

**Why this is a per-user cost and not just a page-weight one.** Inline script is part of the HTML,
so it is re-sent, re-compressed and re-parsed on every navigation. The same bytes as an external
file are fetched once and then served from cache - whitenoise already hashes and far-futures
`/static/`. A user clicking through five pages currently downloads roughly five copies.

Nothing new has to be built to do it: `themes/base.html:451` already loads `js/comment-map.js`
through `{% static %}`, so the pattern, the pipeline and the cache headers all exist and these
blocks simply did not use them. (A first extraction pass has since moved the media-thumb-fallback,
e2ee-oauth-enroll bootstrap, nav-dropdown, global-search-dialog, safety-checkin-banner, tooltips,
page-explainer and notification-push blocks onto this same pattern - see
`src/urbanlens/dashboard/frontend/static/js/`. The dialogs, and the 4+-tag bodies, are still open.)

**What this does and does not cost the server.** gzip of one page measured 2.7-6.0 ms of CPU
(Python's gzip at level 6; nginx's will be the same order), and nginx does compress `text/html` -
confirmed by response header, not assumed, since `gzip_types` in
`config/nginx/nginx.conf:111` does not list it and relies on nginx's implicit inclusion.
Template rendering is 56% of `map.view`'s CPU under cProfile (0.089 s of 0.160 s). **Not measured:
how much of that render is these blocks specifically.** They are `TextNode`s, which are cheap per
byte, so the render share is probably small and the honest claim here is bytes and client-side
parsing rather than server CPU.

**Dialogs are a second, different cost.** 15 to 20 `<dialog>` elements render fully into every
page for interactions most users never start: 58,593 bytes on map (24% of it), 199,292 on organize
(46%). They compress well - organize is 431 KB raw to 43 KB gzipped, 10.1x, because twenty dialogs
are repetitive - so unlike the scripts this is not mainly a wire cost. It is template render time
and DOM the browser builds and never shows. `#add-pin-dialog` alone is 21,524 bytes. The project
already prefers HTMX partials for exactly this shape (`dashboard/CLAUDE.md`), so fetching a dialog
on open is the house pattern rather than a new one.

### What this does not establish

- **That extraction is safe as a mechanical change.** 129 bodies is a large diff, ordering and CSP
  both matter, and the bodies with 4+ template tags (522,169 bytes, the majority) need real work
  rather than a move.
- **How much server CPU it would actually return.** See above - the bytes are measured, the render
  attribution is not.
- **Whether any of this shows up in the capacity ladder.** X28 measured wall time per fragment, not
  payload; nothing has been re-run to see whether a lighter page moves p95.

## P134 — At 1,000 users the app tier is CPU-throttled a third of the time while the database uses a quarter of its cores

`id: P134` · `status: partial` · `updated: 2026-09-22`

Every capacity problem recorded before this one was written as a database problem, and the fixes
were database fixes. The container figures from the 1,000-user ladder
(`tests/perf/results/before2-20260921T204935Z`, 6 workers, app and db each capped at 4 cores) say
the binding constraint is not there:

| hold | app mean cores | app throttled | db mean cores |
|---|---:|---:|---:|
| u100 | 0.44 | 0.35% | 0.19 |
| u250 | 1.05 | 0.42% | 0.34 |
| u500 | 1.96 | 9.61% | 0.57 |
| u1000 | **3.71 of 4** | **31.61%** | **0.91 of 4** |

Demand at u1000 is therefore about 5.4 cores against a 4-core cap; the database is at 23% of its
own. Every page and fragment goes **OVER** budget at that level, and they go over together, which
is what queueing behind a saturated tier looks like rather than any one endpoint being slow. It
also means a database win buys latency and headroom but does not raise the user ceiling until the
app tier stops being the thing that runs out.

**Where the app's CPU goes.** `request_costs.txt` in each results directory already attributes it
per view; the top of that table for this run:

| view | requests | CPU share | mean CPU ms | mean queries |
|---|---:|---:|---:|---:|
| `map.view` | 4047 | 16.4% | 67.2 | 18.0 |
| `pin.details` | 1774 | 10.4% | 97.8 | 27.2 |
| `search.panel` | 1298 | 7.1% | 91.2 | 27.5 |
| `organize.index` | 703 | 6.3% | 147.9 | 21.0 |
| `map.autocomplete.local` | 2994 | 6.1% | 33.7 | 9.8 |
| `map.basemap_tiles` | 46040 | 5.9% | 2.1 | 0.0 |

Page views are about two thirds of it, and no single page dominates - which points at what they
share rather than at any one controller.

**What they share is the navbar, and it is a quarter of a page.** Measured in `ul_perf_app` against
the capacity population, median of twelve renders: `/dashboard/map/` is 71.1 ms wall / 57.1 ms CPU,
and `partials/layout/header.html` alone is 17.2 ms wall / **14.9 ms CPU** of that. Rendering it a
second time inside the same request - so the memos are warm and only the nodes are paid for - costs
8.7 ms wall / 7.9 ms CPU, which splits the navbar into **8.2 ms CPU of data and 7.9 ms of nodes**.

**Which data.** Each deferred context value resolved in its own cold request scope (so shared
costs are counted once per row rather than shared, which is why the totals exceed a real page's
18 statements):

| deferred value | queries | wall ms | CPU ms |
|---|---:|---:|---:|
| `add_dev_toolbar` | 4 | 9.06 | 6.50 |
| `add_feature_access` | 5 | 8.73 | 7.04 |
| `add_unread_messages_badge` | 3 | 5.83 | 4.18 |
| `add_active_checkins_banner` | 2 | 4.11 | 3.00 |
| `add_unread_notifications_badge` | 2 | 4.00 | 3.56 |
| `add_direct_messages` | 2 | 3.07 | 2.39 |
| `add_distance_units` | 1 | 3.10 | 2.77 |
| `add_site_settings` | 1 | 2.56 | 1.70 |

The two dearest ask the same question - is this account a site admin, and what does its
subscription grant - and the answer changes when an admin acts, not when a user browses.

**Done: that answer is now cached per account rather than per page.**
`models/subscriptions/access_state.py` holds it, over the reusable `core/versioned_cache.py`: a
generation counter fetched alongside the entries in one round trip, so the acts that change access
retire every account's answer at once without anyone enumerating keys. The invalidation list is
the load-bearing part, because an account stripped of site admin has to stop being treated as one
on its next page - `SiteSettings`, `UserSubscription`, `RoleSubscription`, `SubscriptionRole`,
`Group` and `Permission` saves and deletes, the three permission/group `m2m_changed` senders, and
`User` saves that touch `is_active` or `is_superuser`. That last filter matters: `last_login` is
written on every sign-in, and bumping per sign-in would empty the namespace exactly when it is
most needed. `test_page_chrome_costs_the_same_at_any_scale.py` holds both halves - the warm-read
ceiling, and one staleness test per invalidation route.

**What bounds the risk is that this gates nothing.** The admin views enforce access through
`PermissionRequiredMixin` with `permission_required = "dashboard.view_site_admin"`, which calls
`has_perm` itself and reads nothing from the cache. A stale answer can show or hide chrome; it
cannot admit anyone to a page.

The settings singleton is still one statement a page. It is already memoised per request and
shared by three context processors and the controller, and caching the *row* across requests would
mean serialising a model instance whose fields change with the schema - a deploy-shaped failure
in exchange for one indexed single-row read. Not attempted.

### What the after-ladder says

Re-run at the identical configuration and population
(`tests/perf/results/after-20260921T233000Z`, same 1,000-account manifest, same 6 workers, same
4+4 cores, dev stack stopped for both). The run measures P133 and this entry together; the search
work of P132 was already in the before2 container.

**Five hundred concurrent users now fit inside budget, and did not before.** That is the result;
everything else is detail.

| hold | endpoints over budget, before | after |
|---|---:|---:|
| u100 | 0 of 25 | 0 of 25 |
| u250 | 0 of 25 | 0 of 25 |
| u500 | **6 of 25** | **0 of 25** |
| u1000 | 24 of 25 | 24 of 25 |

The six that cleared at u500 were `search_panel`, `pin_nearby`, `messages_unread`, `conversation`,
`organize_index` and `pin_details`. Proxy p95 at that hold went 0.441 s to 0.159 s.

At u1000 the tier is saturated in both runs, so its mean core count cannot move - what moves is
how much gets through it:

| hold | metric | before | after |
|---|---|---:|---:|
| u1000 | page views | 4,856 | **5,298** |
| u1000 | proxy requests | 29,273 | **31,616** |
| u1000 | proxy p95 | 6.272 s | **5.251 s** |
| u1000 | app mean cores | 3.71 | 3.74 |
| u1000 | app throttled | 31.61% | **42.63%** |
| u1000 | db mean cores | 0.91 | **0.82** |

The throttle figure going *up* while everything else improves is what a capped tier doing more
work looks like: 8% more requests are being served through the same four cores, so more
accounting periods end pinned at the quota. Read it with the rest of the row, not alone - 8% more
requests, 16% lower p95, and 10% less database CPU for them.

Per view, from `request_costs.txt` (whole run, both):

| view | mean CPU ms | mean statements | p50 wall ms |
|---|---|---|---|
| `map.view` | 67.2 → **58.6** | 18.0 → **14.4** | 235 → **121** |
| `home.view` | 81.7 → **71.1** | 34.0 → **31.4** | 388 → **186** |
| `pin.details` | 97.8 → **90.8** | 27.2 → **25.3** | 380 → **180** |
| `search.panel` | 91.2 → **77.6** | 27.5 → 31.0 | 218 → **150** |
| `organize.index` | 147.9 → **143.1** | 21.0 → **19.2** | 553 → **367** |

`search.panel` is the one statement count that rose, and it is not a regression by any measure of
time - its CPU fell 15% and its SQL time 37%. Two things account for it. The chrome saving applies
to it as it does to everything, but the search work of P132 was synced into the before2 container
from a working tree 47 minutes before it was committed, so the two runs did not run quite the same
search code; and the final form probes by matching ids and then fetching them, which reads the id
list as rows and issues a statement per provider to do it. That trade - more statements, less
planning - is the point of `match_ids`, and it is what the dedicated measurement in P132 priced.
The two runs are therefore a clean A/B for the chrome and not for search.

### The ladder repeated on the released tree

The after run above was measured from a container synced before the last commit of the batch, a
dead-code removal. `tests/perf/results/after3-20260922T004946Z` repeats it from a container synced
at `76b2b8154`, same manifest, same 6 workers, same 4+4 cores, same seven containers running.

**It reproduces.** u100, u250 and u500 are 0 of 25 over budget again, u1000 is 24 of 25 again, and
the database sits at the same fraction of its cores at every hold - 0.19, 0.32, 0.50, 0.82 against
0.19, 0.32, 0.52, 0.82. Per view, the statement counts land within a tenth: `map.view` 14.4 → 14.3,
`search.panel` 31.0 → 31.0, `home.view` 31.4 → 31.3, `organize.index` 19.2 → 19.2.

| hold | metric | after | after3 |
|---|---|---:|---:|
| u500 | proxy p95 | 0.159 s | 0.162 s |
| u1000 | page views | 5,298 | 5,386 |
| u1000 | proxy requests | 31,616 | 31,258 |
| u1000 | proxy p95 | 5.251 s | **3.873 s** |
| u1000 | app mean cores | 3.74 | 3.82 |
| u1000 | app throttled | 42.63% | 55.13% |
| u1000 | db mean cores | 0.82 | 0.82 |

Every one of the 24 u1000 endpoints has a lower p95 in the repeat, by 0.3 to 2.4 s, while the
throttle figure rises another 12 points. Two runs now show the same thing, so treat app throttle
percentage as a statement about the cap rather than about how well the tier is serving: it counts
accounting periods that ended at the quota, and a tier that is pinned either way pins more of them
when it gets more work done.

Two u500 endpoints read worse in the repeat - `pin_visits` 263 → 474 ms and `map_document`
202 → 366 ms. Both are the rarest requests in the journey (23 and 32 samples at that hold), so
their p95 is the second-worst of a couple of dozen draws; `pin_visits` p50 improved, 103 → 89 ms.

A third reading also fixes what the middle one cost to learn: running the ladder with the rest of
the compose project up - the Celery, media and AI workers, several of them crash-looping - spends
about 1.75 cores beside the app and turns u500 proxy p95 from 0.16 s into 4.81 s. The seven
containers named above are the configuration every run in this entry used; the sampler's container
table is what says which ones were actually up.

### What this does not establish

- **What to do about the other 7.9 ms.** Caching the navbar's *markup* would take it, but the
  markup varies by page (`nav_section`, active states) and by badge counts, so the key would have
  to carry the very values that are cheap to read.
- **Whether the badges should be cached too.** They are the values a stale answer is most visible
  in, and `nav_active_checkins` is safety-critical: a banner that hides an active check-in because
  a cache was warm is a worse failure than the CPU it saves. Not attempted deliberately. What they
  could have without a cache is one statement instead of four: the three badge processors and
  `add_direct_messages` each count rows for the same viewer, and every page renders all of them.
  Not attempted.
- **Whether prewarming would help.** Measured and mostly declined - see I6. The one case that
  would is the herd after a global bump, which costs each active viewer four extra statements
  once.
- **Whether `map.basemap_tiles` at 2.1 ms CPU × 46,040 requests is reducible.** It is 5.9% of the
  tier for something that issues no queries at all, so the cost is authorisation and framing. Not
  investigated.

## P135 — `streetview_check` calls Google directly, so it writes no `ApiCallLog` row and no rate limit applies to it

`id: P135` · `status: open` · `updated: 2026-09-21`

Found while surveying which server-side API calls could move to the browser (see I5,
`docs/reports/client-side-api-offload.md`). Unrelated to that question, and the survey does not
propose moving this one - Street View metadata needs a server-only key.

`MapController.streetview_check` (`src/urbanlens/dashboard/controllers/maps.py:455-484`, reached at
`map.streetview_check`, `urls.py:368`) reaches Google with `urllib.request.urlopen` rather than
through a `Gateway`:

```python
params = urllib.parse.urlencode({"location": f"{lat},{lng}", "key": api_key, "source": "outdoor"})
url = f"https://maps.googleapis.com/maps/api/streetview/metadata?{params}"
with urllib.request.urlopen(url, timeout=4) as resp:  # noqa: S310  # nosec B310
```

`dashboard/CLAUDE.md` states the rule this misses: the `Gateway` base wraps every request in a
rate-limited session that writes an `ApiCallLog` row with a `cost_estimate`, and code that bypasses
`self.session` must record itself via `rate_limiter.log_api_call`, as the AI services do. This
does neither, so three things that hold for every other Google call do not hold here:

- **No usage or cost is recorded.** The Street View metadata endpoint is free at Google's current
  terms, so the missing rows cost nothing today; what they cost is the ability to see the call at
  all in the usage view, and the ability to notice when the terms change.
- **No rate limit applies.** The comment above the opt-out gate says this "fires on every map
  right-click", which is a user-driven rate with no ceiling in front of it.
- **The failure is silent.** `except Exception: available = False` reports a timeout, a quota
  rejection and a genuine no-imagery answer identically, so a key that stops working looks like a
  world with no Street View in it.

The opt-out gate itself is correct and is not what this is about: the call is skipped when
`profile.external_apis_enabled` is false, on the same terms as `autocomplete_places`.

**Not yet established**

- Whether a `Gateway` subclass already exists for this host that it should be using, or whether
  the smaller fix is one `rate_limiter.log_api_call` call plus the existing Google service's
  limiter.
- What the real call rate is. No `ApiCallLog` rows exist for it by construction, so the only
  evidence available is nginx access logs for `map.streetview_check`.

## P128 — The add-pin dialog's label chips/suggestions interpolate `icon` into `innerHTML` unescaped, and `icon` is not a fixed enum like `kind` is

`id: P128` · `status: open` · `updated: 2026-09-17`

Found via adversarial review while verifying P92 (`map-page.js` → `map-page.ts` migration,
resolved, see `archive/PROBLEMS-ARCHIVE.md`). Unrelated to P92 and does not reopen it: the
migration is a straight port and carried this forward unchanged, neither introducing nor fixing it.

`src/urbanlens/dashboard/frontend/ts/entries/map-page.ts:5985-5986` (`_apdlgRenderSelectedChips`)
and `:6007-6009` (`_apdlgShowSuggestions`) build a label chip/suggestion row's `innerHTML` by
interpolating `b.icon` directly, with no escaping - `b.name` right next to it on the same line goes
through `_escHtml()`, `b.icon` does not:

```typescript
const iconHtml = b.icon ? `<span class="apdlg-chip-icon">${b.icon}</span>` : "";
chip.innerHTML = `${iconHtml}<span class="apdlg-chip-name">${_escHtml(b.name)}</span>...`;
```

Confirmed pre-existing, not something the migration added: `git show 4f2d494e3^:src/urbanlens/dashboard/frontend/static/js/map-page.js`
has the byte-for-byte same unescaped `${b.icon}` interpolations at its lines 4965 and 4988. This
likely predates `shared/inner-html-escaping.test.ts` even being able to see it - that lint's
`tsFiles()` only walks the `.ts` tree, so the old `.js` file was invisible to it. Migrating to `.ts`
made the lint see this file for the first time, which is why P92's diff had to add a `kindLabel`
entry to that test's `REVIEWED_SAFE` allowlist just to keep it passing - that only accounts for
`kind` (see below), it does not fix the `icon` gap.

**Why `kind` is safe but `icon` is not:** `Label.kind` is `CharField(choices=KIND_CHOICES, ...)`
(`src/urbanlens/dashboard/models/labels/model.py:57`), a fixed 5-value enum
(`src/urbanlens/dashboard/models/labels/meta.py`), and the write serializer validates it with
`serializers.ChoiceField(choices=KIND_CHOICES, ...)` (`src/urbanlens/dashboard/external_api/serializers.py:1339`)
- not exploitable. `Label.icon` is `CharField(max_length=50, null=True, blank=True)` with no
`choices=` at the model level (`model.py:52`), and `LabelWriteSerializer.icon` is a plain
`serializers.CharField(max_length=50, required=False, allow_blank=True, allow_null=True)`
(`serializers.py:1341`) - no choice restriction. `meta.py`'s `ICON_CHOICES`/`ICON_CATEGORIES` feed
only the picker UI's suggested options; nothing found in this pass enforces them server-side. A
user can plausibly set an arbitrary ≤50-char string, including HTML/JS, as their own label's `icon`
via this API.

This codebase's own shared implementation treats the same value as unsafe: `shared/label-picker.ts`
renders the equivalent chip/suggestion widgets and escapes icon every time
(`escHtml(icon)`/`escHtml(item.icon)` at `label-picker.ts:294,359,1073,1097`).
`map-page.ts`'s add-pin dialog has its own separate, duplicate implementation of this widget
(per an existing code comment, add-pin needs different behavior and can't import the shared one)
that diverged from that safe pattern.

**Not yet determined - needed before this can be scoped as a fix:**

1. Whether this is a real stored XSS against a different user, or only self-XSS: hinges on whether
   the label catalog/suggestion list a viewer sees is scoped strictly to their own labels or draws
   from a shared/site-wide/friends-visible pool. Not checked this pass.
2. Whether these two call sites are the only unescaped-icon interpolations, or whether other
   non-shared duplicate label-chip renderers exist elsewhere with the same gap. A full
   codebase search for other `.icon}`-style raw interpolations outside `shared/label-picker.ts`
   was not done.
3. Whether any other `Label` field besides `icon` has the same server-side-unconstrained,
   client-trusted-as-safe gap.

**Recommended fix**, once scoped: per this project's own convention (`CLAUDE.local.md`'s
"every vulnerability found gets a failing test reproducing the attack via TDD before the fix"),
write a failing exploit test first. Then either escape both interpolations in place
(`_escHtml(b.icon)`, matching `_escHtml(b.name)` on the same line - the minimal fix), or, to avoid
adding net-new duplication per the lesson P92 itself just drew, route this widget through
`shared/label-picker.ts`'s already-correct implementation if that turns out to be feasible.

## P130 — `ul_web`'s deliberate `NOCREATEDB` (D11) blocks the exact `docker exec ... pytest` workflow `CLAUDE.local.md` prescribes, on every dev slot that has converged its per-tier roles

`id: P130` · `status: open` · `updated: 2026-09-19`

Found while trying to run this session's new tests for the REData catalogue-wiring work (see
`docs/handoffs/redata-maplibre-catalogue-wiring.md`, N25): `docker exec urbanlens_development_main_app
/app/.venv/bin/python -m pytest ...` — the exact command `CLAUDE.local.md`'s "MyPy and pytest do NOT
work directly on this host" section gives for running anything that touches GeoDjango models — failed
outright before this session's manual fix below. Django's test runner could not create a test database
at all, for any test file, in that container.

**This is not a new defect.** It is D11's per-tier role rollout (`docs/designs/request-isolation-and-connection-budget.md`,
built and verified 2026-09-15) working as designed, and R29 already documents the consequence in its own
"What changes in practice" section: *"Tests in the app container fail: `docker exec <app> pytest` no
longer works, because `ul_web` cannot create a test database. `bin/run_tests.sh`, which runs as the
owner against the test-runner's own test-db, is unaffected"* (`docs/notes/database-roles.md:71`). What
R29 does not record, and what makes this still worth a `P#` four days later, is that nothing propagated
that consequence to the file that actually tells an agent how to run these tests: `CLAUDE.local.md` still
gives the broken `docker exec ... pytest` invocation with no mention of `bin/run_tests.sh` or of the
restriction, and nothing in the repo's tooling refuses to start with a clearer error - it fails as an
opaque `CREATE DATABASE` permission error, several layers down from the command that was actually typed.

Two separate causes, found together on `development_main`:

1. **`ul_web` has `NOCREATEDB`, by design.** `services/core/database_roles.py:213` sets `NOSUPERUSER
   NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS` on every per-tier login role
   `apply_database_roles` converges, and the app container logs in as exactly that role - confirmed
   directly, 2026-09-19: `docker exec urbanlens_development_main_app env` shows `UL_PROCESS_ROLE=web`,
   `UL_DB_USER=ul_web`. Loosening this is not the fix D11 intends: it was deliberately closed, and
   `apply_database_roles` already refuses to start if the limits it converges don't fit the server's
   non-superuser capacity.
2. **`template1` carried neither `postgis` nor `vector` (pgvector, D16).** A plain `CREATE DATABASE
   test_xxx` clones whatever `template1` has; `init.py`'s `enable_postgis()`
   (`src/bin/init.py:168-187`) runs `CREATE EXTENSION IF NOT EXISTS postgis` exactly once, against
   `self.db_name` - the main app database - and never against `template1`, and never for `vector` at
   all. Both fixes were applied together (below), not proven independently, so whether `CREATEDB` alone
   would have been enough - i.e. whether `ul_web`, as owner of a database it created itself, could have
   run `CREATE EXTENSION` there directly - was not isolated and tested separately.

**First attempted, then reverted, on `development_main` only, 2026-09-19** (a manual superuser
session against the `urbanlens_development_main_db` container, not committed anywhere):

```sql
ALTER ROLE ul_web CREATEDB;
\c template1
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
```

This did make `docker exec urbanlens_development_main_app /app/.venv/bin/python -m pytest
src/urbanlens/dashboard/tests/hypothesis/test_basemap_tile_proxy.py -k BasemapCatalogueTests -v`
(unique `UL_TEST_DB_NAME`) create its test database and run to completion - `8 passed, 14
deselected in 235.78s`. But `ALTER ROLE ul_web CREATEDB` is exactly the surface R29 says D11
deliberately closed, not a gap it left open by omission, so loosening it - even locally, even
temporarily - is the wrong fix rather than a scoped one. `bin/run_tests.sh` (R29's already-existing
answer, confirmed working this session: same file/selector, `8 passed, 14 deselected in 219.95s`
against the `test_runner` container's own owner-based role, no role change of any kind) does not
need it. **`ALTER ROLE ul_web NOCREATEDB` was run afterward, restoring the original grant** - the
`template1` extensions were left in place, since they only add capability (no role gained a
privilege it lacked before) and `bin/run_tests.sh`'s own test-db creation may already depend on
`postgis` being there by whatever path it uses; not verified independently which of the two
`template1` extensions, if either, `bin/run_tests.sh` actually needed to pass.

**Not done:**
- **The same check on any other dev slot.** Nothing about `development_main` is special - every slot
  goes through the same `db-setup` → `apply_database_roles` sequence, so a slot that has converged
  its roles since 2026-09-15 or was provisioned fresh after that date should hit the identical
  `docker exec <app> pytest` failure from a clean start, not just an old one catching up. Not
  verified against a second slot this session. `bin/run_tests.sh` is expected to be unaffected
  there too, by the same reasoning that held here, but that is inference, not a second measurement.
- **Reconciling `CLAUDE.local.md` with R29.** `CLAUDE.local.md` is outside this documentation tree and
  not edited here; its docker-exec pytest instructions should either name `bin/run_tests.sh` (R29's
  existing answer) or note the `CREATEDB`/extension prerequisite, whichever the eventual fix picks.

## P131 — REData's ~1.45s PBKDF2 key-check is fixed upstream (confirmed 2026-09-21); basemap tiles now pay 0.43–0.98s for cold-tile rendering instead, and the concurrency bound's own trigger condition is met for auth but not for that

`id: P131` · `status: open` · `updated: 2026-09-21` · `supersedes the 2026-09-19 "~1.45s PBKDF2 per call" claim below: REData shipped a hasher change (their T9, done) that removed it. Left open because the entry's own open items were about what to do once that happened, and that work is now live, not because the original defect is still present.

**Why `open` and not `fixed`/archived:** the thing this entry was created to describe — per-request cost on every authenticated REData call — has not gone away, it changed shape. Auth is now cheap, but a cold basemap tile is not, and two of this entry's three open items (the concurrency bound's value, the nginx-proxy question) were always contingent on this exact measurement. Archiving would lose the connection between the old number and the new one; `docs/README.md`'s "rewrite the claim" rule is followed by rewriting the section in place instead.

### Before: measured 2026-09-19 against `https://redata.urbanlens.org`, superseded by the table below

Each figure the median of three curl runs reporting `time_starttransfer`:

| Request | Result | TTFB |
| --- | --- | --- |
| `GET /api/v1/tiles/sources/` — no `Authorization` header | 401 | **0.061s** |
| `GET /api/v1/tiles/sources/` — `Bearer notarealkey` | 401 | **0.055s** |
| `GET /static/dashboard/style.*.css` — no Django auth at all | 200 | **0.044s** |
| `GET /api/v1/tiles/sources/` — **valid key** | 200 | **1.52s** |
| `GET /api/v1/tiles/street/14/4823/6037/` — **valid key**, second fetch of the same tile | 200 | **1.46s** |

At the time, a wrong key was rejected in 55ms and a valid one cost ~1.4s more — the same on both
the catalogue endpoint (a five-entry list out of a cache) and a tile REData had already cached, so
neither was doing ~1.4s of real work. The cause, read from REData's source as it stood then
(`src/redata/api/services/api_keys.py`, `authenticate_api_key`): keys were stored with Django's
`make_password` and checked with `check_password`, running the default password hasher —
PBKDF2-SHA256 at ~1.2M iterations under Django 6 — on every successful verification. A slow KDF is
the right choice for a low-entropy human password and the wrong one for a 256-bit random API key,
where a single fast digest is the standard answer.

### Now: measured 2026-09-21 from chiron against the same production host, median of the runs shown

| Request | Result | TTFB |
| --- | --- | --- |
| `GET /api/v1/tiles/sources/` — no valid key (`Bearer notarealkey`) | 401 | 0.077s (runs: 0.085, 0.077, 0.062) |
| `GET /api/v1/tiles/sources/` — valid key | 200 | 0.125s (runs: 0.265, 0.111, 0.125) |
| `GET /api/v1/tiles/terrain/14/4823/6037/` — valid key, first fetch | 200 `image/png`, 30158 bytes | 0.980s |
| same tile, four repeats | 200 | 0.110, 0.118, 0.130, 0.105 |
| `GET /api/v1/tiles/satellite/14/4823/6037/` — first fetch / second | 200 `image/jpeg` | 0.477s / 0.107s |
| `GET /api/v1/tiles/borders/14/4823/6037/` — first fetch / second | 200 `image/png` | 0.435s / 0.126s |

**The PBKDF2 cost is gone.** A valid key now costs ~0.05s more than an invalid one, not ~1.4s more.
Confirmed in REData's source, not inferred: `git show origin/main:src/redata/api/services/api_keys.py`
(sibling `/projects/UrbanLens/REData` checkout, fetched from `origin/main` — its working tree sits on
`feat/scout-campaign`, whose copy of this file is stale) now stores a bare SHA-256 under an `rdk1$`
scheme tag; the module docstring gives the same reasoning as above, in REData's own words. Keys issued
before the change still verify through `check_password` and are rewritten into the new format on
first use, so nothing had to be reissued. REData's `docs/INDEX.md` on `origin/main` marks the work
`T9`, `done`.

**What replaced it as the cost that matters here: rendering a tile REData has not served before.**
0.43s to 0.98s cold, ~0.11s warm, on the three raster layers measured above. `street` has no row
because production no longer serves it as tiles at all: `GET /api/v1/tiles/street/14/4823/6037/`
answers `400 vector_layer_not_served` in 0.10s, and the catalogue publishes `street` and `dark` as
`source_type: "vector"` with a `style_url` and **no** `url_template`. That is tile
rendering/caching inside REData, not authentication, and — like the old PBKDF2 cost — it does not
depend on a warm cache existing, so a first viewer of any given tile still pays it. Not re-measured:
whether it is CPU-bound rendering, an upstream fetch, or something else; this entry only has REData's
black-box timing, not a profile of its cause.

**What this changes for UrbanLens.** The fan-out shape is unchanged — a cold map viewport is still
~30 upstream requests, one page view — but the per-request cost dropped from ~1.45s to, on this
measurement, 0.43–0.98s for a genuinely uncached tile and ~0.11–0.13s for one REData has already
rendered. `basemap_tile_upstream_concurrency` (default 2, `src/urbanlens/UrbanLens/settings/app.py:607`)
still exists to keep that fan-out from occupying every gunicorn request thread in a process
(`--threads 4`, `gunicorn.conf.py`) and queuing the rest of the site behind a cold map load; nothing
in this session's measurement removes the need for some bound, only changes what number it should be.

**Open:**

- ~~The hasher change in REData.~~ **Done.** Shipped and confirmed above; this was the entry's
  original "actual fix" and is no longer open.
- **What `basemap_tile_upstream_concurrency` should be, now that the trigger condition is partly
  met.** This entry's own text set the trigger as "if TTFB for a valid key matches the 55ms an
  invalid one gets, the bound is no longer doing useful work" — that is now true for *auth*
  (0.125s valid vs. 0.077s invalid, both dominated by network/TLS, not by key verification) but
  **not** for a cold tile (0.43–0.98s, still far above the auth floor). The bound still has a job:
  containing cold-tile fan-out, not auth cost. The right value for that job is an open measurement —
  this entry does not have enough data to recommend one, and inventing a number here would be exactly
  the kind of unmeasured claim this rewrite exists to correct. `historical_tile_upstream_concurrency`
  (same file, same default) was set independently and is out of scope for this measurement.
- **Whether the tile proxy belongs in Django at all.** nginx `proxy_pass` with the key injected at
  that layer and `proxy_cache` in front would take the fan-out off the request threads entirely, at
  the cost of moving per-user authorization to `auth_request`. This item was explicitly conditioned
  on the latency *not* being fixed at the source — that condition has now **partly failed**: it *was*
  fixed for auth, so a proxy_cache in front of REData would no longer be buying its way around a
  PBKDF2 tax, only around REData's render-and-cache cost for tiles it has not served before. Still
  not attempted; whether the remaining 0.43–0.98s cold-tile cost is worth the infrastructure move is
  a separate, smaller question than the one this item was originally asking.

## P136 — A custom tile-concurrency gate and Leaflet's abort path do not compose: Leaflet drops a tile by overwriting its handlers, so a fast zoom held every slot and the map stopped loading tiles entirely

`id: P136` · `status: fixed` · `updated: 2026-09-22`

Reported from the browser: zooming out quickly left the map blank, zooming out one notch at a time
worked, and once tiles were on screen zooming *in* never fetched detail again until the page was
reloaded. Both raster base layers behaved the same way. It began with the tile work in this range
(`ebe547fd3`..), not with anything in Leaflet.

**`map-layers.ts`'s `createTile` takes a slot from `own-tiles.ts` and gives it back from the
`onload`/`onerror` it installs. Leaflet does not drop a tile by firing either one.** It overwrites
both with a no-op of its own: `_abortLoading` does that to every tile off the new zoom and
`_removeTile` to every one pruned. Neither handler runs again, so a tile dropped mid-request never
reached `finish()` and kept its slot until the 30s watchdog. A fast zoom abandons a viewport at a
time - more than the six slots that exist - so the queue had none left and the zoom the map had
moved to was never requested at all. A slow zoom stayed under the leak rate, which is exactly the
shape of the report.

Worse for detection: an `<img>` with no `src` reports `complete === true`, and `_abortLoading` only
removes a tile it finds incomplete. A tile abandoned while it was still *queued* was therefore left
in the grid with its handlers clobbered and **no event fired at all** - neither `tileabort` nor
`tileunload`. Nothing about that tile is observable from the outside; whether the handlers are still
the ones `createTile` installed is the only signal the two paths share.

Measured on `k3s-staging` before the fix, five wheel notches 120ms apart:

| | tiles | painted | src-less | requests made |
| --- | --- | --- | --- | --- |
| after a fast zoom out | 24 | **0** | **24** | 2 |
| +20s (watchdogs expiring) | 24 | 0 | 24 | 6 |
| after a subsequent fast zoom in | 24 | 0 | 24 | **0** |

After `674a4b053` and `82d451b21`, same page, same gesture, and through a 20-wheel alternating
stress: `QUEUED=0`, every current-level tile painted, `unreported=0` and `_noTilesToLoad() === true`
in Leaflet's own grid, and no DOM tile Leaflet no longer tracks.

Three parts to the fix:

1. **Listen for the events Leaflet does fire.** `tileunload` and `tileabort` carry the element, so a
   `WeakMap` from element to releaser hands the slot back for every tile Leaflet removes.
2. **Check handler identity for the case no event covers.** `tile.onload === onLoad` is false once
   Leaflet has clobbered it, so a queued tile that is handed a slot afterwards returns it instead of
   spending a request on a zoom the map has left.
3. **Still call `done`.** Leaflet counts a tile as outstanding until its `done` runs and prunes the
   ancestor levels it holds underneath only when none are, so an abandoned tile that merely gave its
   slot back left the layer permanently mid-load.

**Not a fault, found while measuring:** a map showing tiles from several zoom levels at once is
normal. Leaflet stacks retained ancestor levels *beneath* the current one - measured at zIndex 16
and 20 under the current level's 21 - so a mixture in the DOM is only evidence of a problem when the
current level is itself short of tiles. An early version of the check flagged this and was wrong.

**Open, not chased:** `OWN_TILE_CONCURRENCY` is 6, chosen against the proxy's upstream budget. With
the leak gone a viewport fills well within a second, but the proxy served 42 concurrent cold tiles
at 200 in 2.9s during P134's work, so 6 may now be narrower than it needs to be. No measurement here
either way.

## P137 — Every map opened on satellite kept a live vector base underneath it, so a metered basemap was billed for tiles nobody could see, on every pan and zoom of the session

`id: P137` · `status: fixed` · `updated: 2026-09-22`

Reported from Protomaps' own usage page: 3,791 tile requests in a day, almost all of it from one
session's testing, against a 1,000,000/month quota. `street` and `dark` resolve to
`api.protomaps.com` when `protomaps_api_key` is set, and the browser fetches those straight from the
CDN - this origin proxies none of them, so neither the Dragonfly cache nor the CDN rules in front of
`/dashboard/map/basemap-tiles/` apply. Every one is quota.

**`syncBaseLayer()` was called before `applyInitialLayers()`.** At that moment no opaque base was on
the map yet, so `map.hasLayer(satelliteLayer)` was false and it added the street-or-dark base.
`applyInitialLayers()` then added satellite on top and nothing synced again, so the MapLibre map
underneath stayed live and attached for the rest of the session, following every zoom. Clicking a
base button ran `setBase()` → `syncBaseLayer()` and fixed it - which is why it never showed up in
testing that started by choosing a layer.

Measured on `k3s-staging`, one session: page load on satellite, two zoom-outs, switch to terrain,
two more zoom-outs, then street.

| | before | after |
| --- | --- | --- |
| page load, satellite active, before any gesture | 13 (12 tiles + style) | **0** |
| two zoom-outs, satellite active | 23 | **0** |
| two zoom-outs, terrain active | 10 | **0** |
| switching to street, then two zoom-outs | 15 | 11, then 0 |
| **session total** | **46** | **11** |

After the fix no MapLibre map is constructed at all until a vector base is selected
(`glMaps=0`), which is the observable that distinguishes "hidden" from "not built".

Second cause, in the same function: **topo kept its base deliberately**, on the reasoning that its
pane is filtered rather than opaque. That held while topo was drawn from `World_Hillshade`, a relief
layer meant to go *under* a map. It is now `World_Topo_Map` - the map itself, opaque JPEG over the
whole viewport (P136's vendor swap) - so the base below is fetched and then covered. A CSS filter on
an opaque image does not make it transparent, so the base did not show through either way.

The MapLibre engine (`maplibre-layers.ts`) resolves one style per base with no stacking, so it never
had this shape.

**Not chased, worth knowing:**
- **The style document carries no `cache-control` at all** (tiles carry `public, max-age=14400`), so
  `styles/v5/<theme>/en.json` is re-fetched on every page load that draws a vector base. One request
  per load, and whether Protomaps bills it was not established.
- **What a legitimate street session costs** was not turned into a per-user monthly figure. The
  measurement above says ~11 requests for one viewport plus a zoom; the quota question is how many
  users pick street or dark at all, now that nothing fetches it unasked.

## P138 — Most maps ignored `Profile.default_map_view` and opened on street, because six call sites each hardcoded their own fallback instead of reading it

`id: P138` · `status: fixed` · `updated: 2026-09-22`

An audit of every basemap construction site (~20 maps) found only four wired to the viewer's
`default_map_view` setting: the main map, pin detail, trip detail, and Memories. Ten ignored it
outright, most because the call site never passed a `defaultBase` at all: the album photo map
(hardcoded `"remember"`), the wiki page's annotations map, spotguessr's guess map, consensus'
round map, both `pin_lists` maps, profile/common_pins, the vault photo-pin confirm map, pin_share
detail, and the two pin-select maps in memories. `floorplan-editor`'s `"satellite"` was checked and
is deliberate - a floorplan is traced over imagery - and was left alone.

**`createMapLayers()` (`frontend/ts/shared/map-layers.ts`) substituted the literal `"street"`
whenever a call site passed no base at all.** That literal dates to the `55a527c12` "Merge v0.4.0b0"
merge (Jul 2026) - it was the function's own hardcoded default from before `default_map_view`
existed as a setting, and nothing revisited it when the setting landed. The same literal was then
spelled independently in five more places, so there was no single point to fix it: `map-layers.ts`
`applyInitialLayers`, `maplibre-layers.ts` `readInitialState`, `normalizeBase`,
`services/map_pins/page_config.py:114` (`str(context["default_map_view"] or "street")`), and
`{{ default_map_view|default:"street" }}` in `trips/detail.html` and `memories/index.html`.

Cost, not just a wrong default: `street` and `dark` resolve to the metered Protomaps vector base
where a key is set, so "we don't know the viewer's preference" was also the answer that spends
quota - see P137.

**Fix: one wiring point instead of per-page edits.** The `{% map_layers_panel %}` tag
(`templatetags/map_components.py`) is now `takes_context=True` and emits `data-default-base` on the
panel root, which every one of these pages already renders. The engines read it through a new
`resolveConfiguredBase(root, requested)` only when the call site names no base at all. The tag also
clamps the value to the bases that page's panel actually offers a button for - pages declare their
own set, e.g. `{% map_layers_panel "street,satellite" %}` - so a viewer whose setting is
topographic is not stranded on a layer with no button to reach it. A new shared constant,
`DEFAULT_BASE_LAYER = "satellite"`, matches `Profile.default_map_view`'s own model default and is
now the only remaining hardcoded fallback.

**Deliberately not changed, and why:**
- **`normalizeBase`'s own `"street"` fallback.** It answers "this identifier is not one I
  recognise", not "no preference was given" - it is shared by `tileLayer()`, `rasterSourceFor()`,
  `vectorStyleFor()`, and `setBase`/`toggleBase`, and it mirrors Python `normalize_layer_mode`
  (`models/markup/meta.py`), whose default is `STREET` and which sanitises `MarkupMap` snapshots
  server-side. Critically, `"dark"` is a valid stored `MapLayerMode` deliberately absent from
  `BASE_ALIASES`, so `normalizeBase("dark") -> "street"` is a semantic mapping - street base, dark
  mode - not a missing-preference fallback. An earlier draft of this fix flipped the constant
  globally; an adversarial review caught that it would have reopened every saved dark-mode map on
  satellite imagery. `normalizeBase` gained an optional `fallback` parameter instead, and only a
  map nobody named a base for takes the new constant.
- **Record defaults**, such as `MarkupMap.layer_mode`, `comment-map.js`'s viewer
  `data.layer_mode || 'street'`, and `services/pins/pin_list_markup.py:30`. These match
  `MarkupMap.layer_mode`'s own model default and describe what a saved record is when unset, not a
  viewer preference to honor.

**Verification, this session:**
- TS: 1,348 tests pass (2 pre-existing `thumb-fallback` contract failures, unrelated). The 6 new
  engine tests were confirmed failing against the unmodified code before the fix.
- Django: 36 targeted tests pass, including new end-to-end tests asserting the album map and the
  wiki map carry the viewer's base. 4 of 7 new tag tests confirmed failing before the fix.
- Browser (Playwright, local dev slot): with `default_map_view = satellite`, the main map, pin
  detail map, and wiki map all opened on satellite; set to `topographic`, all three followed. Set
  to `remember` with nothing stored yet, the main map opened on satellite, stored the choice after
  picking terrain, and restored terrain on reload.

**Left open, not chased:**
- The album map's storage key is the site-wide constant `"ul-album-map-layers"`, not the
  per-profile `ul_layers_v1_<uuid>` the other maps use, so under "remember" two accounts sharing a
  browser share one remembered album base. Pre-existing and separate from this fix.
- A few small non-switchable preview maps still hardcode `tileLayer('street')` and have no layers
  panel to read a default from: the photo-lightbox mini map (`partials/_photo_lightbox.html:463-464`),
  the saved-filter region-draw map (`partials/pin_lists/_saved_filter_dialog_scripts.html:268-270`),
  and the building-import preview (`entries/map-annotations.ts:335-337`). Street is arguably the
  right base for a small reference map, so these were left as-is; listed here so the choice is
  visible rather than forgotten.
- `Profile.map_dark_mode` is visually inert for any viewer on satellite or topographic, because
  `syncBaseLayer()` removes the street/dark base once an opaque layer covers it. A pre-existing
  consequence of the satellite default, not introduced here.

## P139 — The unauthenticated REData media proxies serve whatever Content-Type upstream reports, on the app origin, under a CSP that allows inline script

`id: P139` · `status: open` · `updated: 2026-09-23`

`RedataMediaProxyMixin.serve_media` (`controllers/pin.py`) answers `PinCrisAttachmentView`,
`PinCrisExtractedImageView`, `PinLoopnetPhotoView` and `PinPlaceCidMediaView` with
`HttpResponse(content, content_type=content_type)`, where `content_type` is the `Content-Type`
REData's download response carried (`RedataGateway.download_cultural_resource_attachment` falls
back to `application/octet-stream` only when the header is missing). The routes need no login,
and the site CSP's `script-src` includes `'unsafe-inline'`. An attachment that REData reports as
`text/html` or `image/svg+xml` would therefore render as a page on the app's own origin with inline
script allowed. CRIS attachments are scans that third parties submitted to the state, so the bytes
are not first-party even though REData is.

Not measured: whether REData passes CRIS's own `Content-Type` through or normalises it, and
whether any CRIS record carries an HTML or SVG attachment today.

Article > Sources does not share the gap. `ArticleSourceDocumentView` serves only bytes that open
with `%PDF-`, always as `application/pdf`, under `default-src 'none'; frame-ancestors 'self'`.
Applying the same treatment here would need a failing exploit test first: allow-list image,
video and PDF types, serve anything else as an `application/octet-stream` attachment, and give the
response its own restrictive CSP.

## P141 — The HRSH location-data spec suite still fails on most of its checks, campus-wide

`id: P141` · `status: open` · `updated: 2026-09-23`

A snapshot of `tests/integration/specs/location/` against the former Hudson River State Hospital
campus (41.73328, -73.92812; see `docs/LOCATION_DATA_TESTS.md`, R8), run with
`UL_E2E_LOCATION_DATA=1 bin/run_integration_tests.sh --project location` and a provisioned accounts file.
Recorded as a status list rather than one narrative because the failures are largely independent -
fixing one is unlikely to fix another:

- **CRIS Sources tab shows 0 documents.** `hrsh-sources.spec.ts` polls the Article > Sources subtab
  for its first item and finds none. Worth checking against P24's own note that the bulk
  `fetch-details/` call 403s on a read-only-scoped key and is tolerated silently - not re-confirmed
  this session as the cause here specifically.
- **Building child pins have no outline, so floorplan wall-seeding has nothing to seed from.**
  `hrsh-floorplan-walls.spec.ts:90-91`: none of the campus's building child pins carry a boundary,
  and `_building_outline()` (`controllers/floorplans.py`) refuses to fall back to the parcel line,
  so no floorplan can be seeded until at least one child pin has a BUILDING boundary.
- **The pin's Wikipedia article is missing.** `hrsh-wiki-auto.spec.ts:139`: no article was ever
  seeded from Wikipedia for the campus wiki.
- **The owner record never arrives for the subscriber.** `hrsh-ownership.spec.ts`'s Property
  Records card wait (`waitForPropertyRecordsCard`) times out for the campus pin. Not yet traced to
  a specific line in `services/property/`.
- **`property_records` returns 500 on a building pin.** Observed this session; not yet narrowed to
  a specific view or line - `hrsh-property-data.spec.ts` and `hrsh-panels.spec.ts` are where the
  panel is exercised.
- **Overture building lookups were refused, every one.** Two causes, traced 2026-09-23.
  `_require_narrowing` passed an unset `release` to `overturemaps-py`'s `_get_files_from_stac`, which,
  unlike the library's own read, does not resolve "latest" and asked for
  `stac.overturemaps.org/None/collections.parquet` (404) - so the gateway refused as "index
  unavailable". Fixed: the gateway resolves the release first. Behind it, the 2026-08-19.0 index has
  `collection` null on all 987 rows, so the library's `collection == "building"` filter finds nothing
  anywhere, and a `[]` result then crashes `GeoDataFrame.from_arrow(None)`. Fixed: the gateway reads
  the index itself, matches files on their `theme=/type=` partition path, and reads an empty match as
  an empty frame. Measured after: 17 buildings for the HRSH campus bbox in 3.3 s.
- **Overpass endpoints failing.** Observed this session, not yet correlated with a specific mirror
  or query; see X13/P15 for known Overpass mirror and timeout problems, not confirmed as the same
  cause here.
- **69 smaller legacy parcels remain unrepaired** by `manage.py repair_place_boundaries`. Count is
  from a single run this session, not re-verified, and the command's own selection criteria for
  "legacy" were not re-read afterward to confirm the number is stable.

**Deliberately not pursued this session: Sanborn overlays.** The auto-overlay source needs to be
IIIF/Allmaps-style georeferenced maps, not Library of Congress - see `docs/LOCATION_DATA_TESTS.md`.
Pending Jess's sourcing decision; `hrsh-sanborn.spec.ts` exists but this session's research went no
further than that one sentence and is not preserved beyond it.
